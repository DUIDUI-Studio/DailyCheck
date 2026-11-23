"""WMS bridge MCP tools — 只读查询外部 WMS（朴宇冷库）中本货主的数据。"""
from __future__ import annotations

from mcp_server.infra.errors import ForbiddenError, McpError, ValidationError
from mcp_server.service import wms, wms_web
from mcp_server.service.auth import _current_auth, check_path, resolve_ctx
from mcp_server.service.policy import PUYU_WAREHOUSE_ID, WMS_LINES_ACTION, WMS_LINES_FIELDS
from mcp_server.service.wms import list_documents as svc_list_documents
from mcp_server.service.wms import list_stock as svc_list_stock
from mcp_server.service.wms import summarize_stock as svc_summarize_stock

MAX_DOC_NO_LENGTH = 256
_PUYU_OWNER_ID = 155
_PUYU_OWNER_CODE = "RTS012"


def _get_ctx():
    return resolve_ctx()


def wms_stock_list_impl(args: dict, *, ctx=None) -> list[dict]:
    ctx = ctx if ctx is not None else _get_ctx()
    if not check_path(ctx, "GET", "/api/v1/wms/stock"):
        raise ForbiddenError("forbidden_path")
    return svc_list_stock(limit=args.get("limit"), goods_code=args.get("goods_code"))


def wms_stock_summary_impl(args: dict, *, ctx=None) -> list[dict]:
    ctx = ctx if ctx is not None else _get_ctx()
    if not check_path(ctx, "GET", "/api/v1/wms/stock/summary"):
        raise ForbiddenError("forbidden_path")
    return svc_summarize_stock()


def wms_documents_list_impl(args: dict, *, ctx=None) -> list[dict]:
    ctx = ctx if ctx is not None else _get_ctx()
    if not check_path(ctx, "GET", "/api/v1/wms/documents"):
        raise ForbiddenError("forbidden_path")
    return svc_list_documents(
        direction=args.get("direction") or "in",
        date_from=args.get("date_from"),
        limit=args.get("limit"),
    )


def wms_document_lines_impl(args: dict, *, ctx=None) -> dict:
    if (
        ctx is None or ctx is not _current_auth.get() or ctx.policy is None
        or ctx.policy.wms_warehouse_id != PUYU_WAREHOUSE_ID
        or ctx.policy.fields(WMS_LINES_ACTION) != WMS_LINES_FIELDS
    ):
        raise ForbiddenError("forbidden_tool")
    doc_no = args.get("doc_no")
    direction = args.get("direction")
    if (
        args.keys() != {"doc_no", "direction"} or direction not in ("in", "out")
        or not isinstance(doc_no, str) or not doc_no.strip() or len(doc_no) > MAX_DOC_NO_LENGTH
        or any(ord(ch) < 32 or 127 <= ord(ch) <= 159 or 0xD800 <= ord(ch) <= 0xDFFF for ch in doc_no)
    ):
        raise ValidationError("invalid_arguments")
    cfg = wms.load_config(strict_customer=True)
    if cfg.customer_id != _PUYU_OWNER_ID:
        raise McpError("wms_owner_unconfirmed", "WMS owner configuration does not match", 503)
    rows = wms_web.document_lines_by_number(
        cfg, wms_web.Direction(direction), doc_no,
        warehouse_id=ctx.policy.wms_warehouse_id, owner_code=_PUYU_OWNER_CODE,
    )
    if not rows:
        raise McpError("wms_query_failed", "Missing document match metadata", 502)
    if rows[0]["owner_verified"] != 1:
        raise McpError("wms_owner_unconfirmed", "WMS owner code does not match", 503)
    if rows[0]["match_count"] != 1:
        return {
            "status": "ambiguous" if rows[0]["match_count"] > 1 else "not_found",
            "document": None, "lines": [],
        }
    lines = [
        {field: row[field] for field in WMS_LINES_FIELDS}
        for row in rows[:wms_web.MAX_DETAIL_ROWS] if row["line_id"] is not None
    ]
    if len(rows) > wms_web.MAX_DETAIL_ROWS:
        status = "truncated"
    else:
        status = "complete" if lines else "empty"
    return {"status": status, "document": {"direction": direction, "doc_no": doc_no}, "lines": lines}
