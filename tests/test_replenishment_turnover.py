"""库存周转率（各仓库货量分布卡片）的测试。

锁三件事：
  1. 周转率与「可支撑天数」**严格互逆**（turns = 365 ÷ days）—— 卡片上同时
     出现这两个数，对不上读者就会怀疑其中一个算错了；
  2. 偏差判定相对**本仓自己的政策水位**（d̄(L+R)+SS），不是全链均值，
     因为 DC 与门店的 L+R 不同；
  3. 零消耗 / 零库存这类边界不能吐出 0 次/年当成"正常周转"。
"""
from __future__ import annotations

import re

import pytest

from blueprints import replenishment_pure as R
from blueprints.replenishment_data import SCOPE_ALL, _assemble

WH = [
    {"code": "dc", "name": "配送中心", "warehouse_type": "distribution_center"},
    {"code": "s1", "name": "一号店", "warehouse_type": "storefront"},
]
SERIES = [2.0] * 56          # 稳定日需求 2 箱


def _item(item_id: int, qty: float, cost: float = 100.0) -> dict:
    return {
        "id": item_id,
        "sku": f"S{item_id}",
        "name": f"品项{item_id}",
        "unit": "箱",
        "quantity": qty,
        "unit_cost": cost,
        "canonical_id": item_id,
        "category": "调味酱",
        "on_order": 0.0,
    }


def _plan(wh: dict, qty: float, cost: float = 100.0) -> dict:
    return R.plan_item(
        warehouse=wh, item=_item(1, qty, cost), series=SERIES, abc="A"
    ).as_dict()


# ---------------------------------------------------------------------------
# 公式本身
# ---------------------------------------------------------------------------


def test_turns_is_reciprocal_of_days():
    """周转率与库存天数必须互为倒数（365 ÷ days），否则卡片自相矛盾。"""
    tv = R.inventory_turnover(stock_value=12_000.0, monthly_value=9_000.0, target_value=6_000.0)
    # 日均 300 元 → 12000 / 300 = 40 天 → 365 / 40 = 9.125 次/年
    assert tv.days == pytest.approx(40.0, abs=0.05)
    assert tv.turns == pytest.approx(365.0 / 40.0, rel=0.002)
    # 天数保留 1 位、周转保留 2 位，回算误差必须在显示精度内
    assert tv.turns == pytest.approx(365.0 / tv.days, rel=0.002)


def test_target_days_comes_from_policy_level():
    """目标天数由政策水位反推：目标库存 ÷ 日均消耗。"""
    tv = R.inventory_turnover(stock_value=12_000.0, monthly_value=9_000.0, target_value=6_000.0)
    assert tv.target_days == pytest.approx(20.0, abs=0.05)      # 6000 / 300
    assert tv.target_turns == pytest.approx(365.0 / 20.0, rel=0.002)


def test_stock_equal_to_target_is_reasonable():
    tv = R.inventory_turnover(stock_value=6_000.0, monthly_value=9_000.0, target_value=6_000.0)
    assert tv.gap == pytest.approx(0.0, abs=0.001)
    assert tv.verdict == "合理"
    assert tv.tone == "ok"


def test_holding_twice_target_reads_as_over_stocked():
    """压了一倍于政策水位的货 → 周转只有目标的一半 → 偏高。"""
    tv = R.inventory_turnover(stock_value=12_000.0, monthly_value=9_000.0, target_value=6_000.0)
    assert tv.gap == pytest.approx(-0.5, abs=0.01)
    assert tv.verdict == "偏高"
    assert tv.tone == "warn"


def test_holding_below_target_reads_as_tight():
    """只有目标水位 40% 的货 → 周转快 150% → 偏紧（断货风险）。"""
    tv = R.inventory_turnover(stock_value=2_400.0, monthly_value=9_000.0, target_value=6_000.0)
    assert tv.gap == pytest.approx(1.5, abs=0.02)
    assert tv.verdict == "偏紧"
    assert tv.tone == "info"


def test_zero_consumption_with_stock_is_dead_not_normal():
    """有货但窗口内零消耗：周转是 0，这是呆滞，不能显示成"周转正常"。"""
    tv = R.inventory_turnover(stock_value=5_000.0, monthly_value=0.0, target_value=0.0)
    assert tv.turns == 0.0
    assert tv.days is None          # 分母为 0，天数无上界
    assert tv.verdict == "零消耗"
    assert tv.tone == "warn"


def test_zero_stock_has_no_turnover_bound():
    """零库存时周转无上界 —— 不能写成 0（那会被读成"卖不动"）。"""
    tv = R.inventory_turnover(stock_value=0.0, monthly_value=9_000.0, target_value=6_000.0)
    assert tv.turns is None
    assert tv.days == 0.0
    assert tv.verdict == "零库存"


def test_empty_bucket_is_undetermined():
    tv = R.inventory_turnover(stock_value=0.0, monthly_value=0.0, target_value=0.0)
    assert tv.turns is None
    assert tv.tone == "muted"


