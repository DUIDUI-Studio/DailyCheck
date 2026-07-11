"""Inventory MCP Tools."""
from __future__ import annotations

import os
from mcp_server.service.auth import authenticate, check_path, check_warehouse
from mcp_server.service.inventory import (
    list_items as svc_list_items,
    get_item as svc_get_item,
    list_movements as svc_list_movements,
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


def items_list_impl(args: dict) -> list[dict]:
    warehouse_code = args.get("warehouse_code")
    ctx = _get_ctx()
    if not check_path(ctx, "GET", "/api/v1/items"):
        raise ForbiddenError("forbidden_path")
    if warehouse_code and not check_warehouse(ctx, warehouse_code):
        raise ForbiddenError("forbidden_warehouse")
    return svc_list_items(warehouse_code, ctx)


def items_detail_impl(args: dict) -> list[dict]:
    item_id = args.get("item_id")
    warehouse_code = args.get("warehouse_code")
    ctx = _get_ctx()
    if not check_path(ctx, "GET", "/api/v1/items/<id>"):
        raise ForbiddenError("forbidden_path")
    if warehouse_code and not check_warehouse(ctx, warehouse_code):
        raise ForbiddenError("forbidden_warehouse")
    result = svc_get_item(item_id, warehouse_code, ctx)
    # get_item returns a single dict; wrap in list for consistency
    return [result] if result else []


def movements_list_impl(args: dict) -> list[dict]:
    warehouse_code = args.get("warehouse_code")
    ctx = _get_ctx()
    if not check_path(ctx, "GET", "/api/v1/movements"):
        raise ForbiddenError("forbidden_path")
    if warehouse_code and not check_warehouse(ctx, warehouse_code):
        raise ForbiddenError("forbidden_warehouse")
    return svc_list_movements(warehouse_code, ctx)
