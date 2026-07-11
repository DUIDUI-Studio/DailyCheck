"""Procurement 服务，复用 procurement_pure.py。"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from mcp_server.data.master import resolve_warehouse, list_all_warehouses
from mcp_server.data.unit_of_work import warehouse_connection
from mcp_server.service.auth import AuthContext, check_warehouse
from mcp_server.infra.errors import ForbiddenError, NotFoundError, ValidationError

from blueprints.procurement_pure import aggregate_hub


def _get_store_procurement_json():
    """Lazy import of _store_procurement_json from blueprints.procurement."""
    try:
        from blueprints.procurement import _store_procurement_json
        return _store_procurement_json
    except (ImportError, ModuleNotFoundError):
        return None


def procurement_store(warehouse_code: str, ctx: AuthContext) -> dict:
    if not warehouse_code:
        raise ValidationError("warehouse_code_required")
    if not check_warehouse(ctx, warehouse_code):
        raise ForbiddenError("forbidden_warehouse")
    store_func = _get_store_procurement_json()
    if store_func is None:
        raise NotFoundError("procurement_not_available")
    body = store_func(warehouse_code)
    if body is None:
        raise NotFoundError("warehouse_not_found")
    return body


def procurement_hub(
    ctx: AuthContext,
    warehouse_code: str | None = None,
) -> list[dict]:
    if warehouse_code and not check_warehouse(ctx, warehouse_code):
        raise ForbiddenError("forbidden_warehouse")
    if warehouse_code:
        wh = resolve_warehouse(warehouse_code)
        whs = [wh] if wh else []
    else:
        whs = [dict(r) for r in list_all_warehouses()]
    result = []
    store_func = _get_store_procurement_json()
    if store_func is None:
        return result
    for wh in whs:
        if wh is None:
            continue
        body = store_func(wh["code"])
        if body:
            result.append(body)
    return aggregate_hub(result)
