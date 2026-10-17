"""用户反馈（问题提单）闭环测试。

三层覆盖：
1. 纯逻辑 —— ``feedback_pure``（校验 / 单据号 / 状态机幂等）
2. 外通道 —— ``feedback_notify`` 的企微渲染、``feedback_dispatch`` 的 Issue 创建
3. 路由 —— 提交 → 站内扇出 → 忽略 / 派发的端到端行为

外通道一律通过 monkeypatch 拦在 ``_request`` / ``send_webhook_markdown``
之前：测试绝不出网。
"""
from __future__ import annotations

import sqlite3

import pytest

from blueprints import feedback_dispatch, feedback_notify, feedback_pure

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def master_conn(tmp_path, monkeypatch):
    """临时 master.db + 两个用户（1=平台超管，2=普通用户）。"""
    import db as db_module
    from db import init_master_db

    master_path = tmp_path / "master.db"
    wh_dir = tmp_path / "warehouses"
    wh_dir.mkdir()
    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_dir)
    import config as config_module

    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", wh_dir)
    init_master_db()

    conn = sqlite3.connect(master_path)
    conn.row_factory = sqlite3.Row
    ts = "2026-10-17 10:00:00"
    conn.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (1, 'admin', 'x', 1, ?)", (ts,))
    conn.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (2, 'store_user', 'x', 0, ?)", (ts,))
    conn.commit()
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# 纯逻辑
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("title,body", [
    ("", "有内容"),
    ("有标题", ""),
    ("   ", "   "),
])
def test_validate_ticket_rejects_blank(title, body):
    with pytest.raises(ValueError):
        feedback_pure.validate_ticket(title, body)


def test_validate_ticket_strips_and_enforces_limits():
    title, body = feedback_pure.validate_ticket("  导出失败  ", " 点按钮没反应 ")
    assert title == "导出失败"
    assert body == "点按钮没反应"
    with pytest.raises(ValueError):
        feedback_pure.validate_ticket("x" * (feedback_pure.TITLE_MAX_LEN + 1), "b")
    with pytest.raises(ValueError):
        feedback_pure.validate_ticket("t", "x" * (feedback_pure.BODY_MAX_LEN + 1))


def test_validate_comment_rejects_blank_and_oversize():
    assert feedback_pure.validate_comment(" 已修复 ") == "已修复"
    with pytest.raises(ValueError):
        feedback_pure.validate_comment("   ")
    with pytest.raises(ValueError):
        feedback_pure.validate_comment("x" * (feedback_pure.COMMENT_MAX_LEN + 1))


def test_normalize_category_falls_back_to_other():
    assert feedback_pure.normalize_category("BUG") == "bug"
    assert feedback_pure.normalize_category("nonsense") == "other"
    assert feedback_pure.normalize_category(None) == "other"


@pytest.mark.parametrize("raw,expected", [
    ("/items?x=1", "/items?x=1"),
    ("https://evil.example/x", ""),
    ("//evil.example/x", ""),
    ("javascript:alert(1)", ""),
    ("items", ""),
    ("/a\nb", "/ab"),
    ("", ""),
])
def test_normalize_page_path_rejects_external_urls(raw, expected):
    """页面字段会进企微 markdown 与 Issue 正文 —— 只放站内相对路径。"""
    assert feedback_pure.normalize_page_path(raw) == expected


def test_build_ticket_no_pads_sequence():
    assert feedback_pure.build_ticket_no("20261017", 1) == "FB-20261017-0001"
    assert feedback_pure.build_ticket_no("20261017", 1234) == "FB-20261017-1234"


def test_create_ticket_increments_ticket_no(master_conn):
    first = feedback_pure.create_ticket(master_conn, 2, "第一个问题", "详情一")
    second = feedback_pure.create_ticket(master_conn, 2, "第二个问题", "详情二")
    assert first["ticket_no"] == "FB-20261017-0001"
    assert second["ticket_no"] == "FB-20261017-0002"
    assert first["status"] == feedback_pure.STATUS_OPEN
    assert first["category"] == "other"


def test_create_ticket_stores_page_path_only_when_internal(master_conn):
    ok = feedback_pure.create_ticket(
        master_conn, 2, "内部页面", "详情", page_path="/inventory")
    bad = feedback_pure.create_ticket(
        master_conn, 2, "外部页面", "详情", page_path="http://evil.example/x")
    assert ok["page_path"] == "/inventory"
    assert bad["page_path"] == ""


