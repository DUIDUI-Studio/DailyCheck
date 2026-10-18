"""Issue #108：反馈悬浮窗（就地提交，不打断当前操作）。

为什么单列一个文件：这里锁的不是「弹层长什么样」（CSS 缺陷 pytest 抓不到），
而是**改造后两条提交路径的契约**：

1. ``POST /feedback/api`` —— 悬浮窗用，JSON 进出，400 表示校验失败；
2. ``POST /feedback`` —— 原表单端点，行为**一个字都不能变**（302 + flash），
   因为它是无 JS / 弹层脚本加载失败时的降级通道。

另外钉住模板里两个易被后人「顺手清理」的东西：
``data-feedback-open``（点击拦截入口）与表单 ``action`` 仍指向 PRG 端点。
"""
from __future__ import annotations

import re
import sqlite3

# ---------------------------------------------------------------------------
# 夹具工具
# ---------------------------------------------------------------------------


def _tickets():
    """读临时 master.db 里的反馈单（顺序 = 插入顺序）。"""
    import config

    conn = sqlite3.connect(config.MASTER_DB)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT ticket_no, title, body, category, page_path, status "
            "FROM feedback_tickets ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def _post_api(client, **data):
    payload = {
        "title": "导出按钮没反应",
        "body": "在库存查阅页点导出，页面刷新但没文件下下来",
        "category": "bug",
        "page_path": "/inventory",
    }
    payload.update(data)
    return client.post("/feedback/api", data=payload)


# ---------------------------------------------------------------------------
# JSON 提交端点
# ---------------------------------------------------------------------------


def test_api_creates_ticket_and_returns_ticket_no(logged_client):
    """成功路径：200 + ok=true + 单号；单据真的落库（不是只回一句好听话）。"""
    client, _ = logged_client
    resp = _post_api(client)

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert re.fullmatch(r"FB-\d{8}-\d{4}", body["ticket_no"]), body
    assert body["ticket_no"] in body["message"]

    rows = _tickets()
    assert len(rows) == 1
    assert rows[0]["ticket_no"] == body["ticket_no"]
    assert rows[0]["title"] == "导出按钮没反应"
    assert rows[0]["page_path"] == "/inventory"
    assert rows[0]["status"] == "open"


def test_api_rejects_blank_title_with_400(logged_client):
    """校验失败必须是 4xx —— 若回 200，前端会把「没提交成功」当成功关掉弹层。"""
    client, _ = logged_client
    resp = _post_api(client, title="   ", body="有内容")

    assert resp.status_code == 400
    body = resp.get_json()
    assert body["ok"] is False
    assert "提交失败" in body["message"]
    assert _tickets() == []


def test_api_strips_external_page_path(logged_client):
    """``page_path`` 会进企微 markdown 与 Issue 正文 —— 站外 URL 必须清空。"""
    client, _ = logged_client
    resp = _post_api(client, page_path="https://evil.example/x")

    assert resp.status_code == 200
    assert _tickets()[0]["page_path"] == ""


def test_api_requires_login(logged_client):
    """未登录：登录跳转（与其余 feedback 路由一致），不建单。"""
    client, _ = logged_client
    with client.session_transaction() as s:
        s.pop("user_id", None)

    resp = client.post("/feedback/api", data={"title": "t", "body": "b"})
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]
    assert _tickets() == []


def test_api_unauthenticated_response_cannot_look_like_success(logged_client):
    """**核心回归（code review HIGH）**：未登录时响应绝不能像「已提交成功」。

    背景：``blueprints/auth.py`` 的 ``before_app_request`` 在蓝图钩子之前把
    未登录请求 302 到登录页，全局 ``register_ajax_handlers`` 再把这个 3xx
    改写成 **200 + {ok: true, location}**。所以「有没有单号」是前端唯一
    可信的判据 —— 本用例把这条契约钉死在服务端。
    """
    client, _ = logged_client
    with client.session_transaction() as s:
        s.pop("user_id", None)

    resp = client.post(
        "/feedback/api",
        data={"title": "t", "body": "b"},
        headers={"X-Requested-With": "fetch", "Accept": "application/json"},
    )
    body = resp.get_json()

    assert body is not None, "AJAX 调用必须拿到 JSON（哪怕是失败信封）"
    assert "ticket_no" not in body, "没有建单却给了单号 = 前端会误报成功"
    assert _tickets() == []


