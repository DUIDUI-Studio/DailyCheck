"""Tests for blueprints.publish_recipe_pure — the cross-warehouse
recipe + item publishing logic. Runs without Flask, using only
sqlite3, so the file fixtures are minimal.
"""
import sqlite3
from datetime import datetime
from pathlib import Path


def _bootstrap_two_warehouses(tmp_path: Path):
    """Build a master.db + rd_001.db (source) + wh_001.db (target).

    rd_001 has an ic_recipe + 2 items. wh_001 has its own (different)
    ic_recipe + 1 item with overlapping sku.
    """
    import sqlite3
    from db import init_master_db, init_warehouse_db, migrate_warehouse_db_columns

    master = tmp_path / "master.db"
    rd_db = tmp_path / "rd_001.db"
    wh_db = tmp_path / "wh_001.db"

    import db as db_mod, config as cfg
    db_mod.MASTER_DB = master
    db_mod.WAREHOUSE_DB_DIR = tmp_path
    cfg.MASTER_DB = master
    cfg.WAREHOUSE_DB_DIR = tmp_path
    cfg.BASE_DIR = tmp_path

    init_master_db()
    init_warehouse_db(rd_db)
    migrate_warehouse_db_columns(rd_db)
    init_warehouse_db(wh_db)
    migrate_warehouse_db_columns(wh_db)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Register warehouses in master.db
    m = sqlite3.connect(master)
    m.execute(
        "INSERT INTO warehouses (code, name, db_path, warehouse_type, created_at) "
        "VALUES ('rd_001', 'R&D', ?, 'rd', ?)",
        (str(rd_db.relative_to(tmp_path)), ts))
    m.execute(
        "INSERT INTO warehouses (code, name, db_path, warehouse_type, created_at) "
        "VALUES ('wh_001', '中央仓', ?, 'storefront', ?)",
        (str(wh_db.relative_to(tmp_path)), ts))
    m.commit()
    m.close()

    # rd_001: 1 ic_recipe "开心果1号" with 2 items
    rd = sqlite3.connect(rd_db)
    rd.row_factory = sqlite3.Row
    cat = rd.execute("SELECT id FROM categories WHERE name='乳制品'").fetchone()["id"]
    rd.execute(
        "INSERT INTO items (sku, name, category_id, unit, gram_per_unit, unit_cost, "
        "selling_price, updated_at) VALUES ('SKU-MILK', '牛奶', ?, '件', 1000, 6.0, 10.0, ?)",
        (cat, ts))
    milk = rd.execute("SELECT id FROM items WHERE sku='SKU-MILK'").fetchone()["id"]
    rd.execute(
        "INSERT INTO items (sku, name, category_id, unit, gram_per_unit, unit_cost, "
        "selling_price, updated_at) VALUES ('SKU-SUGAR', '糖', ?, '件', 500, 1.0, 2.0, ?)",
        (cat, ts))
    sugar = rd.execute("SELECT id FROM items WHERE sku='SKU-SUGAR'").fetchone()["id"]
    rd.execute(
        "INSERT INTO ic_recipes (name, output_unit, output_qty, sale_price, "
        "created_at, updated_at) VALUES ('开心果1号', 'g', 740, 250, ?, ?)",
        (ts, ts))
    ic = rd.execute("SELECT id FROM ic_recipes WHERE name='开心果1号'").fetchone()["id"]
    rd.execute(
        "INSERT INTO ic_recipe_items (ic_recipe_id, item_id, qty_per_unit) "
        "VALUES (?, ?, 500), (?, ?, 50)",
        (ic, milk, ic, sugar))
    rd.commit()
    rd.close()

    # wh_001: a placeholder item with overlapping sku (different price).
    wh = sqlite3.connect(wh_db)
    wh.execute(
        "INSERT INTO items (sku, name, category_id, unit, gram_per_unit, unit_cost, "
        "selling_price, updated_at) VALUES ('SKU-MILK', 'milk-store', ?, '件', 1000, 4.0, 7.0, ?)",
        (cat, ts))
    wh.commit()
    wh.close()

    return master, rd_db, wh_db, milk, sugar, ic


# ---------------------------------------------------------------------------
# Recipe version lifecycle
# ---------------------------------------------------------------------------

