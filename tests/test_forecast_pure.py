"""Unit tests for forecast pure functions.

These cover the math only — no DB, no Flask. The blueprint layer wires
these into the /forecast route in TASK 4+.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from blueprints.forecast_pure import (
    classify_confidence,
    compute_daily_avg,
)


# ---------------------------------------------------------------------------
# compute_daily_avg
# ---------------------------------------------------------------------------


def test_compute_daily_avg_empty_returns_zero():
    """No movements → daily_avg is 0 (cold-start callers handle UI)."""
    assert compute_daily_avg([]) == 0.0


def test_compute_daily_avg_single_today():
    """One movement today → daily_avg equals its qty (weight = 30 alone)."""
    today = datetime(2026, 6, 29, 12, 0, 0)
    assert compute_daily_avg([(today, 5.0)]) == pytest.approx(5.0)


def test_compute_daily_avg_uniform_30_days_is_one():
    """30 uniform days (qty=1) → daily_avg = 1.0.

    Weights 30..1, sum(weight)=465, sum(weight*qty)=465, avg=1.0.
    """
    today = datetime(2026, 6, 29, 12, 0, 0)
    rows = [(today - timedelta(days=i), 1.0) for i in range(30)]
    assert compute_daily_avg(rows) == pytest.approx(1.0)


def test_compute_daily_avg_uniform_15_days_is_one():
    """15 uniform days → daily_avg = 1.0 (weights still sum correctly)."""
    today = datetime(2026, 6, 29, 12, 0, 0)
    rows = [(today - timedelta(days=i), 1.0) for i in range(15)]
    assert compute_daily_avg(rows) == pytest.approx(1.0)


def test_compute_daily_avg_sparse_five_days():
    """Only 5 most recent days have data → avg is 1.0 (uniform within window).

    Weights for i=0..4: 30+29+28+27+26 = 140; sum(weight*qty) = 140; avg=1.0.
    """
    today = datetime(2026, 6, 29, 12, 0, 0)
    rows = [(today - timedelta(days=i), 1.0) for i in range(5)]
    assert compute_daily_avg(rows) == pytest.approx(1.0)


def test_compute_daily_avg_recent_spike_dominates():
    """A spike today (qty=100) on top of 6 quiet days (qty=1 each) → avg ~16.71.

    i=0 weight 30 qty 100 → 3000
    i=1..6 weights 29..24 qty 1 → 159
    sum(weight) = 30+29+...+24 = 189
    avg = 3159 / 189 ≈ 16.7142857
    """
    today = datetime(2026, 6, 29, 12, 0, 0)
    rows = [(today - timedelta(days=i), 1.0) for i in range(1, 7)]
    rows.insert(0, (today, 100.0))
    # 2dp quantize rounds 16.714... → 16.71
    assert compute_daily_avg(rows) == pytest.approx(16.71)


def test_compute_daily_avg_old_data_weighs_less_than_recent():
    """Two equal qty=1 events, one 29 days ago and one today → today wins.

    i=0 weight 30, i=29 weight 1: avg = (30+1)/(30+1) = 1.0; but if we make
    the old day 10x larger, the recent should still come out ahead:
    rows = [(today, 1.0), (today-29d, 10.0)]
    avg = (30*1 + 1*10) / (30+1) = 40/31 ≈ 1.29
    → still very close to 1.0 because recent weight dominates 10x.

    Counter-test: if old day had 100x:
    avg = (30*1 + 1*100) / 31 = 130/31 ≈ 4.19
    → confirms recent is weighted much higher than far.
    """
    today = datetime(2026, 6, 29, 12, 0, 0)
    rows = [(today, 1.0), (today - timedelta(days=29), 100.0)]
    # 2dp quantize rounds 4.1935... → 4.19
    # Expected ≈ 4.19, NOT anywhere near 50.5 (which would be naive mean).
    result = compute_daily_avg(rows)
    assert result == pytest.approx(4.19)
    assert result < 10  # well below naive (1+100)/2 = 50.5


def test_compute_daily_avg_quantize_2dp():
    """Result rounds to 2 decimal places (avoid float display noise)."""
    today = datetime(2026, 6, 29, 12, 0, 0)
    rows = [(today, 1.0), (today - timedelta(days=1), 1.0), (today - timedelta(days=2), 1.0)]
    result = compute_daily_avg(rows)
    # 3 uniform entries: weights 30,29,28 → sum=87, sum(wq)=87, avg=1.0
    assert result == 1.0  # already 2dp clean


def test_compute_daily_avg_filters_outside_30d():
    """Movements older than 30 days are ignored (not in window).

    If we hand a 35-day-old row, the weight formula would assign weight -5.
    The implementation must filter i > 29 out before weighting, not just clamp.
    """
    today = datetime(2026, 6, 29, 12, 0, 0)
    rows = [
        (today, 1.0),
        (today - timedelta(days=35), 9999.0),  # should be ignored
    ]
    assert compute_daily_avg(rows) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# classify_confidence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n,expected", [
    (0, "cold_start"),
    (1, "cold_start"),
    (6, "cold_start"),
    (7, "low"),
    (13, "low"),
    (14, "medium"),
    (29, "medium"),
    (30, "high"),
    (100, "high"),
])
def test_classify_confidence(n, expected):
    assert classify_confidence(n) == expected
