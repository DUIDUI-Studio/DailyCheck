"""Issue #93 回归：``page_path`` 的语义边界与派发侧的纠正入口。

为什么单列一个文件：这里锁的不是「探测能不能跑」，而是**探测什么时候不能
被相信**。Issue #93 的教训是「给了错误线索比不给线索更坏」—— 自动定位把
用户在仓库选择页点的反馈，认成了 `blueprints/auth.py` 的问题，而真凶是
`templates/store_ordering/dc_items.html` 里一个漏了 `data-ajax` 的 form。

覆盖四组：

1. 中转页（登录 / 仓库选择）必须拒绝出线索，且正文要明说「别用这个路径」
2. 三种「没线索」（没路径 / 没路由 / 中转页）文案必须可区分
3. 管理员「插入结构模板」留下的空骨架不得进正文
4. 管理员可覆盖「实际页面」重新定位，且该覆盖受站内相对路径约束

``with_commits=False`` 是刻意的：不依赖宿主仓库的 git 历史。
"""
from __future__ import annotations

import sqlite3

import pytest

from blueprints import feedback_dispatch, feedback_introspect


def _submit(client, page_path="/inventory"):
    client.post("/feedback", data={
        "title": "库存页导出没反应",
        "body": "点了导出按钮，没有任何反应，连下载提示都没有。",
        "category": "bug",
        "page_path": page_path,
    })


def _ticket_id():
    import config

    conn = sqlite3.connect(config.MASTER_DB)
    try:
        return conn.execute("SELECT id FROM feedback_tickets").fetchone()[0]
    finally:
        conn.close()


# Issue #93 回归：page_path 是「提交时所在页面」，不是「出问题页面」
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("page", [
    "/select-warehouse",
    "/login",
    "/logout",
])
def test_transient_page_produces_no_clue(logged_client, page):
    """中转页（登录 / 仓库选择）必须拒绝出线索。

    Issue #93 实况：用户在 `/select-warehouse` 顺手提了反馈，自动定位把
    `blueprints/auth.py` 当成出问题的代码 —— 而真凶在
    `templates/store_ordering/dc_items.html`。给错线索比不给更坏：
    开发侧会照着改错文件，还自以为在验收。
    """
    client, _ = logged_client
    clues = feedback_introspect.collect(
        client.application, page, with_commits=False)
    assert clues["matched"] is False
    assert clues["reason"] == feedback_introspect.REASON_TRANSIENT
    # 关键：不得带出 auth.py 之类的"线索"
    assert "source" not in clues
    assert "endpoint" not in clues


def test_transient_page_body_warns_loudly(logged_client):
    client, _ = logged_client
    clues = feedback_introspect.collect(
        client.application, "/select-warehouse", with_commits=False)
    body = feedback_dispatch.build_issue_body(
        {"ticket_no": "FB-T-1", "title": "开关点了回顶部",
         "body": "管理品项时每点一个开关就回到页面顶部。",
         "page_path": "/select-warehouse"},
        [{"is_admin": True, "username": "eric", "created_at": "now",
          "body": "确认是前端体验问题，修一下。"}],
        code_clues=clues,
    )
    assert "是纯中转页" in body
    assert "不能作为出问题位置的证据" in body
    assert "以「现象」为准" in body
    # auth.py 不得作为"视图实现"出现在正文里
    assert "blueprints/auth.py" not in body


def test_unmatched_and_missing_path_keep_distinct_wording(logged_client):
    """三种「没线索」不能混成一句：可信度完全不同。"""
    client, _ = logged_client
    unknown = feedback_introspect.collect(
        client.application, "/no-such-page", with_commits=False)
    missing = feedback_introspect.collect(client.application, "", with_commits=False)
    assert unknown["reason"] == feedback_introspect.REASON_UNMATCHED
    assert missing["reason"] == feedback_introspect.REASON_NO_PATH

    unknown_body = feedback_dispatch.build_issue_body(
        {"ticket_no": "FB-U", "title": "t", "body": "b", "page_path": "/no-such-page"},
        [], code_clues=unknown)
    missing_body = feedback_dispatch.build_issue_body(
        {"ticket_no": "FB-M", "title": "t", "body": "b"}, [], code_clues=missing)
    assert "/no-such-page" in unknown_body
    assert "没有记录出问题的页面" in missing_body


def test_body_labels_page_as_submission_page(logged_client):
    """正文必须写「提交时所在页面」，不能写「页面」—— 措辞本身就是警示。"""
    client, _ = logged_client
    clues = feedback_introspect.collect(
        client.application, "/inventory", with_commits=False)
    body = feedback_dispatch.build_issue_body(
        {"ticket_no": "FB-L", "title": "t", "body": "b", "page_path": "/inventory"},
        [], code_clues=clues)
    assert "提交时所在页面" in body
    assert "以现象为准" in body


def test_acceptance_does_not_assert_the_wrong_page(logged_client):
    """验收标准不得把自动定位的页面当成事实。

    上一版第一条写「站内 `/select-warehouse` 页面按现象描述逐项复现」——
    真按这个验收，改错文件也算通过。
    """
    client, _ = logged_client
    clues = feedback_introspect.collect(
        client.application, "/inventory", with_commits=False)
    body = feedback_dispatch.build_issue_body(
        {"ticket_no": "FB-A", "title": "t", "body": "b", "page_path": "/inventory"},
        [], code_clues=clues)
    assert "站内 `/inventory` 页面按「现象」描述逐项复现" not in body
    assert "确实就是现象所在页面" in body


