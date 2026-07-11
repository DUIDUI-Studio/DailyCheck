"""出库服务。"""
from __future__ import annotations

from mcp_server.data.master import resolve_warehouse
from mcp_server.data.warehouse import (
    create_outbound as _create_outbound,
    item_exists,
    list_outbound as _list_outbound,
    rollback_outbound as _rollback_outbound,
)
from mcp_server.data.unit_of_work import master_connection, warehouse_connection
from mcp_server.service.auth import AuthContext, check_warehouse
from mcp_server.infra.errors import (
    ForbiddenError,
    NotFoundError,
    ValidationError,
)


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
) -> dict:
    """创建出库记录：扣减库存 + 写 stock_movements。

    与 blueprints/outbound.py outbound_submit() 完全一致。
    """
    if not warehouse_code:
        raise ValidationError("warehouse_code required")
    if not check_warehouse(ctx, warehouse_code):
        raise ForbiddenError("forbidden_warehouse")
    if quantity <= 0:
        raise ValidationError("quantity must be positive")
    if item_id <= 0:
        raise ValidationError("item_id must be positive")
    wh = resolve_warehouse(warehouse_code)
    if wh is None:
        raise NotFoundError("warehouse_not_found")
    with warehouse_connection(wh["db_path"]) as conn:
        if not item_exists(conn, item_id):
            raise NotFoundError("item_not_found")
        # Check stock
        row = conn.execute(
            "SELECT quantity FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("item_not_found")
        if float(row["quantity"]) < quantity:
            raise ValidationError(
                f"insufficient_stock: available={row['quantity']}, requested={quantity}"
            )
        req_id = _create_outbound(conn, item_id, quantity, reason)
        _mark_invalid(item_id)
        return {
            "id": req_id,
            "item_id": item_id,
            "quantity": quantity,
            "warehouse_code": warehouse_code,
        }


def list_outbound(warehouse_code: str, ctx: AuthContext) -> list[dict]:
    if not warehouse_code:
        raise ValidationError("warehouse_code required")
    if not check_warehouse(ctx, warehouse_code):
        raise ForbiddenError("forbidden_warehouse")
    wh = resolve_warehouse(warehouse_code)
    if wh is None:
        raise NotFoundError("warehouse_not_found")
    with warehouse_connection(wh["db_path"]) as conn:
        return _list_outbound(conn)


def rollback_outbound(
    request_id: int,
    warehouse_code: str,
    ctx: AuthContext,
) -> dict:
    """回退一条出库记录：归还库存。"""
    if not warehouse_code:
        raise ValidationError("warehouse_code required")
    if not check_warehouse(ctx, warehouse_code):
        raise ForbiddenError("forbidden_warehouse")
    wh = resolve_warehouse(warehouse_code)
    if wh is None:
        raise NotFoundError("warehouse_not_found")
    with warehouse_connection(wh["db_path"]) as conn:
        try:
            req = _rollback_outbound(conn, request_id)
        except ValueError as e:
            msg = str(e)
            if "not_found" in msg:
                raise NotFoundError("outbound_request_not_found")
            if "already_rolled_back" in msg:
                raise ValidationError("already_rolled_back")
            raise
        _mark_invalid(req["item_id"])
        return {"id": request_id, "item_id": req["item_id"], "quantity": float(req["requested_quantity"])}
