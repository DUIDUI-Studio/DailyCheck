"""Forecast MCP Tools."""
from __future__ import annotations

from mcp_server.service.auth import check_path, check_warehouse, resolve_ctx
from mcp_server.service.forecast import get_forecast as svc_get_forecast
from mcp_server.infra.errors import ForbiddenError


def _get_ctx():
    """Return the AuthContext for this call.

    Request-scoped when the token arrived over HTTP (published by
    AuthMiddleware); falls back to the process env token for stdio.
    """
    return resolve_ctx()


def item_forecast_impl(args: dict) -> dict:
    item_id: int = args["item_id"]
    warehouse_code: str = args["warehouse_code"]
    horizon_days: int | None = args.get("horizon_days")
    ctx = _get_ctx()
    if not check_path(ctx, "GET", "/api/v1/forecast/item/<id>"):
        raise ForbiddenError("forbidden_path")
    if not check_warehouse(ctx, warehouse_code):
        raise ForbiddenError("forbidden_warehouse")
    return svc_get_forecast(item_id, warehouse_code, horizon_days, ctx)
