"""平台管理员配置页：权限、租户、CSRF 与完整 CRUD。"""
import sqlite3

import pytest

TS = "2026-10-29 12:00:00"
URL = "/admin/item-routing"


@pytest.fixture
def routing_client(logged_client):
    import db

    client, _ = logged_client
    with sqlite3.connect(db.MASTER_DB) as master:
        master.execute("INSERT OR IGNORE INTO tenants VALUES (1, 'existing', 'active', ?)", (TS,))
        master.execute(
            "INSERT INTO warehouses (code, name, db_path, warehouse_type, created_at) "
            "SELECT 'A', '配送中心A', db_path, 'distribution_center', ? FROM warehouses LIMIT 1",
            (TS,))
        master.execute(
            "INSERT INTO canonical_items (id, canonical_sku, name, unit, created_at, updated_at) "
            "VALUES (1, 'IC-1', '路由测试品', '件', ?, ?)", (TS, TS))
    return client


def test_admin_list_pending_set_delete(routing_client):
    import db

    client = routing_client
    resp = client.get(URL)
    assert resp.status_code == 200
    assert "路由测试品" in resp.text and "待配置" in resp.text
    assert 'name="csrf_token"' in resp.text
    resp = client.post(URL + "/1", data={"dc_code": "A"})
    assert resp.status_code == 302
    with sqlite3.connect(db.MASTER_DB) as master:
        row = master.execute(
            "SELECT fulfillment_warehouse_code, updated_by FROM item_routing").fetchone()
        assert row == ("A", 1)
    assert "配送中心A" in client.get(URL).text
    assert client.get(URL + "/1/delete").status_code == 405
    assert client.post(URL + "/1/delete").status_code == 302
    with sqlite3.connect(db.MASTER_DB) as master:
        assert master.execute("SELECT COUNT(*) FROM item_routing").fetchone()[0] == 0


@pytest.mark.parametrize("suffix,data", [("", {}), ("/1", {"dc_code": "A"}), ("/1/delete", {})])
def test_non_platform_admin_forbidden(nav_client_factory, suffix, data):
    client, _ = nav_client_factory("distribution_center", "admin")
    resp = client.get(URL) if not suffix else client.post(URL + suffix, data=data)
    assert resp.status_code == 403


def test_anonymous_redirects_to_login(routing_client):
    with routing_client.session_transaction() as session:
        session.clear()
    assert "/login" in routing_client.get(URL).location


def test_invalid_target_does_not_write(routing_client):
    import db

    for code in ("wh_test", "unknown"):
        assert routing_client.post(URL + "/1", data={"dc_code": code}).status_code == 400
    with sqlite3.connect(db.MASTER_DB) as master:
        assert master.execute("SELECT COUNT(*) FROM item_routing").fetchone()[0] == 0


def test_other_tenant_platform_admin_fails_closed(routing_client):
    import db

    with sqlite3.connect(db.MASTER_DB) as master:
        master.execute("UPDATE users SET tenant_id=2 WHERE id=1")
    assert routing_client.get(URL).status_code == 403
    assert routing_client.post(URL + "/1", data={"dc_code": "A"}).status_code == 403
    assert routing_client.post(URL + "/1/delete").status_code == 403


def test_post_requires_csrf(routing_client):
    routing_client.application.config["WTF_CSRF_FORCE_ENABLED"] = True
    assert routing_client.post(URL + "/1", data={"dc_code": "A"}).status_code == 400


def test_pending_database_failure_is_visible(routing_client, tmp_path):
    import db

    with sqlite3.connect(db.MASTER_DB) as master:
        master.execute("UPDATE warehouses SET db_path=? WHERE code='A'",
                       (str(tmp_path / "missing.db"),))
    resp = routing_client.get(URL)
    assert resp.status_code == 503
    assert "无法读取" in resp.text


def test_platform_admin_can_discover_page_from_canonical(routing_client):
    resp = routing_client.get("/canonical/list")
    assert resp.status_code == 200
    assert 'href="/admin/item-routing"' in resp.text
