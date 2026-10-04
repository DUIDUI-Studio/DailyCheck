"""End-to-end tests for store-ordering permissions and edge cases."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from db import init_master_db, init_warehouse_db


@pytest.fixture
def e2e_env(tmp_path, monkeypatch):
    """Multi-user environment with two DCs to test switching."""
    import config as config_module
    import db as db_module

    master_path = tmp_path / "master.db"
    wh_dir = tmp_path / "warehouses"
    wh_dir.mkdir()
    dc1_path = wh_dir / "dc1_test.db"
    dc2_path = wh_dir / "dc2_test.db"
    store_path = wh_dir / "store_test.db"

    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_dir)
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", wh_dir)

    init_master_db()
    init_warehouse_db(dc1_path)
    init_warehouse_db(dc2_path)
    init_warehouse_db(store_path)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    m = sqlite3.connect(str(master_path))
    m.row_factory = sqlite3.Row
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (1, 'store_staff', 'x', 0, ?)",
        (ts,),
    )
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (2, 'store_mgr', 'x', 0, ?)",
        (ts,),
    )
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (3, 'dc_mgr', 'x', 0, ?)",
        (ts,),
    )
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (4, 'admin', 'x', 1, ?)",
        (ts,),
    )
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) VALUES (1, 'dc1_test', '测试配送中心1', ?, 'distribution_center', ?)",
        (str(dc1_path), ts),
    )
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) VALUES (2, 'dc2_test', '测试配送中心2', ?, 'distribution_center', ?)",
        (str(dc2_path), ts),
    )
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) VALUES (3, 'store_test', '测试门店', ?, 'storefront', ?)",
        (str(store_path), ts),
    )
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (1, 3, 'staff')")
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (2, 3, 'manager')")
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (3, 1, 'manager')")
    m.execute(
        "INSERT INTO canonical_categories (code, name, description, created_at, updated_at) VALUES ('PACKAGING', '包材', '', ?, ?)",
        (ts, ts),
    )
    m.execute(
        """INSERT INTO canonical_items
           (id, canonical_sku, name, category_code, unit, gram_per_unit, aux_unit, aux_rate,
            status, created_from, created_at, updated_at)
           VALUES (101, 'IC-000101', '测试包材A', 'PACKAGING', '件', 0, NULL, 0, 'active', 'rd_manual', ?, ?)""",
        (ts, ts),
    )
    m.execute(
        """INSERT INTO canonical_items
           (id, canonical_sku, name, category_code, unit, gram_per_unit, aux_unit, aux_rate,
            status, created_from, created_at, updated_at)
           VALUES (102, 'IC-000102', '测试包材B', 'PACKAGING', '件', 0, NULL, 0, 'active', 'rd_manual', ?, ?)""",
        (ts, ts),
    )
    m.commit()
    m.close()

    dc1 = sqlite3.connect(str(dc1_path))
    dc1.row_factory = sqlite3.Row
    cat_id = dc1.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()["id"]
    dc1.execute(
        "INSERT INTO items (sku, name, category_id, quantity, unit, canonical_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("DC1-A", "测试包材A", cat_id, 100.0, "件", 101, ts),
    )
    dc1.commit()
    dc1.close()

    dc2 = sqlite3.connect(str(dc2_path))
    dc2.row_factory = sqlite3.Row
    cat_id = dc2.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()["id"]
    dc2.execute(
        "INSERT INTO items (sku, name, category_id, quantity, unit, canonical_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("DC2-A", "测试包材A", cat_id, 50.0, "件", 101, ts),
    )
    dc2.commit()
    dc2.close()

    store = sqlite3.connect(str(store_path))
    store.row_factory = sqlite3.Row
    cat_id = store.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()["id"]
    store.execute(
        "INSERT INTO items (sku, name, category_id, quantity, unit, canonical_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("ST-A", "测试包材A", cat_id, 0.0, "件", 101, ts),
    )
    # 102 unbound intentionally
    store.commit()
    store.close()

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    yield {
        "app": app,
        "client": client,
        "master_path": master_path,
        "dc1_path": dc1_path,
        "dc2_path": dc2_path,
        "store_path": store_path,
    }


def _login_as(client, user_id, warehouse_id):
    with client.session_transaction() as s:
        s["user_id"] = user_id
        s["warehouse_id"] = warehouse_id


def test_store_user_cannot_access_dc_routes(e2e_env):
    client = e2e_env["client"]
    _login_as(client, 1, 3)
    assert client.get("/store-ordering/review").status_code == 403
    assert client.get("/store-ordering/shipments").status_code == 403


def test_dc_user_redirected_from_catalog_to_review(e2e_env):
    client = e2e_env["client"]
    _login_as(client, 3, 1)
    resp = client.get("/store-ordering/catalog", follow_redirects=True)
    assert resp.status_code == 200
    assert "订货审批" in resp.data.decode()


def test_admin_can_access_admin_orders(e2e_env):
    client = e2e_env["client"]
    _login_as(client, 4, 3)
    resp = client.get("/store-ordering/admin/orders")
    assert resp.status_code == 200


def test_switch_dc_clears_cart(e2e_env):
    client = e2e_env["client"]
    _login_as(client, 1, 3)
    client.post(
        "/store-ordering/cart/add",
        data={"dc": "dc1_test", "canonical_id": 101, "quantity": "5", "unit": "件"},
    )
    resp = client.get("/store-ordering/cart")
    assert b"5" in resp.data

    # Switch DC via catalog.
    client.get("/store-ordering/catalog?dc=dc2_test")
    resp = client.get("/store-ordering/cart", follow_redirects=True)
    assert resp.status_code == 200
    # Cart should be empty after switch.
    assert "购物车为空" in resp.data.decode()


def test_shortage_blocks_submit_and_ship(e2e_env):
    client = e2e_env["client"]
    _login_as(client, 1, 3)
    # DC1 has 100, order 200 to trigger shortage.
    client.post(
        "/store-ordering/cart/add",
        data={"dc": "dc1_test", "canonical_id": 101, "quantity": "200", "unit": "件"},
    )
    tomorrow = (datetime.now().date() + timedelta(days=1)).isoformat()
    resp = client.post(
        "/store-ordering/cart/submit",
        data={"expected_delivery_date": tomorrow, "note": ""},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "库存不足" in resp.data.decode()


def test_unbound_canonical_blocks_submit(e2e_env):
    client = e2e_env["client"]
    _login_as(client, 1, 3)
    # canonical 102 is not bound in store.
    client.post(
        "/store-ordering/cart/add",
        data={"dc": "dc1_test", "canonical_id": 102, "quantity": "1", "unit": "件"},
    )
    tomorrow = (datetime.now().date() + timedelta(days=1)).isoformat()
    resp = client.post(
        "/store-ordering/cart/submit",
        data={"expected_delivery_date": tomorrow, "note": ""},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "未绑定" in body or "未在门店绑定" in body
