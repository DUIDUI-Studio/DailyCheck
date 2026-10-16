"""研发中心配方模块升级 — 路由级测试（2026-10-16）。

覆盖：
- D1：出品配方 ic_recipe 行 qty 语义 = 克（保存后 output_qty 与成本口径一致）
- D2：发布含 ic_recipe 引用行的出品配方 → 目标仓递归重建基料 + 引用行完整
- F1：/recipe-cost/overview 页面 + CSV + 权限
- F2：/recipe-cost/import 三步流（选仓 → 预览 → 导入）+ 权限 + audit
"""
import sqlite3
from datetime import datetime


def _ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _setup(tmp_path, monkeypatch, source_products=False):
    """master + rd_001 + wh_001（可选在 wh_001 建 products/product_bom 存量配方）。

    RD 品项：牛奶（canonical 101, gpu=1000, uc=3, sp=4）
             淡奶油（canonical 102, gpu=1000, uc=8, sp=10）
    RD ic_recipe「基料」：牛奶 800g + 淡奶油 200g → output 1000g,
        cost_purchase = 0.8*3 + 0.2*8 = 4.0, cost_selling = 0.8*4+0.2*10 = 5.2
    """
    import config as config_module
    import db as db_module
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

    ts = _ts()
    m = sqlite3.connect(master)
    m.execute("INSERT INTO users (id, username, password_hash, is_admin, created_at) "
              "VALUES (1, 'admin', 'x', 1, ?)", (ts,))
    m.execute("INSERT INTO warehouses (id, code, name, db_path, warehouse_type, "
              "created_at) VALUES (1, 'rd_001', 'R&D', ?, 'rd', ?)", (str(rd_db), ts))
    m.execute("INSERT INTO warehouses (id, code, name, db_path, warehouse_type, "
              "created_at) VALUES (2, 'wh_001', '门店一', ?, 'storefront', ?)",
              (str(wh_db), ts))
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) "
              "VALUES (1, 1, 'admin'), (1, 2, 'admin')")
    # canonical 主数据（发布纳管校验要求 canonical_id 非空）
    canon = {}
    for sku, name in (("R-MILK", "牛奶"), ("R-CREAM", "淡奶油")):
        cur = m.execute(
            "INSERT INTO canonical_items (canonical_sku, name, unit, status, "
            "created_from, created_at, updated_at) "
            "VALUES (?, ?, '件', 'active', 'rd_manual', ?, ?)", (f"C-{sku}", name, ts, ts))
        canon[sku] = cur.lastrowid
    m.commit()
    m.close()

    rd = sqlite3.connect(rd_db)
    rd.row_factory = sqlite3.Row
    cat = rd.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()["id"]
    rd.execute("INSERT INTO items (sku, name, category_id, unit, gram_per_unit, "
               "unit_cost, selling_price, canonical_id, updated_at) "
               "VALUES ('R-MILK', '牛奶', ?, '盒', 1000, 3.0, 4.0, ?, ?)",
               (cat, canon["R-MILK"], ts))
    rd.execute("INSERT INTO items (sku, name, category_id, unit, gram_per_unit, "
               "unit_cost, selling_price, canonical_id, updated_at) "
               "VALUES ('R-CREAM', '淡奶油', ?, '盒', 1000, 8.0, 10.0, ?, ?)",
               (cat, canon["R-CREAM"], ts))
    milk = rd.execute("SELECT id FROM items WHERE sku='R-MILK'").fetchone()["id"]
    cream = rd.execute("SELECT id FROM items WHERE sku='R-CREAM'").fetchone()["id"]
    rd.execute("INSERT INTO ic_recipes (name, output_unit, output_qty, sale_price, "
               "created_at, updated_at) VALUES ('基料', 'g', 1000, 0, ?, ?)", (ts, ts))
    ic = rd.execute("SELECT id FROM ic_recipes WHERE name='基料'").fetchone()["id"]
    rd.execute("INSERT INTO ic_recipe_items (ic_recipe_id, item_id, qty_per_unit) "
               "VALUES (?, ?, 800), (?, ?, 200)", (ic, milk, ic, cream))
    rd.commit()
    rd.close()

    if source_products:
        # 门店 wh_001 存量配方：products/product_bom（sku 与 RD 不同、canonical 对齐）
        wh = sqlite3.connect(wh_db)
        wh.row_factory = sqlite3.Row
        wcat = wh.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()["id"]
        wh.execute("INSERT INTO items (sku, name, category_id, unit, gram_per_unit, "
                   "unit_cost, selling_price, canonical_id, updated_at) "
                   "VALUES ('S-MILK', '纯牛奶', ?, '盒', 1000, 3.0, 4.0, ?, ?)",
                   (wcat, canon["R-MILK"], ts))
        wh.execute("INSERT INTO items (sku, name, category_id, unit, gram_per_unit, "
                   "unit_cost, selling_price, canonical_id, updated_at) "
                   "VALUES ('S-CREAM', '稀奶油', ?, '盒', 1000, 8.0, 10.0, ?, ?)",
                   (wcat, canon["R-CREAM"], ts))
        s_milk = wh.execute("SELECT id FROM items WHERE sku='S-MILK'").fetchone()["id"]
        s_cream = wh.execute("SELECT id FROM items WHERE sku='S-CREAM'").fetchone()["id"]
        wh.execute("INSERT INTO products (id, name, created_at) "
                   "VALUES (1, '门店香草', ?)", (ts,))
        wh.execute("INSERT INTO product_bom (product_id, item_id, qty_per_unit) "
                   "VALUES (1, ?, 700), (1, ?, 300)", (s_milk, s_cream))
        wh.commit()
        wh.close()

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = 1
        s["warehouse_id"] = 1  # rd_001
    return client, master, rd_db, wh_db, ic


