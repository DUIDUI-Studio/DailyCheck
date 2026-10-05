"""Route tests for storefront receive (partial + full) flow."""
from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from blueprints import store_ordering_pure as sop
from db import init_master_db, init_warehouse_db


@pytest.fixture
def receive_env(tmp_path, monkeypatch):
    """Full env: storefront manager, store staff, DC manager, DC staff, admin."""
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
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (2, 'store_mgr', 'x', 0, ?)",
        (ts,),
    )
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (3, 'dc_mgr', 'x', 0, ?)",
        (ts,),
    )
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (4, 'dc_staff', 'x', 0, ?)",
        (ts,),
    )
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (5, 'store_other', 'x', 0, ?)",
        (ts,),
    )
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (6, 'admin', 'x', 1, ?)",
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
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (2, 2, 'manager')")
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (3, 1, 'manager')")
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (4, 1, 'staff')")
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (5, 2, 'staff')")
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
        ("DC-B", "测试包材B", cat_id, 50.0, "件", 102, ts),
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
        "dc_path": dc_path,
        "store_path": store_path,
    }


def _login_as(client, user_id, warehouse_id):
    with client.session_transaction() as s:
        s["user_id"] = user_id
        s["warehouse_id"] = warehouse_id


def _build_shipped_order(env, qty=10.0):
    """Submit → approve → ship an order; return the master row + order_id."""
    client = env["client"]
    master_path = env["master_path"]
    _login_as(client, 1, 2)
    client.post(
        "/store-ordering/cart/add",
        data={"dc": "dc_test", "canonical_id": 101, "quantity": str(qty), "unit": "件"},
    )
    client.post(
        "/store-ordering/cart/submit",
        data={"expected_delivery_date": datetime.now().strftime("%Y-%m-%d"), "note": ""},
    )
    master = sqlite3.connect(str(master_path))
    master.row_factory = sqlite3.Row
    order_id = master.execute(
        "SELECT id FROM store_orders ORDER BY id DESC LIMIT 1"
    ).fetchone()["id"]
    master.close()
    _login_as(client, 3, 1)
    client.post(
        f"/store-ordering/orders/{order_id}/review",
        data={"decision": "approved", "note": "ok"},
    )
    _login_as(client, 4, 1)
    client.post(
        f"/store-ordering/orders/{order_id}/ship",
        data={"tracking_note": ""},
    )
    return order_id