def test_snapshot_recipe_captures_head_and_lines(tmp_path):
    from blueprints.publish_recipe_pure import snapshot_recipe
    import sqlite3
    master, rd_db, wh_db, milk, sugar, ic = _bootstrap_two_warehouses(tmp_path)
    rd = sqlite3.connect(rd_db)
    rd.row_factory = sqlite3.Row
    snap = snapshot_recipe(rd, "ic_recipe", ic)
    rd.close()

    assert snap is not None
    assert snap["recipe_type"] == "ic_recipe"
    assert snap["head"]["name"] == "开心果1号"
    assert snap["head"]["sale_price"] == 250.0
    assert len(snap["lines"]) == 2
    # item names captured for cross-warehouse replay.
    assert {ln["sku"] for ln in snap["lines"]} == {"SKU-MILK", "SKU-SUGAR"}


def test_upsert_draft_version_creates_then_updates(tmp_path):
    import sqlite3
    from blueprints.publish_recipe_pure import (
        snapshot_recipe, upsert_draft_version, list_versions,
    )
    master, rd_db, wh_db, milk, sugar, ic = _bootstrap_two_warehouses(tmp_path)
    rd = sqlite3.connect(rd_db)
    rd.row_factory = sqlite3.Row

    snap1 = snapshot_recipe(rd, "ic_recipe", ic)
    m = sqlite3.connect(master)
    v1 = upsert_draft_version(m, "ic_recipe", ic, "rd_001", snap1, user_id=1)
    assert v1 > 0

    versions = list_versions(m, "ic_recipe", ic)
    assert len(versions) == 1
    assert versions[0]["version"] == 1
    assert versions[0]["status"] == "draft"

    # Edit snapshot (change sale_price), upsert again → same version id, updated snapshot.
    snap2 = dict(snap1)
    snap2["head"] = dict(snap1["head"])
    snap2["head"]["sale_price"] = 280.0
    v1b = upsert_draft_version(m, "ic_recipe", ic, "rd_001", snap2, user_id=1)
    assert v1b == v1, "draft should be updated in place"
    versions = list_versions(m, "ic_recipe", ic)
    assert versions[0]["version"] == 1
    assert versions[0]["status"] == "draft"
    import json
    stored_snap = json.loads(versions[0]["snapshot_json"])
    assert stored_snap["head"]["sale_price"] == 280.0

    m.close(); rd.close()


def test_publish_then_new_draft_increments_version(tmp_path):
    import sqlite3
    from blueprints.publish_recipe_pure import (
        snapshot_recipe, upsert_draft_version, publish_version,
        list_versions,
    )
    master, rd_db, wh_db, milk, sugar, ic = _bootstrap_two_warehouses(tmp_path)
    rd = sqlite3.connect(rd_db)
    rd.row_factory = sqlite3.Row
    snap = snapshot_recipe(rd, "ic_recipe", ic)
    rd.close()

    m = sqlite3.connect(master)
    v1 = upsert_draft_version(m, "ic_recipe", ic, "rd_001", snap, user_id=1)

    def apply(master_conn, code, snap):
        # Open target warehouse, insert recipe + items.
        row = master_conn.execute(
            "SELECT db_path FROM warehouses WHERE code=?", (code,)
        ).fetchone()
        target_path = tmp_path / row["db_path"]
        import sqlite3 as sq
        tc = sq.connect(target_path)
        tc.row_factory = sq.Row
        cat_name = snap["lines"][0]["category_name"]
        rc = tc.execute("SELECT id FROM categories WHERE name=?", (cat_name,)).fetchone()
        if rc is None:
            tc.execute("INSERT INTO categories (name, description, created_at) VALUES (?, '', ?)",
                       (cat_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            cat_id = tc.execute("SELECT id FROM categories WHERE name=?", (cat_name,)).fetchone()["id"]
        else:
            cat_id = rc["id"]
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # Insert items if missing.
        for ln in snap["lines"]:
            existing = tc.execute("SELECT id FROM items WHERE sku=?", (ln["sku"],)).fetchone()
            if existing is None:
                tc.execute(
                    "INSERT INTO items (sku, name, category_id, unit, gram_per_unit, "
                    "unit_cost, selling_price, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (ln["sku"], ln["item_name"], cat_id, ln["item_unit"],
                     ln["gram_per_unit"], ln["unit_cost"], ln["selling_price"], ts))
        # Insert recipe + bom (use new id).
        cur = tc.execute(
            "INSERT INTO ic_recipes (name, output_unit, output_qty, sale_price, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (snap["head"]["name"], snap["head"]["output_unit"],
             snap["head"]["output_qty"], snap["head"]["sale_price"], ts, ts))
        new_id = cur.lastrowid
        for ln in snap["lines"]:
            item_id = tc.execute("SELECT id FROM items WHERE sku=?", (ln["sku"],)).fetchone()["id"]
            tc.execute(
                "INSERT INTO ic_recipe_items (ic_recipe_id, item_id, qty_per_unit) "
                "VALUES (?, ?, ?)", (new_id, item_id, ln["qty_per_unit"]))
        tc.commit()
        tc.close()

    result = publish_version(m, v1, ["wh_001"], user_id=1, summary="首发", apply_func=apply)
    assert result["status"] == "complete"
    assert result["per_warehouse"][0]["status"] == "success"

    # After publish, v1 is 'published'. Saving again creates a new draft.
    snap2 = dict(snap)
    snap2["head"] = dict(snap["head"])
    snap2["head"]["sale_price"] = 300.0
    v2 = upsert_draft_version(m, "ic_recipe", ic, "rd_001", snap2, user_id=1)
    assert v2 != v1
    versions = list_versions(m, "ic_recipe", ic)
    assert versions[0]["version"] == 2
    assert versions[0]["status"] == "draft"
    # v1 still 'published' until v2 itself is published.
    assert versions[1]["version"] == 1
    assert versions[1]["status"] == "published"

    # Publishing v2 flips v1 → superseded.
    publish_version(m, v2, ["wh_001"], user_id=1, summary="v2", apply_func=apply)
    versions = list_versions(m, "ic_recipe", ic)
    assert versions[0]["status"] == "published"
    assert versions[1]["status"] == "superseded"
    m.close()


