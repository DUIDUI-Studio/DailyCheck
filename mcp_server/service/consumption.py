"""消耗统计服务。"""
from __future__ import annotations

from mcp_server.data.master import resolve_warehouse
from mcp_server.data.warehouse import (
    get_item,
    list_consumption_summary as _list_summary,
    get_item_consumption as _get_item_consumption,
)
from mcp_server.data.unit_of_work import warehouse_connection
from mcp_server.service.auth import AuthContext, check_warehouse
from mcp_server.infra.errors import ForbiddenError, NotFoundError, ValidationError


def warehouse_consumption(
    warehouse_code: str,
    ctx: AuthContext,
    days: int = 7,
    sort_by: str = "qty",
    limit: int = 100,
) -> list[dict]:
    """Return per-item consumption summary for a warehouse.

    Includes: consume_qty, daily_avg, turnover_rate, rank, consume_pct.
    Sort options: qty (default), value, turnover, name.
    """
    if not warehouse_code:
        raise ValidationError("warehouse_code required")
    if not check_warehouse(ctx, warehouse_code):
        raise ForbiddenError("forbidden_warehouse")
    if days not in (7, 30):
        raise ValidationError("days must be 7 or 30")
    if sort_by not in ("qty", "value", "turnover", "name"):
        raise ValidationError("sort_by must be one of: qty, value, turnover, name")
    wh = resolve_warehouse(warehouse_code)
    if wh is None:
        raise NotFoundError("warehouse_not_found")
    with warehouse_connection(wh["db_path"]) as conn:
        return _list_summary(conn, days=days, sort_by=sort_by, limit=limit)


def item_consumption(
    item_id: int,
    warehouse_code: str,
    ctx: AuthContext,
) -> dict:
    """Return consumption stats for a single item: 7d / 30d / monthly / weekly breakdown."""
    if not warehouse_code:
        raise ValidationError("warehouse_code required")
    if not check_warehouse(ctx, warehouse_code):
        raise ForbiddenError("forbidden_warehouse")
    wh = resolve_warehouse(warehouse_code)
    if wh is None:
        raise NotFoundError("warehouse_not_found")
    with warehouse_connection(wh["db_path"]) as conn:
        if not get_item(conn, item_id):
            raise NotFoundError("item_not_found")
        return _get_item_consumption(conn, item_id)
