"""recipe_cost 纯函数测试：单位换算与基础成本行。"""
from decimal import Decimal


def test_qty_to_stock_units_grams_converts_to_stock():
    """启用克的品项：qty 视为克 → 除 gram_per_unit。"""
    from blueprints._helpers import qty_to_stock_units
    item = {"gram_per_unit": 50.0, "aux_rate": 50.0, "unit": "件"}
    assert qty_to_stock_units(100, item) == Decimal("2.00")


def test_qty_to_stock_units_no_grams_returns_qty_unchanged():
    """未启用克的品项：qty 即库存单位。"""
    from blueprints._helpers import qty_to_stock_units
    item = {"gram_per_unit": 0.0, "aux_rate": 0.0, "unit": "件"}
    assert qty_to_stock_units(7.5, item) == Decimal("7.50")


def test_qty_to_stock_units_zero_grams_per_unit():
    """gram_per_unit=0 但 aux_rate>0 不应触发克换算（防御）。"""
    from blueprints._helpers import qty_to_stock_units
    item = {"gram_per_unit": 0.0, "aux_rate": 10.0, "unit": "件"}
    assert qty_to_stock_units(50, item) == Decimal("50.00")


def test_qty_to_stock_units_2dp_quantize():
    """结果量化到 2 位小数。"""
    from blueprints._helpers import qty_to_stock_units
    item = {"gram_per_unit": 3.0, "aux_rate": 3.0, "unit": "件"}
    assert qty_to_stock_units(10, item) == Decimal("3.33")


def test_line_cost_basic_uses_unit_cost_and_selling_price():
    """单行成本：qty × unit_cost 与 qty × selling_price。"""
    from blueprints.recipe_cost_pure import line_cost
    item = {"unit_cost": 5.0, "selling_price": 10.0, "gram_per_unit": 0.0}
    r = line_cost(2.0, item)
    assert r["cost_purchase"] == Decimal("10.00")
    assert r["cost_selling"] == Decimal("20.00")


def test_line_cost_with_grams_converts_first():
    """启用克的品项：qty=克 → 先转库存单位再乘。"""
    from blueprints.recipe_cost_pure import line_cost
    item = {"unit_cost": 5.0, "selling_price": 10.0, "gram_per_unit": 50.0}
    r = line_cost(100.0, item)
    assert r["cost_purchase"] == Decimal("10.00")
    assert r["cost_selling"] == Decimal("20.00")


def test_line_cost_temp_selling_overrides_selling_price():
    """temp_selling_price 覆盖 selling_price。"""
    from blueprints.recipe_cost_pure import line_cost
    item = {"unit_cost": 5.0, "selling_price": 10.0, "gram_per_unit": 0.0}
    r = line_cost(2.0, item, temp_selling_price=Decimal("7.00"))
    assert r["cost_purchase"] == Decimal("10.00")
    assert r["cost_selling"] == Decimal("14.00")


def test_ic_recipe_cost_empty_recipe_returns_zero():
    """空冰激凌配方：cost 全 0、margin None。"""
    import sqlite3
    from blueprints.recipe_cost_pure import ic_recipe_cost
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE ic_recipes (id INTEGER PRIMARY KEY, sale_price REAL, output_qty REAL);
        CREATE TABLE ic_recipe_items (
            id INTEGER PRIMARY KEY, ic_recipe_id INTEGER, item_id INTEGER, qty_per_unit REAL);
        CREATE TABLE items (
            id INTEGER PRIMARY KEY, unit_cost REAL, selling_price REAL,
            gram_per_unit REAL, aux_rate REAL, unit TEXT);
        INSERT INTO ic_recipes VALUES (1, 25.0, 100.0);
        """
    )
    conn.commit()
    result = ic_recipe_cost(conn, 1)
    assert result["cost_purchase"] == Decimal("0.00")
    assert result["cost_selling"] == Decimal("0.00")
    assert result["sale_price"] == 25.0
    assert result["margin_purchase"] == 1.0  # cost 0 → 100% empty margin
    conn.close()


def test_ic_recipe_cost_sums_3_items_with_grams():
    """3 原料含克重 → cost_purchase = sum(qty/gram_per_unit × unit_cost)。"""
    import sqlite3
    from blueprints.recipe_cost_pure import ic_recipe_cost
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE ic_recipes (id INTEGER PRIMARY KEY, sale_price REAL, output_qty REAL);
        CREATE TABLE ic_recipe_items (
            id INTEGER PRIMARY KEY, ic_recipe_id INTEGER, item_id INTEGER, qty_per_unit REAL);
        CREATE TABLE items (
            id INTEGER PRIMARY KEY, unit_cost REAL, selling_price REAL,
            gram_per_unit REAL, aux_rate REAL, unit TEXT);
        INSERT INTO ic_recipes VALUES (1, 25.0, 100.0);
        INSERT INTO items VALUES (1, 5.0, 10.0, 50.0, 50.0, '件');
        INSERT INTO items VALUES (2, 3.0, 6.0, 30.0, 30.0, '件');
        INSERT INTO items VALUES (3, 2.0, 4.0, 0.0, 0.0, '件');
        INSERT INTO ic_recipe_items VALUES (1, 1, 1, 100.0);
        INSERT INTO ic_recipe_items VALUES (2, 1, 2, 60.0);
        INSERT INTO ic_recipe_items VALUES (3, 1, 3, 1.5);
        """
    )
    conn.commit()
    r = ic_recipe_cost(conn, 1)
    assert r["cost_purchase"] == Decimal("19.00")
    assert r["cost_selling"] == Decimal("38.00")
    assert abs(r["margin_purchase"] - 0.24) < 0.001
    assert len(r["lines"]) == 3
    conn.close()