def test_publish_to_invalid_warehouse_returns_partial(tmp_path):
    import sqlite3
    from blueprints.publish_recipe_pure import (
        snapshot_recipe, upsert_draft_version, publish_version,
    )
    master, rd_db, wh_db, milk, sugar, ic = _bootstrap_two_warehouses(tmp_path)
    rd = sqlite3.connect(rd_db)
    rd.row_factory = sqlite3.Row
    snap = snapshot_recipe(rd, "ic_recipe", ic)
    rd.close()
    m = sqlite3.connect(master)
    v = upsert_draft_version(m, "ic_recipe", ic, "rd_001", snap, user_id=1)

    def bad_apply(master_conn, code, snap):
        raise RuntimeError(f"simulated failure for {code}")

    result = publish_version(m, v, ["nonexistent"], user_id=1, summary=None,
                              apply_func=bad_apply)
    assert result["status"] in ("partial", "failed")
    assert result["per_warehouse"][0]["status"] == "failed"
    assert "simulated failure" in result["per_warehouse"][0]["error_message"]
    m.close()


# ---------------------------------------------------------------------------
# Item publish / cross-warehouse sync
# ---------------------------------------------------------------------------

def test_apply_item_to_warehouse_inserts_when_missing(tmp_path):
    import sqlite3
    from blueprints.publish_recipe_pure import apply_item_to_warehouse, snapshot_item
    master, rd_db, wh_db, milk, sugar, ic = _bootstrap_two_warehouses(tmp_path)
    rd = sqlite3.connect(rd_db)
    rd.row_factory = sqlite3.Row
    snap = snapshot_item(rd, sugar)
    rd.close()

    # wh_001 has no sugar item.
    wh = sqlite3.connect(wh_db)
    wh.row_factory = sqlite3.Row
    before = wh.execute("SELECT COUNT(*) AS c FROM items WHERE sku='SKU-SUGAR'").fetchone()["c"]
    assert before == 0
    action = apply_item_to_warehouse(wh, snap, action="overwrite")
    assert action == "inserted"
    after = wh.execute("SELECT COUNT(*) AS c FROM items WHERE sku='SKU-SUGAR'").fetchone()["c"]
    assert after == 1
    row = wh.execute("SELECT * FROM items WHERE sku='SKU-SUGAR'").fetchone()
    # P0-9（2026-10-09）：价格收归主数据 —— 配方发布 INSERT 新行不再写价格
    # （走 items 默认 0），价格由 canonical 扇出统一下发。
    assert float(row["unit_cost"]) == 0.0
    assert float(row["selling_price"]) == 0.0
    wh.commit(); wh.close()