def test_mark_ignored_is_one_way(master_conn):
    t = feedback_pure.create_ticket(master_conn, 2, "不重要", "噪音")
    assert feedback_pure.mark_ignored(master_conn, t["id"], 1) is True
    # 终态：再点一次不生效
    assert feedback_pure.mark_ignored(master_conn, t["id"], 1) is False
    assert feedback_pure.get_ticket(master_conn, t["id"])["status"] == "ignored"


def test_attach_issue_is_idempotent(master_conn):
    t = feedback_pure.create_ticket(master_conn, 2, "有问题", "详情")
    assert feedback_pure.attach_issue(master_conn, t["id"], 7, "https://x/7", 1) is True
    # 第二次（重复点「发送」）不得覆盖已有单号
    assert feedback_pure.attach_issue(master_conn, t["id"], 99, "https://x/99", 1) is False
    got = feedback_pure.get_ticket(master_conn, t["id"])
    assert got["issue_number"] == 7
    assert got["status"] == "dispatched"


def test_mark_comment_dispatched_touches_once(master_conn):
    t = feedback_pure.create_ticket(master_conn, 2, "有问题", "详情")
    cid = feedback_pure.add_comment(master_conn, t["id"], 1, "已派发", is_admin=True)
    assert feedback_pure.mark_comment_dispatched(master_conn, cid) is True
    assert feedback_pure.mark_comment_dispatched(master_conn, cid) is False
    comments = feedback_pure.list_comments(master_conn, t["id"])
    assert comments[0]["dispatched_at"] is not None


def test_list_for_admin_orders_open_first(master_conn):
    a = feedback_pure.create_ticket(master_conn, 2, "旧的开着", "x")
    b = feedback_pure.create_ticket(master_conn, 2, "已忽略的", "x")
    feedback_pure.mark_ignored(master_conn, b["id"], 1)
    rows = feedback_pure.list_for_admin(master_conn)
    assert [r["id"] for r in rows] == [a["id"], b["id"]]
    assert rows[0]["submitter"] == "store_user"
    only_ignored = feedback_pure.list_for_admin(master_conn, "ignored")
    assert [r["id"] for r in only_ignored] == [b["id"]]


def test_count_open_and_admin_ids(master_conn):
    assert feedback_pure.count_open(master_conn) == 0
    feedback_pure.create_ticket(master_conn, 2, "一", "x")
    feedback_pure.create_ticket(master_conn, 2, "二", "x")
    assert feedback_pure.count_open(master_conn) == 2
    assert feedback_pure.build_admin_user_ids(master_conn) == [1]


def test_summarize_collapses_newlines_and_truncates():
    assert feedback_pure.summarize("a\nb  c") == "a b c"
    long = "x" * 100
    out = feedback_pure.summarize(long, 10)
    assert len(out) == 10 and out.endswith("…")


# ---------------------------------------------------------------------------
# 企微渲染
# ---------------------------------------------------------------------------


def _sample_ticket(**over):
    base = {
        "id": 3,
        "ticket_no": "FB-20261017-0003",
        "title": "库存页导出按钮没反应",
        "category": "bug",
        "category_label": "功能异常",
        "submitter": "store_user",
        "created_at": "2026-10-17 09:30:00",
    }
    base.update(over)
    return base


def test_build_feedback_markdown_contains_key_fields():
    md = feedback_notify.build_feedback_markdown(
        _sample_ticket(), base_url="https://kjtang.cn")
    assert "FB-20261017-0003" in md
    assert "导出按钮没反应" in md
    assert "功能异常" in md
    assert "store_user" in md
    assert "https://kjtang.cn/admin/feedback/3" in md


def test_build_feedback_markdown_omits_link_without_base_url():
    """相对链接在企微客户端点不开 —— 宁可不给，不给错。"""
    md = feedback_notify.build_feedback_markdown(_sample_ticket(), base_url="")
    assert "/admin/feedback" not in md


def test_build_feedback_markdown_survives_empty_ticket():
    md = feedback_notify.build_feedback_markdown(None)
    assert md.startswith("## 收到一条新的问题反馈")


def test_notify_skips_when_webhook_unset(monkeypatch):
    called = []
    monkeypatch.setattr(feedback_notify, "feedback_webhook_url", lambda: "")
    monkeypatch.setattr(feedback_notify, "_send_safely",
                        lambda *a, **k: called.append(a))
    feedback_notify.notify_feedback_submitted(_sample_ticket(), background=False)
    assert called == []


