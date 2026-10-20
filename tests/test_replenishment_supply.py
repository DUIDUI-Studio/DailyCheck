"""供应方式（统配 / 自采）筛选口径的测试。

背景（owner 2026-10-20 确认）：`items.is_orderable=0` 不是"暂时下不了单"，
而是**该品项已改为门店自采，不再由配送中心统配备货**。所以：

  * 统配 = DC 备货，建议量进 DC 采购单；
  * 自采 = 门店自行采购，建议量不进 DC 采购单，但看板要显示；
  * 两种方式的金额是两笔预算，**不能相加当成一张采购单**。

这组测试锁两件事：
  1. 分流后 全部 = 统配 + 自采（数量与金额都不能漏也不能重复）；
  2. 状态和建议量必须自洽 —— 状态"正常"的行不该带非零建议量。
"""
from __future__ import annotations

import pytest

from blueprints import replenishment_pure as R
from blueprints.replenishment_data import (
    SUPPLY_ALL,
    SUPPLY_CENTRAL,
    SUPPLY_SELF,
    _assemble,
)

TODAY_SERIES = [2.0] * 56          # 稳定日需求 2 件
# 有波动才有安全库存；σ>0 时目标水位会高于「提前期+周期」的天数，
# 于是出现「覆盖天数已够、但仍在目标水位之下」的区间。
LUMPY_SERIES = [4.0, 0.0] * 28     # 日均 2 件，σ≈2
WH = [
    {"code": "dc", "name": "配送中心", "warehouse_type": "distribution_center"},
    {"code": "s1", "name": "一号店", "warehouse_type": "storefront"},
]


def _item(item_id: int, qty: float, name: str, cost: float = 100.0) -> dict:
    return {
        "id": item_id,
        "sku": f"S{item_id}",
        "name": name,
        "unit": "箱",
        "quantity": qty,
        "unit_cost": cost,
        "canonical_id": item_id,
        "category": "调味酱",
        "on_order": 0.0,
    }


def _plan(wh: dict, item: dict, series, orderable: bool = True) -> dict:
    return R.plan_item(
        warehouse=wh,
        item=item,
        series=series,
        abc="A",
        orderable=orderable,
    ).as_dict()


def _rows() -> list[dict]:
    """三种典型存货状态 × 两种供应方式。"""
    return [
        # 统配 + 断货（库存 0）
        _plan(WH[1], _item(1, 0.0, "断货统配品"), TODAY_SERIES),
        # 统配 + 充足
        _plan(WH[1], _item(2, 500.0, "充足统配品"), TODAY_SERIES),
        # 自采 + 断货
        _plan(WH[1], _item(3, 0.0, "断货自采品"), TODAY_SERIES, orderable=False),
        # 自采 + 充足
        _plan(WH[1], _item(4, 500.0, "充足自采品"), TODAY_SERIES, orderable=False),
        # 统配 + 覆盖够但低于目标水位（σ 大 → SS 大）：最容易被写成
        # 「状态正常却建议订 10 箱」的那一类。
        _plan(WH[1], _item(5, 18.0, "接近目标统配品"), LUMPY_SERIES),
    ]


def test_assemble_splits_by_supply_and_parts_sum_to_whole():
    """全部 = 统配 + 自采，需订数与金额都不能漏算或重复。"""
    board = _assemble(_rows(), WH, R.DEFAULT_PARAMS, supply=SUPPLY_ALL)
    modes = {m["key"]: m for m in board["supply_modes"]}

    assert modes[SUPPLY_ALL]["action_items"] == (
        modes[SUPPLY_CENTRAL]["action_items"] + modes[SUPPLY_SELF]["action_items"]
    )
    assert modes[SUPPLY_ALL]["suggested_value"] == pytest.approx(
        modes[SUPPLY_CENTRAL]["suggested_value"] + modes[SUPPLY_SELF]["suggested_value"],
        abs=0.01,
    )
    assert modes[SUPPLY_CENTRAL]["action_items"] == 1
    assert modes[SUPPLY_SELF]["action_items"] == 1


def test_assemble_filters_rows_per_supply_mode():
    rows = _rows()
    central = _assemble(rows, WH, R.DEFAULT_PARAMS, supply=SUPPLY_CENTRAL)
    self_only = _assemble(rows, WH, R.DEFAULT_PARAMS, supply=SUPPLY_SELF)

    assert {r["supply"] for r in central["rows"]} == {SUPPLY_CENTRAL}
    assert {r["supply"] for r in self_only["rows"]} == {SUPPLY_SELF}
    # 两边的品项数加起来等于全集
    assert len(central["rows"]) + len(self_only["rows"]) == len(rows)


def test_assemble_unknown_supply_falls_back_to_all():
    """URL 上乱传 ?supply=xxx 不能把页面搞空。"""
    board = _assemble(_rows(), WH, R.DEFAULT_PARAMS, supply="nonsense")
    assert board["supply"] == SUPPLY_ALL
    assert len(board["rows"]) == 5


def test_self_items_excluded_from_dc_purchase_value():
    """自采的缺口不能混进统配金额 —— 否则 DC 会照着买自己不该备的货。"""
    board = _assemble(_rows(), WH, R.DEFAULT_PARAMS, supply=SUPPLY_ALL)
    kpi = board["kpi"]

    assert kpi["self_count"] == 1
    assert kpi["central_count"] == 1
    assert kpi["suggested_value"] == pytest.approx(
        kpi["central_value"] + kpi["self_value"], abs=0.01
    )
    # 自采那一项确实带金额，但它归 self_value 而不是 central_value
    assert kpi["self_value"] > 0
    assert kpi["central_value"] > 0


def test_healthy_item_carries_no_suggested_quantity():
    """状态「正常」却带建议量会让明细表自相矛盾（实测 30 行 / ¥6,054）。

    构造点：库存 18、日均 2、L+R=8 天 → 覆盖 9 天已算健康；
    但目标水位 = 16 + 安全库存 9.39 = 25.39，补到水位还差 7.39。
    order_quantity 会给出 8，而本周期并不下单 —— 所以必须归零。
    """
    plan = R.plan_item(
        warehouse=WH[1],
        item=_item(9, 18.0, "接近目标品"),
        series=LUMPY_SERIES,
        abc="A",
    )
    assert plan.status == R.STATUS_HEALTHY
    assert plan.target > plan.quantity          # 确实低于目标水位
    assert plan.suggested_qty == 0.0
    assert plan.suggested_value == 0.0


def test_every_row_with_quantity_is_an_order_row():
    """全表不变式：只有 critical / due 的行允许有非零建议量。"""
    board = _assemble(_rows(), WH, R.DEFAULT_PARAMS, supply=SUPPLY_ALL)
    assert len(board["rows"]) == 5
    offenders = [
        r for r in board["rows"]
        if (r["suggested_qty"] or 0) > 0
        and r["status"] not in (R.STATUS_CRITICAL, R.STATUS_DUE)
    ]
    assert offenders == []
