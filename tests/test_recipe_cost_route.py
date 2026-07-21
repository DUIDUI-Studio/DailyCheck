"""items.selling_price + 配方 CRUD 端到端测试。"""
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


def test_ic_recipe_crud(tmp_path, monkeypatch):
    """冰激凌配方 CRUD 完整链路。"""
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
        "selling_price, unit, gram_per_unit, aux_rate, aux_unit, updated_at) "
        "VALUES ('X-1', '糖', ?, 10, 5, 10, '件', 50, 50, '克', ?)",
        (cat_id, ts))
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

    resp = client.get("/recipe-cost/ic-recipes")
    assert resp.status_code == 200
    assert b"\xe5\x86\xb0\xe6\xbf\x80\xe5\x87\x8c\xe9\x85\x8d\xe6\x96\xb9" in resp.data

    resp = client.post("/recipe-cost/ic-recipes/new", data={
        "name": "香草冰淇淋",
        "note": "测试",
        "output_unit": "g",
        "output_qty": "100",
        "sale_price": "25",
    }, follow_redirects=True)
    assert resp.status_code == 200
    check = sqlite3.connect(wh_path)
    check.row_factory = sqlite3.Row
    ic_id = check.execute(
        "SELECT id FROM ic_recipes WHERE name='香草冰淇淋'"
    ).fetchone()["id"]

    resp = client.post(f"/recipe-cost/ic-recipes/{ic_id}/edit", data={
        "name": "香草冰淇淋",
        "note": "测试",
        "output_unit": "g",
        "output_qty": "100",
        "sale_price": "25",
        "bom_row_id": [""],
        "bom_item_id": [str(item_id)],
        "bom_qty": ["60"],
        "bom_delete": [""],
    }, follow_redirects=True)
    assert resp.status_code == 200

    row = check.execute(
        "SELECT qty_per_unit FROM ic_recipe_items WHERE ic_recipe_id=?",
        (ic_id,),
    ).fetchone()
    assert float(row["qty_per_unit"]) == 60.0

    resp = client.post(f"/recipe-cost/ic-recipes/{ic_id}/delete", follow_redirects=True)
    assert resp.status_code == 200
    cnt = check.execute(
        "SELECT COUNT(*) AS c FROM ic_recipes WHERE id=?", (ic_id,)
    ).fetchone()["c"]
    check.close()
    assert cnt == 0


def test_ic_recipe_delete_blocked_when_referenced(tmp_path, monkeypatch):
    """冰激凌配方被 recipe_items 引用时禁止删除。"""
    import db as db_module
    import config as config_module
    from db import init_master_db, init_warehouse_db
    master_path = tmp_path / "master.db"
    wh_path = tmp_path / "wh.db"
    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", tmp_path)
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", tmp_path)
    init_master_db()
    init_warehouse_db(wh_path)

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
    conn.execute(
        "INSERT INTO ic_recipes (name, output_unit, output_qty, sale_price, "
        "created_at, updated_at) VALUES ('香草冰淇淋', 'g', 100, 25, ?, ?)",
        (ts, ts))
    ic_id = conn.execute("SELECT id FROM ic_recipes WHERE name='香草冰淇淋'").fetchone()["id"]
    conn.execute(
        "INSERT INTO recipes (name, output_unit, output_qty, sale_price, "
        "created_at, updated_at) VALUES ('柠檬茶', '杯', 1, 15, ?, ?)",
        (ts, ts))
    recipe_id = conn.execute("SELECT id FROM recipes WHERE name='柠檬茶'").fetchone()["id"]
    conn.execute(
        "INSERT INTO recipe_items (recipe_id, source_type, ic_recipe_id, qty_per_unit) "
        "VALUES (?, 'ic_recipe', ?, 50)",
        (recipe_id, ic_id),
    )
    conn.commit()
    conn.close()

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = 1
        s["warehouse_id"] = 1

    resp = client.post(f"/recipe-cost/ic-recipes/{ic_id}/delete", follow_redirects=True)
    assert resp.status_code == 200
    check = sqlite3.connect(wh_path)
    check.row_factory = sqlite3.Row
    cnt = check.execute(
        "SELECT COUNT(*) AS c FROM ic_recipes WHERE id=?", (ic_id,)
    ).fetchone()["c"]
    check.close()
    assert cnt == 1


