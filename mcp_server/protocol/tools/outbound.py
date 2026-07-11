"""Outbound MCP Tools."""
from __future__ import annotations

import os
from mcp_server.service.auth import authenticate, check_path, check_warehouse
from mcp_server.service.outbound import (
    create_outbound as svc_create_outbound,
    list_outbound as svc_list_outbound,
    rollback_outbound as svc_rollback_outbound,
)
from mcp_server.infra.errors import UnauthorizedError, ForbiddenError


def _get_ctx():
    token = os.environ.get("DAILYCHECK_MCP_TOKEN")
    if not token:
        raise UnauthorizedError("DAILYCHECK_MCP_TOKEN not set")
    ctx = authenticate(f"Bearer {token}")
    if ctx is None:
        raise UnauthorizedError("invalid token")
    return ctx


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
