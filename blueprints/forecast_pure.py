"""Pure functions for the /forecast blueprint.

Lives in its own module so unit tests can import without pulling in
Flask or the db layer. The blueprint (blueprints/forecast.py) wires
these to routes and adds I/O.

Algorithm choices are documented in
docs/superpowers/specs/2026-06-29-forecast-design.md §0.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal


# How many days back the weighted average looks. PRD §2.1.2 says "过去 30 天".
WINDOW_DAYS = 30

# Confidence thresholds on record count within the window.
COLD_START_MAX = 6      # < 7 records → cold_start (PRD §2.1.3)
LOW_MAX = 13            # 7..13 → low
MEDIUM_MAX = 29         # 14..29 → medium; ≥ 30 → high


def compute_daily_avg(movements: list[tuple[datetime, float]]) -> float:
    """Linear-decay weighted average over the last WINDOW_DAYS days.

    Each (ts, qty) pair is bucketed by days-ago (today = 0, yesterday = 1,
    ...). The weight for bucket i is (WINDOW_DAYS - i). Movements outside
    the window are dropped before the calculation. Returns 0.0 for an
    empty list. Result is quantized to 2 decimal places to match the
    display precision used elsewhere in the system.
    """
    if not movements:
        return 0.0

    today = datetime.now()
    weighted_sum = 0.0
    weight_sum = 0
    for ts, qty in movements:
        days_ago = (today - ts).days
        if days_ago < 0 or days_ago >= WINDOW_DAYS:
            continue
        weight = WINDOW_DAYS - days_ago
        weighted_sum += weight * float(qty)
        weight_sum += weight

    if weight_sum == 0:
        return 0.0

    raw = weighted_sum / weight_sum
    return float(Decimal(str(raw)).quantize(Decimal('0.01')))


def classify_confidence(n_records: int) -> str:
    """Map a record count to one of cold_start / low / medium / high.

    Boundaries (PRD §2.1.2 + spec §0):
        0..6  → cold_start
        7..13 → low
        14..29 → medium
        ≥ 30  → high
    """
    if n_records <= COLD_START_MAX:
        return "cold_start"
    if n_records <= LOW_MAX:
        return "low"
    if n_records <= MEDIUM_MAX:
        return "medium"
    return "high"
