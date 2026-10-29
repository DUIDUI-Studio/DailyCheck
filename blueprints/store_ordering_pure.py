"""Pure logic for store ordering (门店订货跨仓订货).

Conventions:
- All public functions take a sqlite3.Connection for master.db as the first
  argument; warehouse db connections are opened by helpers and passed around.
- No Flask imports (tests can run without an app).
- Quantities are parsed with parse_qty() and stored as float with 2 dp.
- Timestamps use now() in 'YYYY-MM-DD HH:MM:SS' format.

Spec: docs/2026-10-04-store-ordering-design.md §3.3 / §6.
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

import config
from blueprints._helpers import now, parse_qty
from blueprints.fulfillment_pure import parse_price_and_multiple, resolve_fulfillment_item
from blueprints.item_routing_pure import get_routing
from blueprints.notifications_pure import emit_event
from blueprints.procurement_pure import (
    compute_safety_stock,
    compute_suggested_qty,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WAREHOUSE_TYPE_DC: str = "distribution_center"


def multi_warehouse_enabled() -> bool:
    """Rollout switch (design D14). Read per call so tests can monkeypatch it."""
    return bool(getattr(config, "MULTI_WAREHOUSE_FULFILLMENT", False))


# 单据流向（store_orders.flow_type）：
#   store_order —— 门店发起订货（默认，走 pending 审批）
#   dc_transfer —— 总仓主动发起的调货（无审批，创建即 approved）
FLOW_STORE_ORDER: str = "store_order"
FLOW_DC_TRANSFER: str = "dc_transfer"

ORDER_STATUS_PENDING: str = "pending"
ORDER_STATUS_APPROVED: str = "approved"
ORDER_STATUS_REJECTED: str = "rejected"
ORDER_STATUS_SHIPPED: str = "shipped"
ORDER_STATUS_DELIVERED: str = "delivered"
ORDER_STATUS_CANCELLED: str = "cancelled"

ORDER_ITEM_STATUS_PENDING: str = "pending"
ORDER_ITEM_STATUS_FULFILLED: str = "fulfilled"
ORDER_ITEM_STATUS_PARTIAL: str = "partial"
ORDER_ITEM_STATUS_CANCELLED: str = "cancelled"

EVENT_ORDER_SUBMITTED: str = "store_order_submitted"
EVENT_ORDER_APPROVED: str = "store_order_approved"
EVENT_ORDER_REJECTED: str = "store_order_rejected"
EVENT_ORDER_SHIPPED: str = "store_order_shipped"
EVENT_ORDER_DELIVERED: str = "store_order_delivered"
EVENT_ORDER_RECEIVED: str = "store_order_received"
EVENT_ORDER_CANCELLED: str = "store_order_cancelled"
EVENT_TRANSFER_CREATED: str = "dc_transfer_created"

ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    ORDER_STATUS_PENDING: (ORDER_STATUS_APPROVED, ORDER_STATUS_REJECTED, ORDER_STATUS_CANCELLED),
    ORDER_STATUS_APPROVED: (ORDER_STATUS_SHIPPED, ORDER_STATUS_CANCELLED),
    ORDER_STATUS_SHIPPED: (ORDER_STATUS_DELIVERED,),
}

SHIPMENT_ACTION: str = "门店订货出库"
RECEIPT_ACTION: str = "门店订货入库"

# 调货单专用流水动作。必须与 reports_health_pure 的 INBOUND_ACTIONS /
# CONSUME_ACTIONS 白名单以及 restock 入库历史保持同步，否则调拨造成的
# 库存增减会被健康报表静默漏算。
TRANSFER_SHIPMENT_ACTION: str = "总仓调货出库"
TRANSFER_RECEIPT_ACTION: str = "总仓调货入库"


def shipment_action_for(order: dict[str, Any]) -> str:
    """库存流水动作：调货单与门店订货单分开记账，便于报表区分。"""
    if order.get("flow_type") == FLOW_DC_TRANSFER:
        return TRANSFER_SHIPMENT_ACTION
    return SHIPMENT_ACTION


def receipt_action_for(order: dict[str, Any]) -> str:
    """库存流水动作：调货单入库走 ``总仓调货入库``。"""
    if order.get("flow_type") == FLOW_DC_TRANSFER:
        return TRANSFER_RECEIPT_ACTION
    return RECEIPT_ACTION

#: 起订倍数「不限」的取值。items.order_multiple 默认即此值。
ORDER_MULTIPLE_UNLIMITED: float = 1.0


class OrderMultipleError(ValueError):
    """下单数量不满足品项起订倍数。

    继承 ValueError 是为了让既有 ``except ValueError`` 的调用方（路由层）
    无需改动即可捕获并 flash 出可读文案。
    """

    def __init__(self, name: str, quantity: float, multiple: float) -> None:
        self.name = name
        self.quantity = parse_qty(quantity)
        self.multiple = normalize_order_multiple(multiple)
        super().__init__(
            f"{name} 的订购数量必须是 {fmt_multiple(self.multiple)} 的整数倍"
            f"（当前填 {parse_qty(self.quantity)}）"
        )


def normalize_order_multiple(raw: Any) -> float:
    """把配置值归一化成合法的订货倍数。

    非法值（None / 非数字 / <= 0）一律回落成 1（不限），保证任何脏数据都
    不会把门店卡死在「这个品项永远订不了」的状态。
    """
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return ORDER_MULTIPLE_UNLIMITED
    if value <= 0:
        return ORDER_MULTIPLE_UNLIMITED
    return parse_qty(value)


def fmt_multiple(multiple: Any) -> str:
    """倍数的人类可读写法：5.0 → '5'，2.50 → '2.5'。"""
    value = normalize_order_multiple(multiple)
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text or "1"


def is_multiple_of(quantity: Any, multiple: Any) -> bool:
    """quantity 是否为 multiple 的整数倍（multiple=1 时恒为 True）。"""
    step = normalize_order_multiple(multiple)
    if step == ORDER_MULTIPLE_UNLIMITED:
        return True
    qty = parse_qty(quantity)
    if qty <= 0:
        return False
    # 浮点容差：按「离最近整数倍的距离」判定，阈值随步长缩放，
    # 避免 0.3 / 0.1 这类二进制表示误差被误判。
    steps = qty / step
    remainder = abs(qty - round(steps) * step)
    return remainder <= max(1e-9, abs(step) * 1e-9)


def next_valid_quantity(quantity: Any, multiple: Any) -> float:
    """向上取到最近一个合法数量（≥ multiple）。前端/提示文案用。"""
    step = normalize_order_multiple(multiple)
    qty = parse_qty(quantity)
    if step == ORDER_MULTIPLE_UNLIMITED:
        return qty if qty > 0 else step
    if qty <= 0:
        return parse_qty(step)
    steps = Decimal(str(qty)) / Decimal(str(step))
    return parse_qty(float(Decimal(str(step)) * Decimal(int(steps.to_integral_value(rounding="ROUND_CEILING")))))


def get_dc_item_order_multiple(
    master_conn: sqlite3.Connection,
    dc_warehouse_code: str,
    canonical_id: int,
) -> float:
    """取某 DC 某品项的起订倍数；品项不存在时按「不限」处理。"""
    dc_conn = open_warehouse_db(dc_warehouse_code)
    try:
        row = dc_conn.execute(
            "SELECT order_multiple FROM items WHERE canonical_id=? ",
            (canonical_id,),
        ).fetchone()
    finally:
        dc_conn.close()
    if row is None:
        return ORDER_MULTIPLE_UNLIMITED
    return normalize_order_multiple(row[0])



# ---------------------------------------------------------------------------
# Order number generation
# ---------------------------------------------------------------------------

def generate_order_no(master_conn: sqlite3.Connection, prefix: str = "SO") -> str:
    """Generate ``{prefix}-YYYYMMDD-XXXX`` order number, daily auto-increment.

    调货单传 ``prefix="DT"``，与订货单（SO）分号段但共用同一张表的
    ``order_no`` 唯一约束，因此序号必须按完整前缀查重，不能只按日期。
    """
    master_conn.row_factory = sqlite3.Row
    today = datetime.now().strftime("%Y%m%d")
    no_prefix = f"{prefix}-{today}-"
    row = master_conn.execute(
        "SELECT order_no FROM store_orders WHERE order_no LIKE ? ORDER BY order_no DESC LIMIT 1",
        (f"{no_prefix}%",),
    ).fetchone()
    if row is None:
        seq = 1
    else:
        seq = int(row["order_no"].rsplit("-", 1)[1]) + 1
    return f"{no_prefix}{seq:04d}"


# ---------------------------------------------------------------------------
# Warehouse db helpers
# ---------------------------------------------------------------------------

def open_warehouse_db(warehouse_code: str) -> sqlite3.Connection:
    """Open db/warehouses/{code}.db with Row factory and FK enabled.

    Triggers ``migrate_warehouse_db_columns`` so the connection always sees
    the latest schema (canonical_id / is_orderable / …) — matches
    ``db.get_warehouse_db``'s lazy-migration contract and prevents 500s
    when a wh_XXX.db hasn't yet been touched by the request path that
    would normally pull in the migration. See issue #12.
    """
    path = config.WAREHOUSE_DB_DIR / f"{warehouse_code}.db"
    # Local import: avoid an import cycle with ``db`` (which itself imports
    # ``config`` and a few blueprints at module load).
    from db import migrate_warehouse_db_columns
    migrate_warehouse_db_columns(path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ---------------------------------------------------------------------------
# Cart helpers
# ---------------------------------------------------------------------------

def _touch_cart(master_conn: sqlite3.Connection, cart_id: int) -> None:
    master_conn.execute(
        "UPDATE store_order_carts SET updated_at=? WHERE id=?",
        (now(), cart_id),
    )


def get_or_create_cart(
    master_conn: sqlite3.Connection,
    user_id: int,
    store_warehouse_code: str,
    dc_warehouse_code: str | None,
) -> dict[str, Any]:
    """Get or create cart. If dc changed, clear existing cart items first.

    Multi-warehouse mode keeps one cart per (user, store) and stores a NULL
    ``dc_warehouse_code``; routing is resolved per line at display/submit time,
    so switching the header DC can no longer invalidate the cart.
    """
    master_conn.row_factory = sqlite3.Row
    multi = multi_warehouse_enabled()
    if multi:
        dc_warehouse_code = None
    cart = master_conn.execute(
        "SELECT * FROM store_order_carts WHERE user_id=? AND store_warehouse_code=?",
        (user_id, store_warehouse_code),
    ).fetchone()
    if cart is None:
        ts = now()
        cur = master_conn.execute(
            """INSERT INTO store_order_carts
               (user_id, store_warehouse_code, dc_warehouse_code, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, store_warehouse_code, dc_warehouse_code, ts, ts),
        )
        master_conn.commit()
        return {
            "id": int(cur.lastrowid),
            "user_id": user_id,
            "store_warehouse_code": store_warehouse_code,
            "dc_warehouse_code": dc_warehouse_code,
            "created_at": ts,
            "updated_at": ts,
        }

    cart_dict = dict(cart)
    if multi:
        if cart_dict["dc_warehouse_code"] is not None:
            master_conn.execute(
                "UPDATE store_order_carts SET dc_warehouse_code=NULL, updated_at=? WHERE id=?",
                (now(), cart_dict["id"]),
            )
            master_conn.commit()
            cart_dict["dc_warehouse_code"] = None
        return cart_dict

    if cart_dict["dc_warehouse_code"] != dc_warehouse_code:
        # DC changed: clear items and update target dc.
        clear_cart(master_conn, cart_dict["id"])
        master_conn.execute(
            "UPDATE store_order_carts SET dc_warehouse_code=?, updated_at=? WHERE id=?",
            (dc_warehouse_code, now(), cart_dict["id"]),
        )
        master_conn.commit()
        cart_dict["dc_warehouse_code"] = dc_warehouse_code
    return cart_dict


def list_cart_items(
    master_conn: sqlite3.Connection,
    cart_id: int,
) -> list[dict[str, Any]]:
    """Return cart items enriched with canonical info and dc/store stock/binding.

    Each item dict now includes `category_name` (Chinese category name) and
    `unit_price` (selling_price → unit_cost → 0) for cart total / line subtotal
    display. `line_subtotal = unit_price * quantity` (rounded to 2 dp) is also
    included for direct template rendering.
    """
    master_conn.row_factory = sqlite3.Row
    cart = master_conn.execute(
        "SELECT * FROM store_order_carts WHERE id=?", (cart_id,)
    ).fetchone()
    if cart is None:
        return []
    dc_code = cart["dc_warehouse_code"]
    store_code = cart["store_warehouse_code"]
    # Multi-warehouse carts have no header DC: each line's display price and
    # multiple come from its routed source warehouse (submit re-resolves).
    routed_display = multi_warehouse_enabled() or dc_code is None

    rows = master_conn.execute(
        """SELECT sci.id AS cart_item_id, sci.canonical_id, sci.quantity,
                  sci.unit AS cart_unit, sci.created_at AS cart_item_created_at,
                  ci.name, ci.category_code, ci.unit AS canonical_unit
           FROM store_order_cart_items sci
           JOIN canonical_items ci ON ci.id = sci.canonical_id
           WHERE sci.cart_id=?
           ORDER BY sci.id""",
        (cart_id,),
    ).fetchall()

    out: list[dict[str, Any]] = []
    for r in rows:
        item = dict(r)
        canonical_id = item["canonical_id"]
        source_code = (
            get_routing(master_conn, canonical_id) if routed_display else dc_code
        )
        item["source_warehouse_code"] = source_code
        dc_item = (
            get_dc_item_by_canonical(master_conn, source_code, canonical_id)
            if source_code else None
        )
        item["dc_available"] = dc_item["quantity"] if dc_item else 0.0
        item["dc_item_id"] = dc_item["id"] if dc_item else None
        binding = get_store_binding_by_canonical(master_conn, store_code, canonical_id)
        item["store_bound"] = binding is not None
        item["store_item_id"] = binding["id"] if binding else None
        item["category_name"] = category_name_for_display(
            master_conn, dc_item, item["category_code"]
        )
        unit_price = (
            float(dc_item["selling_price"] or 0)
            if dc_item and dc_item["selling_price"] is not None and float(dc_item["selling_price"]) > 0
            else (float(dc_item["unit_cost"] or 0) if dc_item else 0.0)
        )
        item["unit_price"] = unit_price
        item["line_subtotal"] = parse_qty(unit_price * float(item["quantity"]))
        # 起订倍数（DC 本仓字段）：模板用它设 input 的 step/min 与提示文案。
        multiple = normalize_order_multiple(
            dc_item.get("order_multiple") if dc_item else ORDER_MULTIPLE_UNLIMITED
        )
        item["order_multiple"] = multiple
        item["multiple_ok"] = is_multiple_of(item["quantity"], multiple)
        out.append(item)
    return out


