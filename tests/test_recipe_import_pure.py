"""门店存量配方导入纯函数测试（recipe_import_pure）。

夹具：直接建两个 sqlite 文件（源门店 + RD），手工建最小 schema
（products/product_bom/items/categories + ic_recipes/ic_recipe_items）。
"""
import sqlite3
from datetime import datetime

import pytest

from blueprints.recipe_import_pure import import_products, scan_source_products

TS = datetime(2026, 10, 16, 12, 0, 0).strftime("%Y-%m-%d %H:%M:%S")

SOURCE_SCHEMA = """
CREATE TABLE categories (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
    description TEXT, created_at TEXT);
CREATE TABLE items (id INTEGER PRIMARY KEY, sku TEXT, name TEXT NOT NULL,
    category_id INTEGER, unit TEXT DEFAULT '件', gram_per_unit REAL DEFAULT 0,
    unit_cost REAL DEFAULT 0, selling_price REAL DEFAULT 0, canonical_id INTEGER);
CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
    unit TEXT NOT NULL DEFAULT '件', note TEXT, created_at TEXT NOT NULL);
CREATE TABLE product_bom (id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL,
    item_id INTEGER NOT NULL, qty_per_unit REAL NOT NULL);
"""

RD_SCHEMA = """
CREATE TABLE categories (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
    description TEXT, created_at TEXT);
CREATE TABLE items (id INTEGER PRIMARY KEY, sku TEXT, name TEXT NOT NULL,
    category_id INTEGER, unit TEXT DEFAULT '件', gram_per_unit REAL DEFAULT 0,
    unit_cost REAL DEFAULT 0, selling_price REAL DEFAULT 0, canonical_id INTEGER);
CREATE TABLE ic_recipes (id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE, note TEXT, output_unit TEXT NOT NULL DEFAULT 'g',
    output_qty REAL NOT NULL DEFAULT 100, sale_price REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE ic_recipe_items (id INTEGER PRIMARY KEY AUTOINCREMENT,
    ic_recipe_id INTEGER NOT NULL, item_id INTEGER NOT NULL,
    qty_per_unit REAL NOT NULL, UNIQUE(ic_recipe_id, item_id),
    FOREIGN KEY (ic_recipe_id) REFERENCES ic_recipes(id) ON DELETE CASCADE);
CREATE TABLE recipes (id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE, note TEXT, output_unit TEXT DEFAULT 'g',
    output_qty REAL DEFAULT 0, sale_price REAL DEFAULT 0,
    sale_price_updated_at TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE recipe_items (id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id INTEGER NOT NULL, source_type TEXT NOT NULL, item_id INTEGER,
    ic_recipe_id INTEGER, qty_per_unit REAL NOT NULL);
"""


@pytest.fixture
def dbs(tmp_path):
    src = sqlite3.connect(tmp_path / "src.db")
    rd = sqlite3.connect(tmp_path / "rd.db")
    src.executescript(SOURCE_SCHEMA)
    rd.executescript(RD_SCHEMA)
    # 源门店：品类 + 2 原料 + 1 产品（BOM 2 行）
    src.execute("INSERT INTO categories (id, name) VALUES (1, '乳制品')")
    src.execute("INSERT INTO items (id, sku, name, category_id, gram_per_unit, "
                "unit_cost, selling_price, canonical_id) "
                "VALUES (1, 'S-MILK', '纯牛奶', 1, 1000, 3.0, 4.0, 101)")
    src.execute("INSERT INTO items (id, sku, name, category_id, gram_per_unit, "
                "unit_cost, selling_price, canonical_id) "
                "VALUES (2, 'S-CREAM', '淡奶油', 1, 1000, 8.0, 10.0, 102)")
    src.execute("INSERT INTO products (id, name, created_at) "
                "VALUES (1, '香草冰淇淋', ?)", (TS,))
    src.execute("INSERT INTO product_bom (product_id, item_id, qty_per_unit) "
                "VALUES (1, 1, 800), (1, 2, 200)")
    src.commit()
    # RD：品类 + 2 原料（canonical 对齐 101/102，sku 不同 —— 验证 canonical 优先）
    rd.execute("INSERT INTO categories (id, name) VALUES (1, '乳制品')")
    rd.execute("INSERT INTO items (id, sku, name, category_id, gram_per_unit, "
                "unit_cost, selling_price, canonical_id) "
                "VALUES (10, 'R-MILK', '纯牛奶', 1, 1000, 3.2, 4.5, 101)")
    rd.execute("INSERT INTO items (id, sku, name, category_id, gram_per_unit, "
                "unit_cost, selling_price, canonical_id) "
                "VALUES (20, 'R-CREAM', '淡奶油', 1, 1000, 8.5, 11.0, 102)")
    rd.commit()
    yield src, rd
    src.close()
    rd.close()


