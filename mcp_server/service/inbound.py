"""入库服务。"""
from __future__ import annotations

from mcp_server.data.master import resolve_warehouse
from mcp_server.data.warehouse import (
    create_restock as _create_restock,
    item_exists,
    list_restock_movements as _list_restock_movements,
)
from mcp_server.data.unit_of_work import warehouse_connection
from mcp_server.service.auth import AuthContext, check_warehouse
from mcp_server.infra.errors import (
    ForbiddenError,
    NotFoundError,
    ValidationError,
)


def create_restock(
    item_id: int,
    quantity: int,
    warehouse_code: str,
    ctx: AuthContext,
    reason: str | None = None,
) -> dict:
    if not warehouse_code:
        raise ValidationError("warehouse_code_required")
    if not check_warehouse(ctx, warehouse_code):
        raise ForbiddenError("forbidden_warehouse")
    if quantity <= 0:
        raise ValidationError("quantity must be positive")
    wh = resolve_warehouse(warehouse_code)
    if wh is None:
        raise NotFoundError("warehouse_not_found")
    with warehouse_connection(wh["db_path"]) as conn:
        if not item_exists(conn, item_id):
            raise NotFoundError("item_not_found")
        row_id = _create_restock(conn, item_id, quantity, reason)
        return {
            "id": row_id,
            "item_id": item_id,
            "quantity": quantity,
            "warehouse_code": warehouse_code,
        }


def list_restock(warehouse_code: str, ctx: AuthContext) -> list[dict]:
    if not warehouse_code:
        raise ValidationError("warehouse_code_required")
    if not check_warehouse(ctx, warehouse_code):
        raise ForbiddenError("forbidden_warehouse")
    wh = resolve_warehouse(warehouse_code)
    if wh is None:
        raise NotFoundError("warehouse_not_found")
    with warehouse_connection(wh["db_path"]) as conn:
        return _list_restock_movements(conn)