def test_ic_recipe_cost_temp_prices_override():
    """temp_prices 字典按 item_id 覆盖。"""
    import sqlite3
    from decimal import Decimal
    from blueprints.recipe_cost_pure import ic_recipe_cost
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE ic_recipes (id INTEGER PRIMARY KEY, sale_price REAL, output_qty REAL);
        CREATE TABLE ic_recipe_items (
            id INTEGER PRIMARY KEY, ic_recipe_id INTEGER, item_id INTEGER, qty_per_unit REAL);
        CREATE TABLE items (
            id INTEGER PRIMARY KEY, unit_cost REAL, selling_price REAL,
            gram_per_unit REAL, aux_rate REAL, unit TEXT);
        INSERT INTO ic_recipes VALUES (1, 0, 100.0);
        INSERT INTO items VALUES (1, 5.0, 10.0, 0.0, 0.0, '件');
        INSERT INTO ic_recipe_items VALUES (1, 1, 1, 2.0);
        """
    )
    conn.commit()
    r = ic_recipe_cost(conn, 1, temp_prices={1: Decimal("7.00")})
    assert r["cost_purchase"] == Decimal("10.00")
    assert r["cost_selling"] == Decimal("14.00")
    conn.close()


def test_recipe_cost_all_items():
    """出品配方：原料全是 item。"""
    import sqlite3
    from decimal import Decimal
    from blueprints.recipe_cost_pure import recipe_cost
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE recipes (
            id INTEGER PRIMARY KEY, sale_price REAL, output_qty REAL);
        CREATE TABLE recipe_items (
            id INTEGER PRIMARY KEY, recipe_id INTEGER, source_type TEXT,
            item_id INTEGER, ic_recipe_id INTEGER, qty_per_unit REAL);
        CREATE TABLE items (
            id INTEGER PRIMARY KEY, unit_cost REAL, selling_price REAL,
            gram_per_unit REAL, aux_rate REAL, unit TEXT);
        CREATE TABLE ic_recipes (
            id INTEGER PRIMARY KEY, sale_price REAL, output_qty REAL);
        CREATE TABLE ic_recipe_items (
            id INTEGER PRIMARY KEY, ic_recipe_id INTEGER, item_id INTEGER, qty_per_unit REAL);
        INSERT INTO recipes VALUES (10, 15.0, 1.0);
        INSERT INTO items VALUES (1, 2.0, 4.0, 30.0, 30.0, '件');
        INSERT INTO items VALUES (2, 1.0, 2.0, 0.0, 0.0, '件');
        INSERT INTO recipe_items VALUES (1, 10, 'item', 1, NULL, 30.0);
        INSERT INTO recipe_items VALUES (2, 10, 'item', 2, NULL, 3.0);
        """
    )
    conn.commit()
    r = recipe_cost(conn, 10)
    assert r["cost_purchase"] == Decimal("5.00")
    assert r["cost_selling"] == Decimal("10.00")
    assert abs(r["margin_purchase"] - 0.6667) < 0.001
    conn.close()


