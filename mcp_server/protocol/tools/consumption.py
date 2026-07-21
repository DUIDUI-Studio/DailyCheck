"""Consumption MCP Tools."""
from __future__ import annotations

import os
from mcp_server.service.auth import authenticate
from mcp_server.service.consumption import (
    warehouse_consumption as svc_warehouse_consumption,
    item_consumption as svc_item_consumption,
)
from mcp_server.infra.errors import UnauthorizedError


def _get_ctx():
    token = os.environ.get("DAILYCHECK_MCP_TOKEN")
    if not token:
        raise UnauthorizedError("DAILYCHECK_MCP_TOKEN not set")
    ctx = authenticate(f"Bearer {token}")
    if ctx is None:
        raise UnauthorizedError("invalid token")
    return ctx


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
    """
    item_id: int = args["item_id"]
    warehouse_code: str = args["warehouse_code"]
    ctx = _get_ctx()
    return svc_item_consumption(
        item_id=item_id,
        warehouse_code=warehouse_code,
        ctx=ctx,
    )