class TestScanSourceProducts:
    def test_basic_scan_matches_by_canonical(self, dbs):
        src, rd = dbs
        prods = scan_source_products(src, rd)
        assert len(prods) == 1
        p = prods[0]
        assert p["name"] == "香草冰淇淋"
        assert p["bom_count"] == 2
        assert p["total_grams"] == 1000.0
        assert p["importable"] is True
        assert p["conflict"] is False
        # canonical 优先匹配到 RD item 10/20（sku 不同也能匹配）
        matched = {ln["item_name"]: ln["matched_item_id"] for ln in p["lines"]}
        assert matched == {"纯牛奶": 10, "淡奶油": 20}

    def test_unmatched_line_blocks_import(self, dbs):
        src, rd = dbs
        # 源加一个 RD 没有的原料（canonical NULL + sku 不存在）
        src.execute("INSERT INTO items (id, sku, name, category_id, canonical_id) "
                    "VALUES (3, 'S-ORPHAN', '孤儿原料', 1, NULL)")
        src.execute("INSERT INTO product_bom (product_id, item_id, qty_per_unit) "
                    "VALUES (1, 3, 10)")
        src.commit()
        prods = scan_source_products(src, rd)
        p = prods[0]
        assert p["importable"] is False
        orphan = [ln for ln in p["lines"] if ln["item_name"] == "孤儿原料"][0]
        assert orphan["matched_item_id"] is None

    def test_sku_fallback_when_no_canonical(self, dbs):
        src, rd = dbs
        # RD 行 canonical 置空、sku 与源一致 → 走 sku 兜底
        rd.execute("UPDATE items SET canonical_id=NULL, sku='S-MILK' WHERE id=10")
        rd.commit()
        src.execute("UPDATE items SET canonical_id=NULL WHERE id=1")
        src.commit()
        prods = scan_source_products(src, rd)
        milk = [ln for ln in prods[0]["lines"] if ln["item_name"] == "纯牛奶"][0]
        assert milk["matched_item_id"] == 10

    def test_conflict_detected(self, dbs):
        src, rd = dbs
        rd.execute("INSERT INTO ic_recipes (name, output_unit, output_qty, "
                   "sale_price, created_at, updated_at) "
                   "VALUES ('香草冰淇淋', 'g', 1000, 0, ?, ?)", (TS, TS))
        rd.commit()
        prods = scan_source_products(src, rd)
        assert prods[0]["conflict"] is True