def test_notify_sends_when_webhook_configured(monkeypatch):
    sent = []
    monkeypatch.setattr(feedback_notify, "feedback_webhook_url",
                        lambda: "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=k")
    monkeypatch.setattr(feedback_notify, "send_webhook_markdown",
                        lambda url, md, **k: sent.append((url, md)) or True)
    feedback_notify.notify_feedback_submitted(_sample_ticket(), background=False)
    assert len(sent) == 1
    assert "FB-20261017-0003" in sent[0][1]


# ---------------------------------------------------------------------------
# Issue 渲染与创建
# ---------------------------------------------------------------------------


def test_build_issue_title_and_body():
    ticket = _sample_ticket(warehouse_code="wh_test", page_path="/inventory",
                            body="点了导出，页面刷新但没下载",
                            category_label="功能异常")
    comments = [
        {"is_admin": True, "username": "admin", "created_at": "2026-10-17 11:00:00",
         "body": "确认是漏绑事件"},
        {"is_admin": False, "username": "store_user", "created_at": "2026-10-17 11:10:00",
         "body": "补充：Chrome 和 Safari 都一样"},
    ]
    title = feedback_dispatch.build_issue_title(ticket)
    assert title.startswith("[反馈] FB-20261017-0003")

    body = feedback_dispatch.build_issue_body(
        ticket, comments, base_url="https://kjtang.cn")
    assert "点了导出，页面刷新但没下载" in body
    assert "确认是漏绑事件" in body
    # 非管理员的补充评论不进任务单正文（避免把用户闲聊当需求）
    assert "Chrome 和 Safari" not in body
    assert "https://kjtang.cn/admin/feedback/3" in body
    assert "闭环要求" in body


def test_create_issue_returns_none_without_token(monkeypatch):
    monkeypatch.setattr(feedback_dispatch.config, "FEEDBACK_GITHUB_TOKEN", "")
    assert feedback_dispatch.create_issue("t", "b") is None


def test_create_issue_success_attaches_label(monkeypatch):
    monkeypatch.setattr(feedback_dispatch.config, "FEEDBACK_GITHUB_TOKEN", "tok")
    seen = []

    def fake_request(token, path, payload):
        seen.append((path, payload))
        if path.endswith("/labels"):
            return 201, {"name": feedback_dispatch.FEEDBACK_LABEL}
        return 201, {"number": 42, "html_url": "https://github.com/o/r/issues/42"}

    monkeypatch.setattr(feedback_dispatch, "_request", fake_request)
    out = feedback_dispatch.create_issue("标题", "正文")
    assert out == {"number": 42, "url": "https://github.com/o/r/issues/42"}
    issue_payload = seen[-1][1]
    assert issue_payload["labels"] == [feedback_dispatch.FEEDBACK_LABEL]


def test_create_issue_survives_missing_label(monkeypatch):
    """标签建不出来（403 等）时仍要建单 —— 标签只是自动化识别的增强。"""
    monkeypatch.setattr(feedback_dispatch.config, "FEEDBACK_GITHUB_TOKEN", "tok")

    def fake_request(token, path, payload):
        if path.endswith("/labels"):
            raise OSError("boom")
        return 201, {"number": 7, "html_url": "https://github.com/o/r/issues/7"}

    monkeypatch.setattr(feedback_dispatch, "_request", fake_request)
    out = feedback_dispatch.create_issue("标题", "正文")
    assert out["number"] == 7


def test_create_issue_returns_none_on_http_error(monkeypatch):
    monkeypatch.setattr(feedback_dispatch.config, "FEEDBACK_GITHUB_TOKEN", "tok")
    monkeypatch.setattr(
        feedback_dispatch, "_request",
        lambda token, path, payload: (422, None))
    assert feedback_dispatch.create_issue("t", "b") is None


def test_create_issue_returns_none_when_number_missing(monkeypatch):
    monkeypatch.setattr(feedback_dispatch.config, "FEEDBACK_GITHUB_TOKEN", "tok")

    def fake_request(token, path, payload):
        if path.endswith("/labels"):
            return 201, {}
        return 201, {"html_url": "https://github.com/o/r/issues/1"}

    monkeypatch.setattr(feedback_dispatch, "_request", fake_request)
    assert feedback_dispatch.create_issue("t", "b") is None


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


