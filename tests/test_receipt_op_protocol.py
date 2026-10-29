"""Task 6: cross-warehouse receipt mirror protocol (spec v2 §7/§8, D12).

Covers the seven scenarios in the implementation plan:
(a) normal receive accumulates store stock and ``fulfilled_quantity``,
(b) the same ``op_no`` retry only stocks once,
(c) a store failure after the master receipt landed is replay-safe,
(d) receiving more than shipped is rejected (ceiling is shipped, not ordered),
(e) an ``approved`` order still receives per-line shipped quantities,
(f) a cancel racing a committed shipment loses and the order stays approved,
(g) receiving everything transitions to ``delivered`` exactly once.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from blueprints import store_ordering_pure as sop
from db import init_master_db, init_warehouse_db

TS = "2026-10-29 10:00:00"


@pytest.fixture
def receipt_env(tmp_path, monkeypatch):
    """Master + one DC + one storefront, with an order factory."""
    import config as config_module
    import db as db_module

    master_path = tmp_path / "master.db"
    wh_dir = tmp_path / "warehouses"
    wh_dir.mkdir()
    a_path = wh_dir / "A.db"
    store_path = wh_dir / "store_test.db"

    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_dir)
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", wh_dir)

    init_master_db()
    for path in (a_path, store_path):
        init_warehouse_db(path)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    m = sqlite3.connect(str(master_path))
    m.row_factory = sqlite3.Row
    for uid, name, is_admin in (
        (1, "store_staff", 0),
        (2, "store_mgr", 0),
        (3, "dc_staff", 0),
    ):
        m.execute(
            "INSERT INTO users (id, username, password_hash, is_admin, created_at)"
            " VALUES (?, ?, 'x', ?, ?)",
            (uid, name, is_admin, ts),
        )
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at)"
        " VALUES (1, 'A', '配送中心A', ?, 'distribution_center', ?)",
        (str(a_path), ts),
    )
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at)"
        " VALUES (2, 'store_test', '测试门店', ?, 'storefront', ?)",
        (str(store_path), ts),
    )
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (1, 2, 'staff')")
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (2, 2, 'manager')")
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (3, 1, 'staff')")
    m.execute(
        "INSERT INTO canonical_categories (code, name, description, created_at, updated_at)"
        " VALUES ('PACKAGING', '包材', '', ?, ?)",
        (ts, ts),
    )
    for cid, sku, name in ((101, "IC-000101", "测试包材A"), (102, "IC-000102", "测试包材B")):
        m.execute(
            """INSERT INTO canonical_items
               (id, canonical_sku, name, category_code, unit, gram_per_unit, aux_unit,
                aux_rate, status, created_from, created_at, updated_at)
               VALUES (?, ?, ?, 'PACKAGING', '件', 0, NULL, 0, 'active', 'rd_manual', ?, ?)""",
            (cid, sku, name, ts, ts),
        )
    m.commit()

    dc = sqlite3.connect(str(a_path))
    cat_id = dc.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()[0]
    for cid in (101, 102):
        dc.execute(
            "INSERT INTO items (sku, name, category_id, quantity, unit, canonical_id, updated_at)"
            " VALUES (?, ?, ?, ?, '件', ?, ?)",
            (f"A-{cid}", f"品项{cid}", cat_id, 100.0, cid, ts),
        )
    dc.commit()
    dc.close()

    store = sqlite3.connect(str(store_path))
    store.row_factory = sqlite3.Row
    cat_id = store.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()["id"]
    store.execute(
        "INSERT INTO items (sku, name, category_id, quantity, unit, canonical_id, updated_at)"
        " VALUES ('ST-101', '品项101', ?, 0.0, '件', 101, ?)",
        (cat_id, ts),
    )
    store.commit()
    store.close()

    seq = {"n": 0}

    def make_order(lines, status="shipped", head="A"):
        """lines: list of (canonical_id, quantity, shipped_quantity)."""
        seq["n"] += 1
        cur = m.execute(
            """INSERT INTO store_orders
               (order_no, store_warehouse_code, dc_warehouse_code, status,
                requested_by, created_at, updated_at)
               VALUES (?, 'store_test', ?, ?, 1, ?, ?)""",
            (f"SO-RCV-{seq['n']:04d}", head, status, TS, TS),
        )
        order_id = int(cur.lastrowid)
        for cid, qty, shipped in lines:
            m.execute(
                """INSERT INTO store_order_items
                   (order_id, canonical_id, quantity, shipped_quantity, unit,
                    source_warehouse_code, created_at)
                   VALUES (?, ?, ?, ?, '件', ?, ?)""",
                (order_id, cid, qty, shipped, head, TS),
            )
        m.commit()
        ids = [
            int(r["id"]) for r in m.execute(
                "SELECT id FROM store_order_items WHERE order_id=? ORDER BY id",
                (order_id,),
            ).fetchall()
        ]
        return order_id, ids

    def store_stock(canonical_id):
        conn = sqlite3.connect(str(store_path))
        row = conn.execute(
            "SELECT COALESCE(SUM(quantity), 0) FROM items WHERE canonical_id=?",
            (canonical_id,),
        ).fetchone()
        conn.close()
        return float(row[0])

    def receipts(order_id):
        return m.execute(
            "SELECT * FROM store_order_receipts WHERE order_id=? ORDER BY id",
            (order_id,),
        ).fetchall()

    def status_history(order_id, to_status):
        return m.execute(
            "SELECT COUNT(*) FROM store_order_status_history"
            " WHERE order_id=? AND to_status=?",
            (order_id, to_status),
        ).fetchone()[0]

    class FailingFactory:
        """Fails the first store connection, then delegates to the real one."""

        def __init__(self):
            self.calls = 0

        def __call__(self, code):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("injected store failure")
            return sop.open_warehouse_db(code)

    yield {
        "master": m,
        "master_path": master_path,
        "wh_dir": wh_dir,
        "store_path": store_path,
        "make_order": make_order,
        "store_stock": store_stock,
        "receipts": receipts,
        "status_history": status_history,
        "FailingFactory": FailingFactory,
    }
    m.close()


def _receive(env, order_id, op_no, item_qty, received_by=2):
    return sop.receive_partial(
        env["master"],
        sop.open_warehouse_db,
        order_id=order_id,
        op_no=op_no,
        item_qty=item_qty,
        received_by=received_by,
    )


# ---------------------------------------------------------------------------
# (a) normal receive
# ---------------------------------------------------------------------------

def test_receive_partial_accumulates_store_and_fulfilled(receipt_env):
    env = receipt_env
    order_id, ids = env["make_order"]([(101, 5.0, 5.0)])

    result = _receive(env, order_id, "op-r1", {ids[0]: 2.0})

    assert result["idempotent"] is False
    assert result["new_order_status"] == sop.ORDER_STATUS_SHIPPED
    assert result["is_fully_received"] is False
    assert env["store_stock"](101) == 2.0

    rows = env["receipts"](order_id)
    assert len(rows) == 1
    assert rows[0]["op_no"] == f"op-r1:{ids[0]}"
    assert rows[0]["applied"] == 1
    assert rows[0]["quantity"] == 2.0

    with sop.open_warehouse_db("store_test") as store:
        movement = store.execute(
            "SELECT delta, op_no FROM stock_movements ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert movement["delta"] == 2.0
        assert movement["op_no"] == f"op-r1:{ids[0]}"

    item = env["master"].execute(
        "SELECT fulfilled_quantity, status FROM store_order_items WHERE id=?",
        (ids[0],),
    ).fetchone()
    assert item["fulfilled_quantity"] == 2.0
    assert item["status"] == sop.ORDER_ITEM_STATUS_PARTIAL


# ---------------------------------------------------------------------------
# (b) same op_no retry only stocks once
# ---------------------------------------------------------------------------

def test_same_op_no_retry_stocks_once(receipt_env):
    env = receipt_env
    order_id, ids = env["make_order"]([(101, 5.0, 5.0)])

    first = _receive(env, order_id, "op-retry", {ids[0]: 2.0})
    retry = _receive(env, order_id, "op-retry", {ids[0]: 2.0})

    assert retry["idempotent"] is True
    assert retry["receipts"][0]["receipt_id"] == first["receipts"][0]["receipt_id"]
    assert env["store_stock"](101) == 2.0
    assert len(env["receipts"](order_id)) == 1
    item = env["master"].execute(
        "SELECT fulfilled_quantity FROM store_order_items WHERE id=?", (ids[0],)
    ).fetchone()
    assert item["fulfilled_quantity"] == 2.0
    with sop.open_warehouse_db("store_test") as store:
        assert store.execute(
            "SELECT COUNT(*) FROM stock_movements WHERE op_no=?",
            (f"op-retry:{ids[0]}",),
        ).fetchone()[0] == 1


def test_same_op_no_retry_with_different_quantity_rejected(receipt_env):
    env = receipt_env
    order_id, ids = env["make_order"]([(101, 5.0, 5.0)])
    _receive(env, order_id, "op-fixed", {ids[0]: 2.0})

    with pytest.raises(ValueError, match="不同数量"):
        _receive(env, order_id, "op-fixed", {ids[0]: 3.0})

    assert env["store_stock"](101) == 2.0


# ---------------------------------------------------------------------------
# (c) store failure after the master receipt landed -> replay-safe
# ---------------------------------------------------------------------------

def test_store_failure_after_receipt_leaves_replayable_state(receipt_env):
    env = receipt_env
    order_id, ids = env["make_order"]([(101, 5.0, 5.0)])

    with pytest.raises(RuntimeError, match="injected store failure"):
        sop.receive_partial(
            env["master"],
            env["FailingFactory"](),
            order_id=order_id,
            op_no="op-crash",
            item_qty={ids[0]: 2.0},
            received_by=2,
        )

    # Master receipt committed unapplied; stock untouched; line already reserved.
    rows = env["receipts"](order_id)
    assert len(rows) == 1 and rows[0]["applied"] == 0
    assert env["store_stock"](101) == 0.0
    item = env["master"].execute(
        "SELECT fulfilled_quantity FROM store_order_items WHERE id=?", (ids[0],)
    ).fetchone()
    assert item["fulfilled_quantity"] == 2.0

    # The client retries with the same page-level op_no.
    replay = _receive(env, order_id, "op-crash", {ids[0]: 2.0})

    assert replay["idempotent"] is False
    assert env["store_stock"](101) == 2.0
    rows = env["receipts"](order_id)
    assert len(rows) == 1 and rows[0]["applied"] == 1
    item = env["master"].execute(
        "SELECT fulfilled_quantity FROM store_order_items WHERE id=?", (ids[0],)
    ).fetchone()
    assert item["fulfilled_quantity"] == 2.0
    with sop.open_warehouse_db("store_test") as store:
        assert store.execute(
            "SELECT COUNT(*) FROM stock_movements WHERE op_no=?",
            (f"op-crash:{ids[0]}",),
        ).fetchone()[0] == 1


def test_reconcile_orphan_receipts_completes_unapplied_receipt(receipt_env):
    env = receipt_env
    order_id, ids = env["make_order"]([(101, 5.0, 5.0)])
    with pytest.raises(RuntimeError):
        sop.receive_partial(
            env["master"],
            env["FailingFactory"](),
            order_id=order_id,
            op_no="op-recon",
            item_qty={ids[0]: 2.0},
            received_by=2,
        )

    repaired = sop.reconcile_orphan_receipts(env["master"], sop.open_warehouse_db)

    assert len(repaired) == 1
    assert repaired[0]["op_no"] == f"op-recon:{ids[0]}"
    assert repaired[0]["warehouse_code"] == "store_test"
    assert repaired[0]["quantity"] == 2.0
    assert env["store_stock"](101) == 2.0
    assert env["receipts"](order_id)[0]["applied"] == 1
    assert sop.reconcile_orphan_receipts(env["master"], sop.open_warehouse_db) == []


# ---------------------------------------------------------------------------
# (d) over-shipped quantity rejected
# ---------------------------------------------------------------------------

def test_receive_beyond_shipped_rejected(receipt_env):
    env = receipt_env
    order_id, ids = env["make_order"]([(101, 100.0, 2.0)], status="approved")

    with pytest.raises(ValueError, match="超过待收"):
        _receive(env, order_id, "op-over-1", {ids[0]: 100.0})
    with pytest.raises(ValueError, match="超过待收"):
        _receive(env, order_id, "op-over-2", {ids[0]: 3.0})

    assert env["receipts"](order_id) == []
    assert env["store_stock"](101) == 0.0

    ok = _receive(env, order_id, "op-over-3", {ids[0]: 2.0})
    assert ok["is_fully_received"] is False
    assert env["store_stock"](101) == 2.0


# ---------------------------------------------------------------------------
# (e) approved orders keep per-line receive ability
# ---------------------------------------------------------------------------

def test_approved_order_receives_shipped_quantity(receipt_env):
    env = receipt_env
    order_id, ids = env["make_order"]([(101, 30.0, 5.0)], status="approved")

    result = _receive(env, order_id, "op-approved", {ids[0]: 5.0})

    assert result["new_order_status"] == sop.ORDER_STATUS_APPROVED
    assert result["is_fully_received"] is False
    assert env["status_history"](order_id, "delivered") == 0
    item = env["master"].execute(
        "SELECT fulfilled_quantity, status FROM store_order_items WHERE id=?",
        (ids[0],),
    ).fetchone()
    assert item["fulfilled_quantity"] == 5.0
    assert item["status"] == sop.ORDER_ITEM_STATUS_PARTIAL


# ---------------------------------------------------------------------------
# (f) cancel racing a committed shipment loses
# ---------------------------------------------------------------------------

def test_cancel_after_ship_commit_rejected_order_stays_approved(receipt_env):
    env = receipt_env
    order_id, ids = env["make_order"]([(101, 5.0, 0.0)], status="approved")

    # Ship 2 of 5: the order stays approved but stock has left the DC.
    sop.ship_partial(
        env["master"],
        sop.open_warehouse_db,
        order_id=order_id,
        operator_dc_code="A",
        op_no="op-ship-first",
        line_qty={ids[0]: 2.0},
        shipped_by=3,
    )

    with pytest.raises(ValueError, match="已有发货记录"):
        sop.cancel_order(env["master"], order_id, cancelled_by=3, reason="race")

    order = sop.get_order_detail(env["master"], order_id)
    assert order["status"] == sop.ORDER_STATUS_APPROVED
    assert env["status_history"](order_id, "cancelled") == 0


def test_cancel_conditional_update_loses_when_status_moved(receipt_env, monkeypatch):
    """The UPDATE … WHERE status IN (pending, approved) is the race gate.

    The route snapshot still says ``approved`` (injected), but the row has
    already moved to ``shipped``; ``changes()==0`` must abort the cancel.
    """
    env = receipt_env
    order_id, ids = env["make_order"]([(101, 5.0, 0.0)], status="approved")
    env["master"].execute(
        "UPDATE store_orders SET status='shipped' WHERE id=?", (order_id,)
    )
    env["master"].commit()

    real_detail = sop.get_order_detail

    def stale_snapshot(conn, oid):
        detail = real_detail(conn, oid)
        if oid == order_id and detail is not None:
            detail["status"] = sop.ORDER_STATUS_APPROVED
        return detail

    monkeypatch.setattr(sop, "get_order_detail", stale_snapshot)

    with pytest.raises(ValueError, match="状态已变更"):
        sop.cancel_order(env["master"], order_id, cancelled_by=3, reason="race")

    assert env["master"].execute(
        "SELECT status FROM store_orders WHERE id=?", (order_id,)
    ).fetchone()["status"] == "shipped"


# ---------------------------------------------------------------------------
# (g) receive-all -> delivered exactly once
# ---------------------------------------------------------------------------

def test_receive_all_transitions_delivered_once(receipt_env):
    env = receipt_env
    order_id, ids = env["make_order"]([(101, 5.0, 5.0), (102, 3.0, 3.0)])

    first = _receive(env, order_id, "op-line-a", {ids[0]: 5.0})
    assert first["is_fully_received"] is False
    assert first["transitioned_to_delivered"] is False

    second = _receive(env, order_id, "op-line-b", {ids[1]: 3.0})
    assert second["is_fully_received"] is True
    assert second["transitioned_to_delivered"] is True

    order = sop.get_order_detail(env["master"], order_id)
    assert order["status"] == sop.ORDER_STATUS_DELIVERED
    assert order["delivered_at"] is not None
    assert env["status_history"](order_id, "delivered") == 1

    # Replaying the final receipt never re-transitions nor re-writes history.
    retry = _receive(env, order_id, "op-line-b", {ids[1]: 3.0})
    assert retry["idempotent"] is True
    assert retry["transitioned_to_delivered"] is False
    assert env["status_history"](order_id, "delivered") == 1
    assert env["store_stock"](102) == 3.0
