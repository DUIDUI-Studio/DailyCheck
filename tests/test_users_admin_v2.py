"""账号管理 v2 测试（feat/user-admin-v2）。

覆盖：
- AC-01 搜索（username LIKE，≥2 字符）
- AC-02 角色筛选（用户在该角色有任一绑定）
- AC-03 启停筛选
- AC-04 / AC-05 抽屉渲染由 JS 控制；后端只验证行渲染（data-* attrs）
- AC-06 toggle-active
- AC-07 bulk-bind
- AC-08 非 admin 守卫
- AC-09 租户筛选（admin 跨租户可见 + ?tenant_id= 过滤）
- AC-10 停用账号登录拦截
- AC-11 分页（page/page_size）
- AC-12 装饰器（@require_platform_admin 替代 _require_admin）
"""
from __future__ import annotations

import sqlite3

import pytest

TS = "2026-10-12 00:00:00"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def users_v2_env(tmp_path, monkeypatch):
    """Build a 2-tenant env with mixed users + warehouses for the v2 list tests.

    Tenants:
      - tenant 1: admin(super), userA, userB (active)
      - tenant 2: userC, userD (one active, one inactive)

    Warehouses:
      - wh_t1 (tenant 1)
      - wh_t2 (tenant 2)

    Bindings:
      - userA -> wh_t1 (manager)
      - userB -> wh_t1 (staff)
      - userC -> wh_t2 (admin)
      - userD -> wh_t2 (manager), but userD is_active=0

    Returns (app, master_path, client) where client is logged in as admin(super).
    """
    import db as db_module
    from db import init_master_db, init_warehouse_db

    master_path = tmp_path / "master.db"
    wh_dir = tmp_path / "warehouses"
    wh_dir.mkdir()
    wh_t1 = wh_dir / "wh_t1.db"
    wh_t2 = wh_dir / "wh_t2.db"

    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_dir)
    import config as config_module
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", wh_dir)

    init_master_db()
    init_warehouse_db(wh_t1)
    init_warehouse_db(wh_t2)

    m = sqlite3.connect(master_path)
    m.execute("INSERT INTO tenants (id, name, created_at) VALUES (1, '默认', ?)", (TS,))
    m.execute("INSERT INTO tenants (id, name, created_at) VALUES (2, '客户B', ?)", (TS,))
    # super=admin(1), userA(2), userB(3), userC(4), userD(5, inactive)
    rows = [
        (1, "super", 1, 1, 1),  # platform admin, tenant 1
        (2, "userA", 0, 1, 1),
        (3, "userB", 0, 1, 1),
        (4, "userC", 0, 1, 2),
        (5, "userD", 0, 0, 2),  # is_active=0
    ]
    for uid, name, is_admin, is_active, tenant in rows:
        m.execute(
            "INSERT INTO users (id, username, password_hash, is_admin, "
            "created_at, tenant_id, is_active) "
            "VALUES (?, ?, 'x', ?, ?, ?, ?)",
            (uid, name, is_admin, TS, tenant, is_active),
        )
    warehouses = [
        (1, "wh_t1", "仓库一", str(wh_t1), 1),
        (2, "wh_t2", "仓库二", str(wh_t2), 2),
    ]
    for wid, code, name, path, tenant in warehouses:
        m.execute(
            "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, "
            "created_at, tenant_id) VALUES (?, ?, ?, ?, 'storefront', ?, ?)",
            (wid, code, name, path, TS, tenant),
        )
    bindings = [
        (2, 1, "manager"), (3, 1, "staff"),
        (4, 2, "admin"), (5, 2, "manager"),
    ]
    for uid, wid, role in bindings:
        m.execute(
            "INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (?, ?, ?)",
            (uid, wid, role),
        )
    m.commit()
    m.close()

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True

    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = 1  # super admin
        s["warehouse_id"] = 1

    return app, master_path, client