class TestImportProducts:
    def test_import_writes_ic_recipe_and_bom(self, dbs):
        src, rd = dbs
        prods = scan_source_products(src, rd)
        result = import_products(rd, prods, source_warehouse_code="wh_001",
                                 now_fn=lambda: TS)
        assert len(result["imported"]) == 1
        _pid, new_id, name = result["imported"][0]
        assert name == "香草冰淇淋"
        head = rd.execute("SELECT * FROM ic_recipes WHERE id=?", (new_id,)).fetchone()
        assert head[1] == "香草冰淇淋"  # name
        assert float(head[4]) == 1000.0  # output_qty = Σ 克数
        assert float(head[5]) == 0.0  # sale_price 待补
        lines = rd.execute(
            "SELECT item_id, qty_per_unit FROM ic_recipe_items "
            "WHERE ic_recipe_id=? ORDER BY item_id", (new_id,)).fetchall()
        assert [(r[0], float(r[1])) for r in lines] == [(10, 800.0), (20, 200.0)]

    def test_conflict_skip(self, dbs):
        src, rd = dbs
        rd.execute("INSERT INTO ic_recipes (name, output_unit, output_qty, "
                   "sale_price, created_at, updated_at) "
                   "VALUES ('香草冰淇淋', 'g', 500, 20, ?, ?)", (TS, TS))
        rd.commit()
        prods = scan_source_products(src, rd)
        result = import_products(rd, prods, conflict_mode="skip", now_fn=lambda: TS)
        assert result["imported"] == []
        assert len(result["skipped"]) == 1
        # 原配方未被改动
        head = rd.execute("SELECT output_qty, sale_price FROM ic_recipes "
                          "WHERE name='香草冰淇淋'").fetchone()
        assert float(head[0]) == 500.0

    def test_conflict_overwrite(self, dbs):
        src, rd = dbs
        rd.execute("INSERT INTO ic_recipes (name, output_unit, output_qty, "
                   "sale_price, created_at, updated_at) "
                   "VALUES ('香草冰淇淋', 'g', 500, 20, ?, ?)", (TS, TS))
        old_id = rd.execute("SELECT id FROM ic_recipes").fetchone()[0]
        rd.execute("INSERT INTO ic_recipe_items (ic_recipe_id, item_id, qty_per_unit) "
                   "VALUES (?, 10, 500)", (old_id,))
        rd.commit()
        prods = scan_source_products(src, rd)
        result = import_products(rd, prods, conflict_mode="overwrite",
                                 now_fn=lambda: TS)
        assert len(result["imported"]) == 1
        n = rd.execute("SELECT COUNT(*) FROM ic_recipes WHERE name='香草冰淇淋'").fetchone()[0]
        assert n == 1
        # 旧 BOM 行被级联清掉（新配方 2 行、克数正确）
        lines = rd.execute("SELECT item_id, qty_per_unit FROM ic_recipe_items "
                           "ORDER BY item_id").fetchall()
        assert [(r[0], float(r[1])) for r in lines] == [(10, 800.0), (20, 200.0)]

    def test_overwrite_blocked_when_referenced_by_recipe(self, dbs):
        src, rd = dbs
        rd.execute("INSERT INTO ic_recipes (name, output_unit, output_qty, "
                   "sale_price, created_at, updated_at) "
                   "VALUES ('香草冰淇淋', 'g', 500, 20, ?, ?)", (TS, TS))
        old_id = rd.execute("SELECT id FROM ic_recipes").fetchone()[0]
        rd.execute("INSERT INTO recipes (name, output_unit, output_qty, sale_price, "
                   "created_at, updated_at) VALUES ('小杯', 'g', 120, 15, ?, ?)",
                   (TS, TS))
        recipe_id = rd.execute("SELECT id FROM recipes").fetchone()[0]
        rd.execute("INSERT INTO recipe_items (recipe_id, source_type, ic_recipe_id, "
                   "qty_per_unit) VALUES (?, 'ic_recipe', ?, 120)",
                   (recipe_id, old_id))
        rd.commit()
        prods = scan_source_products(src, rd)
        result = import_products(rd, prods, conflict_mode="overwrite",
                                 now_fn=lambda: TS)
        assert result["imported"] == []
        assert len(result["rejected"]) == 1
        assert "出品配方引用" in result["rejected"][0][2]

    def test_unimportable_product_rejected(self, dbs):
        src, rd = dbs
        src.execute("INSERT INTO items (id, sku, name, category_id, canonical_id) "
                    "VALUES (3, 'S-ORPHAN', '孤儿原料', 1, NULL)")
        src.execute("INSERT INTO products (id, name, created_at) "
                    "VALUES (2, '残缺产品', ?)", (TS,))
        src.execute("INSERT INTO product_bom (product_id, item_id, qty_per_unit) "
                    "VALUES (2, 3, 10)")
        src.commit()
        prods = scan_source_products(src, rd)
        result = import_products(rd, prods, now_fn=lambda: TS)
        assert len(result["imported"]) == 1  # 香草冰淇淋照常导入
        assert len(result["rejected"]) == 1
        assert result["rejected"][0][1] == "残缺产品"
        assert "孤儿原料" in result["rejected"][0][2]

    def test_invalid_conflict_mode_raises(self, dbs):
        src, rd = dbs
        prods = scan_source_products(src, rd)
        with pytest.raises(ValueError):
            import_products(rd, prods, conflict_mode="bogus")

    def test_no_products_no_writes(self, dbs):
        _src, rd = dbs
        result = import_products(rd, [], now_fn=lambda: TS)
        assert result == {"imported": [], "skipped": [], "rejected": []}
        assert rd.execute("SELECT COUNT(*) FROM ic_recipes").fetchone()[0] == 0