def test_store_manager_can_receive_one_item(receive_env):
    """Partial receipt: store qty +1, fulfilled=1, order stays shipped, no
    delivered notification."""
    order_id = _build_shipped_order(receive_env, qty=10.0)
    client = receive_env["client"]
    master = sqlite3.connect(str(receive_env["master_path"]))
    master.row_factory = sqlite3.Row
    order_item_id = master.execute(
        "SELECT id FROM store_order_items WHERE order_id=?", (order_id,)
    ).fetchone()["id"]
    # Initial notification count for delivered
    before = master.execute(
        "SELECT COUNT(*) AS c FROM notifications WHERE event_type=? AND target_url=?",
        (sop.EVENT_ORDER_DELIVERED, f"/store-ordering/orders/{order_id}"),
    ).fetchone()["c"]
    master.close()

    _login_as(client, 2, 2)
    resp = client.post(
        f"/store-ordering/orders/{order_id}/receive",
        data={"order_item_id": order_item_id, "quantity": "1", "note": "first batch"},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    master = sqlite3.connect(str(receive_env["master_path"]))
    master.row_factory = sqlite3.Row
    item = master.execute(
        "SELECT fulfilled_quantity, status FROM store_order_items WHERE id=?",
        (order_item_id,),
    ).fetchone()
    assert item["fulfilled_quantity"] == 1.0
    assert item["status"] == sop.ORDER_ITEM_STATUS_PARTIAL
    order = master.execute(
        "SELECT status FROM store_orders WHERE id=?", (order_id,)
    ).fetchone()
    assert order["status"] == sop.ORDER_STATUS_SHIPPED  # still shipped

    # No new delivered notification (partial only).
    after = master.execute(
        "SELECT COUNT(*) AS c FROM notifications WHERE event_type=? AND target_url=?",
        (sop.EVENT_ORDER_DELIVERED, f"/store-ordering/orders/{order_id}"),
    ).fetchone()["c"]
    assert after == before
    master.close()

    # Store stock +1.
    store = sqlite3.connect(str(receive_env["store_path"]))
    store.row_factory = sqlite3.Row
    qty = store.execute(
        "SELECT quantity FROM items WHERE canonical_id=101"
    ).fetchone()["quantity"]
    assert qty == 1.0
    mv = store.execute(
        "SELECT action, delta FROM stock_movements WHERE action=?",
        (sop.RECEIPT_ACTION,),
    ).fetchone()
    assert mv["delta"] == 1.0
    store.close()


def test_multiple_partial_receipts_then_delivered(receive_env):
    """3 + 7 → order delivered + delivered notification."""
    order_id = _build_shipped_order(receive_env, qty=10.0)
    client = receive_env["client"]
    master = sqlite3.connect(str(receive_env["master_path"]))
    master.row_factory = sqlite3.Row
    order_item_id = master.execute(
        "SELECT id FROM store_order_items WHERE order_id=?", (order_id,)
    ).fetchone()["id"]
    master.close()

    _login_as(client, 2, 2)
    client.post(
        f"/store-ordering/orders/{order_id}/receive",
        data={"order_item_id": order_item_id, "quantity": "3", "note": ""},
    )
    client.post(
        f"/store-ordering/orders/{order_id}/receive",
        data={"order_item_id": order_item_id, "quantity": "7", "note": "remaining"},
    )

    master = sqlite3.connect(str(receive_env["master_path"]))
    master.row_factory = sqlite3.Row
    order = master.execute(
        "SELECT status, delivered_at FROM store_orders WHERE id=?", (order_id,)
    ).fetchone()
    assert order["status"] == sop.ORDER_STATUS_DELIVERED
    assert order["delivered_at"] is not None

    delivered = master.execute(
        "SELECT COUNT(*) AS c FROM notifications WHERE event_type=? AND target_url=?",
        (sop.EVENT_ORDER_DELIVERED, f"/store-ordering/orders/{order_id}"),
    ).fetchone()["c"]
    assert delivered >= 1
    master.close()

    # Final store qty = 10
    store = sqlite3.connect(str(receive_env["store_path"]))
    store.row_factory = sqlite3.Row
    qty = store.execute(
        "SELECT quantity FROM items WHERE canonical_id=101"
    ).fetchone()["quantity"]
    assert qty == 10.0
    store.close()


def test_receive_other_store_order_forbidden(receive_env):
    """A second storefront user cannot POST /receive against an order that
    belongs to a different store. (We simulate this by creating a second
    storefront warehouse and submitting an order from it.)"""
    # Add second store + user.
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    store2_path = receive_env["master_path"].parent / "store2_test.db"
    init_warehouse_db(store2_path)
    m = sqlite3.connect(str(receive_env["master_path"]))
    m.row_factory = sqlite3.Row
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) VALUES (3, 'store2_test', '门店2', ?, 'storefront', ?)",
        (str(store2_path), ts),
    )
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (7, 'store2_mgr', 'x', 0, ?)",
        (ts,),
    )
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (7, 3, 'manager')")
    m.commit()
    m.close()

    order_id = _build_shipped_order(receive_env, qty=5.0)
    client = receive_env["client"]
    master = sqlite3.connect(str(receive_env["master_path"]))
    master.row_factory = sqlite3.Row
    order_item_id = master.execute(
        "SELECT id FROM store_order_items WHERE order_id=?", (order_id,)
    ).fetchone()["id"]
    master.close()

    # Login as store2_mgr (user 7) bound to store2_test (warehouse_id=3).
    _login_as(client, 7, 3)
    resp = client.post(
        f"/store-ordering/orders/{order_id}/receive",
        data={"order_item_id": order_item_id, "quantity": "1", "note": ""},
    )
    assert resp.status_code == 403


