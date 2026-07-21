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
