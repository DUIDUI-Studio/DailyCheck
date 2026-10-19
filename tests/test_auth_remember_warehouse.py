"""登录后自动恢复上次仓库：深链不再停在选择仓库页。

生产现象：企微群审批通知 → 未登录 → 登录成功 → 仍先落到
`/select-warehouse`，要点一次仓库才回到审批页。`next` 本身没丢
（`tests/test_auth_next_redirect.py` 已覆盖），丢的是「上次在哪个仓」。

修复口径：`users.last_warehouse_id` 在手动选仓时写入，登录成功时恢复；
恢复前**重跑**租户 + `warehouse_users` 绑定双闸，失效则静默回落到
picker —— 自动化选仓不能比手动点按钮更宽松。

环境复用 `test_auth_next_redirect.py::deeplink_env`（门店 + DC +
可登录的门店经理 + 一张订单 + 一个跨租户仓），避免第二套种子数据。
"""
# pytest 的 fixture 注入在语法上就是「参数遮蔽导入名」，F811 是误报。
# ruff: noqa: F811

from __future__ import annotations

import sqlite3
from urllib.parse import quote

import pytest

from tests.test_auth_next_redirect import (
    PASSWORD,
    STORE_USER_ID,
    STORE_USERNAME,
    STORE_WAREHOUSE_ID,
    _client,
    _location,
    _split,
)
from tests.test_auth_next_redirect import (  # noqa: F401
    deeplink_env as _deeplink_env_fixture,
)


@pytest.fixture
def env(_deeplink_env_fixture):
    """Alias so this module's test signatures don't shadow the imported fixture."""
    return _deeplink_env_fixture

ADMIN_USER_ID = 1
ADMIN_USERNAME = "admin"
DC_WAREHOUSE_ID = 1       # dc_test：与门店经理同租户，但无绑定
CROSS_TENANT_WAREHOUSE = 9  # store_t2：tenant 2


# ---------------------------------------------------------------------------
# 准备/断言辅助
# ---------------------------------------------------------------------------

def _login(client, next_path: str | None = None, *, username=STORE_USERNAME,
           password=PASSWORD):
    data = {"username": username, "password": password}
    url = "/login"
    if next_path is not None:
        url = f"/login?next={quote(next_path, safe='')}"
    return client.post(url, data=data)


def _select(client, warehouse_id=STORE_WAREHOUSE_ID, **extra):
    return client.post(f"/select-warehouse/{warehouse_id}", data=extra)


def _master(env):
    conn = sqlite3.connect(str(env["master_path"]))
    conn.row_factory = sqlite3.Row
    return conn


def _set_last_warehouse(env, user_id: int, warehouse_id: int | None) -> None:
    conn = _master(env)
    conn.execute(
        "UPDATE users SET last_warehouse_id=? WHERE id=?",
        (warehouse_id, user_id),
    )
    conn.commit()
    conn.close()


def _last_warehouse(env, user_id: int):
    conn = _master(env)
    row = conn.execute(
        "SELECT last_warehouse_id FROM users WHERE id=?", (user_id,)
    ).fetchone()
    conn.close()
    return row["last_warehouse_id"]


# ---------------------------------------------------------------------------
# 1) 写入：手动选仓要记住这次选择
# ---------------------------------------------------------------------------

def test_warehouse_select_persists_last_warehouse(env):
    client = _client(env, user_id=STORE_USER_ID)
    _select(client)

    assert _last_warehouse(env, STORE_USER_ID) == STORE_WAREHOUSE_ID


def test_warehouse_select_blocked_path_does_not_persist(env):
    """403 的仓（无绑定）不得写进 last_warehouse_id。"""
    client = _client(env, user_id=STORE_USER_ID)
    resp = _select(client, DC_WAREHOUSE_ID)

    assert resp.status_code == 403
    assert _last_warehouse(env, STORE_USER_ID) is None


# ---------------------------------------------------------------------------
# 2) 恢复：登录后直达深链，不再经过 picker
# ---------------------------------------------------------------------------

