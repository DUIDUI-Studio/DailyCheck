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


@pytest.fixture(autouse=True)
def mcp_test_env(tmp_path, monkeypatch):
    """Every MCP test gets an initialized master and cannot open host databases.

    Patch import-bound constants as well as config: changing only the environment
    after collection leaves UoW pointing at the default master.db. The SQLite
    guard catches future missed aliases, including read-only URI connections.
    """
    from pathlib import Path
    from urllib.parse import parse_qs, unquote, urlsplit

    import config
    import db
    import mcp_server.config as mcp_config
    from mcp_server.data import unit_of_work
    from mcp_server.infra import access_log
    from mcp_server.service.auth import _current_auth

    root = tmp_path.resolve()
    master = root / "master.db"
    warehouses = root / "warehouses"
    warehouses.mkdir()
    log = root / "access.log"
    monkeypatch.setenv("DAILYCHECK_DB_DIR", str(root))
    monkeypatch.delenv("DAILYCHECK_MCP_TOKEN", raising=False)
    monkeypatch.setattr(config, "DB_DIR", root)
    for module in (config, db, mcp_config, unit_of_work):
        monkeypatch.setattr(module, "MASTER_DB", master)
    for module in (config, db):
        monkeypatch.setattr(module, "WAREHOUSE_DB_DIR", warehouses)
    monkeypatch.setattr(access_log, "_ACCESS_LOG_PATH", log)

    connect = sqlite3.connect

    def isolated_connect(database, *args, **kwargs):
        value = str(database)
        if value not in ("", ":memory:"):
            if value.startswith("file:"):
                uri = urlsplit(value)
                value = unquote(uri.path)
                if value == ":memory:" or parse_qs(uri.query).get("mode") == ["memory"]:
                    return connect(database, *args, **kwargs)
            path = Path(value).resolve()
            if not path.is_relative_to(root):
                raise AssertionError(f"MCP test attempted to open a non-test database: {path}")
        return connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", isolated_connect)
    token = _current_auth.set(None)
    try:
        db.init_master_db()
        yield {"root": root, "master": master, "warehouses": warehouses, "log": log}
    finally:
        _current_auth.reset(token)
