"""Route tests for storefront store-ordering flows."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from db import init_master_db, init_warehouse_db


@pytest.fixture
def store_env(tmp_path, monkeypatch):
    """Logged-in store staff + DC + storefront environment."""
    import config as config_module
    import db as db_module

    master_path = tmp_path / "master.db"
    wh_dir = tmp_path / "warehouses"
    wh_dir.mkdir()
    dc_path = wh_dir / "dc_test.db"
    store_path = wh_dir / "store_test.db"

    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_dir)
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", wh_dir)

    init_master_db()
    init_warehouse_db(dc_path)
    init_warehouse_db(store_path)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    m = sqlite3.connect(str(master_path))
    m.row_factory = sqlite3.Row
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (1, 'store_staff', 'x', 0, ?)",
        (ts,),
    )
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (2, 'dc_mgr', 'x', 0, ?)",
        (ts,),
    )
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (3, 'store_other', 'x', 0, ?)",
        (ts,),
    )
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) VALUES (1, 'dc_test', '测试配送中心', ?, 'distribution_center', ?)",
        (str(dc_path), ts),
    )
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) VALUES (2, 'store_test', '测试门店', ?, 'storefront', ?)",
        (str(store_path), ts),
    )
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (1, 2, 'staff')")
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (2, 1, 'manager')")
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (3, 2, 'staff')")
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

    dc = sqlite3.connect(str(dc_path))
    dc.row_factory = sqlite3.Row
    cat_id = dc.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()["id"]
    dc.execute(
        "INSERT INTO items (sku, name, category_id, quantity, unit, canonical_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("DC-A", "测试包材A", cat_id, 100.0, "件", 101, ts),
    )
    dc.execute(
        "INSERT INTO items (sku, name, category_id, quantity, unit, canonical_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("DC-B", "测试包材B", cat_id, 5.0, "件", 102, ts),
    )
    dc.commit()
    dc.close()

    store = sqlite3.connect(str(store_path))
    store.row_factory = sqlite3.Row
    cat_id = store.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()["id"]
    store.execute(
        "INSERT INTO items (sku, name, category_id, quantity, unit, canonical_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("ST-A", "测试包材A", cat_id, 0.0, "件", 101, ts),
    )
    # item 102 intentionally unbound
    store.commit()
    store.close()

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = 1
        s["warehouse_id"] = 2

    yield {
        "app": app,
        "client": client,
        "master_path": master_path,
        "dc_path": dc_path,
        "store_path": store_path,
    }


def test_catalog_selector(store_env):
    resp = store_env["client"].get("/store-ordering/catalog")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "dc_test" in body
    assert "选择配送中心" in body


def test_catalog_with_dc(store_env):
    resp = store_env["client"].get("/store-ordering/catalog?dc=dc_test")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "测试包材A" in body
    assert "加入购物车" in body


def test_add_to_cart_and_cart_view(store_env):
    client = store_env["client"]
    resp = client.post(
        "/store-ordering/cart/add",
        data={"dc": "dc_test", "canonical_id": 101, "quantity": "5", "unit": "件"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "测试包材A" in body
    assert "5" in body


def test_submit_order_success(store_env):
    client = store_env["client"]
    client.post(
        "/store-ordering/cart/add",
        data={"dc": "dc_test", "canonical_id": 101, "quantity": "5", "unit": "件"},
    )
    tomorrow = (datetime.now().date() + timedelta(days=1)).isoformat()
    resp = client.post(
        "/store-ordering/cart/submit",
        data={"expected_delivery_date": tomorrow, "note": "急需"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "订单" in body
    assert "pending" in body or "待审批" in body


def test_submit_order_shortage_blocked(store_env):
    client = store_env["client"]
    client.post(
        "/store-ordering/cart/add",
        data={"dc": "dc_test", "canonical_id": 102, "quantity": "10", "unit": "件"},
    )
    tomorrow = (datetime.now().date() + timedelta(days=1)).isoformat()
    resp = client.post(
        "/store-ordering/cart/submit",
        data={"expected_delivery_date": tomorrow, "note": ""},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "库存不足" in body


def test_submit_order_unbound_blocked(store_env):
    client = store_env["client"]
    # canonical 102 has store binding intentionally missing
    client.post(
        "/store-ordering/cart/add",
        data={"dc": "dc_test", "canonical_id": 102, "quantity": "1", "unit": "件"},
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


def test_store_orders_list(store_env):
    client = store_env["client"]
    client.post(
        "/store-ordering/cart/add",
        data={"dc": "dc_test", "canonical_id": 101, "quantity": "2", "unit": "件"},
    )
    tomorrow = (datetime.now().date() + timedelta(days=1)).isoformat()
    client.post(
        "/store-ordering/cart/submit",
        data={"expected_delivery_date": tomorrow, "note": ""},
    )
    resp = client.get("/store-ordering/orders")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "SO-" in body


def test_order_detail(store_env):
    client = store_env["client"]
    client.post(
        "/store-ordering/cart/add",
        data={"dc": "dc_test", "canonical_id": 101, "quantity": "3", "unit": "件"},
    )
    tomorrow = (datetime.now().date() + timedelta(days=1)).isoformat()
    client.post(
        "/store-ordering/cart/submit",
        data={"expected_delivery_date": tomorrow, "note": ""},
    )
    master = sqlite3.connect(str(store_env["master_path"]))
    master.row_factory = sqlite3.Row
    order_id = master.execute("SELECT id FROM store_orders ORDER BY id DESC LIMIT 1").fetchone()["id"]
    master.close()
    resp = client.get(f"/store-ordering/orders/{order_id}")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "测试包材A" in body


def _login_as(client, user_id, warehouse_id):
    with client.session_transaction() as s:
        s["user_id"] = user_id
        s["warehouse_id"] = warehouse_id


def test_other_user_cannot_modify_my_cart_item(store_env):
    """User 3 must not update or remove cart items belonging to user 1."""
    client = store_env["client"]
    client.post(
        "/store-ordering/cart/add",
        data={"dc": "dc_test", "canonical_id": 101, "quantity": "5", "unit": "件"},
    )
    master = sqlite3.connect(str(store_env["master_path"]))
    master.row_factory = sqlite3.Row
    cart_item_id = master.execute(
        "SELECT id FROM store_order_cart_items ORDER BY id DESC LIMIT 1"
    ).fetchone()["id"]
    master.close()

    _login_as(client, 3, 2)
    resp = client.post(
        f"/store-ordering/cart/update/{cart_item_id}",
        data={"quantity": "99"},
    )
    assert resp.status_code == 403

    resp = client.post(
        f"/store-ordering/cart/remove/{cart_item_id}",
        data={},
    )
    assert resp.status_code == 403
