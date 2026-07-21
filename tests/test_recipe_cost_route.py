"""items.selling_price 字段端到端测试。"""
from datetime import datetime


def test_create_item_stores_selling_price(tmp_path, monkeypatch):
    """POST /items 含 selling_price=12.50 → DB 写入。"""
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
        "INSERT INTO warehouses (id, code, name, db_path, created_at) "
        "VALUES (1, 'wh_t', 'T', ?, ?)", (str(wh_path), ts))
    m.execute(
        "INSERT INTO warehouse_users (user_id, warehouse_id, role) "
        "VALUES (1, 1, 'admin')")
    m.commit()
    m.close()

    wh_conn = sqlite3.connect(wh_path)
    wh_conn.row_factory = sqlite3.Row
    cat_id = wh_conn.execute(
        "SELECT id FROM categories ORDER BY id LIMIT 1"
    ).fetchone()["id"]
    wh_conn.close()

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = 1
        s["warehouse_id"] = 1

    resp = client.post("/items", data={
        "name": "糖",
        "category_id": str(cat_id),
        "quantity": "10",
        "safety_stock": "0",
        "unit_cost": "5",
        "selling_price": "12.50",
        "unit": "件",
        "aux_unit": "",
        "aux_rate": "0",
    }, follow_redirects=True)
    assert resp.status_code == 200

    check = sqlite3.connect(wh_path)
    check.row_factory = sqlite3.Row
    row = check.execute(
        "SELECT selling_price, selling_price_updated_at FROM items WHERE name='糖'"
    ).fetchone()
    check.close()
    assert float(row["selling_price"]) == 12.50
    assert row["selling_price_updated_at"] is not None


def test_edit_item_updates_selling_price(tmp_path, monkeypatch):
    """POST /items/<id>/edit 改 selling_price → DB 更新 + 时间戳。"""
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
        "INSERT INTO warehouses (id, code, name, db_path, created_at) "
        "VALUES (1, 'wh_t', 'T', ?, ?)", (str(wh_path), ts))
    m.execute(
        "INSERT INTO warehouse_users (user_id, warehouse_id, role) "
        "VALUES (1, 1, 'admin')")
    m.commit()
    m.close()

    conn = sqlite3.connect(wh_path)
    conn.row_factory = sqlite3.Row
    cat_id = conn.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()["id"]
    conn.execute(
        "INSERT INTO items (sku, name, category_id, quantity, unit_cost, "
        "selling_price, selling_price_updated_at, unit, gram_per_unit, aux_rate, "
        "aux_unit, updated_at) "
        "VALUES ('X-1', '糖', ?, 10, 5, 10, ?, '件', 0, 0, NULL, ?)",
        (cat_id, ts, ts))
    item_id = conn.execute("SELECT id FROM items WHERE name='糖'").fetchone()["id"]
    conn.commit()
    conn.close()

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = 1
        s["warehouse_id"] = 1

    resp = client.post(f"/items/{item_id}/edit", data={
        "name": "糖",
        "category_id": str(cat_id),
        "safety_stock": "0",
        "unit_cost": "5",
        "selling_price": "20.00",
        "unit": "件",
        "aux_unit": "",
        "aux_rate": "0",
    }, follow_redirects=True)
    assert resp.status_code == 200

    check = sqlite3.connect(wh_path)
    check.row_factory = sqlite3.Row
    row = check.execute(
        "SELECT selling_price, selling_price_updated_at FROM items WHERE id=?",
        (item_id,),
    ).fetchone()
    check.close()
    assert float(row["selling_price"]) == 20.00
    assert row["selling_price_updated_at"] is not None
