"""行内表单 AJAX 拦截器（A 类）行为测试。

约定：前端 ajax-form.js 给请求打 X-Requested-With: fetch + Accept: application/json；
register_ajax_handlers(app) 在 create_app 里注册的 after_request hook 检测到这两个头后，
把视图返回的 3xx redirect 改写成 200 JSON 信封 {ok, location, message}。

非 AJAX 请求（一模 POST / 全页跳转 / MCP）保留原 302 行为字节级一致 —— 这是本改造的硬性契约。
"""
import re


def _csrf(html: str) -> str:
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    assert m, "应为该商品 GET 页面渲染 csrf_token hidden"
    return m.group(1)


def _seed_item(wh_path, name="test-ajax-item"):
    """写入一个最简品项，让 /items/<id>/delete 有合法目标。"""
    import sqlite3 as _sq
    ts = "2026-10-11 00:00:00"
    with _sq.connect(wh_path) as db:
        db.execute(
            "INSERT OR IGNORE INTO categories (id, name) VALUES (1, '测试')"
        )
        cur = db.execute(
            "INSERT INTO items (sku, name, category_id, unit, quantity, "
            "safety_stock, unit_cost, gram_per_unit, aux_rate, updated_at) "
            "VALUES (?, ?, 1, 'g', 0, 0, 0, 0, 0, ?)",
            (f"AJAX-{name}", name, ts),
        )
        item_id = cur.lastrowid
        db.commit()
    return item_id


def test_ajax_post_returns_json_envelope(logged_client):
    """AJAX 头 → 302 redirect 被改写成 200 JSON 信封。"""
    client, wh_path = logged_client
    item_id = _seed_item(wh_path)
    # 拿 token
    r = client.get("/items")
    token = _csrf(r.get_data(as_text=True))
    # 走 AJAX 路径
    r2 = client.post(
        f"/items/{item_id}/delete",
        data={"csrf_token": token},
        headers={
            "X-Requested-With": "fetch",
            "Accept": "application/json",
        },
    )
    assert r2.status_code == 200
    assert r2.is_json, f"应返回 JSON，实际 Content-Type: {r2.headers.get('Content-Type')}"
    body = r2.get_json()
    assert body["ok"] is True
    # location 字段保留原 redirect 目标
    assert "location" in body and "/items" in body["location"]


def test_non_ajax_post_keeps_302_redirect(logged_client):
    """非 AJAX 头 → 维持 302 redirect 字节级兼容（老浏览器 / 老测试）。"""
    client, wh_path = logged_client
    item_id = _seed_item(wh_path, name="non-ajax-item")
    r = client.get("/items")
    token = _csrf(r.get_data(as_text=True))
    r2 = client.post(
        f"/items/{item_id}/delete",
        data={"csrf_token": token},
        # 无 X-Requested-With, Accept 也不带 application/json
    )
    assert r2.status_code == 302, f"非 AJAX 应保留 302 redirect，实际 {r2.status_code}"
    assert "/items" in r2.headers.get("Location", "")


def test_ajax_accept_json_alone_triggers_json(logged_client):
    """只看 Accept: application/json（无 X-Requested-With）也应触发 JSON 路径。

    这是 forward-compatible 约定 —— 让 curl / 测试 / 第三方工具不用记得加 X-Requested-With。
    """
    client, wh_path = logged_client
    item_id = _seed_item(wh_path, name="accept-json-only")
    r = client.get("/items")
    token = _csrf(r.get_data(as_text=True))
    r2 = client.post(
        f"/items/{item_id}/delete",
        data={"csrf_token": token},
        headers={"Accept": "application/json"},
    )
    assert r2.status_code == 200
    assert r2.is_json


def test_ajax_flash_message_in_envelope(logged_client):
    """服务端 flash 消息应随 JSON 信封一起送达客户端（前端 ajax-flash 浮窗用）。

    先 GET 一次页面拿 csrf，再 POST 一次（这一步 flash 排队 + redirect），
    但我们用 AJAX 路径 —— flash 消息不进下次同步渲染的 session，
    而是被 after_request hook pop 出来塞进 envelope.message。
    """
    client, wh_path = logged_client
    item_id = _seed_item(wh_path, name="flash-test-item")
    r = client.get("/items")
    token = _csrf(r.get_data(as_text=True))
    r2 = client.post(
        f"/items/{item_id}/delete",
        data={"csrf_token": token},
        headers={"X-Requested-With": "fetch"},
    )
    body = r2.get_json()
    assert body["ok"] is True
    # items.delete 视图里会 flash('已删除 ...') —— 验证 message 字段非空即可
    assert "message" in body and body["message"], f"应带回服务端 flash 文本，实际: {body!r}"