def _client_with(app, user_id, warehouse_id=None):
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = user_id
        if warehouse_id is not None:
            s["warehouse_id"] = warehouse_id
    return client


# ---------------------------------------------------------------------------
# AC-12 装饰器（@require_platform_admin）
# ---------------------------------------------------------------------------

def test_non_admin_users_returns_403(users_v2_env):
    """非 admin 访问 /users 必须 403（@require_platform_admin 守卫）。"""
    app, _, _ = users_v2_env
    client = _client_with(app, user_id=2, warehouse_id=1)  # userA is non-admin
    r = client.get("/users")
    assert r.status_code == 403


def test_anonymous_redirects_to_login(users_v2_env):
    """未登录访问 /users 必须重定向到 /login。"""
    app, _, _ = users_v2_env
    client = app.test_client()
    r = client.get("/users")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


# ---------------------------------------------------------------------------
# AC-01 搜索
# ---------------------------------------------------------------------------

def test_search_filters_by_username(users_v2_env):
    app, _, client = users_v2_env
    r = client.get("/users?q=userA")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    # userA 出现，其他同名 userB/C/D 不出现（除非用户名也含 userA）。
    assert "userA" in body
    assert "userB" not in body
    assert "userC" not in body
    assert "userD" not in body


def test_search_min_length_2(users_v2_env):
    """1 字符搜索不过滤（前端 + 后端都按 ≥2 字）。"""
    app, _, client = users_v2_env
    r = client.get("/users?q=u")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    # 1 字符不该过滤 —— 所有用户都在
    for u in ("userA", "userB", "userC", "userD", "super"):
        assert u in body


def test_search_returns_json_envelope(users_v2_env):
    app, _, client = users_v2_env
    r = client.get("/users?q=userA&format=json")
    assert r.status_code == 200
    data = r.get_json()
    assert "users" in data and isinstance(data["users"], list)
    assert all(u["username"] == "userA" for u in data["users"])
    assert data["total"] == 1
    assert data["page"] == 1


# ---------------------------------------------------------------------------
# AC-02 角色筛选
# ---------------------------------------------------------------------------

def test_role_filter_manager(users_v2_env):
    app, _, client = users_v2_env
    r = client.get("/users?role=manager&format=json")
    data = r.get_json()
    usernames = {u["username"] for u in data["users"]}
    # userA(manager), userD(manager) — userD is_active=0 但角色筛选不过滤状态
    assert usernames == {"userA", "userD"}


def test_role_filter_staff(users_v2_env):
    app, _, client = users_v2_env
    r = client.get("/users?role=staff&format=json")
    data = r.get_json()
    usernames = {u["username"] for u in data["users"]}
    assert usernames == {"userB"}


def test_role_filter_admin(users_v2_env):
    app, _, client = users_v2_env
    r = client.get("/users?role=admin&format=json")
    data = r.get_json()
    # userC 在 wh_t2 上有 admin 角色；super 是平台 admin 但没有仓库绑定
    # —— 平台 admin 不算 role=admin（这是仓库级角色）。
    usernames = {u["username"] for u in data["users"]}
    assert usernames == {"userC"}


# ---------------------------------------------------------------------------
# AC-03 启停筛选
# ---------------------------------------------------------------------------

def test_is_active_filter_only_inactive(users_v2_env):
    app, _, client = users_v2_env
    r = client.get("/users?is_active=0&format=json")
    data = r.get_json()
    usernames = {u["username"] for u in data["users"]}
    assert usernames == {"userD"}


def test_is_active_filter_only_active(users_v2_env):
    app, _, client = users_v2_env
    r = client.get("/users?is_active=1&format=json")
    data = r.get_json()
    usernames = {u["username"] for u in data["users"]}
    # userD 是唯一 is_active=0，其余全部 1
    assert "userD" not in usernames
    assert usernames == {"super", "userA", "userB", "userC"}


