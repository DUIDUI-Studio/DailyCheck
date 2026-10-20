"""Unit tests for blueprints/replenishment_pure.py.

纯函数层不打 DB、不起 Flask，所以这里全部用构造数据断言公式本身。
公式与业务口径的对应关系见 docs/2026-10-20-replenishment-board.md。
"""
from __future__ import annotations

import math
from datetime import date

import pytest

from blueprints import replenishment_pure as R

TODAY = date(2026, 10, 20)


# ---------------------------------------------------------------------------
# build_daily_series
# ---------------------------------------------------------------------------


def test_build_daily_series_includes_zero_days():
    """零消耗日必须进序列 —— 否则速率会被事件数而不是天数除。"""
    series = R.build_daily_series([("2026-10-20", 2.0), ("2026-10-18", 3.0)], TODAY, 7)
    assert len(series) == 7
    assert series[-1] == 2.0          # 今天
    assert series[-3] == 3.0          # 两天前
    assert series.count(0.0) == 5


def test_build_daily_series_drops_out_of_window_and_non_positive():
    series = R.build_daily_series(
        [("2026-09-01", 99.0), ("2026-10-20", 0.0), ("2026-10-20", -1.0), (None, 5)],
        TODAY,
        7,
    )
    assert sum(series) == 0.0


def test_build_daily_series_aggregates_same_day():
    series = R.build_daily_series([("2026-10-20", 1.5), ("2026-10-20", 2.5)], TODAY, 3)
    assert series[-1] == 4.0


def test_build_daily_series_zero_window():
    assert R.build_daily_series([("2026-10-20", 1.0)], TODAY, 0) == []


# ---------------------------------------------------------------------------
# estimate_demand
# ---------------------------------------------------------------------------


def test_estimate_demand_no_signal_is_not_zero_rate():
    """无记录 → status=no_signal，rate=0，但调用方能据此区分「没消耗」与「没数据」。"""
    est = R.estimate_demand([0.0] * 56)
    assert est.status == R.STATUS_NO_SIGNAL
    assert est.rate == 0.0
    assert est.has_signal is False
    assert est.nonzero_days == 0


def test_estimate_demand_continuous_uses_weighted_mean():
    series = [2.0] * 56
    est = R.estimate_demand(series)
    assert est.method == R.METHOD_CONTINUOUS
    assert est.status == R.STATUS_OK
    assert est.rate == pytest.approx(2.0, abs=1e-6)
    assert est.sigma == pytest.approx(0.0, abs=1e-6)


def test_estimate_demand_survives_quiet_second_half():
    """后 4 周零消耗（很可能就是断货）不能把速率拉到 0，否则永不补货。

    近远半段 2:1 加权 → (2·0 + 4)/3 = 1.33。纯 SES 会衰减到 1e-5。
    """
    series = [4.0] * 28 + [0.0] * 28
    est = R.estimate_demand(series)
    assert est.rate == pytest.approx(4.0 / 3.0, abs=0.01)
    assert est.rate > 1.0


def test_estimate_demand_sigma_captures_zero_days():
    """一半天数有消耗：σ 必须反映波动，不能被压成 0。"""
    series = [4.0] * 28 + [0.0] * 28
    est = R.estimate_demand(series)
    assert est.sigma > 1.5


def test_estimate_demand_intermittent_croston_rate():
    """每 7 天领 3 件 → 日均 3/7；用事件平均会算成 3（高估 7 倍）。"""
    series = [0.0] * 56
    for i in range(55, 0, -7):
        series[i] = 3.0
    est = R.estimate_demand(series)
    assert est.method == R.METHOD_INTERMITTENT
    assert est.rate == pytest.approx(3.0 / 7.0, rel=0.15)
    assert est.nonzero_days == 8
    # compound Poisson：σ = sqrt(λ·(var + mean²))，单值大小 → σ ≈ sqrt(λ)·mean
    assert est.sigma == pytest.approx(math.sqrt(1 / 7) * 3.0, rel=0.2)


