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


def test_validate_cart_for_submit_ignores_shortage(ordering_env):
    """v3 A7: 库存不足不再阻止下单——门店不感知 DC 库存。
    shortages 字段已从返回结构中移除；unbound 仍要拦截未绑定品项。"""
    conn = ordering_env["master_conn"]
    cart = sop.get_or_create_cart(conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test")
    # canonical 101 在门店已绑定（fixture），但这里人为把 DC 库存扣到 0 让 v2 会 shortages。
    dc_conn = sqlite3.connect(str(ordering_env["dc_path"]))
    dc_conn.execute("UPDATE items SET quantity=? WHERE canonical_id=?", (0.0, 101))
    dc_conn.commit()
    dc_conn.close()
    sop.add_cart_item(conn, cart["id"], 101, 10.0, "件")
    result = sop.validate_cart_for_submit(conn, cart)
    assert result["ok"] is True, f"non-shortage should not block, got {result}"
    assert result["unbound"] == []
    # 确认 shortages 字段已从返回中移除（A7）。
    assert "shortages" not in result

    # 反向验证：unbound 仍要拦截。
    cart = sop.get_or_create_cart(conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test")
    sop.clear_cart(conn, cart["id"])
    sop.add_cart_item(conn, cart["id"], 102, 1.0, "件")  # 102 在 store 未绑定
    result2 = sop.validate_cart_for_submit(conn, cart)
    assert result2["ok"] is False
    assert len(result2["unbound"]) == 1


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


def test_ship_order_full_changes_status_to_shipped(ordering_env):
    """v3 T10: 全量一次 ship（legacy ship_order_full）→ status=shipped + 通知元数据齐全。

    ship_order_full 内部遍历所有明细调用新 ship_order 接口；订单 status
    翻转 + is_fully_shipped=True + delivery_id 非空 + 通知元数据齐全。
    """
    conn = ordering_env["master_conn"]
    cart = sop.get_or_create_cart(conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test")
    sop.add_cart_item(conn, cart["id"], 101, 10.0, "件")
    order = sop.submit_order(conn, cart["id"], requested_by=2, expected_delivery_date=None, note="")
    order = sop.review_order(conn, order["id"], sop.ORDER_STATUS_APPROVED, actor_id=5)
    result = sop.ship_order_full(conn, order["id"], shipped_by=4, tracking_note="batch-1")
    assert result["status"] == sop.ORDER_STATUS_SHIPPED
    assert result["is_fully_shipped"] is True
    assert result["new_order_status"] == sop.ORDER_STATUS_SHIPPED
    assert result["delivery_id"] > 0
    # 累计到 quantity 一致，明细状态全部 fulfilled。
    assert len(result["shipped_items"]) == 1
    assert result["shipped_items"][0]["shipped_quantity"] == 10.0
    for item in result["order_items"]:
        assert item["shipped_quantity"] == 10.0
        assert item["status"] == sop.ORDER_ITEM_STATUS_FULFILLED

    # DC stock deducted
    dc = sqlite3.connect(str(ordering_env["dc_path"]))
    dc.row_factory = sqlite3.Row
    row = dc.execute("SELECT quantity FROM items WHERE canonical_id=101").fetchone()
    assert row["quantity"] == 90.0
    # stock movement recorded
    mv = dc.execute("SELECT * FROM stock_movements WHERE action=?", (sop.SHIPMENT_ACTION,)).fetchone()
    assert mv["delta"] == -10.0
    assert str(result["order_no"]) in mv["note"]
    dc.close()

    # P0: store inventory unchanged at ship time
    store = sqlite3.connect(str(ordering_env["store_path"]))
    store.row_factory = sqlite3.Row
    row = store.execute("SELECT quantity FROM items WHERE canonical_id=101").fetchone()
    assert row["quantity"] == 0.0
    store.close()


def test_ship_order_negative_stock_allowed(ordering_env):
    """v3 F1=A: DC 库存不足时仍允许发货，库存跌为负值。

    即使把 DC 库存扣到 0 以下，ship_order 不抛异常；订单完成发货后
    状态正常推进到 shipped；DC items.quantity 跌为负值。"""
    conn = ordering_env["master_conn"]
    cart = sop.get_or_create_cart(conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test")
    sop.add_cart_item(conn, cart["id"], 101, 10.0, "件")  # DC has 100 initially
    order = sop.submit_order(conn, cart["id"], requested_by=2, expected_delivery_date=None, note="")
    order = sop.review_order(conn, order["id"], sop.ORDER_STATUS_APPROVED, actor_id=5)

    # Deplete DC stock so DC=5 但要出 10 件。
    dc = sqlite3.connect(str(ordering_env["dc_path"]))
    dc.execute("UPDATE items SET quantity=? WHERE canonical_id=?", (5.0, 101))
    dc.commit()
    dc.close()

    # v3: 不抛异常。
    result = sop.ship_order_full(conn, order["id"], shipped_by=4)
    assert result["status"] == sop.ORDER_STATUS_SHIPPED
    assert result["is_fully_shipped"] is True

    # DC 库存跌为 -5（5 - 10 = -5）。
    dc = sqlite3.connect(str(ordering_env["dc_path"]))
    dc.row_factory = sqlite3.Row
    row = dc.execute("SELECT quantity FROM items WHERE canonical_id=101").fetchone()
    assert row["quantity"] == -5.0
    # stock_movements 仍然记录 delta=-10。
    mv = dc.execute(
        "SELECT delta FROM stock_movements WHERE action=?",
        (sop.SHIPMENT_ACTION,),
    ).fetchone()
    assert mv["delta"] == -10.0
    dc.close()


def test_mark_order_delivered(ordering_env):
    conn = ordering_env["master_conn"]
    cart = sop.get_or_create_cart(conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test")
    sop.add_cart_item(conn, cart["id"], 101, 5.0, "件")
    order = sop.submit_order(conn, cart["id"], requested_by=2, expected_delivery_date=None, note="")
    order = sop.review_order(conn, order["id"], sop.ORDER_STATUS_APPROVED, actor_id=5)
    order = sop.ship_order_full(conn, order["id"], shipped_by=4)
    order = sop.mark_order_delivered(conn, order["id"], actor_id=3)
    assert order["status"] == sop.ORDER_STATUS_DELIVERED
    assert order["delivered_at"] is not None


def test_receive_order_item_basic(ordering_env):
    """Single full receipt: order_item fulfilled, order delivered."""
    conn = ordering_env["master_conn"]
    cart = sop.get_or_create_cart(conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test")
    sop.add_cart_item(conn, cart["id"], 101, 5.0, "件")
    order = sop.submit_order(conn, cart["id"], requested_by=2, expected_delivery_date=None, note="")
    order = sop.review_order(conn, order["id"], sop.ORDER_STATUS_APPROVED, actor_id=5)
    order = sop.ship_order_full(conn, order["id"], shipped_by=4)
    order_item_id = order["order_items"][0]["id"]

    result = sop.receive_order_item(
        conn, order_id=order["id"], order_item_id=order_item_id,
        qty=5.0, actor_id=3, note="ok",
    )
    assert result["new_order_status"] == sop.ORDER_STATUS_DELIVERED
    assert result["is_fully_received"] is True
    assert result["fulfilled_quantity"] == 5.0

    # Store stock increased by 5.
    store = sqlite3.connect(str(ordering_env["store_path"]))
    store.row_factory = sqlite3.Row
    qty = store.execute(
        "SELECT quantity FROM items WHERE canonical_id=101"
    ).fetchone()["quantity"]
    assert qty == 5.0
    # stock_movements written.
    mv = store.execute(
        "SELECT action, delta FROM stock_movements WHERE action=?",
        (sop.RECEIPT_ACTION,),
    ).fetchone()
    assert mv["delta"] == 5.0
    store.close()


def test_receive_order_item_partial_then_full(ordering_env):
    """Two partial receipts → 全部收齐 → delivered."""
    conn = ordering_env["master_conn"]
    cart = sop.get_or_create_cart(conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test")
    sop.add_cart_item(conn, cart["id"], 101, 10.0, "件")
    order = sop.submit_order(conn, cart["id"], requested_by=2, expected_delivery_date=None, note="")
    order = sop.review_order(conn, order["id"], sop.ORDER_STATUS_APPROVED, actor_id=5)
    order = sop.ship_order_full(conn, order["id"], shipped_by=4)
    order_item_id = order["order_items"][0]["id"]

    r1 = sop.receive_order_item(
        conn, order_id=order["id"], order_item_id=order_item_id,
        qty=3.0, actor_id=3,
    )
    assert r1["is_fully_received"] is False
    assert r1["new_order_status"] == sop.ORDER_STATUS_SHIPPED  # still shipped
    # Item is partial, fulfilled=3
    item = conn.execute(
        "SELECT fulfilled_quantity, status FROM store_order_items WHERE id=?",
        (order_item_id,),
    ).fetchone()
    assert item["fulfilled_quantity"] == 3.0
    assert item["status"] == sop.ORDER_ITEM_STATUS_PARTIAL

    r2 = sop.receive_order_item(
        conn, order_id=order["id"], order_item_id=order_item_id,
        qty=7.0, actor_id=3,
    )
    assert r2["is_fully_received"] is True
    assert r2["new_order_status"] == sop.ORDER_STATUS_DELIVERED

    # Receipts: 2 rows.
    receipts = sop.list_order_receipts(conn, order["id"])
    assert len(receipts) == 2
    # Newest first.
    assert float(receipts[0]["quantity"]) == 7.0
    assert float(receipts[1]["quantity"]) == 3.0


def test_receive_order_item_over_remaining_rejected(ordering_env):
    conn = ordering_env["master_conn"]
    cart = sop.get_or_create_cart(conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test")
    sop.add_cart_item(conn, cart["id"], 101, 5.0, "件")
    order = sop.submit_order(conn, cart["id"], requested_by=2, expected_delivery_date=None, note="")
    order = sop.review_order(conn, order["id"], sop.ORDER_STATUS_APPROVED, actor_id=5)
    order = sop.ship_order_full(conn, order["id"], shipped_by=4)
    order_item_id = order["order_items"][0]["id"]

    # First, receive 4 valid.
    sop.receive_order_item(
        conn, order_id=order["id"], order_item_id=order_item_id,
        qty=4.0, actor_id=3,
    )
    # Now try to over-collect (remaining=1).
    with pytest.raises(ValueError):
        sop.receive_order_item(
            conn, order_id=order["id"], order_item_id=order_item_id,
            qty=2.0, actor_id=3,
        )


def test_receive_order_item_status_must_be_shipped(ordering_env):
    conn = ordering_env["master_conn"]
    cart = sop.get_or_create_cart(conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test")
    sop.add_cart_item(conn, cart["id"], 101, 5.0, "件")
    order = sop.submit_order(conn, cart["id"], requested_by=2, expected_delivery_date=None, note="")
    order_item_id = order["order_items"][0]["id"]

    # status=pending — not shipped yet.
    with pytest.raises(ValueError, match="shipped"):
        sop.receive_order_item(
            conn, order_id=order["id"], order_item_id=order_item_id,
            qty=1.0, actor_id=3,
        )


def test_receive_order_item_auto_creates_store_item(ordering_env):
    """When the storefront has no local items row for the canonical_id, receive
    must auto-create one (quantity=0) and immediately add the received qty."""
    conn = ordering_env["master_conn"]
    # canonical_id 102 is NOT bound in store_test (intentional from fixture).
    # We bypass submit_order (which would block unbound) by inserting the order
    # directly. This still exercises the receive_order_item auto-create path.
    ts = sop.now()
    cur = conn.execute(
        """INSERT INTO store_orders
           (order_no, store_warehouse_code, dc_warehouse_code, status,
            requested_by, expected_delivery_date, note, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, NULL, '', ?, ?)""",
        ("SO-AUTO-0001", "store_test", "dc_test",
         sop.ORDER_STATUS_SHIPPED, 2, ts, ts),
    )
    order_id = int(cur.lastrowid)
    # ship_order logic needs dc_item_id resolved; reuse ship_order's effect by
    # inserting one shipped order_item.
    dc = sqlite3.connect(str(ordering_env["dc_path"]))
    dc.row_factory = sqlite3.Row
    dc_item_id = int(dc.execute(
        "SELECT id FROM items WHERE canonical_id=102"
    ).fetchone()["id"])
    dc.close()
    cur = conn.execute(
        """INSERT INTO store_order_items
           (order_id, canonical_id, dc_item_id, quantity, unit,
            fulfilled_quantity, status, created_at)
           VALUES (?, ?, ?, 3.0, '件', 0, 'pending', ?)""",
        (order_id, 102, dc_item_id, ts),
    )
    order_item_id = int(cur.lastrowid)

    result = sop.receive_order_item(
        conn, order_id=order_id, order_item_id=order_item_id,
        qty=3.0, actor_id=3,
    )
    assert result["is_fully_received"] is True

    # Store has a new row for canonical_id=102, quantity=3.
    store = sqlite3.connect(str(ordering_env["store_path"]))
    store.row_factory = sqlite3.Row
    row = store.execute(
        "SELECT quantity, canonical_id FROM items WHERE canonical_id=102"
    ).fetchone()
    assert row is not None
    assert row["quantity"] == 3.0
    store.close()


def test_list_order_receipts_descending(ordering_env):
    """list_order_receipts must return newest first."""
    conn = ordering_env["master_conn"]
    cart = sop.get_or_create_cart(conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test")
    sop.add_cart_item(conn, cart["id"], 101, 5.0, "件")
    order = sop.submit_order(conn, cart["id"], requested_by=2, expected_delivery_date=None, note="")
    order = sop.review_order(conn, order["id"], sop.ORDER_STATUS_APPROVED, actor_id=5)
    order = sop.ship_order_full(conn, order["id"], shipped_by=4)
    order_item_id = order["order_items"][0]["id"]

    sop.receive_order_item(
        conn, order_id=order["id"], order_item_id=order_item_id,
        qty=2.0, actor_id=3, note="first",
    )
    sop.receive_order_item(
        conn, order_id=order["id"], order_item_id=order_item_id,
        qty=3.0, actor_id=3, note="second",
    )

    receipts = sop.list_order_receipts(conn, order["id"])
    assert [float(r["quantity"]) for r in receipts] == [3.0, 2.0]
    assert [r["note"] for r in receipts] == ["second", "first"]


def test_list_available_dc_items_includes_category_name_and_unit_price(ordering_env):
    conn = ordering_env["master_conn"]
    items = sop.list_available_dc_items(conn, "dc_test")
    assert items, "fixture should expose at least one DC item"
    for it in items:
        assert "category_name" in it
        assert "unit_price" in it
        # category_name for fixture 'PACKAGING' must be the Chinese name from
        # canonical_categories.
        if it["canonical_id"] == 101:
            assert it["category_name"] == "包材"
            # unit_price is 0 because fixture did not set selling_price/unit_cost.
            assert float(it["unit_price"]) == 0.0


def test_mark_order_delivered_legacy_compat(ordering_env):
    """mark_order_delivered must still work end-to-end (calls receive internally)."""
    conn = ordering_env["master_conn"]
    cart = sop.get_or_create_cart(conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test")
    sop.add_cart_item(conn, cart["id"], 101, 4.0, "件")
    order = sop.submit_order(conn, cart["id"], requested_by=2, expected_delivery_date=None, note="")
    order = sop.review_order(conn, order["id"], sop.ORDER_STATUS_APPROVED, actor_id=5)
    order = sop.ship_order_full(conn, order["id"], shipped_by=4)
    order = sop.mark_order_delivered(conn, order["id"], actor_id=3)
    assert order["status"] == sop.ORDER_STATUS_DELIVERED
    # Legacy path wrote receipts for the whole remaining qty.
    receipts = sop.list_order_receipts(conn, order["id"])
    assert len(receipts) == 1
    assert float(receipts[0]["quantity"]) == 4.0


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


# ---------------------------------------------------------------------------
# v3 T10: 部分发货 + 库存校验移除 + DC 库存视图
# ---------------------------------------------------------------------------

def _make_approved_order(ordering_env, qty=5.0, canonical_id=101):
    """Helper: build a cart, submit, approve. Return (conn, order_id)."""
    conn = ordering_env["master_conn"]
    cart = sop.get_or_create_cart(
        conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test",
    )
    sop.add_cart_item(conn, cart["id"], canonical_id, qty, "件")
    order = sop.submit_order(conn, cart["id"], requested_by=2, expected_delivery_date=None, note="")
    order = sop.review_order(conn, order["id"], sop.ORDER_STATUS_APPROVED, actor_id=5)
    return conn, order["id"]


def test_ship_order_partial_single_item(ordering_env):
    """v3 T10: 部分发货不抛错，shipped_quantity 累计。

    单次 ship 1/3：DC -1，明细 shipped_quantity=1，明细 status=partial，
    订单仍 approved，deliveries 1 行，is_fully_shipped=False。
    """
    conn, order_id = _make_approved_order(ordering_env, qty=3.0)
    order = sop.get_order_detail(conn, order_id)
    item_id = int(order["order_items"][0]["id"])

    result = sop.ship_order(conn, order_id, {item_id: 1.0}, shipped_by=4, tracking_note="batch-1")

    assert result["status"] == sop.ORDER_STATUS_APPROVED  # partial — 不翻转
    assert result["is_fully_shipped"] is False
    assert result["new_order_status"] == sop.ORDER_STATUS_APPROVED
    assert result["delivery_id"] > 0
    assert len(result["shipped_items"]) == 1
    assert result["shipped_items"][0]["order_item_id"] == item_id
    assert result["shipped_items"][0]["shipped_quantity"] == 1.0

    # 明细状态推进到 partial。
    item = next(it for it in result["order_items"] if int(it["id"]) == item_id)
    assert item["shipped_quantity"] == 1.0
    assert item["status"] == sop.ORDER_ITEM_STATUS_PARTIAL

    # DC 库存 -1。
    dc = sqlite3.connect(str(ordering_env["dc_path"]))
    dc.row_factory = sqlite3.Row
    row = dc.execute("SELECT quantity FROM items WHERE canonical_id=101").fetchone()
    assert row["quantity"] == 99.0
    # stock_movements delta=-1，note 含订单号 + partial + 数量。
    mv = dc.execute(
        "SELECT * FROM stock_movements WHERE action=? ORDER BY id DESC LIMIT 1",
        (sop.SHIPMENT_ACTION,),
    ).fetchone()
    assert mv["delta"] == -1.0
    assert str(order["order_no"]) in mv["note"]
    assert "partial 1" in mv["note"]
    dc.close()

    # deliveries 1 行。
    refreshed = sop.get_order_detail(conn, order_id)
    assert len(refreshed["deliveries"]) == 1
    assert refreshed["deliveries"][0]["shipped_by"] == 4
    assert refreshed["deliveries"][0]["tracking_note"] == "batch-1"
    # 写入一条状态历史（approved → approved，note 写本批发货 X 件）。
    history_notes = [h["note"] for h in refreshed["status_history"]]
    assert any("本批发货" in (n or "") for n in history_notes)


def test_ship_order_partial_multi_batch(ordering_env):
    """v3 T10: 多次部分发货累计 → 最终一票发齐 → status=shipped。
    """
    conn, order_id = _make_approved_order(ordering_env, qty=3.0)
    order = sop.get_order_detail(conn, order_id)
    item_id = int(order["order_items"][0]["id"])

    # 第一次：发 1 件。
    r1 = sop.ship_order(conn, order_id, {item_id: 1.0}, shipped_by=4)
    assert r1["is_fully_shipped"] is False
    assert r1["new_order_status"] == sop.ORDER_STATUS_APPROVED

    # 第二次：发 2 件，凑齐 3 件。
    r2 = sop.ship_order(conn, order_id, {item_id: 2.0}, shipped_by=4)
    assert r2["is_fully_shipped"] is True
    assert r2["new_order_status"] == sop.ORDER_STATUS_SHIPPED

    # 累计验证。
    refreshed = sop.get_order_detail(conn, order_id)
    item = next(it for it in refreshed["order_items"] if int(it["id"]) == item_id)
    assert item["shipped_quantity"] == 3.0
    assert item["status"] == sop.ORDER_ITEM_STATUS_FULFILLED
    assert refreshed["status"] == sop.ORDER_STATUS_SHIPPED
    assert refreshed["shipped_at"] is not None
    # deliveries 2 行。
    assert len(refreshed["deliveries"]) == 2


def test_ship_order_zero_qty_skipped(ordering_env):
    """v3 T10: shipped_items_map 中 qty=0 的明细视为「本批不发」，不动库存不写 movements。

    整张 map 全是 0 → raise ValueError；qty=0 + 正常 qty 混合 → 静默跳过 0，
    仅处理 >0 的明细。无效 key（非订单明细）→ ValueError（不静默）。
    """
    conn, order_id = _make_approved_order(ordering_env, qty=5.0)
    order = sop.get_order_detail(conn, order_id)
    item_id = int(order["order_items"][0]["id"])

    # 全部 qty=0：抛错（"本批发货数量全部为空"）。
    with pytest.raises(ValueError, match="本批发货数量全部为空"):
        sop.ship_order(conn, order_id, {item_id: 0}, shipped_by=4)

    with pytest.raises(ValueError, match="本批发货数量全部为空"):
        sop.ship_order(conn, order_id, {item_id: 0.0}, shipped_by=4)

    # 混合：把无效 key 放在前面，验证依然抛错（设计：无效 key 显式拒绝）。
    with pytest.raises(ValueError, match="不属于"):
        sop.ship_order(conn, order_id, {999999: 0.0, item_id: 5.0}, shipped_by=4)

    # 单 key + 正常值：发齐。
    result = sop.ship_order(conn, order_id, {item_id: 5.0}, shipped_by=4)
    assert result["is_fully_shipped"] is True
    assert result["shipped_items"][0]["shipped_quantity"] == 5.0

    # DC 应该 -5：发 5 件，所以 100 - 5 = 95。
    dc = sqlite3.connect(str(ordering_env["dc_path"]))
    dc.row_factory = sqlite3.Row
    row = dc.execute("SELECT quantity FROM items WHERE canonical_id=101").fetchone()
    assert row["quantity"] == 95.0
    # 仅一次成功的 ship 写了 stock_movement（前两次被拒）。
    mv_count = dc.execute(
        "SELECT COUNT(*) FROM stock_movements WHERE action=?",
        (sop.SHIPMENT_ACTION,),
    ).fetchone()[0]
    assert mv_count == 1
    dc.close()


def test_ship_order_partial_invalid_item_id(ordering_env):
    """shipped_items_map 包含不属于该订单的 id → ValueError。"""
    conn, order_id = _make_approved_order(ordering_env, qty=3.0)
    with pytest.raises(ValueError, match="不属于"):
        sop.ship_order(conn, order_id, {99999: 1.0}, shipped_by=4)


def test_ship_order_partial_overshoot_rejected(ordering_env):
    """累计发货超过 quantity → ValueError。"""
    conn, order_id = _make_approved_order(ordering_env, qty=3.0)
    order = sop.get_order_detail(conn, order_id)
    item_id = int(order["order_items"][0]["id"])
    sop.ship_order(conn, order_id, {item_id: 2.0}, shipped_by=4)
    # 已发 2 / 3，再发 2 件就超发。
    with pytest.raises(ValueError, match="累计发货"):
        sop.ship_order(conn, order_id, {item_id: 2.0}, shipped_by=4)


def test_ship_order_partial_not_approved(ordering_env):
    """订单非 approved 时调用 partial ship → ValueError。"""
    conn = ordering_env["master_conn"]
    cart = sop.get_or_create_cart(
        conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test",
    )
    sop.add_cart_item(conn, cart["id"], 101, 3.0, "件")
    order = sop.submit_order(conn, cart["id"], requested_by=2, expected_delivery_date=None, note="")
    item_id = int(order["order_items"][0]["id"])
    # pending 状态。
    with pytest.raises(ValueError, match="approved"):
        sop.ship_order(conn, order["id"], {item_id: 1.0}, shipped_by=4)


def test_get_dc_items_for_order_detail(ordering_env):
    """v3 T10 / A6: 返回 {order_item_id: {name, dc_available, unit_price, category_name}}。"""
    conn, order_id = _make_approved_order(ordering_env, qty=3.0)
    order = sop.get_order_detail(conn, order_id)
    item_id = int(order["order_items"][0]["id"])

    info = sop.get_dc_items_for_order_detail(conn, order_id)
    assert info, "应至少返回 1 条明细的 DC 库存信息"
    assert item_id in info
    row = info[item_id]
    # name：canonical_items.name
    assert row["name"] == "测试包材A"
    # dc_available：DC 仓 items.quantity（发货前）
    assert row["dc_available"] == 100.0
    # unit_price：selling_price > unit_cost > 0；fixture 都 0 所以 0。
    assert row["unit_price"] == 0.0
    # category_name：canonical_categories.name 中文 fallback
    assert row["category_name"] == "包材"

    # 部分发货后，dc_available 下降。
    sop.ship_order(conn, order_id, {item_id: 2.0}, shipped_by=4)
    info2 = sop.get_dc_items_for_order_detail(conn, order_id)
    assert info2[item_id]["dc_available"] == 98.0

    # 不存在的订单 → {}。
    assert sop.get_dc_items_for_order_detail(conn, 99999) == {}
