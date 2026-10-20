"""品类 breakdown（默认汇总 / 展开到品项）的口径测试。

看板「各品类总货量」默认只显示品类行，点开才展示该品类下的品项与状态。
展开里的品项是模板用 `rows | selectattr(category == 品类名)` 现算的，
所以品类行上的 `sku_count` 必须**等于**该品类的行数 —— 否则会出现
「写着 60 项，展开只有 58 条」，用户会以为丢数据。
"""
from __future__ import annotations

from blueprints import replenishment_pure as R
from blueprints.replenishment_data import SUPPLY_ALL, _assemble

SERIES = [2.0] * 56
NO_DEMAND = [0.0] * 56      # 无消耗记录：必须仍然出现在品类里，不能被静默丢条
WH = [
    {"code": "dc", "name": "配送中心", "warehouse_type": "distribution_center"},
    {"code": "s1", "name": "一号店", "warehouse_type": "storefront"},
]


def _item(item_id: int, qty: float, name: str, category: str, cost: float = 100.0) -> dict:
    return {
        "id": item_id,
        "sku": f"S{item_id}",
        "name": name,
        "unit": "箱",
        "quantity": qty,
        "unit_cost": cost,
        "canonical_id": item_id,
        "category": category,
        "on_order": 0.0,
    }


def _rows() -> list[dict]:
    plans = [
        (WH[0], _item(1, 0.0, "DC 断货品", "奶浆")),
        (WH[0], _item(2, 500.0, "DC 充足品", "奶浆")),
        (WH[1], _item(3, 0.0, "一号店断货品", "包材")),
        (WH[1], _item(4, 0.0, "一号店自采品", "包材")),
        (WH[1], _item(5, 300.0, "一号店充足品", "辅料")),
    ]
    out = [
        R.plan_item(warehouse=wh, item=it, series=SERIES, abc="A").as_dict()
        for wh, it in plans
    ]
    # 无消耗记录的品项：品类行必须把它算进品项数，展开也要能查到（否则会「少一行」）
    out.append(
        R.plan_item(
            warehouse=WH[1],
            item=_item(6, 12.0, "无消耗品", "辅料"),
            series=NO_DEMAND,
            abc="C",
        ).as_dict()
    )
    return out


def test_category_sku_count_equals_expanded_rows():
    """品类行上的品项数，必须等于展开后能看到的条数。"""
    board = _assemble(_rows(), WH, R.DEFAULT_PARAMS, supply=SUPPLY_ALL)

    actual: dict[str, int] = {}
    for r in board["rows"]:
        actual[r["category"]] = actual.get(r["category"], 0) + 1

    for c in board["categories"]:
        assert c["sku_count"] == actual[c["name"]], c["name"]


def test_category_item_counts_sum_to_total():
    """各品类相加等于总数：展开不会漏项，也不会重复计。"""
    board = _assemble(_rows(), WH, R.DEFAULT_PARAMS, supply=SUPPLY_ALL)
    assert sum(c["sku_count"] for c in board["categories"]) == len(board["rows"])
    assert sum(c["sku_count"] for c in board["categories"]) == board["kpi"]["item_count"]


def test_category_breakdown_follows_supply_filter():
    """切到统配/自采时，品类的品项数也要跟着缩 —— 展开里不能还留着被过滤掉的行。"""
    board = _assemble(_rows(), WH, R.DEFAULT_PARAMS, supply="self")
    assert all(r["supply"] == "self" for r in board["rows"])
    assert sum(c["sku_count"] for c in board["categories"]) == len(board["rows"])


def test_category_breakdown_follows_scope():
    """切到单仓时，展开内容是那个仓的品项，不是全链的。"""
    board = _assemble(_rows(), WH, R.DEFAULT_PARAMS, supply=SUPPLY_ALL, scope="s1")
    assert {r["warehouse_code"] for r in board["rows"]} == {"s1"}
    assert sum(c["sku_count"] for c in board["categories"]) == len(board["rows"])
    assert sum(c["sku_count"] for c in board["categories"]) == 4
    # 奶浆只有 DC 有，单店视角不该出现这个品类
    assert "奶浆" not in [c["name"] for c in board["categories"]]
