"""Integration tests for the full store-ordering lifecycle."""
from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from blueprints import store_ordering_pure as sop
from db import init_master_db, init_warehouse_db


@pytest.fixture
def integration_env(tmp_path, monkeypatch):
    """Full environment with store staff, DC manager and DC staff."""
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
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (3, 'dc_staff', 'x', 0, ?)",
        (ts,),
    )
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (4, 'store_mgr', 'x', 0, ?)",
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
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (3, 1, 'staff')")
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (4, 2, 'manager')")
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
    m.commit()
    m.close()

    dc = sqlite3.connect(str(dc_path))
    dc.row_factory = sqlite3.Row
    cat_id = dc.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()["id"]
    dc.execute(
        "INSERT INTO items (sku, name, category_id, quantity, unit, canonical_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("DC-A", "测试包材A", cat_id, 100.0, "件", 101, ts),
    )
    dc.commit()
    dc.close()

    store = sqlite3.connect(str(store_path))
    store.row_factory = sqlite3.Row
    cat_id = store.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()["id"]
    store.execute(
        "INSERT INTO items (sku, name, category_id, quantity, unit, canonical_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("ST-A", "测试包材A", cat_id, 10.0, "件", 101, ts),
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


def test_full_lifecycle_and_notifications(integration_env):
    client = integration_env["client"]
    master_path = integration_env["master_path"]

    # 1. Store staff submits order.
    _login_as(client, 1, 2)
    client.post(
        "/store-ordering/cart/add",
        data={"dc": "dc_test", "canonical_id": 101, "quantity": "8", "unit": "件"},
    )
    resp = client.post(
        "/store-ordering/cart/submit",
        data={"expected_delivery_date": datetime.now().strftime("%Y-%m-%d"), "note": "加急"},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    master = sqlite3.connect(str(master_path))
    master.row_factory = sqlite3.Row
    order = master.execute("SELECT * FROM store_orders ORDER BY id DESC LIMIT 1").fetchone()
    assert order["status"] == "pending"
    submitted_count = master.execute(
        "SELECT COUNT(*) AS c FROM notifications WHERE event_type=?", (sop.EVENT_ORDER_SUBMITTED,)
    ).fetchone()["c"]
    # No platform admin in fixture; only DC manager (user 2) is target.
    assert submitted_count == 1
    order_id = order["id"]

    # 2. DC manager approves.
    _login_as(client, 2, 1)
    client.post(
        f"/store-ordering/orders/{order_id}/review",
        data={"decision": "approved", "note": "有货"},
    )
    order = master.execute("SELECT * FROM store_orders WHERE id=?", (order_id,)).fetchone()
    assert order["status"] == "approved"
    approved_count = master.execute(
        "SELECT COUNT(*) AS c FROM notifications WHERE event_type=?", (sop.EVENT_ORDER_APPROVED,)
    ).fetchone()["c"]
    # Requester + store manager.
    assert approved_count == 2

    # 3. DC staff ships.
    _login_as(client, 3, 1)
    client.post(
        f"/store-ordering/orders/{order_id}/ship",
        data={"tracking_note": "顺丰"},
    )
    order = master.execute("SELECT * FROM store_orders WHERE id=?", (order_id,)).fetchone()
    assert order["status"] == "shipped"
    shipped_count = master.execute(
        "SELECT COUNT(*) AS c FROM notifications WHERE event_type=?", (sop.EVENT_ORDER_SHIPPED,)
    ).fetchone()["c"]
    assert shipped_count == 2

    # DC stock deducted.
    dc = sqlite3.connect(str(integration_env["dc_path"]))
    dc.row_factory = sqlite3.Row
    dc_qty = dc.execute("SELECT quantity FROM items WHERE canonical_id=101").fetchone()["quantity"]
    assert dc_qty == 92.0
    movement = dc.execute("SELECT * FROM stock_movements WHERE action=?", (sop.SHIPMENT_ACTION,)).fetchone()
    assert movement["delta"] == -8.0
    assert order["order_no"] in movement["note"]
    dc.close()

    # 4. Store manager delivers.
    _login_as(client, 4, 2)
    client.post(
        f"/store-ordering/orders/{order_id}/deliver",
    )
    order = master.execute("SELECT * FROM store_orders WHERE id=?", (order_id,)).fetchone()
    assert order["status"] == "delivered"
    delivered_count = master.execute(
        "SELECT COUNT(*) AS c FROM notifications WHERE event_type=?", (sop.EVENT_ORDER_DELIVERED,)
    ).fetchone()["c"]
    # Deliver actor (store manager) is excluded from own notification.
    assert delivered_count == 1

    # v2: store inventory increased by the received qty (10 base + 8 received).
    store = sqlite3.connect(str(integration_env["store_path"]))
    store.row_factory = sqlite3.Row
    store_qty = store.execute("SELECT quantity FROM items WHERE canonical_id=101").fetchone()["quantity"]
    assert store_qty == 18.0
    # And a 门店订货入库 stock_movement was written.
    inbound = store.execute(
        "SELECT * FROM stock_movements WHERE action=?", (sop.RECEIPT_ACTION,)
    ).fetchone()
    assert inbound["delta"] == 8.0
    assert order["order_no"] in inbound["note"]
    store.close()

    master.close()


def test_status_history_records_each_transition(integration_env):
    client = integration_env["client"]
    master_path = integration_env["master_path"]

    _login_as(client, 1, 2)
    client.post(
        "/store-ordering/cart/add",
        data={"dc": "dc_test", "canonical_id": 101, "quantity": "2", "unit": "件"},
    )
    client.post(
        "/store-ordering/cart/submit",
        data={"expected_delivery_date": datetime.now().strftime("%Y-%m-%d"), "note": ""},
    )

    master = sqlite3.connect(str(master_path))
    master.row_factory = sqlite3.Row
    order_id = master.execute("SELECT id FROM store_orders ORDER BY id DESC LIMIT 1").fetchone()["id"]
    master.close()

    _login_as(client, 2, 1)
    client.post(f"/store-ordering/orders/{order_id}/review", data={"decision": "approved", "note": ""})
    _login_as(client, 3, 1)
    client.post(f"/store-ordering/orders/{order_id}/ship", data={})
    _login_as(client, 4, 2)
    client.post(f"/store-ordering/orders/{order_id}/deliver", data={})

    master = sqlite3.connect(str(master_path))
    master.row_factory = sqlite3.Row
    history = master.execute(
        "SELECT to_status FROM store_order_status_history WHERE order_id=? ORDER BY id",
        (order_id,),
    ).fetchall()
    statuses = [h["to_status"] for h in history]
    assert statuses == ["pending", "approved", "shipped", "delivered"]
    master.close()
