"""深链回跳：`next` 全链路透传 + 同源校验（开放重定向防御）。

生产现象：企微群订货通知里的 `https://kjtang.cn/store-ordering/orders/<id>`
点开后落到登录页，登录完回到仓库选择页 / 首页，**没有回到那张订单**。

三处断点，本文件逐条覆盖：
  1. `templates/login.html` 的 form action 不带 next → POST /login 拿不到目标；
  2. `permissions.require_login` / `auth.before_app_request` 跳转不带 next
     （且 `/select-warehouse` 跳转同样丢），选仓后固定 `/land`；
  3. `auth.login` / `auth.warehouse_select` 的 `redirect(next)` 无同源校验
     —— 一旦 next 打通就是开放重定向。

环境复用 `tests/conftest.py::_seed_store_ordering_env`（门店 + 配送中心 +
5 个用户 + 绑定），本文件只补两样它没有的东西：一个**可校验密码**的账号
（conftest 播种的 `password_hash='x'` 无法通过 `check_password_hash`），
以及一张已存在的订单。订单行直接落 master.db——深链用例的前态是「订单早就
存在」，建单链路本身由 `tests/test_store_ordering_store_route.py` 覆盖，
此处不重复造第二套。
"""
from __future__ import annotations

import html as html_lib
import re
import sqlite3
from datetime import datetime
from urllib.parse import parse_qs, quote, urlsplit

import pytest
from werkzeug.security import generate_password_hash

from blueprints.auth import _safe_next
from tests.conftest import _seed_store_ordering_env

# 可登录账号：store_mgr(id=3)，绑定 store_test(仓库 2)，租户 1。
STORE_USER_ID = 3
STORE_USERNAME = "store_mgr"
STORE_WAREHOUSE_ID = 2
PASSWORD = "deeplink-pw-1"

# 未登录 / 未选仓时用户想访问的目标（企微通知里的深链形态）
_ORDER_PATH = "/store-ordering/orders/{order_id}"


@pytest.fixture
def deeplink_env(tmp_path, monkeypatch):
    """门店 + DC + 可登录的门店经理 + 一张 pending 订单。"""
    app, _client, master_path, dc_path, store_path = _seed_store_ordering_env(
        tmp_path, monkeypatch
    )
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    m = sqlite3.connect(str(master_path))
    m.row_factory = sqlite3.Row
    m.execute(
        "UPDATE users SET password_hash=? WHERE id=?",
        (generate_password_hash(PASSWORD), STORE_USER_ID),
    )
    # 订单详情要 JOIN canonical_items / canonical_categories（模板还要中文分类名）。
    m.execute(
        "INSERT INTO canonical_categories (code, name, description, created_at, updated_at) "
        "VALUES ('PACKAGING', '包材', '', ?, ?)",
        (ts, ts),
    )
    m.execute(
        """INSERT INTO canonical_items
           (id, canonical_sku, name, category_code, unit, gram_per_unit, aux_unit,
            aux_rate, status, created_from, created_at, updated_at)
           VALUES (101, 'IC-000101', '测试包材A', 'PACKAGING', '件', 0, NULL, 0,
                   'active', 'rd_manual', ?, ?)""",
        (ts, ts),
    )
    cur = m.execute(
        """INSERT INTO store_orders
           (order_no, store_warehouse_code, dc_warehouse_code, status, requested_by,
            expected_delivery_date, note, created_at, updated_at)
           VALUES ('SO-DEEPLINK-0001', 'store_test', 'dc_test', 'pending', ?, ?, '', ?, ?)""",
        (STORE_USER_ID, ts[:10], ts, ts),
    )
    order_id = int(cur.lastrowid)
    m.execute(
        """INSERT INTO store_order_items
           (order_id, canonical_id, quantity, unit, fulfilled_quantity, status, created_at)
           VALUES (?, 101, 5.0, '件', 0.0, 'pending', ?)""",
        (order_id, ts),
    )
    # 跨租户仓（tenant 2）：用于断言选仓的租户闸没被 next 改动破坏。
    m.execute(
        """INSERT INTO warehouses
           (id, code, name, db_path, warehouse_type, tenant_id, created_at)
           VALUES (9, 'store_t2', '跨租户门店', ?, 'storefront', 2, ?)""",
        (str(store_path), ts),
    )
    m.commit()
    m.close()

    return {
        "app": app,
        "master_path": master_path,
        "dc_path": dc_path,
        "store_path": store_path,
        "order_id": order_id,
        "order_path": _ORDER_PATH.format(order_id=order_id),
    }


