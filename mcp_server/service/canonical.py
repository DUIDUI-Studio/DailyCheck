"""品项主数据（canonical）服务层。

调用 blueprints.canonical_pure 中的纯函数（策略来源），不重写任何策略；
唯一额外的职责是：
  - 打开 master / 仓库连接（Q7 §7.7.2 前的常规操作）
  - 校验 token 的 path 白名单（caller 负责）
  - 把纯函数的 ValueError 抬高为 MCP 错误码

权限模型：
  - 所有路径以 GET / POST /api/v1/canonical* 映射到 Flask 蓝图路由
  - 写操作的「manager+」、「platform admin only」控制完全下沉到
    agent_tokens.allowed_write_paths —— 本服务不重复检查角色
  - 读操作同理（按 token ACL）

数据完整性：
  - 写仓操作（claim / unclaim / fanout）必须先调用 fanout / claim 内部的
    Q7 守卫（备份 + 三快照）。本层不绕过它们。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import blueprints.canonical_pure as cp
from config import BASE_DIR
from db import migrate_warehouse_db_columns
from mcp_server.data.master import resolve_warehouse
from mcp_server.data.unit_of_work import master_connection
from mcp_server.infra.errors import (
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from mcp_server.service.auth import AuthContext, check_warehouse

# ─────────────────────────────────────────────────────────────────────
# 内部 helpers
# ─────────────────────────────────────────────────────────────────────

def _open_warehouse(db_path: str | Path) -> sqlite3.Connection:
    """打开仓库连接（与 blueprints.canonical.py 中的 _open_warehouse 行为一致）。

    直连前先 migrate_warehouse_db_columns() 跑一次幂等列迁移，避免旧库
    撞 `no such column: is_active`。
    """
    p = Path(db_path)
    if not p.is_absolute():
        p = BASE_DIR / p
    migrate_warehouse_db_columns(p)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _validate_warehouse(warehouse_code: str, ctx: AuthContext) -> None:
    if not warehouse_code:
        raise ValidationError("warehouse_code required")
    if not check_warehouse(ctx, warehouse_code):
        raise ForbiddenError("forbidden_warehouse")


def _wrap_value_error(err: ValueError) -> None:
    """把纯函数抛的 ValueError 翻译成 MCP 错误。

    Q3=deactivate_only (set_canonical_status) / NewItemDenied / 字段白名单
    等都从 ValueError 派生；这里做一次透传（无需显式分类，错误消息已足够
    排查）。
    """
    raise ValidationError(str(err)) from err


# ─────────────────────────────────────────────────────────────────────
# Read
# ─────────────────────────────────────────────────────────────────────

def list_canonical(
    *,
    status: str | None = None,
    category_code: str | None = None,
    q: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict:
    """列主数据 + 命中总数。"""
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    with master_connection() as conn:
        items = cp.list_canonical_items(
            conn,
            status=status or None,
            category_code=category_code or None,
            q=q or None,
            limit=limit,
            offset=offset,
        )
        total = cp.count_canonical_items(
            conn,
            status=status or None,
            category_code=category_code or None,
            q=q or None,
        )
        categories = cp.list_canonical_categories(conn)
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "truncated": total > offset + len(items),
        "categories": categories,
    }


def get_canonical_detail(canonical_id: int) -> dict:
    """单条主数据 + 别名 + 跨仓绑定概览。"""
    with master_connection() as conn:
        try:
            detail = cp.get_canonical_item_detail(conn, int(canonical_id))
        except ValueError as e:
            raise NotFoundError(str(e)) from None
        bindings = cp.collect_bindings(conn, warehouse_types=cp.BINDING_ALL_TYPES)
    matching: list[dict] = []
    for wh_code, rows in bindings.items():
        for r in rows:
            if r["canonical_id"] == int(canonical_id):
                matching.append({"warehouse_code": wh_code, **r})
    return {
        "canonical": detail,
        "bindings": matching,
        "bound_warehouse_count": len({m["warehouse_code"] for m in matching}),
    }


def list_canonical_categories() -> list[dict]:
    """品类筛选下拉（附每品类主数据条数）。"""
    with master_connection() as conn:
        return cp.list_canonical_categories(conn)


def list_fanout_targets() -> list[dict]:
    """可作为扇出目标的仓：storefront / rd / distribution_center。

    2026-10-10 补：配送中心也是扇出目标 —— /items 的新增入口已全仓关闭，
    而门店订货目录从「DC 自己的 items 行」出发 (list_available_dc_items)，
    DC 没有绑定行 ⇒ 品项永远订不到。
    """
    with master_connection() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT code, name, warehouse_type FROM warehouses
               WHERE warehouse_type IN
                   ('storefront', 'rd', 'distribution_center')
               ORDER BY CASE warehouse_type
                          WHEN 'distribution_center' THEN 0
                          WHEN 'storefront'         THEN 1
                          ELSE 2 END,
                        code"""
        ).fetchall()
        return [dict(r) for r in rows]