def test_login_restores_last_warehouse_and_skips_picker(env):
    _set_last_warehouse(env, STORE_USER_ID, STORE_WAREHOUSE_ID)

    client = _client(env)  # 新会话 = 企微里新开的 webview
    resp = _login(client, env["order_path"])

    assert _location(resp) == env["order_path"]
    with client.session_transaction() as s:
        assert s["warehouse_id"] == STORE_WAREHOUSE_ID


def test_deep_link_full_chain_no_picker_stop(env):
    """未登录点审批深链 → 登录 → 直接 200，中途不落 picker。"""
    _set_last_warehouse(env, STORE_USER_ID, STORE_WAREHOUSE_ID)

    client = _client(env)
    resp = _login(client, env["order_path"])
    location = _location(resp)
    assert "/select-warehouse" not in location

    page = client.get(location)
    assert page.status_code == 200


def test_login_restore_defaults_to_land_without_next(env):
    """没有 next 时，有历史仓就直接进总览，而不是再问一次。"""
    _set_last_warehouse(env, STORE_USER_ID, STORE_WAREHOUSE_ID)

    client = _client(env)
    assert _location(_login(client)) == "/land"


def test_login_without_history_still_shows_picker(env):
    client = _client(env)

    assert _location(_login(client)) == "/select-warehouse"
    with client.session_transaction() as s:
        assert "warehouse_id" not in s


# ---------------------------------------------------------------------------
# 3) 失效回落：恢复必须重新过闸，不能绕过授权
# ---------------------------------------------------------------------------

def test_login_ignores_cross_tenant_last_warehouse(env):
    _set_last_warehouse(env, STORE_USER_ID, CROSS_TENANT_WAREHOUSE)

    client = _client(env)
    assert _location(_login(client)) == "/select-warehouse"
    with client.session_transaction() as s:
        assert "warehouse_id" not in s


def test_login_ignores_revoked_binding(env):
    """同租户但 warehouse_users 里没有绑定 → 当没选过。"""
    _set_last_warehouse(env, STORE_USER_ID, DC_WAREHOUSE_ID)

    client = _client(env)
    assert _location(_login(client)) == "/select-warehouse"


def test_login_ignores_deleted_warehouse(env):
    _set_last_warehouse(env, STORE_USER_ID, 999)

    client = _client(env)
    assert _location(_login(client)) == "/select-warehouse"


def test_login_admin_may_restore_cross_tenant_warehouse(env):
    """平台管理员跨租户，与 warehouse_select 的 admin 例外保持一致。"""
    conn = _master(env)
    from werkzeug.security import generate_password_hash
    conn.execute(
        "UPDATE users SET password_hash=?, last_warehouse_id=? WHERE id=?",
        (generate_password_hash(PASSWORD), CROSS_TENANT_WAREHOUSE, ADMIN_USER_ID),
    )
    conn.commit()
    conn.close()

    client = _client(env)
    _login(client, username=ADMIN_USERNAME)

    with client.session_transaction() as s:
        assert s["warehouse_id"] == CROSS_TENANT_WAREHOUSE


# ---------------------------------------------------------------------------
# 4) 请求级校验：session 里的仓也要过绑定闸
# ---------------------------------------------------------------------------

def test_session_with_unbound_warehouse_is_cleared(env):
    """旧行为只校验租户；未绑定的同租户仓必须被踢回 picker。"""
    client = _client(env, user_id=STORE_USER_ID)
    with client.session_transaction() as s:
        s["warehouse_id"] = DC_WAREHOUSE_ID

    resp = client.get("/land")

    assert _split(_location(resp)).path == "/select-warehouse"
    with client.session_transaction() as s:
        assert "warehouse_id" not in s


def test_session_with_bound_warehouse_passes(env):
    client = _client(env, user_id=STORE_USER_ID)
    with client.session_transaction() as s:
        s["warehouse_id"] = STORE_WAREHOUSE_ID

    assert client.get("/land").status_code == 200
