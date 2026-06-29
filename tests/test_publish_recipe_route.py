"""Integration tests for POST /admin/publish/recipe.

Spec: docs/superpowers/specs/2026-06-29-recipe-publish-design.md §1.2, §3, §7.2.
PRD : §2.5 (recipe publish + cross-warehouse notification).

Covers 5 cases:
  1. product not found  → 404
  2. empty bom_items    → 400
  3. full success (2 warehouses)
  4. partial success (1 ok + 1 fail)
  5. emit_event → /notifications shows 1
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime


def _make_product(wh_path, name="柠檬茶") -> int:
    """Insert a product directly in the warehouse db. Returns product_id."""
    conn = sqlite3.connect(wh_path)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "INSERT INTO products (name, unit, note, created_at) VALUES (?, '件', '', ?)",
        (name, ts),
    )
    pid = int(cur.lastrowid)
    conn.commit()
    conn.close()
    return pid


def _add_warehouse(master_path, code, name, wh_path):
    """Add a second warehouse to master.db. Returns its code."""
    # Initialize the schema so writes succeed.
    from db import init_warehouse_db
    init_warehouse_db(wh_path)
    conn = sqlite3.connect(master_path)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # If already present, just return
    existing = conn.execute("SELECT id FROM warehouses WHERE code=?", (code,)).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO warehouses (code, name, db_path, created_at) VALUES (?, ?, ?, ?)",
            (code, name, str(wh_path), ts),
        )
        conn.commit()
    conn.close()


def _seed_item(wh_path, name):
    conn = sqlite3.connect(wh_path)
    cat_id = conn.execute(
        "SELECT id FROM categories ORDER BY id LIMIT 1"
    ).fetchone()[0]
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "INSERT INTO items (sku, name, category_id, quantity, safety_stock, unit_cost, unit, gram_per_unit, updated_at) "
        "VALUES (?, ?, ?, 0, 0, 0, '件', 0, ?)",
        (f"T-{name}", name, cat_id, ts),
    )
    iid = int(cur.lastrowid)
    conn.commit()
    conn.close()
    return iid


# ---------------------------------------------------------------------------
# T3.1 product not found → 404
# ---------------------------------------------------------------------------


def test_publish_recipe_product_not_found_returns_404(logged_client):
    client, _ = logged_client
    resp = client.post(
        "/admin/publish/recipe",
        json={
            "product_id": 9999,
            "bom_items": [{"item_id": 1, "qty_per_unit": 0.5}],
            "warehouse_codes": ["wh_test"],
            "summary": "不存在的产品",
        },
    )
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "product_not_found"}


# ---------------------------------------------------------------------------
# T3.2 empty bom_items → 400
# ---------------------------------------------------------------------------


def test_publish_recipe_empty_bom_returns_400(logged_client, tmp_path):
    client, wh_path = logged_client
    pid = _make_product(wh_path)
    resp = client.post(
        "/admin/publish/recipe",
        json={
            "product_id": pid,
            "bom_items": [],
            "warehouse_codes": ["wh_test"],
            "summary": "空 BOM",
        },
    )
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "empty_bom"}


# ---------------------------------------------------------------------------
# T3.3 full success — version inserted, 2 warehouses linked, event emitted
# ---------------------------------------------------------------------------


def test_publish_recipe_full_success_two_warehouses(logged_client, tmp_path):
    client, wh_path = logged_client
    # Add a second warehouse to master.db
    import config as config_module
    wh2_path = tmp_path / "wh2.db"
    _add_warehouse(config_module.MASTER_DB, "wh_002", "门店2", wh2_path)
    pid = _make_product(wh_path, "经典柠檬茶")
    item_id = _seed_item(wh_path, "茶叶")

    resp = client.post(
        "/admin/publish/recipe",
        json={
            "product_id": pid,
            "bom_items": [{"item_id": item_id, "qty_per_unit": 0.5}],
            "warehouse_codes": ["wh_test", "wh_002"],
            "summary": "经典柠檬茶 v1 已发布",
        },
    )
    assert resp.status_code == 200, resp.data
    body = resp.get_json()
    assert "version_id" in body and body["version_id"] > 0
    assert "publish_event_id" in body and body["publish_event_id"] > 0

    # Verify product_bom_versions row
    conn = sqlite3.connect(wh_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT version, bom_json FROM product_bom_versions WHERE id=?",
        (body["version_id"],),
    ).fetchone()
    assert row["version"] == 1
    assert json.loads(row["bom_json"]) == [{"item_id": item_id, "qty_per_unit": 0.5}]
    # products.current_version_id was updated
    p_row = conn.execute(
        "SELECT current_version_id FROM products WHERE id=?", (pid,),
    ).fetchone()
    assert p_row["current_version_id"] == body["version_id"]
    conn.close()

    # Verify product_bom_store_versions: 2 rows
    with sqlite3.connect(wh_path) as c:
        c.row_factory = sqlite3.Row
        store_rows = c.execute(
            "SELECT warehouse_code, bom_version_id FROM product_bom_store_versions WHERE product_id=?",
            (pid,),
        ).fetchall()
    codes = sorted(r["warehouse_code"] for r in store_rows)
    assert codes == ["wh_002", "wh_test"]
    for r in store_rows:
        assert r["bom_version_id"] == body["version_id"]

    # Verify master.db recipe_publish_events + per-warehouse rows
    m = sqlite3.connect(config_module.MASTER_DB)
    m.row_factory = sqlite3.Row
    ev = m.execute(
        "SELECT * FROM recipe_publish_events WHERE id=?", (body["publish_event_id"],),
    ).fetchone()
    assert ev["product_id"] == pid
    assert ev["bom_version_id"] == body["version_id"]
    assert ev["summary"] == "经典柠檬茶 v1 已发布"
    wh_statuses = m.execute(
        "SELECT warehouse_code, status FROM recipe_publish_event_warehouses "
        "WHERE publish_event_id=? ORDER BY warehouse_code",
        (body["publish_event_id"],),
    ).fetchall()
    assert [(r["warehouse_code"], r["status"]) for r in wh_statuses] == [
        ("wh_002", "success"),
        ("wh_test", "success"),
    ]
    m.close()


# ---------------------------------------------------------------------------
# T3.4 partial success — 1 warehouse ok, 1 fails (invalid wh code → no row inserted)
# ---------------------------------------------------------------------------


def test_publish_recipe_partial_success_one_warehouse_unknown(logged_client):
    """Unknown warehouse_code is treated as a per-store failure: version
    is still inserted, valid warehouses get linked, invalid one is
    recorded in recipe_publish_event_warehouses as status='failed'."""
    client, wh_path = logged_client
    pid = _make_product(wh_path)
    item_id = _seed_item(wh_path, "茶叶")
    resp = client.post(
        "/admin/publish/recipe",
        json={
            "product_id": pid,
            "bom_items": [{"item_id": item_id, "qty_per_unit": 1.0}],
            "warehouse_codes": ["wh_test", "does_not_exist"],
            "summary": "部分发布",
        },
    )
    assert resp.status_code == 200, resp.data
    body = resp.get_json()
    # The version was inserted; the publish_event lists the partial failure
    assert "version_id" in body
    assert "publish_event_id" in body
    assert "failed_warehouses" in body
    failed = body["failed_warehouses"]
    assert any(f["warehouse_code"] == "does_not_exist" for f in failed)
    # product_bom_store_versions has 1 row (wh_test only)
    with sqlite3.connect(wh_path) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT warehouse_code FROM product_bom_store_versions WHERE product_id=?",
            (pid,),
        ).fetchall()
    assert [r["warehouse_code"] for r in rows] == ["wh_test"]


# ---------------------------------------------------------------------------
# T3.5 emit_event → /notifications shows 1
# ---------------------------------------------------------------------------


def test_publish_recipe_emits_notification_visible_in_feed(logged_client):
    client, wh_path = logged_client
    pid = _make_product(wh_path)
    item_id = _seed_item(wh_path, "茶叶")
    resp = client.post(
        "/admin/publish/recipe",
        json={
            "product_id": pid,
            "bom_items": [{"item_id": item_id, "qty_per_unit": 1.0}],
            "warehouse_codes": ["wh_test"],
            "summary": "经典柠檬茶 v1",
        },
    )
    assert resp.status_code == 200
    body = resp.get_json()
    # Now hit /notifications and expect 1 unread event with summary + target_url
    feed = client.get("/notifications?unread=true").get_json()
    assert feed["unread_count"] == 1
    e = feed["events"][0]
    assert e["event_type"] == "recipe_published"
    assert e["summary"] == "经典柠檬茶 v1"
    expected_url = f"/products/{pid}/versions/{body['version_id']}"
    assert e["target_url"] == expected_url


# ---------------------------------------------------------------------------
# T3.6 require platform admin — staff cannot publish
# ---------------------------------------------------------------------------


def test_publish_recipe_staff_user_forbidden(staff_client, tmp_path):
    client, wh_path = staff_client
    pid = _make_product(wh_path)
    resp = client.post(
        "/admin/publish/recipe",
        json={
            "product_id": pid,
            "bom_items": [{"item_id": 1, "qty_per_unit": 0.5}],
            "warehouse_codes": ["wh_test"],
            "summary": "staff attempt",
        },
    )
    # staff is_admin=0 → require_platform_admin blocks → 403
    assert resp.status_code == 403