def test_estimate_demand_single_event_still_has_rate():
    series = [0.0] * 56
    series[30] = 5.0
    est = R.estimate_demand(series)
    assert est.method == R.METHOD_INTERMITTENT
    assert est.rate > 0
    assert est.status == R.STATUS_STALE  # 26 天前，超过 21 天断档阈值


def test_estimate_demand_stale_when_continuous_goes_quiet():
    series = [3.0] * 30 + [0.0] * 26
    est = R.estimate_demand(series)
    assert est.status == R.STATUS_STALE
    assert est.days_since_last == 26


def test_estimate_demand_empty_series():
    est = R.estimate_demand([])
    assert est.status == R.STATUS_NO_SIGNAL
    assert est.window_days == 0


# ---------------------------------------------------------------------------
# safety_stock / target_level
# ---------------------------------------------------------------------------


def test_safety_stock_uses_lead_plus_review():
    """周期盘点口径：z·σ·√(L+R)，不是 √L。"""
    ss = R.safety_stock(sigma=2.0, lead_days=3, review_days=7, z=1.645)
    assert ss == pytest.approx(1.645 * 2.0 * math.sqrt(10), abs=0.01)


def test_safety_stock_sqrt_l_would_understate():
    """同一 σ 下，用 √L 会把安全库存低估 √(L+R)/√L 倍。"""
    ss_periodic = R.safety_stock(2.0, 1, 7, 1.645)
    ss_daily = R.safety_stock(2.0, 1, 0, 1.645)
    assert ss_periodic / ss_daily == pytest.approx(math.sqrt(8), rel=1e-3)


def test_safety_stock_zero_inputs():
    assert R.safety_stock(0.0, 3, 7, 1.645) == 0.0
    assert R.safety_stock(2.0, 0, 0, 1.645) == 0.0
    assert R.safety_stock(2.0, 3, 7, 0.0) == 0.0


def test_target_level_is_demand_over_exposure_plus_ss():
    target = R.target_level(rate=1.5, lead_days=3, review_days=7, ss=4.0)
    assert target == pytest.approx(1.5 * 10 + 4.0, abs=0.01)


def test_target_level_without_demand_equals_ss():
    assert R.target_level(0.0, 3, 7, 2.5) == 2.5


# ---------------------------------------------------------------------------
# order_quantity
# ---------------------------------------------------------------------------


def test_order_quantity_nets_on_hand_and_on_order():
    qty = R.order_quantity(target=20.0, on_hand=5.0, on_order=4.0)
    assert qty == 11.0


def test_order_quantity_never_negative():
    assert R.order_quantity(target=5.0, on_hand=9.0, on_order=0.0) == 0.0


def test_order_quantity_rounds_up_to_pack():
    # 需要 1.2 箱，1 箱起订且不能拆 → 2 箱
    assert R.order_quantity(target=2.2, on_hand=1.0, pack_size=1.0) == 2.0
    # 0.5 箱规格：需要 1.2 → 1.5
    assert R.order_quantity(target=1.2, on_hand=0.0, pack_size=0.5) == 1.5


def test_order_quantity_respects_min_order():
    assert R.order_quantity(target=0.4, on_hand=0.0, min_order=1.0) == 1.0


def test_order_quantity_tiny_need_still_rounds_to_one_pack():
    """长尾品项：需要 0.01 桶，1 桶起订 → 订 1 桶（由 over_rounding 负责标注）。"""
    assert R.order_quantity(target=0.01, on_hand=0.0) == 1.0


# ---------------------------------------------------------------------------
# days_of_cover / status
# ---------------------------------------------------------------------------


def test_days_of_cover_none_without_demand():
    assert R.days_of_cover(10.0, 0.0) is None


def test_days_of_cover_basic():
    assert R.days_of_cover(9.0, 1.5) == 6.0


