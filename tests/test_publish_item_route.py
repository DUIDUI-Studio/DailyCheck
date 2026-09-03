"""Tests for the cross-warehouse item publish web routes."""
from datetime import datetime


def _setup_rd_with_two_storefronts(tmp_path, monkeypatch):
    """master + rd_001 + wh_001 + wh_002. Two items in rd_001."""
    import db as db_module
    import config as config_module
    from db import init_master_db, init_warehouse_db, migrate_warehouse_db_columns
    master = tmp_path / "master.db"
    rd = tmp_path / "rd.db"
    wh1 = tmp_path / "wh1.db"
    wh2 = tmp_path / "wh2.db"
    monkeypatch.setattr(db_module, "MASTER_DB", master)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", tmp_path)
    monkeypatch.setattr(config_module, "MASTER_DB", master)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", tmp_path)
    monkeypatch.setattr(config_module, "BASE_DIR", tmp_path)
    init_master_db()
    for p in (rd, wh1, wh2):
        init_warehouse_db(p)
        migrate_warehouse_db_columns(p)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    import sqlite3
    m = sqlite3.connect(master)
    m.execute("INSERT INTO users (username, password_hash, is_admin, created_at) "
              "VALUES ('admin', 'x', 1, ?)", (ts,))
    m.execute("INSERT INTO warehouses (code, name, db_path, warehouse_type, created_at) "
              "VALUES ('rd_001', 'R', ?, 'rd', ?)", (str(rd), ts))
    m.execute("INSERT INTO warehouses (code, name, db_path, warehouse_type, created_at) "
              "VALUES ('wh_001', 'W1', ?, 'storefront', ?)",
              (str(wh1), ts))
    m.execute("INSERT INTO warehouses (code, name, db_path, warehouse_type, created_at) "
              "VALUES ('wh_002', 'W2', ?, 'storefront', ?)",
              (str(wh2), ts))
    m.commit()
    rd_id = m.execute("SELECT id FROM warehouses WHERE code='rd_001'").fetchone()[0]
    m.close()

    # 2 items in rd_001.
    rd_conn = sqlite3.connect(rd)
    rd_conn.row_factory = sqlite3.Row
    cat = rd_conn.execute("SELECT id FROM categories WHERE name='乳制品'").fetchone()["id"]
    rd_conn.execute("INSERT INTO items (sku, name, category_id, unit, gram_per_unit, "
                     "unit_cost, selling_price, updated_at) "
                     "VALUES ('SKU-X', 'X', ?, '件', 1000, 1.0, 2.0, ?)", (cat, ts))
    x = rd_conn.execute("SELECT id FROM items WHERE sku='SKU-X'").fetchone()["id"]
    rd_conn.execute("INSERT INTO items (sku, name, category_id, unit, gram_per_unit, "
                     "unit_cost, selling_price, updated_at) "
                     "VALUES ('SKU-Y', 'Y', ?, '件', 500, 3.0, 5.0, ?)", (cat, ts))
    y = rd_conn.execute("SELECT id FROM items WHERE sku='SKU-Y'").fetchone()["id"]
    rd_conn.commit()
    rd_conn.close()

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = 1
        s["warehouse_id"] = rd_id
    return client, master, rd, wh1, wh2, x, y


def test_items_publish_get_renders_picker(tmp_path, monkeypatch):
    client, *_ = _setup_rd_with_two_storefronts(tmp_path, monkeypatch)
    r = client.get("/items/publish")
    assert r.status_code == 200
    body = r.data.decode("utf-8")
    assert "批量同步品项" in body
    assert "SKU-X" in body
    assert "wh_001" in body