# ---------------------------------------------------------------------------
# 空骨架剥离（「插入结构模板」留下的三行空标签）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("现象：\n影响范围：\n期望结果：\n用户点开关回顶部", "用户点开关回顶部"),
    ("现象:\n影响范围:  \n期望结果：\n真内容", "真内容"),
    # 有内容的标签行必须原样保留
    ("现象：点导出没反应\n影响范围：只影响报表页", "现象：点导出没反应\n影响范围：只影响报表页"),
    # 行中间的冒号不误伤
    ("现象：a\n影响范围：b\n期望结果：c", "现象：a\n影响范围：b\n期望结果：c"),
])
def test_strip_empty_structure(raw, expected):
    from blueprints.feedback_issue_body import strip_empty_structure

    assert strip_empty_structure(raw) == expected


def test_strip_empty_structure_handles_empty_input():
    from blueprints.feedback_issue_body import strip_empty_structure

    assert strip_empty_structure(None) == ""
    assert strip_empty_structure("") == ""
    assert strip_empty_structure("现象：\n影响范围：\n期望结果：") == ""


def test_admin_note_with_only_skeleton_is_dropped():
    """全是空骨架的评论等于没写 —— 不能当成「管理员判定」进正文。"""
    notes = feedback_dispatch.admin_notes([
        {"is_admin": True, "username": "eric", "body": "现象：\n影响范围：\n期望结果："},
        {"is_admin": True, "username": "eric", "body": "现象：\n确认是漏绑事件"},
    ])
    assert len(notes) == 1
    assert notes[0]["body"] == "确认是漏绑事件"


def test_body_does_not_contain_skeleton_lines(logged_client):
    client, _ = logged_client
    body = feedback_dispatch.build_issue_body(
        {"ticket_no": "FB-S", "title": "t", "body": "b"},
        [{"is_admin": True, "username": "eric", "created_at": "now",
          "body": "现象：\n影响范围：\n期望结果：\n用户在管理采购品项时点开关会回顶部"}],
        code_clues={},
    )
    assert "用户在管理采购品项时点开关会回顶部" in body
    # 空的「影响范围：」不得独占一行
    assert "\n- **eric**（now）：影响范围：" not in body
    assert "影响范围：" not in body


# ---------------------------------------------------------------------------
# 管理员覆盖「实际页面」
# ---------------------------------------------------------------------------


def test_dispatch_uses_page_override(logged_client, monkeypatch):
    """管理员手填实际页面 → 用它重新定位，而不是用提交时的路径。"""
    client, _ = logged_client
    _submit(client, page_path="/select-warehouse")
    captured: dict = {}

    def fake_create_issue(title, body, label=None):
        captured["body"] = body
        return {"number": 999, "url": "https://example.invalid/999"}

    monkeypatch.setattr(feedback_dispatch, "create_issue", fake_create_issue)
    monkeypatch.setattr(feedback_dispatch, "dispatch_enabled", lambda: True)

    resp = client.post(f"/admin/feedback/{_ticket_id()}/dispatch", data={
        "comment": "确认是前端体验问题，修一下。",
        "page_override": "/inventory",
    }, follow_redirects=True)
    assert resp.status_code == 200
    body = captured["body"]
    assert "实际页面（管理员指定）" in body
    assert "`/inventory`" in body
    assert "items.py" in body          # 覆盖后定位到真实视图
    assert "blueprints/auth.py" not in body   # 原中转页不再出现在线索里


def test_dispatch_ignores_external_page_override(logged_client, monkeypatch):
    """覆盖值同样受「只可能是站内相对路径」约束 —— 这个字段会进 Issue 正文。"""
    client, _ = logged_client
    _submit(client, page_path="/inventory")
    captured: dict = {}

    def fake_create_issue(title, body, label=None):
        captured["body"] = body
        return {"number": 1000, "url": "https://example.invalid/1000"}

    monkeypatch.setattr(feedback_dispatch, "create_issue", fake_create_issue)
    monkeypatch.setattr(feedback_dispatch, "dispatch_enabled", lambda: True)

    client.post(f"/admin/feedback/{_ticket_id()}/dispatch", data={
        "comment": "确认是 bug。",
        "page_override": "https://evil.example/x",
    }, follow_redirects=True)
    assert "evil.example" not in captured["body"]


# ---------------------------------------------------------------------------
# DC 品项开关：缺 data-ajax 导致整页重载（Issue #93 本体）
# ---------------------------------------------------------------------------


def test_dc_items_toggle_uses_ajax_reload_stay():
    """模板层断言：这个 form 必须带 data-ajax，否则点一次就整页回到顶部。

    pytest 抓不到 JS 行为，所以锁住"标记存在"这个可静态检查的前提 ——
    少了它，ajax-form.js 根本不会接管该表单。
    """
    from pathlib import Path

    import config

    html = (Path(config.BASE_DIR) / "templates" / "store_ordering" / "dc_items.html").read_text(
        encoding="utf-8")
    marker = html.find("store_ordering.manage_dc_items")
    assert marker != -1, "dc_items.html 里应存在可订开关表单"
    form_tag = html[html.rfind("<form", 0, marker):html.find(">", marker)]
    assert "data-ajax" in form_tag
    assert 'data-ajax-mode="reload-stay"' in form_tag
