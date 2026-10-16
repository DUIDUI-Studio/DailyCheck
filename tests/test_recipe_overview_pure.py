"""overview_rows / sort_overview_rows（F1 全品牌成本总览）纯函数测试。"""
import sqlite3
from datetime import datetime

import pytest

from blueprints.recipe_cost_pure import (
    OVERVIEW_SORTABLE,
    overview_rows,
    sort_overview_rows,
)

TS = datetime(2026, 10, 16, 12, 0, 0).strftime("%Y-%m-%d %H:%M:%S")

SCHEMA = """
CREATE TABLE categories (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE items (id INTEGER PRIMARY KEY, sku TEXT, name TEXT, category_id INTEGER,
    unit TEXT DEFAULT '件', gram_per_unit REAL DEFAULT 0, aux_rate REAL DEFAULT 0,
    unit_cost REAL DEFAULT 0, selling_price REAL DEFAULT 0);
CREATE TABLE ic_recipes (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
    note TEXT, output_unit TEXT DEFAULT 'g', output_qty REAL DEFAULT 0,
    sale_price REAL DEFAULT 0, created_at TEXT, updated_at TEXT);
CREATE TABLE ic_recipe_items (id INTEGER PRIMARY KEY AUTOINCREMENT,
    ic_recipe_id INTEGER, item_id INTEGER, qty_per_unit REAL);
CREATE TABLE recipes (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
    note TEXT, output_unit TEXT DEFAULT 'g', output_qty REAL DEFAULT 0,
    sale_price REAL DEFAULT 0, sale_price_updated_at TEXT,
    created_at TEXT, updated_at TEXT);
CREATE TABLE recipe_items (id INTEGER PRIMARY KEY AUTOINCREMENT, recipe_id INTEGER,
    source_type TEXT, item_id INTEGER, ic_recipe_id INTEGER, qty_per_unit REAL);
"""


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "wh.db")
    c.executescript(SCHEMA)
    c.execute("INSERT INTO categories (id, name) VALUES (1, '乳制品')")
    # 原料：gram_per_unit=1000（1kg=1盒），采购 3、销售 4
    c.execute("INSERT INTO items (id, sku, name, category_id, gram_per_unit, "
              "unit_cost, selling_price) VALUES (1, 'M', '牛奶', 1, 1000, 3.0, 4.0)")
    # ic_recipe「基料」：1000g 牛奶 → cost_purchase=3, cost_selling=4, 售价 10
    c.execute("INSERT INTO ic_recipes (id, name, output_qty, sale_price, "
              "created_at, updated_at) VALUES (1, '基料', 1000, 10, ?, ?)", (TS, TS))
    c.execute("INSERT INTO ic_recipe_items (ic_recipe_id, item_id, qty_per_unit) "
              "VALUES (1, 1, 1000)")
    # recipe「小杯」：120g 基料（ic 引用）+ 售价 6
    c.execute("INSERT INTO recipes (id, name, output_qty, sale_price, "
              "created_at, updated_at) VALUES (1, '小杯', 120, 6, ?, ?)", (TS, TS))
    c.execute("INSERT INTO recipe_items (recipe_id, source_type, ic_recipe_id, "
              "qty_per_unit) VALUES (1, 'ic_recipe', 1, 120)")
    c.commit()
    yield c
    c.close()


class TestOverviewRows:
    def test_includes_both_kinds(self, conn):
        rows = overview_rows(conn)
        assert {r["name"] for r in rows} == {"基料", "小杯"}
        kinds = {r["name"]: r["kind"] for r in rows}
        assert kinds == {"基料": "ic_recipe", "小杯": "recipe"}

    def test_kind_filter(self, conn):
        assert [r["name"] for r in overview_rows(conn, "ic_recipe")] == ["基料"]
        assert [r["name"] for r in overview_rows(conn, "recipe")] == ["小杯"]

    def test_ic_row_numbers(self, conn):
        row = [r for r in overview_rows(conn) if r["name"] == "基料"][0]
        # 1000g 牛奶 / gram_per_unit 1000 = 1 盒 → 采购 3、销售 4
        assert row["cost_purchase"] == pytest.approx(3.0)
        assert row["cost_selling"] == pytest.approx(4.0)
        assert row["sale_price"] == 10.0
        # 零售毛利 = (10-3)/10 = 0.7；供货毛利 = (4-3)/4 = 0.25；供货利润 = 1
        assert row["margin_purchase"] == pytest.approx(0.7)
        assert row["margin_supply"] == pytest.approx(0.25)
        assert row["profit"] == pytest.approx(1.0)

    def test_serving_row_uses_ic_per_gram_cost(self, conn):
        """D1 口径：小杯 120g 基料 = 120 × (每克成本)。"""
        row = [r for r in overview_rows(conn) if r["name"] == "小杯"][0]
        assert row["output_qty"] == 120.0
        # 每克采购 = 3/1000 → 120g = 0.36；每克销售 = 4/1000 → 0.48
        assert row["cost_purchase"] == pytest.approx(0.36, abs=0.01)
        assert row["cost_selling"] == pytest.approx(0.48, abs=0.01)
        # 零售毛利 = (6 - 0.36)/6 = 0.94
        assert row["margin_purchase"] == pytest.approx((6 - 0.36) / 6, abs=0.01)

    def test_unpriced_flag(self, conn):
        conn.execute("UPDATE recipes SET sale_price=0 WHERE id=1")
        conn.commit()
        row = [r for r in overview_rows(conn) if r["name"] == "小杯"][0]
        assert row["priced"] is False
        assert row["margin_purchase"] is None

    def test_empty_tables(self, conn):
        conn.execute("DELETE FROM ic_recipes")
        conn.execute("DELETE FROM recipes")
        conn.commit()
        assert overview_rows(conn) == []


class TestSortOverviewRows:
    def test_sort_by_profit_desc(self, conn):
        rows = overview_rows(conn)
        s = sort_overview_rows(rows, "profit", desc=True)
        assert [r["name"] for r in s] == ["基料", "小杯"]  # 1.0 > 0.12
        s2 = sort_overview_rows(rows, "profit", desc=False)
        assert [r["name"] for r in s2] == ["小杯", "基料"]

    def test_none_values_last_regardless_of_dir(self, conn):
        conn.execute("UPDATE recipes SET sale_price=0 WHERE id=1")
        conn.commit()
        rows = overview_rows(conn)  # 小杯 margin_purchase=None
        for desc in (True, False):
            s = sort_overview_rows(rows, "margin_purchase", desc=desc)
            assert s[-1]["name"] == "小杯"

    def test_unknown_sort_key_falls_back_to_name(self, conn):
        rows = overview_rows(conn)
        s = sort_overview_rows(rows, "'; DROP TABLE items;--", desc=True)
        assert {r["name"] for r in rows} == {"基料", "小杯"}  # 数据未损
        assert s  # 正常返回

    def test_sortable_whitelist_covers_columns(self):
        assert {"name", "profit", "margin_purchase", "margin_supply"} <= OVERVIEW_SORTABLE
