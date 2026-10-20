"""视角（全链 / 配送中心 / 单店）筛选口径的测试。

背景（owner 2026-10-20）：看板要能切换「站在谁的立场看该订什么」。

  * 全链     = 所有仓汇总；
  * 配送中心 = DC 向供应商采购的量（需求 = 各门店消耗之和，拉动口径）；
  * 单店     = 该店向 DC 订货的量。

语义要点：DC 的量与门店的量是同一批货的**两层**，不能相加当成一张采购单；
但同一层内，各仓之和必须等于全链。

这组测试锁三件事：
  1. 各仓之和 = 全链（同层可比）；
  2. 视角筛选发生在聚合之前 —— KPI 不能还是全量的；
  3. 视角与供应方式正交：单仓视角下「统配 + 自采 = 全部」仍然成立。
"""
from __future__ import annotations

import pytest

from blueprints import replenishment_pure as R
from blueprints.replenishment_data import (
    SCOPE_ALL,
    SUPPLY_ALL,
    SUPPLY_CENTRAL,
    SUPPLY_SELF,
    _assemble,
)

SERIES = [2.0] * 56
WH = [
    {"code": "dc", "name": "配送中心", "warehouse_type": "distribution_center"},
    {"code": "s1", "name": "一号店", "warehouse_type": "storefront"},
    {"code": "s2", "name": "二号店", "warehouse_type": "storefront"},
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


def _plan(wh: dict, item: dict, orderable: bool = True, source: str = "own") -> dict:
    return R.plan_item(
        warehouse=wh,
        item=item,
        series=SERIES,
        abc="A",
        orderable=orderable,
        demand_source=source,
    ).as_dict()


def _rows() -> list[dict]:
    """DC 1 项 + 两店各 2 项，其中一店带一个自采品项。"""
    return [
        _plan(WH[0], _item(1, 0.0, "DC 断货品"), source="downstream"),
        _plan(WH[0], _item(2, 500.0, "DC 充足品")),
        _plan(WH[1], _item(3, 0.0, "一号店断货品")),
        _plan(WH[1], _item(4, 0.0, "一号店自采品"), orderable=False),
        _plan(WH[2], _item(5, 300.0, "二号店充足品")),
    ]


def test_scopes_include_chain_and_every_warehouse():
    board = _assemble(_rows(), WH, R.DEFAULT_PARAMS, supply=SUPPLY_ALL)
    keys = [s["key"] for s in board["scopes"]]

    assert keys == [SCOPE_ALL, "dc", "s1", "s2"]
    assert board["scope"] == SCOPE_ALL
    assert board["scope_label"] == "全链"


def test_warehouse_scope_values_sum_to_chain():
    """同层可比：各仓之和必须等于全链，否则切换视角会凭空多出或少掉钱。"""
    board = _assemble(_rows(), WH, R.DEFAULT_PARAMS, supply=SUPPLY_ALL)
    per_wh = [s for s in board["scopes"] if s["key"] != SCOPE_ALL]
    chain = board["scopes"][0]

    assert sum(s["action_items"] for s in per_wh) == chain["action_items"]
    assert sum(s["suggested_value"] for s in per_wh) == pytest.approx(
        chain["suggested_value"], abs=0.01
    )
    assert sum(s["sku_count"] for s in per_wh) == chain["sku_count"]


def test_store_scope_only_returns_that_store_rows_and_kpis():
    board = _assemble(_rows(), WH, R.DEFAULT_PARAMS, supply=SUPPLY_ALL, scope="s1")

    assert {r["warehouse_code"] for r in board["rows"]} == {"s1"}
    assert [w["code"] for w in board["warehouses"]] == ["s1"]
    # KPI 必须跟着缩小，不能还是全量（这是只过滤表格的典型错）
    assert board["kpi"]["item_count"] == len(board["rows"])
    assert board["kpi"]["stock_value"] == pytest.approx(0.0, abs=0.01)
    assert board["scope_is_store"] is True
    assert board["scope_is_dc"] is False


def test_dc_scope_is_flagged_and_keeps_downstream_demand():
    board = _assemble(_rows(), WH, R.DEFAULT_PARAMS, supply=SUPPLY_ALL, scope="dc")

    assert board["scope_is_dc"] is True
    assert board["scope_is_store"] is False
    assert {r["warehouse_code"] for r in board["rows"]} == {"dc"}
    # DC 行的需求口径仍是「下游拉动」，切视角不改计算
    sources = {r["demand_source"] for r in board["rows"]}
    assert sources == {"downstream", "own"}


def test_unknown_scope_falls_back_to_chain():
    board = _assemble(_rows(), WH, R.DEFAULT_PARAMS, supply=SUPPLY_ALL, scope="wh_999")
    assert board["scope"] == SCOPE_ALL
    assert len(board["rows"]) == 5


def test_scope_and_supply_are_orthogonal():
    """单仓视角下，供应方式的小计仍要相加等于该仓全部。"""
    board = _assemble(_rows(), WH, R.DEFAULT_PARAMS, supply=SUPPLY_ALL, scope="dc")
    modes = {m["key"]: m for m in board["supply_modes"]}

    assert modes[SUPPLY_ALL]["item_count"] == 2          # 只有 DC 的两项
    assert modes[SUPPLY_ALL]["action_items"] == 1        # 只有 1 项要订
    assert modes[SUPPLY_ALL]["suggested_value"] == pytest.approx(
        modes[SUPPLY_CENTRAL]["suggested_value"] + modes[SUPPLY_SELF]["suggested_value"],
        abs=0.01,
    )

    central = _assemble(_rows(), WH, R.DEFAULT_PARAMS, supply=SUPPLY_CENTRAL, scope="s1")
    assert {r["supply"] for r in central["rows"]} == {SUPPLY_CENTRAL}
    assert {r["warehouse_code"] for r in central["rows"]} == {"s1"}


def test_store_demand_share_sums_to_one():
    """DC 视角的「各门店需求分布」份额必须合计 100%。"""
    board = _assemble(_rows(), WH, R.DEFAULT_PARAMS, supply=SUPPLY_ALL)
    demand = board["store_demand"]

    assert [d["code"] for d in demand] == ["s1", "s2"] or [d["code"] for d in demand] == ["s2", "s1"]
    assert sum(d["share"] for d in demand) == pytest.approx(1.0, abs=0.001)
