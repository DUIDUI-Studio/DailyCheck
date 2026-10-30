"""门店订单只读服务层。

策略来源复用 ``blueprints.store_ordering_pure`` 的 ``list_orders`` /
``get_order_detail``（与 ``service/canonical.py`` 复用 ``canonical_pure``
的先例一致）；本层只负责三件事：

  - 打开 master.db 连接（``data/unit_of_work.master_connection``）
  - 把 token 的仓库范围（ACL）翻译成查询过滤
  - 把纯函数返回的行整理成 MCP 契约的返回形状

权限模型：
  - 路径级：``GET /api/v1/store-orders`` 与 ``GET /api/v1/store-orders/<id>``，
    由 ``protocol/tools/store_order.py`` 用 ``check_path`` 校验。
  - 仓库级：**「订单参与仓」可见性**（spec §10）——订单的门店仓，或任一
    明细行来源仓（历史 NULL 行回落头仓；无明细行的存量单回落头仓）命中
    token 的 ``allowed_warehouses`` 即可见；``allowed_warehouses is None``
    表示不限仓，全可见。
  - ACL 扩权语义（显式）：token 命中**任一参与仓** → 可读**全单**，包括由
    其他参与仓履约的明细行、金额与收货历史。这是有意的扩权，不是漏洞。

列表、计数与详情三处可见性共用 ``_visibility_sql`` 同一段 SQL 谓词，
避免「列表能看、详情 404」或计数与分页不一致。

只读：本模块只调用纯函数的查询入口，不写任何表、不删任何行、不改 schema。
"""
from __future__ import annotations

import sqlite3

