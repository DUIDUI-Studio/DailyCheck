"""Inbound MCP Tools."""
from __future__ import annotations

from mcp_server.infra.errors import ForbiddenError
from mcp_server.service.auth import check_path, check_warehouse, resolve_ctx
from mcp_server.service.inbound import create_restock as svc_create_restock
from mcp_server.service.inbound import list_restock as svc_list_restock


def _get_ctx():
    """Return the AuthContext for this call.

    Request-scoped when the token arrived over HTTP (published by
    AuthMiddleware); falls back to the process env token for stdio.
    """
    return resolve_ctx()


def restock_create_impl(args: dict, *, ctx=None) -> dict:
    item_id: int = args["item_id"]
    quantity: int = args["quantity"]
    warehouse_code: str = args["warehouse_code"]
    reason: str | None = args.get("reason")
    ctx = ctx if ctx is not None else _get_ctx()
    if not check_path(ctx, "POST", "/api/v1/restock"):
        raise ForbiddenError("forbidden_path")
    if not check_warehouse(ctx, warehouse_code):
        raise ForbiddenError("forbidden_warehouse")
    return svc_create_restock(item_id, quantity, warehouse_code, ctx, reason)


def restock_list_impl(args: dict, *, ctx=None) -> dict:
    warehouse_code: str = args["warehouse_code"]
    ctx = ctx if ctx is not None else _get_ctx()
    if not check_path(ctx, "GET", "/api/v1/restock"):
        raise ForbiddenError("forbidden_path")
    if not check_warehouse(ctx, warehouse_code):
        raise ForbiddenError("forbidden_warehouse")
    return svc_list_restock(
        warehouse_code,
        ctx,
        limit=args.get("limit", 100),
        offset=args.get("offset", 0),
    )