def add_cart_item(
    master_conn: sqlite3.Connection,
    cart_id: int,
    canonical_id: int,
    quantity: float,
    unit: str,
) -> None:
    """Add or merge a cart item by canonical_id.

    Raises OrderMultipleError when the merged quantity is not a multiple of
    the DC item's ``order_multiple``（起订倍数）.
    """
    master_conn.row_factory = sqlite3.Row
    qty = parse_qty(quantity)
    if qty <= 0:
        raise ValueError("quantity must be positive")
    unit = (unit or "").strip() or "件"
    existing = master_conn.execute(
        "SELECT id, quantity FROM store_order_cart_items WHERE cart_id=? AND canonical_id=?",
        (cart_id, canonical_id),
    ).fetchone()
    new_qty = parse_qty(qty) if existing is None else parse_qty(existing["quantity"] + qty)
    multiple = get_cart_item_multiple(master_conn, cart_id, canonical_id)
    if not is_multiple_of(new_qty, multiple):
        raise OrderMultipleError(_canonical_name(master_conn, canonical_id), new_qty, multiple)
    ts = now()
    if existing is None:
        master_conn.execute(
            """INSERT INTO store_order_cart_items
               (cart_id, canonical_id, quantity, unit, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (cart_id, canonical_id, new_qty, unit, ts, ts),
        )
    else:
        master_conn.execute(
            "UPDATE store_order_cart_items SET quantity=?, unit=?, updated_at=? WHERE id=?",
            (new_qty, unit, ts, existing["id"]),
        )
    _touch_cart(master_conn, cart_id)
    master_conn.commit()


def _canonical_name(master_conn: sqlite3.Connection, canonical_id: int) -> str:
    """品项显示名，取不到时回落到 #id（错误文案永不为空）。"""
    master_conn.row_factory = sqlite3.Row
    row = master_conn.execute(
        "SELECT name FROM canonical_items WHERE id=?", (canonical_id,)
    ).fetchone()
    return str(row["name"]) if row else f"#{canonical_id}"


def get_cart_item_multiple(
    master_conn: sqlite3.Connection,
    cart_id: int,
    canonical_id: int,
) -> float:
    """购物车所属 DC 上该品项的起订倍数（多仓模式按路由来源仓）。"""
    master_conn.row_factory = sqlite3.Row
    row = master_conn.execute(
        "SELECT dc_warehouse_code FROM store_order_carts WHERE id=?", (cart_id,)
    ).fetchone()
    if row is None:
        return ORDER_MULTIPLE_UNLIMITED
    dc_code = row["dc_warehouse_code"]
    if multi_warehouse_enabled() or dc_code is None:
        dc_code = get_routing(master_conn, canonical_id)
        if dc_code is None:
            return ORDER_MULTIPLE_UNLIMITED
    return get_dc_item_order_multiple(master_conn, dc_code, canonical_id)


def update_cart_item_quantity(
    master_conn: sqlite3.Connection,
    cart_item_id: int,
    quantity: float,
) -> None:
    """Update cart item quantity; delete if <= 0.

    Raises OrderMultipleError when the new quantity does not match the
    DC item's 起订倍数.
    """
    master_conn.row_factory = sqlite3.Row
    qty = parse_qty(quantity)
    if qty <= 0:
        remove_cart_item(master_conn, cart_item_id)
        return
    item = master_conn.execute(
        "SELECT cart_id, canonical_id FROM store_order_cart_items WHERE id=?",
        (cart_item_id,),
    ).fetchone()
    if item is None:
        raise ValueError(f"cart_item_id={cart_item_id} not found")
    multiple = get_cart_item_multiple(master_conn, item["cart_id"], item["canonical_id"])
    if not is_multiple_of(qty, multiple):
        raise OrderMultipleError(
            _canonical_name(master_conn, item["canonical_id"]), qty, multiple
        )
    master_conn.execute(
        "UPDATE store_order_cart_items SET quantity=?, updated_at=? WHERE id=?",
        (qty, now(), cart_item_id),
    )
    _touch_cart(master_conn, item["cart_id"])
    master_conn.commit()


def remove_cart_item(
    master_conn: sqlite3.Connection,
    cart_item_id: int,
) -> None:
    """Delete a single cart item."""
    master_conn.row_factory = sqlite3.Row
    item = master_conn.execute(
        "SELECT cart_id FROM store_order_cart_items WHERE id=?", (cart_item_id,)
    ).fetchone()
    if item is None:
        return
    master_conn.execute("DELETE FROM store_order_cart_items WHERE id=?", (cart_item_id,))
    _touch_cart(master_conn, item["cart_id"])
    master_conn.commit()


def clear_cart(
    master_conn: sqlite3.Connection,
    cart_id: int,
) -> None:
    """Clear all items in a cart."""
    master_conn.execute("DELETE FROM store_order_cart_items WHERE cart_id=?", (cart_id,))
    _touch_cart(master_conn, cart_id)
    master_conn.commit()


def get_cart_item_user_id(
    master_conn: sqlite3.Connection,
    cart_item_id: int,
) -> int | None:
    """Return the user_id owning the cart that contains cart_item_id, or None."""
    master_conn.row_factory = sqlite3.Row
    row = master_conn.execute(
        """SELECT c.user_id
           FROM store_order_cart_items ci
           JOIN store_order_carts c ON c.id = ci.cart_id
           WHERE ci.id=?""",
        (cart_item_id,),
    ).fetchone()
    return int(row["user_id"]) if row else None


# ---------------------------------------------------------------------------
# Available DC items
# ---------------------------------------------------------------------------

def list_available_dc_items(
    master_conn: sqlite3.Connection,
    dc_warehouse_code: str,
    category_code: str | None = None,
    keyword: str | None = None,
    store_warehouse_code: str | None = None,
) -> list[dict[str, Any]]:
    """List DC items that have a non-null canonical_id and active canonical item.

    `store_warehouse_code`（P0-10）：传入后额外排除「该门店已单仓停用
    （items.is_active = 0）」的主数据项 —— 门店停用某品项后，订货目录里
    不再出现它；不传则不做该过滤（如 admin 无门店上下文）。

    Each returned dict is enriched with:
      - canonical_name / canonical_unit (from canonical_items)
      - category_code (from canonical_items; for chip filtering)
      - category_name (Chinese; from DC categories.name → fallback to
        canonical_categories.name → fallback to category_code → fallback to '—')
      - unit_price (selling_price → unit_cost → 0)
      - store_quantity / store_bound (summed from storefront warehouse rows
        matched by canonical_id; missing rows are zero, never DC stock)
    """
    master_conn.row_factory = sqlite3.Row
    # P0-10 + Issue #67：先取该门店已停用的 canonical_id 与本地库存。
    # 后者必须与 DC 行分开，不能用 DC `items.quantity` 回退。
    deactivated: set[int] = set()
    store_quantities: dict[int, float] = {}
    if store_warehouse_code:
        store_conn = open_warehouse_db(store_warehouse_code)
        try:
            store_rows = store_conn.execute(
                """SELECT canonical_id,
                          SUM(quantity) AS quantity,
                          MAX(CASE WHEN COALESCE(is_active, 1) = 0 THEN 1 ELSE 0 END)
                              AS deactivated
                   FROM items
                   WHERE canonical_id IS NOT NULL
                   GROUP BY canonical_id"""
            ).fetchall()
            for row in store_rows:
                canonical_id = int(row["canonical_id"])
                store_quantities[canonical_id] = float(row["quantity"] or 0)
                if int(row["deactivated"]):
                    deactivated.add(canonical_id)
        finally:
            store_conn.close()

    # Resolve canonical_ids in master first so category filtering does not
    # require cross-database subqueries.
    where_parts = ["status = 'active'"]
    params: list[Any] = []
    if category_code:
        where_parts.append("category_code = ?")
        params.append(category_code)
    where = " AND ".join(where_parts)
    canonical_rows = master_conn.execute(
        f"SELECT id, name, category_code, unit, aux_unit, aux_rate FROM canonical_items WHERE {where}",
        params,
    ).fetchall()
    active_canonical_map = {
        int(r["id"]): dict(r) for r in canonical_rows
    }

    dc_conn = open_warehouse_db(dc_warehouse_code)
    try:
        dc_conn.row_factory = sqlite3.Row
        params = []
        where_parts = ["canonical_id IS NOT NULL", "is_orderable = 1"]
        if keyword:
            where_parts.append("(name LIKE ? OR sku LIKE ?)")
            params.extend([f"%{keyword}%", f"%{keyword}%"])
        where = " AND ".join(where_parts)
        rows = dc_conn.execute(
            f"""SELECT *
                FROM items
                WHERE {where}
                ORDER BY name""",
            params,
        ).fetchall()
        # Enrich and filter active canonical items in master.
        out: list[dict[str, Any]] = []
        for r in rows:
            item = dict(r)
            canonical_id = item["canonical_id"]
            canon = active_canonical_map.get(canonical_id)
            if canon is None:
                continue
            if canonical_id in deactivated:
                continue  # P0-10：该门店已单仓停用
            item["canonical_name"] = canon["name"]
            item["category_code"] = canon["category_code"]
            item["canonical_unit"] = canon["unit"]
            item["aux_unit"] = canon["aux_unit"]
            item["aux_rate"] = canon["aux_rate"]
            item["category_name"] = category_name_for_display(
                master_conn, item, canon["category_code"]
            )
            selling_price = float(item.get("selling_price") or 0)
            unit_cost = float(item.get("unit_cost") or 0)
            item["unit_price"] = selling_price if selling_price > 0 else unit_cost
            item["order_multiple"] = normalize_order_multiple(item.get("order_multiple"))
            item["store_quantity"] = store_quantities.get(canonical_id, 0.0)
            item["store_bound"] = canonical_id in store_quantities
            out.append(item)
        return out
    finally:
        dc_conn.close()


def _store_exclusive_canonical_ids(store_warehouse_code: str) -> set[int]:
    """canonical ids the store marked ``is_store_exclusive=1`` (design D10)."""
    store_conn = open_warehouse_db(store_warehouse_code)
    try:
        rows = store_conn.execute(
            "SELECT DISTINCT canonical_id FROM items "
            "WHERE canonical_id IS NOT NULL AND is_store_exclusive=1"
        ).fetchall()
        return {int(row[0]) for row in rows}
    finally:
        store_conn.close()


def list_available_routed_items(
    master_conn: sqlite3.Connection,
    store_warehouse_code: str,
    category_code: str | None = None,
    keyword: str | None = None,
) -> list[dict[str, Any]]:
    """One storefront catalogue merged across every routed source warehouse.

    Reuses :func:`list_available_dc_items` per DC (canonical active, source
    ``is_orderable=1``, store deactivation, price/multiple all stay untouched)
    then intersects with the explicit routing table and removes items the
    store marked exclusive. Unrouted canonicals never appear, so a store
    cannot add something that submit would have to reject (design §5).
    """
    master_conn.row_factory = sqlite3.Row
    exclusive = _store_exclusive_canonical_ids(store_warehouse_code)
    routing_rows = master_conn.execute(
        "SELECT canonical_id, fulfillment_warehouse_code FROM item_routing WHERE tenant_id=1"
    ).fetchall()
    by_dc: dict[str, set[int]] = {}
    for canonical_id, dc_code in routing_rows:
        canonical_id = int(canonical_id)
        if canonical_id in exclusive:
            continue
        by_dc.setdefault(str(dc_code), set()).add(canonical_id)

    out: list[dict[str, Any]] = []
    for dc_code in sorted(by_dc):
        allowed = by_dc[dc_code]
        for item in list_available_dc_items(
            master_conn, dc_code, category_code, keyword,
            store_warehouse_code=store_warehouse_code,
        ):
            canonical_id = item["canonical_id"]
            if canonical_id is None or int(canonical_id) not in allowed:
                continue
            item["source_warehouse_code"] = dc_code
            out.append(item)
    out.sort(key=lambda item: (str(item.get("canonical_name") or ""), int(item["canonical_id"])))
    return out


def list_unorderable_routed_canonicals(
    master_conn: sqlite3.Connection,
    canonical_ids: list[int],
) -> list[dict[str, Any]]:
    """Submit-time re-check of ``is_orderable`` on each line's routed source."""
    out: list[dict[str, Any]] = []
    for canonical_id in canonical_ids:
        dc_code = get_routing(master_conn, canonical_id)
        if dc_code is None:
            continue  # missing routing has its own整单退回 message
        out.extend(list_unorderable_cart_canonicals(master_conn, dc_code, [canonical_id]))
    return out


# ---------------------------------------------------------------------------
# v3.1: 品项可订开关（DC manager + admin）
# ---------------------------------------------------------------------------

#: 主数据总闸的「放行」状态。canonical_items.status 取此值时品项才可能进入
#: 订货目录（list_available_dc_items 的 WHERE 亦为 status = 'active'）。
CANONICAL_ACTIVE_STATUS = "active"

_CANONICAL_STATUS_LABELS = {
    "active": "正常",
    "disabled": "主数据已停用",
    "inactive": "主数据未启用",
}


def canonical_status_label(status: str | None) -> str:
    """把 canonical_items.status 转成人类可读文案（DC 品项管理页用）。"""
    if status in _CANONICAL_STATUS_LABELS:
        return _CANONICAL_STATUS_LABELS[status]
    return f"主数据状态异常（{status or 'NULL'}）"


def list_dc_items_for_management(
    master_conn: sqlite3.Connection,
    dc_warehouse_code: str,
) -> list[dict[str, Any]]:
    """List ALL DC items (含不可订)，用于品项管理页面。

    Enrichment 同 list_available_dc_items；按 category_code 分组返回。

    额外透传**主数据总闸**状态：
      - ``canonical_status``：canonical_items.status 原值
      - ``canonical_active``：是否 == 'active'
      - ``canonical_status_label``：人类可读文案

    为什么必须透传：本页只显示 DC 本仓 ``is_orderable`` 会误导 —— 主数据
    ``status != 'active'`` 时，品项无论 ``is_orderable`` 为何都已从订货目录
    消失（见 ``list_available_dc_items``），操作者看到「可订」会以为主数据
    停用没生效。
    """
    master_conn.row_factory = sqlite3.Row
    canonical_rows = master_conn.execute(
        "SELECT id, name, category_code, status FROM canonical_items"
    ).fetchall()
    canonical_map = {int(r["id"]): dict(r) for r in canonical_rows}

    dc_conn = open_warehouse_db(dc_warehouse_code)
    try:
        dc_conn.row_factory = sqlite3.Row
        rows = dc_conn.execute(
            """SELECT id, sku, name, unit, quantity, canonical_id, is_orderable,
                      order_multiple
               FROM items
               WHERE canonical_id IS NOT NULL
               ORDER BY name"""
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            item = dict(r)
            canonical_id = item["canonical_id"]
            canon = canonical_map.get(canonical_id)
            if canon is None:
                continue
            item["order_multiple"] = normalize_order_multiple(item.get("order_multiple"))
            item["canonical_name"] = canon["name"]
            item["category_code"] = canon["category_code"]
            status = canon.get("status")
            item["canonical_status"] = status
            item["canonical_active"] = status == CANONICAL_ACTIVE_STATUS
            item["canonical_status_label"] = canonical_status_label(status)
            out.append(item)
        return out
    finally:
        dc_conn.close()


def set_dc_item_orderable(
    master_conn: sqlite3.Connection,
    dc_warehouse_code: str,
    canonical_id: int,
    is_orderable: bool,
) -> bool:
    """Toggle a single DC item's orderability.

    Returns True iff a row was updated. Raises ValueError if the DC has
    no item bound to that canonical_id (so the UI can flash an error), or
    if the canonical item is not active in master.

    Why the master-status guard: 主数据总闸（canonical_items.status）不是
    'active' 时，该品项已被 list_available_dc_items 整体过滤掉 —— DC 分闸
    无论怎么改，门店都看不到、也订不了。此时允许写入只会制造「我点了恢复
    可订，门店还是订不了」的假象，故直接拒绝并提示去主数据处理。
    """
    master_conn.row_factory = sqlite3.Row
    canon = master_conn.execute(
        "SELECT status FROM canonical_items WHERE id = ?",
        (canonical_id,),
    ).fetchone()
    if canon is None:
        raise ValueError(f"主数据中不存在 canonical_id={canonical_id} 的品项")
    canon_status = canon["status"]
    if canon_status != CANONICAL_ACTIVE_STATUS:
        raise ValueError(
            f"该品项主数据状态为「{canonical_status_label(canon_status)}」，"
            "配送中心开关不生效；请先到品项主数据中启用后再调整。"
        )

    dc_conn = open_warehouse_db(dc_warehouse_code)
    try:
        dc_conn.row_factory = sqlite3.Row
        cur = dc_conn.execute(
            "UPDATE items SET is_orderable=? WHERE canonical_id=?",
            (1 if is_orderable else 0, canonical_id),
        )
        dc_conn.commit()
        if cur.rowcount == 0:
            raise ValueError(
                f"DC {dc_warehouse_code} 无 canonical_id={canonical_id} 的品项"
            )
        return True
    finally:
        dc_conn.close()


def set_dc_item_order_multiple(
    master_conn: sqlite3.Connection,
    dc_warehouse_code: str,
    canonical_id: int,
    multiple: float,
) -> float:
    """设置单品的起订倍数，返回归一化后真正落库的值。

    ``multiple`` <= 1（或非法）→ 落 1，等于「不限倍数」。
    与 `set_dc_item_orderable` 不同，这里**不**校验主数据状态：倍数只影响
    数量校验，主数据停用的品项本来就订不到，配置留着无害。

    Raises ValueError 当该 DC 没有绑定此 canonical_id 的品项。
    """
    step = normalize_order_multiple(multiple)
    dc_conn = open_warehouse_db(dc_warehouse_code)
    try:
        cur = dc_conn.execute(
            "UPDATE items SET order_multiple=? WHERE canonical_id=?",
            (step, canonical_id),
        )
        dc_conn.commit()
        if cur.rowcount == 0:
            raise ValueError(
                f"DC {dc_warehouse_code} 无 canonical_id={canonical_id} 的品项"
            )
    finally:
        dc_conn.close()
    return step


def list_unorderable_cart_canonicals(
    master_conn: sqlite3.Connection,
    dc_warehouse_code: str,
    canonical_ids: list[int],
) -> list[dict[str, Any]]:
    """Return canonical_id → canonical_name for items that exist in
    ``canonical_ids`` but have ``is_orderable=0`` in the given DC.

    Used by submit-time validation to block cart submissions against
    items that have been pulled since they were added.
    """
    if not canonical_ids:
        return []
    dc_conn = open_warehouse_db(dc_warehouse_code)
    try:
        dc_conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" * len(canonical_ids))
        rows = dc_conn.execute(
            f"""SELECT canonical_id, name AS canonical_name
                FROM items
                WHERE canonical_id IN ({placeholders})
                  AND is_orderable = 0""",
            canonical_ids,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        dc_conn.close()


# ---------------------------------------------------------------------------
# P1-5: 订货量建议（基于门店近 7 天消耗 + 安全库存）
# ---------------------------------------------------------------------------

def compute_suggested_order_qty(
    store_conn: sqlite3.Connection,
    master_conn: sqlite3.Connection,
    store_warehouse_code: str,
    *,
    cover_days: int = 7,
    min_absolute: float = 0.0,
    window_days: int = 7,
) -> dict[int, int]:
    """Per-canonical suggested order quantity for a storefront warehouse.

    Returns: ``{canonical_id: suggested_qty}``. canonical_ids with no
    consumption and no in-transit orders are omitted.

    Data sources:
      - daily_avg = outbound_requests in last ``window_days``, grouped by
        canonical_id via the store's local items.canonical_id
      - current_qty = store's local items.quantity (sum if duplicate
        canonical bindings exist)
      - in_transit = sum of (quantity - fulfilled_quantity) for store's
        own store_order_items whose parent order is in pending/approved/
        shipped (尚未收齐的)

    Formula (复用 procurement_pure):
      safety_stock = compute_safety_stock(daily_avg, cover_days, min_absolute)
      suggested    = ceil(max(0, safety_stock - current_qty - in_transit))
    """
    store_conn.row_factory = sqlite3.Row
    master_conn.row_factory = sqlite3.Row

    # 1. 每 canonical 7 天消耗
    out_rows = store_conn.execute(
        """SELECT i.canonical_id AS canonical_id,
                  SUM(o.requested_quantity) AS total_qty
           FROM outbound_requests o
           JOIN items i ON i.id = o.item_id
           WHERE o.rolled_back = 0
             AND i.canonical_id IS NOT NULL
             AND o.created_at >= datetime('now', ?)
           GROUP BY i.canonical_id""",
        (f"-{window_days} days",),
    ).fetchall()
    daily_avg_by_canonical: dict[int, float] = {
        int(r["canonical_id"]): float(r["total_qty"]) / float(window_days)
        for r in out_rows
    }

    # 2. 当前门店库存
    cur_rows = store_conn.execute(
        """SELECT canonical_id, SUM(quantity) AS total_qty
           FROM items WHERE canonical_id IS NOT NULL
           GROUP BY canonical_id""",
    ).fetchall()
    current_qty_by_canonical: dict[int, float] = {
        int(r["canonical_id"]): float(r["total_qty"]) for r in cur_rows
    }

    # 3. 在途：pending / approved / shipped 未收齐的本店订单
    transit_rows = master_conn.execute(
        """SELECT soi.canonical_id AS canonical_id,
                  SUM(soi.quantity - soi.fulfilled_quantity) AS pending_qty
           FROM store_order_items soi
           JOIN store_orders so ON so.id = soi.order_id
           WHERE so.store_warehouse_code = ?
             AND so.status IN (?, ?, ?)
             AND (soi.quantity - soi.fulfilled_quantity) > 0
           GROUP BY soi.canonical_id""",
        (
            store_warehouse_code,
            ORDER_STATUS_PENDING,
            ORDER_STATUS_APPROVED,
            ORDER_STATUS_SHIPPED,
        ),
    ).fetchall()
    transit_by_canonical: dict[int, float] = {
        int(r["canonical_id"]): float(r["pending_qty"]) for r in transit_rows
    }

    # 4. 计算
    all_canonicals = (
        set(daily_avg_by_canonical)
        | set(transit_by_canonical)
    )
    out: dict[int, int] = {}
    for cid in all_canonicals:
        daily_avg = daily_avg_by_canonical.get(cid, 0.0)
        current = current_qty_by_canonical.get(cid, 0.0)
        transit = transit_by_canonical.get(cid, 0.0)
        safety = compute_safety_stock(daily_avg, cover_days, min_absolute)
        suggested = compute_suggested_qty(safety, current, transit)
        if suggested > 0:
            out[cid] = suggested
    return out


# ---------------------------------------------------------------------------
# P2-4: 报表（按门店/DC/品类/品项汇总订货/出货/收货/欠收率）
# ---------------------------------------------------------------------------

def compute_store_order_report(
    master_conn: sqlite3.Connection,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    store_warehouse_code: str | None = None,
    dc_warehouse_code: str | None = None,
    category_code: str | None = None,
) -> dict[str, Any]:
    """多维门店订货报表。

    指标口径（基础单位）：
      - ordered_qty  : store_order_items.quantity 累加（订货）
      - shipped_qty  : store_order_items.shipped_quantity 累加（DC 出货）
      - received_qty : store_order_items.fulfilled_quantity 累加（门店收货）
      - shortfall_qty: max(0, ordered - received)（欠收量；为负时按 0 算）
      - shortfall_rate: shortfall / ordered_qty（欠收率；ordered=0 时为 0）

    时间范围过滤：store_orders.created_at ∈ [start_date 00:00, end_date 23:59]
    类别过滤：canonical_items.category_code
    返回：含 ``summary`` + 4 个分维数组的 dict（按 4 个 group key 求和）。
    """
    master_conn.row_factory = sqlite3.Row

    where_parts = ["so.flow_type = ?"]
    params: list[Any] = [FLOW_STORE_ORDER]
    if start_date:
        where_parts.append("so.created_at >= ?")
        params.append(f"{start_date} 00:00:00")
    if end_date:
        where_parts.append("so.created_at <= ?")
        params.append(f"{end_date} 23:59:59")
    if store_warehouse_code:
        where_parts.append("so.store_warehouse_code = ?")
        params.append(store_warehouse_code)
    if dc_warehouse_code:
        where_parts.append("so.dc_warehouse_code = ?")
        params.append(dc_warehouse_code)
    if category_code:
        where_parts.append("ci.category_code = ?")
        params.append(category_code)
    where = " AND ".join(where_parts)

    sql = f"""
        SELECT
            so.store_warehouse_code AS store_code,
            so.dc_warehouse_code    AS dc_code,
            ci.category_code        AS cat_code,
            ci.name                 AS canonical_name,
            soi.canonical_id        AS canonical_id,
            SUM(soi.quantity)              AS ordered_qty,
            SUM(soi.shipped_quantity)      AS shipped_qty,
            SUM(soi.fulfilled_quantity)    AS received_qty,
            COUNT(DISTINCT so.id)          AS order_count
        FROM store_order_items soi
        JOIN store_orders so ON so.id = soi.order_id
        JOIN canonical_items ci ON ci.id = soi.canonical_id
        WHERE {where}
        GROUP BY so.store_warehouse_code, so.dc_warehouse_code,
                 ci.category_code, ci.name, soi.canonical_id
    """
    rows = master_conn.execute(sql, params).fetchall()

    by_store: dict[str, dict] = {}
    by_dc: dict[str, dict] = {}
    by_category: dict[str, dict] = {}
    by_canonical: list[dict] = []
    summary = {
        "ordered_qty": 0.0,
        "shipped_qty": 0.0,
        "received_qty": 0.0,
        "shortfall_qty": 0.0,
        "shipping_fee": 0.0,
        "order_count": 0,
    }
    total_shortfall = 0.0
    total_ordered_for_rate = 0.0

    for r in rows:
        oq = float(r["ordered_qty"] or 0)
        sq = float(r["shipped_qty"] or 0)
        rq = float(r["received_qty"] or 0)
        shortfall = max(0.0, oq - rq)

        summary["ordered_qty"] += oq
        summary["shipped_qty"] += sq
        summary["received_qty"] += rq
        summary["shortfall_qty"] += shortfall
        total_shortfall += shortfall
        total_ordered_for_rate += oq

        # by_store
        s = by_store.setdefault(
            r["store_code"],
            {"store_code": r["store_code"], "ordered_qty": 0.0,
             "shipped_qty": 0.0, "received_qty": 0.0, "shortfall_qty": 0.0,
             "shipping_fee": 0.0,
             "order_count": 0},
        )
        s["ordered_qty"] += oq
        s["shipped_qty"] += sq
        s["received_qty"] += rq
        s["shortfall_qty"] += shortfall

        # by_dc
        d = by_dc.setdefault(
            r["dc_code"],
            {"dc_code": r["dc_code"], "ordered_qty": 0.0,
             "shipped_qty": 0.0, "received_qty": 0.0, "shortfall_qty": 0.0,
             "shipping_fee": 0.0,
             "order_count": 0},
        )
        d["ordered_qty"] += oq
        d["shipped_qty"] += sq
        d["received_qty"] += rq
        d["shortfall_qty"] += shortfall

        # by_category
        c = by_category.setdefault(
            r["cat_code"],
            {"category_code": r["cat_code"], "ordered_qty": 0.0,
             "shipped_qty": 0.0, "received_qty": 0.0, "shortfall_qty": 0.0},
        )
        c["ordered_qty"] += oq
        c["shipped_qty"] += sq
        c["received_qty"] += rq
        c["shortfall_qty"] += shortfall

        by_canonical.append({
            "canonical_id": r["canonical_id"],
            "canonical_name": r["canonical_name"],
            "category_code": r["cat_code"],
            "ordered_qty": oq,
            "shipped_qty": sq,
            "received_qty": rq,
            "shortfall_qty": shortfall,
        })

    # 后算 order_count（去重）：每个 store 在过滤范围内出现的订单数
    # oc_where_parts 复用 where_parts，但移除引用 ci. 的部分
    oc_where_parts: list[str] = []
    oc_params: list[Any] = []
    p_iter = iter(params)
    for w in where_parts:
        if w == "1=1":
            continue
        if "ci." in w:
            # skip ci-related predicate and its param
            next(p_iter, None)
            continue
        oc_where_parts.append(w)
        oc_params.append(next(p_iter))
    if not oc_where_parts:
        oc_where = "1=1"
    else:
        oc_where = " AND ".join(oc_where_parts)
    oc_sql = f"""
        SELECT so.store_warehouse_code AS store_code,
               so.dc_warehouse_code    AS dc_code,
               COUNT(DISTINCT so.id)   AS order_count,
               SUM(so.shipping_fee)    AS shipping_fee
        FROM store_orders so
        WHERE {oc_where}
        GROUP BY so.store_warehouse_code, so.dc_warehouse_code
    """
    for r in master_conn.execute(oc_sql, oc_params).fetchall():
        sf = float(r["shipping_fee"] or 0)
        if r["store_code"] in by_store:
            by_store[r["store_code"]]["order_count"] = int(r["order_count"])
            by_store[r["store_code"]]["shipping_fee"] += sf
        if r["dc_code"] in by_dc:
            by_dc[r["dc_code"]]["order_count"] = int(r["order_count"])
            by_dc[r["dc_code"]]["shipping_fee"] += sf
        summary["order_count"] += int(r["order_count"])
        summary["shipping_fee"] += sf

    # 排序 + 算费率
    for d in by_store.values():
        d["shortfall_rate"] = (d["shortfall_qty"] / d["ordered_qty"]) if d["ordered_qty"] > 0 else 0.0
    for d in by_dc.values():
        d["shortfall_rate"] = (d["shortfall_qty"] / d["ordered_qty"]) if d["ordered_qty"] > 0 else 0.0
    for d in by_category.values():
        d["shortfall_rate"] = (d["shortfall_qty"] / d["ordered_qty"]) if d["ordered_qty"] > 0 else 0.0
    for d in by_canonical:
        d["shortfall_rate"] = (d["shortfall_qty"] / d["ordered_qty"]) if d["ordered_qty"] > 0 else 0.0

    summary["shortfall_rate"] = (
        total_shortfall / total_ordered_for_rate if total_ordered_for_rate > 0 else 0.0
    )
    # 排序：by_store / by_dc 按 ordered_qty DESC
    by_store_list = sorted(by_store.values(), key=lambda x: x["ordered_qty"], reverse=True)
    by_dc_list = sorted(by_dc.values(), key=lambda x: x["ordered_qty"], reverse=True)
    by_category_list = sorted(by_category.values(), key=lambda x: x["ordered_qty"], reverse=True)
    by_canonical_list = sorted(by_canonical, key=lambda x: x["ordered_qty"], reverse=True)

    return {
        "summary": summary,
        "by_store": by_store_list,
        "by_dc": by_dc_list,
        "by_category": by_category_list,
        "by_canonical": by_canonical_list,
    }


def get_dc_item_by_canonical(
    master_conn: sqlite3.Connection,
    dc_warehouse_code: str,
    canonical_id: int,
) -> dict[str, Any] | None:
    """Return DC warehouse item bound to canonical_id, or None."""
    master_conn.row_factory = sqlite3.Row
    canon = master_conn.execute(
        "SELECT name, category_code, unit, status FROM canonical_items WHERE id=?",
        (canonical_id,),
    ).fetchone()
    if canon is None or canon["status"] != "active":
        return None
    dc_conn = open_warehouse_db(dc_warehouse_code)
    try:
        dc_conn.row_factory = sqlite3.Row
        row = dc_conn.execute(
            "SELECT * FROM items WHERE canonical_id=?", (canonical_id,)
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["canonical_name"] = canon["name"]
        item["category_code"] = canon["category_code"]
        item["canonical_unit"] = canon["unit"]
        return item
    finally:
        dc_conn.close()


def get_store_binding_by_canonical(
    master_conn: sqlite3.Connection,
    store_warehouse_code: str,
    canonical_id: int,
) -> dict[str, Any] | None:
    """Return store warehouse item bound to canonical_id, or None."""
    master_conn.row_factory = sqlite3.Row
    store_conn = open_warehouse_db(store_warehouse_code)
    try:
        store_conn.row_factory = sqlite3.Row
        row = store_conn.execute(
            "SELECT * FROM items WHERE canonical_id=?", (canonical_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        store_conn.close()


def category_name_for_display(
    master_conn: sqlite3.Connection,
    dc_item: dict[str, Any] | None,
    canonical_category_code: str | None,
) -> str:
    """Resolve a Chinese category name for display.

    Priority:
      1. DC warehouse `categories.name` joined on `items.category_id`
         (only if dc_item was provided and has category_id).
      2. `canonical_categories.name` joined on canonical_category_code.
      3. canonical_category_code itself (raw code).
      4. '—' (placeholder).
    """
    master_conn.row_factory = sqlite3.Row
    if dc_item and dc_item.get("category_id"):
        # Look up via the dc_item's warehouse db. Use the dc_item's
        # `category_name` attribute if present (some callers pre-join).
        if "category_name" in dc_item and dc_item["category_name"]:
            name = str(dc_item["category_name"])
            if name:
                return name
        # Otherwise, we don't have a warehouse db handle here; fall through.
    if canonical_category_code:
        row = master_conn.execute(
            "SELECT name FROM canonical_categories WHERE code=?",
            (canonical_category_code,),
        ).fetchone()
        if row and row["name"]:
            return str(row["name"])
        return str(canonical_category_code)
    return "—"


# ---------------------------------------------------------------------------
# Submit / validation
# ---------------------------------------------------------------------------

def validate_cart_for_submit(
    master_conn: sqlite3.Connection,
    cart: dict[str, Any],
) -> dict[str, Any]:
    """Validate cart for order submission.

    v3 change (A7: 门店不感知库存): DC 库存校验移除——门店可以订出
    DC 库存为 0 的品项。仅保留 canonical_id 绑定校验（unbound）和购物车
    非空校验。

    Returns {"ok": bool, "unbound": [...], "multiples": [...]}
      - ok: True 当且仅当购物车非空、没有未绑定品项、且每行数量都满足起订倍数
      - unbound: list[{canonical_id, name}] 等待门店补绑定的品项
      - multiples: list[{canonical_id, name, quantity, multiple, unit}] 数量
        不满足 DC 端配置的起订倍数的品项（v3, 2026-10-18）
    """
    master_conn.row_factory = sqlite3.Row
    items = list_cart_items(master_conn, cart["id"])
    unbound: list[dict[str, Any]] = []
    multiples: list[dict[str, Any]] = []
    for item in items:
        canonical_id = item["canonical_id"]
        name = item["name"]
        if not item.get("store_bound"):
            unbound.append({"canonical_id": canonical_id, "name": name})
        if not item.get("multiple_ok", True):
            multiples.append({
                "canonical_id": canonical_id,
                "name": name,
                "quantity": parse_qty(item["quantity"]),
                "multiple": normalize_order_multiple(item.get("order_multiple")),
                "unit": item.get("cart_unit") or "",
            })
    return {
        "ok": not unbound and not multiples and bool(items),
        "unbound": unbound,
        "multiples": multiples,
    }


def _primary_source_warehouse(codes: list[str]) -> str:
    """D8 header rule: most lines wins; ties break on the smallest warehouse code."""
    counts: dict[str, int] = {}
    for code in codes:
        counts[code] = counts.get(code, 0) + 1
    return min(counts, key=lambda code: (-counts[code], code))


def _resolve_routed_cart_lines(
    master_conn: sqlite3.Connection,
    cart_items: list[dict[str, Any]],
    store_warehouse_code: str,
) -> list[dict[str, Any]]:
    """Resolve routing → unique fulfillment row → price/multiple, once, at submit.

    Missing routing rejects the whole order (D6) with the item list; store
    exclusive items never enter a DC order (D10). Resolution failures keep the
    fulfillment error text so the storepage can point at the exact item.
    """
    exclusive = _store_exclusive_canonical_ids(store_warehouse_code)
    missing: list[str] = []
    blocked: list[str] = []
    lines: list[dict[str, Any]] = []
    for ci in cart_items:
        canonical_id = int(ci["canonical_id"])
        name = ci["name"]
        if canonical_id in exclusive:
            blocked.append(name)
            continue
        dc_code = get_routing(master_conn, canonical_id)
        if dc_code is None:
            missing.append(name)
            continue
        wh_conn = open_warehouse_db(dc_code)
        try:
            resolved = resolve_fulfillment_item(
                master_conn, wh_conn, canonical_id=canonical_id, warehouse_code=dc_code,
            )
            row = wh_conn.execute("SELECT * FROM items WHERE id=?", (resolved["item_id"],)).fetchone()
            unit_price, _ = parse_price_and_multiple(row)
        except ValueError as exc:
            raise ValueError(f"{name} 无法解析履约行：{exc}") from exc
        finally:
            wh_conn.close()
        lines.append({
            "canonical_id": canonical_id,
            "name": name,
            "quantity": parse_qty(ci["quantity"]),
            "unit": ci.get("cart_unit") or resolved["unit"],
            "store_item_id": ci.get("store_item_id"),
            "dc_item_id": resolved["item_id"],
            "source_warehouse_code": dc_code,
            "unit_price": unit_price,
            "order_multiple": resolved["order_multiple"],
        })
    if missing:
        raise ValueError("以下品项未配置履约仓，整单退回：" + "、".join(missing))
    if blocked:
        raise ValueError("以下品项为门店专属，不能提交订货单：" + "、".join(blocked))
    return lines


def _snapshot_changes(
    lines: list[dict[str, Any]],
    price_snapshot: dict[int, float] | None,
    multiple_snapshot: dict[int, float] | None,
) -> list[dict[str, Any]]:
    """Compare resolved values against what the cart page showed the store."""
    changes: list[dict[str, Any]] = []
    for line in lines:
        canonical_id = line["canonical_id"]
        if price_snapshot is not None and canonical_id in price_snapshot:
            old = parse_qty(price_snapshot[canonical_id])
            new = parse_qty(line["unit_price"])
            if abs(old - new) > 1e-9:
                changes.append({
                    "canonical_id": canonical_id, "name": line["name"],
                    "field": "price", "old": old, "new": new,
                })
        if multiple_snapshot is not None and canonical_id in multiple_snapshot:
            old_m = normalize_order_multiple(multiple_snapshot[canonical_id])
            new_m = normalize_order_multiple(line["order_multiple"])
            if abs(old_m - new_m) > 1e-9:
                changes.append({
                    "canonical_id": canonical_id, "name": line["name"],
                    "field": "multiple", "old": old_m, "new": new_m,
                })
    return changes


def submit_order(
    master_conn: sqlite3.Connection,
    cart_id: int,
    requested_by: int,
    expected_delivery_date: str | None,
    note: str | None,
    *,
    price_snapshot: dict[int, float] | None = None,
    multiple_snapshot: dict[int, float] | None = None,
    confirm_price_change: bool = False,
) -> dict[str, Any]:
    """Convert cart to order, return order dict.

    v3.1: 写入 shipping_fee（基于当前 active 运费规则 + 订单金额合计）。
    运费在 submit 时锁定，ship / receive 时不再重算。

    多仓开关开：逐行解析路由与履约行，把 ``source_warehouse_code`` /
    ``dc_item_id`` 固化到订单行；路由缺失整单退回（D6）。购物车阶段展示的
    价格/倍数与本刻重解析不一致时，返回 ``{"price_changed": [...]}``，门店
    带 ``confirm_price_change=1`` 重提交才建单（设计 §5）。
    """
    master_conn.row_factory = sqlite3.Row
    cart = master_conn.execute(
        "SELECT * FROM store_order_carts WHERE id=?", (cart_id,)
    ).fetchone()
    if cart is None:
        raise ValueError(f"cart_id={cart_id} not found")
    cart_dict = dict(cart)
    validation = validate_cart_for_submit(master_conn, cart_dict)
    if not validation["ok"]:
        raise ValueError(f"cart validation failed: {validation}")

    cart_items = list_cart_items(master_conn, cart_id)
    ts = now()
    multi = multi_warehouse_enabled()
    if multi:
        lines = _resolve_routed_cart_lines(
            master_conn, cart_items, cart_dict["store_warehouse_code"],
        )
        changes = _snapshot_changes(lines, price_snapshot, multiple_snapshot)
        if changes and not confirm_price_change:
            return {"order": None, "price_changed": changes}
        subtotal = sum(
            parse_qty(line["unit_price"] * line["quantity"]) for line in lines
        )
        header_dc = _primary_source_warehouse(
            [line["source_warehouse_code"] for line in lines]
        )
    else:
        lines = [{
            "canonical_id": ci["canonical_id"],
            "name": ci["name"],
            "quantity": parse_qty(ci["quantity"]),
            "unit": ci["cart_unit"],
            "store_item_id": ci.get("store_item_id"),
            "dc_item_id": None,
            "source_warehouse_code": cart_dict["dc_warehouse_code"],
            "unit_price": ci.get("unit_price"),
            "order_multiple": ci.get("order_multiple"),
        } for ci in cart_items]
        subtotal = sum(parse_qty(ci.get("line_subtotal") or 0) for ci in cart_items)
        header_dc = cart_dict["dc_warehouse_code"]

    order_no = generate_order_no(master_conn)
    # v3.1: 算订单金额合计 + 运费
    rule = get_active_shipping_rule(master_conn)
    shipping_fee = compute_shipping_fee(subtotal, rule)

    cur = master_conn.execute(
        """INSERT INTO store_orders
           (order_no, store_warehouse_code, dc_warehouse_code, status,
            requested_by, expected_delivery_date, note, created_at, updated_at,
            shipping_fee)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            order_no, cart_dict["store_warehouse_code"], header_dc,
            ORDER_STATUS_PENDING, requested_by, expected_delivery_date,
            note, ts, ts, shipping_fee,
        ),
    )
    order_id = int(cur.lastrowid)
    for line in lines:
        master_conn.execute(
            """INSERT INTO store_order_items
               (order_id, canonical_id, dc_item_id, store_item_id, source_warehouse_code,
                quantity, unit, fulfilled_quantity, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                order_id, line["canonical_id"], line["dc_item_id"], line["store_item_id"],
                line["source_warehouse_code"], line["quantity"], line["unit"], 0.0,
                ORDER_ITEM_STATUS_PENDING, ts,
            ),
        )
    # Clear cart items but keep the cart row so user can build next order.
    clear_cart(master_conn, cart_id)
    _insert_status_history(master_conn, order_id, None, ORDER_STATUS_PENDING, requested_by, note)
    master_conn.commit()
    return get_order_detail(master_conn, order_id)


# ---------------------------------------------------------------------------
# v3.1: 运费规则（admin 配置 + submit 时计算）
# ---------------------------------------------------------------------------

def compute_shipping_fee(subtotal: float, rule: dict | None) -> float:
    """fee = base_fee + subtotal × pct_fee, 2dp 量化。

    rule 为 None（无 active 规则）→ 返回 0（向后兼容）。
    """
    if rule is None:
        return 0.0
    base = float(rule.get("base_fee") or 0)
    pct = float(rule.get("pct_fee") or 0)
    raw = base + parse_qty(subtotal) * pct
    # 2dp 量化，与货币一致
    return float(Decimal(str(raw)).quantize(Decimal("0.01")))


def get_active_shipping_rule(master_conn: sqlite3.Connection) -> dict | None:
    """Return the single active rule, or None if no rule is active."""
    master_conn.row_factory = sqlite3.Row
    row = master_conn.execute(
        "SELECT * FROM shipping_rules WHERE active=1 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def upsert_shipping_rule(
    master_conn: sqlite3.Connection,
    *,
    name: str = "默认",
    base_fee: float,
    pct_fee: float,
    active: bool = True,
) -> int:
    """Create or update the active rule (MVP 单条规则)。

    Strategy: deactivate any existing active row, then INSERT new.
    Returns the new rule id.
    """
    ts = now()
    master_conn.row_factory = sqlite3.Row
    if active:
        # 关闭所有现有 active（应只有一条）
        master_conn.execute("UPDATE shipping_rules SET active=0, updated_at=?", (ts,))
    cur = master_conn.execute(
        """INSERT INTO shipping_rules
           (name, base_fee, pct_fee, active, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (name, float(base_fee), float(pct_fee), 1 if active else 0, ts, ts),
    )
    master_conn.commit()
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# Order queries
# ---------------------------------------------------------------------------

def get_dc_items_for_order_detail(
    master_conn: sqlite3.Connection,
    order_id: int,
) -> dict[int, dict[str, Any]]:
    """Return {order_item_id: {name, dc_available, unit_price, category_name}}.

    Used by ``order_detail`` to render the "DC 库存" column on every line of
    the DC-view table. Returns an empty dict when the order does not exist.
    发货已禁止超库存（2026-10-10），新建订单的 ``dc_available`` 不会为负；
    历史遗留的负库存行仍会原样返回，模板以红色高亮提示。

    Lookup strategy for ``dc_available`` (per F4=A: DC 库存始终可见):
      - 优先用 ``store_order_items.dc_item_id``（出库后回填的 dc 仓 items.id）
      - 若 ``dc_item_id`` 为 NULL（pending 阶段还没出库），按 ``canonical_id``
        在 DC 仓 ``items`` 表查询；仍找不到则 ``dc_available=0.0``。
    """
    master_conn.row_factory = sqlite3.Row
    order_row = master_conn.execute(
        "SELECT dc_warehouse_code FROM store_orders WHERE id=?",
        (order_id,),
    ).fetchone()
    if order_row is None:
        return {}
    dc_code = order_row["dc_warehouse_code"]

    rows = master_conn.execute(
        """SELECT soi.id AS order_item_id, soi.canonical_id,
                  soi.dc_item_id, ci.name AS canonical_name, ci.category_code
           FROM store_order_items soi
           JOIN canonical_items ci ON ci.id = soi.canonical_id
           WHERE soi.order_id=?""",
        (order_id,),
    ).fetchall()

    dc_conn = open_warehouse_db(dc_code)
    try:
        out: dict[int, dict[str, Any]] = {}
        for r in rows:
            dc_available = 0.0
            unit_price = 0.0
            category_name = ""
            dc_row = None
            if r["dc_item_id"] is not None:
                dc_row = dc_conn.execute(
                    """SELECT i.quantity, i.selling_price, i.unit_cost,
                              c.name AS category_name
                       FROM items i
                       LEFT JOIN categories c ON c.id = i.category_id
                       WHERE i.id=?""",
                    (r["dc_item_id"],),
                ).fetchone()
            if dc_row is None and r["canonical_id"] is not None:
                # Fallback: resolve DC stock via canonical_id (used pre-shipment).
                dc_row = dc_conn.execute(
                    """SELECT i.quantity, i.selling_price, i.unit_cost,
                              c.name AS category_name
                       FROM items i
                       LEFT JOIN categories c ON c.id = i.category_id
                       WHERE i.canonical_id=?""",
                    (int(r["canonical_id"]),),
                ).fetchone()
            if dc_row is not None:
                dc_available = parse_qty(dc_row["quantity"])
                selling = float(dc_row["selling_price"] or 0)
                cost = float(dc_row["unit_cost"] or 0)
                unit_price = selling if selling > 0 else cost
                if dc_row["category_name"]:
                    category_name = str(dc_row["category_name"])
            if not category_name and r["category_code"]:
                cc_row = master_conn.execute(
                    "SELECT name FROM canonical_categories WHERE code=?",
                    (r["category_code"],),
                ).fetchone()
                if cc_row:
                    category_name = str(cc_row["name"])
            out[int(r["order_item_id"])] = {
                "name": r["canonical_name"],
                "dc_available": dc_available,
                "unit_price": unit_price,
                "category_name": category_name or "—",
            }
        return out
    finally:
        dc_conn.close()


def _participants_by_order(
    master_conn: sqlite3.Connection,
    order_ids: list[int],
) -> dict[int, list[str]]:
    """Map order_id -> sorted participating warehouse codes (one batch query).

    Uses ``DISTINCT`` over the line sources so an order with several lines in
    the same warehouse is still counted once (spec §10 EXISTS semantics).
    """
    if not order_ids:
        return {}
    master_conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in order_ids)
    rows = master_conn.execute(
        f"""SELECT DISTINCT so.id AS order_id,
                   COALESCE(soi.source_warehouse_code, so.dc_warehouse_code) AS code
              FROM store_orders so
              LEFT JOIN store_order_items soi ON soi.order_id = so.id
             WHERE so.id IN ({placeholders})""",
        order_ids,
    ).fetchall()
    codes: dict[int, set[str]] = {}
    for row in rows:
        if row["code"]:
            codes.setdefault(int(row["order_id"]), set()).add(str(row["code"]))
    return {order_id: sorted(warehouses) for order_id, warehouses in codes.items()}


def _warehouse_name_map(master_conn: sqlite3.Connection) -> dict[str, str]:
    master_conn.row_factory = sqlite3.Row
    return {
        str(row["code"]): str(row["name"] or row["code"])
        for row in master_conn.execute("SELECT code, name FROM warehouses").fetchall()
    }


def _attach_participant_warehouses(
    master_conn: sqlite3.Connection,
    orders: list[dict[str, Any]],
) -> None:
    """Annotate list rows with participant codes/names and a cross-warehouse flag.

    Orders without any line row (legacy fixtures / pre-migration dirty data)
    fall back to the header warehouse — that is the only signal left.
    """
    if not orders:
        return
    codes_by_order = _participants_by_order(
        master_conn, [int(order["id"]) for order in orders]
    )
    names = _warehouse_name_map(master_conn)
    for order in orders:
        codes = codes_by_order.get(int(order["id"]))
        if not codes:
            head = order.get("dc_warehouse_code")
            codes = [str(head)] if head else []
        order["participant_warehouses"] = [
            {"code": code, "name": names.get(code, code)} for code in codes
        ]
        order["is_multi_warehouse"] = len(codes) > 1


def list_orders(
    master_conn: sqlite3.Connection,
    *,
    store_warehouse_code: str | None = None,
    dc_warehouse_code: str | None = None,
    participant_warehouse_code: str | None = None,
    status: str | None = None,
    flow_type: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Query orders with optional filters.

    ``participant_warehouse_code`` filters by the unified "参与仓" predicate
    (spec §10): any line whose source warehouse (header fallback for legacy
    NULL rows) equals the code. Orders with no line rows at all also match
    their header warehouse — pre-multi-warehouse data had no per-line source.
    """
    master_conn.row_factory = sqlite3.Row
    where_parts: list[str] = []
    params: list[Any] = []
    if store_warehouse_code:
        where_parts.append("store_warehouse_code = ?")
        params.append(store_warehouse_code)
    if dc_warehouse_code:
        where_parts.append("dc_warehouse_code = ?")
        params.append(dc_warehouse_code)
    if participant_warehouse_code:
        where_parts.append(
            f"({order_participation_sql('so')} OR "
            "(so.dc_warehouse_code = ? AND NOT EXISTS ("
            "SELECT 1 FROM store_order_items legacy WHERE legacy.order_id = so.id)))"
        )
        params.extend([participant_warehouse_code, participant_warehouse_code])
    if status:
        where_parts.append("status = ?")
        params.append(status)
    if flow_type:
        where_parts.append("flow_type = ?")
        params.append(flow_type)
    if start_date:
        where_parts.append("created_at >= ?")
        params.append(f"{start_date} 00:00:00")
    if end_date:
        where_parts.append("created_at <= ?")
        params.append(f"{end_date} 23:59:59")
    where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    params.extend([limit, offset])
    rows = master_conn.execute(
        f"""SELECT so.*,
                   req.username AS requested_by_username,
                   appr.username AS approved_by_username,
                   ship.username AS shipped_by_username,
                   ws.name AS store_warehouse_name,
                   wd.name AS dc_warehouse_name
            FROM store_orders so
            LEFT JOIN users req ON req.id = so.requested_by
            LEFT JOIN users appr ON appr.id = so.approved_by
            LEFT JOIN users ship ON ship.id = so.shipped_by
            LEFT JOIN warehouses ws ON ws.code = so.store_warehouse_code
            LEFT JOIN warehouses wd ON wd.code = so.dc_warehouse_code
            {where}
            ORDER BY so.created_at DESC, so.id DESC
            LIMIT ? OFFSET ?""",
        params,
    ).fetchall()
    orders = [dict(r) for r in rows]
    _attach_participant_warehouses(master_conn, orders)
    return orders


def count_orders_by_status(
    master_conn: sqlite3.Connection,
    *,
    store_warehouse_code: str | None = None,
    dc_warehouse_code: str | None = None,
    participant_warehouse_code: str | None = None,
) -> dict[str, int]:
    """Return {status: count} over exactly the same scope as list_orders.

    过滤下推到 SQL 的 GROUP BY，不把订单全表捞回来在应用层数——Issue #19 的
    合并订单页要在页签上显示各状态计数。参与仓口径走 EXISTS，多行同仓不翻倍。
    """
    master_conn.row_factory = sqlite3.Row
    where_parts: list[str] = []
    params: list[Any] = []
    alias = "store_orders"
    if store_warehouse_code:
        where_parts.append(f"{alias}.store_warehouse_code = ?")
        params.append(store_warehouse_code)
    if dc_warehouse_code:
        where_parts.append(f"{alias}.dc_warehouse_code = ?")
        params.append(dc_warehouse_code)
    if participant_warehouse_code:
        where_parts.append(
            f"({order_participation_sql(alias)} OR "
            f"({alias}.dc_warehouse_code = ? AND NOT EXISTS ("
            f"SELECT 1 FROM store_order_items legacy WHERE legacy.order_id = {alias}.id)))"
        )
        params.extend([participant_warehouse_code, participant_warehouse_code])
    where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    rows = master_conn.execute(
        f"SELECT status, COUNT(*) AS n FROM {alias} {where} GROUP BY status",
        params,
    ).fetchall()
    return {str(r["status"]): int(r["n"]) for r in rows}


def get_order_dc_code(
    master_conn: sqlite3.Connection,
    order_id: int,
) -> str | None:
    """Return the dc_warehouse_code for an order, or None if not found."""
    master_conn.row_factory = sqlite3.Row
    row = master_conn.execute(
        "SELECT dc_warehouse_code FROM store_orders WHERE id=?", (order_id,)
    ).fetchone()
    return str(row["dc_warehouse_code"]) if row else None


def get_order_detail(
    master_conn: sqlite3.Connection,
    order_id: int,
) -> dict[str, Any] | None:
    """Return order with items, status history and deliveries."""
    master_conn.row_factory = sqlite3.Row
    row = master_conn.execute(
        """SELECT so.*,
                  req.username AS requested_by_username,
                  appr.username AS approved_by_username,
                  ship.username AS shipped_by_username,
                  cancel.username AS cancelled_by_username,
                  ws.name AS store_warehouse_name,
                  wd.name AS dc_warehouse_name
           FROM store_orders so
           LEFT JOIN users req ON req.id = so.requested_by
           LEFT JOIN users appr ON appr.id = so.approved_by
           LEFT JOIN users ship ON ship.id = so.shipped_by
           LEFT JOIN users cancel ON cancel.id = so.cancelled_by
           LEFT JOIN warehouses ws ON ws.code = so.store_warehouse_code
           LEFT JOIN warehouses wd ON wd.code = so.dc_warehouse_code
           WHERE so.id=?""",
        (order_id,),
    ).fetchone()
    if row is None:
        return None
    order = dict(row)
    order["order_items"] = [
        dict(r) for r in master_conn.execute(
            """SELECT soi.*, ci.name AS canonical_name, ci.category_code
               FROM store_order_items soi
               JOIN canonical_items ci ON ci.id = soi.canonical_id
               WHERE soi.order_id=?
               ORDER BY soi.id""",
            (order_id,),
        ).fetchall()
    ]
    order["status_history"] = [
        dict(r) for r in master_conn.execute(
            """SELECT h.*, u.username AS actor_username
               FROM store_order_status_history h
               LEFT JOIN users u ON u.id = h.actor_id
               WHERE h.order_id=?
               ORDER BY h.created_at DESC, h.id DESC""",
            (order_id,),
        ).fetchall()
    ]
    order["deliveries"] = [
        dict(r) for r in master_conn.execute(
            """SELECT d.*, u.username AS shipped_by_username
               FROM store_order_deliveries d
               LEFT JOIN users u ON u.id = d.shipped_by
               WHERE d.order_id=?
               ORDER BY d.created_at DESC""",
            (order_id,),
        ).fetchall()
    ]
    order["receipts"] = [
        dict(r) for r in master_conn.execute(
            """SELECT r.*, u.username AS receiver_username,
                      ci.name AS canonical_name
               FROM store_order_receipts r
               LEFT JOIN users u ON u.id = r.received_by
               JOIN store_order_items soi ON soi.id = r.order_item_id
               JOIN canonical_items ci ON ci.id = soi.canonical_id
               WHERE r.order_id=?
               ORDER BY r.created_at DESC, r.id DESC""",
            (order_id,),
        ).fetchall()
    ]
    # 多仓履约（Task 8）：审批视角需要「整单全部行 + 每行来源仓标签」，
    # 发货视角由模板按来源仓只读/可操作。历史 NULL 行回落头仓。
    _attach_participant_warehouses(master_conn, [order])
    names = _warehouse_name_map(master_conn)
    for item in order["order_items"]:
        source = item.get("source_warehouse_code") or order["dc_warehouse_code"]
        item["effective_source_warehouse_code"] = source
        item["source_warehouse_name"] = names.get(str(source), source) if source else None
    return order


# ---------------------------------------------------------------------------
# Review / ship / deliver / cancel
# ---------------------------------------------------------------------------

def review_order(
    master_conn: sqlite3.Connection,
    order_id: int,
    decision: str,
    actor_id: int,
    note: str | None = None,
) -> dict[str, Any]:
    """Approve or reject an order.

    Multi-warehouse §8: the transition is conditional on the status read
    inside the locked transaction, so a concurrent cancel/ship that commits
    first makes ``changes()==0`` and this call fails instead of overwriting
    the newer state. One approval moves the whole order (D2/D5).
    """
    master_conn.row_factory = sqlite3.Row
    if decision not in (ORDER_STATUS_APPROVED, ORDER_STATUS_REJECTED):
        raise ValueError(f"decision must be approved/rejected, got {decision!r}")
    owns_tx = _begin_immediate(master_conn)
    try:
        order = get_order_detail(master_conn, order_id)
        if order is None:
            raise ValueError(f"order_id={order_id} not found")
        _assert_transition(order["status"], decision)
        ts = now()
        moved = master_conn.execute(
            """UPDATE store_orders
               SET status=?, approved_by=?, approved_at=?, approved_note=?, updated_at=?
               WHERE id=? AND status=?""",
            (decision, actor_id, ts, note, ts, order_id, order["status"]),
        )
        if moved.rowcount != 1:
            raise ValueError("订单状态已变更，请刷新后重试")
        _insert_status_history(master_conn, order_id, order["status"], decision, actor_id, note)
        master_conn.commit()
    except Exception:
        if owns_tx and master_conn.in_transaction:
            master_conn.rollback()
        raise
    return get_order_detail(master_conn, order_id)


def generate_op_no() -> str:
    """Return a fresh shipment operation key (uuid4 hex, never contains ``:``)."""
    return uuid.uuid4().hex


def _begin_immediate(conn: sqlite3.Connection) -> bool:
    """Start a write-locked transaction unless the caller already owns one.

    Returns True when this call started the transaction (and therefore owns
    the matching commit/rollback). Callers that already sit inside a
    transaction (e.g. legacy tests seeding rows on the same connection) keep
    their transaction; the protocol still runs inside it.
    """
    if conn.in_transaction:
        return False
    conn.execute("BEGIN IMMEDIATE")
    return True


def _line_op_no(op_no: str, order_item_id: int) -> str:
    """Derive the per-line idempotency key from the request-level ``op_no``.

    ``store_order_delivery_items.op_no`` is single-column UNIQUE (Task 1
    schema), so a multi-line batch cannot store the same request key on every
    row. The derived key is deterministic, which lets a retry (or orphan
    reconciliation) find exactly the same row. Requests are uuid4 hex, so the
    first ``:`` unambiguously separates the request key from the line id.
    """
    return f"{op_no}:{order_item_id}"


def _line_source_warehouse(order: dict[str, Any], item: dict[str, Any]) -> str:
    """Frozen line source; the header is only a legacy NULL fallback (spec §10)."""
    return item.get("source_warehouse_code") or order["dc_warehouse_code"]


def _select_ship_lines(
    order: dict[str, Any],
    line_qty: dict[int, float],
    operator_dc_code: str,
    op_no: str,
) -> list[tuple[int, dict[str, Any], float]]:
    """Validate every requested line before any stock is touched.

    Zero/blank quantities are no-ops (UI blank rows). Unknown line IDs and
    lines whose frozen source is not the operation warehouse are hard errors,
    so a forged foreign row can never deduct stock.
    """
    items_by_id: dict[int, dict[str, Any]] = {
        int(it["id"]): it for it in order["order_items"]
    }
    selected: list[tuple[int, dict[str, Any], float]] = []
    for raw_id, raw_qty in line_qty.items():
        try:
            order_item_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        item = items_by_id.get(order_item_id)
        if item is None:
            raise ValueError(
                f"order_item_id={order_item_id} 不属于 order_id={order['id']}"
            )
        try:
            qty = parse_qty(raw_qty)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        source = _line_source_warehouse(order, item)
        if source != operator_dc_code:
            raise ValueError(
                f"order_item_id={order_item_id} 不属于操作仓 {operator_dc_code}"
                f"（行来源 {source}）"
            )
        already = parse_qty(item.get("shipped_quantity") or 0)
        target = parse_qty(item["quantity"])
        # A retry of this very operation already carried the quantity into
        # shipped_quantity; the idempotency lookup returns the existing batch
        # instead of applying it again.
        if item.get("ship_op_no") != op_no and already + qty > target + 1e-9:
            raise ValueError(
                f"累计发货 {already + qty:g} 超过订单数 {target:g}"
                f"（order_item_id={order_item_id}）"
            )
        selected.append((order_item_id, item, qty))
    if not selected:
        raise ValueError("本批发货数量全部为空，请至少填写一项")
    return selected


def _find_existing_batch(
    master_conn: sqlite3.Connection,
    order_id: int,
    line_keys: dict[int, str],
) -> int | None:
    """Return the delivery id already written for this request's keys.

    Raises when the keys resolve to a different order or a different batch;
    silently reusing them would corrupt an unrelated order.
    """
    placeholders = ",".join("?" for _ in line_keys)
    rows = master_conn.execute(
        f"""SELECT di.op_no, di.delivery_id, d.order_id
              FROM store_order_delivery_items di
              JOIN store_order_deliveries d ON d.id = di.delivery_id
             WHERE di.op_no IN ({placeholders})""",
        tuple(line_keys.values()),
    ).fetchall()
    if not rows:
        return None
    if any(int(row["order_id"]) != int(order_id) for row in rows):
        raise ValueError("op_no 已属于其他订单，拒绝复用")
    delivery_ids = {int(row["delivery_id"]) for row in rows}
    if len(delivery_ids) != 1 or set(line_keys.values()) != {
        row["op_no"] for row in rows
    }:
        raise ValueError("op_no 已用于不同批次，拒绝复用")
    return next(iter(delivery_ids))


def _ship_result(
    master_conn: sqlite3.Connection,
    order_id: int,
    *,
    delivery_id: int,
    op_no: str,
    warehouse_code: str,
    selected_ids: list[int],
    transitioned_to_shipped: bool = False,
    idempotent: bool = False,
) -> dict[str, Any]:
    """Refresh the order and attach the protocol metadata callers rely on."""
    order = get_order_detail(master_conn, order_id) or {}
    items = {int(it["id"]): it for it in order.get("order_items", [])}
    is_fully_shipped = bool(items) and all(
        parse_qty(it["shipped_quantity"]) >= parse_qty(it["quantity"]) - 1e-9
        for it in items.values()
    )
    order.update(
        {
            "shipped_items": [
                {
                    "order_item_id": oid,
                    "shipped_quantity": parse_qty(
                        items[oid]["shipped_quantity"]
                    ),
                }
                for oid in selected_ids
                if oid in items
            ],
            "delivery_id": delivery_id,
            "op_no": op_no,
            "warehouse_code": warehouse_code,
            "is_fully_shipped": is_fully_shipped,
            "transitioned_to_shipped": transitioned_to_shipped,
            "idempotent": idempotent,
            "new_order_status": order.get("status"),
        }
    )
    return order


def _apply_warehouse_shipment(
    master_conn: sqlite3.Connection,
    wh_conn_factory,
    operator_dc_code: str,
    order: dict[str, Any],
    selected: list[tuple[int, dict[str, Any], float]],
    line_keys: dict[int, str],
    ship_action: str,
    ts: str,
) -> dict[int, int]:
    """Conditionally decrement local stock and write one ``op_no`` movement.

    The whole batch runs under ``BEGIN IMMEDIATE`` so two orders racing the
    last unit are serialized: the loser's ``UPDATE … WHERE quantity >= ?``
    changes zero rows and the batch is rejected without any partial write.
    A line whose movement already carries this request's key is skipped, so a
    retry after a master failure cannot deduct twice.
    """
    from blueprints.fulfillment_pure import resolve_fulfillment_item

    wh_conn = wh_conn_factory(operator_dc_code)
    item_ids: dict[int, int] = {}
    try:
        wh_conn.row_factory = sqlite3.Row
        wh_conn.execute("BEGIN IMMEDIATE")
        for order_item_id, item, qty in selected:
            resolved = resolve_fulfillment_item(
                master_conn,
                wh_conn,
                canonical_id=int(item["canonical_id"]),
                warehouse_code=operator_dc_code,
            )
            item_id = int(resolved["item_id"])
            item_ids[order_item_id] = item_id
            applied = wh_conn.execute(
                "SELECT 1 FROM stock_movements WHERE op_no=? LIMIT 1",
                (line_keys[order_item_id],),
            ).fetchone()
            if applied is not None:
                continue
            updated = wh_conn.execute(
                """UPDATE items SET quantity = quantity - ?
                    WHERE id = ? AND quantity >= ?""",
                (qty, item_id, qty),
            )
            if updated.rowcount != 1:
                row = wh_conn.execute(
                    "SELECT quantity FROM items WHERE id=?", (item_id,)
                ).fetchone()
                available = parse_qty(row["quantity"]) if row else 0.0
                raise ValueError(
                    f"库存不足：{item.get('canonical_name')} 本批发货 {qty:g}，"
                    f"配送中心现有库存 {available:g}"
                )
            wh_conn.execute(
                """INSERT INTO stock_movements
                   (item_id, action, delta, note, created_at, op_no)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    item_id,
                    ship_action,
                    -qty,
                    f"{ship_action} #{order['order_no']} partial {qty:g}",
                    ts,
                    line_keys[order_item_id],
                ),
            )
        wh_conn.commit()
    except Exception:
        try:
            wh_conn.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        wh_conn.close()
    return item_ids


def _insert_master_batch(
    master_conn: sqlite3.Connection,
    order_id: int,
    operator_dc_code: str,
    selected: list[tuple[int, dict[str, Any], float]],
    line_keys: dict[int, str],
    item_ids: dict[int, int],
    op_no: str,
    shipped_by: int,
    tracking_note: str | None,
    ts: str,
) -> int:
    """Insert the batch header + line rows and accumulate line quantities."""
    delivery_no = _generate_delivery_no(master_conn)
    cur = master_conn.execute(
        """INSERT INTO store_order_deliveries
           (order_id, delivery_no, warehouse_code, shipped_by, shipped_at,
            tracking_note, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (order_id, delivery_no, operator_dc_code, shipped_by, ts, tracking_note, ts),
    )
    delivery_id = int(cur.lastrowid)
    for order_item_id, _item, qty in selected:
        master_conn.execute(
            """INSERT INTO store_order_delivery_items
               (delivery_id, order_item_id, warehouse_code, item_id, quantity,
                op_no, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                delivery_id,
                order_item_id,
                operator_dc_code,
                item_ids[order_item_id],
                qty,
                line_keys[order_item_id],
                ts,
            ),
        )
        updated = master_conn.execute(
            """UPDATE store_order_items
                  SET shipped_quantity = shipped_quantity + ?,
                      ship_op_no = ?,
                      dc_item_id = ?,
                      status = CASE
                          WHEN shipped_quantity + ? >= quantity - 0.000000001
                              THEN 'fulfilled'
                          WHEN shipped_quantity + ? > 0 THEN 'partial'
                          ELSE status END
                WHERE id = ?
                  AND shipped_quantity + ? <= quantity + 0.000000001
                  AND (ship_op_no IS NULL OR ship_op_no <> ?)""",
            (
                qty,
                op_no,
                item_ids[order_item_id],
                qty,
                qty,
                order_item_id,
                qty,
                op_no,
            ),
        )
        if updated.rowcount != 1:
            raise ValueError(
                f"order_item_id={order_item_id} 行累计超发或本批已应用"
            )
    return delivery_id


def _finalise_order_state(
    master_conn: sqlite3.Connection,
    order_id: int,
    shipped_by: int,
    tracking_note: str | None,
    selected: list[tuple[int, dict[str, Any], float]],
    ts: str,
) -> bool:
    """Re-aggregate every line inside the master lock and transition once.

    Returns True only for the call whose conditional
    ``UPDATE … WHERE status='approved'`` actually moved the order, so the
    route emits exactly one ``store_order_shipped`` notification even when two
    warehouses ship their last lines back to back.
    """
    rows = master_conn.execute(
        """SELECT id, quantity, shipped_quantity
             FROM store_order_items WHERE order_id=?""",
        (order_id,),
    ).fetchall()
    is_fully_shipped = bool(rows) and all(
        parse_qty(row["shipped_quantity"]) >= parse_qty(row["quantity"]) - 1e-9
        for row in rows
    )
    if not is_fully_shipped:
        _insert_status_history(
            master_conn,
            order_id,
            ORDER_STATUS_APPROVED,
            ORDER_STATUS_APPROVED,
            shipped_by,
            f"本批发货 {sum(qty for _, _, qty in selected):g} 件",
        )
        return False
    moved = master_conn.execute(
        """UPDATE store_orders
              SET status=?, shipped_by=?, shipped_at=?, updated_at=?
            WHERE id=? AND status=?""",
        (ORDER_STATUS_SHIPPED, shipped_by, ts, ts, order_id, ORDER_STATUS_APPROVED),
    )
    if moved.rowcount != 1:
        return False
    _insert_status_history(
        master_conn,
        order_id,
        ORDER_STATUS_APPROVED,
        ORDER_STATUS_SHIPPED,
        shipped_by,
        tracking_note,
    )
    return True


def ship_partial(
    master_conn: sqlite3.Connection,
    wh_conn_factory,
    *,
    order_id: int,
    operator_dc_code: str,
    op_no: str,
    line_qty: dict[int, float],
    shipped_by: int,
    tracking_note: str | None = None,
) -> dict[str, Any]:
    """Ship one source warehouse's lines under a caller-supplied idempotency key.

    Protocol (spec v2 §7/§8):
      1. Validate every line's frozen source equals ``operator_dc_code``.
      2. Warehouse db ``BEGIN IMMEDIATE``: conditional atomic decrement
         (``WHERE quantity >= ?``) + ``stock_movements(op_no)``; a line already
         carrying this request's derived key is skipped (retry-safe).
      3. Master db ``BEGIN IMMEDIATE``: insert batch header + line rows
         (``store_order_delivery_items.op_no`` UNIQUE → same request returns
         the existing batch), accumulate ``shipped_quantity`` with a guard,
         re-read **all** lines inside the lock and conditionally migrate
         ``approved → shipped`` exactly once.
    """
    if not op_no:
        raise ValueError("op_no is required")
    master_conn.row_factory = sqlite3.Row
    order = get_order_detail(master_conn, order_id)
    if order is None:
        raise ValueError(f"order_id={order_id} not found")
    selected = _select_ship_lines(order, line_qty, operator_dc_code, op_no)
    selected_ids = [order_item_id for order_item_id, _, _ in selected]
    line_keys = {oid: _line_op_no(op_no, oid) for oid in selected_ids}

    existing = _find_existing_batch(master_conn, order_id, line_keys)
    if existing is not None:
        return _ship_result(
            master_conn,
            order_id,
            delivery_id=existing,
            op_no=op_no,
            warehouse_code=operator_dc_code,
            selected_ids=selected_ids,
            idempotent=True,
        )

    if order["status"] != ORDER_STATUS_APPROVED:
        raise ValueError(
            f"order must be approved to ship, got {order['status']!r}"
        )

    ts = now()
    item_ids = _apply_warehouse_shipment(
        master_conn,
        wh_conn_factory,
        operator_dc_code,
        order,
        selected,
        line_keys,
        shipment_action_for(order),
        ts,
    )

    try:
        master_conn.execute("BEGIN IMMEDIATE")
        current = master_conn.execute(
            "SELECT status FROM store_orders WHERE id=?", (order_id,)
        ).fetchone()
        if current is None:
            raise ValueError(f"order_id={order_id} not found")
        existing = _find_existing_batch(master_conn, order_id, line_keys)
        if existing is not None:
            master_conn.rollback()
            return _ship_result(
                master_conn,
                order_id,
                delivery_id=existing,
                op_no=op_no,
                warehouse_code=operator_dc_code,
                selected_ids=selected_ids,
                idempotent=True,
            )
        if current["status"] != ORDER_STATUS_APPROVED:
            raise ValueError(
                f"order must be approved to ship, got {current['status']!r}"
            )
        try:
            delivery_id = _insert_master_batch(
                master_conn,
                order_id,
                operator_dc_code,
                selected,
                line_keys,
                item_ids,
                op_no,
                shipped_by,
                tracking_note,
                ts,
            )
        except sqlite3.IntegrityError:
            master_conn.rollback()
            existing = _find_existing_batch(master_conn, order_id, line_keys)
            if existing is not None:
                return _ship_result(
                    master_conn,
                    order_id,
                    delivery_id=existing,
                    op_no=op_no,
                    warehouse_code=operator_dc_code,
                    selected_ids=selected_ids,
                    idempotent=True,
                )
            raise
        transitioned = _finalise_order_state(
            master_conn, order_id, shipped_by, tracking_note, selected, ts
        )
        master_conn.commit()
    except Exception:
        if master_conn.in_transaction:
            master_conn.rollback()
        raise

    return _ship_result(
        master_conn,
        order_id,
        delivery_id=delivery_id,
        op_no=op_no,
        warehouse_code=operator_dc_code,
        selected_ids=selected_ids,
        transitioned_to_shipped=transitioned,
    )


REVERSAL_OP_NO_PREFIX: str = "REV:"


def reconcile_orphan_shipments(
    master_conn: sqlite3.Connection,
    wh_conn_factory,
) -> list[dict[str, Any]]:
    """Reverse warehouse stock movements that have no master batch row.

    A crash between the warehouse commit and the master batch INSERT leaves
    stock already deducted but no delivery record. This job adds the quantity
    back with a red reversal movement keyed ``REV:<line_op_no>`` and returns
    the affected rows as the alert payload. Re-running is a no-op because the
    reversal key is checked first.
    """
    master_conn.row_factory = sqlite3.Row
    warehouses = master_conn.execute(
        "SELECT code FROM warehouses WHERE warehouse_type=? ORDER BY code",
        (WAREHOUSE_TYPE_DC,),
    ).fetchall()
    reconciled: list[dict[str, Any]] = []
    for warehouse in warehouses:
        code = warehouse["code"]
        wh_conn = wh_conn_factory(code)
        try:
            wh_conn.row_factory = sqlite3.Row
            columns = {
                row[1] for row in wh_conn.execute("PRAGMA table_info(stock_movements)")
            }
            if "op_no" not in columns:
                continue
            wh_conn.execute("BEGIN IMMEDIATE")
            rows = wh_conn.execute(
                """SELECT id, item_id, action, delta, op_no
                     FROM stock_movements
                    WHERE op_no IS NOT NULL AND delta < 0
                    ORDER BY id"""
            ).fetchall()
            for row in rows:
                op_no = str(row["op_no"])
                if op_no.startswith(REVERSAL_OP_NO_PREFIX):
                    continue
                delivered = master_conn.execute(
                    "SELECT 1 FROM store_order_delivery_items WHERE op_no=? LIMIT 1",
                    (op_no,),
                ).fetchone()
                if delivered is not None:
                    continue
                reversal_key = REVERSAL_OP_NO_PREFIX + op_no
                if wh_conn.execute(
                    "SELECT 1 FROM stock_movements WHERE op_no=? LIMIT 1",
                    (reversal_key,),
                ).fetchone() is not None:
                    continue
                quantity = parse_qty(-parse_qty(row["delta"]))
                if quantity <= 0:
                    continue
                wh_conn.execute(
                    "UPDATE items SET quantity = quantity + ? WHERE id=?",
                    (quantity, row["item_id"]),
                )
                wh_conn.execute(
                    """INSERT INTO stock_movements
                       (item_id, action, delta, note, created_at, op_no)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        row["item_id"],
                        row["action"],
                        quantity,
                        f"红字冲销 孤儿发货 op_no={op_no}",
                        now(),
                        reversal_key,
                    ),
                )
                reconciled.append(
                    {
                        "warehouse_code": code,
                        "movement_id": int(row["id"]),
                        "item_id": int(row["item_id"]),
                        "quantity": quantity,
                        "op_no": op_no,
                    }
                )
            wh_conn.commit()
        except Exception:
            try:
                wh_conn.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            wh_conn.close()
    return reconciled


def ship_order(
    master_conn: sqlite3.Connection,
    order_id: int,
    third: int | dict[int, float],
    shipped_by: int | None = None,
    tracking_note: str | None = None,
    *,
    op_no: str | None = None,
) -> dict[str, Any]:
    """Ship an approved order (v3 partial-ship entry-point).

    Two calling conventions are supported so the v1/v2 full-ship API
    remains callable by existing routes and tests:

    1. **Partial ship (v3, preferred):**
       ``ship_order(master, order_id, shipped_items_map: dict[int, float],
       shipped_by: int, tracking_note=None)``
       where ``shipped_items_map`` maps ``store_order_items.id`` to the
       quantity being shipped this batch. The operation warehouse is derived
       from the selected lines' frozen sources (all must agree); HTTP callers
       should pass ``operator_dc_code`` through ``ship_partial`` instead.

    2. **Legacy full ship (v1/v2):**
       ``ship_order(master, order_id, shipped_by: int, tracking_note=None)``
       ships every remaining quantity in a single batch. Equivalent to
       calling form 1 with a map of ``{order_item_id: quantity - shipped_quantity}``.

    Dispatch is determined by ``third``: ``dict`` → form 1, ``int`` → form 2.
    """
    if isinstance(third, dict):
        if shipped_by is None:
            raise ValueError("ship_order: shipped_by is required when shipped_items_map is passed")
        return _ship_order_partial_impl(
            master_conn, order_id, third, shipped_by, tracking_note, op_no=op_no,
        )
    if isinstance(third, int):
        # Legacy: ``third`` IS ``shipped_by``; ``tracking_note`` follows positionally.
        return _ship_order_full_impl(
            master_conn, order_id, third, tracking_note, op_no=op_no,
        )
    raise TypeError(
        "ship_order: third arg must be dict[int, float] (v3 partial) or "
        f"int (legacy shipped_by), got {type(third).__name__}"
    )


def ship_order_full(
    master_conn: sqlite3.Connection,
    order_id: int,
    shipped_by: int,
    tracking_note: str | None = None,
    *,
    operator_dc_code: str | None = None,
    op_no: str | None = None,
) -> dict[str, Any]:
    """Legacy-friendly helper: ship all remaining quantity in a single batch.

    ``operator_dc_code`` scopes the batch to one warehouse's outstanding
    lines; without it the order must be single-warehouse (legacy tests).
    """
    return _ship_order_full_impl(
        master_conn,
        order_id,
        shipped_by,
        tracking_note,
        operator_dc_code=operator_dc_code,
        op_no=op_no,
    )


def _ship_order_full_impl(
    master_conn: sqlite3.Connection,
    order_id: int,
    shipped_by: int,
    tracking_note: str | None,
    *,
    operator_dc_code: str | None = None,
    op_no: str | None = None,
) -> dict[str, Any]:
    """Legacy one-shot: build a full-batch map and run partial-ship on it."""
    master_conn.row_factory = sqlite3.Row
    order = get_order_detail(master_conn, order_id)
    if order is None:
        raise ValueError(f"order_id={order_id} not found")
    if order["status"] != ORDER_STATUS_APPROVED:
        raise ValueError(
            f"order must be approved to ship, got {order['status']!r}"
        )
    items_map: dict[int, float] = {}
    sources: set[str] = set()
    for item in order["order_items"]:
        source = _line_source_warehouse(order, item)
        if operator_dc_code is not None and source != operator_dc_code:
            continue
        already = parse_qty(item.get("shipped_quantity") or 0)
        remaining = parse_qty(item["quantity"]) - already
        if remaining > 0:
            items_map[int(item["id"])] = remaining
            sources.add(source)
    if not items_map:
        if operator_dc_code is not None:
            raise ValueError(f"操作仓 {operator_dc_code} 无可发货明细")
        raise ValueError("订单已全部发齐，无需再次发货")
    if operator_dc_code is None:
        if len(sources) != 1:
            raise ValueError("订单跨仓，必须显式指定操作仓")
        operator_dc_code = next(iter(sources))
    return ship_partial(
        master_conn,
        open_warehouse_db,
        order_id=order_id,
        operator_dc_code=operator_dc_code,
        op_no=op_no or generate_op_no(),
        line_qty=items_map,
        shipped_by=shipped_by,
        tracking_note=tracking_note,
    )


def _ship_order_partial_impl(
    master_conn: sqlite3.Connection,
    order_id: int,
    shipped_items_map: dict[int, float],
    shipped_by: int,
    tracking_note: str | None,
    *,
    op_no: str | None = None,
) -> dict[str, Any]:
    """Backwards-compatible wrapper around the cross-warehouse protocol.

    Resolves the operation warehouse from the selected lines' frozen sources;
    HTTP callers pass an explicit session warehouse to ``ship_partial``.
    """
    master_conn.row_factory = sqlite3.Row
    order = get_order_detail(master_conn, order_id)
    if order is None:
        raise ValueError(f"order_id={order_id} not found")
    items_by_id = {int(it["id"]): it for it in order["order_items"]}
    sources: set[str] = set()
    for raw_id, raw_qty in shipped_items_map.items():
        try:
            qty = parse_qty(raw_qty)
        except (TypeError, ValueError):
            continue
        try:
            item = items_by_id.get(int(raw_id))
        except (TypeError, ValueError):
            item = None
        if item is None or qty <= 0:
            continue
        sources.add(_line_source_warehouse(order, item))
    if not sources:
        operator_dc_code = order["dc_warehouse_code"]
    elif len(sources) == 1:
        operator_dc_code = next(iter(sources))
    else:
        raise ValueError("订单跨仓，必须显式指定操作仓")
    return ship_partial(
        master_conn,
        open_warehouse_db,
        order_id=order_id,
        operator_dc_code=operator_dc_code,
        op_no=op_no or generate_op_no(),
        line_qty=shipped_items_map,
        shipped_by=shipped_by,
        tracking_note=tracking_note,
    )


# Order statuses in which the storefront is allowed to receive goods.
# v3 keeps an order in ``approved`` while the DC ships it in batches and only
# flips it to ``shipped`` once every line is fully shipped — so "may I receive?"
# cannot be keyed off the order status alone, or partial shipments stall.
# See ``order_item_receivable_qty`` for the per-line rule.
RECEIVABLE_ORDER_STATUSES: tuple[str, ...] = (
    ORDER_STATUS_APPROVED,
    ORDER_STATUS_SHIPPED,
)


def order_item_receivable_qty(
    order_status: str, item: dict[str, Any]
) -> float:
    """Return how much of ``item`` the storefront may still receive.

    Derives the amount from the line itself (what the DC actually shipped vs
    what the store already received) rather than from the order's coarse main
    status — a partially-shipped order stays ``approved`` and never reaches
    ``shipped`` until every line is complete, so a status-only check would make
    partial shipments unreceivable (2026-10-10 fix).

    - ``shipped`` orders whose ``shipped_quantity`` was never recorded (v2
      legacy rows, written by the old one-shot path) are treated as fully
      shipped → fall back to ``quantity``.
    - ``approved`` orders expose only what has genuinely been shipped so far,
      so an unshipped line yields ``0``.

    Returns a non-negative float; ``0`` means "nothing to receive here".
    """
    ordered = parse_qty(item["quantity"])
    fulfilled = parse_qty(item.get("fulfilled_quantity") or 0)
    shipped = parse_qty(item.get("shipped_quantity") or 0)
    if shipped <= 0 and order_status == ORDER_STATUS_SHIPPED:
        shipped = ordered
    return max(0.0, min(shipped, ordered) - fulfilled)


def mark_order_delivered(
    master_conn: sqlite3.Connection,
    order_id: int,
    actor_id: int,
) -> dict[str, Any]:
    """Quick-path: mark the whole order delivered in a single action.

    The canonical path for receiving partial / full inventory is
    ``receive_order_item`` (one call per order_item). This entry-point is
    preserved as a "deliver everything already shipped" shortcut for the
    storefront manager UI: it forwards by issuing one ``receive_order_item``
    call per order_item to consume whatever the DC has actually shipped, then
    returns the post-delivery order detail.

    Works for both ``approved`` (DC shipped only part of the order) and
    ``shipped`` (fully shipped) orders; lines with nothing shipped yet are
    skipped, so a partially-shipped order stays ``approved`` afterwards.
    """
    master_conn.row_factory = sqlite3.Row
    order = get_order_detail(master_conn, order_id)
    if order is None:
        raise ValueError(f"order_id={order_id} not found")
    if order["status"] not in RECEIVABLE_ORDER_STATUSES:
        raise ValueError(
            f"order must be approved or shipped to deliver, got {order['status']}"
        )
    for item in order["order_items"]:
        # 只收「已发未收」的部分；未发货的行 pending=0 自动跳过。
        pending = order_item_receivable_qty(order["status"], item)
        if pending <= 0:
            continue
        receive_order_item(
            master_conn, order_id=order_id, order_item_id=item["id"],
            qty=pending, actor_id=actor_id, note="legacy auto-receive",
        )
    return get_order_detail(master_conn, order_id)


def _find_existing_receipts(
    master_conn: sqlite3.Connection,
    order_id: int,
    line_keys: dict[int, str],
) -> dict[str, sqlite3.Row]:
    """Return receipts already written for this request's derived keys.

    Raises when a key belongs to another order; silently reusing it would
    corrupt unrelated receiving history.
    """
    if not line_keys:
        return {}
    placeholders = ",".join("?" for _ in line_keys)
    rows = master_conn.execute(
        f"SELECT * FROM store_order_receipts WHERE op_no IN ({placeholders})",
        tuple(line_keys.values()),
    ).fetchall()
    found: dict[str, sqlite3.Row] = {}
    for row in rows:
        if int(row["order_id"]) != int(order_id):
            raise ValueError("op_no 已属于其他订单，拒绝复用")
        found[str(row["op_no"])] = row
    return found


def _ensure_store_item(
    store_conn: sqlite3.Connection,
    master_conn: sqlite3.Connection,
    canonical_id: int,
    ts: str,
) -> sqlite3.Row:
    """Return the storefront ``items`` row, auto-creating it when unbound (§3.1)."""
    store_item = store_conn.execute(
        "SELECT * FROM items WHERE canonical_id=?",
        (canonical_id,),
    ).fetchone()
    if store_item is not None:
        return store_item
    canon = master_conn.execute(
        "SELECT name, category_code, unit FROM canonical_items WHERE id=?",
        (canonical_id,),
    ).fetchone()
    if canon is None:
        raise ValueError(f"canonical_id={canonical_id} not found")
    # Auto-create: pick category by canonical_category_code → categories.name.
    cat_id: int | None = None
    if canon["category_code"]:
        row = store_conn.execute(
            "SELECT id FROM categories WHERE canonical_code=?",
            (canon["category_code"],),
        ).fetchone()
        if row is not None:
            cat_id = int(row["id"])
        else:
            cc = master_conn.execute(
                "SELECT name FROM canonical_categories WHERE code=?",
                (canon["category_code"],),
            ).fetchone()
            cn = cc["name"] if cc else None
            if cn:
                row = store_conn.execute(
                    "SELECT id FROM categories WHERE name=?",
                    (cn,),
                ).fetchone()
                if row is not None:
                    cat_id = int(row["id"])
    if cat_id is None:
        row = store_conn.execute(
            "SELECT id FROM categories ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            raise ValueError(
                f"门店仓尚未初始化品类，无法为 canonical_id={canonical_id} 自动创建 items"
            )
        cat_id = int(row["id"])
    cur = store_conn.execute(
        """INSERT INTO items
           (sku, name, category_id, quantity, safety_stock,
            unit, unit_cost, gram_per_unit, aux_unit, aux_rate,
            canonical_id, updated_at)
           VALUES (?, ?, ?, 0, 0, ?, 0, 0, NULL, 0, ?, ?)""",
        (
            f"AUTO-RECEIVE-{canonical_id}",
            str(canon["name"]),
            cat_id,
            str(canon["unit"] or "件"),
            canonical_id,
            ts,
        ),
    )
    return store_conn.execute(
        "SELECT * FROM items WHERE id=?", (int(cur.lastrowid),)
    ).fetchone()


def _apply_store_receipts(
    master_conn: sqlite3.Connection,
    store_conn_factory,
    store_code: str,
    order: dict[str, Any],
    pending: list[dict[str, Any]],
    ts: str,
) -> dict[int, int]:
    """Mirror the receipt into the storefront db under ``BEGIN IMMEDIATE``.

    Each line's derived ``op_no`` is written to ``stock_movements`` so a replay
    after a crash skips the add (the movement already exists). Returns the
    storefront ``items.id`` per order_item so master can backfill
    ``store_item_id``.
    """
    store_conn = store_conn_factory(store_code)
    item_ids: dict[int, int] = {}
    try:
        store_conn.row_factory = sqlite3.Row
        store_conn.execute("BEGIN IMMEDIATE")
        recv_action = receipt_action_for(order)
        for entry in pending:
            order_item_id = int(entry["order_item_id"])
            canonical_id = int(entry["canonical_id"])
            qty = parse_qty(entry["quantity"])
            line_op_no = str(entry["op_no"])
            existing = store_conn.execute(
                "SELECT item_id FROM stock_movements WHERE op_no=? LIMIT 1",
                (line_op_no,),
            ).fetchone()
            if existing is not None:
                item_ids[order_item_id] = int(existing["item_id"])
                continue
            store_item = _ensure_store_item(
                store_conn, master_conn, canonical_id, ts
            )
            item_id = int(store_item["id"])
            store_conn.execute(
                "UPDATE items SET quantity = quantity + ?, updated_at=? WHERE id=?",
                (qty, ts, item_id),
            )
            store_conn.execute(
                """INSERT INTO stock_movements
                   (item_id, action, delta, note, created_at, op_no)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    item_id,
                    recv_action,
                    qty,
                    f"{recv_action} #{order['order_no']}",
                    ts,
                    line_op_no,
                ),
            )
            item_ids[order_item_id] = item_id
        store_conn.commit()
    except Exception:
        try:
            store_conn.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        store_conn.close()
    return item_ids


def _finalise_receipt_state(
    master_conn: sqlite3.Connection,
    order_id: int,
    actor_id: int,
    note: str | None,
    ts: str,
) -> bool:
    """Re-read every line under the master lock; transition to delivered once.

    Returns True only for the call whose conditional
    ``UPDATE … WHERE status='shipped'`` actually moved the order, so the route
    emits exactly one ``store_order_delivered`` notification.
    """
    rows = master_conn.execute(
        """SELECT id, quantity, fulfilled_quantity
             FROM store_order_items WHERE order_id=?""",
        (order_id,),
    ).fetchall()
    all_done = bool(rows) and all(
        parse_qty(row["fulfilled_quantity"]) >= parse_qty(row["quantity"]) - 1e-9
        for row in rows
    )
    if not all_done:
        return False
    moved = master_conn.execute(
        """UPDATE store_orders
              SET status=?, delivered_at=?, updated_at=?
            WHERE id=? AND status=?""",
        (
            ORDER_STATUS_DELIVERED,
            ts,
            ts,
            order_id,
            ORDER_STATUS_SHIPPED,
        ),
    )
    if moved.rowcount != 1:
        return False
    master_conn.execute(
        "UPDATE store_order_deliveries SET delivered_at=? WHERE order_id=?",
        (ts, order_id),
    )
    _insert_status_history(
        master_conn,
        order_id,
        ORDER_STATUS_SHIPPED,
        ORDER_STATUS_DELIVERED,
        actor_id,
        note or "门店收货自动完成",
    )
    return True


def receive_partial(
    master_conn: sqlite3.Connection,
    store_conn_factory,
    *,
    order_id: int,
    op_no: str,
    item_qty: dict[int, float],
    received_by: int,
    note: str | None = None,
) -> dict[str, Any]:
    """Receive one or more lines under a request-level idempotency key.

    Mirror protocol (spec v2 §7):
      1. Master ``BEGIN IMMEDIATE``: insert the receipt rows (``op_no`` =
         ``f'{op_no}:{order_item_id}'``, ``applied=0``) and conditionally
         accumulate ``fulfilled_quantity + ? <=`` this line's receivable
         ceiling (shipped, or ordered for identifiable v2 legacy rows). The
         whole batch rolls back if any line exceeds its ceiling.
      2. Storefront db ``BEGIN IMMEDIATE``: conditional-increment each
         ``items.quantity`` and write one ``stock_movements(op_no)``; a line
         whose movement already carries the key is skipped (retry-safe).
      3. Master ``BEGIN IMMEDIATE``: flip ``applied=1`` and, when every line is
         fulfilled, conditionally migrate ``shipped → delivered`` exactly once.

    A crash between 2 and 3 leaves ``applied=0``; replaying with the same
    ``op_no`` (or ``reconcile_orphan_receipts``) completes the mirror without
    double-adding stock or double-accumulating ``fulfilled_quantity``.
    """
    if not op_no:
        raise ValueError("op_no is required")
    master_conn.row_factory = sqlite3.Row
    order = get_order_detail(master_conn, order_id)
    if order is None:
        raise ValueError(f"order_id={order_id} not found")
    items_by_id = {int(it["id"]): it for it in order["order_items"]}
    selected: list[tuple[int, dict[str, Any], float]] = []
    for raw_id, raw_qty in item_qty.items():
        try:
            order_item_id = int(raw_id)
        except (TypeError, ValueError):
            raise ValueError(f"order_item_id={raw_id!r} 无效") from None
        item = items_by_id.get(order_item_id)
        if item is None:
            raise ValueError(
                f"order_item_id={order_item_id} 不属于 order_id={order_id}"
            )
        qty = parse_qty(raw_qty)
        if qty <= 0:
            raise ValueError("收货数量必须大于 0")
        selected.append((order_item_id, item, qty))
    if not selected:
        raise ValueError("请至少填写一项收货数量")

    line_keys = {oid: _line_op_no(op_no, oid) for oid, _, _ in selected}
    store_code = order["store_warehouse_code"]
    ts = now()
    pending_store: list[dict[str, Any]] = []
    needs_work = False

    owns_tx = _begin_immediate(master_conn)
    try:
        row = master_conn.execute(
            "SELECT status FROM store_orders WHERE id=?", (order_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"order_id={order_id} not found")
        status = str(row["status"])
        existing = _find_existing_receipts(master_conn, order_id, line_keys)
        if status not in RECEIVABLE_ORDER_STATUSES:            # A retry of the receipt that already completed the order must stay
            # idempotent even though ``delivered`` is no longer receivable.
            for order_item_id, _item, qty in selected:
                receipt = existing.get(line_keys[order_item_id])
                if receipt is None or int(receipt["applied"]) == 0:
                    raise ValueError(
                        f"order must be approved or shipped to receive, got {status!r}"
                    )
                if int(receipt["order_item_id"]) != order_item_id:
                    raise ValueError(
                        f"op_no={line_keys[order_item_id]} 已用于其他明细，拒绝复用"
                    )
                if abs(parse_qty(receipt["quantity"]) - qty) > 1e-9:
                    raise ValueError(
                        f"op_no 已用于不同数量的收货（order_item_id={order_item_id}）"
                    )
        else:
            for order_item_id, item, qty in selected:
                key = line_keys[order_item_id]
                receipt = existing.get(key)
                if receipt is not None:
                    if int(receipt["order_item_id"]) != order_item_id:
                        raise ValueError(
                            f"op_no={key} 已用于其他明细，拒绝复用"
                        )
                    if abs(parse_qty(receipt["quantity"]) - qty) > 1e-9:
                        raise ValueError(
                            f"op_no 已用于不同数量的收货（order_item_id={order_item_id}）"
                        )
                    if int(receipt["applied"]) == 0:
                        needs_work = True
                        pending_store.append(
                            {
                                "order_item_id": order_item_id,
                                "op_no": key,
                                "quantity": parse_qty(receipt["quantity"]),
                                "canonical_id": int(item["canonical_id"]),
                            }
                        )
                    continue
                needs_work = True
                line = master_conn.execute(
                    """SELECT id, quantity, shipped_quantity, fulfilled_quantity
                         FROM store_order_items WHERE id=?""",
                    (order_item_id,),
                ).fetchone()
                if line is None:
                    raise ValueError(f"order_item_id={order_item_id} not found")
                remaining = order_item_receivable_qty(status, dict(line))
                if qty > remaining + 1e-9:
                    raise ValueError(f"收货数量 {qty:g} 超过待收 {remaining:g}")
                ceiling = parse_qty(line["fulfilled_quantity"]) + remaining
                updated = master_conn.execute(
                    """UPDATE store_order_items
                          SET fulfilled_quantity = fulfilled_quantity + ?,
                              status = CASE
                                  WHEN fulfilled_quantity + ? >= quantity - 0.000000001
                                      THEN 'fulfilled'
                                  WHEN fulfilled_quantity + ? > 0 THEN 'partial'
                                  ELSE status END
                        WHERE id = ?
                          AND fulfilled_quantity + ? <= ? + 0.000000001""",
                    (qty, qty, qty, order_item_id, qty, ceiling),
                )
                if updated.rowcount != 1:
                    raise ValueError(f"收货数量 {qty:g} 超过待收 {remaining:g}")
                master_conn.execute(
                    """INSERT INTO store_order_receipts
                       (order_id, order_item_id, quantity, received_by, note,
                        created_at, op_no, applied)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
                    (order_id, order_item_id, qty, received_by, note, ts, key),
                )
                pending_store.append(
                    {
                        "order_item_id": order_item_id,
                        "op_no": key,
                        "quantity": qty,
                        "canonical_id": int(item["canonical_id"]),
                    }
                )
        master_conn.commit()
    except Exception:
        if owns_tx and master_conn.in_transaction:
            master_conn.rollback()
        raise

    transitioned = False
    if pending_store:
        item_ids = _apply_store_receipts(
            master_conn, store_conn_factory, store_code, order, pending_store, ts
        )
        owns_tx = _begin_immediate(master_conn)
        try:
            for entry in pending_store:
                master_conn.execute(
                    """UPDATE store_order_receipts SET applied=1
                        WHERE op_no=? AND applied=0""",
                    (entry["op_no"],),
                )
                store_item_id = item_ids.get(int(entry["order_item_id"]))
                if store_item_id is not None:
                    master_conn.execute(
                        """UPDATE store_order_items SET store_item_id=?
                            WHERE id=?""",
                        (store_item_id, entry["order_item_id"]),
                    )
            transitioned = _finalise_receipt_state(
                master_conn, order_id, received_by, note, ts
            )
            master_conn.commit()
        except Exception:
            if owns_tx and master_conn.in_transaction:
                master_conn.rollback()
            raise

    refreshed = get_order_detail(master_conn, order_id) or {}
    refreshed_items = {
        int(it["id"]): it for it in refreshed.get("order_items", [])
    }
    receipt_by_key = {
        str(r["op_no"]): r
        for r in refreshed.get("receipts", [])
        if r.get("op_no")
    }
    receipts_out: list[dict[str, Any]] = []
    for order_item_id, _item, qty in selected:
        receipt = receipt_by_key.get(line_keys[order_item_id])
        receipts_out.append(
            {
                "order_item_id": order_item_id,
                "receipt_id": int(receipt["id"]) if receipt else None,
                "quantity": parse_qty(receipt["quantity"]) if receipt else qty,
                "applied": int(receipt["applied"]) if receipt else 0,
                "fulfilled_quantity": parse_qty(
                    refreshed_items[order_item_id]["fulfilled_quantity"]
                ) if order_item_id in refreshed_items else 0.0,
            }
        )
    new_order_status = refreshed.get("status", status)
    return {
        "order_id": order_id,
        "op_no": op_no,
        "receipts": receipts_out,
        "new_order_status": new_order_status,
        "is_fully_received": new_order_status == ORDER_STATUS_DELIVERED,
        "transitioned_to_delivered": transitioned,
        "idempotent": not needs_work,
    }


def receive_order_item(
    master_conn: sqlite3.Connection,
    *,
    order_id: int,
    order_item_id: int,
    qty: float,
    actor_id: int,
    note: str | None = None,
) -> dict[str, Any]:
    """Legacy single-line entry point; delegates to :func:`receive_partial`.

    A fresh ``op_no`` is generated per call, so callers that need retry
    idempotency must use :func:`receive_partial` with their own key (the web
    form carries the page-level key).
    """
    result = receive_partial(
        master_conn,
        open_warehouse_db,
        order_id=order_id,
        op_no=generate_op_no(),
        item_qty={order_item_id: qty},
        received_by=actor_id,
        note=note,
    )
    line = next(
        (
            r for r in result["receipts"]
            if int(r["order_item_id"]) == int(order_item_id)
        ),
        None,
    )
    return {
        "order_id": order_id,
        "order_item_id": order_item_id,
        "receipt_id": line["receipt_id"] if line else None,
        "fulfilled_quantity": line["fulfilled_quantity"] if line else 0.0,
        "new_order_status": result["new_order_status"],
        "is_fully_received": result["is_fully_received"],
    }


def reconcile_orphan_receipts(
    master_conn: sqlite3.Connection,
    store_conn_factory,
) -> list[dict[str, Any]]:
    """Complete receipts whose master row landed but whose store add did not.

    ``applied=0`` means the storefront mirror was never acknowledged (crash
    between the master commit and the store write, or before the write-back).
    This job adds the quantity once, keyed ``f'{op_no}'`` on the store
    movement, then flips ``applied=1``. Re-running is a no-op because the
    movement key and the ``applied`` flag are checked first.
    """
    master_conn.row_factory = sqlite3.Row
    rows = master_conn.execute(
        """SELECT r.id, r.op_no, r.quantity, r.order_item_id,
                  so.order_no, so.flow_type, so.store_warehouse_code,
                  soi.canonical_id
             FROM store_order_receipts r
             JOIN store_orders so ON so.id = r.order_id
             JOIN store_order_items soi ON soi.id = r.order_item_id
            WHERE r.applied=0 AND r.op_no IS NOT NULL
            ORDER BY r.id"""
    ).fetchall()
    repaired: list[dict[str, Any]] = []
    for row in rows:
        code = str(row["store_warehouse_code"])
        line_op_no = str(row["op_no"])
        qty = parse_qty(row["quantity"])
        ts = now()
        store_conn = store_conn_factory(code)
        try:
            store_conn.row_factory = sqlite3.Row
            store_conn.execute("BEGIN IMMEDIATE")
            existing = store_conn.execute(
                "SELECT item_id FROM stock_movements WHERE op_no=? LIMIT 1",
                (line_op_no,),
            ).fetchone()
            if existing is not None:
                item_id = int(existing["item_id"])
            else:
                store_item = _ensure_store_item(
                    store_conn, master_conn, int(row["canonical_id"]), ts
                )
                item_id = int(store_item["id"])
                store_conn.execute(
                    "UPDATE items SET quantity = quantity + ?, updated_at=? WHERE id=?",
                    (qty, ts, item_id),
                )
                action = (
                    TRANSFER_RECEIPT_ACTION
                    if str(row["flow_type"]) == FLOW_DC_TRANSFER
                    else RECEIPT_ACTION
                )
                store_conn.execute(
                    """INSERT INTO stock_movements
                       (item_id, action, delta, note, created_at, op_no)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        item_id,
                        action,
                        qty,
                        f"{action} #{row['order_no']} 对账补录",
                        ts,
                        line_op_no,
                    ),
                )
            store_conn.commit()
        except Exception:
            try:
                store_conn.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            store_conn.close()
        repaired.append(
            {
                "receipt_id": int(row["id"]),
                "order_item_id": int(row["order_item_id"]),
                "warehouse_code": code,
                "quantity": qty,
                "op_no": line_op_no,
                "store_item_id": item_id,
            }
        )
    if repaired:
        owns_tx = _begin_immediate(master_conn)
        try:
            for entry in repaired:
                master_conn.execute(
                    """UPDATE store_order_receipts SET applied=1
                        WHERE id=? AND applied=0""",
                    (entry["receipt_id"],),
                )
                master_conn.execute(
                    "UPDATE store_order_items SET store_item_id=? WHERE id=?",
                    (entry["store_item_id"], entry["order_item_id"]),
                )
            master_conn.commit()
        except Exception:
            if owns_tx and master_conn.in_transaction:
                master_conn.rollback()
            raise
    return repaired



def list_order_receipts(
    master_conn: sqlite3.Connection,
    order_id: int,
) -> list[dict[str, Any]]:
    """Return all receipts for an order, newest first."""
    master_conn.row_factory = sqlite3.Row
    return [
        dict(r) for r in master_conn.execute(
            """SELECT r.*, u.username AS receiver_username,
                      ci.name AS canonical_name
               FROM store_order_receipts r
               LEFT JOIN users u ON u.id = r.received_by
               JOIN store_order_items soi ON soi.id = r.order_item_id
               JOIN canonical_items ci ON ci.id = soi.canonical_id
               WHERE r.order_id=?
               ORDER BY r.created_at DESC, r.id DESC""",
            (order_id,),
        ).fetchall()
    ]


def get_order_item_pending_qty(
    master_conn: sqlite3.Connection,
    order_item_id: int,
) -> float:
    """Return quantity - fulfilled_quantity for an order_item."""
    master_conn.row_factory = sqlite3.Row
    row = master_conn.execute(
        "SELECT quantity, fulfilled_quantity FROM store_order_items WHERE id=?",
        (order_item_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"order_item_id={order_item_id} not found")
    return parse_qty(float(row["quantity"]) - float(row["fulfilled_quantity"]))


def cancel_order(
    master_conn: sqlite3.Connection,
    order_id: int,
    cancelled_by: int,
    reason: str,
) -> dict[str, Any]:
    """Cancel a pending or approved order inside one locked transaction.

    The status transition is conditional
    (``WHERE id=? AND status IN ('pending','approved')``): a concurrent
    shipment that commits first makes ``changes()==0``, so the check-then-act
    gap is closed and the order keeps its post-shipment state (spec §8/D12).
    """
    master_conn.row_factory = sqlite3.Row
    owns_tx = _begin_immediate(master_conn)
    try:
        order = get_order_detail(master_conn, order_id)
        if order is None:
            raise ValueError(f"order_id={order_id} not found")
        _assert_transition(order["status"], ORDER_STATUS_CANCELLED)
        # 已有收货记录则拒绝取消：货已实际入库，作废订单会造成账实不符。
        # （2026-10-10：approved 状态开放「部分发货即可收货」后新增的防护路径；
        #  此前 approved 不可收货，故不存在该状态。）
        received = master_conn.execute(
            "SELECT COUNT(*) AS n FROM store_order_receipts WHERE order_id=?",
            (order_id,),
        ).fetchone()
        if received is not None and int(received["n"]) > 0:
            raise ValueError("订单已有收货记录，无法取消")
        # 同理：只要发过货（含部分发货），总仓库存已扣、货在途，作废单据会让
        # 在途货物失去账面对应。调货单同样适用（2026-10-19）。
        shipped = master_conn.execute(
            "SELECT COALESCE(SUM(shipped_quantity), 0) AS q "
            "FROM store_order_items WHERE order_id=?",
            (order_id,),
        ).fetchone()
        if shipped is not None and parse_qty(shipped["q"]) > 0:
            raise ValueError("订单已有发货记录，无法取消")
        ts = now()
        moved = master_conn.execute(
            """UPDATE store_orders
                  SET status=?, cancelled_by=?, cancelled_at=?,
                      cancel_reason=?, updated_at=?
                WHERE id=? AND status IN (?, ?)""",
            (
                ORDER_STATUS_CANCELLED,
                cancelled_by,
                ts,
                reason,
                ts,
                order_id,
                ORDER_STATUS_PENDING,
                ORDER_STATUS_APPROVED,
            ),
        )
        if moved.rowcount != 1:
            raise ValueError("订单状态已变更，无法取消")
        _insert_status_history(
            master_conn, order_id, order["status"], ORDER_STATUS_CANCELLED,
            cancelled_by, reason,
        )
        master_conn.commit()
    except Exception:
        if owns_tx and master_conn.in_transaction:
            master_conn.rollback()
        raise
    return get_order_detail(master_conn, order_id)


def _assert_transition(from_status: str, to_status: str) -> None:
    allowed = ALLOWED_TRANSITIONS.get(from_status, ())
    if to_status not in allowed:
        raise ValueError(f"invalid transition {from_status!r} -> {to_status!r}")


def _insert_status_history(
    master_conn: sqlite3.Connection,
    order_id: int,
    from_status: str | None,
    to_status: str,
    actor_id: int | None,
    note: str | None,
) -> None:
    master_conn.execute(
        """INSERT INTO store_order_status_history
           (order_id, from_status, to_status, actor_id, note, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (order_id, from_status, to_status, actor_id, note, now()),
    )


def _generate_delivery_no(master_conn: sqlite3.Connection) -> str:
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"SD-{today}-"
    row = master_conn.execute(
        "SELECT delivery_no FROM store_order_deliveries WHERE delivery_no LIKE ? ORDER BY delivery_no DESC LIMIT 1",
        (f"{prefix}%",),
    ).fetchone()
    seq = 1 if row is None else int(row["delivery_no"].rsplit("-", 1)[1]) + 1
    return f"{prefix}{seq:04d}"


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def notify_order_event(
    master_conn: sqlite3.Connection,
    event_type: str,
    order: dict[str, Any],
    actor_user_id: int | None = None,
) -> int:
    """Fan out an order event to the right users. Returns number of rows written."""
    master_conn.row_factory = sqlite3.Row
    # 调货单与订货单共用事件通道，但对外文案必须区分，否则分仓会收到
    # 「订货单已发出」这种与自己没有订货关系、无法据此操作的通知。
    is_transfer = order.get("flow_type") == FLOW_DC_TRANSFER
    doc_label = "总仓调货单" if is_transfer else "订货单"
    if event_type == EVENT_TRANSFER_CREATED:
        # 总仓发起调货 → 通知分仓经理/管理员准备接收。
        user_ids = _recipients_for_store_users(master_conn, order)
        user_ids = _exclude_self(user_ids, actor_user_id)
        summary = f"总仓调货单 {order['order_no']} 已发起，请准备接收"
    elif event_type == EVENT_ORDER_SUBMITTED:
        user_ids = _recipients_for_dc_reviewers(master_conn, order)
        user_ids = _exclude_self(user_ids, actor_user_id)
        summary = f"门店订货单 {order['order_no']} 已提交"
    elif event_type == EVENT_ORDER_APPROVED:
        user_ids = _recipients_for_store_users(master_conn, order)
        user_ids = _exclude_self(user_ids, actor_user_id)
        summary = f"{doc_label} {order['order_no']} 已审批通过"
    elif event_type == EVENT_ORDER_REJECTED:
        user_ids = _recipients_for_store_users(master_conn, order)
        user_ids = _exclude_self(user_ids, actor_user_id)
        summary = f"{doc_label} {order['order_no']} 已被拒绝"
    elif event_type == EVENT_ORDER_SHIPPED:
        user_ids = _recipients_for_store_users(master_conn, order)
        user_ids = _exclude_self(user_ids, actor_user_id)
        summary = f"{doc_label} {order['order_no']} 已出库配送"
    elif event_type == EVENT_ORDER_DELIVERED:
        user_ids = _recipients_for_store_users(master_conn, order)
        user_ids = _exclude_self(user_ids, actor_user_id)
        summary = f"{doc_label} {order['order_no']} 已送达"
    elif event_type == EVENT_ORDER_RECEIVED:
        # 部分收货通知发给 DC 审批人 + 平台管理员，让 DC 知道门店收到多少
        user_ids = _recipients_for_dc_reviewers(master_conn, order)
        user_ids = _exclude_self(user_ids, actor_user_id)
        summary = f"{doc_label} {order['order_no']} 部分收货"
    elif event_type == EVENT_ORDER_CANCELLED:
        # 取消通知发给对侧：门店取消 → DC 审批人；DC 取消 → 门店用户。
        # 简化做法：双侧都发给 requester + dc reviewers（去重 + 排自己）。
        user_ids = _recipients_for_store_users(master_conn, order)
        user_ids += [
            uid for uid in _recipients_for_dc_reviewers(master_conn, order)
            if uid not in user_ids
        ]
        user_ids = _exclude_self(user_ids, actor_user_id)
        summary = f"订货单 {order['order_no']} 已取消"
    else:
        raise ValueError(f"unknown event_type {event_type!r}")

    target_url = f"/store-ordering/orders/{order['id']}"
    if not user_ids:
        return 0
    return emit_event(master_conn, event_type, summary, target_url, user_ids)


def _recipients_for_dc_reviewers(
    master_conn: sqlite3.Connection,
    order: dict[str, Any],
) -> list[int]:
    """DC managers/admins for the target DC plus platform admins."""
    master_conn.row_factory = sqlite3.Row
    dc_code = order["dc_warehouse_code"]
    rows = master_conn.execute(
        """SELECT wu.user_id
           FROM warehouse_users wu
           JOIN warehouses w ON w.id = wu.warehouse_id
           WHERE w.code=? AND wu.role IN ('manager', 'admin')""",
        (dc_code,),
    ).fetchall()
    user_ids = {int(r["user_id"]) for r in rows}
    admins = master_conn.execute(
        "SELECT id FROM users WHERE is_admin=1"
    ).fetchall()
    user_ids.update(int(r["id"]) for r in admins)
    return sorted(user_ids)


def _recipients_for_store_users(
    master_conn: sqlite3.Connection,
    order: dict[str, Any],
) -> list[int]:
    """Requester + store managers/admins."""
    master_conn.row_factory = sqlite3.Row
    store_code = order["store_warehouse_code"]
    user_ids: set[int] = {int(order["requested_by"])}
    rows = master_conn.execute(
        """SELECT wu.user_id
           FROM warehouse_users wu
           JOIN warehouses w ON w.id = wu.warehouse_id
           WHERE w.code=? AND wu.role IN ('manager', 'admin')""",
        (store_code,),
    ).fetchall()
    user_ids.update(int(r["user_id"]) for r in rows)
    return sorted(user_ids)


def _exclude_self(user_ids: list[int], actor_user_id: int | None) -> list[int]:
    """Remove the actor from recipients unless they are part of the target set.

    Because user_ids is already built from role rules, keeping the actor when
    they hold a target role is the default behaviour. This helper is a defensive
    filter that removes the actor if present; callers build the role set first.
    """
    if actor_user_id is None:
        return user_ids
    return sorted({uid for uid in user_ids if uid != actor_user_id})


def order_participation_sql(alias: str = "so") -> str:
    """SQL predicate for one warehouse code; callers supply a trusted SQL alias."""
    return (
        "EXISTS (SELECT 1 FROM store_order_items soi "
        f"WHERE soi.order_id={alias}.id "
        f"AND COALESCE(soi.source_warehouse_code, {alias}.dc_warehouse_code) = ?)"
    )


def order_participants(master_conn, order_id: int) -> list[str]:
    """Return sorted line-source warehouses, falling back only for NULL sources."""
    rows = master_conn.execute(
        """SELECT DISTINCT COALESCE(soi.source_warehouse_code, so.dc_warehouse_code) AS code
             FROM store_order_items soi
             JOIN store_orders so ON so.id = soi.order_id
            WHERE so.id = ?
            ORDER BY code""",
        (order_id,),
    ).fetchall()
    return [row[0] for row in rows]


def can_act_on_order(
    user_id: int, order_id: int, *, perm: str = "order.approve",
) -> tuple[bool, list[str]]:
    """Request-bound RBAC adapter; SQL participation helpers remain Flask-free."""
    from db import get_master_db
    from permissions import has_permission

    master = get_master_db()
    user = master.execute("SELECT is_admin FROM users WHERE id=?", (user_id,)).fetchone()
    if user is not None and user[0]:
        return True, []
    participants = order_participants(master, order_id)
    missing = [
        code for code in participants
        if not has_permission(user_id, perm, warehouse_code=code)
    ]
    return bool(participants) and not missing, missing


# ---------------------------------------------------------------------------
# Approval coverage / diagnostics (Task 8, spec §11)
# ---------------------------------------------------------------------------

NO_APPROVER_ALERT_EVENT: str = "store_order_no_approver_alert"


def _users_with_permission(
    master_conn: sqlite3.Connection,
    perm: str,
    warehouse_code: str,
    *,
    tenant_id: int = 1,
) -> set[int]:
    """Non-admin active users holding `perm` on one warehouse (pure SQL).

    Mirrors ``permissions._has_target_permission`` rules (builtin or same-tenant
    role, tenant match, fail closed) without importing Flask.
    """
    master_conn.row_factory = sqlite3.Row
    rows = master_conn.execute(
        """SELECT DISTINCT wu.user_id
             FROM warehouse_users wu
             JOIN warehouses w ON w.id = wu.warehouse_id AND w.tenant_id = ?
             JOIN users u ON u.id = wu.user_id AND u.is_active = 1 AND u.is_admin = 0
             JOIN roles r ON r.code = wu.role AND (r.is_builtin = 1 OR r.tenant_id = ?)
             JOIN role_permissions rp ON rp.role_id = r.id
            WHERE w.code = ? AND rp.permission_code = ?""",
        (tenant_id, tenant_id, warehouse_code, perm),
    ).fetchall()
    return {int(row["user_id"]) for row in rows}


def has_full_approver(
    master_conn: sqlite3.Connection,
    participants: list[str],
    *,
    perm: str = "order.approve",
    tenant_id: int = 1,
) -> bool:
    """True iff some non-admin active user holds `perm` on every participant.

    Platform admins are deliberately excluded: they are the escalation target
    of the alert, not evidence that the warehouse side can approve.
    """
    if not participants:
        return False
    holders: set[int] | None = None
    for code in participants:
        current = _users_with_permission(master_conn, perm, code, tenant_id=tenant_id)
        holders = current if holders is None else holders & current
        if not holders:
            return False
    return bool(holders)


def alert_no_full_approver(
    master_conn: sqlite3.Connection,
    order: dict[str, Any],
    *,
    tenant_id: int = 1,
) -> int:
    """Inbox alert for platform admins when nobody can approve a cross order.

    Dedupe key is (event_type, target_url, admin) so re-rendering the board or
    re-processing the same order never duplicates the alert (spec §11).
    Returns the number of rows written.
    """
    if order.get("status") != ORDER_STATUS_PENDING:
        return 0
    participants = [
        str(p["code"]) for p in (order.get("participant_warehouses") or [])
    ]
    if len(participants) < 2 or has_full_approver(
        master_conn, participants, tenant_id=tenant_id
    ):
        return 0
    master_conn.row_factory = sqlite3.Row
    admins = [
        int(row["id"]) for row in master_conn.execute(
            "SELECT id FROM users WHERE is_admin=1 AND is_active=1 AND tenant_id=?",
            (tenant_id,),
        ).fetchall()
    ]
    target_url = f"/store-ordering/orders/{order['id']}"
    already = {
        int(row["user_id"]) for row in master_conn.execute(
            "SELECT user_id FROM notifications WHERE event_type=? AND target_url=?",
            (NO_APPROVER_ALERT_EVENT, target_url),
        ).fetchall()
    }
    recipients = [uid for uid in admins if uid not in already]
    if not recipients:
        return 0
    summary = (
        f"订货单 {order['order_no']} 跨仓（{'、'.join(participants)}）"
        "无全权审批人，请平台管理员介入"
    )
    return emit_event(
        master_conn, NO_APPROVER_ALERT_EVENT, summary, target_url, recipients,
    )
