"""Inbound MCP Tools."""
from __future__ import annotations

import os
from mcp_server.service.auth import authenticate, check_path, check_warehouse
from mcp_server.service.inbound import (
    create_restock as svc_create_restock,
    list_restock as svc_list_restock,
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


def restock_create_impl(args: dict) -> dict:
    item_id: int = args["item_id"]
    quantity: int = args["quantity"]
    warehouse_code: str = args["warehouse_code"]
    reason: str | None = args.get("reason")
    ctx = _get_ctx()
    if not check_path(ctx, "POST", "/api/v1/restock"):
        raise ForbiddenError("forbidden_path")
    if not check_warehouse(ctx, warehouse_code):
        raise ForbiddenError("forbidden_warehouse")
    return svc_create_restock(item_id, quantity, warehouse_code, ctx, reason)


def restock_list_impl(args: dict) -> list[dict]:
    warehouse_code: str = args["warehouse_code"]
    ctx = _get_ctx()
    if not check_path(ctx, "GET", "/api/v1/restock"):
        raise ForbiddenError("forbidden_path")
    if not check_warehouse(ctx, warehouse_code):
        raise ForbiddenError("forbidden_warehouse")
    return svc_list_restock(warehouse_code, ctx)
