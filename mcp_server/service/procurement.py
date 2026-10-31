"""Procurement 服务，复用 procurement_pure.py。"""
from __future__ import annotations

from blueprints.procurement_pure import aggregate_hub
from mcp_server.data.master import list_all_warehouses, resolve_warehouse
from mcp_server.data.unit_of_work import master_connection
from mcp_server.infra.errors import ForbiddenError, NotFoundError, ValidationError
from mcp_server.service.auth import AuthContext, check_warehouse
from warehouse_paths import resolve_warehouse_path


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
    body = _readonly_store(warehouse_code, ctx) if ctx.read_only else store_func(warehouse_code)
    if body is None:
        raise NotFoundError("warehouse_not_found")
    return body


def procurement_hub(
    ctx: AuthContext,
    warehouse_code: str | None = None,
) -> list[dict]:
    if warehouse_code and not check_warehouse(ctx, warehouse_code):
        raise ForbiddenError("forbidden_warehouse")
    store_func = _get_store_procurement_json()
    if store_func is None:
        return []
    if warehouse_code:
        wh = resolve_warehouse(warehouse_code)
        whs = [wh] if wh else []
    else:
        whs = [dict(r) for r in list_all_warehouses()]
    result = []
    for wh in whs:
        if wh is None or not check_warehouse(ctx, wh["code"]):
            continue
        body = _readonly_store(wh["code"], ctx) if ctx.read_only else store_func(wh["code"])
        if body:
            result.append(body)
    return aggregate_hub(result)


def _readonly_store(warehouse_code: str, ctx: AuthContext) -> dict | None:
    """Thin no-cache adapter; schema initialization remains an app lifecycle action."""
    from datetime import datetime, timezone

    from blueprints.procurement import compute_store_procurement

    wh = resolve_warehouse(warehouse_code)
    if wh is None:
        return None
    with master_connection(read_only=True) as conn:
        row = conn.execute(
            "SELECT cover_days, min_absolute FROM procurement_config WHERE id=1"
        ).fetchone()
    values = (int(row["cover_days"]), float(row["min_absolute"])) if row else (14, 0.0)
    items = compute_store_procurement(
        warehouse_code, str(resolve_warehouse_path(wh["db_path"], ctx.warehouse_base_dir)),
        config_values=values, read_only=True,
    )
    for item in items:
        item.pop("_computed_at", None)
    return {
        "warehouse_code": warehouse_code,
        "computed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "items": items,
    }