# ---------------------------------------------------------------------------
# AC-09 租户筛选
# ---------------------------------------------------------------------------

def test_admin_sees_all_tenants_by_default(users_v2_env):
    app, _, client = users_v2_env
    r = client.get("/users?format=json")
    data = r.get_json()
    usernames = {u["username"] for u in data["users"]}
    # super(admin) 跨租户，默认看见全部 5 个
    assert usernames == {"super", "userA", "userB", "userC", "userD"}


def test_tenant_filter_scopes_to_one_tenant(users_v2_env):
    app, _, client = users_v2_env
    r = client.get("/users?tenant_id=2&format=json")
    data = r.get_json()
    usernames = {u["username"] for u in data["users"]}
    # super 也在 tenant 1，所以 tenant=2 时 super 不出现
    assert usernames == {"userC", "userD"}


# ---------------------------------------------------------------------------
# AC-06 toggle-active
# ---------------------------------------------------------------------------

def test_toggle_active_flips(users_v2_env):
    app, _, client = users_v2_env
    # userB 初始 is_active=1
    r = client.post("/users/3/toggle-active",
                    data={"csrf_token": "x"},  # CSRF gate 在 TESTING 下默认关闭
                    follow_redirects=False)
    assert r.status_code == 302
    # 校验 DB
    m = sqlite3.connect(users_v2_env[1])
    row = m.execute("SELECT is_active FROM users WHERE id=3").fetchone()
    m.close()
    assert row[0] == 0


def test_toggle_active_blocks_self(users_v2_env):
    """admin 不能停用自己。"""
    app, _, client = users_v2_env
    r = client.post("/users/1/toggle-active", data={"csrf_token": "x"})
    assert r.status_code == 302
    # flash 消息需要 follow_redirect 才能在下一渲染的页面看到。
    r2 = client.get("/users", follow_redirects=False)
    assert r2.status_code == 200
    body = r2.get_data(as_text=True)
    assert "不能停用" in body


# ---------------------------------------------------------------------------
# AC-10 停用登录拦截
# ---------------------------------------------------------------------------

def test_inactive_user_cannot_log_in(tmp_path, monkeypatch):
    """v2: users.is_active=0 必须在 auth.login 拒绝。"""
    import db as db_module
    from db import init_master_db, init_warehouse_db

    master_path = tmp_path / "master.db"
    wh_dir = tmp_path / "warehouses"
    wh_dir.mkdir()
    wh_path = wh_dir / "wh_test.db"

    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_dir)
    import config as config_module
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", wh_dir)

    init_master_db()
    init_warehouse_db(wh_path)

    # Insert user with is_active=0
    m = sqlite3.connect(master_path)
    m.execute(
        "INSERT INTO users (username, password_hash, is_admin, created_at, is_active) "
        "VALUES ('inactive', 'x', 0, ?, 0)", (TS,))
    m.commit()
    m.close()

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    # 不管密码对错，is_active=0 必须先被拒
    r = client.post("/login", data={"username": "inactive", "password": "anything"})
    assert r.status_code == 403
    body = r.get_data(as_text=True)
    assert "已停用" in body


def test_active_user_can_log_in(tmp_path, monkeypatch):
    """sanity check: is_active=1 走原登录路径（密码错仍 401）。"""
    import db as db_module
    from db import init_master_db, init_warehouse_db

    master_path = tmp_path / "master.db"
    wh_dir = tmp_path / "warehouses"
    wh_dir.mkdir()
    wh_path = wh_dir / "wh_test.db"

    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_dir)
    import config as config_module
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", wh_dir)

    init_master_db()
    init_warehouse_db(wh_path)

    m = sqlite3.connect(master_path)
    m.execute(
        "INSERT INTO users (username, password_hash, is_admin, created_at, is_active) "
        "VALUES ('active', 'x', 0, ?, 1)", (TS,))
    m.commit()
    m.close()

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    # 密码错（'x' 不可能是 pbkdf2:sha256 的合法哈希）→ 401（不是 403）
    r = client.post("/login", data={"username": "active", "password": "wrong"})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# AC-07 bulk-bind
