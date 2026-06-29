"""/forecast blueprint: HTTP routes for the daily-average / horizon
prediction feature (PRD §2.1).

Routes:
  GET  /forecast/item/<item_id>?horizon_days=14
  GET  /forecast/product/<product_id>?horizon_days=14
  POST /forecast/recompute              (TASK 6 — added in next commit)

Pure math lives in blueprints/forecast_pure.py; this module is the
HTTP/DB glue only.

Daily-batch scheduling is implemented as a module-level daemon thread
(see `_start_scheduler`) so the feature is self-contained without
introducing a heavyweight dependency.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from flask import Blueprint, abort, g, jsonify, request

from db import get_warehouse_db
from .forecast_pure import (
    classify_confidence,
    compute_daily_avg,
    compute_forecast_total,
)

bp = Blueprint("forecast", __name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_MIN_HORIZON = 1
_MAX_HORIZON = 90
_DEFAULT_HORIZON = 14


def _parse_horizon(raw) -> int | None:
    """Return int horizon in [1, 90] or None on invalid input."""
    if raw is None:
        return _DEFAULT_HORIZON
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    if n < _MIN_HORIZON or n > _MAX_HORIZON:
        return None
    return n


def _warehouse_code() -> str:
    return g.warehouse["code"] if g.warehouse else "unknown"


def _fetch_outbound_rows(item_id: int) -> list[tuple[datetime, float]]:
    """Return [(datetime, qty), ...] for non-rolled-back outbounds in last 30d."""
    db = get_warehouse_db()
    rows = db.execute(
        """SELECT requested_quantity, created_at
           FROM outbound_requests
           WHERE item_id = ? AND rolled_back = 0
             AND created_at >= datetime('now', '-30 days')""",
        (item_id,),
    ).fetchall()
    parsed: list[tuple[datetime, float]] = []
    for r in rows:
        try:
            ts = datetime.strptime(r["created_at"], "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue
        parsed.append((ts, float(r["requested_quantity"])))
    return parsed


def _fetch_production_rows(product_id: int) -> list[tuple[datetime, float]]:
    """Return [(datetime, qty), ...] for non-rolled-back production outputs in last 30d."""
    db = get_warehouse_db()
    rows = db.execute(
        """SELECT output_qty, created_at
           FROM production_runs
           WHERE product_id = ? AND rolled_back = 0
             AND created_at >= datetime('now', '-30 days')""",
        (product_id,),
    ).fetchall()
    parsed: list[tuple[datetime, float]] = []
    for r in rows:
        try:
            ts = datetime.strptime(r["created_at"], "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue
        parsed.append((ts, float(r["output_qty"])))
    return parsed


def _build_response(
    target_id: int,
    horizon: int,
    movements: list[tuple[datetime, float]],
) -> dict:
    """Assemble the JSON response (PRD §2.1.2 contract)."""
    n = len(movements)
    confidence = classify_confidence(n)
    if confidence == "cold_start":
        daily_avg = 0.0
        forecast_total = 0.0
        data_status = "cold_start"
    else:
        daily_avg = compute_daily_avg(movements)
        forecast_total = compute_forecast_total(daily_avg, horizon)
        data_status = "ok"
    return {
        "item_id": target_id,            # for product responses, this field
        "warehouse_code": _warehouse_code(),  # name is stable per spec
        "horizon_days": horizon,
        "daily_avg": daily_avg,
        "forecast_total": forecast_total,
        "confidence": confidence,
        "computed_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data_status": data_status,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@bp.route("/forecast/item/<int:item_id>", methods=["GET"])
def forecast_item(item_id: int):
    horizon = _parse_horizon(request.args.get("horizon_days"))
    if horizon is None:
        return jsonify({"error": "invalid_horizon"}), 400

    db = get_warehouse_db()
    if db.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone() is None:
        return jsonify({"error": "not_found"}), 404

    movements = _fetch_outbound_rows(item_id)
    return jsonify(_build_response(item_id, horizon, movements))


@bp.route("/forecast/product/<int:product_id>", methods=["GET"])
def forecast_product(product_id: int):
    horizon = _parse_horizon(request.args.get("horizon_days"))
    if horizon is None:
        return jsonify({"error": "invalid_horizon"}), 400

    db = get_warehouse_db()
    if db.execute("SELECT 1 FROM products WHERE id=?", (product_id,)).fetchone() is None:
        return jsonify({"error": "not_found"}), 404

    movements = _fetch_production_rows(product_id)
    body = _build_response(product_id, horizon, movements)
    # Spec §1: the field is "item_id" for both shapes (stability for Agent).
    # We keep the same key, just changing its semantic to the product id.
    return jsonify(body)


# ---------------------------------------------------------------------------
# Scheduler (TASK 8 — minimal placeholder; full impl in next commits)
# ---------------------------------------------------------------------------

_scheduler_lock = threading.Lock()
_scheduler_started = False


def _start_scheduler() -> None:
    """Idempotent — safe to call from create_app() multiple times."""
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        _scheduler_started = True
    # Full scheduler body is in TASK 8; placeholder keeps the symbol
    # import-safe for tests that import this module.
