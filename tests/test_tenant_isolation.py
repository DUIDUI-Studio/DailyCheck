"""多租户 PoC（P0-1 轻方案）隔离测试。

模型：tenants 注册表；users/warehouses/canonical_items.tenant_id（默认 1）；
平台管理员 is_admin=1 跨租户；普通用户锁本租户。
PoC 范围 = 租户闸 + 数据放置；canonical 扇出/订单/MCP 的全链路租户化
属 PoC 通过后的全力改造阶段（见 2026-10-10 计划文档）。
"""
import sqlite3

import pytest

from migrations import MIGRATIONS
from migrations_runner import current_version, migrate_down, migrate_up

TS = "2026-10-10 00:00:00"


@pytest.fixture
def two_tenants(tmp_path, monkeypatch):
    """两个租户、各一仓、跨租户脏绑定（userA 绑了租户 2 的仓，验证租户闸兜底）。"""
    import db as db_module
    from db import init_master_db, init_warehouse_db

    master_path = tmp_path / "master.db"
    wh_dir = tmp_path / "warehouses"
    wh_dir.mkdir()
    wh1 = wh_dir / "wh_t1.db"
    wh2 = wh_dir / "wh_t2.db"

    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_dir)
    import config as config_module
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", wh_dir)

    init_master_db()
    init_warehouse_db(wh1)
    init_warehouse_db(wh2)

    m = sqlite3.connect(master_path)
    m.execute("INSERT INTO tenants (id, name, created_at) VALUES (1, '默认', ?)", (TS,))
    m.execute("INSERT INTO tenants (id, name, created_at) VALUES (2, '客户B', ?)", (TS,))
    # 用户 10 = 租户 1 仓内 admin（非平台管理员）；用户 20 = 租户 2 普通用户；
    # 用户 99 = 平台管理员（租户 1）。
    for uid, name, is_admin, tenant in [
        (10, "userA", 0, 1), (20, "userB", 0, 2), (99, "super", 1, 1),
    ]:
        m.execute(
            "INSERT INTO users (id, username, password_hash, is_admin, created_at, tenant_id) "
            "VALUES (?, ?, 'x', ?, ?, ?)", (uid, name, is_admin, TS, tenant))
    for wid, code, path, tenant in [
        (1, "wh_t1", str(wh1), 1), (2, "wh_t2", str(wh2), 2),
    ]:
        m.execute(
            "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at, tenant_id) "
            "VALUES (?, ?, ?, ?, 'storefront', ?, ?)", (wid, code, code, path, TS, tenant))
    # 绑定：userA→wh_t1(admin) + 脏绑定 userA→wh_t2；userB→wh_t2(staff)
    m.execute("INSERT INTO warehouse_users VALUES (10, 1, 'admin')")
    m.execute("INSERT INTO warehouse_users VALUES (10, 2, 'staff')")  # 脏数据
    m.execute("INSERT INTO warehouse_users VALUES (20, 2, 'staff')")
    m.commit()
    m.close()

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app, wh1, wh2


def _client_with(app, user_id, warehouse_id=None):
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = user_id
        if warehouse_id is not None:
            s["warehouse_id"] = warehouse_id
    return client


def test_picker_hides_cross_tenant_dirty_binding(two_tenants):
    """userA 虽有 wh_t2 绑定（脏数据），picker 只能看见本租户仓。"""
    app, _, _ = two_tenants
    client = _client_with(app, 10)
    r = client.get("/select-warehouse")
    body = r.get_data(as_text=True)
    assert "wh_t1" in body
    assert "wh_t2" not in body


def test_select_cross_tenant_warehouse_403(two_tenants):
    """脏绑定也过不了租户闸：userA 选 wh_t2 → 403。"""
    app, _, _ = two_tenants
    client = _client_with(app, 10)
    assert client.post("/select-warehouse/2").status_code == 403


def test_request_hook_rejects_cross_tenant_warehouse(two_tenants):
    """会话里强塞跨租户 warehouse_id：钩子不加载该仓 → 重定向 picker。"""
    app, _, _ = two_tenants
    client = _client_with(app, 10, warehouse_id=2)
    r = client.get("/inventory")
    assert r.status_code == 302
    assert "/select-warehouse" in r.headers["Location"]


def test_own_tenant_warehouse_still_works(two_tenants):
    """本租户内正常访问不受影响（防租户闸写反误伤）。"""
    app, _, _ = two_tenants
    client = _client_with(app, 10, warehouse_id=1)
    assert client.get("/select-warehouse").status_code == 200
    assert client.post("/select-warehouse/1").status_code == 302


def test_platform_admin_bypasses_tenant(two_tenants):
    """平台管理员（is_admin=1）跨租户可用（运营者视角）。"""
    app, _, _ = two_tenants
    client = _client_with(app, 99, warehouse_id=2)
    assert client.post("/select-warehouse/2").status_code == 302


def test_tenant_b_user_untouched_by_tenant_a_data(two_tenants):
    """userB（租户 2）picker 只见 wh_t2。"""
    app, _, _ = two_tenants
    client = _client_with(app, 20)
    body = client.get("/select-warehouse").get_data(as_text=True)
    assert "wh_t2" in body and "wh_t1" not in body


def test_default_tenant_backfill_keeps_legacy_rows(tmp_path, monkeypatch):
    """老库（无 tenant_id 列）跑迁移 0002 后，存量行自动归属租户 1。"""
    import db as db_module
    from db import init_master_db

    master_path = tmp_path / "master.db"
    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    import config as config_module
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    init_master_db()
    m = sqlite3.connect(master_path)
    m.execute(
        "INSERT INTO users (username, password_hash, is_admin, created_at) "
        "VALUES ('legacy', 'x', 0, ?)", (TS,))
    m.commit()
    tenant_id = m.execute(
        "SELECT tenant_id FROM users WHERE username='legacy'").fetchone()[0]
    m.close()
    assert tenant_id == 1


def test_migration_0002_up_down(tmp_path):
    """0002 在含旧结构表的库上 up 幂等、down 可逆。"""
    p = tmp_path / "master.db"
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)")
    conn.execute("CREATE TABLE warehouses (id INTEGER PRIMARY KEY, code TEXT)")
    conn.execute("CREATE TABLE canonical_items (id INTEGER PRIMARY KEY, sku TEXT)")
    conn.commit()
    conn.close()

    migs = [(v, d, up, down) for v, d, up, down in MIGRATIONS if v == 2]
    assert migrate_up(p, migs, tmp_path / "bak") == [2]
    assert migrate_up(p, migs, tmp_path / "bak") == []  # 幂等
    assert migrate_down(p, 1, migs, tmp_path / "bak") == [2]

    conn = sqlite3.connect(p)
    assert current_version(conn) == 1
    ucols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    conn.close()
    assert "tenant_id" not in ucols
