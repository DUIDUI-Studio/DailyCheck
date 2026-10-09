"""T11 端到端冒烟：模拟 storefront → DC → storefront 三角色流。"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / ".venv/lib/python3.12/site-packages"))

from app import create_app  # noqa: E402

MASTER_DB = REPO / "db/master.db"


def _make_test_env():
    """创建临时 master.db + warehouse dbs（不影响 dev 数据）。"""
    import tempfile, os, shutil
    from db import init_master_db, init_warehouse_db

    tmp = Path(tempfile.mkdtemp(prefix="dc_e2e_t11_"))
    master_path = tmp / "master.db"
    wh_dir = tmp / "warehouses"
    wh_dir.mkdir()
    dc_path = wh_dir / "dc.db"
    store_path = wh_dir / "store.db"
    init_master_db()
    # monkey-patch
    import db as db_module
    import config as config_module
    db_module.MASTER_DB = master_path
    db_module.WAREHOUSE_DB_DIR = wh_dir
    config_module.MASTER_DB = master_path
    config_module.WAREHOUSE_DB_DIR = wh_dir
    init_master_db()
    init_warehouse_db(dc_path)
    init_warehouse_db(store_path)

    ts = "2026-10-05 16:30:00"
    m = sqlite3.connect(str(master_path))
    m.row_factory = sqlite3.Row
    m.execute("INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (1, 'store_mgr', 'x', 0, ?)", (ts,))
    m.execute("INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (2, 'dc_mgr', 'x', 0, ?)", (ts,))
    m.execute("INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (3, 'dc_staff', 'x', 0, ?)", (ts,))
    m.execute("INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) VALUES (1, 'dc', 'DC', ?, 'distribution_center', ?)", (str(dc_path), ts))
    m.execute("INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) VALUES (2, 'store', 'Store', ?, 'storefront', ?)", (str(store_path), ts))
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (1, 2, 'manager')")
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (2, 1, 'manager')")
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (3, 1, 'staff')")
    m.execute("INSERT INTO canonical_categories (code, name, description, created_at, updated_at) VALUES ('PACKAGING', '包材', '', ?, ?)", (ts, ts))
    m.execute("""INSERT INTO canonical_items
       (id, canonical_sku, name, category_code, unit, gram_per_unit, aux_unit, aux_rate,
        status, created_from, created_at, updated_at)
       VALUES (101, 'IC-000101', '测试包材', 'PACKAGING', '件', 0, NULL, 0, 'active', 'rd_manual', ?, ?)""", (ts, ts))
    m.commit()
    m.close()

    dc = sqlite3.connect(str(dc_path))
    dc.row_factory = sqlite3.Row
    cat_id = dc.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()["id"]
    dc.execute(
        "INSERT INTO items (sku, name, category_id, quantity, unit, selling_price, unit_cost, canonical_id, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("DC-001", "测试包材", cat_id, 50.0, "件", 5.50, 4.00, 101, ts),
    )
    dc.commit()
    dc.close()

    store = sqlite3.connect(str(store_path))
    store.row_factory = sqlite3.Row
    cat_id = store.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()["id"]
    store.execute(
        "INSERT INTO items (sku, name, category_id, quantity, unit, canonical_id, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("ST-001", "测试包材", cat_id, 0.0, "件", 101, ts),
    )
    store.commit()
    store.close()

    return tmp, master_path


def e2e_smoke():
    tmp, master_path = _make_test_env()
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    # 1. storefront 用户加购 + 提交
    with client.session_transaction() as s:
        s["user_id"] = 1  # store_mgr
        s["warehouse_id"] = 2  # store
    r = client.post(
        "/store-ordering/cart/add",
        data={"dc": "dc", "canonical_id": 101, "quantity": "10", "unit": "件"},
        follow_redirects=True,
    )
    assert r.status_code == 200
    print("[1] storefront 加购 OK")

    r = client.post(
        "/store-ordering/cart/submit",
        data={"expected_delivery_date": "2026-10-07", "note": "T11 冒烟"},
        follow_redirects=True,
    )
    assert r.status_code == 200
    body = r.data.decode()
    assert "提交成功" in body
    m = sqlite3.connect(str(master_path))
    m.row_factory = sqlite3.Row
    order_id = m.execute("SELECT id FROM store_orders ORDER BY id DESC LIMIT 1").fetchone()["id"]
    m.close()
    print(f"[2] 订单提交成功: id={order_id}")

    # 2. DC manager 审批通过
    with client.session_transaction() as s:
        s["user_id"] = 2
        s["warehouse_id"] = 1
    r = client.post(
        f"/store-ordering/orders/{order_id}/review",
        data={"decision": "approved", "note": "ok"},
    )
    assert r.status_code == 302
    print("[3] DC 审批通过 OK")

    # 3. DC staff 部分发货 6/10 (POST shipped_items[N]=6)
    m = sqlite3.connect(str(master_path))
    m.row_factory = sqlite3.Row
    item_id = m.execute("SELECT id FROM store_order_items WHERE order_id=?", (order_id,)).fetchone()["id"]
    m.close()
    with client.session_transaction() as s:
        s["user_id"] = 3
        s["warehouse_id"] = 1
    r = client.post(
        f"/store-ordering/orders/{order_id}/ship",
        data={f"shipped_items[{item_id}]": "6", "tracking_note": "顺丰"},
        follow_redirects=True,
    )
    assert r.status_code == 200
    body = r.data.decode()
    assert "本批发货" in body  # partial flash
    assert "出库成功" not in body
    m = sqlite3.connect(str(master_path))
    m.row_factory = sqlite3.Row
    status = m.execute("SELECT status FROM store_orders WHERE id=?", (order_id,)).fetchone()["status"]
    shipped_qty = m.execute("SELECT shipped_quantity FROM store_order_items WHERE id=?", (item_id,)).fetchone()["shipped_quantity"]
    m.close()
    assert status == "approved", f"partial 应仍 approved，实际 {status}"
    assert shipped_qty == 6.0, f"shipped_quantity 应 6.0，实际 {shipped_qty}"
    print(f"[4] 部分发货 6/10 OK: status={status} shipped_qty={shipped_qty}")

    # 4. 继续发剩余 4/10
    r = client.post(
        f"/store-ordering/orders/{order_id}/ship",
        data={f"shipped_items[{item_id}]": "4"},
        follow_redirects=True,
    )
    assert r.status_code == 200
    body = r.data.decode()
    assert "出库成功" in body  # fully shipped
    m = sqlite3.connect(str(master_path))
    m.row_factory = sqlite3.Row
    status = m.execute("SELECT status FROM store_orders WHERE id=?", (order_id,)).fetchone()["status"]
    shipped_qty = m.execute("SELECT shipped_quantity FROM store_order_items WHERE id=?", (item_id,)).fetchone()["shipped_quantity"]
    m.close()
    assert status == "shipped"
    assert shipped_qty == 10.0
    print(f"[5] 发齐 10/10 OK: status={status}")

    # 5. storefront 用户 部分收货 4/10
    with client.session_transaction() as s:
        s["user_id"] = 1
        s["warehouse_id"] = 2
    r = client.post(
        f"/store-ordering/orders/{order_id}/receive",
        data={"order_item_id": item_id, "quantity": "4", "note": "首收"},
        follow_redirects=True,
    )
    assert r.status_code == 200

    m = sqlite3.connect(str(master_path))
    m.row_factory = sqlite3.Row
    fulfilled = m.execute("SELECT fulfilled_quantity FROM store_order_items WHERE id=?", (item_id,)).fetchone()["fulfilled_quantity"]
    m.close()
    assert fulfilled == 4.0
    print(f"[6] 部分收货 4/10 OK: fulfilled={fulfilled}")

    # 6. 继续收齐
    r = client.post(
        f"/store-ordering/orders/{order_id}/receive",
        data={"order_item_id": item_id, "quantity": "6", "note": "余收"},
        follow_redirects=True,
    )
    assert r.status_code == 200
    m = sqlite3.connect(str(master_path))
    m.row_factory = sqlite3.Row
    status = m.execute("SELECT status FROM store_orders WHERE id=?", (order_id,)).fetchone()["status"]
    fulfilled = m.execute("SELECT fulfilled_quantity FROM store_order_items WHERE id=?", (item_id,)).fetchone()["fulfilled_quantity"]
    m.close()
    assert status == "delivered"
    assert fulfilled == 10.0
    print(f"[7] 收齐 10/10 → delivered OK: status={status}")

    # 7. 访问 order_detail 三角色看视图
    for label, uid, wid in [
        ("DC staff", 3, 1),
        ("storefront mgr", 1, 2),
    ]:
        with client.session_transaction() as s:
            s["user_id"] = uid
            s["warehouse_id"] = wid
        r = client.get(f"/store-ordering/orders/{order_id}")
        assert r.status_code == 200
        body = r.data.decode()
        assert "订单总金额" in body
        assert "info-card-total-value" in body
        print(f"[8] {label} 视图 order_detail OK: 含 info-card + 总金额")

    import shutil
    shutil.rmtree(tmp)
    print("\n=== T11 E2E SMOKE PASSED ===")


if __name__ == "__main__":
    e2e_smoke()