def test_ajax_post_4xx_passthrough(logged_client):
    """视图直接 abort(400) / 返回 4xx —— 不应被 after_request hook 改写。

    hook 只拦截 3xx redirect（成功路径 PRG），4xx 失败响应原样透传给客户端，
    让 fetch 的 res.ok=false 走 alert 失败分支。
    """
    client, _ = logged_client
    # /items/999999/delete → 视图大概率 abort(404) 或 redirect（具体取决于实现）。
    # 我们只断言：要么 302（A），要么 4xx（B），但绝不会是 200 JSON 信封（C）——
    # 因为 hook 只对 3xx 改写。
    r2 = client.post(
        "/items/999999/delete",
        data={},
        headers={"X-Requested-With": "fetch"},
    )
    # 不应是 after_request 包装过的 200 JSON 信封（hook 只动 3xx）
    if r2.status_code == 200:
        assert not r2.is_json, f"hook 不应把 4xx/5xx 改成 200 JSON: {r2.get_data(as_text=True)[:200]}"
    # 兜底：其余情形（302、404 等）都符合契约
    assert r2.status_code in (302, 400, 403, 404)


def test_ajax_form_js_loaded_in_base_html(logged_client):
    """base.html 必须引入 ajax-form.js —— 这是拦截器能被浏览器加载的入口。

    若此测试失败，意味着 base.html 的 <script> 标签被改/丢了，
    行内小动作的"不跳顶部"承诺立刻失效。
    """
    client, _ = logged_client
    r = client.get("/items")
    html = r.get_data(as_text=True)
    assert 'ajax-form.js' in html, "base.html 应包含 ajax-form.js 的 <script> 标签"


# ---------------------------------------------------------------------------
# F2 回归：3xx 语义分流，杜绝「登录过期被当成操作成功」（2026-10-18 假成功事故）
# ---------------------------------------------------------------------------


def test_ajax_expired_session_returns_401_not_false_success(logged_client):
    """核心判据：会话过期时 AJAX POST 必须返 401，绝不能是 200 {ok:true}。

    事故形态：用户点「删除」→ 会话已过期 → auth.before_app_request 短路
    redirect(/login) → 旧 hook 无条件改写成 200 {ok:true} → 前端弹「已删除」
    并移除该行，但服务端一条没删。本用例锁死这个回归。
    """
    client, wh_path = logged_client
    item_id = _seed_item(wh_path, name="expired-session-item")
    r = client.get("/items")
    token = _csrf(r.get_data(as_text=True))

    # 清掉登录态，模拟会话过期（保留 CSRF token 以便走到 auth 短路而非 CSRF 拦截）
    with client.session_transaction() as s:
        s.clear()

    r2 = client.post(
        f"/items/{item_id}/delete",
        data={"csrf_token": token},
        headers={"X-Requested-With": "fetch", "Accept": "application/json"},
    )
    assert r2.status_code == 401, (
        f"会话过期必须 401，实际 {r2.status_code}（旧 bug 会返 200）"
    )
    assert r2.is_json
    body = r2.get_json()
    assert body["ok"] is False, "过期绝不能报 ok:true"
    assert body.get("reason") == "auth_required"
    assert "/login" in body.get("location", "")


def test_ajax_business_redirect_still_200_ok(logged_client):
    """真 PRG 成功（登录态正常）仍是 200 {ok:true} —— 分流不能误伤正常路径。"""
    client, wh_path = logged_client
    item_id = _seed_item(wh_path, name="still-ok-item")
    r = client.get("/items")
    token = _csrf(r.get_data(as_text=True))
    r2 = client.post(
        f"/items/{item_id}/delete",
        data={"csrf_token": token},
        headers={"X-Requested-With": "fetch", "Accept": "application/json"},
    )
    assert r2.status_code == 200
    body = r2.get_json()
    assert body["ok"] is True
    assert body.get("reason") is None


def test_redirect_kind_classification_matrix():
    """_redirect_kind 分类矩阵单测（纯函数，不依赖 app 上下文之外的状态）。

    只认「重定向去哪」：→登录页=auth（logout 除外）、→选仓页=warehouse、其余=business。
    """
    from flask import Flask

    from blueprints._helpers import _redirect_kind

    app = Flask(__name__)
    # 注册最小路由表，让 url_for 能解析 auth.login / auth.warehouse_picker
    from blueprints import auth as auth_bp

    app.register_blueprint(auth_bp.bp)
    with app.test_request_context("/"):
        login = "/login?next=/items"
        picker = "/select-warehouse?next=/items"
        assert _redirect_kind(login, "items.delete") == "auth"
        assert _redirect_kind(picker, "items.delete") == "warehouse"
        assert _redirect_kind("/items", "items.delete") == "business"
        # logout 成功也回登录页，但那是真成功
        assert _redirect_kind("/login", "auth.logout") == "business"
        # 无 Location 保守当业务成功
        assert _redirect_kind(None, "items.delete") == "business"