import blueprints.store_ordering_pure as sop
from mcp_server.data.master import resolve_warehouse
from mcp_server.data.unit_of_work import master_connection
from mcp_server.infra.errors import (
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from mcp_server.service.auth import AuthContext, check_warehouse

#: 单页默认行数 / 上限（与 canonical 服务层同口径）。
DEFAULT_LIMIT = 200
MAX_LIMIT = 500

#: 订单头之外的四个子段（纯函数返回 dict 里的键）。
_COLLECTION_KEYS = ("order_items", "deliveries", "receipts", "status_history")


# ─────────────────────────────────────────────────────────────────────
# 统一可见性谓词
# ─────────────────────────────────────────────────────────────────────

def _visibility_sql(alias: str, code_count: int) -> str:
    """「订单参与仓」SQL 谓词（spec §10），列表/计数/详情共用。

    ``alias`` 是调用方传入的可信表别名（不来自用户输入）。谓词表达：

      * 门店仓命中；或
      * 任一明细行来源仓命中（``COALESCE(source_warehouse_code, 头仓)``
        兼容迁移前的历史 NULL 行）；或
      * 没有任何明细行的存量单，头仓命中。

    ``EXISTS`` 保证多行同仓不会把订单计数翻倍。
    """
    marks = ",".join("?" for _ in range(code_count))
    return (
        f"({alias}.store_warehouse_code IN ({marks})"
        f" OR EXISTS (SELECT 1 FROM store_order_items soi"
        f" WHERE soi.order_id = {alias}.id"
        f" AND COALESCE(soi.source_warehouse_code, {alias}.dc_warehouse_code)"
        f" IN ({marks}))"
        f" OR ({alias}.dc_warehouse_code IN ({marks}) AND NOT EXISTS ("
        f"SELECT 1 FROM store_order_items legacy"
        f" WHERE legacy.order_id = {alias}.id)))"
    )


def _visibility_params(codes: list[str]) -> list[str]:
    """``_visibility_sql`` 的三组占位符按顺序各吃一遍仓库 code。"""
    return [*codes, *codes, *codes]


# ─────────────────────────────────────────────────────────────────────
# 内部 helpers
# ─────────────────────────────────────────────────────────────────────

def _normalize_limit(limit: object) -> int:
    """把 limit 夹到 [1, MAX_LIMIT]。"""
    try:
        value = int(limit)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        value = DEFAULT_LIMIT
    return max(1, min(value, MAX_LIMIT))


def _normalize_offset(offset: object) -> int:
    """offset 至少为 0。"""
    try:
        value = int(offset)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        value = 0
    return max(0, value)


def _scope_filters(
    codes: list[str] | None,
    warehouse_code: str | None,
) -> list[dict[str, object]]:
    """把可见性翻译成 ``sop.list_orders`` 的调用参数组合。

    ``sop.list_orders`` 的 ``store_warehouse_code`` / ``dc_warehouse_code``
    是 AND 关系，一次调用表达不了 OR 可见性，所以按「门店侧」与「参与仓」
    两个入口各查一次，再按订单 id 合并去重：

      * 显式 ``warehouse_code``（调用方已保证它在 token 范围内，因此该仓
        命中的订单必然可见）：只需 ``store=W`` 与 ``participant=W`` 两次；
      * 否则按 token 的每个仓展开 ``store=code`` 与 ``participant=code``；
      * ``codes is None``（不限仓）：单次无过滤调用。
    """
    if warehouse_code is not None:
        return [
            {"store_warehouse_code": warehouse_code},
            {"participant_warehouse_code": warehouse_code},
        ]
    if codes is None:
        return [{}]
    scopes: list[dict[str, object]] = []
    for code in sorted(codes):
        scopes.append({"store_warehouse_code": code})
        scopes.append({"participant_warehouse_code": code})
    return scopes


def _order_key(row: dict) -> tuple[str, int]:
    """与 ``sop.list_orders`` 的 ORDER BY 一致：created_at DESC, id DESC。"""
    return (str(row.get("created_at") or ""), int(row.get("id") or 0))


def _fetch_orders(
    conn: sqlite3.Connection,
    codes: list[str] | None,
    warehouse_code: str | None,
    status: str | None,
    limit: int,
    offset: int,
) -> list[dict]:
    """取一页可见订单（已按可见性过滤、按创建时间倒序）。

    每个子集各取前 ``limit + offset`` 行：并集的前 K 行必然落在各子集的
    前 K 行之内（若某行在并集里排前 K，它在自己子集里也排前 K），
    因此合并后再切片，分页结果依然正确。
    """
    window = limit + offset
    merged: dict[int, dict] = {}
    for scope in _scope_filters(codes, warehouse_code):
        rows = sop.list_orders(
            conn,
            status=status or None,
            limit=window,
            offset=0,
            **scope,
        )
        for row in rows:
            merged[int(row["id"])] = row
    ordered = sorted(merged.values(), key=_order_key, reverse=True)
    return ordered[offset:offset + limit]


def _count_orders(
    conn: sqlite3.Connection,
    codes: list[str] | None,
    warehouse_code: str | None,
    status: str | None,
) -> int:
    """统计可见订单总数（同一段 ``_visibility_sql`` 谓词）。"""
    where: list[str] = []
    params: list[object] = []
    if codes is not None:
        if not codes:
            return 0
        where.append(_visibility_sql("store_orders", len(codes)))
        params.extend(_visibility_params(codes))
    if warehouse_code is not None:
        where.append(_visibility_sql("store_orders", 1))
        params.extend(_visibility_params([warehouse_code]))
    if status:
        where.append("status = ?")
        params.append(status)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM store_orders {clause}", params
    ).fetchone()
    return int(row["n"])


def _is_visible(
    conn: sqlite3.Connection,
    order_id: int,
    codes: list[str] | None,
) -> bool:
    """详情可见性：与列表/计数同一段 SQL 谓词。"""
    if codes is None:
        return True
    if not codes:
        return False
    row = conn.execute(
        f"SELECT 1 FROM store_orders so WHERE so.id = ? "
        f"AND {_visibility_sql('so', len(codes))}",
        [order_id, *_visibility_params(codes)],
    ).fetchone()
    return row is not None