# ---------------------------------------------------------------------------

def test_bulk_bind_creates_rows(users_v2_env):
    app, master_path, client = users_v2_env
    r = client.post("/users/bulk-bind", data={
        "user_ids": "2,3",
        "warehouse_id": "2",  # wh_t2 (tenant 2)
        "role": "staff",
    }, follow_redirects=False)
    assert r.status_code == 302
    m = sqlite3.connect(master_path)
    rows = m.execute(
        "SELECT user_id, warehouse_id, role FROM warehouse_users "
        "WHERE warehouse_id=2 AND role='staff' ORDER BY user_id"
    ).fetchall()
    m.close()
    assert {r[0] for r in rows} == {2, 3}


def test_bulk_bind_idempotent(users_v2_env):
    """已经绑定的 user+warehouse 第二次 bulk-bind 不应抛 IntegrityError。"""
    app, master_path, client = users_v2_env
    # userC 已经绑到 wh_t2 (admin)，第二次 bulk-bind 同 (userC, wh_t2) 应被 INSERT OR IGNORE 跳过。
    r = client.post("/users/bulk-bind", data={
        "user_ids": "4",
        "warehouse_id": "2",
        "role": "admin",
    }, follow_redirects=False)
    assert r.status_code == 302
    # DB 里仍然只有一条 (userC, wh_t2, admin)
    m = sqlite3.connect(master_path)
    count = m.execute(
        "SELECT COUNT(*) FROM warehouse_users WHERE user_id=4 AND warehouse_id=2"
    ).fetchone()[0]
    m.close()
    assert count == 1


def test_bulk_bind_invalid_role_400_flash(users_v2_env):
    app, _, client = users_v2_env
    r = client.post("/users/bulk-bind", data={
        "user_ids": "2",
        "warehouse_id": "1",
        "role": "boss",  # 非法角色
    }, follow_redirects=False)
    assert r.status_code == 302  # flash + redirect
    # flash 消息需要在后续 GET /users 渲染的页面里读到
    r2 = client.get("/users", follow_redirects=False)
    assert r2.status_code == 200
    body = r2.get_data(as_text=True)
    assert "角色必须" in body


def test_bulk_bind_invalid_warehouse_400_flash(users_v2_env):
    app, _, client = users_v2_env
    r = client.post("/users/bulk-bind", data={
        "user_ids": "2",
        "warehouse_id": "999",  # 不存在
        "role": "staff",
    }, follow_redirects=False)
    assert r.status_code == 302
    r2 = client.get("/users", follow_redirects=False)
    body = r2.get_data(as_text=True)
    assert "仓库不存在" in body


def test_bulk_bind_empty_user_ids_400(users_v2_env):
    app, _, client = users_v2_env
    r = client.post("/users/bulk-bind", data={
        "user_ids": "",
        "warehouse_id": "1",
        "role": "staff",
    }, follow_redirects=False)
    assert r.status_code == 302
    r2 = client.get("/users", follow_redirects=False)
    body = r2.get_data(as_text=True)
    assert "至少选择一个用户" in body


# ---------------------------------------------------------------------------
# AC-11 分页
# ---------------------------------------------------------------------------