def list_open_conflicts(canonical_id: int | None = None) -> list[dict]:
    """列出 status='open' 的冲突；可选按 canonical_id 过滤。"""
    with master_connection() as conn:
        conn.row_factory = sqlite3.Row
        sql = (
            "SELECT c.*, ci.canonical_sku, ci.name AS canonical_name "
            "FROM canonical_conflicts c "
            "LEFT JOIN canonical_items ci ON ci.id = c.canonical_id "
            "WHERE c.status = 'open'"
        )
        params: tuple = ()
        if canonical_id is not None:
            sql += " AND c.canonical_id = ?"
            params = (int(canonical_id),)
        sql += " ORDER BY c.created_at DESC LIMIT 200"
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def get_fanout_event(event_id: int) -> dict | None:
    """单次扇出事件详情（含 per-仓 / per-item 应用结果）。"""
    with master_connection() as conn:
        conn.row_factory = sqlite3.Row
        ev = conn.execute(
            "SELECT * FROM canonical_publish_events WHERE id=?",
            (int(event_id),),
        ).fetchone()
        if ev is None:
            return None
        warehouses = [
            dict(r) for r in conn.execute(
                """SELECT * FROM canonical_publish_event_warehouses
                   WHERE publish_event_id=?""",
                (int(event_id),),
            ).fetchall()
        ]
        items = [
            dict(r) for r in conn.execute(
                """SELECT * FROM canonical_publish_event_items
                   WHERE publish_event_id=?
                   ORDER BY id""",
                (int(event_id),),
            ).fetchall()
        ]
    return {
        "event": dict(ev),
        "warehouses": warehouses,
        "items": items,
    }


# ─────────────────────────────────────────────────────────────────────
# Write — canonical_items
# ─────────────────────────────────────────────────────────────────────

def create_canonical(
    *,
    name: str,
    unit: str,
    category_code: str | None = None,
    gram_per_unit: float = 0,
    aux_unit: str | None = None,
    aux_rate: float = 0,
    barcode: str | None = None,
    status: str = "active",
    selling_price: float | None = None,
    unit_cost: float | None = None,
    actor_id: int | None = None,
) -> dict:
    """新建一条主数据。返回新行（含 canonical_sku）。"""
    if not name or not name.strip():
        raise ValidationError("name required")
    if not unit or not unit.strip():
        raise ValidationError("unit required")
    if gram_per_unit < 0 or aux_rate < 0:
        raise ValidationError("numeric fields must be non-negative")
    if selling_price is not None and selling_price < 0:
        raise ValidationError("selling_price must be non-negative")
    if unit_cost is not None and unit_cost < 0:
        raise ValidationError("unit_cost must be non-negative")
    with master_connection() as conn:
        try:
            return cp.create_canonical_item(
                conn,
                name=name.strip(),
                unit=unit.strip(),
                category_code=category_code,
                gram_per_unit=float(gram_per_unit),
                aux_unit=aux_unit,
                aux_rate=float(aux_rate),
                barcode=barcode,
                status=status,
                selling_price=selling_price,
                unit_cost=unit_cost,
                created_from="mcp",
                created_by=actor_id,
            )
        except ValueError as _wrap_value_error_target:
            raise ValidationError(str(_wrap_value_error_target)) from None