def test_recipe_crud_with_mixed_sources(tmp_path, monkeypatch):
    """出品配方 CRUD：含 item + ic_recipe 引用。"""
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
        "selling_price, unit, gram_per_unit, aux_rate, aux_unit, updated_at) "
        "VALUES ('X-1', '糖', ?, 10, 5, 10, '件', 50, 50, '克', ?)",
        (cat_id, ts))
    item_id = conn.execute("SELECT id FROM items WHERE name='糖'").fetchone()["id"]
    conn.execute(
        "INSERT INTO ic_recipes (name, output_unit, output_qty, sale_price, "
        "created_at, updated_at) VALUES ('香草冰淇淋', 'g', 100, 25, ?, ?)",
        (ts, ts))
    ic_id = conn.execute("SELECT id FROM ic_recipes WHERE name='香草冰淇淋'").fetchone()["id"]
    conn.commit()
    conn.close()

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = 1
        s["warehouse_id"] = 1

    resp = client.post("/recipe-cost/recipes/new", data={
        "name": "柠檬茶",
        "note": "test",
        "output_unit": "杯",
        "output_qty": "1",
        "sale_price": "15",
    }, follow_redirects=True)
    assert resp.status_code == 200
    check = sqlite3.connect(wh_path)
    check.row_factory = sqlite3.Row
    rid = check.execute(
        "SELECT id FROM recipes WHERE name='柠檬茶'"
    ).fetchone()["id"]

    resp = client.post(f"/recipe-cost/recipes/{rid}/edit", data={
        "name": "柠檬茶",
        "note": "test",
        "output_unit": "杯",
        "output_qty": "1",
        "sale_price": "15",
        "bom_row_id": ["", ""],
        "bom_source_type": ["item", "ic_recipe"],
        "bom_item_id": [str(item_id), ""],
        "bom_ic_recipe_id": ["", str(ic_id)],
        "bom_qty": ["60", "50"],
        "bom_delete": ["", ""],
    }, follow_redirects=True)
    assert resp.status_code == 200

    rows = check.execute(
        "SELECT source_type, item_id, ic_recipe_id, qty_per_unit FROM recipe_items "
        "WHERE recipe_id=? ORDER BY id",
        (rid,),
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["source_type"] == "item"
    assert rows[0]["item_id"] == item_id
    assert float(rows[0]["qty_per_unit"]) == 60.0
    assert rows[1]["source_type"] == "ic_recipe"
    assert rows[1]["ic_recipe_id"] == ic_id
    assert float(rows[1]["qty_per_unit"]) == 50.0

    resp = client.post(f"/recipe-cost/recipes/{rid}/delete", follow_redirects=True)
    assert resp.status_code == 200
    cnt = check.execute(
        "SELECT COUNT(*) AS c FROM recipes WHERE id=?", (rid,)
    ).fetchone()["c"]
    check.close()
    assert cnt == 0


def test_update_selling_price_endpoint(tmp_path, monkeypatch):
    """POST /recipe-cost/items/<id>/update-selling-price 写 DB + audit_log。"""
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
        "selling_price, unit, gram_per_unit, aux_rate, aux_unit, updated_at) "
        "VALUES ('X-1', '糖', ?, 10, 5, 10, '件', 0, 0, NULL, ?)",
        (cat_id, ts))
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

    resp = client.post(f"/recipe-cost/items/{item_id}/update-selling-price",
                       data={"selling_price": "15.00"},
                       follow_redirects=True)
    assert resp.status_code == 200

    check = sqlite3.connect(wh_path)
    check.row_factory = sqlite3.Row
    row = check.execute(
        "SELECT selling_price, selling_price_updated_at FROM items WHERE id=?",
        (item_id,),
    ).fetchone()
    check.close()
    assert float(row["selling_price"]) == 15.00
    assert row["selling_price_updated_at"] is not None


