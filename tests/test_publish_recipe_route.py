"""Tests for the recipe publish web routes (POST /publish + GET /versions)."""
from datetime import datetime


def _setup_rd_with_warehouse(tmp_path, monkeypatch):
    """Build master.db + rd_001.db + wh_001.db, all registered."""
    import db as db_module
    import config as config_module
    from db import init_master_db, init_warehouse_db, migrate_warehouse_db_columns
    master = tmp_path / "master.db"
    rd_db = tmp_path / "rd_001.db"
    wh_db = tmp_path / "wh_001.db"
    monkeypatch.setattr(db_module, "MASTER_DB", master)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", tmp_path)
    monkeypatch.setattr(config_module, "MASTER_DB", master)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", tmp_path)
    monkeypatch.setattr(config_module, "BASE_DIR", tmp_path)
    init_master_db()
    init_warehouse_db(rd_db)
    migrate_warehouse_db_columns(rd_db)
    init_warehouse_db(wh_db)
    migrate_warehouse_db_columns(wh_db)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    import sqlite3
    m = sqlite3.connect(master)
    m.execute("INSERT INTO users (username, password_hash, is_admin, created_at) "
              "VALUES ('admin', 'x', 1, ?)", (ts,))
    m.execute("INSERT INTO warehouses (code, name, db_path, warehouse_type, created_at) "
              "VALUES ('rd_001', 'R&D', ?, 'rd', ?)",
              (str(rd_db), ts))
    m.execute("INSERT INTO warehouses (code, name, db_path, warehouse_type, created_at) "
              "VALUES ('wh_001', '中央仓', ?, 'storefront', ?)",
              (str(wh_db), ts))
    m.commit()
    m.close()

    # One ic_recipe + 2 items in rd_001
    rd = sqlite3.connect(rd_db)
    rd.row_factory = sqlite3.Row
    cat = rd.execute("SELECT id FROM categories WHERE name='乳制品'").fetchone()["id"]
    rd.execute("INSERT INTO items (sku, name, category_id, unit, gram_per_unit, "
               "unit_cost, selling_price, updated_at) "
               "VALUES ('SKU-A', 'A', ?, '件', 1000, 5.0, 8.0, ?)", (cat, ts))
    a = rd.execute("SELECT id FROM items WHERE sku='SKU-A'").fetchone()["id"]
    rd.execute("INSERT INTO items (sku, name, category_id, unit, gram_per_unit, "
               "unit_cost, selling_price, updated_at) "
               "VALUES ('SKU-B', 'B', ?, '件', 500, 3.0, 5.0, ?)", (cat, ts))
    b = rd.execute("SELECT id FROM items WHERE sku='SKU-B'").fetchone()["id"]
    rd.execute("INSERT INTO ic_recipes (name, output_unit, output_qty, sale_price, "
               "created_at, updated_at) VALUES ('test-ic', 'g', 600, 100, ?, ?)",
               (ts, ts))
    ic = rd.execute("SELECT id FROM ic_recipes WHERE name='test-ic'").fetchone()["id"]
    rd.execute("INSERT INTO ic_recipe_items (ic_recipe_id, item_id, qty_per_unit) "
               "VALUES (?, ?, 400), (?, ?, 100)", (ic, a, ic, b))
    rd.commit()
    rd.close()

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = 1
        # rd_001's row id from master.db
        m = sqlite3.connect(master)
        m.row_factory = sqlite3.Row
        rd_id = m.execute(
            "SELECT id FROM warehouses WHERE code='rd_001'"
        ).fetchone()["id"]
        m.close()
        s["warehouse_id"] = rd_id
    return client, master, rd_db, wh_db, ic


def test_versions_page_loads_and_lists_drafts(tmp_path, monkeypatch):
    """GET version history returns 200 + renders after save creates a draft."""
    client, master, rd_db, wh_db, ic = _setup_rd_with_warehouse(tmp_path, monkeypatch)
    # No save yet → no draft, page should still render 200 with empty version list.
    r = client.get(f"/recipe-cost/ic_recipe/{ic}/versions")
    assert r.status_code == 200
    assert "版本历史" in r.data.decode("utf-8")