def _client(env, *, user_id=None):
    """新建 test client；给了 user_id 就直接写 session（不设 warehouse_id）。"""
    client = env["app"].test_client()
    if user_id is not None:
        with client.session_transaction() as s:
            s["user_id"] = user_id
    return client


def _location(resp) -> str:
    assert resp.status_code == 302, f"期望 302，实际 {resp.status_code}"
    return resp.headers["Location"]


def _split(location: str):
    return urlsplit(location)


# --- 表单驱动：按浏览器的方式来，而不是在测试里手动塞 next ---------------
# 模板断点（action 丢 next / 隐藏域被删）只有在「读渲染结果再提交」时才暴露：
# 手动给 POST 塞 next 会让模板回归悄悄溜过去。
_FORM_RE = re.compile(r"<form\b[^>]*action=\"([^\"]*)\"[^>]*>(.*?)</form>", re.S)
_HIDDEN_RE = re.compile(
    r"<input[^>]*type=\"hidden\"[^>]*name=\"([^\"]+)\"[^>]*value=\"([^\"]*)\"", re.S
)


def _rendered_forms(page: str) -> list[tuple[str, dict[str, str]]]:
    """从渲染后的 HTML 里取出 (form action, {hidden name: value})。"""
    return [
        (
            html_lib.unescape(action),
            {
                name: html_lib.unescape(value)
                for name, value in _HIDDEN_RE.findall(inner)
            },
        )
        for action, inner in _FORM_RE.findall(page)
    ]


def _submit_login_form(client, next_path, *, username=STORE_USERNAME,
                       password=PASSWORD):
    """GET 登录页 → 照渲染出来的 action + 隐藏域 POST（浏览器行为）。"""
    page = client.get(f"/login?next={quote(next_path, safe='')}")
    action, hidden = _rendered_forms(page.get_data(as_text=True))[0]
    hidden.update({"username": username, "password": password})
    return client.post(action, data=hidden)


def _submit_picker_form(client, next_path, *, warehouse_id=STORE_WAREHOUSE_ID):
    """GET 选仓页 → 点某个仓库的按钮（照渲染出来的 action + 隐藏域）。"""
    page = client.get(f"/select-warehouse?next={quote(next_path, safe='')}")
    for action, hidden in _rendered_forms(page.get_data(as_text=True)):
        if f"/select-warehouse/{warehouse_id}" in action:
            return client.post(action, data=hidden)
    raise AssertionError(f"选仓页没有渲染仓库 {warehouse_id} 的表单")


# ---------------------------------------------------------------------------
# 1) 未登录访问深链 → /login?next=<原路径>
# ---------------------------------------------------------------------------

def test_anonymous_deeplink_redirects_to_login_with_next(deeplink_env):
    client = _client(deeplink_env)
    resp = client.get(deeplink_env["order_path"])

    parts = _split(_location(resp))
    assert parts.path == "/login"
    assert parse_qs(parts.query)["next"] == [deeplink_env["order_path"]]


def test_anonymous_deeplink_keeps_query_string(deeplink_env):
    """深链带查询串（如列表页筛选）时 next 必须整串保留，不能只剩 path。"""
    client = _client(deeplink_env)
    target = "/store-ordering/orders?status=pending"
    parts = _split(_location(client.get(target)))

    assert parts.path == "/login"
    assert parse_qs(parts.query)["next"] == [target]


# ---------------------------------------------------------------------------
# 2) 已登录但未选仓 → /select-warehouse?next=<原路径>
# ---------------------------------------------------------------------------

def test_logged_in_without_warehouse_redirects_to_picker_with_next(deeplink_env):
    client = _client(deeplink_env, user_id=STORE_USER_ID)
    parts = _split(_location(client.get(deeplink_env["order_path"])))

    assert parts.path == "/select-warehouse"
    assert parse_qs(parts.query)["next"] == [deeplink_env["order_path"]]