def test_update_selling_price_rejects_negative(tmp_path, monkeypatch):
    """负数 → flash 拦截 + DB 不变。"""
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
        "selling_price, unit, gram_per_unit, aux_rate, aux_unit, updated_at) "
        "VALUES ('X-1', '糖', ?, 10, 5, 10, '件', 0, 0, NULL, ?)",
        (cat_id, ts))
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

    resp = client.post(f"/recipe-cost/items/{item_id}/update-selling-price",
                       data={"selling_price": "-1"},
                       follow_redirects=True)
    assert resp.status_code == 200

    check = sqlite3.connect(wh_path)
    check.row_factory = sqlite3.Row
    row = check.execute(
        "SELECT selling_price FROM items WHERE id=?", (item_id,)
    ).fetchone()
    check.close()
    assert float(row["selling_price"]) == 10.0


def test_api_cost_returns_json(tmp_path, monkeypatch):
    """GET /recipe-cost/api/cost/<kind>/<id> 返回成本 JSON。"""
    import db as db_module
    import config as config_module
    from db import init_master_db, init_warehouse_db
    master_path = tmp_path / "master.db"
    wh_path = tmp_path / "wh.db"
    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", tmp_path)
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", tmp_path)
    init_master_db()
    init_warehouse_db(wh_path)

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
    conn.execute(
        "INSERT INTO ic_recipes (name, output_unit, output_qty, sale_price, "
        "created_at, updated_at) VALUES ('香草冰淇淋', 'g', 100, 25, ?, ?)",
        (ts, ts))
    ic_id = conn.execute("SELECT id FROM ic_recipes WHERE name='香草冰淇淋'").fetchone()["id"]
    conn.commit()
    conn.close()

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = 1
        s["warehouse_id"] = 1

    resp = client.get(f"/recipe-cost/api/cost/ic_recipe/{ic_id}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["cost_purchase"] == 0.0
    assert data["cost_selling"] == 0.0
    assert data["sale_price"] == 25.0
    assert data["margin_purchase"] == 1.0  # (25-0)/25 = 100%


def test_items_delete_blocked_when_referenced_by_recipe(tmp_path, monkeypatch):
    """品项被 ic_recipe_items 引用时禁止删除。"""
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
        "selling_price, unit, gram_per_unit, aux_rate, aux_unit, updated_at) "
        "VALUES ('X-1', '糖', ?, 10, 5, 10, '件', 0, 0, NULL, ?)",
        (cat_id, ts))
    item_id = conn.execute("SELECT id FROM items WHERE name='糖'").fetchone()["id"]
    conn.execute(
        "INSERT INTO ic_recipes (name, output_unit, output_qty, sale_price, "
        "created_at, updated_at) VALUES ('香草冰淇淋', 'g', 100, 25, ?, ?)",
        (ts, ts))
    ic_id = conn.execute("SELECT id FROM ic_recipes WHERE name='香草冰淇淋'").fetchone()["id"]
    conn.execute(
        "INSERT INTO ic_recipe_items (ic_recipe_id, item_id, qty_per_unit) "
        "VALUES (?, ?, 50)", (ic_id, item_id))
    conn.commit()
    conn.close()

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = 1
        s["warehouse_id"] = 1

    resp = client.post(f"/items/{item_id}/delete", follow_redirects=True)
    assert resp.status_code == 200
    check = sqlite3.connect(wh_path)
    check.row_factory = sqlite3.Row
    cnt = check.execute(
        "SELECT COUNT(*) AS c FROM items WHERE id=?", (item_id,)
    ).fetchone()["c"]
    check.close()
    assert cnt == 1