def update_canonical(
    canonical_id: int,
    *,
    name: str | None = None,
    unit: str | None = None,
    category_code: str | None = None,
    gram_per_unit: float | None = None,
    aux_unit: str | None = None,
    aux_rate: float | None = None,
    barcode: str | None = None,
    status: str | None = None,
    selling_price: float | None = None,
    unit_cost: float | None = None,
    actor_id: int | None = None,
) -> dict:
    """更新主数据（仅白名单字段；Q3=deactivate_only 在 pure 层 enforce）。"""
    fields: dict[str, object] = {}
    if name is not None:
        fields["name"] = name
    if unit is not None:
        fields["unit"] = unit
    if category_code is not None:
        fields["category_code"] = category_code if category_code else None
    if gram_per_unit is not None:
        fields["gram_per_unit"] = float(gram_per_unit)
    if aux_unit is not None:
        fields["aux_unit"] = aux_unit if aux_unit else None
    if aux_rate is not None:
        fields["aux_rate"] = float(aux_rate)
    if barcode is not None:
        fields["barcode"] = barcode if barcode else None
    if status is not None:
        fields["status"] = status
    if selling_price is not None:
        fields["selling_price"] = float(selling_price)
    if unit_cost is not None:
        fields["unit_cost"] = float(unit_cost)
    if not fields:
        raise ValidationError("no fields to update")
    with master_connection() as conn:
        try:
            return cp.update_canonical_item(conn, int(canonical_id), fields)
        except ValueError as e:
            msg = str(e)
            if "不存在" in msg:
                raise NotFoundError(msg) from None
            raise ValidationError(msg) from None


# ─────────────────────────────────────────────────────────────────────
# Write — claim / unclaim / fanout
# ─────────────────────────────────────────────────────────────────────

def claim_item(
    *,
    warehouse_code: str,
    local_item_id: int,
    canonical_id: int,
    ctx: AuthContext,
    is_alias: bool = False,
    local_keep_name: str | None = None,
    reason: str | None = None,
    actor_id: int | None = None,
) -> dict:
    """认领仓内行到主数据（T7）。

    严格只写 ALLOWED_CLAIM_UPDATES 4 列；不动 inventory / 主数据字段。
    写仓路径走 Q7：备份 + 三快照断言在 cp.claim_item 内部执行。
    """
    _validate_warehouse(warehouse_code, ctx)
    wh = resolve_warehouse(warehouse_code)
    if wh is None:
        raise NotFoundError("warehouse_not_found")
    conn = _open_warehouse(wh["db_path"])
    try:
        with master_connection() as m_conn:
            try:
                return cp.claim_item(
                    m_conn, conn,
                    warehouse_code=warehouse_code,
                    local_item_id=int(local_item_id),
                    canonical_id=int(canonical_id),
                    is_alias=bool(is_alias),
                    is_store_exclusive=False,
                    submitted_by=actor_id,
                    local_keep_name=local_keep_name,
                    reason=reason,
                )
            except ValueError as e:
                raise ValidationError(str(e)) from None
    finally:
        conn.close()


def unclaim_item(
    *,
    warehouse_code: str,
    local_item_id: int,
    ctx: AuthContext,
    reason: str | None = None,
    actor_id: int | None = None,
) -> dict:
    """解绑（对称于 claim_item，§7.8.3）。"""
    _validate_warehouse(warehouse_code, ctx)
    wh = resolve_warehouse(warehouse_code)
    if wh is None:
        raise NotFoundError("warehouse_not_found")
    conn = _open_warehouse(wh["db_path"])
    try:
        with master_connection() as m_conn:
            try:
                return cp.unclaim_item(
                    m_conn, conn,
                    warehouse_code=warehouse_code,
                    local_item_id=int(local_item_id),
                    reviewed_by=actor_id,
                    reason=reason,
                )
            except ValueError as e:
                raise ValidationError(str(e)) from None
    finally:
        conn.close()