def test_pagination_truncates_and_reports_total(tmp_path, monkeypatch):
    """插入 1 super + 25 普通用户，page_size=10 → page 1 出 10 条，total=26。"""
    import db as db_module
    from db import init_master_db, init_warehouse_db

    master_path = tmp_path / "master.db"
    wh_dir = tmp_path / "warehouses"
    wh_dir.mkdir()
    wh_path = wh_dir / "wh_test.db"

    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_dir)
    import config as config_module
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", wh_dir)

    init_master_db()
    init_warehouse_db(wh_path)

    m = sqlite3.connect(master_path)
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (1, 'super', 'x', 1, ?)", (TS,))
    for i in range(2, 27):
        m.execute(
            "INSERT INTO users (username, password_hash, is_admin, created_at) "
            "VALUES (?, 'x', 0, ?)", (f"u{i:03d}", TS))
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) "
        "VALUES (1, 'wh_test', '测试仓', ?, 'storefront', ?)", (str(wh_path), TS))
    m.commit()
    m.close()

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = 1
        s["warehouse_id"] = 1

    r = client.get("/users?page_size=10&page=1&format=json")
    data = r.get_json()
    assert len(data["users"]) == 10
    assert data["total"] == 26  # 1 super + 25
    assert data["total_pages"] == 3
    assert data["page"] == 1

    r = client.get("/users?page_size=10&page=3&format=json")
    data = r.get_json()
    assert len(data["users"]) == 6  # 26 - 2*10
    assert data["page"] == 3


def test_pagination_max_page_size_capped(users_v2_env):
    app, _, client = users_v2_env
    r = client.get("/users?page_size=999&format=json")
    data = r.get_json()
    # MAX_PAGE_SIZE = 100；这里一共 5 个用户，全返回
    assert len(data["users"]) == 5
    assert data["page_size"] == 100  # 服务端强制 cap 到 100


# ---------------------------------------------------------------------------
# AC-05 行渲染（data-* attrs 给抽屉 JS 用）
# ---------------------------------------------------------------------------

def test_row_data_attrs_present(users_v2_env):
    app, _, client = users_v2_env
    r = client.get("/users?format=html")
    body = r.get_data(as_text=True)
    # 每行应带 data-user-id / data-username / data-is-admin / data-is-active / data-tenant-id
    assert 'data-user-id="2"' in body
    assert 'data-username="userA"' in body
    assert 'data-is-admin="0"' in body
    assert 'data-is-active="1"' in body


def test_inactive_row_renders_offline_badge(users_v2_env):
    app, _, client = users_v2_env
    r = client.get("/users?is_active=0")
    body = r.get_data(as_text=True)
    assert "已停用" in body
    assert "userD" in body


# ---------------------------------------------------------------------------
# Schema 迁移：users.is_active + idx_users_tenant_active
# ---------------------------------------------------------------------------

def test_schema_migration_adds_is_active(tmp_path, monkeypatch):
    """init_master_db() on a fresh db adds users.is_active + idx_users_tenant_active."""
    import db as db_module
    from db import init_master_db

    master_path = tmp_path / "master.db"
    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    import config as config_module
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)

    init_master_db()

    cols = {r[1] for r in sqlite3.connect(master_path).execute(
        "PRAGMA table_info(users)"
    ).fetchall()}
    assert "is_active" in cols

    indexes = {r[0] for r in sqlite3.connect(master_path).execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='users'"
    ).fetchall()}
    assert "idx_users_tenant_active" in indexes


def test_schema_migration_idempotent(tmp_path, monkeypatch):
    """二次 init_master_db() 不抛「duplicate column name」。"""
    import db as db_module
    from db import init_master_db

    master_path = tmp_path / "master.db"
    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    import config as config_module
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)

    init_master_db()
    init_master_db()  # 第二次：必须幂等
    init_master_db()  # 第三次：稳


# ---------------------------------------------------------------------------
# 既有 endpoint 仍工作（回归）
# ---------------------------------------------------------------------------

def test_existing_create_user_still_works(users_v2_env):
    """v2 不破坏既有 create_user endpoint。"""
    app, master_path, client = users_v2_env
    r = client.post("/users/create", data={
        "username": "newbie",
        "password": "pw123456",
        "is_admin": "0",
    }, follow_redirects=False)
    assert r.status_code == 302
    m = sqlite3.connect(master_path)
    row = m.execute(
        "SELECT username, is_admin, is_active FROM users WHERE username='newbie'"
    ).fetchone()
    m.close()
    assert row is not None
    assert row[1] == 0  # not admin
    assert row[2] == 1  # is_active 默认 1


