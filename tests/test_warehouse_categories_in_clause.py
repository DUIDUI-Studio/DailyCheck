"""去硬编码验证:warehouse_categories_in_clause 读仓库自身 categories 表。"""
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest


def _make_warehouse(master_path: Path, wh_path: Path, cats: list[str]) -> None:
    from db import init_master_db
    init_master_db()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    m = sqlite3.connect(master_path)
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (1, 'admin', 'x', 1, ?)", (ts,))
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, created_at) "
        "VALUES (1, 'wh_t', 'T', ?, ?)", (str(wh_path), ts))
    m.execute(
        "INSERT INTO warehouse_users (user_id, warehouse_id, role) "
        "VALUES (1, 1, 'admin')")
    m.commit()
    m.close()
    w = sqlite3.connect(wh_path)
    w.execute("""CREATE TABLE categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        description TEXT,
        created_at TEXT NOT NULL)""")
    for c in cats:
        w.execute(
            "INSERT INTO categories (name, description, created_at) VALUES (?, ?, ?)",
            (c, "", ts))
    w.commit()
    w.close()


def test_returns_all_categories_in_wh(tmp_path, monkeypatch):
    """仓库种了 5 个分类,函数返回 5 个 name。"""
    import db as db_module
    master_path = tmp_path / "master.db"
    wh_path = tmp_path / "warehouses" / "wh_t.db"
    wh_path.parent.mkdir()
    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_path.parent)
    _make_warehouse(master_path, wh_path, ["常温物料", "水果", "包装材料", "冷冻&冷藏食品", "定制周边"])

    from blueprints._helpers import warehouse_categories_in_clause
    from app import create_app
    app = create_app()
    with app.test_request_context():
        from flask import g
        g.warehouse_db_path = str(wh_path)
        g.warehouse_id = 1
        g.user = type("U", (), {"is_admin": True})()
        placeholders, params = warehouse_categories_in_clause()
        assert params == ["常温物料", "水果", "包装材料", "冷冻&冷藏食品", "定制周边"]
        assert placeholders == "?,?,?,?,?"


def test_empty_categories_returns_safe_clause(tmp_path, monkeypatch):
    """无分类的极端情况返回 (1, [0]) 防御性 0 行。"""
    import db as db_module
    master_path = tmp_path / "master.db"
    wh_path = tmp_path / "warehouses" / "wh_t.db"
    wh_path.parent.mkdir()
    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_path.parent)
    _make_warehouse(master_path, wh_path, [])

    from blueprints._helpers import warehouse_categories_in_clause
    from app import create_app
    app = create_app()
    with app.test_request_context():
        from flask import g
        g.warehouse_db_path = str(wh_path)
        placeholders, params = warehouse_categories_in_clause()
        assert (placeholders, params) == ("1", [0])