"""Forecast 服务，复用 forecast_pure.py。"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from mcp_server.data.master import resolve_warehouse
from mcp_server.data.warehouse import item_exists
from mcp_server.data.unit_of_work import warehouse_connection
from mcp_server.service.auth import AuthContext, check_warehouse
from mcp_server.infra.errors import ForbiddenError, NotFoundError, ValidationError

# 复用 forecast_pure 的核心逻辑
from blueprints.forecast_pure import compute_daily_avg, classify_confidence, compute_forecast_total
from blueprints.consumption import fetch_item_movements_30d


def parse_horizon(raw) -> int | None:
    """Return int horizon in [1, 90] or None on invalid input."""
    if raw is None:
        return 14  # default horizon
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    if n < 1 or n > 90:
        return None
    return n


def get_forecast(
    item_id: int,
    warehouse_code: str,
    horizon_days: int | None,
    ctx: AuthContext,
) -> dict:
    if not warehouse_code:
        raise ValidationError("warehouse_code_required")
    if not check_warehouse(ctx, warehouse_code):
        raise ForbiddenError("forbidden_warehouse")
    wh = resolve_warehouse(warehouse_code)
    if wh is None:
        raise NotFoundError("warehouse_not_found")
    horizon = parse_horizon(horizon_days)
    if horizon is None:
        raise ValidationError("invalid_horizon")
    with warehouse_connection(wh["db_path"]) as conn:
        if not item_exists(conn, item_id):
            raise NotFoundError("not_found")
        movements = fetch_item_movements_30d(conn, item_id)
        # Build forecast using pure functions
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
        body = {
            "item_id": item_id,
            "warehouse_code": warehouse_code,
            "horizon_days": horizon,
            "daily_avg": daily_avg,
            "forecast_total": forecast_total,
            "confidence": confidence,
            "computed_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "data_status": data_status,
        }
        return body