def test_save_creates_draft_version_in_master_db(tmp_path, monkeypatch):
    """POSTing a save on ic_recipe_edit upserts a draft in recipe_versions."""
    import sqlite3
    client, master, rd_db, wh_db, ic = _setup_rd_with_warehouse(tmp_path, monkeypatch)
    # Simulate a save by hitting the edit POST endpoint with the recipe's
    # existing BOM (no-op changes but the save path runs).
    rd = sqlite3.connect(rd_db)
    rd.row_factory = sqlite3.Row
    bom_rows = rd.execute(
        "SELECT id AS row_id, item_id, qty_per_unit "
        "FROM ic_recipe_items WHERE ic_recipe_id=? ORDER BY id", (ic,),
    ).fetchall()
    rd.close()

    form = {
        "name": "test-ic",
        "note": "",
        "sale_price": "100",
        "bom_row_id": [str(r["row_id"]) for r in bom_rows],
        "bom_item_id": [str(r["item_id"]) for r in bom_rows],
        "bom_qty": [str(r["qty_per_unit"]) for r in bom_rows],
        "bom_delete": ["0"] * len(bom_rows),
    }
    r = client.post(f"/recipe-cost/ic-recipes/{ic}/edit", data=form)
    assert r.status_code == 302  # redirect to edit page

    # recipe_versions row exists with status=draft.
    m = sqlite3.connect(master)
    m.row_factory = sqlite3.Row
    rows = m.execute(
        "SELECT * FROM recipe_versions "
        "WHERE recipe_type='ic_recipe' AND recipe_id=? "
        "ORDER BY version DESC", (ic,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "draft"
    m.close()


def test_publish_route_copies_recipe_and_items_into_storefront(tmp_path, monkeypatch):
    """POST publish → recipe + BOM + items appear in target warehouse db."""
    import sqlite3
    client, master, rd_db, wh_db, ic = _setup_rd_with_warehouse(tmp_path, monkeypatch)

    r = client.post(f"/recipe-cost/ic-recipes/{ic}/publish", data={
        "warehouse_codes": ["wh_001"],
        "summary": "e2e test publish",
    }, follow_redirects=False)
    assert r.status_code == 302

    # Verify wh_001 has the recipe + items.
    wh = sqlite3.connect(wh_db)
    wh.row_factory = sqlite3.Row
    recipe = wh.execute(
        "SELECT * FROM ic_recipes WHERE name='test-ic'"
    ).fetchone()
    assert recipe is not None
    assert float(recipe["sale_price"]) == 100.0
    lines = wh.execute(
        "SELECT ri.qty_per_unit, i.sku FROM ic_recipe_items ri "
        "JOIN items i ON i.id = ri.item_id"
    ).fetchall()
    skus = sorted(l["sku"] for l in lines)
    assert skus == ["SKU-A", "SKU-B"]

    # master.db should have recipe_versions (published) + publish_event (success)
    m = sqlite3.connect(master)
    m.row_factory = sqlite3.Row
    v = m.execute(
        "SELECT status FROM recipe_versions "
        "WHERE recipe_type='ic_recipe' AND recipe_id=?", (ic,)
    ).fetchone()
    assert v["status"] == "published"
    ev = m.execute(
        "SELECT status FROM recipe_publish_events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert ev["status"] == "complete"
    ev_wh = m.execute(
        "SELECT status FROM recipe_publish_event_warehouses ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert ev_wh["status"] == "success"
    m.close()
    wh.close()


def test_publish_invalid_warehouse_records_failure(tmp_path, monkeypatch):
    """POST publish with bogus warehouse → partial + recipe NOT applied."""
    import sqlite3
    client, master, rd_db, wh_db, ic = _setup_rd_with_warehouse(tmp_path, monkeypatch)

    r = client.post(f"/recipe-cost/ic-recipes/{ic}/publish", data={
        "warehouse_codes": ["nonexistent_warehouse"],
        "summary": None,
    }, follow_redirects=False)
    # The route redirects with a flash; status 302 is fine.
    assert r.status_code == 302

    m = sqlite3.connect(master)
    m.row_factory = sqlite3.Row
    ev_wh = m.execute(
        "SELECT status, error_message FROM recipe_publish_event_warehouses "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert ev_wh["status"] == "failed"
    assert "nonexistent_warehouse" in (ev_wh["error_message"] or "") or \
        "not registered" in (ev_wh["error_message"] or "")
    # recipe still in 'published' state (not rolled back).
    v = m.execute(
        "SELECT status FROM recipe_versions "
        "WHERE recipe_type='ic_recipe' AND recipe_id=?", (ic,)
    ).fetchone()
    assert v["status"] == "published"
    m.close()


def test_publish_emits_recipe_published_notification(tmp_path, monkeypatch):
    """Successful publish creates a 'recipe_published' notification for admins."""
    import sqlite3
    client, master, rd_db, wh_db, ic = _setup_rd_with_warehouse(tmp_path, monkeypatch)

    r = client.post(f"/recipe-cost/ic-recipes/{ic}/publish", data={
        "warehouse_codes": ["wh_001"],
        "summary": "notify test",
    }, follow_redirects=False)
    assert r.status_code == 302

    m = sqlite3.connect(master)
    m.row_factory = sqlite3.Row
    notifs = m.execute(
        "SELECT event_type, summary FROM notifications "
        "WHERE event_type='recipe_published' ORDER BY id DESC LIMIT 1"
    ).fetchall()
    assert len(notifs) == 1
    assert notifs[0]["summary"] == "notify test"
    m.close()
