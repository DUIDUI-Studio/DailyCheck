"""Unit tests for the three-channel fusion layer in replenishment_pure.

覆盖 2026-10-20 增补的「产量 / 领料 / 盘点」三口径融合、偏差识别与回测指标。
"""
from __future__ import annotations

import pytest

from blueprints import replenishment_pure as R


def _rate(qty: float, days: float, samples: int, method: str, outliers: int = 0):
    return R.channel_rate(qty, days, samples, method, outliers)


# ---------------------------------------------------------------------------
# ChannelRate / channel_rate
# ---------------------------------------------------------------------------


def test_channel_rate_basic():
    ch = _rate(56.0, 56.0, 8, R.METHOD_COUNT)
    assert ch.rate == 1.0
    assert ch.available is True
    assert ch.coverage == 1.0        # 8 个周期 / 2 起算 → 封顶 1


def test_channel_rate_zero_days():
    ch = _rate(10.0, 0.0, 0, R.METHOD_ISSUE)
    assert ch.rate == 0.0
    assert ch.available is False


def test_channel_rate_coverage_partial():
    ch = _rate(10.0, 14.0, 1, R.METHOD_COUNT)
    assert ch.coverage == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# bias_factor / gap_ratio / yield_ratio
# ---------------------------------------------------------------------------


def test_bias_factor_uplifts_under_recorded_issue():
    """盘点速率高于领料 → k>1，表示有未记录损耗。"""
    assert R.bias_factor(1.5, 1.0) == pytest.approx(1.5)


def test_bias_factor_clamped():
    assert R.bias_factor(100.0, 1.0) == R.BIAS_FACTOR_MAX
    assert R.bias_factor(0.01, 1.0) == R.BIAS_FACTOR_MIN


def test_bias_factor_falls_back_to_prior():
    assert R.bias_factor(0.0, 1.0, prior=1.2) == 1.2
    assert R.bias_factor(1.0, 0.0, prior=0.9) == 0.9


def test_gap_ratio_sign():
    # 领料比盘点多记 → 正数
    assert R.gap_ratio(1.2, 1.0) == pytest.approx(0.2)
    # 领料少记 → 负数（有未记录损耗）
    assert R.gap_ratio(0.8, 1.0) == pytest.approx(-0.2)
    assert R.gap_ratio(1.0, 0.0) is None


def test_yield_ratio():
    assert R.yield_ratio(105.0, 100.0) == pytest.approx(1.05)
    assert R.yield_ratio(100.0, 0.0) is None


# ---------------------------------------------------------------------------
# fuse_demand
# ---------------------------------------------------------------------------


def _count_ch(rate: float = 1.0, samples: int = 6, days: float = 42.0):
    return _rate(rate * days, days, samples, R.METHOD_COUNT)


def _issue_ch(rate: float = 1.0, days: float = 56.0):
    return _rate(rate * days, days, 20, R.METHOD_ISSUE)


def test_fuse_prefers_count_when_available():
    fused = R.fuse_demand(count=_count_ch(1.4), issue=_issue_ch(1.0))
    assert fused.method == R.METHOD_COUNT
    assert fused.rate == pytest.approx(1.4, abs=0.01)
    assert fused.bias_factor == pytest.approx(1.4, abs=0.01)
    assert fused.gap_ratio == pytest.approx((1.0 - 1.4) / 1.4, abs=0.01)
    assert fused.confidence == "high"


def test_fuse_needs_two_count_intervals():
    """只有一个盘点周期不足以判断趋势，应回退领料口径。"""
    fused = R.fuse_demand(count=_count_ch(1.4, samples=1), issue=_issue_ch(1.0))
    assert fused.method == R.METHOD_ISSUE


def test_fuse_rejects_order_of_magnitude_mismatch():
    """盘点 60/天 vs 领料 0.002/天 —— 实测的录入手误形态，必须回退。"""
    fused = R.fuse_demand(count=_count_ch(60.0), issue=_issue_ch(0.002))
    assert fused.method == R.METHOD_ISSUE
    assert fused.suspect is True
    assert "录入异常" in fused.note


def test_fuse_falls_back_to_issue_with_category_bias():
    fused = R.fuse_demand(issue=_issue_ch(1.0), category_bias=1.3)
    assert fused.method == R.METHOD_ISSUE
    assert fused.rate == pytest.approx(1.3, abs=0.01)


def test_fuse_production_only():
    prod = _rate(56.0, 56.0, 30, R.METHOD_PRODUCTION)
    fused = R.fuse_demand(production=prod)
    assert fused.method == R.METHOD_PRODUCTION
    assert fused.confidence == "low"


def test_fuse_no_signal():
    fused = R.fuse_demand()
    assert fused.method == R.METHOD_NONE_RATE
    assert fused.rate == 0.0
    assert fused.has_signal is False


def test_fuse_shrink_rate_positive_when_count_exceeds_issue():
    fused = R.fuse_demand(count=_count_ch(1.5), issue=_issue_ch(1.0))
    assert fused.shrink_rate == pytest.approx(0.5, abs=0.01)


