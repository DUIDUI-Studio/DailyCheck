"""Unit tests for procurement pure functions.

Math + rules only — no DB, no Flask.
"""
from __future__ import annotations

import pytest

from blueprints.procurement_pure import (
    aggregate_hub,
    compute_safety_stock,
    compute_suggested_qty,
)


# ---------------------------------------------------------------------------
# compute_safety_stock
# ---------------------------------------------------------------------------


def test_safety_stock_zero_daily_returns_min_absolute():
    assert compute_safety_stock(0.0, cover_days=14, min_absolute=0.0) == 0.0
    assert compute_safety_stock(0.0, cover_days=14, min_absolute=1.0) == 1.0


def test_safety_stock_basic_multiplication():
    assert compute_safety_stock(0.1, cover_days=14, min_absolute=0.0) == pytest.approx(1.4)


def test_safety_stock_max_with_min_absolute():
    """daily_avg=0.05 * 14 = 0.7 < 1.0 → returns 1.0 (PRD §2.2.3 max)."""
    assert compute_safety_stock(0.05, cover_days=14, min_absolute=1.0) == 1.0


def test_safety_stock_high_daily_ignores_min_absolute():
    """daily_avg=2.0 * 14 = 28.0 > 1.0 → returns 28.0."""
    assert compute_safety_stock(2.0, cover_days=14, min_absolute=1.0) == pytest.approx(28.0)


def test_safety_stock_quantize_2dp():
    """0.07 * 14 = 0.98 → expect 2dp output."""
    assert compute_safety_stock(0.07, cover_days=14, min_absolute=0.0) == pytest.approx(0.98)


# ---------------------------------------------------------------------------
# compute_suggested_qty
# ---------------------------------------------------------------------------


def test_suggested_qty_all_zero_returns_zero():
    assert compute_suggested_qty(0, 0, 0) == 0


def test_suggested_qty_basic_ceiling():
    """safety=5, current=2, in_transit=1 → ceil(5-2-1) = 2."""
    assert compute_suggested_qty(5, 2, 1) == 2


def test_suggested_qty_oversupplied_returns_zero():
    """safety=5, current=10, in_transit=0 → max(0, -5) = 0."""
    assert compute_suggested_qty(5, 10, 0) == 0


def test_suggested_qty_ceiling_rounds_up():
    """safety=0.1, current=0, in_transit=0 → ceil(0.1) = 1 (never partial)."""
    assert compute_suggested_qty(0.1, 0, 0) == 1


def test_suggested_qty_handles_negative_safety_cleanly():
    """If somehow safety is negative, treat as 0 (defensive)."""
    # PRD doesn't allow safety<0 (it's a max()), but the fn should not crash.
    assert compute_suggested_qty(-1, 0, 0) == 0


def test_suggested_qty_returns_int_type():
    """PRD spec uses int suggested_qty (ceil, no fractions)."""
    result = compute_suggested_qty(5.0, 2.0, 1.0)
    assert isinstance(result, int)


# ---------------------------------------------------------------------------
# aggregate_hub
# ---------------------------------------------------------------------------


def test_aggregate_hub_empty_input():
    assert aggregate_hub([]) == []


def test_aggregate_hub_single_store_single_item():
    store_reports = [
        {"warehouse_code": "wh_001", "items": [
            {"item_id": 1, "item_name": "x", "suggested_qty": 3},
        ]},
    ]
    out = aggregate_hub(store_reports)
    assert len(out) == 1
    assert out[0]["item_id"] == 1
    assert out[0]["total_suggested_qty"] == 3
    assert out[0]["stores_needing"] == 1
    assert out[0]["stores_detail"] == [
        {"warehouse_code": "wh_001", "suggested_qty": 3}
    ]


def test_aggregate_hub_two_stores_same_item():
    store_reports = [
        {"warehouse_code": "wh_001", "items": [
            {"item_id": 1, "item_name": "x", "suggested_qty": 2},
        ]},
        {"warehouse_code": "wh_002", "items": [
            {"item_id": 1, "item_name": "x", "suggested_qty": 5},
        ]},
    ]
    out = aggregate_hub(store_reports)
    assert out[0]["total_suggested_qty"] == 7
    assert out[0]["stores_needing"] == 2
    assert len(out[0]["stores_detail"]) == 2


def test_aggregate_hub_stores_needing_only_counts_positive():
    """A store with suggested_qty=0 should NOT be counted as 'needing'."""
    store_reports = [
        {"warehouse_code": "wh_001", "items": [
            {"item_id": 1, "item_name": "x", "suggested_qty": 0},
            {"item_id": 2, "item_name": "y", "suggested_qty": 4},
        ]},
    ]
    out = aggregate_hub(store_reports)
    by_id = {o["item_id"]: o for o in out}
    assert by_id[1]["stores_needing"] == 0
    assert by_id[1]["total_suggested_qty"] == 0
    assert by_id[2]["stores_needing"] == 1
    assert by_id[2]["total_suggested_qty"] == 4


def test_aggregate_hub_sorted_by_total_suggested_qty_desc():
    """Hub view: highest-demand items first."""
    store_reports = [
        {"warehouse_code": "wh_001", "items": [
            {"item_id": 1, "item_name": "low", "suggested_qty": 1},
            {"item_id": 2, "item_name": "high", "suggested_qty": 10},
        ]},
    ]
    out = aggregate_hub(store_reports)
    assert [o["item_id"] for o in out] == [2, 1]
