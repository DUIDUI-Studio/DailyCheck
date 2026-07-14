"""库存查询服务。"""
from __future__ import annotations

from mcp_server.data.master import resolve_warehouse, list_all_warehouses
from mcp_server.data.warehouse import (
    list_items as _list_items,
    get_item as _get_item,
    list_movements as _list_movements,
)
from mcp_server.data.unit_of_work import warehouse_connection
from mcp_server.service.auth import AuthContext, check_warehouse
from mcp_server.infra.errors import ForbiddenError, NotFoundError, ValidationError


def list_warehouses_for_token(ctx: AuthContext) -> list[dict]:
    """List all warehouses, filtered by token's allowed_warehouses whitelist."""
    all_wh = list_all_warehouses()
    result = []
    for wh in all_wh:
        if check_warehouse(ctx, wh["code"]):
            result.append({"code": wh["code"], "name": wh["name"]})
    return result


def list_items(warehouse_code: str, ctx: AuthContext) -> list[dict]:
    _validate_warehouse(warehouse_code, ctx)
    wh = resolve_warehouse(warehouse_code)
    if wh is None:
        raise NotFoundError("warehouse_not_found")
    with warehouse_connection(wh["db_path"]) as conn:
        return _list_items(conn)


def get_item(item_id: int, warehouse_code: str, ctx: AuthContext) -> dict:
    _validate_warehouse(warehouse_code, ctx)
    wh = resolve_warehouse(warehouse_code)
    if wh is None:
        raise NotFoundError("warehouse_not_found")
    with warehouse_connection(wh["db_path"]) as conn:
        item = _get_item(conn, item_id)
    if item is None:
        raise NotFoundError("not_found")
    return item


def list_movements(warehouse_code: str, ctx: AuthContext) -> list[dict]:
    _validate_warehouse(warehouse_code, ctx)
    wh = resolve_warehouse(warehouse_code)
    if wh is None:
        raise NotFoundError("warehouse_not_found")
    with warehouse_connection(wh["db_path"]) as conn:
        return _list_movements(conn)


def _validate_warehouse(warehouse_code: str, ctx: AuthContext) -> None:
    if not warehouse_code:
        raise ValidationError("warehouse_code_required")
    if not check_warehouse(ctx, warehouse_code):
        raise ForbiddenError("forbidden_warehouse")
