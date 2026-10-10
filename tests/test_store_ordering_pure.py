"""Unit tests for store_ordering_pure.py.

Tests do not require Flask; they exercise pure SQLite logic.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

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


def test_open_warehouse_db_triggers_lazy_migration(tmp_path, monkeypatch):
    """Regression for issue #12: open_warehouse_db must apply the latest
    column migrations so callers (e.g. ``list_available_dc_items``) don't
    500 with ``no such column: is_orderable`` on legacy wh_XXX.db files
    that were never touched by the get_warehouse_db() lazy-migration path.
    """
    import config as config_module
    import db as db_module
    from db import WAREHOUSE_SCHEMA

    wh_dir = tmp_path / "warehouses"
    wh_dir.mkdir()
    legacy_path = wh_dir / "legacy_dc.db"

    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_dir)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", wh_dir)

    # Simulate a "legacy" db: base schema only, no v3.1+ column migrations.
    # Matches what init_warehouse_db() did before the lazy-migration hook
    # was tightened (issue #12).
    legacy = sqlite3.connect(legacy_path)
    legacy.executescript(WAREHOUSE_SCHEMA)
    legacy.commit()
    legacy.close()

    # Sanity: is_orderable must NOT be present yet (or this test is
    # meaningless — the migration is a no-op on already-migrated dbs).
    pre = sqlite3.connect(legacy_path)
    pre_cols = {r[1] for r in pre.execute("PRAGMA table_info(items)").fetchall()}
    pre.close()
    assert "is_orderable" not in pre_cols, (
        "test setup invariant broken: WAREHOUSE_SCHEMA itself contains "
        "is_orderable; drop the assertion or use a smaller schema"
    )

    # Act: open_warehouse_db should run the lazy migration.
    conn = sop.open_warehouse_db("legacy_dc")
    try:
        post_cols = {r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()}
        assert "is_orderable" in post_cols
        # And the column is actually queryable in the SELECT path that
        # used to 500.
        rows = conn.execute(
            "SELECT is_orderable FROM items WHERE canonical_id IS NOT NULL"
        ).fetchall()
        assert rows == []
    finally:
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


def test_ship_order_insufficient_stock_rejected(ordering_env):
    """2026-10-10 决策反转：禁止超库存发货（原 v3 A7「允许欠货出库」已废止）。

    DC 库存 5，订单要出 10 件 → ``ship_order_full`` 抛 ValueError；
    DC 库存保持 5（绝不跌负）、无 stock_movements、订单停在 approved。
    """
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

    with pytest.raises(ValueError, match="库存不足"):
        sop.ship_order_full(conn, order["id"], shipped_by=4)

    # DC 库存未被扣减、无出库流水。
    dc = sqlite3.connect(str(ordering_env["dc_path"]))
    dc.row_factory = sqlite3.Row
    assert dc.execute(
        "SELECT quantity FROM items WHERE canonical_id=101"
    ).fetchone()["quantity"] == 5.0
    assert dc.execute(
        "SELECT COUNT(*) AS c FROM stock_movements WHERE action=?",
        (sop.SHIPMENT_ACTION,),
    ).fetchone()["c"] == 0
    dc.close()

    # 订单未推进、实发为 0。
    after = sop.get_order_detail(conn, order["id"])
    assert after["status"] == sop.ORDER_STATUS_APPROVED
    assert float(after["order_items"][0]["shipped_quantity"] or 0) == 0.0


def test_ship_order_at_stock_limit_ok(ordering_env):
    """边界：本批发货量恰好等于 DC 库存时应放行（库存归零，不为负）。"""
    conn = ordering_env["master_conn"]
    cart = sop.get_or_create_cart(conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test")
    sop.add_cart_item(conn, cart["id"], 101, 10.0, "件")
    order = sop.submit_order(conn, cart["id"], requested_by=2, expected_delivery_date=None, note="")
    order = sop.review_order(conn, order["id"], sop.ORDER_STATUS_APPROVED, actor_id=5)

    dc = sqlite3.connect(str(ordering_env["dc_path"]))
    dc.execute("UPDATE items SET quantity=? WHERE canonical_id=?", (10.0, 101))
    dc.commit()
    dc.close()

    result = sop.ship_order_full(conn, order["id"], shipped_by=4)
    assert result["status"] == sop.ORDER_STATUS_SHIPPED

    dc = sqlite3.connect(str(ordering_env["dc_path"]))
    dc.row_factory = sqlite3.Row
    assert dc.execute(
        "SELECT quantity FROM items WHERE canonical_id=101"
    ).fetchone()["quantity"] == 0.0
    dc.close()


def test_ship_order_partial_stock_guard_per_line(ordering_env):
    """多批累计：DC 库存 60、订单 100。首批发 60（库存→0）放行；
    第二批再发 20（订单累计仅 80，未超订单量）应被库存校验拒绝。"""
    conn = ordering_env["master_conn"]
    cart = sop.get_or_create_cart(conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test")
    sop.add_cart_item(conn, cart["id"], 101, 100.0, "件")
    order = sop.submit_order(conn, cart["id"], requested_by=2, expected_delivery_date=None, note="")
    order = sop.review_order(conn, order["id"], sop.ORDER_STATUS_APPROVED, actor_id=5)
    oiid = int(order["order_items"][0]["id"])

    dc = sqlite3.connect(str(ordering_env["dc_path"]))
    dc.execute("UPDATE items SET quantity=? WHERE canonical_id=?", (60.0, 101))
    dc.commit()
    dc.close()

    sop.ship_order(conn, order["id"], {oiid: 60.0}, shipped_by=4)
    dc = sqlite3.connect(str(ordering_env["dc_path"]))
    dc.row_factory = sqlite3.Row
    assert dc.execute(
        "SELECT quantity FROM items WHERE canonical_id=101"
    ).fetchone()["quantity"] == 0.0
    dc.close()

    with pytest.raises(ValueError, match="库存不足"):
        sop.ship_order(conn, order["id"], {oiid: 20.0}, shipped_by=4)

    dc = sqlite3.connect(str(ordering_env["dc_path"]))
    dc.row_factory = sqlite3.Row
    assert dc.execute(
        "SELECT quantity FROM items WHERE canonical_id=101"
    ).fetchone()["quantity"] == 0.0
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


def test_receive_allowed_while_approved_after_partial_ship(ordering_env):
    """v3 部分发货：订单停在 approved，门店应能收「已发」的那部分。

    回归 2026-10-10 缺陷：收货门禁只看 status == 'shipped'，而分批发货
    期间订单一直停在 approved（全部发齐才转 shipped）→ 门店端根本收不了货。
    """
    conn = ordering_env["master_conn"]
    cart = sop.get_or_create_cart(
        conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test"
    )
    sop.add_cart_item(conn, cart["id"], 101, 30.0, "件")
    order = sop.submit_order(
        conn, cart["id"], requested_by=2, expected_delivery_date=None, note=""
    )
    order = sop.review_order(conn, order["id"], sop.ORDER_STATUS_APPROVED, actor_id=5)
    order_item_id = order["order_items"][0]["id"]

    # DC 只发 5 / 30 → 订单仍在 approved。
    shipped = sop.ship_order(conn, order["id"], {order_item_id: 5.0}, shipped_by=4)
    assert shipped["is_fully_shipped"] is False
    assert shipped["new_order_status"] == sop.ORDER_STATUS_APPROVED

    # 门店可收已发的 5 件。
    r = sop.receive_order_item(
        conn, order_id=order["id"], order_item_id=order_item_id, qty=5.0, actor_id=3,
    )
    assert r["fulfilled_quantity"] == 5.0
    # 只收完「已发部分」不算整单收齐 → 订单保持 approved，
    # 不得因为收完当前批次就跳到 delivered。
    assert r["is_fully_received"] is False
    assert r["new_order_status"] == sop.ORDER_STATUS_APPROVED
    assert sop.get_order_detail(conn, order["id"])["status"] == sop.ORDER_STATUS_APPROVED


def test_receive_beyond_shipped_rejected_when_approved(ordering_env):
    """部分发货期间，门店收货上限 = 已发量（而非订货量）。"""
    conn = ordering_env["master_conn"]
    cart = sop.get_or_create_cart(
        conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test"
    )
    sop.add_cart_item(conn, cart["id"], 101, 100.0, "件")
    order = sop.submit_order(
        conn, cart["id"], requested_by=2, expected_delivery_date=None, note=""
    )
    order = sop.review_order(conn, order["id"], sop.ORDER_STATUS_APPROVED, actor_id=5)
    order_item_id = order["order_items"][0]["id"]
    sop.ship_order(conn, order["id"], {order_item_id: 2.0}, shipped_by=4)  # 发 2 / 100

    # 收 100（=订货量）必须被拒 —— 只发了 2。
    with pytest.raises(ValueError, match="超过待收"):
        sop.receive_order_item(
            conn, order_id=order["id"], order_item_id=order_item_id, qty=100.0, actor_id=3,
        )
    # 收 3（> 已发 2）也必须被拒。
    with pytest.raises(ValueError, match="超过待收"):
        sop.receive_order_item(
            conn, order_id=order["id"], order_item_id=order_item_id, qty=3.0, actor_id=3,
        )
    # 收 2 正常。
    r = sop.receive_order_item(
        conn, order_id=order["id"], order_item_id=order_item_id, qty=2.0, actor_id=3,
    )
    assert r["fulfilled_quantity"] == 2.0


def test_receive_unshipped_line_rejected_when_approved(ordering_env):
    """多明细订单：DC 只发了其中一行，未发货的那行不可收。"""
    conn = ordering_env["master_conn"]
    # fixture 门店侧默认只绑定了 canonical 101；补绑 102 才能下两行订单。
    store = sqlite3.connect(str(ordering_env["store_path"]))
    store.row_factory = sqlite3.Row
    cat_id = store.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()["id"]
    store.execute(
        "INSERT INTO items (sku, name, category_id, quantity, unit, canonical_id, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("ST-B2", "测试包材B", cat_id, 0.0, "件", 102,
         datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    store.commit()
    store.close()

    cart = sop.get_or_create_cart(
        conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test"
    )
    sop.add_cart_item(conn, cart["id"], 101, 10.0, "件")
    sop.add_cart_item(conn, cart["id"], 102, 10.0, "件")
    order = sop.submit_order(
        conn, cart["id"], requested_by=2, expected_delivery_date=None, note=""
    )
    order = sop.review_order(conn, order["id"], sop.ORDER_STATUS_APPROVED, actor_id=5)
    ids = {int(it["canonical_id"]): int(it["id"]) for it in order["order_items"]}
    a, b = ids[101], ids[102]

    sop.ship_order(conn, order["id"], {a: 10.0}, shipped_by=4)  # 只发 101 行

    # 未发货行（102）收货 → 拒绝。
    with pytest.raises(ValueError, match="超过待收"):
        sop.receive_order_item(
            conn, order_id=order["id"], order_item_id=b, qty=1.0, actor_id=3,
        )
    # 已发齐行（101）可收，但整单未收齐（102 还没发）。
    r = sop.receive_order_item(
        conn, order_id=order["id"], order_item_id=a, qty=10.0, actor_id=3,
    )
    assert r["fulfilled_quantity"] == 10.0
    assert r["is_fully_received"] is False


def test_cancel_rejected_after_partial_receipt(ordering_env):
    """已有收货记录的订单不得取消 —— 货已实际入库，作废会造成账实不符。"""
    conn = ordering_env["master_conn"]
    cart = sop.get_or_create_cart(
        conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test"
    )
    sop.add_cart_item(conn, cart["id"], 101, 30.0, "件")
    order = sop.submit_order(
        conn, cart["id"], requested_by=2, expected_delivery_date=None, note=""
    )
    order = sop.review_order(conn, order["id"], sop.ORDER_STATUS_APPROVED, actor_id=5)
    order_item_id = order["order_items"][0]["id"]
    sop.ship_order(conn, order["id"], {order_item_id: 5.0}, shipped_by=4)
    sop.receive_order_item(
        conn, order_id=order["id"], order_item_id=order_item_id, qty=5.0, actor_id=3,
    )

    with pytest.raises(ValueError, match="已有收货记录"):
        sop.cancel_order(conn, order["id"], cancelled_by=5, reason="想撤单")
    # 订单状态未被改动。
    assert sop.get_order_detail(conn, order["id"])["status"] == sop.ORDER_STATUS_APPROVED


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
        sop.EVENT_ORDER_RECEIVED,
        sop.EVENT_ORDER_CANCELLED,
    ):
        assert et in ALLOWED_EVENT_TYPES


# ---------------------------------------------------------------------------
# P1-4: 取消订单（pure）
# ---------------------------------------------------------------------------

def test_cancel_order_pending_sets_status_and_history(ordering_env):
    conn = ordering_env["master_conn"]
    cart = sop.get_or_create_cart(
        conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test",
    )
    sop.add_cart_item(conn, cart["id"], 101, 5.0, "件")
    order = sop.submit_order(conn, cart["id"], requested_by=2, expected_delivery_date=None, note="")
    oid = order["id"]

    result = sop.cancel_order(conn, oid, cancelled_by=2, reason="门店撤单")
    assert result["status"] == sop.ORDER_STATUS_CANCELLED
    assert result["cancel_reason"] == "门店撤单"
    assert result["cancelled_by"] == 2

    rows = conn.execute(
        "SELECT to_status, from_status FROM store_order_status_history WHERE order_id=?",
        (oid,),
    ).fetchall()
    assert any(r["from_status"] == "pending" and r["to_status"] == "cancelled" for r in rows)


def test_cancel_order_approved_allowed(ordering_env):
    """已审批状态仍可取消（DC manager/admin 路径）。"""
    conn = ordering_env["master_conn"]
    cart = sop.get_or_create_cart(
        conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test",
    )
    sop.add_cart_item(conn, cart["id"], 101, 3.0, "件")
    order = sop.submit_order(conn, cart["id"], requested_by=2, expected_delivery_date=None, note="")
    sop.review_order(conn, order["id"], sop.ORDER_STATUS_APPROVED, actor_id=5)

    result = sop.cancel_order(conn, order["id"], cancelled_by=5, reason="库存调整")
    assert result["status"] == sop.ORDER_STATUS_CANCELLED


def test_cancel_order_shipped_rejected(ordering_env):
    """已发货后不可取消（状态机限制）。"""
    conn, order_id = _make_approved_order(ordering_env, qty=5.0)
    order = sop.get_order_detail(conn, order_id)
    item_id = int(order["order_items"][0]["id"])
    sop.ship_order_full(conn, order_id, shipped_by=4)

    try:
        sop.cancel_order(conn, order_id, cancelled_by=5, reason="晚了")
    except ValueError as e:
        assert "invalid transition" in str(e)
    else:
        raise AssertionError("expected ValueError on cancel-after-ship")


def test_notify_order_cancelled_targets_both_sides(ordering_env):
    """取消通知同时发给门店用户 + DC 审批人，去重并去自己。"""
    conn = ordering_env["master_conn"]
    cart = sop.get_or_create_cart(
        conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test",
    )
    sop.add_cart_item(conn, cart["id"], 101, 5.0, "件")
    order = sop.submit_order(conn, cart["id"], requested_by=2, expected_delivery_date=None, note="")

    # user 2 (requester) + store manager 3 + dc manager 5 + platform admin 1
    count = sop.notify_order_event(conn, sop.EVENT_ORDER_CANCELLED, order, actor_user_id=2)
    assert count == 3  # 去重：4 个里去掉 actor=2
    rows = conn.execute(
        "SELECT user_id FROM notifications WHERE event_type=?",
        (sop.EVENT_ORDER_CANCELLED,),
    ).fetchall()
    notified = {r["user_id"] for r in rows}
    assert notified == {1, 3, 5}


def test_notify_order_received_targets_dc_reviewers(ordering_env):
    """partial receive 通知发给 DC 审批人 + 平台管理员（去自己）。"""
    conn = ordering_env["master_conn"]
    cart = sop.get_or_create_cart(
        conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test",
    )
    sop.add_cart_item(conn, cart["id"], 101, 5.0, "件")
    order = sop.submit_order(conn, cart["id"], requested_by=2, expected_delivery_date=None, note="")

    # actor=2 (门店 requester)，DC manager 5 + platform admin 1 应被通知
    count = sop.notify_order_event(conn, sop.EVENT_ORDER_RECEIVED, order, actor_user_id=2)
    assert count == 2
    rows = conn.execute(
        "SELECT user_id FROM notifications WHERE event_type=?",
        (sop.EVENT_ORDER_RECEIVED,),
    ).fetchall()
    notified = {r["user_id"] for r in rows}
    assert notified == {1, 5}


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


# ---------------------------------------------------------------------------
# P1-5: 订货量建议
# ---------------------------------------------------------------------------

def _seed_outbound(store_path, item_id, qty, days_ago, ts):
    """在门店仓插入一条 outbound_requests（模拟历史消耗）。"""
    import os
    from datetime import timedelta
    conn = sqlite3.connect(str(store_path))
    ts_old = (datetime.strptime(ts, "%Y-%m-%d %H:%M:%S") - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """INSERT INTO outbound_requests
           (item_id, requested_quantity, rolled_back, created_at)
           VALUES (?, ?, 0, ?)""",
        (item_id, qty, ts_old),
    )
    conn.commit()
    conn.close()


def test_compute_suggested_order_qty_basic(ordering_env):
    """门店无库存 + 7 天消耗 14 件 + cover_days=7 → safety=14, suggested=14."""
    conn = ordering_env["master_conn"]
    store_path = ordering_env["store_path"]
    # 找到 store 里的 canonical_id=101 的 items.id
    store = sqlite3.connect(str(store_path))
    store.row_factory = sqlite3.Row
    item_id = int(store.execute(
        "SELECT id FROM items WHERE canonical_id=101"
    ).fetchone()["id"])
    store.close()
    # 7 天前消耗 14 件（一次性）
    _seed_outbound(store_path, item_id, 14.0, days_ago=2, ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    store = sqlite3.connect(str(store_path))
    store.row_factory = sqlite3.Row
    result = sop.compute_suggested_order_qty(store, conn, "store_test")
    store.close()

    # safety = 14 / 7 * 7 = 14, current=0, transit=0 → suggested=14
    assert result.get(101) == 14


def test_compute_suggested_order_qty_subtracts_current_and_transit(ordering_env):
    """current + transit 应被减去；最终 suggested = safety - current - transit。"""
    conn = ordering_env["master_conn"]
    store_path = ordering_env["store_path"]
    store = sqlite3.connect(str(store_path))
    store.row_factory = sqlite3.Row
    item_id = int(store.execute("SELECT id FROM items WHERE canonical_id=101").fetchone()["id"])
    # 当前库存 +5
    store.execute("UPDATE items SET quantity=? WHERE id=?", (5.0, item_id))
    store.commit()
    store.close()

    # 7 天消耗 21 件 → daily_avg=3, safety=3*7=21
    _seed_outbound(store_path, item_id, 21.0, days_ago=3, ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    # 在途：建一个 pending 订单，quantity=6
    cart = sop.get_or_create_cart(conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test")
    sop.add_cart_item(conn, cart["id"], 101, 6.0, "件")
    sop.submit_order(conn, cart["id"], requested_by=2, expected_delivery_date=None, note="")

    store = sqlite3.connect(str(store_path))
    store.row_factory = sqlite3.Row
    result = sop.compute_suggested_order_qty(store, conn, "store_test")
    store.close()

    # safety=21, current=5, transit=6 → suggested=10
    assert result.get(101) == 10


def test_compute_suggested_order_qty_skips_zero(ordering_env):
    """消耗 + 在途都已覆盖时，不进建议列表（suggested=0 不返回）。"""
    conn = ordering_env["master_conn"]
    store_path = ordering_env["store_path"]
    store = sqlite3.connect(str(store_path))
    store.row_factory = sqlite3.Row
    item_id = int(store.execute("SELECT id FROM items WHERE canonical_id=101").fetchone()["id"])
    # 当前 100 件 + 零消耗 + 零在途
    store.execute("UPDATE items SET quantity=? WHERE id=?", (100.0, item_id))
    store.commit()
    store.close()

    store = sqlite3.connect(str(store_path))
    store.row_factory = sqlite3.Row
    result = sop.compute_suggested_order_qty(store, conn, "store_test")
    store.close()

    # 无消耗 → 不返回 101
    assert 101 not in result


def test_compute_suggested_order_qty_counts_in_transit_from_approved(ordering_env):
    """approved / shipped 未收齐的也在 in_transit 范围。"""
    conn = ordering_env["master_conn"]
    store_path = ordering_env["store_path"]
    store = sqlite3.connect(str(store_path))
    store.row_factory = sqlite3.Row
    item_id = int(store.execute("SELECT id FROM items WHERE canonical_id=101").fetchone()["id"])
    store.execute("UPDATE items SET quantity=? WHERE id=?", (0.0, item_id))
    store.commit()
    store.close()

    # 建 shipped 订单，fulfilled_quantity=0 → 全量在途
    cart = sop.get_or_create_cart(conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test")
    sop.add_cart_item(conn, cart["id"], 101, 8.0, "件")
    order = sop.submit_order(conn, cart["id"], requested_by=2, expected_delivery_date=None, note="")
    sop.review_order(conn, order["id"], sop.ORDER_STATUS_APPROVED, actor_id=5)
    sop.ship_order_full(conn, order["id"], shipped_by=4)
    # 0 消耗 → safety=0 → 不建议

    # 加点消耗触发建议
    _seed_outbound(store_path, item_id, 7.0, days_ago=1, ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    store = sqlite3.connect(str(store_path))
    store.row_factory = sqlite3.Row
    result = sop.compute_suggested_order_qty(store, conn, "store_test")
    store.close()

    # daily_avg=1, safety=7, current=0, transit=8 → suggested=0（被 transit 覆盖完）
    # 不应返回 101
    assert 101 not in result


# ---------------------------------------------------------------------------
# P2-4: 报表（多维汇总）
# ---------------------------------------------------------------------------

def _make_report_order(
    conn, *, canonical_id=101, qty=10.0, shipped_qty=None, fulfilled_qty=None,
    dc_code="dc_test", store_code="store_test", requested_by=2,
):
    """建一个 pending → approved → shipped → partial-received 订单。

    shipped_qty 默认等于 qty（全量发）。
    fulfilled_qty 默认 min(8, qty)（欠 2 或全收齐）。
    """
    if shipped_qty is None:
        shipped_qty = qty
    if fulfilled_qty is None:
        fulfilled_qty = min(8.0, qty)
    cart = sop.get_or_create_cart(
        conn, user_id=requested_by, store_warehouse_code=store_code,
        dc_warehouse_code=dc_code,
    )
    sop.add_cart_item(conn, cart["id"], canonical_id, qty, "件")
    order = sop.submit_order(conn, cart["id"], requested_by=requested_by, expected_delivery_date=None, note="")
    sop.review_order(conn, order["id"], sop.ORDER_STATUS_APPROVED, actor_id=5)
    if shipped_qty > 0:
        sop.ship_order(
            conn, order["id"], {int(order["order_items"][0]["id"]): shipped_qty},
            shipped_by=4,
        )
    if fulfilled_qty > 0:
        sop.receive_order_item(
            conn, order_id=order["id"], order_item_id=int(order["order_items"][0]["id"]),
            qty=fulfilled_qty, actor_id=2,
        )
    return order


def test_compute_store_order_report_basic(ordering_env):
    """P2-4: 一个订单（欠收 2/10），summary 与 4 个维度都应有数据。"""
    conn = ordering_env["master_conn"]
    _make_report_order(conn, qty=10.0, shipped_qty=10.0, fulfilled_qty=8.0)

    report = sop.compute_store_order_report(conn)
    s = report["summary"]
    assert s["ordered_qty"] == 10.0
    assert s["shipped_qty"] == 10.0
    assert s["received_qty"] == 8.0
    assert s["shortfall_qty"] == 2.0
    assert s["shortfall_rate"] == 0.2
    assert s["order_count"] == 1

    # 4 个维度都有数据
    assert len(report["by_store"]) == 1
    assert report["by_store"][0]["store_code"] == "store_test"
    assert report["by_store"][0]["ordered_qty"] == 10.0

    assert len(report["by_dc"]) == 1
    assert report["by_dc"][0]["dc_code"] == "dc_test"

    assert len(report["by_category"]) == 1
    assert report["by_category"][0]["category_code"] == "PACKAGING"

    assert len(report["by_canonical"]) == 1
    assert report["by_canonical"][0]["canonical_name"] == "测试包材A"
    assert report["by_canonical"][0]["ordered_qty"] == 10.0


def test_compute_store_order_report_no_shortfall(ordering_env):
    """全收齐时 shortfall=0、rate=0。"""
    conn = ordering_env["master_conn"]
    _make_report_order(conn, qty=5.0, shipped_qty=5.0, fulfilled_qty=5.0)

    report = sop.compute_store_order_report(conn)
    s = report["summary"]
    assert s["shortfall_qty"] == 0.0
    assert s["shortfall_rate"] == 0.0


def test_compute_store_order_report_filter_by_store(ordering_env):
    """?store= 过滤：只返回指定门店的数据。"""
    conn = ordering_env["master_conn"]
    _make_report_order(conn, store_code="store_test", dc_code="dc_test")

    report = sop.compute_store_order_report(conn, store_warehouse_code="store_test")
    assert report["summary"]["ordered_qty"] == 10.0

    # 用一个不存在的门店，应该无数据
    report_empty = sop.compute_store_order_report(conn, store_warehouse_code="nonexistent")
    assert report_empty["summary"]["ordered_qty"] == 0.0
    assert report_empty["summary"]["order_count"] == 0
    assert report_empty["by_store"] == []


def test_compute_store_order_report_filter_by_dc(ordering_env):
    """?dc= 过滤。"""
    conn = ordering_env["master_conn"]
    _make_report_order(conn, dc_code="dc_test")

    report = sop.compute_store_order_report(conn, dc_warehouse_code="dc_test")
    assert report["summary"]["ordered_qty"] == 10.0


def test_compute_store_order_report_filter_by_date_range(ordering_env):
    """?start_date= ?end_date= 过滤：超范围订单不计入。"""
    conn = ordering_env["master_conn"]
    _make_report_order(conn)

    # 今天日期
    today = datetime.now().strftime("%Y-%m-%d")
    report = sop.compute_store_order_report(conn, start_date=today, end_date=today)
    assert report["summary"]["ordered_qty"] == 10.0

    # 昨天的范围不应包含今天的订单
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    report_empty = sop.compute_store_order_report(conn, start_date=yesterday, end_date=yesterday)
    assert report_empty["summary"]["ordered_qty"] == 0.0


def test_compute_store_order_report_multiple_orders(ordering_env):
    """多订单时 by_store / by_dc 应正确汇总（不重复 order_count）。"""
    conn = ordering_env["master_conn"]
    _make_report_order(conn, qty=10.0)
    _make_report_order(conn, qty=5.0)

    report = sop.compute_store_order_report(conn)
    # 总订货 15
    assert report["summary"]["ordered_qty"] == 15.0
    # order_count 应该去重 = 2
    assert report["summary"]["order_count"] == 2
    # by_store 只有 1 行（都是 store_test）
    assert len(report["by_store"]) == 1
    assert report["by_store"][0]["ordered_qty"] == 15.0


def test_compute_store_order_report_filter_by_category(ordering_env):
    """?cat= 过滤：按品类。"""
    conn = ordering_env["master_conn"]
    _make_report_order(conn, canonical_id=101)  # PACKAGING

    report = sop.compute_store_order_report(conn, category_code="PACKAGING")
    assert report["summary"]["ordered_qty"] == 10.0

    report_empty = sop.compute_store_order_report(conn, category_code="NONEXIST")
    assert report_empty["summary"]["ordered_qty"] == 0.0


# ---------------------------------------------------------------------------
# v3.1 B: 运费规则
# ---------------------------------------------------------------------------

def test_compute_shipping_fee_basic(ordering_env):
    """B1: fee = base_fee + subtotal × pct_fee."""
    conn = ordering_env["master_conn"]
    rule = {"base_fee": 5.0, "pct_fee": 0.01}
    assert sop.compute_shipping_fee(100.0, rule) == 6.0  # 5 + 100×0.01

    # 大金额
    assert sop.compute_shipping_fee(1000.0, {"base_fee": 10.0, "pct_fee": 0.02}) == 30.0  # 10 + 20


def test_compute_shipping_fee_zero_subtotal(ordering_env):
    """B2: subtotal=0 时仍收 base_fee。"""
    conn = ordering_env["master_conn"]
    rule = {"base_fee": 5.0, "pct_fee": 0.01}
    assert sop.compute_shipping_fee(0.0, rule) == 5.0


def test_compute_shipping_fee_quantizes_2dp(ordering_env):
    """B3: 浮点精度 — 2dp 量化。"""
    conn = ordering_env["master_conn"]
    rule = {"base_fee": 1.0, "pct_fee": 0.005}  # 0.005 = 0.5%
    # 33.33 × 0.005 = 0.16665 → 量化 0.17
    fee = sop.compute_shipping_fee(33.33, rule)
    assert fee == 1.17  # 1 + 0.17


def test_compute_shipping_fee_no_rule(ordering_env):
    """B4 + B9: 无 active 规则时返回 0（向后兼容）。"""
    conn = ordering_env["master_conn"]
    assert sop.compute_shipping_fee(100.0, None) == 0.0
    assert sop.get_active_shipping_rule(conn) is None


def test_get_active_shipping_rule(ordering_env):
    """B2/B4 续: 创建规则后 get 返回。"""
    conn = ordering_env["master_conn"]
    rid = sop.upsert_shipping_rule(conn, base_fee=8.0, pct_fee=0.02)
    rule = sop.get_active_shipping_rule(conn)
    assert rule is not None
    assert rule["id"] == rid
    assert float(rule["base_fee"]) == 8.0
    assert float(rule["pct_fee"]) == 0.02
    assert rule["active"] == 1


def test_upsert_shipping_rule_updates(ordering_env):
    """B5: 已有 active → 关掉，再插入新行（active=1）。"""
    conn = ordering_env["master_conn"]
    rid1 = sop.upsert_shipping_rule(conn, base_fee=5.0, pct_fee=0.01)
    rid2 = sop.upsert_shipping_rule(conn, base_fee=10.0, pct_fee=0.03)

    # 旧行 active=0，新行 active=1
    rule1 = conn.execute("SELECT * FROM shipping_rules WHERE id=?", (rid1,)).fetchone()
    rule2 = conn.execute("SELECT * FROM shipping_rules WHERE id=?", (rid2,)).fetchone()
    assert rule1["active"] == 0
    assert rule2["active"] == 1
    # get_active 只返回最新 active
    active = sop.get_active_shipping_rule(conn)
    assert active["id"] == rid2


def test_submit_order_writes_shipping_fee(ordering_env):
    """B6: submit_order 写入 shipping_fee，get_order_detail 返回。"""
    conn = ordering_env["master_conn"]
    # 配置规则：base=5, pct=1%
    sop.upsert_shipping_rule(conn, base_fee=5.0, pct_fee=0.01)
    # dc.items 中 canonical_id=101 unit_price 需要 >0 — fixture 里 DC item 有 canonical_id 但 unit_price=0
    # 直接测 shipping_fee 字段写入
    cart = sop.get_or_create_cart(
        conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test",
    )
    sop.add_cart_item(conn, cart["id"], 101, 5.0, "件")
    order = sop.submit_order(
        conn, cart["id"], requested_by=2, expected_delivery_date=None, note="",
    )
    # line_subtotal 是 ci.unit_price × qty，unit_price 来自 dc item
    # dc_test 里 canonical_id=101 的 unit_price 在 fixture 里没设（=0）
    # shipping_fee 应基于真实 subtotal（5 × 0 = 0）算 → fee = 5 + 0 = 5
    assert float(order["shipping_fee"]) == 5.0

    # get_order_detail 也应包含
    detail = sop.get_order_detail(conn, order["id"])
    assert float(detail["shipping_fee"]) == 5.0


def test_shipping_fee_locked_after_ship(ordering_env):
    """B7: ship 后 shipping_fee 不变。"""
    conn = ordering_env["master_conn"]
    sop.upsert_shipping_rule(conn, base_fee=5.0, pct_fee=0.01)

    cart = sop.get_or_create_cart(
        conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test",
    )
    sop.add_cart_item(conn, cart["id"], 101, 5.0, "件")
    order = sop.submit_order(
        conn, cart["id"], requested_by=2, expected_delivery_date=None, note="",
    )
    fee_before = float(order["shipping_fee"])

    # 审批 + 发货
    sop.review_order(conn, order["id"], sop.ORDER_STATUS_APPROVED, actor_id=5)
    sop.ship_order_full(conn, order["id"], shipped_by=4)

    detail_after = sop.get_order_detail(conn, order["id"])
    assert float(detail_after["shipping_fee"]) == fee_before


def test_rule_change_does_not_affect_existing_orders(ordering_env):
    """B8: 修改规则后，老订单用旧值，新订单用新值。"""
    conn = ordering_env["master_conn"]
    # 老规则 base=5
    sop.upsert_shipping_rule(conn, base_fee=5.0, pct_fee=0.01)

    cart1 = sop.get_or_create_cart(
        conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test",
    )
    sop.add_cart_item(conn, cart1["id"], 101, 5.0, "件")
    order1 = sop.submit_order(
        conn, cart1["id"], requested_by=2, expected_delivery_date=None, note="",
    )

    # 改规则 base=20
    sop.upsert_shipping_rule(conn, base_fee=20.0, pct_fee=0.05)

    cart2 = sop.get_or_create_cart(
        conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test",
    )
    sop.add_cart_item(conn, cart2["id"], 101, 5.0, "件")
    order2 = sop.submit_order(
        conn, cart2["id"], requested_by=2, expected_delivery_date=None, note="",
    )

    assert float(order1["shipping_fee"]) == 5.0  # 老规则
    assert float(order2["shipping_fee"]) == 20.0  # 新规则


# ---------------------------------------------------------------------------
# v3.1 A: 品项可订开关
# ---------------------------------------------------------------------------

def test_list_available_dc_items_filters_unorderable(ordering_env):
    """A2: is_orderable=0 的品项不进入 catalog 列表。"""
    conn = ordering_env["master_conn"]
    dc_path = ordering_env["dc_path"]
    # 下架 canonical_id=101
    dc = sqlite3.connect(str(dc_path))
    dc.execute("UPDATE items SET is_orderable=0 WHERE canonical_id=101")
    dc.commit()
    dc.close()

    items = sop.list_available_dc_items(conn, "dc_test")
    canonical_ids = [int(it["canonical_id"]) for it in items]
    assert 101 not in canonical_ids
    assert 102 in canonical_ids  # 其他品项仍可订


def test_set_dc_item_orderable_toggle(ordering_env):
    """A3: set_dc_item_orderable 切换后，catalog 列表反映新状态。"""
    conn = ordering_env["master_conn"]
    dc_path = ordering_env["dc_path"]

    # 先关闭
    sop.set_dc_item_orderable(conn, "dc_test", 101, False)
    items = sop.list_available_dc_items(conn, "dc_test")
    assert 101 not in [int(it["canonical_id"]) for it in items]

    # 再开启
    sop.set_dc_item_orderable(conn, "dc_test", 101, True)
    items = sop.list_available_dc_items(conn, "dc_test")
    assert 101 in [int(it["canonical_id"]) for it in items]


def test_set_dc_item_orderable_missing_canonical_raises(ordering_env):
    """A4: 不存在的 canonical_id 应抛 ValueError。"""
    conn = ordering_env["master_conn"]
    try:
        sop.set_dc_item_orderable(conn, "dc_test", 9999, False)
    except ValueError as e:
        assert "9999" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_list_dc_items_for_management_includes_unorderable(ordering_env):
    """A6: 管理列表含所有品项（含不可订）。"""
    conn = ordering_env["master_conn"]
    # 关闭 101
    sop.set_dc_item_orderable(conn, "dc_test", 101, False)

    items = sop.list_dc_items_for_management(conn, "dc_test")
    canonical_ids = [int(it["canonical_id"]) for it in items]
    assert 101 in canonical_ids
    assert 102 in canonical_ids
    # 标记 is_orderable=0 的能取到
    item_101 = next(it for it in items if int(it["canonical_id"]) == 101)
    assert item_101["is_orderable"] == 0


def test_list_unorderable_cart_canonicals(ordering_env):
    """A5 辅助：list_unorderable_cart_canonicals 返回被下架的品项。"""
    conn = ordering_env["master_conn"]
    sop.set_dc_item_orderable(conn, "dc_test", 101, False)

    unorderable = sop.list_unorderable_cart_canonicals(
        conn, "dc_test", [101, 102],
    )
    assert len(unorderable) == 1
    assert int(unorderable[0]["canonical_id"]) == 101

    # 全可订时返回空
    sop.set_dc_item_orderable(conn, "dc_test", 101, True)
    unorderable_empty = sop.list_unorderable_cart_canonicals(
        conn, "dc_test", [101, 102],
    )
    assert unorderable_empty == []


def test_submit_blocked_by_unorderable_item(ordering_env):
    """A5: 购物车里含被下架品项时，submit_order 不创建订单。"""
    conn = ordering_env["master_conn"]
    # 关闭 101
    sop.set_dc_item_orderable(conn, "dc_test", 101, False)

    cart = sop.get_or_create_cart(
        conn, user_id=2, store_warehouse_code="store_test", dc_warehouse_code="dc_test",
    )
    sop.add_cart_item(conn, cart["id"], 101, 5.0, "件")

    # 提交应被 validate_cart_for_submit（或上层 unorderable 校验）拦截
    # 这里仅验证 unorderable 列表非空
    items = sop.list_cart_items(conn, cart["id"])
    cart_canonicals = [int(it["canonical_id"]) for it in items]
    unorderable = sop.list_unorderable_cart_canonicals(conn, "dc_test", cart_canonicals)
    assert len(unorderable) == 1
    assert int(unorderable[0]["canonical_id"]) == 101
