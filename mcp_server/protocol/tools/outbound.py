"""Outbound MCP Tools."""
from __future__ import annotations

from mcp_server.service.auth import check_path, check_warehouse, resolve_ctx
from mcp_server.service.outbound import (
    create_outbound as svc_create_outbound,
    list_outbound as svc_list_outbound,
    rollback_outbound as svc_rollback_outbound,
)
from mcp_server.infra.errors import ForbiddenError


def _get_ctx():
    """Return the AuthContext for this call.

    Request-scoped when the token arrived over HTTP (published by
    AuthMiddleware); falls back to the process env token for stdio.
    """
    return resolve_ctx()


def outbound_create_impl(args: dict) -> dict:
    """Create an outbound request: deduct stock + write movement record.

    Mirrors the Flask /outbound/submit endpoint exactly.
    """
    item_id: int = args["item_id"]
    quantity: float = args["quantity"]
    warehouse_code: str = args["warehouse_code"]
    reason: str | None = args.get("reason")
    ctx = _get_ctx()
    if not check_path(ctx, "POST", "/api/v1/outbound"):
        raise ForbiddenError("forbidden_path")
    return svc_create_outbound(item_id, quantity, warehouse_code, ctx, reason)


def outbound_list_impl(args: dict) -> list[dict]:
    warehouse_code: str = args["warehouse_code"]
    ctx = _get_ctx()
    if not check_path(ctx, "GET", "/api/v1/outbound"):
        raise ForbiddenError("forbidden_path")
    return svc_list_outbound(warehouse_code, ctx)


def outbound_rollback_impl(args: dict) -> dict:
    """Roll back an outbound request: return stock to warehouse."""
    request_id: int = args["request_id"]
    warehouse_code: str = args["warehouse_code"]
    ctx = _get_ctx()
    if not check_path(ctx, "POST", "/api/v1/outbound/rollback"):
        raise ForbiddenError("forbidden_path")
    return svc_rollback_outbound(request_id, warehouse_code, ctx)