def test_apply_item_to_warehouse_overwrite_replaces_prices(tmp_path):
    """Canonical Item M2 (T12) — overwrite now updates name/unit/unit_family
    but no longer touches unit_cost / selling_price / safety_stock (§7.2).
    P0-9（2026-10-09）后价格收归主数据，由 canonical 扇出下发，配方发布
    仍不是价格通道；本测试锁定「发布不覆盖门店价」这一不变式。"""
    import sqlite3
    from blueprints.publish_recipe_pure import apply_item_to_warehouse, snapshot_item
    master, rd_db, wh_db, milk, sugar, ic = _bootstrap_two_warehouses(tmp_path)
    rd = sqlite3.connect(rd_db)
    rd.row_factory = sqlite3.Row
    snap = snapshot_item(rd, milk)  # rd milk: cost=6.0, sp=10.0
    rd.close()
    # wh_001 already has SKU-MILK with cost=4.0, sp=7.0
    wh = sqlite3.connect(wh_db)
    wh.row_factory = sqlite3.Row
    action = apply_item_to_warehouse(wh, snap, action="overwrite")
    assert action == "overwritten"
    row = wh.execute("SELECT * FROM items WHERE sku='SKU-MILK'").fetchone()
    # Behaviour change (§7.2): unit_cost / selling_price no longer overwritten
    assert float(row["unit_cost"]) == 4.0  # preserved (was 6.0 in pre-T12)
    assert float(row["selling_price"]) == 7.0
    wh.close()


def test_apply_item_to_warehouse_keep_doesnt_touch_existing(tmp_path):
    import sqlite3
    from blueprints.publish_recipe_pure import apply_item_to_warehouse, snapshot_item
    master, rd_db, wh_db, milk, sugar, ic = _bootstrap_two_warehouses(tmp_path)
    rd = sqlite3.connect(rd_db)
    rd.row_factory = sqlite3.Row
    snap = snapshot_item(rd, milk)
    rd.close()
    wh = sqlite3.connect(wh_db)
    wh.row_factory = sqlite3.Row
    action = apply_item_to_warehouse(wh, snap, action="keep")
    assert action == "kept"
    row = wh.execute("SELECT * FROM items WHERE sku='SKU-MILK'").fetchone()
    # unchanged
    assert float(row["unit_cost"]) == 4.0
    assert float(row["selling_price"]) == 7.0
    wh.close()


def test_publish_items_partial_when_one_warehouse_missing(tmp_path):
    import sqlite3
    from blueprints.publish_recipe_pure import publish_items, snapshot_item
    master, rd_db, wh_db, milk, sugar, ic = _bootstrap_two_warehouses(tmp_path)
    rd = sqlite3.connect(rd_db)
    rd.row_factory = sqlite3.Row
    m = sqlite3.connect(master)
    result = publish_items(m, rd, "rd_001", [sugar],
                           ["wh_001", "nonexistent"], user_id=1,
                           summary=None, default_action="overwrite")
    rd.close()
    assert result["status"] == "partial"
    # wh_001 succeeded for sugar; nonexistent failed.
    from blueprints.publish_recipe_pure import get_item_event_details
    details = get_item_event_details(m, result["event_id"])
    codes = {w["warehouse_code"]: w["status"] for w in details["warehouses"]}
    assert codes == {"wh_001": "success", "nonexistent": "failed"}
    # sugar should now exist in wh_001.
    wh = sqlite3.connect(wh_db)
    row = wh.execute("SELECT * FROM items WHERE sku='SKU-SUGAR'").fetchone()
    assert row is not None
    wh.close()
    m.close()


