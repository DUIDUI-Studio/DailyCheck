"""Unit tests for the pure `parse_summary_dates` function.

Spec §5.1 enumerates 8 cases. The function takes a dict-like args
object (start / end / range) and returns (start_date, end_date, error)
where error is None on success and a flash message on failure.
"""
from __future__ import annotations

import datetime as _dt
from types import SimpleNamespace

import pytest

from blueprints.summary_dates import parse_summary_dates


TODAY = _dt.date(2026, 6, 29)


@pytest.fixture(autouse=True)
def _freeze_today(monkeypatch):
    """The fn uses datetime.date.today(); freeze it to TODAY for deterministic tests."""
    import blueprints.summary_dates as mod
    monkeypatch.setattr(mod._dt.date, "today", staticmethod(lambda: TODAY))


# ---------------------------------------------------------------------------
# 5.1 cases
# ---------------------------------------------------------------------------


def test_all_absent_uses_7d_default():
    """全部缺省 → (today-7, today)."""
    args = SimpleNamespace(start=None, end=None, range=None)
    start, end, err = parse_summary_dates(args)
    assert err is None
    assert start == _dt.date(2026, 6, 22)
    assert end == TODAY


def test_start_only_end_defaults_to_today():
    """start only → (start, today)."""
    args = SimpleNamespace(start="2026-06-01", end=None, range=None)
    start, end, err = parse_summary_dates(args)
    assert err is None
    assert start == _dt.date(2026, 6, 1)
    assert end == TODAY


def test_end_only_start_defaults_to_today_minus_7():
    """end only → (today-7, end)."""
    args = SimpleNamespace(start=None, end="2026-06-30", range=None)
    start, end, err = parse_summary_dates(args)
    assert err is None
    assert start == _dt.date(2026, 6, 22)
    assert end == _dt.date(2026, 6, 30)


def test_both_provided():
    """都有 → (start, end)."""
    args = SimpleNamespace(start="2026-06-01", end="2026-06-30", range=None)
    start, end, err = parse_summary_dates(args)
    assert err is None
    assert start == _dt.date(2026, 6, 1)
    assert end == _dt.date(2026, 6, 30)


def test_start_after_end_errors():
    """start > end → error. Flash message locked: '开始日期不能晚于结束日期'."""
    args = SimpleNamespace(start="2026-06-30", end="2026-06-01", range=None)
    start, end, err = parse_summary_dates(args)
    assert start is None
    assert end is None
    assert err == "开始日期不能晚于结束日期"


def test_span_over_365_days_errors():
    """跨度 > 365 天 → error. Flash message locked: '时间范围不能超过 1 年'."""
    args = SimpleNamespace(start="2025-01-01", end="2026-06-29", range=None)
    start, end, err = parse_summary_dates(args)
    assert start is None
    assert end is None
    assert err == "时间范围不能超过 1 年"


def test_end_in_future_past_today_plus_1_errors():
    """未来日期 end 不允许超过 today + 1."""
    args = SimpleNamespace(start="2026-06-01", end="2026-07-01", range=None)
    # 2026-07-01 is > today(2026-06-29) + 1 day = 2026-06-30
    start, end, err = parse_summary_dates(args)
    assert start is None
    assert end is None
    assert err is not None
    # the message is about future, not the format/start-after-end ones
    assert "开始日期" not in err
    assert "时间范围" not in err


def test_end_today_plus_one_is_allowed():
    """end = today + 1 是允许的边界。"""
    args = SimpleNamespace(start="2026-06-29", end="2026-06-30", range=None)
    start, end, err = parse_summary_dates(args)
    assert err is None
    assert end == _dt.date(2026, 6, 30)


def test_invalid_format_errors():
    """格式错 → error. Flash message locked: '日期格式应为 YYYY-MM-DD'."""
    args = SimpleNamespace(start="not-a-date", end=None, range=None)
    start, end, err = parse_summary_dates(args)
    assert start is None
    assert end is None
    assert err == "日期格式应为 YYYY-MM-DD"


def test_range_param_is_ignored():
    """range=7d → 忽略,按缺省(start=None, end=None → today-7..today)."""
    args = SimpleNamespace(start=None, end=None, range="7d")
    start, end, err = parse_summary_dates(args)
    assert err is None
    assert start == _dt.date(2026, 6, 22)
    assert end == TODAY
