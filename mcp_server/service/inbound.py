"""入库服务。"""
from __future__ import annotations

from mcp_server.data.master import resolve_warehouse
from mcp_server.data.unit_of_work import warehouse_connection
from mcp_server.data.warehouse import count_restock_movements as _count_restock_movements
from mcp_server.data.warehouse import create_restock as _create_restock
from mcp_server.data.warehouse import list_restock_movements as _list_restock_movements
from mcp_server.infra.errors import (
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from mcp_server.service import document_writes as documents
from mcp_server.service.auth import AuthContext, check_warehouse
from mcp_server.service.pagination import parse_limit, parse_offset


def create_restock(
    item_id: int,
    quantity: float,
    warehouse_code: str,
    ctx: AuthContext,
    reason: str | None = None,
    *,
    doc_no=documents.OMITTED,
    line_no=documents.OMITTED,
    factor=1,
) -> dict:
    documents.authorize(warehouse_code, ctx)
    payload = documents.validate(item_id, quantity, reason, doc_no, line_no, factor)
    wh = documents.resolve(warehouse_code, ctx, resolve_warehouse)
    return documents.create("restock", payload, warehouse_code, wh, reason,
                            warehouse_connection, _create_restock)


def list_restock(
    warehouse_code: str,
    ctx: AuthContext,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """List inbound restock requests (入库单) for a warehouse, paginated.

    Rows come from `restock_requests` (read path switched in Issue #72).
    Shape per row: {id, item_id, item_name, quantity, status, reason,
    created_at}, newest first. Envelope: {items, total, limit, offset,
    truncated} (Issue #74).
    """
    if not warehouse_code:
        raise ValidationError("warehouse_code_required")
    if not check_warehouse(ctx, warehouse_code):
        raise ForbiddenError("forbidden_warehouse")
    wh = resolve_warehouse(warehouse_code)
    if wh is None:
        raise NotFoundError("warehouse_not_found")
    limit = parse_limit(limit)
    offset = parse_offset(offset)
    with warehouse_connection(wh["db_path"]) as conn:
        items = _list_restock_movements(conn, limit=limit, offset=offset)
        total = _count_restock_movements(conn)
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "truncated": total > offset + len(items),
    }