# ---------------------------------------------------------------------------
# D1: ic_recipe 行 qty = 克
# ---------------------------------------------------------------------------

def _create_serving_recipe(client, ic_id, ic_grams=120):
    """通过 UI 表单建一个出品配方「小杯」：ic 引用行 ic_grams 克。"""
    return client.post("/recipe-cost/recipes/new", data={
        "name": "小杯",
        "note": "",
        "output_unit": "g",
        "sale_price": "6",
        "bom_row_id": [""],
        "bom_source_type": ["ic_recipe"],
        "bom_item_id": [""],
        "bom_ic_recipe_id": [str(ic_id)],
        "bom_qty": [str(ic_grams)],
        "bom_delete": ["0"],
    }, follow_redirects=False)


def test_d1_saving_ic_line_qty_is_grams(tmp_path, monkeypatch):
    """小杯 = 120g 基料 → output_qty 必须是 120（克），不是 120×1000。"""
    client, _master, rd_db, _wh, ic = _setup(tmp_path, monkeypatch)
    r = _create_serving_recipe(client, ic, 120)
    assert r.status_code == 302
    rd = sqlite3.connect(rd_db)
    rd.row_factory = sqlite3.Row
    row = rd.execute("SELECT output_qty FROM recipes WHERE name='小杯'").fetchone()
    assert float(row["output_qty"]) == 120.0
    rd.close()


def test_d1_cost_api_consistent_with_output_qty(tmp_path, monkeypatch):
    """api/cost 的成本与 output_qty 口径一致：120g 基料 → 采购 ≈ 4.0/1000×120。"""
    client, _master, rd_db, _wh, ic = _setup(tmp_path, monkeypatch)
    _create_serving_recipe(client, ic, 120)
    rd = sqlite3.connect(rd_db)
    rid = rd.execute("SELECT id FROM recipes WHERE name='小杯'").fetchone()[0]
    rd.close()
    r = client.get(f"/recipe-cost/api/cost/recipe/{rid}")
    assert r.status_code == 200
    data = r.get_json()
    assert data["cost_purchase"] == 0.48   # 4.0/1000g × 120g
    assert data["cost_selling"] == 0.62    # 5.2/1000g × 120g（round 0.01）


# ---------------------------------------------------------------------------
# D2: 发布多态出品配方
# ---------------------------------------------------------------------------