# ---------------------------------------------------------------------------
# 3) POST /login 把 next 带到目标页（隐藏域 / 查询串两种形态）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("transport", ["query", "hidden_field", "rendered_form"])
def test_login_post_redirects_to_next(deeplink_env, transport):
    client = _client(deeplink_env)
    data = {"username": STORE_USERNAME, "password": PASSWORD}
    url = "/login"
    if transport == "query":
        url = f"/login?next={quote(deeplink_env['order_path'], safe='')}"
    elif transport == "hidden_field":
        data["next"] = deeplink_env["order_path"]
    else:
        # 真实路径：按渲染出来的 action + 隐藏域提交（模板回归在这里现形）
        resp = _submit_login_form(client, deeplink_env["order_path"])
        location = _location(resp)
        assert location == deeplink_env["order_path"]
        assert "/select-warehouse" not in location
        with client.session_transaction() as s:
            assert s["user_id"] == STORE_USER_ID
        return

    location = _location(client.post(url, data=data))

    assert location == deeplink_env["order_path"]
    assert "/select-warehouse" not in location
    with client.session_transaction() as s:
        assert s["user_id"] == STORE_USER_ID


# ---------------------------------------------------------------------------
# 4) POST /select-warehouse/<id>：有 next 回 next，无 next 回 /land
# ---------------------------------------------------------------------------

def test_warehouse_select_redirects_to_next(deeplink_env):
    client = _client(deeplink_env, user_id=STORE_USER_ID)
    resp = client.post(f"/select-warehouse/{STORE_WAREHOUSE_ID}", data={
        "next": deeplink_env["order_path"],
    })

    assert _location(resp) == deeplink_env["order_path"]
    with client.session_transaction() as s:
        assert s["warehouse_id"] == STORE_WAREHOUSE_ID


def test_warehouse_select_via_rendered_form_redirects_to_next(deeplink_env):
    """点选仓页的按钮（照渲染的 action + 隐藏域）也要回得到 next。"""
    client = _client(deeplink_env, user_id=STORE_USER_ID)
    resp = _submit_picker_form(client, deeplink_env["order_path"])

    assert _location(resp) == deeplink_env["order_path"]


def test_warehouse_select_without_next_goes_to_land(deeplink_env):
    client = _client(deeplink_env, user_id=STORE_USER_ID)
    resp = client.post(f"/select-warehouse/{STORE_WAREHOUSE_ID}", data={})

    assert _location(resp) == "/land"


def test_picker_form_carries_next_into_select(deeplink_env):
    """选仓页每个 button 的 form action 必须带 next（否则断点 2 白修）。"""
    client = _client(deeplink_env, user_id=STORE_USER_ID)
    path = deeplink_env["order_path"]
    body = client.get(f"/select-warehouse?next={quote(path, safe='')}").get_data(
        as_text=True
    )

    # url_for 不转义查询串里的 "/"（合法），故断言用原样路径。
    assert f'action="/select-warehouse/{STORE_WAREHOUSE_ID}?next={path}"' in body
    assert f'name="next" value="{path}"' in body


# ---------------------------------------------------------------------------
# 5) 开放重定向防御
# ---------------------------------------------------------------------------

# 全部是「浏览器会解析到别的 host」或「根本不是路径」的形态。
UNSAFE_NEXT_VALUES = [
    "http://evil.example/phish",
    "//evil.example/phish",
    "/\\evil.example/phish",
    "https:/evil.example",
    "javascript:alert(1)",
    "",
]


@pytest.mark.parametrize("bad", UNSAFE_NEXT_VALUES)
def test_safe_next_rejects_unsafe_values(bad):
    assert _safe_next(bad) is None


@pytest.mark.parametrize("good", ["/store-ordering/orders/1", "/items?a=1&b=2", "/"])
def test_safe_next_accepts_same_origin_paths(good):
    assert _safe_next(good) == good