def fanout_canonical(
    *,
    canonical_ids: list[int],
    warehouse_codes: list[str],
    action: str = "overwrite",
    force: bool = False,
    summary: str | None = None,
    actor_id: int | None = None,
) -> dict:
    """M2 主路径：把选中主数据项的字段下发到指定目标仓。

    - Q4=freeze：local != last_synced → frozen（除非 force）
    - Q6=canonical_managed：selling_price / unit_cost 进 written 集
    - 失败整体回滚；DC 必须含在 warehouse_codes 中（否则新品订不到）
    - 0-canonical_id / 0-warehouse 入参必拒
    """
    if not canonical_ids:
        raise ValidationError("canonical_ids required")
    if not warehouse_codes:
        raise ValidationError("warehouse_codes required")
    if action not in ("overwrite", "skip"):
        raise ValidationError("action must be 'overwrite' or 'skip'")
    if "wh_000" not in warehouse_codes and "WH000" not in warehouse_codes:
        # 兼容 dev 仓码：列表里必须含 distribution_center
        with master_connection() as conn:
            conn.row_factory = sqlite3.Row
            placeholders = ", ".join("?" * len(warehouse_codes))
            dc_count = conn.execute(
                f"""SELECT COUNT(*) AS c FROM warehouses
                    WHERE warehouse_type='distribution_center'
                      AND code IN ({placeholders})""",
                list(warehouse_codes),
            ).fetchone()["c"]
        if dc_count == 0:
            raise ValidationError(
                "warehouse_codes must include at least one "
                "distribution_center (P0-10 否则新品订不到)"
            )
    with master_connection() as m_conn:
        wh_db_map: dict[str, sqlite3.Connection] = {}
        try:
            m_conn.row_factory = sqlite3.Row
            placeholders = ", ".join("?" * len(warehouse_codes))
            wh_rows = m_conn.execute(
                f"""SELECT code, db_path, warehouse_type FROM warehouses
                    WHERE code IN ({placeholders})""",
                list(warehouse_codes),
            ).fetchall()
            for w in wh_rows:
                wh_db_map[w["code"]] = _open_warehouse(w["db_path"])
            try:
                return cp.fanout_canonical_items(
                    m_conn, wh_db_map,
                    canonical_ids=[int(c) for c in canonical_ids],
                    warehouse_codes=list(warehouse_codes),
                    action=action,
                    force=bool(force),
                    started_by=actor_id,
                    summary=summary,
                )
            except ValueError as e:
                raise ValidationError(str(e)) from None
        finally:
            for c in wh_db_map.values():
                try:
                    c.close()
                except Exception:  # noqa: BLE001
                    pass


def resolve_conflict(
    *,
    conflict_id: int,
    decision: str,  # 'keep_local' | 'accept_canonical' | 'waive'
    note: str | None = None,
    actor_id: int | None = None,
) -> dict:
    """裁决冲突（status: open → resolved_keep_local/.../waived）。"""
    if decision not in ("keep_local", "accept_canonical", "waive"):
        raise ValidationError(
            "decision must be keep_local / accept_canonical / waive"
        )
    with master_connection() as conn:
        try:
            return cp.resolve_conflict(
                conn,
                conflict_id=int(conflict_id),
                decision=decision,
                reviewed_by=int(actor_id) if actor_id is not None else 0,
                note=note,
            )
        except ValueError as e:
            raise ValidationError(str(e)) from None


def set_binding_active(
    *,
    warehouse_code: str,
    canonical_id: int,
    active: bool,
    ctx: AuthContext,
) -> dict:
    """P0-10 —— 按仓启停某个主数据项的绑定行（items.is_active）。"""
    _validate_warehouse(warehouse_code, ctx)
    wh = resolve_warehouse(warehouse_code)
    if wh is None:
        raise NotFoundError("warehouse_not_found")
    conn = _open_warehouse(wh["db_path"])
    try:
        try:
            return cp.set_binding_active(
                conn, warehouse_code, int(canonical_id), bool(active),
            )
        except ValueError as e:
            raise ValidationError(str(e)) from None
    finally:
        conn.close()