@pytest.mark.parametrize(
    "on_hand,rate,expected",
    [
        (0.5, 1.0, R.STATUS_CRITICAL),   # 0.5 天 < 提前期 1 天
        (5.0, 1.0, R.STATUS_DUE),        # 5 天 < 1+7
        (20.0, 1.0, R.STATUS_HEALTHY),   # 20 天，正常
        (100.0, 1.0, R.STATUS_OVERSTOCK),  # 100 天 > 45
    ],
)
def test_classify_status_by_cover(on_hand, rate, expected):
    assert R.classify_stock_status(
        on_hand=on_hand, on_order=0.0, rate=rate,
        lead_days=1, review_days=7, sig_status=R.STATUS_OK,
    ) == expected


def test_classify_status_counts_on_order():
    """在途要计入覆盖，否则已下单的品项会被误判成断货。"""
    assert R.classify_stock_status(
        on_hand=0.0, on_order=9.0, rate=1.0,
        lead_days=1, review_days=7, sig_status=R.STATUS_OK,
    ) == R.STATUS_HEALTHY


def test_classify_status_no_signal_states():
    assert R.classify_stock_status(
        0.0, 0.0, 0.0, 1, 7, R.STATUS_NO_SIGNAL
    ) == R.STATUS_DEAD
    assert R.classify_stock_status(
        12.0, 0.0, 0.0, 1, 7, R.STATUS_NO_SIGNAL
    ) == "no_signal"


# ---------------------------------------------------------------------------
# ABC / EOQ
# ---------------------------------------------------------------------------


def test_abc_classify_pareto_split():
    # 第一项占 80%，第二项 15%，其余 5%
    values = [80.0, 15.0, 3.0, 2.0]
    assert R.abc_classify(values) == ["A", "B", "C", "C"]


def test_abc_classify_all_zero_is_c():
    assert R.abc_classify([0.0, 0.0]) == ["C", "C"]


def test_abc_classify_empty():
    assert R.abc_classify([]) == []


def test_economic_order_qty_formula():
    eoq = R.economic_order_qty(rate=1.0, unit_cost=100.0)
    # sqrt(2*80*365 / (0.22*100)) = sqrt(58400/22) ≈ 51.5
    assert eoq == pytest.approx(math.sqrt(2 * 80 * 365 / 22), abs=0.05)


def test_economic_order_qty_none_without_cost():
    assert R.economic_order_qty(1.0, 0.0) is None
    assert R.economic_order_qty(0.0, 100.0) is None


def test_order_frequency_per_year():
    assert R.order_frequency_per_year(rate=1.0, qty=365.0) == 1.0


# ---------------------------------------------------------------------------
# aggregate_series
# ---------------------------------------------------------------------------


def test_aggregate_series_sums_elementwise():
    assert R.aggregate_series([[1.0, 0.0], [0.0, 2.0]]) == [1.0, 2.0]


def test_aggregate_series_truncates_to_shortest():
    assert len(R.aggregate_series([[1.0, 1.0, 1.0], [1.0]])) == 1


def test_aggregate_series_empty():
    assert R.aggregate_series([]) == []


def test_aggregate_series_captures_correlation():
    """同一天一起卖的两个店，合并后 σ 应大于独立假设的 sqrt(a²+b²)。"""
    a = [0.0] * 52 + [5.0] * 4
    b = [0.0] * 52 + [5.0] * 4
    combined = R.aggregate_series([a, b])
    est = R.estimate_demand(combined)
    independent = math.sqrt(R.estimate_demand(a).sigma ** 2 + R.estimate_demand(b).sigma ** 2)
    assert est.sigma > independent


# ---------------------------------------------------------------------------
# rollup
# ---------------------------------------------------------------------------


def test_rollup_coverage_days_is_value_weighted():
    rows = [
        {"quantity": 10.0, "unit_cost": 10.0, "rate": 1.0, "suggested_qty": 0,
         "safety_stock": 0, "status": R.STATUS_HEALTHY},
        {"quantity": 0.0, "unit_cost": 100.0, "rate": 1.0, "suggested_qty": 2,
         "safety_stock": 1, "status": R.STATUS_DUE},
    ]
    ru = R.rollup("x", rows)
    assert ru.stock_value == 100.0
    # 两行 rate 都是 1/天：10 元品 30 元/月 + 100 元品 3000 元/月
    assert ru.monthly_value == pytest.approx(3300.0)
    # 100 元库存 ÷ (3300/30 = 110 元/天) ≈ 0.9 天
    assert ru.coverage_days == pytest.approx(0.9)
    assert ru.action_count == 1
    assert ru.suggested_value == 200.0