@pytest.mark.parametrize("bad", UNSAFE_NEXT_VALUES)
def test_login_post_ignores_unsafe_next(deeplink_env, bad):
    """外部 next 一律丢弃，回落到默认的仓库选择页；Location 不得指向外部域。"""
    client = _client(deeplink_env)
    resp = client.post("/login", data={
        "username": STORE_USERNAME,
        "password": PASSWORD,
        "next": bad,
    })

    location = _location(resp)
    parts = _split(location)
    assert parts.scheme == ""
    assert parts.netloc == ""
    assert parts.path == "/select-warehouse"
    assert "evil.example" not in location


@pytest.mark.parametrize("bad", UNSAFE_NEXT_VALUES)
def test_warehouse_select_ignores_unsafe_next(deeplink_env, bad):
    client = _client(deeplink_env, user_id=STORE_USER_ID)
    location = _location(client.post(
        f"/select-warehouse/{STORE_WAREHOUSE_ID}", data={"next": bad},
    ))

    parts = _split(location)
    assert parts.scheme == ""
    assert parts.netloc == ""
    assert parts.path == "/land"
    assert "evil.example" not in location


def test_login_page_hidden_field_and_action_carry_next(deeplink_env):
    """登录页必须双保险：action 带 next 查询串 + 隐藏域带 next。"""
    client = _client(deeplink_env)
    path = deeplink_env["order_path"]
    body = client.get(f"/login?next={quote(path, safe='')}").get_data(as_text=True)

    # url_for 不转义查询串里的 "/"（合法），故断言用原样路径。
    assert f'action="/login?next={path}"' in body
    assert f'name="next" value="{path}"' in body


# ---------------------------------------------------------------------------
# 6) 回归：不带 next 的老行为 + 选仓的租户/绑定闸
# ---------------------------------------------------------------------------

def test_login_without_next_still_goes_to_warehouse_picker(deeplink_env):
    client = _client(deeplink_env)
    resp = client.post("/login", data={
        "username": STORE_USERNAME, "password": PASSWORD,
    })

    assert _location(resp) == "/select-warehouse"


def test_warehouse_select_cross_tenant_still_403(deeplink_env):
    client = _client(deeplink_env, user_id=STORE_USER_ID)
    resp = client.post("/select-warehouse/9", data={
        "next": deeplink_env["order_path"],
    })

    assert resp.status_code == 403


def test_warehouse_select_unbound_still_403(deeplink_env):
    """仓库 1（dc_test）与 store_mgr(3) 无绑定关系 → 403，与 next 无关。"""
    client = _client(deeplink_env, user_id=STORE_USER_ID)
    resp = client.post("/select-warehouse/1", data={
        "next": deeplink_env["order_path"],
    })

    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 7) 端到端：未登录点深链 → 登录 → 选仓 → 落到订单详情 200
# ---------------------------------------------------------------------------

def test_deep_link_full_chain_reaches_order_detail(deeplink_env):
    """企微通知点击后的完整链路，逐跳断言（等价于真机操作一遍）。

    登录与选仓都**按渲染出来的表单提交**（不手动塞 next）——模板断点
    （action 丢 next / 隐藏域被删）必须能让这条链路红。
    """
    path = deeplink_env["order_path"]
    client = _client(deeplink_env)  # ① 企微内置浏览器，无会话

    # ① 未登录点深链 → 登录页，next 指向订单
    hop1 = _split(_location(client.get(path)))
    assert hop1.path == "/login"
    assert parse_qs(hop1.query)["next"] == [path]

    # ② 输账号密码（照登录表单的 action + 隐藏域提交）→ 回到订单路径
    hop2_location = _location(_submit_login_form(client, path))
    assert hop2_location == path

    # ③ 还没选仓 → 仓库选择页，next 仍在
    hop3 = _split(_location(client.get(hop2_location)))
    assert hop3.path == "/select-warehouse"
    assert parse_qs(hop3.query)["next"] == [path]

    # ④ 点仓库按钮（照选仓表单提交）→ 回到订单
    hop4_location = _location(_submit_picker_form(client, path))
    assert hop4_location == path

    # ⑤ 终于落到那张订单
    resp = client.get(hop4_location)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "SO-DEEPLINK-0001" in body