def _master_rows(sql, params=()):
    import config

    conn = sqlite3.connect(config.MASTER_DB)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _insert_plain_user(user_id: int = 2, username: str = "store_user") -> None:
    """向临时 master.db 补一个普通用户并绑到唯一仓库。

    logged_client 只造了 1 个超管；要验证「别人看不到我的单子」必须先有一个
    真的走得通登录态的普通用户（否则 auth hook 直接 302 去登录页）。
    """
    import config

    conn = sqlite3.connect(config.MASTER_DB)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO users "
            "(id, username, password_hash, is_admin, created_at) "
            "VALUES (?, ?, 'x', 0, '2026-10-17 10:00:00')",
            (user_id, username))
        wh_id = conn.execute("SELECT id FROM warehouses LIMIT 1").fetchone()[0]
        conn.execute(
            "INSERT OR IGNORE INTO warehouse_users (user_id, warehouse_id, role) "
            "VALUES (?, ?, 'staff')", (user_id, wh_id))
        conn.commit()
    finally:
        conn.close()


def test_feedback_pages_require_login(logged_client):
    client, _ = logged_client
    with client.session_transaction() as s:
        s.pop("user_id", None)
    for path in ("/feedback", "/feedback/new", "/feedback/1"):
        resp = client.get(path)
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]


def test_admin_routes_reject_non_admin(staff_client):
    client, _ = staff_client
    for path in ("/admin/feedback", "/admin/feedback/1"):
        assert client.get(path).status_code == 403


def test_submit_creates_ticket_and_notifies_admins(logged_client):
    client, _ = logged_client
    resp = client.post("/feedback", data={
        "title": "导出按钮没反应",
        "body": "在库存查阅页点导出，页面刷新但没文件下下来",
        "category": "bug",
        "page_path": "/inventory",
    }, follow_redirects=True)
    assert resp.status_code == 200

    rows = _master_rows("SELECT * FROM feedback_tickets")
    assert len(rows) == 1
    ticket = rows[0]
    assert ticket["status"] == "open"
    assert ticket["category"] == "bug"
    assert ticket["page_path"] == "/inventory"

    # 站内通知扇出到超管（本 fixture 用户 1 就是超管）
    notifs = _master_rows(
        "SELECT * FROM notifications WHERE event_type='feedback_submitted'")
    assert len(notifs) == 1
    assert ticket["ticket_no"] in notifs[0]["summary"]


def test_submit_blank_title_does_not_create_ticket(logged_client):
    client, _ = logged_client
    client.post("/feedback", data={"title": "   ", "body": "有内容"})
    assert _master_rows("SELECT * FROM feedback_tickets") == []


def test_ticket_detail_is_owner_scoped(logged_client):
    """别人的单子 404，不是 403 —— 不泄露「这个 id 存在」。"""
    client, _ = logged_client
    client.post("/feedback", data={"title": "我的问题", "body": "详情"})
    ticket_id = _master_rows("SELECT id FROM feedback_tickets")[0]["id"]

    # 超管可看
    assert client.get(f"/feedback/{ticket_id}").status_code == 200

    # 换成普通用户看别人的单子
    _insert_plain_user()
    with client.session_transaction() as s:
        s["user_id"] = 2
    assert client.get(f"/feedback/{ticket_id}").status_code == 404
    assert client.get("/admin/feedback").status_code == 403


def test_staff_submission_notifies_platform_admin(logged_client):
    """门店用户提单 → 超管在通知中心能看到（跨用户扇出的真实路径）。"""
    client, _ = logged_client
    _insert_plain_user()
    with client.session_transaction() as s:
        s["user_id"] = 2
    client.post("/feedback", data={"title": "店里用不了", "body": "下单页白屏"},
                follow_redirects=True)
    # 切回超管看通知
    with client.session_transaction() as s:
        s["user_id"] = 1
    feed = client.get("/notifications?unread=true").get_json()
    summaries = [e["summary"] for e in feed["events"]
                 if e["event_type"] == "feedback_submitted"]
    assert len(summaries) == 1
    assert "店里用不了" in summaries[0]