def _shape_item(row: dict) -> dict:
    """明细行形状：补齐 dc_item_id 是否为 null 的显式标记。

    ``dc_item_bound`` = True 表示该行已回填配送中心 items.id（出库时匹配上）；
    False 表示还没匹配上（订货行仍处于未发货态）。
    ``source_warehouse_code`` 是行级来源仓（迁移前历史行可能为 NULL，
    由头仓兜底），契约上始终存在该键。
    """
    item = dict(row)
    item.setdefault("source_warehouse_code", None)
    dc_item_id = item.get("dc_item_id")
    item["dc_item_id"] = dc_item_id
    item["dc_item_bound"] = dc_item_id is not None
    item["quantity"] = float(item.get("quantity") or 0)
    item["fulfilled_quantity"] = float(item.get("fulfilled_quantity") or 0)
    item["shipped_quantity"] = float(item.get("shipped_quantity") or 0)
    return item


def _shape_delivery(row: dict) -> dict:
    """批次形状：``warehouse_code`` 是本批实际发货仓（物流事实）。"""
    delivery = dict(row)
    delivery.setdefault("warehouse_code", None)
    return delivery


# ─────────────────────────────────────────────────────────────────────
# Read
# ─────────────────────────────────────────────────────────────────────

def list_store_orders(
    ctx: AuthContext,
    warehouse_code: str | None = None,
    status: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> dict:
    """列门店订单（只读）。

    ``warehouse_code`` 是可选的**单仓**过滤（该仓作为门店方或任一参与仓
    都算命中）；``status`` 是订单状态过滤。两者叠加在 token 的仓库范围之上。

    ACL 扩权语义：命中所返回订单的**任一参与仓**即可读，返回的是**全单**。
    """
    limit = _normalize_limit(limit)
    offset = _normalize_offset(offset)
    codes = ctx.allowed_warehouses

    if warehouse_code is not None:
        warehouse_code = str(warehouse_code).strip()
        if not warehouse_code:
            warehouse_code = None

    if warehouse_code is not None:
        if not check_warehouse(ctx, warehouse_code):
            raise ForbiddenError("forbidden_warehouse")
        if resolve_warehouse(warehouse_code) is None:
            raise NotFoundError("warehouse_not_found")

    with master_connection() as conn:
        orders = _fetch_orders(
            conn, codes, warehouse_code, status or None, limit, offset
        )
        total = _count_orders(conn, codes, warehouse_code, status or None)

    return {
        "orders": orders,
        "total": total,
        "limit": limit,
        "offset": offset,
        "truncated": total > offset + len(orders),
    }


def get_store_order_detail(ctx: AuthContext, order_id: int) -> dict:
    """单个门店订单详情（只读）。

    返回五段结构：``order``（头）+ ``items`` + ``deliveries`` +
    ``receipts`` + ``status_history``。命中任一参与仓即返回**全单**
    （含其他参与仓履约的行、金额与收货历史）。
    """
    try:
        order_id = int(order_id)
    except (TypeError, ValueError):
        raise ValidationError("order_id required") from None

    with master_connection() as conn:
        detail = sop.get_order_detail(conn, order_id)
        visible = (
            detail is not None
            and _is_visible(conn, order_id, ctx.allowed_warehouses)
        )
    if not visible or detail is None:
        # 范围外的订单「不可见」—— 与不存在同义，不泄露其存在性。
        raise NotFoundError("not_found")

    header = {k: v for k, v in detail.items() if k not in _COLLECTION_KEYS}
    return {
        "order": header,
        "items": [_shape_item(r) for r in detail.get("order_items", [])],
        "deliveries": [_shape_delivery(r) for r in detail.get("deliveries", [])],
        "receipts": detail.get("receipts", []),
        "status_history": detail.get("status_history", []),
    }