def test_items_publish_post_inserts_items_into_target_storefronts(tmp_path, monkeypatch):
    """POST publishes items to selected warehouses."""
    import sqlite3
    client, master, rd, wh1, wh2, x, y = _setup_rd_with_two_storefronts(tmp_path, monkeypatch)

    r = client.post("/items/publish", data={
        "item_ids": [str(x), str(y)],
        "warehouse_codes": ["wh_001", "wh_002"],
        "default_action": "overwrite",
        "summary": "e2e test publish",
    }, follow_redirects=False)
    assert r.status_code == 302

    for wh_path in (wh1, wh2):
        w = sqlite3.connect(wh_path)
        w.row_factory = sqlite3.Row
        rows = w.execute("SELECT sku, name, unit_cost, selling_price FROM items").fetchall()
        skus = sorted(r["sku"] for r in rows)
        assert skus == ["SKU-X", "SKU-Y"], f"{wh_path} missing items"
        for r in rows:
            if r["sku"] == "SKU-X":
                assert float(r["unit_cost"]) == 1.0
            else:
                assert float(r["unit_cost"]) == 3.0
        w.close()

    # master.db has the publish event.
    m = sqlite3.connect(master)
    m.row_factory = sqlite3.Row
    ev = m.execute(
        "SELECT status, item_count FROM item_publish_events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert ev["status"] == "complete"
    assert ev["item_count"] == 2
    wh_rows = m.execute(
        "SELECT warehouse_code, status FROM item_publish_event_warehouses"
    ).fetchall()
    statuses = {r["warehouse_code"]: r["status"] for r in wh_rows}
    assert statuses == {"wh_001": "success", "wh_002": "success"}
    m.close()


def test_items_publish_partial_when_invalid_warehouse(tmp_path, monkeypatch):
    """Invalid warehouse → partial + valid ones still get the items."""
    import sqlite3
    client, master, rd, wh1, wh2, x, y = _setup_rd_with_two_storefronts(tmp_path, monkeypatch)

    r = client.post("/items/publish", data={
        "item_ids": [str(x)],
        "warehouse_codes": ["wh_001", "nonexistent"],
        "default_action": "overwrite",
        "summary": None,
    }, follow_redirects=False)
    assert r.status_code == 302

    m = sqlite3.connect(master)
    m.row_factory = sqlite3.Row
    ev = m.execute(
        "SELECT status FROM item_publish_events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert ev["status"] == "partial"
    wh_status = m.execute(
        "SELECT warehouse_code, status FROM item_publish_event_warehouses"
    ).fetchall()
    statuses = {r["warehouse_code"]: r["status"] for r in wh_status}
    assert statuses["wh_001"] == "success"
    assert statuses["nonexistent"] == "failed"

    # wh_001 got the item.
    w = sqlite3.connect(wh1)
    w.row_factory = sqlite3.Row
    row = w.execute("SELECT sku FROM items WHERE sku='SKU-X'").fetchone()
    assert row is not None
    w.close()
    m.close()


def test_items_publish_keep_doesnt_touch_existing(tmp_path, monkeypatch):
    """default_action=keep → existing storefront item is unchanged."""
    import sqlite3
    client, master, rd, wh1, wh2, x, y = _setup_rd_with_two_storefronts(tmp_path, monkeypatch)

    # Pre-populate wh_001 with SKU-X at different price.
    w = sqlite3.connect(wh1)
    cat = w.execute("SELECT id FROM categories WHERE name='乳制品'").fetchone()[0]
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    w.execute("INSERT INTO items (sku, name, category_id, unit, gram_per_unit, "
               "unit_cost, selling_price, updated_at) "
               "VALUES ('SKU-X', 'X-existing', ?, '件', 1000, 99.0, 199.0, ?)",
               (cat, ts))
    w.commit()
    w.close()

    r = client.post("/items/publish", data={
        "item_ids": [str(x)],
        "warehouse_codes": ["wh_001"],
        "default_action": "keep",
    }, follow_redirects=False)
    assert r.status_code == 302

    # wh_001 still has the original X at cost=99.
    w = sqlite3.connect(wh1)
    w.row_factory = sqlite3.Row
    row = w.execute("SELECT sku, unit_cost FROM items WHERE sku='SKU-X'").fetchone()
    assert float(row["unit_cost"]) == 99.0  # unchanged
    w.close()


def test_items_publish_history_page_lists_events(tmp_path, monkeypatch):
    """GET history page returns 200 + shows event rows after a publish."""
    client, master, rd, wh1, wh2, x, y = _setup_rd_with_two_storefronts(tmp_path, monkeypatch)
    # First, do a publish so there's an event.
    client.post("/items/publish", data={
        "item_ids": [str(x)],
        "warehouse_codes": ["wh_001"],
        "default_action": "overwrite",
    }, follow_redirects=False)
    r = client.get("/items/publish/history")
    assert r.status_code == 200
    body = r.data.decode("utf-8")
    assert "品项同步历史" in body
    assert "SKU-X" in body or "1" in body  # event count
