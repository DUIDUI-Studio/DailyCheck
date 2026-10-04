"""Unit tests for store_ordering_pure.py.

Tests do not require Flask; they exercise pure SQLite logic.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from blueprints import store_ordering_pure as sop
from blueprints.notifications_pure import ALLOWED_EVENT_TYPES
from db import init_master_db, init_warehouse_db


@pytest.fixture
def ordering_env(tmp_path, monkeypatch):
    """Provide a fresh master + DC + storefront environment."""
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
    # users
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (1, 'admin', 'x', 1, ?)",
        (ts,),
    )
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (2, 'store_staff', 'x', 0, ?)",
        (ts,),
    )
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (3, 'store_mgr', 'x', 0, ?)",
        (ts,),
    )
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (4, 'dc_staff', 'x', 0, ?)",
        (ts,),
    )
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (5, 'dc_mgr', 'x', 0, ?)",
        (ts,),
    )
    # warehouses
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) VALUES (1, 'dc_test', '测试配送中心', ?, 'distribution_center', ?)",
        (str(dc_path), ts),
    )
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) VALUES (2, 'store_test', '测试门店', ?, 'storefront', ?)",
        (str(store_path), ts),
    )
    # roles
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (2, 2, 'staff')")
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (3, 2, 'manager')")
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (4, 1, 'staff')")
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (5, 1, 'manager')")

    # canonical categories
    m.execute(
        "INSERT INTO canonical_categories (code, name, description, created_at, updated_at) VALUES ('PACKAGING', '包材', '', ?, ?)",
        (ts, ts),
    )
    # canonical items
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
    m.execute(
        """INSERT INTO canonical_items
           (id, canonical_sku, name, category_code, unit, gram_per_unit, aux_unit, aux_rate,
            status, created_from, created_at, updated_at)
           VALUES (103, 'IC-000103', '测试包材C', 'PACKAGING', '件', 0, NULL, 0, 'inactive', 'rd_manual', ?, ?)""",
        (ts, ts),
    )
    m.commit()
    m.close()

    # seed DC items
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
    dc.execute(
        "INSERT INTO items (sku, name, category_id, quantity, unit, canonical_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("DC-C", "测试包材C-停用", cat_id, 50.0, "件", 103, ts),
    )
    dc.commit()
    dc.close()

    # seed store items (A bound, B unbound)
    store = sqlite3.connect(str(store_path))
    store.row_factory = sqlite3.Row
    cat_id = store.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()["id"]
    store.execute(
        "INSERT INTO items (sku, name, category_id, quantity, unit, canonical_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("ST-A", "测试包材A", cat_id, 0.0, "件", 101, ts),
    )
    store.execute(
        "INSERT INTO items (sku, name, category_id, quantity, unit, canonical_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("ST-B", "测试包材B", cat_id, 0.0, "件", None, ts),
    )
    store.commit()
    store.close()

    m = sqlite3.connect(str(master_path))
    m.row_factory = sqlite3.Row
    yield {
        "master_conn": m,
        "master_path": master_path,
        "dc_path": dc_path,
        "store_path": store_path,
    }
    m.close()


def test_generate_order_no(ordering_env):
    conn = ordering_env["master_conn"]
    today = datetime.now().strftime("%Y%m%d")
    no1 = sop.generate_order_no(conn)
    conn.execute(
        "INSERT INTO store_orders (order_no, store_warehouse_code, dc_warehouse_code, status, requested_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (no1, "store_test", "dc_test", sop.ORDER_STATUS_PENDING, 2, sop.now(), sop.now()),
    )
    conn.commit()
    no2 = sop.generate_order_no(conn)
    assert no1.startswith(f"SO-{today}-")
    assert no2.startswith(f"SO-{today}-")
    assert no2 > no1


def test_open_warehouse_db(ordering_env):
    conn = sop.open_warehouse_db("dc_test")
    row = conn.execute("SELECT 1 AS one").fetchone()
    assert row["one"] == 1
    conn.close()


def test_cart_crud(ordering_env):
    conn = ordering_env["master_conn"]
    cart = sop.get_or_create_cart(conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test")
    assert cart["user_id"] == 2
    assert cart["store_warehouse_code"] == "store_test"

    sop.add_cart_item(conn, cart["id"], 101, 10.0, "件")
    items = sop.list_cart_items(conn, cart["id"])
    assert len(items) == 1
    assert items[0]["canonical_id"] == 101
    assert items[0]["quantity"] == 10.0

    sop.update_cart_item_quantity(conn, items[0]["cart_item_id"], 25.0)
    items = sop.list_cart_items(conn, cart["id"])
    assert items[0]["quantity"] == 25.0

    sop.remove_cart_item(conn, items[0]["cart_item_id"])
    assert sop.list_cart_items(conn, cart["id"]) == []

    sop.add_cart_item(conn, cart["id"], 101, 5.0, "件")
    sop.add_cart_item(conn, cart["id"], 102, 3.0, "件")
    assert len(sop.list_cart_items(conn, cart["id"])) == 2
    sop.clear_cart(conn, cart["id"])
    assert sop.list_cart_items(conn, cart["id"]) == []


def test_cart_switch_dc_clears_items(ordering_env):
    conn = ordering_env["master_conn"]
    cart = sop.get_or_create_cart(conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test")
    sop.add_cart_item(conn, cart["id"], 101, 5.0, "件")
    assert len(sop.list_cart_items(conn, cart["id"])) == 1

    # Need a second DC db to switch to.
    dc2_path = ordering_env["master_path"].parent / "warehouses" / "dc_test2.db"
    init_warehouse_db(dc2_path)
    m = conn
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) VALUES (3, 'dc_test2', '测试配送中心2', ?, 'distribution_center', ?)",
        (str(dc2_path), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    m.commit()

    cart2 = sop.get_or_create_cart(conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test2")
    assert cart2["id"] == cart["id"]
    assert cart2["dc_warehouse_code"] == "dc_test2"
    assert sop.list_cart_items(conn, cart["id"]) == []


def test_list_available_dc_items(ordering_env):
    conn = ordering_env["master_conn"]
    items = sop.list_available_dc_items(conn, "dc_test")
    ids = {i["canonical_id"] for i in items}
    assert 101 in ids
    assert 102 in ids
    assert 103 not in ids  # inactive canonical


def test_validate_cart_shortage(ordering_env):
    conn = ordering_env["master_conn"]
    cart = sop.get_or_create_cart(conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test")
    sop.add_cart_item(conn, cart["id"], 102, 10.0, "件")  # DC only has 5
    result = sop.validate_cart_for_submit(conn, cart)
    assert result["ok"] is False
    assert len(result["shortages"]) == 1
    assert result["shortages"][0]["available"] == 5.0
    assert result["shortages"][0]["requested"] == 10.0


def test_validate_cart_unbound(ordering_env):
    conn = ordering_env["master_conn"]
    cart = sop.get_or_create_cart(conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test")
    # Add canonical 102 which store has not bound
    sop.add_cart_item(conn, cart["id"], 102, 1.0, "件")
    result = sop.validate_cart_for_submit(conn, cart)
    assert result["ok"] is False
    assert len(result["unbound"]) == 1
    assert result["unbound"][0]["canonical_id"] == 102


def test_validate_cart_ok(ordering_env):
    conn = ordering_env["master_conn"]
    cart = sop.get_or_create_cart(conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test")
    sop.add_cart_item(conn, cart["id"], 101, 10.0, "件")
    result = sop.validate_cart_for_submit(conn, cart)
    assert result["ok"] is True


def test_submit_order(ordering_env):
    conn = ordering_env["master_conn"]
    cart = sop.get_or_create_cart(conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test")
    sop.add_cart_item(conn, cart["id"], 101, 10.0, "件")
    order = sop.submit_order(conn, cart["id"], requested_by=2, expected_delivery_date=None, note=" urgent")
    assert order["status"] == sop.ORDER_STATUS_PENDING
    assert order["store_warehouse_code"] == "store_test"
    assert order["dc_warehouse_code"] == "dc_test"
    assert len(order["order_items"]) == 1
    assert order["order_items"][0]["quantity"] == 10.0
    # cart cleared
    assert sop.list_cart_items(conn, cart["id"]) == []


def test_review_approve_and_reject(ordering_env):
    conn = ordering_env["master_conn"]
    cart = sop.get_or_create_cart(conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test")
    sop.add_cart_item(conn, cart["id"], 101, 5.0, "件")
    order = sop.submit_order(conn, cart["id"], requested_by=2, expected_delivery_date=None, note="")
    order_id = order["id"]

    order = sop.review_order(conn, order_id, sop.ORDER_STATUS_APPROVED, actor_id=5, note="ok")
    assert order["status"] == sop.ORDER_STATUS_APPROVED
    assert order["approved_by"] == 5
    assert any(h["to_status"] == sop.ORDER_STATUS_APPROVED for h in order["status_history"])

    # Reject is only allowed from pending; from approved it is invalid.
    with pytest.raises(ValueError):
        sop.review_order(conn, order_id, sop.ORDER_STATUS_REJECTED, actor_id=5, note="no")

    # Fresh order to test reject from pending.
    cart = sop.get_or_create_cart(conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test")
    sop.add_cart_item(conn, cart["id"], 101, 1.0, "件")
    order = sop.submit_order(conn, cart["id"], requested_by=2, expected_delivery_date=None, note="")
    order = sop.review_order(conn, order["id"], sop.ORDER_STATUS_REJECTED, actor_id=5, note="no")
    assert order["status"] == sop.ORDER_STATUS_REJECTED


def test_review_invalid_transition(ordering_env):
    conn = ordering_env["master_conn"]
    cart = sop.get_or_create_cart(conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test")
    sop.add_cart_item(conn, cart["id"], 101, 5.0, "件")
    order = sop.submit_order(conn, cart["id"], requested_by=2, expected_delivery_date=None, note="")
    with pytest.raises(ValueError):
        sop.review_order(conn, order["id"], sop.ORDER_STATUS_SHIPPED, actor_id=5)


def test_ship_order_success(ordering_env):
    conn = ordering_env["master_conn"]
    cart = sop.get_or_create_cart(conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test")
    sop.add_cart_item(conn, cart["id"], 101, 10.0, "件")
    order = sop.submit_order(conn, cart["id"], requested_by=2, expected_delivery_date=None, note="")
    order = sop.review_order(conn, order["id"], sop.ORDER_STATUS_APPROVED, actor_id=5)
    order = sop.ship_order(conn, order["id"], shipped_by=4)
    assert order["status"] == sop.ORDER_STATUS_SHIPPED

    # DC stock deducted
    dc = sqlite3.connect(str(ordering_env["dc_path"]))
    dc.row_factory = sqlite3.Row
    row = dc.execute("SELECT quantity FROM items WHERE canonical_id=101").fetchone()
    assert row["quantity"] == 90.0
    # stock movement recorded
    mv = dc.execute("SELECT * FROM stock_movements WHERE action=?", (sop.SHIPMENT_ACTION,)).fetchone()
    assert mv["delta"] == -10.0
    assert str(order["order_no"]) in mv["note"]
    dc.close()

    # P0: store inventory unchanged
    store = sqlite3.connect(str(ordering_env["store_path"]))
    store.row_factory = sqlite3.Row
    row = store.execute("SELECT quantity FROM items WHERE canonical_id=101").fetchone()
    assert row["quantity"] == 0.0
    store.close()


def test_ship_order_insufficient_stock_keeps_approved(ordering_env):
    conn = ordering_env["master_conn"]
    cart = sop.get_or_create_cart(conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test")
    sop.add_cart_item(conn, cart["id"], 101, 10.0, "件")  # DC has 100 initially
    order = sop.submit_order(conn, cart["id"], requested_by=2, expected_delivery_date=None, note="")
    order = sop.review_order(conn, order["id"], sop.ORDER_STATUS_APPROVED, actor_id=5)

    # Deplete DC stock after approval so shipping fails.
    dc = sqlite3.connect(str(ordering_env["dc_path"]))
    dc.execute("UPDATE items SET quantity=? WHERE canonical_id=?", (5.0, 101))
    dc.commit()
    dc.close()

    with pytest.raises(ValueError, match="库存不足"):
        sop.ship_order(conn, order["id"], shipped_by=4)

    order = sop.get_order_detail(conn, order["id"])
    assert order["status"] == sop.ORDER_STATUS_APPROVED
    # No delivery created
    assert order["deliveries"] == []


def test_mark_order_delivered(ordering_env):
    conn = ordering_env["master_conn"]
    cart = sop.get_or_create_cart(conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test")
    sop.add_cart_item(conn, cart["id"], 101, 5.0, "件")
    order = sop.submit_order(conn, cart["id"], requested_by=2, expected_delivery_date=None, note="")
    order = sop.review_order(conn, order["id"], sop.ORDER_STATUS_APPROVED, actor_id=5)
    order = sop.ship_order(conn, order["id"], shipped_by=4)
    order = sop.mark_order_delivered(conn, order["id"], actor_id=3)
    assert order["status"] == sop.ORDER_STATUS_DELIVERED
    assert order["delivered_at"] is not None


def test_notify_order_event_targets(ordering_env):
    conn = ordering_env["master_conn"]
    cart = sop.get_or_create_cart(conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test")
    sop.add_cart_item(conn, cart["id"], 101, 5.0, "件")
    order = sop.submit_order(conn, cart["id"], requested_by=2, expected_delivery_date=None, note="")

    # submitted -> DC manager (5) + admin (1)
    count = sop.notify_order_event(conn, sop.EVENT_ORDER_SUBMITTED, order, actor_user_id=2)
    assert count == 2
    rows = conn.execute("SELECT user_id FROM notifications WHERE event_type=?", (sop.EVENT_ORDER_SUBMITTED,)).fetchall()
    notified = {r["user_id"] for r in rows}
    assert notified == {1, 5}

    # approved -> requester (2) + store manager (3)
    order = sop.review_order(conn, order["id"], sop.ORDER_STATUS_APPROVED, actor_id=5)
    count = sop.notify_order_event(conn, sop.EVENT_ORDER_APPROVED, order, actor_user_id=5)
    assert count == 2
    rows = conn.execute("SELECT user_id FROM notifications WHERE event_type=?", (sop.EVENT_ORDER_APPROVED,)).fetchall()
    notified = {r["user_id"] for r in rows}
    assert notified == {2, 3}


def test_allowed_event_types_include_store_ordering():
    for et in (
        sop.EVENT_ORDER_SUBMITTED,
        sop.EVENT_ORDER_APPROVED,
        sop.EVENT_ORDER_REJECTED,
        sop.EVENT_ORDER_SHIPPED,
        sop.EVENT_ORDER_DELIVERED,
    ):
        assert et in ALLOWED_EVENT_TYPES
