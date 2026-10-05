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


# =========================================================================
# T09 — e2e tests covering v2 incremental PRD §F (14-step user journey)
# =========================================================================

def _make_dc_item_pure(conn, dc_path, sku, name, qty, cat_id, canonical_id, ts):
    """Helper to insert a DC item with selling_price + unit_cost so price tests
    can assert amount previews."""
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "INSERT INTO items (sku, name, category_id, quantity, unit, "
        "selling_price, unit_cost, canonical_id, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (sku, name, cat_id, qty, "件", 5.50, 4.00, canonical_id, ts),
    )
    return int(cur.lastrowid)


def test_catalog_renders_chinese_categories_and_cards(e2e_env):
    """F1: catalog page shows Chinese category names, inv-card layout, prices."""
    client = e2e_env["client"]
    _login_as(client, 1, 3)
    resp = client.get("/store-ordering/catalog?dc=dc1_test")
    assert resp.status_code == 200
    body = resp.data.decode()

    # F1: Chinese category name from canonical_categories.
    assert "包材" in body
    # F1: chips show Chinese name (not the code); the code may still appear in
    # data-cat attributes used by JS, so we only assert it's not in the chip
    # rendered markup (chip <a> inner text).
    import re
    chip_re = re.compile(r'class="cat-chip[^"]*"[^>]*>([^<]+)</a>')
    chips = chip_re.findall(body)
    assert any("包材" in c for c in chips), chips
    assert not any("PACKAGING" in c for c in chips), chips

    # F2: each card has inv-card / inv-cat / inv-name / status-pill / 单价.
    assert "inv-card" in body
    assert "inv-cat" in body
    assert "inv-name" in body
    assert "status-pill" in body
    assert "单价" in body

    # F3: modal scaffold present (overlay / pill-group / amount hint).
    assert "modal-overlay" in body
    assert "pill-group" in body
    assert "cm-amount" in body

    # F4/F5: hidden batch form fields ship per-card.
    assert 'name="selected[]"' in body
    assert 'name="qty[]"' in body
    assert 'name="unit[]"' in body
    assert "加入购物车" in body


def test_cart_view_renders_total_and_subtotals(e2e_env):
    """F6: cart page shows subtotals + cart total."""
    client = e2e_env["client"]
    _login_as(client, 1, 3)
    client.post(
        "/store-ordering/cart/add",
        data={"dc": "dc1_test", "canonical_id": 101, "quantity": "3", "unit": "件"},
    )
    resp = client.get("/store-ordering/cart")
    assert resp.status_code == 200
    body = resp.data.decode()
    # Cart total label + price column visible.
    assert "购物车总金额" in body
    assert "小计" in body
    assert "单价" in body
    assert "发送" in body
    assert "继续购物" in body
    # Cart total cell is present (the fixture leaves prices at 0 so the value
    # is ¥ 0.00 — we just assert the field is rendered as currency).
    import re
    total_re = re.compile(r"<th[^>]*colspan=\"2\"[^>]*>¥ [^<]+</th>")
    assert total_re.search(body), "cart total cell not rendered"


def test_submit_button_text_is_send(e2e_env):
    """F7: submit page button label is 发送 (no 去提交订单)."""
    client = e2e_env["client"]
    _login_as(client, 1, 3)
    client.post(
        "/store-ordering/cart/add",
        data={"dc": "dc1_test", "canonical_id": 101, "quantity": "1", "unit": "件"},
    )
    resp = client.get("/store-ordering/cart/submit")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "发送" in body
    assert "去提交订单" not in body