def test_existing_reset_password_still_works(users_v2_env):
    app, _, client = users_v2_env
    r = client.post("/users/3/reset-password", data={
        "password": "newpass",
    }, follow_redirects=False)
    assert r.status_code == 302


def test_existing_toggle_admin_blocks_self(users_v2_env):
    app, _, client = users_v2_env
    r = client.post("/users/1/toggle-admin", data={"csrf_token": "x"})
    assert r.status_code == 302
    r2 = client.get("/users", follow_redirects=False)
    body = r2.get_data(as_text=True)
    assert "不能修改自己" in body


def test_existing_delete_blocks_self(users_v2_env):
    app, _, client = users_v2_env
    r = client.post("/users/1/delete", data={"csrf_token": "x"})
    assert r.status_code == 302
    r2 = client.get("/users", follow_redirects=False)
    body = r2.get_data(as_text=True)
    assert "不能删除自己" in body


# ---------------------------------------------------------------------------
# 排序（sort whitelist + 方向）
#
# 回归：v1 的 whitelist 用未剥离 "-" 的原始字符串做集合判断，导致
# "-last_login_at" 落到 fallback "id" 而方向仍是 DESC —— 工具栏「最近登录」
# 实际按 id 倒序，任何降序排序都静默失效。
# ---------------------------------------------------------------------------

def _seed_sort_users(master_path):
    """插入 id 顺序 / 字典序 / 登录时间三者互不一致的数据，用于判别排序生效。"""
    m = sqlite3.connect(master_path)
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at, "
        "tenant_id, is_active, last_login_at) "
        "VALUES (6, 'zzz', 'x', 0, ?, 1, 1, '2026-03-01')",
        (TS,),
    )
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at, "
        "tenant_id, is_active, last_login_at) "
        "VALUES (7, 'aaa', 'x', 0, ?, 1, 1, '2026-01-05')",
        (TS,),
    )
    # userB(3) 是最新登录 —— id 排在中间，能区分「按 id」与「按登录时间」。
    m.execute("UPDATE users SET last_login_at=? WHERE id=3", ("2026-05-01",))
    m.commit()
    m.close()


def _user_order(client, query):
    r = client.get(f"/users?{query}&format=json&page_size=100")
    assert r.status_code == 200, (query, r.status_code)
    return [u["username"] for u in r.get_json()["users"]]


def test_sort_asc_username_orders_by_username(users_v2_env):
    app, master_path, client = users_v2_env
    _seed_sort_users(master_path)
    assert _user_order(client, "sort=username")[0] == "aaa"


def test_sort_desc_username_orders_by_username(users_v2_env):
    """?sort=-username 必须按用户名字典序倒序（而不是 id 倒序）。"""
    app, master_path, client = users_v2_env
    _seed_sort_users(master_path)
    order = _user_order(client, "sort=-username")
    assert order[0] == "zzz", order
    assert order == sorted(order, reverse=True), order


def test_sort_desc_last_login_at_orders_by_last_login(users_v2_env):
    """?sort=-last_login_at（工具栏「最近登录」）必须按 last_login_at 倒序。"""
    app, master_path, client = users_v2_env
    _seed_sort_users(master_path)
    order = _user_order(client, "sort=-last_login_at")
    assert order[0] == "userB", order
    assert order[1] == "zzz", order


def test_sort_unknown_column_falls_back_and_stays_safe(users_v2_env):
    """未知/注入式 sort 值必须被 whitelist 拦下，回退 id 升序且不报错。"""
    app, _, client = users_v2_env
    order = _user_order(client, "sort=username%3B%20DROP%20TABLE%20users")
    assert order == ["super", "userA", "userB", "userC", "userD"], order
    # 表还在 → 后续请求仍正常
    assert client.get("/users").status_code == 200