def test_rollup_counts_no_signal_and_valued():
    rows = [
        {"quantity": 1.0, "unit_cost": 0.0, "rate": 0.0, "status": R.STATUS_DEAD},
        {"quantity": 1.0, "unit_cost": 5.0, "rate": 0.0, "status": "no_signal"},
    ]
    ru = R.rollup("x", rows)
    assert ru.value_coverage == 0.5
    assert ru.no_signal_count == 2


def test_rollup_empty():
    ru = R.rollup("x", [])
    assert ru.stock_value == 0.0
    assert ru.coverage_days is None
    assert ru.value_coverage == 1.0


# ---------------------------------------------------------------------------
# plan_item (end-to-end of the model, still no I/O)
# ---------------------------------------------------------------------------


def _item(**kw):
    base = {
        "id": 1, "sku": "S1", "name": "测试品", "unit": "箱",
        "quantity": 0.0, "unit_cost": 100.0, "canonical_id": 1,
        "category": "调味酱", "on_order": 0.0,
    }
    base.update(kw)
    return base


def _wh(wh_type="storefront"):
    return {"code": "w1", "name": "某店", "warehouse_type": wh_type}


def test_plan_item_store_uses_store_lead_and_review():
    # 有波动的序列，保证 σ>0 时 SS 也参与
    series = [3.0] * 20 + [0.0] * 8 + [3.0] * 20 + [0.0] * 8
    plan = R.plan_item(warehouse=_wh(), item=_item(), series=series, abc="A")
    assert plan.lead_days == 1
    assert plan.review_days == 7
    assert plan.service_level == pytest.approx(0.95)
    assert plan.safety_stock > 0
    assert plan.target > plan.safety_stock


def test_plan_item_dc_uses_dc_lead_time():
    series = [3.0] * 20 + [0.0] * 8 + [3.0] * 20 + [0.0] * 8
    plan = R.plan_item(
        warehouse=_wh("distribution_center"), item=_item(), series=series, abc="A"
    )
    assert plan.lead_days == 3
    assert plan.review_days == 7
    # DC 敞口 3+7=10 天 > 门店 1+7=8 天 → 同样 σ 下安全库存更高
    store = R.plan_item(warehouse=_wh(), item=_item(), series=series, abc="A")
    assert plan.safety_stock > store.safety_stock


def test_plan_item_no_demand_gives_zero_order():
    plan = R.plan_item(warehouse=_wh(), item=_item(quantity=5.0), series=[0.0] * 56, abc="C")
    assert plan.suggested_qty == 0.0
    assert plan.status == "no_signal"
    assert plan.cover_days is None


def test_plan_item_over_rounding_flag():
    """需求极小的品项按整包起订，覆盖天数会远超阈值 → 必须被标出来。"""
    series = [0.0] * 56
    series[50] = 0.05
    plan = R.plan_item(warehouse=_wh(), item=_item(unit_cost=1000.0), series=series, abc="C")
    assert plan.suggested_qty >= 1.0
    assert plan.over_rounding is True


def test_plan_item_demand_source_passthrough():
    plan = R.plan_item(
        warehouse=_wh("distribution_center"), item=_item(), series=[1.0] * 56,
        abc="A", demand_source="downstream",
    )
    assert plan.demand_source == "downstream"
    assert plan.as_dict()["demand_source"] == "downstream"


def test_plan_item_excess_only_when_above_target():
    plan = R.plan_item(warehouse=_wh(), item=_item(quantity=100.0), series=[1.0] * 56, abc="C")
    assert plan.excess_qty == pytest.approx(100.0 - plan.target, abs=0.01)
    assert plan.status == R.STATUS_OVERSTOCK
