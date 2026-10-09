"""Tests for the cross-warehouse item publish web routes.

2026-10-09（方案 P0-5）：rd → 门店 的 `/items/publish` 直发通道已下线，
品项下发统一走 canonical 扇出（/canonical/fanout）。本文件覆盖：
  - 旧路由（GET/POST）重定向到 /canonical/fanout，且不再写任何数据
  - 历史页 /items/publish/history 仍可查询（只读）
事件数据用 pure `publish_items()` 直接构造（route 已不可用）。
"""
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


def _publish_via_pure(master_path, rd_path, item_ids, target_codes,
                      action="overwrite", summary=None):
    """直接调用 pure publish_items 造发布事件（route 已下线后仍需要事件数据）。"""
    import sqlite3
    from contextlib import closing

    from blueprints.publish_recipe_pure import publish_items

    with closing(sqlite3.connect(master_path)) as m:
        m.row_factory = sqlite3.Row
        m.execute("PRAGMA foreign_keys = ON")
        with closing(sqlite3.connect(rd_path)) as rd:
            rd.row_factory = sqlite3.Row
            result = publish_items(
                m, rd, "rd_001", item_ids, target_codes,
                user_id=1, summary=summary, default_action=action,
            )
        m.commit()
    return result


def test_items_publish_route_redirects_to_canonical_fanout(tmp_path, monkeypatch):
    """旧通道已下线：GET/POST 都 302 到 /canonical/fanout，且不写任何数据。"""
    import sqlite3
    client, master, rd, wh1, wh2, x, y = _setup_rd_with_two_storefronts(
        tmp_path, monkeypatch)

    r = client.get("/items/publish", follow_redirects=False)
    assert r.status_code == 302
    assert "/canonical/fanout" in r.headers["Location"]

    r = client.post("/items/publish", data={
        "item_ids": [str(x), str(y)],
        "warehouse_codes": ["wh_001", "wh_002"],
        "default_action": "overwrite",
    }, follow_redirects=False)
    assert r.status_code == 302
    assert "/canonical/fanout" in r.headers["Location"]

    for wh_path in (wh1, wh2):
        w = sqlite3.connect(wh_path)
        count = w.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        w.close()
        assert count == 0, f"{wh_path} 不应被旧通道写入"

    m = sqlite3.connect(master)
    count = m.execute("SELECT COUNT(*) FROM item_publish_events").fetchone()[0]
    m.close()
    assert count == 0, "旧通道不应再产生发布事件"


def test_items_publish_history_page_lists_events(tmp_path, monkeypatch):
    """GET history page returns 200 + shows event rows."""
    client, master, rd, wh1, wh2, x, y = _setup_rd_with_two_storefronts(
        tmp_path, monkeypatch)
    _publish_via_pure(master, rd, [x], ["wh_001"],
                      summary="history page test")

    r = client.get("/items/publish/history")
    assert r.status_code == 200
    body = r.data.decode("utf-8")
    assert "品项同步历史" in body


def test_history_detail_renders_event_items(tmp_path, monkeypatch):
    """GET /items/publish/history/<event_id> must render the per-item table.

    Regression: the dict returned by get_item_event_details used the key
    'items', which Jinja's attribute lookup interprets as dict.items (the
    builtin method) → TypeError → HTTP 500. Renamed the key to
    'event_items' in the pure helper; template uses detail.event_items.
    """
    client, master, rd, wh1, wh2, x, y = _setup_rd_with_two_storefronts(
        tmp_path, monkeypatch)
    result = _publish_via_pure(master, rd, [x], ["wh_001"],
                               summary="history detail test")
    event_id = result["event_id"]

    r = client.get(f"/items/publish/history/{event_id}")
    assert r.status_code == 200, "history detail must not 500"
    body = r.data.decode("utf-8")
    assert "事件 #" in body
    assert "按品项" in body
    assert str(x) in body  # item_id column rendered
    assert "history detail test" in body  # summary rendered


def test_history_detail_shows_item_name(tmp_path, monkeypatch):
    """Detail table must show item name + sku, not just the integer id."""
    client, master, rd, wh1, wh2, x, y = _setup_rd_with_two_storefronts(
        tmp_path, monkeypatch)
    result = _publish_via_pure(master, rd, [x, y], ["wh_001"],
                               summary="names test")

    r = client.get(f"/items/publish/history/{result['event_id']}")
    assert r.status_code == 200
    body = r.data.decode("utf-8")
    assert "SKU-X" in body, "item name 'SKU-X' should appear in detail table"
    assert "SKU-Y" in body, "item name 'SKU-Y' should appear in detail table"