# ---------------------------------------------------------------------------
# 创建账号的权限矩阵默认值
#
# 回归：抽屉里给 manager 也加了 checked，浏览器取文档序靠后的那个，于是
# 「不动矩阵直接创建」会给新账号授上**每个仓库**的 manager（v1 默认无权限）。
# JS 侧的 checked 数量由浏览器冒烟验证；这里守住服务端那一半。
# ---------------------------------------------------------------------------

def test_create_with_default_empty_matrix_grants_no_bindings(users_v2_env):
    """wh_<id>="" （默认「无」）提交时不得写入任何仓库绑定。"""
    app, master_path, client = users_v2_env
    r = client.post("/users/create", data={
        "username": "newbie", "password": "pw",
        "csrf_token": "x", "wh_1": "", "wh_2": "",
    })
    assert r.status_code == 302
    m = sqlite3.connect(master_path)
    uid = m.execute("SELECT id FROM users WHERE username='newbie'").fetchone()[0]
    n = m.execute(
        "SELECT COUNT(*) FROM warehouse_users WHERE user_id=?", (uid,)
    ).fetchone()[0]
    m.close()
    assert n == 0


def test_create_with_explicit_role_binds_only_that_warehouse(users_v2_env):
    """显式选 manager 时只绑定被选中的那一个仓库。"""
    app, master_path, client = users_v2_env
    r = client.post("/users/create", data={
        "username": "newbie2", "password": "pw",
        "csrf_token": "x", "wh_1": "manager", "wh_2": "",
    })
    assert r.status_code == 302
    m = sqlite3.connect(master_path)
    uid = m.execute("SELECT id FROM users WHERE username='newbie2'").fetchone()[0]
    rows = m.execute(
        "SELECT warehouse_id, role FROM warehouse_users WHERE user_id=? ORDER BY warehouse_id",
        (uid,),
    ).fetchall()
    m.close()
    assert rows == [(1, "manager")]


# ---------------------------------------------------------------------------
# /users 不可缓存
#
# 回归：本 PR 让工具栏用 fetch 拉取 /users 的整页 HTML，而这属于"非导航 GET"，
# 会被 sw.js 的 cache-first 分支写进 Cache API。实测后果：① 服务端数据变了
# 列表仍显示旧的；② 缓存副本按 URL 命中即返回、不经过服务端鉴权 —— 匿名会话
# 也能拿到 200 的管理员页面。sw.js 已收窄为只缓存 /static/，这里守住第二道闸。
# ---------------------------------------------------------------------------

def test_users_html_is_no_store(users_v2_env):
    app, _, client = users_v2_env
    r = client.get("/users")
    assert r.status_code == 200
    assert r.headers.get("Cache-Control") == "no-store"


def test_users_json_is_no_store(users_v2_env):
    app, _, client = users_v2_env
    r = client.get("/users?format=json")
    assert r.status_code == 200
    assert r.headers.get("Cache-Control") == "no-store"


def test_users_service_worker_does_not_cache_dynamic_get():
    """sw.js 不得再对非 /static/ 的 GET 走 cache-first。

    这条是静态断言（JS 侧没有 pytest 覆盖的替代品）：把 sw.js 里"只缓存
    /static/"的那道判断钉住，防止有人把它改回"所有非导航 GET 都缓存"。
    """
    from pathlib import Path

    sw = (Path(__file__).resolve().parent.parent / "static" / "sw.js").read_text(
        encoding="utf-8"
    )
    assert 'url.pathname.indexOf("/static/") !== 0' in sw, "sw.js 缺少 /static/ 范围判断"
    assert "cache.put(req, copy)" in sw
    # cache.put 必须受 res.ok 与范围判断保护，不能无条件写入
    assert "if (res.ok) {" in sw
