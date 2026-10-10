"""Route tests for distribution-center store-ordering flows."""
from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from db import init_master_db, init_warehouse_db


@pytest.fixture
def dc_env(tmp_path, monkeypatch):
    """DC manager (user 2) + store staff (user 1) environment with one submitted order."""
    import config as config_module
    import db as db_module

    master_path = tmp_path / "master.db"
    wh_dir = tmp_path / "warehouses"
    wh_dir.mkdir()
    dc_path = wh_dir / "dc_test.db"
    dc2_path = wh_dir / "dc2_test.db"
    store_path = wh_dir / "store_test.db"

    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_dir)
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", wh_dir)

    init_master_db()
    init_warehouse_db(dc_path)
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
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (2, 'dc_mgr', 'x', 0, ?)",
        (ts,),
    )
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (3, 'dc_staff', 'x', 0, ?)",
        (ts,),
    )
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (4, 'dc2_mgr', 'x', 0, ?)",
        (ts,),
    )
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (5, 'admin', 'x', 1, ?)",
        (ts,),
    )
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) VALUES (1, 'dc_test', '测试配送中心', ?, 'distribution_center', ?)",
        (str(dc_path), ts),
    )
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) VALUES (4, 'dc2_test', '测试配送中心2', ?, 'distribution_center', ?)",
        (str(dc2_path), ts),
    )
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) VALUES (2, 'store_test', '测试门店', ?, 'storefront', ?)",
        (str(store_path), ts),
    )
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (1, 2, 'staff')")
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (2, 1, 'manager')")
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (3, 1, 'staff')")
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (4, 4, 'manager')")
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

    dc2 = sqlite3.connect(str(dc2_path))
    dc2.row_factory = sqlite3.Row
    cat_id = dc2.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()["id"]
    dc2.execute(
        "INSERT INTO items (sku, name, category_id, quantity, unit, canonical_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("DC2-A", "测试包材A", cat_id, 100.0, "件", 101, ts),
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
    store.commit()
    store.close()

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    # Create an order as store user.
    with client.session_transaction() as s:
        s["user_id"] = 1
        s["warehouse_id"] = 2
    client.post(
        "/store-ordering/cart/add",
        data={"dc": "dc_test", "canonical_id": 101, "quantity": "5", "unit": "件"},
    )
    client.post(
        "/store-ordering/cart/submit",
        data={"expected_delivery_date": datetime.now().strftime("%Y-%m-%d"), "note": ""},
    )
    # Grab order id.
    master = sqlite3.connect(str(master_path))
    master.row_factory = sqlite3.Row
    order_id = master.execute("SELECT id FROM store_orders ORDER BY id DESC LIMIT 1").fetchone()["id"]
    master.close()

    yield {
        "app": app,
        "client": client,
        "master_path": master_path,
        "dc_path": dc_path,
        "dc2_path": dc2_path,
        "store_path": store_path,
        "order_id": order_id,
    }


def _login_as(client, user_id, warehouse_id):
    with client.session_transaction() as s:
        s["user_id"] = user_id
        s["warehouse_id"] = warehouse_id


def test_review_list(dc_env):
    client = dc_env["client"]
    _login_as(client, 2, 1)
    resp = client.get("/store-ordering/review")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "SO-" in body


def test_review_approve(dc_env):
    client = dc_env["client"]
    _login_as(client, 2, 1)
    resp = client.post(
        f"/store-ordering/orders/{dc_env['order_id']}/review",
        data={"decision": "approved", "note": "ok"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    master = sqlite3.connect(str(dc_env["master_path"]))
    master.row_factory = sqlite3.Row
    status = master.execute("SELECT status FROM store_orders WHERE id=?", (dc_env["order_id"],)).fetchone()["status"]
    master.close()
    assert status == "approved"


def test_review_reject_requires_note(dc_env):
    client = dc_env["client"]
    _login_as(client, 2, 1)
    resp = client.post(
        f"/store-ordering/orders/{dc_env['order_id']}/review",
        data={"decision": "rejected"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "必须填写原因" in body


def test_shipment_list_and_ship(dc_env):
    client = dc_env["client"]
    # Approve as manager
    _login_as(client, 2, 1)
    client.post(
        f"/store-ordering/orders/{dc_env['order_id']}/review",
        data={"decision": "approved", "note": "ok"},
    )
    # Ship as staff — DC operations (review + ship) live on the unified
    # /review page; /shipments is now a legacy alias that redirects.
    _login_as(client, 3, 1)
    resp = client.get("/store-ordering/shipments")
    assert resp.status_code == 302
    assert "/store-ordering/review" in resp.headers.get("Location", "")

    # The combined review page lists approved orders under
    # 「已审批 · 待出库」.
    resp = client.get("/store-ordering/review")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "已审批 · 待出库" in body
    assert "SO-" in body

    resp = client.post(
        f"/store-ordering/orders/{dc_env['order_id']}/ship",
        data={"tracking_note": "快递"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    master = sqlite3.connect(str(dc_env["master_path"]))
    master.row_factory = sqlite3.Row
    status = master.execute("SELECT status FROM store_orders WHERE id=?", (dc_env["order_id"],)).fetchone()["status"]
    master.close()
    assert status == "shipped"


def test_ship_insufficient_stock_rejected(dc_env):
    """2026-10-10 决策反转：DC 发货不得超过现有库存（原 A7 欠货出库已废止）。

    路由层 /ship 应 flash「出库失败：库存不足…」；订单停在 approved；
    DC 库存不被扣减、不跌负。
    """
    client = dc_env["client"]
    # Approve as manager
    _login_as(client, 2, 1)
    client.post(
        f"/store-ordering/orders/{dc_env['order_id']}/review",
        data={"decision": "approved", "note": "ok"},
    )
    # Deplete DC stock to 1 while the order needs more (canonical 101).
    dc = sqlite3.connect(str(dc_env["dc_path"]))
    dc.execute("UPDATE items SET quantity=? WHERE canonical_id=?", (1.0, 101))
    dc.commit()
    dc.close()
    # Try ship as staff — 必须被拒。
    _login_as(client, 3, 1)
    resp = client.post(
        f"/store-ordering/orders/{dc_env['order_id']}/ship",
        data={"tracking_note": ""},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "库存不足" in body
    assert "出库失败" in body
    # 不应出现「出库成功」。
    assert "出库成功" not in body

    # 订单仍在 approved。
    master = sqlite3.connect(str(dc_env["master_path"]))
    master.row_factory = sqlite3.Row
    status = master.execute("SELECT status FROM store_orders WHERE id=?", (dc_env["order_id"],)).fetchone()["status"]
    master.close()
    assert status == "approved"

    # DC 库存未被扣减（仍为 1，不为负）。
    dc = sqlite3.connect(str(dc_env["dc_path"]))
    dc.row_factory = sqlite3.Row
    qty = dc.execute("SELECT quantity FROM items WHERE canonical_id=101").fetchone()["quantity"]
    assert qty == 1.0
    dc.close()


def test_dc_staff_can_view_review_but_cannot_approve(dc_env):
    """Combined review page is read-only for DC staff — they see the lists but
    cannot act. The inline approve/reject forms live on order_detail and
    are guarded by can_review (manager+ only).
    """
    client = dc_env["client"]
    _login_as(client, 3, 1)
    # Page is reachable.
    resp = client.get("/store-ordering/review")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "订货审批" in body
    # But POST /review must fail (only managers can decide).
    post_resp = client.post(
        f"/store-ordering/orders/{dc_env['order_id']}/review",
        data={"decision": "approved"},
    )
    assert post_resp.status_code == 403


def test_review_and_ship_other_dc_order_forbidden(dc_env):
    """DC manager of dc_test must not review/ship an order bound to dc2_test."""
    client = dc_env["client"]
    # Create an order bound to dc2_test as store user.
    _login_as(client, 1, 2)
    client.post(
        "/store-ordering/cart/add",
        data={"dc": "dc2_test", "canonical_id": 101, "quantity": "3", "unit": "件"},
    )
    client.post(
        "/store-ordering/cart/submit",
        data={"expected_delivery_date": datetime.now().strftime("%Y-%m-%d"), "note": ""},
    )
    master = sqlite3.connect(str(dc_env["master_path"]))
    master.row_factory = sqlite3.Row
    order_id = master.execute("SELECT id FROM store_orders WHERE dc_warehouse_code='dc2_test'").fetchone()["id"]
    master.close()

    # dc_test manager attempts review.
    _login_as(client, 2, 1)
    resp = client.post(
        f"/store-ordering/orders/{order_id}/review",
        data={"decision": "approved", "note": "ok"},
    )
    assert resp.status_code == 403

    # dc_test staff attempts ship.
    _login_as(client, 3, 1)
    resp = client.post(
        f"/store-ordering/orders/{order_id}/ship",
        data={"tracking_note": ""},
    )
    assert resp.status_code == 403


def test_admin_can_review_and_ship_any_dc_order(dc_env):
    """Platform admin logged into a storefront can still review/ship DC orders."""
    client = dc_env["client"]
    order_id = dc_env["order_id"]

    # Admin logged into storefront reviews an order bound to dc_test.
    _login_as(client, 5, 2)
    resp = client.post(
        f"/store-ordering/orders/{order_id}/review",
        data={"decision": "approved", "note": "admin ok"},
    )
    assert resp.status_code == 302

    master = sqlite3.connect(str(dc_env["master_path"]))
    master.row_factory = sqlite3.Row
    status = master.execute("SELECT status FROM store_orders WHERE id=?", (order_id,)).fetchone()["status"]
    master.close()
    assert status == "approved"

    # Admin ships the order as well.
    _login_as(client, 5, 2)
    resp = client.post(
        f"/store-ordering/orders/{order_id}/ship",
        data={"tracking_note": "admin ship"},
    )
    assert resp.status_code == 302

    master = sqlite3.connect(str(dc_env["master_path"]))
    master.row_factory = sqlite3.Row
    status = master.execute("SELECT status FROM store_orders WHERE id=?", (order_id,)).fetchone()["status"]
    master.close()
    assert status == "shipped"