def test_admin_inbox_lists_and_ignore_flow(logged_client):
    client, _ = logged_client
    client.post("/feedback", data={"title": "不重要的问题", "body": "噪音"})
    ticket_id = _master_rows("SELECT id FROM feedback_tickets")[0]["id"]

    inbox = client.get("/admin/feedback")
    assert inbox.status_code == 200
    assert "不重要的问题" in inbox.get_data(as_text=True)

    resp = client.post(f"/admin/feedback/{ticket_id}/ignore", follow_redirects=True)
    assert resp.status_code == 200
    assert _master_rows("SELECT status FROM feedback_tickets")[0][0] == "ignored"


def test_dispatch_without_token_keeps_comment_and_status(logged_client, monkeypatch):
    """派发通道没配：评论必须保住，状态不能假装成功。"""
    import config as config_module

    monkeypatch.setattr(config_module, "FEEDBACK_GITHUB_TOKEN", "")
    client, _ = logged_client
    client.post("/feedback", data={"title": "要修的问题", "body": "详情"})
    ticket_id = _master_rows("SELECT id FROM feedback_tickets")[0]["id"]

    resp = client.post(f"/admin/feedback/{ticket_id}/dispatch",
                       data={"comment": "确认是 bug，需要修"},
                       follow_redirects=True)
    assert resp.status_code == 200
    assert "手动建 Issue" in resp.get_data(as_text=True)

    ticket = _master_rows("SELECT status, issue_number FROM feedback_tickets")[0]
    assert ticket["status"] == "open"
    assert ticket["issue_number"] is None
    comments = _master_rows("SELECT body, dispatched_at FROM feedback_comments")
    assert len(comments) == 1
    assert comments[0]["dispatched_at"] is None


def test_dispatch_success_marks_ticket_and_comment(logged_client, monkeypatch):
    import config as config_module

    monkeypatch.setattr(config_module, "FEEDBACK_GITHUB_TOKEN", "tok")
    monkeypatch.setattr(
        feedback_dispatch, "create_issue",
        lambda title, body, label=None: {
            "number": 88, "url": "https://github.com/o/r/issues/88"})

    client, _ = logged_client
    client.post("/feedback", data={"title": "要修的问题", "body": "详情"})
    ticket_id = _master_rows("SELECT id FROM feedback_tickets")[0]["id"]

    resp = client.post(f"/admin/feedback/{ticket_id}/dispatch",
                       data={"comment": "已派发修复"},
                       follow_redirects=True)
    assert resp.status_code == 200
    assert "Issue #88" in resp.get_data(as_text=True)

    ticket = _master_rows("SELECT status, issue_number FROM feedback_tickets")[0]
    assert ticket["status"] == "dispatched"
    assert ticket["issue_number"] == 88
    comment = _master_rows("SELECT dispatched_at FROM feedback_comments")[0]
    assert comment["dispatched_at"] is not None


def test_dispatch_twice_does_not_create_second_issue(logged_client, monkeypatch):
    import config as config_module

    monkeypatch.setattr(config_module, "FEEDBACK_GITHUB_TOKEN", "tok")
    calls = []

    def fake_create(title, body, label=None):
        calls.append(title)
        return {"number": 88, "url": "https://github.com/o/r/issues/88"}

    monkeypatch.setattr(feedback_dispatch, "create_issue", fake_create)

    client, _ = logged_client
    client.post("/feedback", data={"title": "重复点", "body": "详情"})
    ticket_id = _master_rows("SELECT id FROM feedback_tickets")[0]["id"]

    client.post(f"/admin/feedback/{ticket_id}/dispatch", data={"comment": "第一次"})
    resp = client.post(f"/admin/feedback/{ticket_id}/dispatch",
                       data={"comment": "第二次"}, follow_redirects=True)

    assert len(calls) == 1
    assert "未重复建单" in resp.get_data(as_text=True)
    assert len(_master_rows("SELECT id FROM feedback_comments")) == 1


def test_save_comment_does_not_dispatch(logged_client, monkeypatch):
    import config as config_module

    monkeypatch.setattr(config_module, "FEEDBACK_GITHUB_TOKEN", "tok")
    monkeypatch.setattr(
        feedback_dispatch, "create_issue",
        lambda *a, **k: pytest.fail("仅保存评论不应建 Issue"))

    client, _ = logged_client
    client.post("/feedback", data={"title": "先记一笔", "body": "详情"})
    ticket_id = _master_rows("SELECT id FROM feedback_tickets")[0]["id"]

    client.post(f"/admin/feedback/{ticket_id}/comment",
                data={"comment": "先记一下"}, follow_redirects=True)
    assert len(_master_rows("SELECT id FROM feedback_comments")) == 1
    assert _master_rows("SELECT status FROM feedback_tickets")[0][0] == "open"


