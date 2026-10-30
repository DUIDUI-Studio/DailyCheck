"""出库服务。"""
from __future__ import annotations

import sqlite3

from mcp_server.data.master import resolve_warehouse
from mcp_server.data.unit_of_work import master_connection, warehouse_connection
from mcp_server.data.warehouse import count_outbound as _count_outbound
from mcp_server.data.warehouse import create_outbound as _create_outbound
from mcp_server.data.warehouse import list_outbound as _list_outbound
from mcp_server.data.warehouse import rollback_outbound as _rollback_outbound
from mcp_server.infra.errors import (
    ForbiddenError,
    NotFoundError,
    ValidationError,
    WriteError,
)
from mcp_server.service import document_writes as documents
from mcp_server.service.auth import AuthContext, check_warehouse
from mcp_server.service.pagination import parse_limit, parse_offset


def _mark_invalid(item_id: int) -> None:
    """Mark procurement_cache for this item as invalid (forces recompute on next read)."""
    with master_connection() as conn:
        conn.execute(
            "UPDATE OR IGNORE procurement_cache SET invalid = 1 WHERE item_id = ?",
            (item_id,),
        )
        conn.commit()


def create_outbound(
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
    response = documents.create("outbound", payload, warehouse_code, wh, reason,
                                warehouse_connection, _create_outbound)
    documents.invalidate(_mark_invalid, item_id, response["id"], payload.doc_no is not None)
    return response


def list_outbound(
    warehouse_code: str,
    ctx: AuthContext,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    if not warehouse_code:
        raise ValidationError("warehouse_code required")
    if not check_warehouse(ctx, warehouse_code):
        raise ForbiddenError("forbidden_warehouse")
    wh = resolve_warehouse(warehouse_code)
    if wh is None:
        raise NotFoundError("warehouse_not_found")
    limit = parse_limit(limit)
    offset = parse_offset(offset)
    with warehouse_connection(wh["db_path"]) as conn:
        items = _list_outbound(conn, limit=limit, offset=offset)
        total = _count_outbound(conn)
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "truncated": total > offset + len(items),
    }


def rollback_outbound(
    request_id: int,
    warehouse_code: str,
    ctx: AuthContext,
) -> dict:
    """Return stored effective quantity at most once, including Web races."""
    wh = documents.resolve(warehouse_code, ctx, resolve_warehouse)
    already = False
    try:
        with warehouse_connection(wh["db_path"]) as conn:
            conn.execute("PRAGMA busy_timeout=5000")
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    req = _rollback_outbound(conn, request_id)
                except ValueError as exc:
                    if str(exc) == "already_rolled_back":
                        already = True
                        req = conn.execute("SELECT * FROM outbound_requests WHERE id=?",
                                           (request_id,)).fetchone()
                    elif "not_found" in str(exc):
                        raise NotFoundError("outbound_request_not_found") from None
                    else:
                        raise
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc) or "busy" in str(exc):
            raise WriteError("database_busy", "database_busy", 503, retry_safe=True) from None
        raise
    documents.invalidate(_mark_invalid, req["item_id"], request_id, True)
    if already:
        raise ValidationError("already_rolled_back")
    return {"id": request_id, "item_id": req["item_id"], "quantity": float(req["requested_quantity"])}
