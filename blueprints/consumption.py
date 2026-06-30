"""Single source of truth for item-level consumption SQL.

The "what counts as consumption" definition is shared between:
  - /inventory page (blueprints/items.py c7 CTE)
  - /forecast/item/<id> (subproject 1)
  - /procurement/store (subproject 2)
  - /api/v1/movements (subproject 6)

All four paths MUST agree. This module owns the SQL and exposes
helpers that take a sqlite3 connection (callers open it themselves to
keep test fixture monkeypatching working).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal


# Mirrors blueprints.forecast_pure.WINDOW_DAYS. Inlined here to keep
# the modules independent.
WINDOW_DAYS = 30


def _is_iso(ts: str) -> bool:
    try:
        datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        return True
    except (TypeError, ValueError):
        return False


def _extract(r) -> tuple[str, float]:
    """Read (created_at, qty) from a row that may be Row or tuple."""
    try:
        return r["created_at"], r["qty"]
    except (TypeError, KeyError, IndexError):
        return r[1], r[0]


def fetch_item_movements_30d(db: sqlite3.Connection, item_id: int) -> list[tuple[datetime, float]]:
    """Return (datetime, qty) for the given item's consumption in the
    last 30 days. Source = outbound_requests.rolled_back=0 UNION
    production_run_items (where production_runs.rolled_back=0). Matches
    /inventory c7 CTE.
    """
    rows = db.execute(
        """SELECT qty, created_at FROM (
              SELECT o.requested_quantity AS qty, o.created_at
              FROM outbound_requests o
              WHERE o.item_id = ? AND o.rolled_back = 0
                AND created_at >= datetime('now', '-30 days')
              UNION ALL
              SELECT pri.actual_qty AS qty, pr.created_at
              FROM production_run_items pri
              JOIN production_runs pr ON pr.id = pri.run_id
              WHERE pri.item_id = ? AND pr.rolled_back = 0
                AND pr.created_at >= datetime('now', '-30 days')
           )""",
        (item_id, item_id),
    ).fetchall()
    parsed: list[tuple[datetime, float]] = []
    for r in rows:
        ts, qty = _extract(r)
        if not _is_iso(ts):
            continue
        parsed.append((datetime.strptime(ts, "%Y-%m-%d %H:%M:%S"), float(qty)))
    return parsed


def compute_weighted_daily_avg(movements: list[tuple[datetime, float]]) -> float:
    """Linear-decay weighted average over the last WINDOW_DAYS days.

    Mirrors blueprints.forecast_pure.compute_daily_avg exactly so the
    two endpoints agree. Returns 0.0 if no recent movements.
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
        w_ = WINDOW_DAYS - days_ago
        weighted_sum += w_ * float(qty)
        weight_sum += w_
    if weight_sum == 0:
        return 0.0
    raw = weighted_sum / weight_sum
    return float(Decimal(str(raw)).quantize(Decimal('0.01')))


def raw_30d_sum(db: sqlite3.Connection, item_id: int) -> float:
    """Raw (unweighted) sum of an item's 30-day consumption.

    Same source as fetch_item_movements_30d. Used by callers that need
    the total over 30 days (e.g. /inventory sums the last 7 days; we
    expose 30 here for consistency with the forecast window).
    """
    row = db.execute(
        """SELECT COALESCE(SUM(qty), 0) AS total FROM (
              SELECT o.requested_quantity AS qty
              FROM outbound_requests o
              WHERE o.item_id = ? AND o.rolled_back = 0
                AND created_at >= datetime('now', '-30 days')
              UNION ALL
              SELECT pri.actual_qty AS qty
              FROM production_run_items pri
              JOIN production_runs pr ON pr.id = pri.run_id
              WHERE pri.item_id = ? AND pr.rolled_back = 0
                AND pr.created_at >= datetime('now', '-30 days')
           )""",
        (item_id, item_id),
    ).fetchone()
    if isinstance(row, sqlite3.Row):
        val = row["total"]
    else:
        val = row[0]
    return float(val or 0)


def count_30d_records(db: sqlite3.Connection, item_id: int) -> int:
    """Count of consumption records in last 30 days (outbound + production).

    Used for cold-start threshold (PRD §2.1.3: < 7 → cold_start).
    """
    row = db.execute(
        """SELECT COUNT(*) AS c FROM (
              SELECT o.id
              FROM outbound_requests o
              WHERE o.item_id = ? AND o.rolled_back = 0
                AND created_at >= datetime('now', '-30 days')
              UNION ALL
              SELECT pri.id
              FROM production_run_items pri
              JOIN production_runs pr ON pr.id = pri.run_id
              WHERE pri.item_id = ? AND pr.rolled_back = 0
                AND pr.created_at >= datetime('now', '-30 days')
           )""",
        (item_id, item_id),
    ).fetchone()
    if isinstance(row, sqlite3.Row):
        val = row["c"]
    else:
        val = row[0]
    return int(val or 0)
