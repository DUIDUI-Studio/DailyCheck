"""库存查询服务。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import config as root_config
from mcp_server.data.master import list_all_warehouses, resolve_warehouse
from mcp_server.data.unit_of_work import warehouse_connection
from mcp_server.data.warehouse import (
    get_item as _get_item,
)
from mcp_server.data.warehouse import (
    list_items as _list_items,
)
from mcp_server.data.warehouse import (
    list_movements as _list_movements,
)
from mcp_server.infra.errors import ForbiddenError, McpError, NotFoundError, ValidationError
from mcp_server.readonly import (
    ReadonlyPolicyError,
    read_all_warehouses,
    read_warehouse_metadata,
    readonly_connection,
)
from mcp_server.service.auth import AuthContext, check_warehouse
from warehouse_paths import resolve_warehouse_path


def list_warehouses_for_token(ctx: AuthContext) -> list[dict]:
    """List all warehouses, filtered by token's allowed_warehouses whitelist."""
    policy = ctx.readonly_policy
    if policy is not None:
        try:
            all_wh = [
                row for row in read_all_warehouses(policy)
                if row["code"] in policy.warehouses
            ]
        except ReadonlyPolicyError as exc:
            raise _unavailable_error() from exc
    else:
        all_wh = list_all_warehouses()
    result = []
    for wh in all_wh:
        if check_warehouse(ctx, wh["code"]):
            result.append({"code": wh["code"], "name": wh["name"]})
    return result


def list_items(warehouse_code: str, ctx: AuthContext) -> list[dict]:
    _validate_warehouse(warehouse_code, ctx)
    strict_path = _strict_warehouse_file(warehouse_code, ctx)
    if strict_path is not None:
        return _strict_read(strict_path, _list_items)
    wh = resolve_warehouse(warehouse_code)
    if wh is None:
        raise NotFoundError("warehouse_not_found")
    with warehouse_connection(wh["db_path"], read_only=ctx.read_only, base_dir=ctx.warehouse_base_dir) as conn:
        return _list_items(conn)


def get_item(item_id: int, warehouse_code: str, ctx: AuthContext) -> dict:
    _validate_warehouse(warehouse_code, ctx)
    strict_path = _strict_warehouse_file(warehouse_code, ctx)
    if strict_path is not None:
        item = _strict_read(strict_path, lambda conn: _get_item(conn, item_id))
    else:
        wh = resolve_warehouse(warehouse_code)
        if wh is None:
            raise NotFoundError("warehouse_not_found")
        with warehouse_connection(wh["db_path"], read_only=ctx.read_only, base_dir=ctx.warehouse_base_dir) as conn:
            item = _get_item(conn, item_id)
    if item is None:
        raise NotFoundError("not_found")
    return item


def list_movements(warehouse_code: str, ctx: AuthContext) -> list[dict]:
    _validate_warehouse(warehouse_code, ctx)
    wh = resolve_warehouse(warehouse_code)
    if wh is None:
        raise NotFoundError("warehouse_not_found")
    with warehouse_connection(wh["db_path"], read_only=ctx.read_only, base_dir=ctx.warehouse_base_dir) as conn:
        return _list_movements(conn)


def _validate_warehouse(warehouse_code: str, ctx: AuthContext) -> None:
    if not warehouse_code:
        raise ValidationError("warehouse_code_required")
    if not check_warehouse(ctx, warehouse_code):
        raise ForbiddenError("forbidden_warehouse")


def _strict_warehouse_file(warehouse_code: str, ctx: AuthContext) -> Path | None:
    """Return the exact approved file for a strict read, or None in standard mode."""
    policy = ctx.readonly_policy
    if policy is None:
        return None
    approved = policy.approved_file(warehouse_code)
    if approved is None:
        # Cross-warehouse code: fail before resolving/opening any target.
        raise ForbiddenError("forbidden_warehouse")
    try:
        metadata = read_warehouse_metadata(policy, warehouse_code)
    except ReadonlyPolicyError as exc:
        raise _unavailable_error() from exc
    if metadata is None:
        raise NotFoundError("warehouse_not_found")
    try:
        resolved = resolve_warehouse_path(
            metadata["db_path"], root_config.BASE_DIR, db_dir=root_config.DB_DIR
        ).resolve()
    except (OSError, TypeError, ValueError) as exc:
        raise ForbiddenError("forbidden_warehouse") from exc
    if resolved != approved:
        # Metadata re-pointed to another permitted warehouse, a traversal, or a
        # symlink target: root containment alone is not enough.
        raise ForbiddenError("forbidden_warehouse")
    return approved


def _strict_read(path: Path, reader):
    try:
        with readonly_connection(path) as conn:
            return reader(conn)
    except ReadonlyPolicyError as exc:
        raise _unavailable_error() from exc
    except sqlite3.Error as exc:
        raise _database_error(exc) from exc


def _database_error(exc: sqlite3.Error) -> McpError:
    message = str(exc).lower()
    if "no such table" in message or "no such column" in message:
        return McpError(
            "warehouse_schema_incompatible", "warehouse schema is incompatible", 503
        )
    return _unavailable_error()


def _unavailable_error() -> McpError:
    return McpError(
        "warehouse_unavailable", "warehouse data source unavailable", 503
    )