def test_d2_publish_recipe_with_ic_line_recreates_base_in_target(tmp_path, monkeypatch):
    """发布含 ic 引用的出品配方 → 目标仓有「基料」ic_recipe + 完整引用行。"""
    client, master, rd_db, wh_db, ic = _setup(tmp_path, monkeypatch)
    _create_serving_recipe(client, ic, 120)
    rd = sqlite3.connect(rd_db)
    rid = rd.execute("SELECT id FROM recipes WHERE name='小杯'").fetchone()[0]
    rd.close()

    r = client.post(f"/recipe-cost/recipes/{rid}/publish", data={
        "warehouse_codes": ["wh_001"],
        "summary": "d2 test",
    }, follow_redirects=False)
    assert r.status_code == 302

    wh = sqlite3.connect(wh_db)
    wh.row_factory = sqlite3.Row
    # 目标仓被递归重建了「基料」
    base = wh.execute("SELECT * FROM ic_recipes WHERE name='基料'").fetchone()
    assert base is not None
    base_lines = wh.execute(
        "SELECT i.sku, l.qty_per_unit FROM ic_recipe_items l "
        "JOIN items i ON i.id=l.item_id WHERE l.ic_recipe_id=? ORDER BY i.sku",
        (base["id"],)).fetchall()
    assert [(row["sku"], float(row["qty_per_unit"])) for row in base_lines] == \
        [("R-CREAM", 200.0), ("R-MILK", 800.0)]
    # 出品配方引用行完整（不再被 skip）
    serv = wh.execute("SELECT * FROM recipes WHERE name='小杯'").fetchone()
    assert serv is not None
    ref = wh.execute(
        "SELECT source_type, ic_recipe_id, qty_per_unit FROM recipe_items "
        "WHERE recipe_id=?", (serv["id"],)).fetchone()
    assert ref["source_type"] == "ic_recipe"
    assert int(ref["ic_recipe_id"]) == int(base["id"])
    assert float(ref["qty_per_unit"]) == 120.0
    wh.close()

    m = sqlite3.connect(master)
    m.row_factory = sqlite3.Row
    ev = m.execute("SELECT status FROM recipe_publish_events "
                   "ORDER BY id DESC LIMIT 1").fetchone()
    assert ev["status"] == "complete"
    m.close()


def test_d2_publish_reuses_existing_base_by_name(tmp_path, monkeypatch):
    """目标仓已有同名基料 → 复用其 id，不重复建、不覆盖门店数据。"""
    client, master, rd_db, wh_db, ic = _setup(tmp_path, monkeypatch)
    wh = sqlite3.connect(wh_db)
    wh.execute("INSERT INTO ic_recipes (name, output_unit, output_qty, sale_price, "
               "created_at, updated_at) VALUES ('基料', 'g', 999, 88, ?, ?)",
               (_ts(), _ts()))
    wh.commit()
    existing_id = wh.execute("SELECT id FROM ic_recipes WHERE name='基料'").fetchone()[0]
    wh.close()

    _create_serving_recipe(client, ic, 120)
    rd = sqlite3.connect(rd_db)
    rid = rd.execute("SELECT id FROM recipes WHERE name='小杯'").fetchone()[0]
    rd.close()
    r = client.post(f"/recipe-cost/recipes/{rid}/publish",
                    data={"warehouse_codes": ["wh_001"]}, follow_redirects=False)
    assert r.status_code == 302

    wh = sqlite3.connect(wh_db)
    wh.row_factory = sqlite3.Row
    n = wh.execute("SELECT COUNT(*) FROM ic_recipes WHERE name='基料'").fetchone()[0]
    assert n == 1  # 未重复建
    head = wh.execute("SELECT sale_price, output_qty FROM ic_recipes "
                      "WHERE id=?", (existing_id,)).fetchone()
    assert float(head["sale_price"]) == 88.0  # 门店数据未被覆盖
    assert float(head["output_qty"]) == 999.0
    ref = wh.execute("SELECT ic_recipe_id FROM recipe_items ri "
                     "JOIN recipes r ON r.id=ri.recipe_id WHERE r.name='小杯'").fetchone()
    assert int(ref["ic_recipe_id"]) == existing_id
    wh.close()


# ---------------------------------------------------------------------------
# F1: overview
# ---------------------------------------------------------------------------

def test_overview_page_lists_both_kinds(tmp_path, monkeypatch):
    client, _m, _rd, _wh, ic = _setup(tmp_path, monkeypatch)
    _create_serving_recipe(client, ic, 120)
    r = client.get("/recipe-cost/overview")
    assert r.status_code == 200
    html = r.data.decode("utf-8")
    assert "基料" in html and "小杯" in html
    assert "未定价" in html  # 基料 sale_price=0


def test_overview_kind_filter(tmp_path, monkeypatch):
    client, _m, _rd, _wh, ic = _setup(tmp_path, monkeypatch)
    _create_serving_recipe(client, ic, 120)
    r = client.get("/recipe-cost/overview?kind=recipe")
    html = r.data.decode("utf-8")
    assert "小杯" in html
    # 基料行不在表体内（tab 链接文案「冰激凌配方（基料）」出现在 option 里，
    # 因此断言 ic_recipe 编辑链接不存在）
    assert f"/recipe-cost/ic-recipes/{ic}/edit" not in html


def test_overview_csv_export(tmp_path, monkeypatch):
    client, _m, _rd, _wh, ic = _setup(tmp_path, monkeypatch)
    _create_serving_recipe(client, ic, 120)
    r = client.get("/recipe-cost/overview/export.csv")
    assert r.status_code == 200
    body = r.data.decode("utf-8")
    assert "供货毛利率%" in body and "基料" in body and "小杯" in body


