"""集成测试夹具：临时仓库 db + 直设 session 登录。

绕过密码校验（直接写 session），规避 Python 3.9 无 hashlib.scrypt
导致 werkzeug 默认 hash 不可用的问题。
"""
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest


@pytest.fixture
def logged_client(tmp_path, monkeypatch):
    import db as db_module
    from db import init_master_db, init_warehouse_db

    master_path = tmp_path / "master.db"
    wh_dir = tmp_path / "warehouses"
    wh_dir.mkdir()
    wh_path = wh_dir / "wh_test.db"

    # 让 db 模块用临时路径
    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_dir)
    # Also patch config directly — several blueprints (forecast,
    # procurement, notifications) import MASTER_DB from config at call
    # time to bypass flask 'g', so without this patch the test would
    # write to the real on-disk master.db and pollute it.
    import config as config_module
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", wh_dir)

    init_master_db()
    init_warehouse_db(wh_path)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    m = sqlite3.connect(master_path)
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (1, 'admin', 'x', 1, ?)", (ts,))
    # db_path 存绝对路径：auth.py 用 BASE_DIR / db_path，绝对路径右值会覆盖
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, created_at) "
        "VALUES (1, 'wh_test', '测试仓', ?, ?)", (str(wh_path), ts))
    m.execute(
        "INSERT INTO warehouse_users (user_id, warehouse_id, role) "
        "VALUES (1, 1, 'admin')")
    m.commit()
    m.close()

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = 1
        s["warehouse_id"] = 1

    return client, wh_path


def _wh(wh_path):
    conn = sqlite3.connect(wh_path)
    conn.row_factory = sqlite3.Row
    return conn


def _seed_item(wh_path, name, qty, unit_cost, gram_per_unit=0):
    """插入一个测试品项,返回 (item_id, category_id)。"""
    conn = _wh(wh_path)
    cat_id = conn.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()["id"]
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "INSERT INTO items (sku, name, category_id, quantity, safety_stock, unit_cost, unit, gram_per_unit, updated_at) "
        "VALUES (?, ?, ?, ?, 0, ?, '件', ?, ?)",
        (f"T-{name}", name, cat_id, qty, unit_cost, gram_per_unit, ts))
    item_id = cur.lastrowid
    conn.commit()
    conn.close()
    return item_id, cat_id


def _seed_outbound(wh_path, item_id, qty, reason=None):
    """插入一条出库请求。"""
    conn = _wh(wh_path)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO outbound_requests (item_id, requested_quantity, reason, rolled_back, created_at) "
        "VALUES (?, ?, ?, 0, ?)",
        (item_id, qty, reason, ts))
    conn.commit()
    conn.close()


def _seed_production_consumption(wh_path, item_id, qty):
    """插入一条生产消耗(自动建 product + production_run)。"""
    conn = _wh(wh_path)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "INSERT INTO products (name, unit, note, created_at) VALUES ('test-product', '件', '', ?)",
        (ts,))
    product_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO production_runs (product_id, output_qty, note, rolled_back, created_at) "
        "VALUES (?, 1, 'test', 0, ?)", (product_id, ts))
    run_id = cur.lastrowid
    conn.execute(
        "INSERT INTO production_run_items (run_id, item_id, planned_qty, actual_qty) VALUES (?, ?, ?, ?)",
        (run_id, item_id, qty, qty))
    conn.commit()
    conn.close()
