"""Forecast MCP Tools."""
from __future__ import annotations

import os
from mcp_server.service.auth import authenticate, check_path, check_warehouse
from mcp_server.service.forecast import get_forecast as svc_get_forecast
from mcp_server.infra.errors import UnauthorizedError, ForbiddenError


def _get_ctx():
    """Extract and validate AuthContext from the env token."""
    token = os.environ.get("DAILYCHECK_MCP_TOKEN")
    if not token:
        raise UnauthorizedError("DAILYCHECK_MCP_TOKEN not set")
    ctx = authenticate(f"Bearer {token}")
    if ctx is None:
        raise UnauthorizedError("invalid token")
    return ctx


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
