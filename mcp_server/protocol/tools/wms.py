"""WMS bridge MCP tools — 只读查询外部 WMS（朴宇冷库）中本货主的数据。"""
from __future__ import annotations

from mcp_server.infra.errors import ForbiddenError
from mcp_server.service.auth import check_path, resolve_ctx
from mcp_server.service.wms import list_documents as svc_list_documents
from mcp_server.service.wms import list_stock as svc_list_stock
from mcp_server.service.wms import summarize_stock as svc_summarize_stock


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