def test_recipe_cost_references_ic_recipe_by_sale_price():
    """出品引用冰激凌配方：按 ic_recipe cost × (qty / output_qty) 计入，
    不展开到底层 items（避免双重计算）。"""
    import sqlite3
    from decimal import Decimal
    from blueprints.recipe_cost_pure import recipe_cost
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE recipes (
            id INTEGER PRIMARY KEY, sale_price REAL, output_qty REAL);
        CREATE TABLE recipe_items (
            id INTEGER PRIMARY KEY, recipe_id INTEGER, source_type TEXT,
            item_id INTEGER, ic_recipe_id INTEGER, qty_per_unit REAL);
        CREATE TABLE items (
            id INTEGER PRIMARY KEY, unit_cost REAL, selling_price REAL,
            gram_per_unit REAL, aux_rate REAL, unit TEXT);
        CREATE TABLE ic_recipes (
            id INTEGER PRIMARY KEY, sale_price REAL, output_qty REAL);
        CREATE TABLE ic_recipe_items (
            id INTEGER PRIMARY KEY, ic_recipe_id INTEGER, item_id INTEGER, qty_per_unit REAL);
        -- ic_recipe 1: 每份 (100g) 含 50g item 100 (unit_cost=5, gpu=50) = 5
        --              + 60g item 101 (unit_cost=3, gpu=30) = 6 → 合计 11
        INSERT INTO ic_recipes VALUES (1, 25.0, 100.0);
        INSERT INTO items VALUES (100, 5.0, 10.0, 50.0, 50.0, '件');
        INSERT INTO items VALUES (101, 3.0, 6.0, 30.0, 30.0, '件');
        INSERT INTO ic_recipe_items VALUES (1, 1, 100, 50.0);
        INSERT INTO ic_recipe_items VALUES (2, 1, 101, 60.0);
        -- recipe 10: 柠檬茶, 售价 15, 引 ic_recipe 1 用量 50g
        INSERT INTO recipes VALUES (10, 15.0, 1.0);
        INSERT INTO recipe_items VALUES (1, 10, 'ic_recipe', NULL, 1, 50.0);
        """
    )
    conn.commit()
    r = recipe_cost(conn, 10)
    # ic_recipe cost_purchase = 50/50*5 + 60/30*3 = 5+6 = 11
    # per_unit_purchase = 11/100 = 0.11
    # line_purchase = 50 * 0.11 = 5.50
    assert r["cost_purchase"] == Decimal("5.50")
    # ic_recipe cost_selling = 50/50*10 + 60/30*6 = 10+12 = 22
    # per_unit_selling = 22/100 = 0.22
    # line_selling = 50 * 0.22 = 11.00
    assert r["cost_selling"] == Decimal("11.00")
    assert abs(r["margin_purchase"] - 0.6333) < 0.001
    conn.close()


def test_recipe_cost_empty_returns_zero():
    """空出品配方。"""
    import sqlite3
    from blueprints.recipe_cost_pure import recipe_cost
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE recipes (id INTEGER PRIMARY KEY, sale_price REAL, output_qty REAL);
        CREATE TABLE recipe_items (
            id INTEGER PRIMARY KEY, recipe_id INTEGER, source_type TEXT,
            item_id INTEGER, ic_recipe_id INTEGER, qty_per_unit REAL);
        CREATE TABLE items (
            id INTEGER PRIMARY KEY, unit_cost REAL, selling_price REAL,
            gram_per_unit REAL, aux_rate REAL, unit TEXT);
        CREATE TABLE ic_recipes (id INTEGER PRIMARY KEY, sale_price REAL, output_qty REAL);
        INSERT INTO recipes VALUES (10, 0.0, 1.0);
        """
    )
    conn.commit()
    r = recipe_cost(conn, 10)
    assert r["cost_purchase"] == Decimal("0.00")
    assert r["cost_selling"] == Decimal("0.00")
    assert r["margin_purchase"] is None
    conn.close()


def test_recipe_cost_with_temp_prices_in_mixed_lines():
    """出品含 ic_recipe 引用 + item + temp_prices 混合。"""
    import sqlite3
    from decimal import Decimal
    from blueprints.recipe_cost_pure import recipe_cost
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE recipes (id INTEGER PRIMARY KEY, sale_price REAL, output_qty REAL);
        CREATE TABLE recipe_items (
            id INTEGER PRIMARY KEY, recipe_id INTEGER, source_type TEXT,
            item_id INTEGER, ic_recipe_id INTEGER, qty_per_unit REAL);
        CREATE TABLE items (
            id INTEGER PRIMARY KEY, unit_cost REAL, selling_price REAL,
            gram_per_unit REAL, aux_rate REAL, unit TEXT);
        CREATE TABLE ic_recipes (id INTEGER PRIMARY KEY, sale_price REAL, output_qty REAL);
        CREATE TABLE ic_recipe_items (
            id INTEGER PRIMARY KEY, ic_recipe_id INTEGER, item_id INTEGER, qty_per_unit REAL);
        -- ic_recipe 1: 100g output, item 1 unit_cost=5, gpu=50, qty=100
        -- cost_purchase = 100/50*5 = 10; cost_selling = 100/50*10 = 20
        INSERT INTO ic_recipes VALUES (1, 25.0, 100.0);
        INSERT INTO items VALUES (1, 5.0, 10.0, 50.0, 50.0, '件');
        INSERT INTO ic_recipe_items VALUES (1, 1, 1, 100.0);
        -- recipe 10: 售价 15
        INSERT INTO recipes VALUES (10, 15.0, 1.0);
        INSERT INTO items VALUES (2, 2.0, 4.0, 0.0, 0.0, '件');
        -- 原料: ic_recipe 1 用量 50g + item 2 用量 2 件
        INSERT INTO recipe_items VALUES (1, 10, 'ic_recipe', NULL, 1, 50.0);
        INSERT INTO recipe_items VALUES (2, 10, 'item', 2, NULL, 2.0);
        """
    )
    conn.commit()
    r = recipe_cost(conn, 10, temp_prices={2: Decimal("3.00")})
    # cost_purchase: ic_recipe 部分 50 * (10/100) = 5；item 部分 2*2 = 4；合计 9
    assert r["cost_purchase"] == Decimal("9.00")
    # cost_selling: ic_recipe 50 * (20/100) = 10；item 2*3 = 6；合计 16
    assert r["cost_selling"] == Decimal("16.00")
    conn.close()
