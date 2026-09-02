"""Verify base.html nav shows '品类与品项' for admin in R&D warehouse."""
from datetime import datetime


def _ctx(app, monkeypatch, tmp_path, wh_type):
    import db as db_module
    import config as config_module
    from db import init_master_db, init_warehouse_db, migrate_warehouse_db_columns

    master_path = tmp_path / "master.db"
    wh_path = tmp_path / "wh.db"
    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", tmp_path)
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", tmp_path)
    init_master_db()
    init_warehouse_db(wh_path)
    migrate_warehouse_db_columns(wh_path)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    import sqlite3
    m = sqlite3.connect(master_path)
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (1, 'admin', 'x', 1, ?)", (ts,))
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) "
        "VALUES (1, 'wh_t', 'T', ?, ?, ?)", (str(wh_path), wh_type, ts))
    m.commit()
    m.close()

    with app.app_context(), app.test_request_context():
        from flask import g
        g.user = {"id": 1, "username": "admin", "is_admin": 1}
        g.role = None
        g.warehouse = {
            "id": 1, "code": "wh_t", "name": "T", "warehouse_type": wh_type,
        }
        from flask import render_template
        html = render_template("base.html", title="t")
    return html


def test_admin_sees_items_in_rd(tmp_path, monkeypatch):
    from app import create_app
    app = create_app()
    html = _ctx(app, monkeypatch, tmp_path, "rd")
    assert "品类与品项" in html, "admin in R&D should see 品类与品项 nav link"


def test_admin_sees_items_in_storefront(tmp_path, monkeypatch):
    from app import create_app
    app = create_app()
    html = _ctx(app, monkeypatch, tmp_path, "storefront")
    assert "品类与品项" in html, "admin in storefront should see 品类与品项 nav link"