def test_snapshot_recipe_picks_up_source_type_for_polymorphic(tmp_path, monkeypatch):
    """snapshot_recipe must SELECT source_type from recipe_items so the
    apply step can skip ic_recipe lines. Regression: the snapshot dict
    used to be missing source_type, so polymorphic lines went through
    apply_item_to_warehouse with all-NULL fields and crashed on
    INSERT INTO categories (NOT NULL constraint on name).
    """
    import db as db_module
    import config as config_module
    from db import init_master_db, init_warehouse_db, migrate_warehouse_db_columns
    master = tmp_path / "master.db"
    rd = tmp_path / "rd.db"
    monkeypatch.setattr(db_module, "MASTER_DB", master)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", tmp_path)
    monkeypatch.setattr(config_module, "MASTER_DB", master)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", tmp_path)
    monkeypatch.setattr(config_module, "BASE_DIR", tmp_path)
    init_master_db()
    init_warehouse_db(rd)
    migrate_warehouse_db_columns(rd)

    ts = "2026-09-03 12:00:00"
    import sqlite3
    conn = sqlite3.connect(rd)
    conn.row_factory = sqlite3.Row
    cat = conn.execute("SELECT id FROM categories WHERE name='乳制品'").fetchone()["id"]
    conn.execute("INSERT INTO items (sku, name, category_id, unit, gram_per_unit, "
                 "unit_cost, selling_price, updated_at) "
                 "VALUES ('SKU-A', 'A', ?, '件', 1000, 1.0, 2.0, ?)", (cat, ts))
    a = conn.execute("SELECT id FROM items WHERE sku='SKU-A'").fetchone()["id"]
    conn.execute("INSERT INTO ic_recipes (name, output_unit, output_qty, sale_price, "
                 "created_at, updated_at) VALUES ('inner', 'g', 100, 50, ?, ?)", (ts, ts))
    inner = conn.execute("SELECT id FROM ic_recipes WHERE name='inner'").fetchone()["id"]
    conn.execute("INSERT INTO ic_recipe_items (ic_recipe_id, item_id, qty_per_unit) "
                 "VALUES (?, ?, 100)", (inner, a))
    conn.execute("INSERT INTO recipes (name, output_unit, output_qty, sale_price, "
                 "created_at, updated_at) VALUES ('polymorphic', 'g', 500, 30, ?, ?)",
                 (ts, ts))
    outer = conn.execute("SELECT id FROM recipes WHERE name='polymorphic'").fetchone()["id"]
    conn.execute("INSERT INTO recipe_items (recipe_id, source_type, item_id, ic_recipe_id, "
                 "qty_per_unit) VALUES (?, 'ic_recipe', NULL, ?, 2.0)", (outer, inner))
    conn.commit()

    from blueprints.publish_recipe_pure import snapshot_recipe
    snap = snapshot_recipe(conn, "recipe", outer)
    assert snap is not None
    assert snap["lines"][0]["source_type"] == "ic_recipe"
    assert snap["lines"][0]["ic_recipe_id"] == inner
    conn.close()


