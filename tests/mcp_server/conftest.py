"""MCP 测试夹具：真实 SQLite 仓库库（内存外的临时文件）。"""
import sqlite3

import pytest

WAREHOUSE_SCHEMA = """
CREATE TABLE categories (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
    description TEXT, created_at TEXT);
CREATE TABLE items (
    id INTEGER PRIMARY KEY, sku TEXT, name TEXT, category_id INTEGER,
    quantity INTEGER, safety_stock INTEGER, unit TEXT,
    unit_cost REAL, gram_per_unit REAL, updated_at TEXT);
CREATE TABLE stock_movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL,
    action TEXT NOT NULL, delta REAL NOT NULL, note TEXT,
    created_at TEXT NOT NULL);
CREATE TABLE restock_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL,
    requested_quantity REAL NOT NULL, reason TEXT,
    status TEXT NOT NULL DEFAULT '提交', created_at TEXT NOT NULL);
"""


@pytest.fixture
def warehouse_db(tmp_path):
    """建一个含 1 个库存品的仓库库，返回 db 文件路径。"""
    db_path = tmp_path / "wh001.db"
    from db import init_warehouse_db
    init_warehouse_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO items (id, sku, name, category_id, quantity, unit, updated_at) "
        "VALUES (1, 'SKU1', 'Item A', 1, 10, 'pcs', '2026-10-30')"
    )
    conn.commit()
    conn.close()
    return str(db_path)