def test_receive_over_remaining_rejected(receive_env):
    """POST qty > remaining → flash error, no state change."""
    order_id = _build_shipped_order(receive_env, qty=5.0)
    client = receive_env["client"]
    master = sqlite3.connect(str(receive_env["master_path"]))
    master.row_factory = sqlite3.Row
    order_item_id = master.execute(
        "SELECT id FROM store_order_items WHERE order_id=?", (order_id,)
    ).fetchone()["id"]
    master.close()

    _login_as(client, 2, 2)
    resp = client.post(
        f"/store-ordering/orders/{order_id}/receive",
        data={"order_item_id": order_item_id, "quantity": "99", "note": ""},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "超过待收" in body

    master = sqlite3.connect(str(receive_env["master_path"]))
    master.row_factory = sqlite3.Row
    item = master.execute(
        "SELECT fulfilled_quantity FROM store_order_items WHERE id=?",
        (order_item_id,),
    ).fetchone()
    assert item["fulfilled_quantity"] == 0.0
    master.close()


def test_receive_status_must_be_shipped(receive_env):
    """Cannot receive an order that isn't yet shipped."""
    client = receive_env["client"]
    _login_as(client, 1, 2)
    client.post(
        "/store-ordering/cart/add",
        data={"dc": "dc_test", "canonical_id": 101, "quantity": "5", "unit": "件"},
    )
    client.post(
        "/store-ordering/cart/submit",
        data={"expected_delivery_date": datetime.now().strftime("%Y-%m-%d"), "note": ""},
    )
    master = sqlite3.connect(str(receive_env["master_path"]))
    master.row_factory = sqlite3.Row
    order_id = master.execute(
        "SELECT id FROM store_orders ORDER BY id DESC LIMIT 1"
    ).fetchone()["id"]
    order_item_id = master.execute(
        "SELECT id FROM store_order_items WHERE order_id=?", (order_id,)
    ).fetchone()["id"]
    master.close()

    _login_as(client, 2, 2)
    resp = client.post(
        f"/store-ordering/orders/{order_id}/receive",
        data={"order_item_id": order_item_id, "quantity": "1", "note": ""},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "shipped" in body or "出库" in body


def test_receive_unbound_auto_creates_item(receive_env):
    """Receive for an order_item whose canonical has no store binding yet auto-
    creates the items row in the storefront db."""
    # Submit an order for canonical 102 (which has no store binding).
    client = receive_env["client"]
    _login_as(client, 1, 2)
    client.post(
        "/store-ordering/cart/add",
        data={"dc": "dc_test", "canonical_id": 102, "quantity": "3", "unit": "件"},
    )
    # submit_order validates cart and would block unbound → bypass via cart row
    # is removed; instead we directly insert an order row + item.
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    m = sqlite3.connect(str(receive_env["master_path"]))
    m.row_factory = sqlite3.Row
    cur = m.execute(
        """INSERT INTO store_orders
           (order_no, store_warehouse_code, dc_warehouse_code, status,
            requested_by, expected_delivery_date, note, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, NULL, '', ?, ?)""",
        ("SO-AUTO2-0001", "store_test", "dc_test",
         sop.ORDER_STATUS_SHIPPED, 1, ts, ts),
    )
    order_id = int(cur.lastrowid)
    # Need dc_item_id (canon 102 is seeded in dc db).
    dc = sqlite3.connect(str(receive_env["dc_path"]))
    dc.row_factory = sqlite3.Row
    dc_item_id = int(dc.execute(
        "SELECT id FROM items WHERE canonical_id=102"
    ).fetchone()["id"])
    dc.close()
    cur = m.execute(
        """INSERT INTO store_order_items
           (order_id, canonical_id, dc_item_id, quantity, unit,
            fulfilled_quantity, status, created_at)
           VALUES (?, ?, ?, 3.0, '件', 0, 'pending', ?)""",
        (order_id, 102, dc_item_id, ts),
    )
    order_item_id = int(cur.lastrowid)
    m.commit()
    m.close()

    _login_as(client, 2, 2)
    resp = client.post(
        f"/store-ordering/orders/{order_id}/receive",
        data={"order_item_id": order_item_id, "quantity": "3", "note": ""},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    store = sqlite3.connect(str(receive_env["store_path"]))
    store.row_factory = sqlite3.Row
    row = store.execute(
        "SELECT quantity FROM items WHERE canonical_id=102"
    ).fetchone()
    assert row is not None
    assert row["quantity"] == 3.0
    store.close()