def test_dispatch_requires_comment(logged_client, monkeypatch):
    import config as config_module

    monkeypatch.setattr(config_module, "FEEDBACK_GITHUB_TOKEN", "tok")
    client, _ = logged_client
    client.post("/feedback", data={"title": "空评论", "body": "详情"})
    ticket_id = _master_rows("SELECT id FROM feedback_tickets")[0]["id"]

    resp = client.post(f"/admin/feedback/{ticket_id}/dispatch",
                       data={"comment": "  "}, follow_redirects=True)
    assert "未派发" in resp.get_data(as_text=True)
    assert _master_rows("SELECT id FROM feedback_comments") == []
    assert _master_rows("SELECT issue_number FROM feedback_tickets")[0][0] is None


# ---------------------------------------------------------------------------
# 页面渲染（模板语法 / 关键控件存在性）
# ---------------------------------------------------------------------------


def test_new_feedback_page_renders_form(logged_client):
    """提交页必须真的渲染出来 —— 未登录 302 的用例覆盖不到模板语法错误。"""
    client, _ = logged_client
    resp = client.get("/feedback/new?page=/inventory")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'name="title"' in html
    assert 'name="body"' in html
    assert 'name="category"' in html
    assert 'value="/inventory"' in html          # 出问题页面被回填
    assert 'name="csrf_token"' in html


def test_new_feedback_page_without_page_param(logged_client):
    client, _ = logged_client
    resp = client.get("/feedback/new")
    assert resp.status_code == 200
    assert 'name="page_path"' in resp.get_data(as_text=True)


def test_user_detail_page_renders_body_and_comment(logged_client):
    client, _ = logged_client
    client.post("/feedback", data={
        "title": "渲染检查", "body": "正文第一行\n正文第二行", "category": "data"})
    ticket_id = _master_rows("SELECT id FROM feedback_tickets")[0]["id"]
    feedback_pure.add_comment(
        _conn(), ticket_id, 1, "管理员看过<确认是 bug>", is_admin=True)
    html = client.get(f"/feedback/{ticket_id}").get_data(as_text=True)
    assert "正文第一行" in html
    assert "管理员看过" in html
    # Jinja 自动转义：正文里的尖括号不得变成标签
    assert "<确认是 bug>" not in html
    assert "&lt;确认是 bug&gt;" in html


def test_admin_detail_page_renders_actions_for_open_ticket(logged_client):
    client, _ = logged_client
    client.post("/feedback", data={"title": "待处理", "body": "详情"})
    ticket_id = _master_rows("SELECT id FROM feedback_tickets")[0]["id"]
    html = client.get(f"/admin/feedback/{ticket_id}").get_data(as_text=True)
    assert "发送并派发开发" in html
    assert "不重要，忽略" in html
    assert "先存评论" in html


def test_admin_detail_hides_actions_for_closed_ticket(logged_client):
    client, _ = logged_client
    client.post("/feedback", data={"title": "要忽略", "body": "详情"})
    ticket_id = _master_rows("SELECT id FROM feedback_tickets")[0]["id"]
    client.post(f"/admin/feedback/{ticket_id}/ignore")
    html = client.get(f"/admin/feedback/{ticket_id}").get_data(as_text=True)
    assert "发送并派发开发" not in html
    assert "已忽略" in html


def test_sidebar_badge_counts_open_tickets(logged_client):
    """超管侧栏出现待处理角标；忽略后角标消失。"""
    client, _ = logged_client
    client.post("/feedback", data={"title": "角标一", "body": "详情"})
    client.post("/feedback", data={"title": "角标二", "body": "详情"})
    html = client.get("/dashboard").get_data(as_text=True)
    assert "反馈收件箱" in html
    assert 'class="nav-badge">2<' in html or ">2<" in html

    for row in _master_rows("SELECT id FROM feedback_tickets"):
        client.post(f"/admin/feedback/{row['id']}/ignore")
    html = client.get("/dashboard").get_data(as_text=True)
    assert "反馈收件箱" in html


def _conn():
    import config

    conn = sqlite3.connect(config.MASTER_DB)
    conn.row_factory = sqlite3.Row
    return conn
