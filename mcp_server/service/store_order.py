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
  - 仓库级：**订单可见性规则** —— ``store_warehouse_code`` 或
    ``dc_warehouse_code`` 任一命中 token 的 ``allowed_warehouses`` 即可见；
    ``allowed_warehouses is None`` 表示不限仓，全可见。

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


def _scope_pairs(
    codes: list[str] | None,
    warehouse_code: str | None,
) -> list[tuple[str | None, str | None]]:
    """把可见性 OR 规则翻译成 ``sop.list_orders`` 能表达的过滤器组合。

    ``sop.list_orders`` 的 ``store_warehouse_code`` / ``dc_warehouse_code``
    是 **AND** 关系，而 ACL 可见性是「store 命中 **OR** dc 命中」，
    因此要把 OR 展开成若干次 AND 查询的并集：

      * 无 ``warehouse_code`` 过滤：``store ∈ codes`` ∪ ``dc ∈ codes``
      * 有 ``warehouse_code`` 过滤（记作 W，调用方已保证 W ∈ codes）：
        ``(store ∈ codes ∪ dc ∈ codes)`` ∧ ``(store = W ∪ dc = W)``

    展开后每个组合都能用一次 ``list_orders`` 调用表达：

      ==========================  ==========================================
      组合                        含义
      ==========================  ==========================================
      ``(store=W, None)``         store = W
      ``(None, W)``               dc = W
      ``(X, W)`` / ``(W, X)``     store = X ∧ dc = W / dc = X ∧ store = W
      ==========================  ==========================================

    ``codes is None``（不限仓）时退化成单次无过滤调用。
    """
    if codes is None:
        if warehouse_code is None:
            return [(None, None)]
        return [(warehouse_code, None), (None, warehouse_code)]

    pairs: list[tuple[str | None, str | None]] = []
    if warehouse_code is not None:
        pairs.append((warehouse_code, None))
        pairs.append((None, warehouse_code))
    for code in sorted(codes):
        if warehouse_code is None:
            pairs.append((code, None))
            pairs.append((None, code))
        elif code != warehouse_code:
            pairs.append((code, warehouse_code))
            pairs.append((warehouse_code, code))
    return pairs


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
    for store_code, dc_code in _scope_pairs(codes, warehouse_code):
        rows = sop.list_orders(
            conn,
            store_warehouse_code=store_code,
            dc_warehouse_code=dc_code,
            status=status or None,
            limit=window,
            offset=0,
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
    """统计可见订单总数（与列表同一套过滤条件）。

    ``sop.count_orders_by_status`` 的 store/dc 过滤也是 AND 关系，
    表达不了本层的 OR 可见性，所以这里单独写一条 COUNT。
    """
    where: list[str] = []
    params: list[object] = []
    if codes is not None:
        placeholders = ",".join("?" for _ in codes)
        where.append(
            f"(store_warehouse_code IN ({placeholders}) "
            f"OR dc_warehouse_code IN ({placeholders}))"
        )
        params.extend(codes)
        params.extend(codes)
    if warehouse_code is not None:
        where.append("(store_warehouse_code = ? OR dc_warehouse_code = ?)")
        params.extend([warehouse_code, warehouse_code])
    if status:
        where.append("status = ?")
        params.append(status)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM store_orders {clause}", params
    ).fetchone()
    return int(row["n"])


def _is_visible(order: dict, codes: list[str] | None) -> bool:
    """订单可见性：store 或 dc 任一命中 allowed_warehouses 即可见。

    ``codes is None``（不限仓）→ 全可见。
    """
    if codes is None:
        return True
    return (
        order.get("store_warehouse_code") in codes
        or order.get("dc_warehouse_code") in codes
    )


def _shape_item(row: dict) -> dict:
    """明细行形状：补齐 dc_item_id 是否为 null 的显式标记。

    ``dc_item_bound`` = True 表示该行已回填配送中心 items.id（出库时匹配上）；
    False 表示还没匹配上（订货行仍处于未发货态）。
    """
    item = dict(row)
    dc_item_id = item.get("dc_item_id")
    item["dc_item_id"] = dc_item_id
    item["dc_item_bound"] = dc_item_id is not None
    item["quantity"] = float(item.get("quantity") or 0)
    item["fulfilled_quantity"] = float(item.get("fulfilled_quantity") or 0)
    item["shipped_quantity"] = float(item.get("shipped_quantity") or 0)
    return item


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

    ``warehouse_code`` 是可选的**单仓**过滤（该仓作为门店方或总仓方都算命中）；
    ``status`` 是订单状态过滤。两者叠加在 token 的仓库范围之上。
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

    with master_connection(read_only=ctx.read_only) as conn:
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
    ``receipts`` + ``status_history``。
    """
    try:
        order_id = int(order_id)
    except (TypeError, ValueError):
        raise ValidationError("order_id required") from None

    with master_connection(read_only=ctx.read_only) as conn:
        detail = sop.get_order_detail(conn, order_id)
    if detail is None:
        raise NotFoundError("not_found")
    if not _is_visible(detail, ctx.allowed_warehouses):
        # 范围外的订单「不可见」—— 与不存在同义，不泄露其存在性。
        raise NotFoundError("not_found")

    header = {k: v for k, v in detail.items() if k not in _COLLECTION_KEYS}
    return {
        "order": header,
        "items": [_shape_item(r) for r in detail.get("order_items", [])],
        "deliveries": detail.get("deliveries", []),
        "receipts": detail.get("receipts", []),
        "status_history": detail.get("status_history", []),
    }
