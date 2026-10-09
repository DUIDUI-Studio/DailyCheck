"""Consumption MCP Tools."""
from __future__ import annotations


from mcp_server.service.auth import resolve_ctx
from mcp_server.service.consumption import (
    item_consumption as svc_item_consumption,
)
from mcp_server.service.consumption import (
    warehouse_consumption as svc_warehouse_consumption,
)


def _get_ctx():
    """Return the AuthContext for this call.

    Request-scoped when the token arrived over HTTP (published by
    AuthMiddleware); falls back to the process env token for stdio.
    """
    return resolve_ctx()


def warehouse_consumption_impl(args: dict) -> list[dict]:
    """Return per-item consumption summary for a warehouse.

    Includes rank, consume_qty, daily_avg, turnover_rate, consume_pct.
    Mirrors /inventory page consumption calculation exactly.
    """
    warehouse_code: str = args["warehouse_code"]
    days: int = args.get("days", 7)
    sort_by: str = args.get("sort_by", "qty")
    limit: int = min(args.get("limit", 100), 200)
    ctx = _get_ctx()
    return svc_warehouse_consumption(
        warehouse_code=warehouse_code,
        ctx=ctx,
        days=days,
        sort_by=sort_by,
        limit=limit,
    )


def item_consumption_impl(args: dict) -> dict:
    """Return consumption stats for a single item.

    Includes: 7d, 14d, 30d, monthly totals + weekly breakdown + daily avg.
    Optional: inventory_turnover (stocktake-anchored, opt-in via include_turnover).
    """
    item_id: int = args["item_id"]
    warehouse_code: str = args["warehouse_code"]
    include_turnover: bool = bool(args.get("include_turnover", False))
    turnover_days: int = args.get("turnover_days", 30)
    ctx = _get_ctx()
    return svc_item_consumption(
        item_id=item_id,
        warehouse_code=warehouse_code,
        ctx=ctx,
        include_turnover=include_turnover,
        turnover_days=turnover_days,
    )