def test_landing_redirects_to_overview(tmp_path, monkeypatch):
    client, _m, _rd, _wh, _ic = _setup(tmp_path, monkeypatch)
    r = client.get("/recipe-cost/")
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/recipe-cost/overview")


# ---------------------------------------------------------------------------
# F2: import
# ---------------------------------------------------------------------------

def test_import_index_lists_storefront(tmp_path, monkeypatch):
    client, _m, _rd, _wh, _ic = _setup(tmp_path, monkeypatch, source_products=True)
    r = client.get("/recipe-cost/import")
    assert r.status_code == 200
    html = r.data.decode("utf-8")
    assert "wh_001" in html and "门店一" in html


def test_import_preview_shows_matched_lines(tmp_path, monkeypatch):
    client, _m, _rd, _wh, _ic = _setup(tmp_path, monkeypatch, source_products=True)
    r = client.get("/recipe-cost/import/preview?warehouse=wh_001")
    assert r.status_code == 200
    html = r.data.decode("utf-8")
    assert "门店香草" in html
    # 源 sku 与 RD 不同名，但 canonical 匹配 → 状态列显示「可导入」
    assert "可导入" in html
    # 匹配到的 RD 品项名展示在 BOM 明细里
    assert "牛奶" in html


def test_import_execute_creates_ic_recipe_with_draft(tmp_path, monkeypatch):
    client, master, rd_db, _wh, _ic = _setup(tmp_path, monkeypatch, source_products=True)
    r = client.post("/recipe-cost/import", data={
        "warehouse": "wh_001",
        "conflict_mode": "skip",
        "product_ids": ["1"],
    }, follow_redirects=False)
    assert r.status_code == 302

    rd = sqlite3.connect(rd_db)
    rd.row_factory = sqlite3.Row
    head = rd.execute("SELECT * FROM ic_recipes WHERE name='门店香草'").fetchone()
    assert head is not None
    assert float(head["output_qty"]) == 1000.0  # 700 + 300
    lines = rd.execute(
        "SELECT i.sku, l.qty_per_unit FROM ic_recipe_items l "
        "JOIN items i ON i.id=l.item_id WHERE l.ic_recipe_id=? ORDER BY i.sku",
        (head["id"],)).fetchall()
    assert [(row["sku"], float(row["qty_per_unit"])) for row in lines] == \
        [("R-CREAM", 300.0), ("R-MILK", 700.0)]
    new_ic_id = head["id"]
    rd.close()

    # 草稿版本已建（可直接走发布链路）
    m = sqlite3.connect(master)
    m.row_factory = sqlite3.Row
    v = m.execute("SELECT status FROM recipe_versions WHERE recipe_type='ic_recipe' "
                  "AND recipe_id=?", (new_ic_id,)).fetchone()
    assert v is not None and v["status"] == "draft"
    m.close()


def test_import_conflict_skip_keeps_existing(tmp_path, monkeypatch):
    client, _m, rd_db, _wh, _ic = _setup(tmp_path, monkeypatch, source_products=True)
    # RD 先有同名配方（不同克数）
    rd = sqlite3.connect(rd_db)
    rd.execute("INSERT INTO ic_recipes (name, output_unit, output_qty, sale_price, "
               "created_at, updated_at) VALUES ('门店香草', 'g', 111, 9, ?, ?)",
               (_ts(), _ts()))
    rd.commit()
    rd.close()

    client.post("/recipe-cost/import", data={
        "warehouse": "wh_001", "conflict_mode": "skip", "product_ids": ["1"],
    })
    rd = sqlite3.connect(rd_db)
    rows = rd.execute("SELECT output_qty FROM ic_recipes WHERE name='门店香草'").fetchall()
    assert len(rows) == 1
    assert float(rows[0][0]) == 111.0  # 未被覆盖
    rd.close()


def test_import_requires_selection(tmp_path, monkeypatch):
    client, _m, _rd, _wh, _ic = _setup(tmp_path, monkeypatch, source_products=True)
    r = client.post("/recipe-cost/import", data={"warehouse": "wh_001"})
    assert r.status_code == 302  # flash 重定向，不 500


def test_import_bad_warehouse_redirects(tmp_path, monkeypatch):
    client, _m, _rd, _wh, _ic = _setup(tmp_path, monkeypatch, source_products=True)
    r = client.post("/recipe-cost/import", data={
        "warehouse": "no_such_wh", "product_ids": ["1"]})
    assert r.status_code == 302