def test_api_without_warehouse_returns_409(logged_client):
    """登录但没选仓：409 + 明确提示，而不是被改写成假成功。"""
    client, _ = logged_client
    with client.session_transaction() as s:
        s.pop("warehouse_id", None)

    resp = client.post(
        "/feedback/api",
        data={"title": "t", "body": "b"},
        headers={"X-Requested-With": "fetch", "Accept": "application/json"},
    )

    assert resp.status_code == 409
    body = resp.get_json()
    assert body["ok"] is False
    assert body["reason"] == "no_warehouse"
    assert "ticket_no" not in body
    assert _tickets() == []


def test_api_rejects_missing_csrf_token_when_enforced(logged_client):
    """CSRF 打开时无 token 必须被拦（弹层表单带 csrf_token，不能靠豁免混过去）。"""
    client, _ = logged_client
    app = client.application
    app.config["WTF_CSRF_FORCE_ENABLED"] = True
    try:
        denied = client.post("/feedback/api", data={"title": "t", "body": "b"})
        assert denied.status_code == 400
        assert _tickets() == []
    finally:
        app.config["WTF_CSRF_FORCE_ENABLED"] = False


# ---------------------------------------------------------------------------
# 原表单端点：回归（降级通道不能坏）
# ---------------------------------------------------------------------------


def test_plain_form_endpoint_keeps_prg_behaviour(logged_client):
    """``POST /feedback`` 仍是 302 回「我的反馈」+ flash —— 无 JS 时靠它。"""
    client, _ = logged_client
    resp = client.post("/feedback", data={
        "title": "店里用不了",
        "body": "下单页白屏",
        "category": "bug",
    })

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/feedback")
    assert len(_tickets()) == 1


def test_plain_form_endpoint_keeps_success_flash(logged_client):
    """降级通道的提示文案也要钉住 —— 删了 flash 用户会以为没提交成功。"""
    client, _ = logged_client
    html = client.post("/feedback", data={
        "title": "店里用不了",
        "body": "下单页白屏",
    }, follow_redirects=True).get_data(as_text=True)

    assert "已提交（FB-" in html


# ---------------------------------------------------------------------------
# 模板契约：入口可拦截、表单有降级、每个页面都能弹
# ---------------------------------------------------------------------------


def test_base_template_renders_widget_hooks(logged_client):
    """弹层与入口必须由 base.html 渲染 —— 否则只有首页能弹。"""
    client, _ = logged_client
    html = client.get("/land").get_data(as_text=True)

    assert 'data-feedback-open' in html, "ctx-bar 的反馈入口丢了拦截标记"
    assert 'id="feedback-modal"' in html, "悬浮窗容器没渲染"
    assert 'id="feedback-modal-form"' in html
    # 表单 action 指向 PRG 端点 = 脚本挂了也能提交；data-endpoint 才是 AJAX 目标
    form_tag = re.search(r'<form id="feedback-modal-form"[^>]*>', html)
    assert form_tag is not None
    assert 'action="/feedback"' in form_tag.group(0)
    assert 'data-endpoint="/feedback/api"' in form_tag.group(0)
    # page_path 由前端填当前页；不预填会让「提交时所在页面」恒为空
    assert 'name="page_path"' in html
    # 弹层表单必须带 csrf_token：忘加 → 生产 CSRF 直接 400（TESTING 关闭时不报）
    assert re.search(r'name="csrf_token" value="[^"]+"', html) is not None
    # 错误行必须可被读屏播报
    assert 'aria-live="assertive"' in html


def test_widget_partial_is_guarded_by_login(logged_client):
    """弹层片段自身必须带 ``{% if g.user %}`` 守卫。

    断言模板源码而不是渲染结果：匿名可访问的页面（/login）不继承 base.html，
    拿它断言「没有弹层」是恒真的假绿（code review 指出）。
    """
    from pathlib import Path

    src = Path("templates/_feedback_widget.html").read_text(encoding="utf-8")
    assert "{% if g.user %}" in src, "删掉登录守卫会让匿名页也渲染带 csrf 的弹层"
    assert "{% endif %}" in src


def test_widget_script_is_referenced(logged_client):
    """新脚本必须被引入 —— 少这一行则点击仍整页跳转（回到 Issue 原状）。"""
    client, _ = logged_client
    html = client.get("/land").get_data(as_text=True)
    assert "feedback-widget.js" in html