# ---------------------------------------------------------------------------
# scale_series_by_total
# ---------------------------------------------------------------------------


def test_scale_series_preserves_shape():
    out = R.scale_series_by_total([1.0, 0.0, 3.0], 8.0)
    assert sum(out) == pytest.approx(8.0)
    assert out[0] == pytest.approx(2.0)
    assert out[1] == 0.0
    assert out[2] == pytest.approx(6.0)


def test_scale_series_zero_series_spreads_evenly():
    out = R.scale_series_by_total([0.0, 0.0, 0.0, 0.0], 8.0)
    assert out == [2.0, 2.0, 2.0, 2.0]


def test_scale_series_empty():
    assert R.scale_series_by_total([], 5.0) == []


# ---------------------------------------------------------------------------
# cusum_alarm
# ---------------------------------------------------------------------------


def test_cusum_flags_persistent_drift():
    """连续同向 8% 偏差累积到阈值 → 告警；这是单周看不出来的漂移。"""
    series = [0.08] * 10
    assert R.cusum_alarm(series, slack=0.05, threshold=0.15) is not None


def test_cusum_ignores_noise():
    series = [0.02, -0.01, 0.03, -0.02, 0.01]
    assert R.cusum_alarm(series, slack=0.05, threshold=0.15) is None


def test_cusum_empty():
    assert R.cusum_alarm([], slack=0.05, threshold=0.15) is None


# ---------------------------------------------------------------------------
# backtest_scores
# ---------------------------------------------------------------------------


def test_backtest_scores_perfect():
    s = R.backtest_scores([1.0, 2.0], [1.0, 2.0])
    assert s["wape"] == 0.0
    assert s["bias"] == 0.0


def test_backtest_scores_systematic_underforecast():
    s = R.backtest_scores([10.0, 10.0], [5.0, 5.0])
    assert s["wape"] == pytest.approx(0.5)
    assert s["bias_pct"] == pytest.approx(-0.5)


def test_backtest_scores_empty():
    assert R.backtest_scores([], [])["n"] == 0


# ---------------------------------------------------------------------------
# plan_item 与三口径的接线
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


def test_plan_item_without_channels_keeps_single_channel_behavior():
    plan = R.plan_item(warehouse=_wh(), item=_item(), series=[2.0] * 56, abc="A")
    assert plan.fuse_method == R.METHOD_ISSUE
    assert plan.rate == pytest.approx(plan.rate_issue)
    assert plan.count_qty is None


def test_plan_item_uses_count_channel_and_scales_sigma():
    """盘点口径放大 2 倍 → 速率与 σ 同时放大，否则安全库存会算小。"""
    series = [1.0] * 28 + [0.0] * 14 + [1.0] * 14
    base = R.plan_item(warehouse=_wh(), item=_item(), series=series, abc="A")
    channels = {
        "issue": _rate(sum(series), 56.0, 42, R.METHOD_ISSUE),
        "count": _rate(2.0 * sum(series), 56.0, 8, R.METHOD_COUNT),
        "production": None,
        "production_qty": 0.0,
        "yield_ratio": None,
    }
    fused = R.plan_item(
        warehouse=_wh(), item=_item(), series=series, abc="A", channels=channels
    )
    assert fused.fuse_method == R.METHOD_COUNT
    # 盘点口径：84 件 / 56 天 = 1.5/天（序列总量 42 的 2 倍）
    assert fused.rate == pytest.approx(1.5)
    # σ 按同一比例放大：1.5 / 0.667 ≈ 2.25 倍
    assert fused.sigma == pytest.approx(base.sigma * 1.5 / base.rate, rel=0.01)
    assert fused.safety_stock > base.safety_stock


def test_plan_item_count_suspect_falls_back():
    series = [1.0] * 56
    channels = {
        "issue": _rate(56.0, 56.0, 40, R.METHOD_ISSUE),
        "count": _rate(6000.0, 56.0, 8, R.METHOD_COUNT),
        "production": None,
        "production_qty": 0.0,
        "yield_ratio": None,
    }
    plan = R.plan_item(
        warehouse=_wh(), item=_item(), series=series, abc="A", channels=channels
    )
    assert plan.count_suspect is True
    assert plan.fuse_method == R.METHOD_ISSUE
    assert plan.rate == pytest.approx(1.0, rel=0.01)


def test_plan_item_records_production_diagnostics():
    series = [1.0] * 56
    channels = {
        "issue": _rate(56.0, 56.0, 40, R.METHOD_ISSUE),
        "count": None,
        "production": _rate(56.0, 56.0, 20, R.METHOD_PRODUCTION),
        "production_qty": 120.0,
        "yield_ratio": 1.08,
    }
    plan = R.plan_item(
        warehouse=_wh(), item=_item(), series=series, abc="A", channels=channels
    )
    assert plan.production_qty == 120.0
    assert plan.yield_ratio == pytest.approx(1.08)
    assert plan.rate_production == pytest.approx(1.0, abs=0.01)
