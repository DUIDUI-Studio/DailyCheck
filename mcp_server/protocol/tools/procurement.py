"""Procurement MCP Tools."""
from __future__ import annotations

import os
from mcp_server.service.auth import authenticate, check_path, check_warehouse
from mcp_server.service.procurement import (
    procurement_store as svc_store,
    procurement_hub as svc_hub,
)
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


def procurement_store_impl(args: dict) -> dict:
    warehouse_code: str = args["warehouse_code"]
    ctx = _get_ctx()
    if not check_path(ctx, "GET", "/api/v1/procurement/store"):
        raise ForbiddenError("forbidden_path")
    if not check_warehouse(ctx, warehouse_code):
        raise ForbiddenError("forbidden_warehouse")
    return svc_store(warehouse_code, ctx)


def procurement_hub_impl(args: dict) -> list[dict]:
    warehouse_code: str | None = args.get("warehouse_code")
    ctx = _get_ctx()
    if not check_path(ctx, "GET", "/api/v1/procurement/hub"):
        raise ForbiddenError("forbidden_path")
    if warehouse_code and not check_warehouse(ctx, warehouse_code):
        raise ForbiddenError("forbidden_warehouse")
    return svc_hub(ctx, warehouse_code)