def test_missing_target_yields_numbers_but_no_verdict():
    """亏了政策水位（老数据没 target）仍要给出实际周转，只是不下判断。"""
    tv = R.inventory_turnover(stock_value=3_000.0, monthly_value=9_000.0, target_value=0.0)
    assert tv.turns == pytest.approx(365.0 / 10.0, abs=0.1)
    assert tv.gap is None
    assert tv.verdict == "—"


def test_turnover_payload_is_json_safe():
    payload = R.inventory_turnover(3_000.0, 9_000.0, 6_000.0).as_dict()
    assert set(payload) == {
        "days", "turns", "target_days", "target_turns", "gap", "tone", "verdict", "note"
    }
    assert isinstance(payload["tone"], str)


# ---------------------------------------------------------------------------
# Rollup 聚合
# ---------------------------------------------------------------------------


def test_rollup_sums_target_and_idle_value():
    rows = [
        {"quantity": 10.0, "unit_cost": 100.0, "rate": 2.0, "target": 20.0, "status": "healthy"},
        {"quantity": 5.0, "unit_cost": 100.0, "rate": 0.0, "target": 0.0, "status": "no_signal"},
    ]
    ru = R.rollup("t", rows)
    assert ru.target_value == pytest.approx(2_000.0)        # 20 × 100
    assert ru.stock_value == pytest.approx(1_500.0)
    # 零消耗那行的 500 元计入呆滞，正是它把周转拖慢
    assert ru.idle_value == pytest.approx(500.0)


# ---------------------------------------------------------------------------
# 装配层：每个仓的目标不同（L+R 不同），不能用全链均值
# ---------------------------------------------------------------------------


def _board_rows():
    """两个仓各持一份同样的货，库存恰好等于各自的政策目标水位。"""
    rows = []
    for wh in WH:
        probe = _plan(wh, qty=0.0)
        rows.append(_plan(wh, qty=probe["target"]))
    return rows


def test_each_warehouse_gets_its_own_target_turnover():
    board = _assemble(_board_rows(), WH, R.DEFAULT_PARAMS, scope=SCOPE_ALL)
    by_code = {w["code"]: w for w in board["warehouses"]}

    dc, store = by_code["dc"], by_code["s1"]
    assert dc["turnover"]["turns"] is not None
    assert store["turnover"]["turns"] is not None

    # 库存刚好等于目标水位 → 实际周转 = 目标周转，判定"合理"
    assert dc["turnover"]["verdict"] == "合理"
    assert store["turnover"]["verdict"] == "合理"

    # DC 的 L+R（3+7）比门店（1+7）长，压同样的货周转必然更慢
    assert dc["turnover"]["turns"] < store["turnover"]["turns"]
    assert dc["turnover"]["target_turns"] < store["turnover"]["target_turns"]


def test_board_kpi_carries_turnover():
    board = _assemble(_board_rows(), WH, R.DEFAULT_PARAMS, scope=SCOPE_ALL)
    kpi = board["kpi"]
    assert kpi["turnover"]["turns"] is not None
    assert kpi["target_value"] > 0
    # 整体周转介于两仓之间（各仓库存等权时不可能越界）
    assert (
        board["warehouses"][0]["turnover"]["turns"]
        <= kpi["turnover"]["turns"]
        <= board["warehouses"][1]["turnover"]["turns"]
    )


def test_warehouse_turnover_ignores_scope_filter():
    """卡片永远展示全部仓，即使看板切成单仓视角 —— 否则对比就没了。"""
    board = _assemble(_board_rows(), WH, R.DEFAULT_PARAMS, scope="dc")
    assert len(board["warehouses"]) == 1
    assert board["warehouses"][0]["turnover"]["turns"] is not None


# ---------------------------------------------------------------------------
# 模板：卡片上真的把周转率渲染出来了
# ---------------------------------------------------------------------------


def test_card_renders_turnover_block(logged_client):
    """渲染级断言：卡片区块存在，且数字与判定真的来自模型。

    纯函数全绿但模板漏字段（拼错 `tv.turns`、改字段名忘了同步）是这类改动
    最常见的翻车点，光测模型抓不到。这里刻意造一个「有货、无消耗」的仓：
    库存 ¥1,000、消耗 0 → 周转必须渲染成 `0.0`，判定必须是「零消耗」。
    """
    from tests.conftest import _seed_item

    client, wh_path = logged_client
    _seed_item(wh_path, "turnoverItem", qty=10, unit_cost=100)
    body = client.get("/forecast").data.decode("utf-8")

    assert "fc-whturn" in body
    assert "库存周转" in body
    assert "次/年" in body
    # 有货无消耗：0 次/年 + 「零消耗」，不是空白也不是"正常"
    assert re.search(r'fc-whturn-val">\s*0\.0', body)
    # 徽标本身必须是「零消耗」。别只断言 "零消耗" in body —— 呆滞那行的
    # title 提示里也有这三个字，会把断言变成永真。
    assert 'fc-badge tone-warn">零消耗</span>' in body