def test_apply_polymorphic_skips_ic_recipe_lines(tmp_path, monkeypatch):
    """_apply_recipe_snapshot_to_warehouse must skip polymorphic ic_recipe
    lines in the items loop (the recipe head still gets inserted).

    Regression: previously, polymorphic lines crashed with
    'NOT NULL constraint failed: categories.name' because the
    all-NULL item snapshot was passed to apply_item_to_warehouse.
    """
    import db as db_module
    import config as config_module
    from db import init_master_db, init_warehouse_db, migrate_warehouse_db_columns
    from blueprints.recipe_cost import _apply_recipe_snapshot_to_warehouse
    master = tmp_path / "master.db"
    rd = tmp_path / "rd.db"
    wh = tmp_path / "wh.db"
    monkeypatch.setattr(db_module, "MASTER_DB", master)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", tmp_path)
    monkeypatch.setattr(config_module, "MASTER_DB", master)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", tmp_path)
    monkeypatch.setattr(config_module, "BASE_DIR", tmp_path)
    init_master_db()
    init_warehouse_db(rd)
    init_warehouse_db(wh)
    migrate_warehouse_db_columns(rd)
    migrate_warehouse_db_columns(wh)

    ts = "2026-09-03 12:00:00"
    import sqlite3
    m = sqlite3.connect(master)
    m.row_factory = sqlite3.Row
    m.execute("INSERT INTO warehouses (code, name, db_path, warehouse_type, created_at) "
              "VALUES ('rd_001', 'R', ?, 'rd', ?)",
              (str(rd), ts))
    m.execute("INSERT INTO warehouses (code, name, db_path, warehouse_type, created_at) "
              "VALUES ('wh_001', 'W1', ?, 'storefront', ?)",
              (str(wh), ts))
    m.commit()
    rd_conn = sqlite3.connect(rd)
    rd_conn.row_factory = sqlite3.Row
    cat = rd_conn.execute("SELECT id FROM categories WHERE name='乳制品'").fetchone()["id"]
    rd_conn.execute("INSERT INTO items (sku, name, category_id, unit, gram_per_unit, "
                    "unit_cost, selling_price, updated_at) "
                    "VALUES ('SKU-A', 'A', ?, '件', 1000, 1.0, 2.0, ?)", (cat, ts))
    a = rd_conn.execute("SELECT id FROM items WHERE sku='SKU-A'").fetchone()["id"]
    rd_conn.execute("INSERT INTO ic_recipes (name, output_unit, output_qty, sale_price, "
                    "created_at, updated_at) VALUES ('inner', 'g', 100, 50, ?, ?)",
                    (ts, ts))
    inner = rd_conn.execute("SELECT id FROM ic_recipes WHERE name='inner'").fetchone()["id"]
    rd_conn.execute("INSERT INTO ic_recipe_items (ic_recipe_id, item_id, qty_per_unit) "
                    "VALUES (?, ?, 100)", (inner, a))
    rd_conn.execute("INSERT INTO recipes (name, output_unit, output_qty, sale_price, "
                    "created_at, updated_at) VALUES ('polymorphic', 'g', 500, 30, ?, ?)",
                    (ts, ts))
    outer = rd_conn.execute("SELECT id FROM recipes WHERE name='polymorphic'").fetchone()["id"]
    rd_conn.execute("INSERT INTO recipe_items (recipe_id, source_type, item_id, ic_recipe_id, "
                    "qty_per_unit) VALUES (?, 'ic_recipe', NULL, ?, 2.0)", (outer, inner))
    rd_conn.commit()

    from blueprints.publish_recipe_pure import snapshot_recipe
    snap = snapshot_recipe(rd_conn, "recipe", outer)
    rd_conn.close()

    # Apply should not raise (would have raised NOT NULL constraint failed
    # before the fix). Recipe head inserts, BOM insert skipped for ic_recipe.
    _apply_recipe_snapshot_to_warehouse(m, "wh_001", snap)

    wh_conn = sqlite3.connect(wh)
    wh_conn.row_factory = sqlite3.Row
    head = wh_conn.execute("SELECT name FROM recipes WHERE name='polymorphic'").fetchone()
    assert head is not None, "recipe head should be inserted"
    # BOM row should NOT be inserted (polymorphic line skipped).
    lines = wh_conn.execute("SELECT * FROM recipe_items").fetchall()
    assert len(lines) == 0, f"expected 0 recipe_items, got {len(lines)}"
    wh_conn.close()
    m.close()


# ---------------------------------------------------------------------------
# P0-4 —— BOM 纳管校验（docs/2026-10-09-item-master-unify-plan.md §3 P0-4）
# ---------------------------------------------------------------------------

def test_list_unmanaged_bom_items_flags_null_canonical(tmp_path):
    """canonical_id 为空即视为未纳管；纳管后不再报。"""
    from blueprints.publish_recipe_pure import list_unmanaged_bom_items
    master, rd_db, wh_db, milk, sugar, ic = _bootstrap_two_warehouses(tmp_path)
    rd = sqlite3.connect(rd_db)
    rd.row_factory = sqlite3.Row

    rows = list_unmanaged_bom_items(rd, "ic_recipe", ic)
    assert {r["sku"] for r in rows} == {"SKU-MILK", "SKU-SUGAR"}

    rd.execute("UPDATE items SET canonical_id = id")
    rd.commit()
    assert list_unmanaged_bom_items(rd, "ic_recipe", ic) == []
    rd.close()


