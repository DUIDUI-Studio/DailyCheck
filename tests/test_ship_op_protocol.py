"""Task 5: cross-warehouse shipment commit protocol (spec v2 §7/§8).

Covers the eight scenarios in the implementation plan:
(a) normal ship, (b) same op_no retry deducts once, (c) foreign line rejected
without touching stock, (d) master batch failure -> orphan reconciliation with
a red reversal, (e) two same-warehouse orders race the last unit, (f) two
warehouses ship their last line -> exactly one ``shipped`` transition and one
notification, (g) legacy empty-form/``qty`` entries only act on the session
warehouse, (h) platform admin without an explicit DC operation warehouse is
rejected.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from blueprints import store_ordering_pure as sop
from db import init_master_db, init_warehouse_db

TS = "2026-10-29 10:00:00"


@pytest.fixture
def ship_env(tmp_path, monkeypatch):
    """Master + two DCs (A/B) + one storefront, with an order factory."""
    import config as config_module
    import db as db_module

    master_path = tmp_path / "master.db"
    wh_dir = tmp_path / "warehouses"
    wh_dir.mkdir()
    a_path = wh_dir / "A.db"
    b_path = wh_dir / "B.db"
    store_path = wh_dir / "store_test.db"

    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_dir)
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", wh_dir)

    init_master_db()
    for path in (a_path, b_path, store_path):
        init_warehouse_db(path)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    m = sqlite3.connect(str(master_path))
    m.row_factory = sqlite3.Row
    users = [
        (1, "store_staff", 0),
        (2, "dc_a_staff", 0),
        (3, "dc_b_staff", 0),
        (4, "platform_admin", 1),
    ]
    for uid, name, is_admin in users:
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
        " VALUES (2, 'B', '配送中心B', ?, 'distribution_center', ?)",
        (str(b_path), ts),
    )
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at)"
        " VALUES (3, 'store_test', '测试门店', ?, 'storefront', ?)",
        (str(store_path), ts),
    )
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (1, 3, 'staff')")
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (2, 1, 'staff')")
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (3, 2, 'staff')")
    m.execute(
        "INSERT INTO canonical_categories (code, name, description, created_at, updated_at)"
        " VALUES ('PACKAGING', '包材', '', ?, ?)",
        (ts, ts),
    )
    for cid, sku, name in ((101, "IC-000101", '测试包材A'), (102, "IC-000102", '测试包材B')):
        m.execute(
            """INSERT INTO canonical_items
               (id, canonical_sku, name, category_code, unit, gram_per_unit, aux_unit,
                aux_rate, status, created_from, created_at, updated_at)
               VALUES (?, ?, ?, 'PACKAGING', '件', 0, NULL, 0, 'active', 'rd_manual', ?, ?)""",
            (cid, sku, name, ts, ts),
        )
    m.commit()

    for code, path in (("A", a_path), ("B", b_path)):
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        cat_id = conn.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()["id"]
        for cid, qty in ((101, 10.0), (102, 10.0)):
            conn.execute(
                "INSERT INTO items (sku, name, category_id, quantity, unit, canonical_id, updated_at)"
                " VALUES (?, ?, ?, ?, '件', ?, ?)",
                (f"{code}-{cid}", f"品项{cid}", cat_id, qty, cid, ts),
            )
        conn.commit()
        conn.close()

    store = sqlite3.connect(str(store_path))
    store.row_factory = sqlite3.Row
    cat_id = store.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()["id"]
    store.execute(
        "INSERT INTO items (sku, name, category_id, quantity, unit, canonical_id, updated_at)"
        " VALUES ('ST-A', '品项101', ?, 0.0, '件', 101, ?)",
        (cat_id, ts),
    )
    store.commit()
    store.close()

    seq = {"n": 0}

    def create_order(lines, head="A", status="approved"):
        """lines: list of (canonical_id, quantity, source_warehouse_code)."""
        seq["n"] += 1
        order_no = f"SO-TEST-{seq['n']:04d}"
        cur = m.execute(
            """INSERT INTO store_orders
               (order_no, store_warehouse_code, dc_warehouse_code, status,
                requested_by, created_at, updated_at)
               VALUES (?, 'store_test', ?, ?, 1, ?, ?)""",
            (order_no, head, status, TS, TS),
        )
        order_id = int(cur.lastrowid)
        for cid, qty, source in lines:
            m.execute(
                """INSERT INTO store_order_items
                   (order_id, canonical_id, quantity, unit, source_warehouse_code, created_at)
                   VALUES (?, ?, ?, '件', ?, ?)""",
                (order_id, cid, qty, source, TS),
            )
        m.commit()
        ids = [
            int(r["id"]) for r in m.execute(
                "SELECT id FROM store_order_items WHERE order_id=? ORDER BY id", (order_id,)
            ).fetchall()
        ]
        return order_id, ids

    def set_stock(warehouse_code, canonical_id, qty):
        conn = sqlite3.connect(str(wh_dir / f"{warehouse_code}.db"))
        conn.execute("UPDATE items SET quantity=? WHERE canonical_id=?", (qty, canonical_id))
        conn.commit()
        conn.close()

    def stock(warehouse_code, canonical_id):
        conn = sqlite3.connect(str(wh_dir / f"{warehouse_code}.db"))
        qty = conn.execute(
            "SELECT quantity FROM items WHERE canonical_id=?", (canonical_id,)
        ).fetchone()[0]
        conn.close()
        return float(qty)

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    yield {
        "master": m,
        "master_path": master_path,
        "wh_dir": wh_dir,
        "a_path": a_path,
        "b_path": b_path,
        "store_path": store_path,
        "create_order": create_order,
        "set_stock": set_stock,
        "stock": stock,
        "client": client,
    }
    m.close()


def _ship(env, order_id, operator, op_no, line_qty, shipped_by=2):
    return sop.ship_partial(
        env["master"],
        sop.open_warehouse_db,
        order_id=order_id,
        operator_dc_code=operator,
        op_no=op_no,
        line_qty=line_qty,
        shipped_by=shipped_by,
    )


# ---------------------------------------------------------------------------
# (a) normal ship
# ---------------------------------------------------------------------------

def test_normal_ship_deducts_batch_and_accumulates(ship_env):
    env = ship_env
    order_id, ids = env["create_order"]([(101, 5.0, "A")])

    result = _ship(env, order_id, "A", "op-normal", {ids[0]: 2.0})

    assert result["delivery_id"]
    assert result["is_fully_shipped"] is False
    assert result["transitioned_to_shipped"] is False
    assert env["stock"]("A", 101) == 8.0

    order = sop.get_order_detail(env["master"], order_id)
    assert order["status"] == sop.ORDER_STATUS_APPROVED
    assert order["order_items"][0]["shipped_quantity"] == 2.0
    assert order["order_items"][0]["status"] == sop.ORDER_ITEM_STATUS_PARTIAL
    assert order["deliveries"][0]["warehouse_code"] == "A"
    rows = env["master"].execute(
        "SELECT op_no, quantity, item_id FROM store_order_delivery_items WHERE delivery_id=?",
        (result["delivery_id"],),
    ).fetchall()
    assert [r["op_no"] for r in rows] == [f"op-normal:{ids[0]}"]
    assert rows[0]["quantity"] == 2.0

    with sop.open_warehouse_db("A") as wh:
        movement = wh.execute(
            "SELECT delta, op_no FROM stock_movements ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert movement["delta"] == -2.0
        assert movement["op_no"] == f"op-normal:{ids[0]}"

    second = _ship(env, order_id, "A", "op-rest", {ids[0]: 3.0})
    assert second["is_fully_shipped"] is True
    assert second["transitioned_to_shipped"] is True
    assert env["stock"]("A", 101) == 5.0
    order = sop.get_order_detail(env["master"], order_id)
    assert order["status"] == sop.ORDER_STATUS_SHIPPED
    shipped_history = [
        h for h in order["status_history"]
        if h["from_status"] == "approved" and h["to_status"] == "shipped"
    ]
    assert len(shipped_history) == 1


# ---------------------------------------------------------------------------
# (b) same op_no retry only deducts once
# ---------------------------------------------------------------------------

def test_same_op_no_retry_deducts_once(ship_env):
    env = ship_env
    order_id, ids = env["create_order"]([(101, 5.0, "A")])

    first = _ship(env, order_id, "A", "op-retry", {ids[0]: 2.0})
    assert env["stock"]("A", 101) == 8.0

    retry = _ship(env, order_id, "A", "op-retry", {ids[0]: 2.0})

    assert retry["delivery_id"] == first["delivery_id"]
    assert retry["idempotent"] is True
    assert env["stock"]("A", 101) == 8.0
    count = env["master"].execute(
        "SELECT COUNT(*) FROM store_order_delivery_items WHERE delivery_id=?",
        (first["delivery_id"],),
    ).fetchone()[0]
    assert count == 1
    order = sop.get_order_detail(env["master"], order_id)
    assert order["order_items"][0]["shipped_quantity"] == 2.0


def test_multi_line_batch_uses_distinct_derived_keys_and_retry_hits_idempotency(ship_env):
    env = ship_env
    order_id, ids = env["create_order"]([(101, 5.0, "A"), (102, 5.0, "A")])

    first = _ship(env, order_id, "A", "op-multi", {ids[0]: 1.0, ids[1]: 2.0})
    keys = {
        r["op_no"] for r in env["master"].execute(
            "SELECT op_no FROM store_order_delivery_items WHERE delivery_id=?",
            (first["delivery_id"],),
        ).fetchall()
    }
    assert keys == {f"op-multi:{ids[0]}", f"op-multi:{ids[1]}"}
    assert env["stock"]("A", 101) == 9.0
    assert env["stock"]("A", 102) == 8.0

    retry = _ship(env, order_id, "A", "op-multi", {ids[0]: 1.0, ids[1]: 2.0})
    assert retry["delivery_id"] == first["delivery_id"]
    assert retry["idempotent"] is True
    assert env["stock"]("A", 101) == 9.0
    assert env["stock"]("A", 102) == 8.0


# ---------------------------------------------------------------------------
# (c) a foreign line ID is rejected before any stock changes
# ---------------------------------------------------------------------------

def test_foreign_line_id_rejected_without_stock_changes(ship_env):
    env = ship_env
    order_id, ids = env["create_order"]([(101, 5.0, "A"), (102, 5.0, "B")])

    with pytest.raises(ValueError, match="不属于"):
        _ship(env, order_id, "A", "op-mixed", {ids[0]: 1.0, ids[1]: 1.0})

    assert env["stock"]("A", 101) == 10.0
    assert env["stock"]("B", 102) == 10.0
    for code in ("A", "B"):
        with sop.open_warehouse_db(code) as wh:
            assert wh.execute(
                "SELECT COUNT(*) FROM stock_movements WHERE op_no IS NOT NULL"
            ).fetchone()[0] == 0
    assert env["master"].execute(
        "SELECT COUNT(*) FROM store_order_deliveries"
    ).fetchone()[0] == 0


# ---------------------------------------------------------------------------
# (d) master batch INSERT failure -> orphan reconciliation + red reversal
# ---------------------------------------------------------------------------

def test_master_batch_failure_leaves_orphan_reconciled_by_red_reversal(
    ship_env, monkeypatch,
):
    env = ship_env
    order_id, ids = env["create_order"]([(101, 5.0, "A")])

    def boom(_conn):
        raise RuntimeError("injected master batch failure")

    monkeypatch.setattr(sop, "_generate_delivery_no", boom)
    with pytest.raises(RuntimeError, match="injected"):
        _ship(env, order_id, "A", "op-orphan", {ids[0]: 2.0})

    # Warehouse committed; master rolled back.
    assert env["stock"]("A", 101) == 8.0
    assert env["master"].execute(
        "SELECT COUNT(*) FROM store_order_delivery_items"
    ).fetchone()[0] == 0

    orphans = sop.reconcile_orphan_shipments(env["master"], sop.open_warehouse_db)
    assert len(orphans) == 1
    assert orphans[0]["warehouse_code"] == "A"
    assert orphans[0]["op_no"] == f"op-orphan:{ids[0]}"
    assert orphans[0]["quantity"] == 2.0
    assert env["stock"]("A", 101) == 10.0
    with sop.open_warehouse_db("A") as wh:
        reversal = wh.execute(
            "SELECT delta, op_no FROM stock_movements WHERE op_no=?",
            (f"REV:op-orphan:{ids[0]}",),
        ).fetchone()
        assert reversal["delta"] == 2.0

    assert sop.reconcile_orphan_shipments(env["master"], sop.open_warehouse_db) == []


# ---------------------------------------------------------------------------
# (e) two same-warehouse orders compete for the last units
# ---------------------------------------------------------------------------

def test_two_orders_same_warehouse_race_last_stock_exactly_one_wins(ship_env):
    env = ship_env
    env["set_stock"]("A", 101, 5.0)
    order_a, ids_a = env["create_order"]([(101, 5.0, "A")])
    order_b, ids_b = env["create_order"]([(101, 5.0, "A")])

    _ship(env, order_a, "A", "op-winner", {ids_a[0]: 5.0})
    with pytest.raises(ValueError, match="库存不足"):
        _ship(env, order_b, "A", "op-loser", {ids_b[0]: 5.0})

    assert env["stock"]("A", 101) == 0.0
    assert sop.get_order_detail(env["master"], order_b)["order_items"][0][
        "shipped_quantity"
    ] == 0.0
    assert env["master"].execute(
        "SELECT COUNT(*) FROM store_order_deliveries"
    ).fetchone()[0] == 1


# ---------------------------------------------------------------------------
# (f) two warehouses ship their last line -> shipped exactly once
# ---------------------------------------------------------------------------

def test_two_warehouses_ship_last_lines_transition_once(ship_env):
    env = ship_env
    order_id, ids = env["create_order"]([(101, 5.0, "A"), (102, 5.0, "B")])

    first = _ship(env, order_id, "A", "op-a", {ids[0]: 5.0})
    assert first["is_fully_shipped"] is False
    assert sop.get_order_detail(env["master"], order_id)["status"] == "approved"

    second = _ship(env, order_id, "B", "op-b", {ids[1]: 5.0})
    assert second["is_fully_shipped"] is True
    assert second["transitioned_to_shipped"] is True

    # A retry of the losing warehouse's batch never re-transitions.
    retry = _ship(env, order_id, "B", "op-b", {ids[1]: 5.0})
    assert retry["idempotent"] is True
    assert retry["transitioned_to_shipped"] is False

    history = env["master"].execute(
        "SELECT COUNT(*) FROM store_order_status_history"
        " WHERE order_id=? AND to_status='shipped'",
        (order_id,),
    ).fetchone()[0]
    assert history == 1


def test_two_warehouses_route_ship_notifies_once(ship_env):
    env = ship_env
    order_id, ids = env["create_order"]([(101, 5.0, "A"), (102, 5.0, "B")])
    client = env["client"]

    def login(user_id, warehouse_id):
        with client.session_transaction() as s:
            s["user_id"] = user_id
            s["warehouse_id"] = warehouse_id

    login(2, 1)  # A staff
    resp = client.post(
        f"/store-ordering/orders/{order_id}/ship",
        data={"op_no": "route-op-a", f"shipped_items[{ids[0]}]": "5"},
    )
    assert resp.status_code == 302

    login(3, 2)  # B staff
    resp = client.post(
        f"/store-ordering/orders/{order_id}/ship",
        data={"op_no": "route-op-b", f"shipped_items[{ids[1]}]": "5"},
    )
    assert resp.status_code == 302

    assert sop.get_order_detail(env["master"], order_id)["status"] == "shipped"
    notifications = env["master"].execute(
        "SELECT COUNT(*) FROM notifications WHERE event_type=?",
        (sop.EVENT_ORDER_SHIPPED,),
    ).fetchone()[0]
    assert notifications == 1


# ---------------------------------------------------------------------------
# (g) legacy empty-form / qty entries only act on the session warehouse
# ---------------------------------------------------------------------------

def test_legacy_empty_form_only_ships_session_warehouse_lines(ship_env):
    env = ship_env
    order_id, ids = env["create_order"]([(101, 5.0, "A"), (102, 5.0, "B")])
    client = env["client"]
    with client.session_transaction() as s:
        s["user_id"] = 2
        s["warehouse_id"] = 1

    resp = client.post(f"/store-ordering/orders/{order_id}/ship", data={})
    assert resp.status_code == 302

    order = sop.get_order_detail(env["master"], order_id)
    by_item = {int(it["id"]): it for it in order["order_items"]}
    assert by_item[ids[0]]["shipped_quantity"] == 5.0
    assert by_item[ids[1]]["shipped_quantity"] == 0.0
    assert order["status"] == "approved"
    deliveries = env["master"].execute(
        "SELECT warehouse_code FROM store_order_deliveries WHERE order_id=?",
        (order_id,),
    ).fetchall()
    assert [d["warehouse_code"] for d in deliveries] == ["A"]


def test_legacy_qty_field_only_ships_session_warehouse_lines(ship_env):
    env = ship_env
    order_id, ids = env["create_order"]([(101, 5.0, "A"), (102, 5.0, "B")])
    client = env["client"]
    with client.session_transaction() as s:
        s["user_id"] = 2
        s["warehouse_id"] = 1

    resp = client.post(
        f"/store-ordering/orders/{order_id}/ship", data={"qty": "2"}
    )
    assert resp.status_code == 302

    order = sop.get_order_detail(env["master"], order_id)
    by_item = {int(it["id"]): it for it in order["order_items"]}
    assert by_item[ids[0]]["shipped_quantity"] == 2.0
    assert by_item[ids[1]]["shipped_quantity"] == 0.0


# ---------------------------------------------------------------------------
# (h) platform admin without an explicit DC operation warehouse is rejected
# ---------------------------------------------------------------------------

def test_admin_store_session_cannot_ship_without_operation_warehouse(ship_env):
    env = ship_env
    order_id, ids = env["create_order"]([(101, 5.0, "A")])
    client = env["client"]
    with client.session_transaction() as s:
        s["user_id"] = 4  # platform admin
        s["warehouse_id"] = 3  # storefront session

    resp = client.post(f"/store-ordering/orders/{order_id}/ship", data={})
    assert resp.status_code == 403

    assert env["stock"]("A", 101) == 10.0
    assert env["master"].execute(
        "SELECT COUNT(*) FROM store_order_deliveries"
    ).fetchone()[0] == 0
    assert sop.get_order_detail(env["master"], order_id)["status"] == "approved"