def test_cart_add_batch_with_multiple_items(e2e_env):
    """F5: POST /cart/add-batch accepts parallel selected[]/qty[]/unit[] and
    creates one cart row per valid (qty > 0) entry. Empty qty skipped."""
    client = e2e_env["client"]
    _login_as(client, 1, 3)
    # Add canonical 102 first via single endpoint (unbound won't block cart add).
    client.post(
        "/store-ordering/cart/add",
        data={"dc": "dc1_test", "canonical_id": 101, "quantity": "5", "unit": "件"},
    )
    # Now exercise the batch route.
    resp = client.post(
        "/store-ordering/cart/add-batch",
        data={
            "dc": "dc1_test",
            "selected[]": ["101", "102"],
            "qty[]": ["7", "0"],   # second one is zero → skipped
            "unit[]": ["base", "base"],
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    body = resp.data.decode()
    # 7 added to 101 (was 5 → 12); 102 not added.
    master = sqlite3.connect(str(e2e_env["master_path"]))
    master.row_factory = sqlite3.Row
    rows = master.execute(
        """SELECT canonical_id, quantity FROM store_order_cart_items
           ORDER BY canonical_id"""
    ).fetchall()
    by_canon = {r["canonical_id"]: r["quantity"] for r in rows}
    assert by_canon.get(101) == 12.0
    assert 102 not in by_canon
    master.close()
    assert "已加入购物车" in body


def test_full_user_journey_select_to_delivered(e2e_env):
    """F8-F14: complete user journey — review, ship, partial receive, full receive,
    delivered. Verifies notifications and stock movements on both sides."""
    client = e2e_env["client"]
    master_path = e2e_env["master_path"]
    dc_path = e2e_env["dc1_path"]
    store_path = e2e_env["store_path"]

    # Store staff: pick DC, add 10 of item 101.
    _login_as(client, 1, 3)
    client.get("/store-ordering/catalog?dc=dc1_test")  # F1: catalog renders
    client.post(
        "/store-ordering/cart/add",
        data={"dc": "dc1_test", "canonical_id": 101, "quantity": "10", "unit": "件"},
    )
    # Send → submit order.
    tomorrow = (datetime.now().date() + timedelta(days=1)).isoformat()
    client.post(
        "/store-ordering/cart/submit",
        data={"expected_delivery_date": tomorrow, "note": "完整流程"},
    )

    master = sqlite3.connect(str(master_path))
    master.row_factory = sqlite3.Row
    order_id = master.execute(
        "SELECT id FROM store_orders ORDER BY id DESC LIMIT 1"
    ).fetchone()["id"]
    order_no = master.execute(
        "SELECT order_no FROM store_orders WHERE id=?", (order_id,)
    ).fetchone()["order_no"]
    order_item_id = master.execute(
        "SELECT id FROM store_order_items WHERE order_id=?", (order_id,)
    ).fetchone()["id"]
    master.close()

    # F8: DC admin sees pending.
    _login_as(client, 3, 1)  # dc_mgr
    resp = client.get("/store-ordering/review")
    assert resp.status_code == 200
    assert order_no in resp.data.decode()

    # F9: approve.
    client.post(
        f"/store-ordering/orders/{order_id}/review",
        data={"decision": "approved", "note": "ok"},
    )
    master = sqlite3.connect(str(master_path))
    master.row_factory = sqlite3.Row
    assert master.execute(
        "SELECT status FROM store_orders WHERE id=?", (order_id,)
    ).fetchone()["status"] == "approved"
    master.close()

    # F10: ship.
    _login_as(client, 3, 1)
    client.post(
        f"/store-ordering/orders/{order_id}/ship",
        data={"tracking_note": "顺丰"},
    )
    master = sqlite3.connect(str(master_path))
    master.row_factory = sqlite3.Row
    assert master.execute(
        "SELECT status FROM store_orders WHERE id=?", (order_id,)
    ).fetchone()["status"] == "shipped"
    master.close()
    dc = sqlite3.connect(str(dc_path))
    dc.row_factory = sqlite3.Row
    dc_qty = dc.execute(
        "SELECT quantity FROM items WHERE canonical_id=101"
    ).fetchone()["quantity"]
    assert dc_qty == 90.0  # 100 - 10
    dc_ship_mv = dc.execute(
        "SELECT action, delta, note FROM stock_movements WHERE action=?", ("门店订货出库",)
    ).fetchone()
    assert dc_ship_mv["delta"] == -10.0
    assert order_no in dc_ship_mv["note"]
    dc.close()

    # F11: store manager receives 1 (partial).
    _login_as(client, 2, 3)  # store_mgr
    client.post(
        f"/store-ordering/orders/{order_id}/receive",
        data={"order_item_id": order_item_id, "quantity": "1", "note": "首收"},
    )
    store = sqlite3.connect(str(store_path))
    store.row_factory = sqlite3.Row
    assert store.execute(
        "SELECT quantity FROM items WHERE canonical_id=101"
    ).fetchone()["quantity"] == 1.0
    master = sqlite3.connect(str(master_path))
    master.row_factory = sqlite3.Row
    item = master.execute(
        "SELECT fulfilled_quantity, status FROM store_order_items WHERE id=?",
        (order_item_id,),
    ).fetchone()
    assert item["fulfilled_quantity"] == 1.0
    assert item["status"] == "partial"
    # Order still shipped, no delivered notification.
    assert master.execute(
        "SELECT status FROM store_orders WHERE id=?", (order_id,)
    ).fetchone()["status"] == "shipped"
    before = master.execute(
        "SELECT COUNT(*) AS c FROM notifications WHERE event_type=?",
        ("store_order_delivered",),
    ).fetchone()["c"]
    master.close()
    store.close()

    # F12: receive remaining 9 → delivered + notification.
    _login_as(client, 2, 3)
    client.post(
        f"/store-ordering/orders/{order_id}/receive",
        data={"order_item_id": order_item_id, "quantity": "9", "note": "余货"},
    )
    store = sqlite3.connect(str(store_path))
    store.row_factory = sqlite3.Row
    assert store.execute(
        "SELECT quantity FROM items WHERE canonical_id=101"
    ).fetchone()["quantity"] == 10.0
    inbound = store.execute(
        "SELECT action, delta FROM stock_movements WHERE action=?", ("门店订货入库",)
    ).fetchall()
    total_in = sum(r["delta"] for r in inbound)
    assert total_in == 10.0
    store.close()

    master = sqlite3.connect(str(master_path))
    master.row_factory = sqlite3.Row
    assert master.execute(
        "SELECT status FROM store_orders WHERE id=?", (order_id,)
    ).fetchone()["status"] == "delivered"
    after = master.execute(
        "SELECT COUNT(*) AS c FROM notifications WHERE event_type=?",
        ("store_order_delivered",),
    ).fetchone()["c"]
    assert after > before  # F14

    # F14: all four event types have ≥1 notification row in this journey.
    counts = {}
    for et in (
        "store_order_submitted",
        "store_order_approved",
        "store_order_shipped",
        "store_order_delivered",
    ):
        counts[et] = master.execute(
            "SELECT COUNT(*) AS c FROM notifications WHERE event_type=?", (et,)
        ).fetchone()["c"]
    for et, c in counts.items():
        assert c >= 1, f"missing notifications for {et}"
    master.close()


def test_partial_received_fulfilled_quantity_accumulates(e2e_env):
    """F11 assertion: 多次部分收货 → fulfilled_quantity 累加 → final total."""
    client = e2e_env["client"]
    master_path = e2e_env["master_path"]
    _login_as(client, 1, 3)
    client.post(
        "/store-ordering/cart/add",
        data={"dc": "dc1_test", "canonical_id": 101, "quantity": "8", "unit": "件"},
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
    order_item_id = master.execute(
        "SELECT id FROM store_order_items WHERE order_id=?", (order_id,)
    ).fetchone()["id"]
    master.close()
    _login_as(client, 3, 1)
    client.post(
        f"/store-ordering/orders/{order_id}/review",
        data={"decision": "approved", "note": ""},
    )
    _login_as(client, 3, 1)
    client.post(f"/store-ordering/orders/{order_id}/ship", data={})

    _login_as(client, 2, 3)
    for q in ("2", "3", "3"):
        client.post(
            f"/store-ordering/orders/{order_id}/receive",
            data={"order_item_id": order_item_id, "quantity": q, "note": ""},
        )

    master = sqlite3.connect(str(master_path))
    master.row_factory = sqlite3.Row
    item = master.execute(
        "SELECT fulfilled_quantity, status FROM store_order_items WHERE id=?",
        (order_item_id,),
    ).fetchone()
    assert item["fulfilled_quantity"] == 8.0
    assert item["status"] == "fulfilled"
    assert master.execute(
        "SELECT status FROM store_orders WHERE id=?", (order_id,)
    ).fetchone()["status"] == "delivered"
    # 3 receipt rows.
    n = master.execute(
        "SELECT COUNT(*) AS c FROM store_order_receipts WHERE order_id=?",
        (order_id,),
    ).fetchone()["c"]
    assert n == 3
    master.close()