def test_list_unmanaged_bom_items_skips_polymorphic_ic_recipe(tmp_path):
    """recipe 类型里 source_type='ic_recipe' 的行引用的是别的配方，不参与校验。"""
    from blueprints.publish_recipe_pure import list_unmanaged_bom_items
    master, rd_db, wh_db, milk, sugar, ic = _bootstrap_two_warehouses(tmp_path)
    ts = "2026-10-09 12:00:00"
    rd = sqlite3.connect(rd_db)
    rd.row_factory = sqlite3.Row
    rd.execute(
        "INSERT INTO recipes (name, output_unit, output_qty, sale_price, "
        "created_at, updated_at) VALUES ('poly', 'g', 500, 30, ?, ?)", (ts, ts))
    outer = rd.execute("SELECT id FROM recipes WHERE name='poly'").fetchone()["id"]
    rd.execute(
        "INSERT INTO recipe_items (recipe_id, source_type, item_id, ic_recipe_id, "
        "qty_per_unit) VALUES (?, 'ic_recipe', NULL, ?, 2.0)", (outer, ic))
    rd.commit()
    # 纯多态行 → 无需纳管
    assert list_unmanaged_bom_items(rd, "recipe", outer) == []

    # 混一条未纳管的 item 行 → 只报这条
    rd.execute(
        "INSERT INTO recipe_items (recipe_id, source_type, item_id, ic_recipe_id, "
        "qty_per_unit) VALUES (?, 'item', ?, NULL, 1.0)", (outer, milk))
    rd.commit()
    rows = list_unmanaged_bom_items(rd, "recipe", outer)
    assert [r["sku"] for r in rows] == ["SKU-MILK"]
    rd.close()


def test_publish_rejects_unmanaged_bom(tmp_path, monkeypatch):
    """未纳管 → POST 发布被拒（302 回原页），不产生任何 publish event。"""
    import config as config_module
    import db as db_module
    from app import create_app
    from db import init_master_db, init_warehouse_db, migrate_warehouse_db_columns

    master = tmp_path / "master.db"
    rd_db = tmp_path / "rd.db"
    wh_db = tmp_path / "wh.db"
    monkeypatch.setattr(db_module, "MASTER_DB", master)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", tmp_path)
    monkeypatch.setattr(config_module, "MASTER_DB", master)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", tmp_path)
    monkeypatch.setattr(config_module, "BASE_DIR", tmp_path)
    init_master_db()
    for p in (rd_db, wh_db):
        init_warehouse_db(p)
        migrate_warehouse_db_columns(p)

    ts = "2026-10-09 12:00:00"
    m = sqlite3.connect(master)
    m.row_factory = sqlite3.Row
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (1, 'admin', 'x', 1, ?)", (ts,))
    m.execute(
        "INSERT INTO warehouses (code, name, db_path, warehouse_type, created_at) "
        "VALUES ('rd_001', 'R&D', ?, 'rd', ?)", (str(rd_db), ts))
    m.execute(
        "INSERT INTO warehouses (code, name, db_path, warehouse_type, created_at) "
        "VALUES ('wh_001', '中央仓', ?, 'storefront', ?)", (str(wh_db), ts))
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (1, 1, 'admin')")
    m.commit()
    m.close()

    rd = sqlite3.connect(rd_db)
    rd.row_factory = sqlite3.Row
    cat = rd.execute("SELECT id FROM categories WHERE name='乳制品'").fetchone()["id"]
    rd.execute(
        "INSERT INTO items (sku, name, category_id, unit, gram_per_unit, unit_cost, "
        "selling_price, updated_at) VALUES ('SKU-MILK', '牛奶', ?, '件', 1000, 6.0, 10.0, ?)",
        (cat, ts))
    milk = rd.execute("SELECT id FROM items WHERE sku='SKU-MILK'").fetchone()["id"]
    rd.execute(
        "INSERT INTO ic_recipes (name, output_unit, output_qty, sale_price, "
        "created_at, updated_at) VALUES ('开心果1号', 'g', 740, 250, ?, ?)", (ts, ts))
    ic = rd.execute("SELECT id FROM ic_recipes WHERE name='开心果1号'").fetchone()["id"]
    rd.execute(
        "INSERT INTO ic_recipe_items (ic_recipe_id, item_id, qty_per_unit) "
        "VALUES (?, ?, 500)", (ic, milk))
    rd.commit()
    rd.close()

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = 1
        s["warehouse_id"] = 1   # rd_001

    resp = client.post(
        f"/recipe-cost/ic-recipes/{ic}/publish",
        data={"warehouse_codes": ["wh_001"]},
        follow_redirects=False,
    )
    assert resp.status_code == 302

    # 未纳管 → 一条发布事件都不该产生
    check = sqlite3.connect(master)
    check.row_factory = sqlite3.Row
    assert check.execute("SELECT COUNT(*) AS c FROM recipe_publish_events").fetchone()["c"] == 0
    check.close()
    # 目标仓也不该出现该品项
    wh = sqlite3.connect(wh_db)
    assert wh.execute("SELECT COUNT(*) AS c FROM items").fetchone()[0] == 0
    wh.close()
