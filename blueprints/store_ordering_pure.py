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
from datetime import datetime
from typing import Any

import config
from blueprints._helpers import now, parse_qty
from blueprints.notifications_pure import emit_event

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WAREHOUSE_TYPE_DC: str = "distribution_center"

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

ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    ORDER_STATUS_PENDING: (ORDER_STATUS_APPROVED, ORDER_STATUS_REJECTED, ORDER_STATUS_CANCELLED),
    ORDER_STATUS_APPROVED: (ORDER_STATUS_SHIPPED, ORDER_STATUS_CANCELLED),
    ORDER_STATUS_SHIPPED: (ORDER_STATUS_DELIVERED,),
}

SHIPMENT_ACTION: str = "门店订货出库"
RECEIPT_ACTION: str = "门店订货入库"


# ---------------------------------------------------------------------------
# Order number generation
# ---------------------------------------------------------------------------

def generate_order_no(master_conn: sqlite3.Connection) -> str:
    """Generate SO-YYYYMMDD-XXXX order number, daily auto-increment."""
    master_conn.row_factory = sqlite3.Row
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"SO-{today}-"
    row = master_conn.execute(
        "SELECT order_no FROM store_orders WHERE order_no LIKE ? ORDER BY order_no DESC LIMIT 1",
        (f"{prefix}%",),
    ).fetchone()
    if row is None:
        seq = 1
    else:
        seq = int(row["order_no"].rsplit("-", 1)[1]) + 1
    return f"{prefix}{seq:04d}"


# ---------------------------------------------------------------------------
# Warehouse db helpers
# ---------------------------------------------------------------------------

def open_warehouse_db(warehouse_code: str) -> sqlite3.Connection:
    """Open db/warehouses/{code}.db with Row factory and FK enabled."""
    path = config.WAREHOUSE_DB_DIR / f"{warehouse_code}.db"
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
    dc_warehouse_code: str,
) -> dict[str, Any]:
    """Get or create cart. If dc changed, clear existing cart items first."""
    master_conn.row_factory = sqlite3.Row
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
        dc_item = get_dc_item_by_canonical(master_conn, dc_code, canonical_id)
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
        out.append(item)
    return out


def add_cart_item(
    master_conn: sqlite3.Connection,
    cart_id: int,
    canonical_id: int,
    quantity: float,
    unit: str,
) -> None:
    """Add or merge a cart item by canonical_id."""
    master_conn.row_factory = sqlite3.Row
    qty = parse_qty(quantity)
    if qty <= 0:
        raise ValueError("quantity must be positive")
    unit = (unit or "").strip() or "件"
    existing = master_conn.execute(
        "SELECT id, quantity FROM store_order_cart_items WHERE cart_id=? AND canonical_id=?",
        (cart_id, canonical_id),
    ).fetchone()
    ts = now()
    if existing is None:
        master_conn.execute(
            """INSERT INTO store_order_cart_items
               (cart_id, canonical_id, quantity, unit, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (cart_id, canonical_id, qty, unit, ts, ts),
        )
    else:
        new_qty = parse_qty(existing["quantity"] + qty)
        master_conn.execute(
            "UPDATE store_order_cart_items SET quantity=?, unit=?, updated_at=? WHERE id=?",
            (new_qty, unit, ts, existing["id"]),
        )
    _touch_cart(master_conn, cart_id)
    master_conn.commit()


def update_cart_item_quantity(
    master_conn: sqlite3.Connection,
    cart_item_id: int,
    quantity: float,
) -> None:
    """Update cart item quantity; delete if <= 0."""
    master_conn.row_factory = sqlite3.Row
    qty = parse_qty(quantity)
    if qty <= 0:
        remove_cart_item(master_conn, cart_item_id)
        return
    item = master_conn.execute(
        "SELECT cart_id FROM store_order_cart_items WHERE id=?", (cart_item_id,)
    ).fetchone()
    if item is None:
        raise ValueError(f"cart_item_id={cart_item_id} not found")
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
) -> list[dict[str, Any]]:
    """List DC items that have a non-null canonical_id and active canonical item.

    Each returned dict is enriched with:
      - canonical_name / canonical_unit (from canonical_items)
      - category_code (from canonical_items; for chip filtering)
      - category_name (Chinese; from DC categories.name → fallback to
        canonical_categories.name → fallback to category_code → fallback to '—')
      - unit_price (selling_price → unit_cost → 0)
    """
    master_conn.row_factory = sqlite3.Row
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
        where_parts = ["canonical_id IS NOT NULL"]
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
            out.append(item)
        return out
    finally:
        dc_conn.close()


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

    Returns {"ok": bool, "shortages": [...], "unbound": [...]}
    shortages: {canonical_id, name, requested, available}
    unbound: {canonical_id, name}
    """
    master_conn.row_factory = sqlite3.Row
    items = list_cart_items(master_conn, cart["id"])
    shortages: list[dict[str, Any]] = []
    unbound: list[dict[str, Any]] = []
    for item in items:
        canonical_id = item["canonical_id"]
        name = item["name"]
        requested = parse_qty(item["quantity"])
        available = parse_qty(item.get("dc_available") or 0)
        if requested > available:
            shortages.append({
                "canonical_id": canonical_id,
                "name": name,
                "requested": requested,
                "available": available,
            })
        if not item.get("store_bound"):
            unbound.append({"canonical_id": canonical_id, "name": name})
    return {
        "ok": not shortages and not unbound and bool(items),
        "shortages": shortages,
        "unbound": unbound,
    }


def submit_order(
    master_conn: sqlite3.Connection,
    cart_id: int,
    requested_by: int,
    expected_delivery_date: str | None,
    note: str | None,
) -> dict[str, Any]:
    """Convert cart to order, return order dict."""
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
    order_no = generate_order_no(master_conn)
    ts = now()
    cur = master_conn.execute(
        """INSERT INTO store_orders
           (order_no, store_warehouse_code, dc_warehouse_code, status,
            requested_by, expected_delivery_date, note, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            order_no, cart_dict["store_warehouse_code"], cart_dict["dc_warehouse_code"],
            ORDER_STATUS_PENDING, requested_by, expected_delivery_date,
            note, ts, ts,
        ),
    )
    order_id = int(cur.lastrowid)
    for ci in cart_items:
        master_conn.execute(
            """INSERT INTO store_order_items
               (order_id, canonical_id, store_item_id, quantity, unit,
                fulfilled_quantity, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                order_id, ci["canonical_id"], ci.get("store_item_id"),
                parse_qty(ci["quantity"]), ci["cart_unit"], 0.0,
                ORDER_ITEM_STATUS_PENDING, ts,
            ),
        )
    # Clear cart items but keep the cart row so user can build next order.
    clear_cart(master_conn, cart_id)
    _insert_status_history(master_conn, order_id, None, ORDER_STATUS_PENDING, requested_by, note)
    master_conn.commit()
    return get_order_detail(master_conn, order_id)


# ---------------------------------------------------------------------------
# Order queries
# ---------------------------------------------------------------------------

def list_orders(
    master_conn: sqlite3.Connection,
    *,
    store_warehouse_code: str | None = None,
    dc_warehouse_code: str | None = None,
    status: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Query orders with optional filters."""
    master_conn.row_factory = sqlite3.Row
    where_parts: list[str] = []
    params: list[Any] = []
    if store_warehouse_code:
        where_parts.append("store_warehouse_code = ?")
        params.append(store_warehouse_code)
    if dc_warehouse_code:
        where_parts.append("dc_warehouse_code = ?")
        params.append(dc_warehouse_code)
    if status:
        where_parts.append("status = ?")
        params.append(status)
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
                   ship.username AS shipped_by_username
            FROM store_orders so
            LEFT JOIN users req ON req.id = so.requested_by
            LEFT JOIN users appr ON appr.id = so.approved_by
            LEFT JOIN users ship ON ship.id = so.shipped_by
            {where}
            ORDER BY so.created_at DESC, so.id DESC
            LIMIT ? OFFSET ?""",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


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
                  cancel.username AS cancelled_by_username
           FROM store_orders so
           LEFT JOIN users req ON req.id = so.requested_by
           LEFT JOIN users appr ON appr.id = so.approved_by
           LEFT JOIN users ship ON ship.id = so.shipped_by
           LEFT JOIN users cancel ON cancel.id = so.cancelled_by
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
    """Approve or reject an order."""
    master_conn.row_factory = sqlite3.Row
    if decision not in (ORDER_STATUS_APPROVED, ORDER_STATUS_REJECTED):
        raise ValueError(f"decision must be approved/rejected, got {decision!r}")
    order = get_order_detail(master_conn, order_id)
    if order is None:
        raise ValueError(f"order_id={order_id} not found")
    _assert_transition(order["status"], decision)
    ts = now()
    if decision == ORDER_STATUS_APPROVED:
        master_conn.execute(
            """UPDATE store_orders
               SET status=?, approved_by=?, approved_at=?, approved_note=?, updated_at=?
               WHERE id=?""",
            (ORDER_STATUS_APPROVED, actor_id, ts, note, ts, order_id),
        )
    else:
        master_conn.execute(
            """UPDATE store_orders
               SET status=?, approved_by=?, approved_at=?, approved_note=?, updated_at=?
               WHERE id=?""",
            (ORDER_STATUS_REJECTED, actor_id, ts, note, ts, order_id),
        )
    _insert_status_history(master_conn, order_id, order["status"], decision, actor_id, note)
    master_conn.commit()
    return get_order_detail(master_conn, order_id)


def ship_order(
    master_conn: sqlite3.Connection,
    order_id: int,
    shipped_by: int,
    tracking_note: str | None = None,
) -> dict[str, Any]:
    """Ship all items at once. Raises ValueError if DC stock insufficient."""
    master_conn.row_factory = sqlite3.Row
    order = get_order_detail(master_conn, order_id)
    if order is None:
        raise ValueError(f"order_id={order_id} not found")
    _assert_transition(order["status"], ORDER_STATUS_SHIPPED)
    if order["status"] != ORDER_STATUS_APPROVED:
        raise ValueError(f"order must be approved to ship, got {order['status']}")

    dc_code = order["dc_warehouse_code"]
    dc_conn = open_warehouse_db(dc_code)
    try:
        dc_conn.row_factory = sqlite3.Row
        # Validate stock first.
        for item in order["order_items"]:
            canonical_id = item["canonical_id"]
            dc_item = dc_conn.execute(
                "SELECT id, quantity FROM items WHERE canonical_id=?", (canonical_id,)
            ).fetchone()
            if dc_item is None:
                raise ValueError(f"DC missing canonical_id={canonical_id}")
            available = parse_qty(dc_item["quantity"])
            requested = parse_qty(item["quantity"])
            if requested > available:
                raise ValueError(
                    f"库存不足：{item['canonical_name']} 需 {requested}，可用 {available}"
                )

        # All good: deduct stock, write movements, create delivery, update order.
        ts = now()
        delivery_no = _generate_delivery_no(master_conn)
        dc_item_id_map: dict[int, int] = {}
        for item in order["order_items"]:
            canonical_id = item["canonical_id"]
            requested = parse_qty(item["quantity"])
            dc_item = dc_conn.execute(
                "SELECT id, quantity FROM items WHERE canonical_id=?", (canonical_id,)
            ).fetchone()
            assert dc_item is not None
            dc_item_id_map[canonical_id] = int(dc_item["id"])
            new_qty = parse_qty(dc_item["quantity"] - requested)
            dc_conn.execute(
                "UPDATE items SET quantity=? WHERE id=?",
                (new_qty, dc_item["id"]),
            )
            dc_conn.execute(
                """INSERT INTO stock_movements
                   (item_id, action, delta, note, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    dc_item["id"], SHIPMENT_ACTION, -requested,
                    f"{SHIPMENT_ACTION} #{order['order_no']}", ts,
                ),
            )
        dc_conn.commit()

        master_conn.execute(
            """INSERT INTO store_order_deliveries
               (order_id, delivery_no, shipped_by, shipped_at, tracking_note, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (order_id, delivery_no, shipped_by, ts, tracking_note, ts),
        )
        master_conn.execute(
            """UPDATE store_orders
               SET status=?, shipped_by=?, shipped_at=?, updated_at=?
               WHERE id=?""",
            (ORDER_STATUS_SHIPPED, shipped_by, ts, ts, order_id),
        )
        for item in order["order_items"]:
            # v2: keep fulfilled_quantity at 0 here so receive_order_item can
            # drive partial receipts. We only backfill dc_item_id so the
            # receipt flow can locate the shipped item.
            master_conn.execute(
                """UPDATE store_order_items
                   SET dc_item_id=?, status=?
                   WHERE id=?""",
                (
                    dc_item_id_map[item["canonical_id"]],
                    ORDER_ITEM_STATUS_PENDING,
                    item["id"],
                ),
            )
        _insert_status_history(
            master_conn, order_id, order["status"], ORDER_STATUS_SHIPPED, shipped_by, tracking_note,
        )
        master_conn.commit()
    except Exception:
        # Ensure we close the DC connection; master_conn rollback is caller's
        # responsibility if it is managed in a broader transaction. Here we do
        # not alter master_conn state until after stock deduction succeeds, so
        # a raised exception leaves master_conn untouched.
        dc_conn.rollback()
        dc_conn.close()
        raise
    else:
        dc_conn.close()
    return get_order_detail(master_conn, order_id)


def mark_order_delivered(
    master_conn: sqlite3.Connection,
    order_id: int,
    actor_id: int,
) -> dict[str, Any]:
    """DEPRECATED wrapper — single-shot deliver using receive_order_item().

    v2: the canonical path is to call ``receive_order_item`` once per order
    item. This legacy entry-point is preserved so v1's
    ``test_mark_order_delivered`` keeps passing: if the order is already
    shipped and there are no receipts yet, we forward by issuing one
    ``receive_order_item`` call per order item to consume the entire
    remaining quantity, then return the post-delivery order detail.
    """
    master_conn.row_factory = sqlite3.Row
    order = get_order_detail(master_conn, order_id)
    if order is None:
        raise ValueError(f"order_id={order_id} not found")
    if order["status"] != ORDER_STATUS_SHIPPED:
        raise ValueError(f"order must be shipped to deliver, got {order['status']}")
    for item in order["order_items"]:
        pending = parse_qty(item["quantity"]) - parse_qty(item["fulfilled_quantity"])
        if pending <= 0:
            continue
        receive_order_item(
            master_conn, order_id=order_id, order_item_id=item["id"],
            qty=pending, actor_id=actor_id, note="legacy auto-receive",
        )
    return get_order_detail(master_conn, order_id)


def receive_order_item(
    master_conn: sqlite3.Connection,
    *,
    order_id: int,
    order_item_id: int,
    qty: float,
    actor_id: int,
    note: str | None = None,
) -> dict[str, Any]:
    """Receive (partial or full) a single order_item at the storefront warehouse.

    - Validates order.status == 'shipped'.
    - Validates the order_item belongs to the order.
    - Validates qty > 0 and qty <= (quantity - fulfilled_quantity).
    - Writes a row to ``store_order_receipts``.
    - Adds ``qty`` to the storefront warehouse ``items.quantity`` (auto-creating
      a local row if needed; see §3.1 + §9.1). Writes a ``stock_movements``
      entry with action='门店订货入库'.
    - Increments ``store_order_items.fulfilled_quantity`` and bumps its
      status to 'partial' / 'fulfilled' accordingly.
    - When every order_item is fulfilled, flips ``store_orders.status`` to
      'delivered' (sets delivered_at, history). Does NOT emit notifications;
      the route layer is responsible for calling ``notify_order_event``.

    Returns a dict with ``order_id``, ``order_item_id``, ``receipt_id``,
    ``fulfilled_quantity``, ``new_order_status``, ``is_fully_received``.
    Raises ValueError for any of the validation failures listed above.
    """
    master_conn.row_factory = sqlite3.Row
    if qty is None:
        raise ValueError("qty is required")
    qty = parse_qty(qty)
    if qty <= 0:
        raise ValueError("收货数量必须大于 0")

    order = get_order_detail(master_conn, order_id)
    if order is None:
        raise ValueError(f"order_id={order_id} not found")
    if order["status"] != ORDER_STATUS_SHIPPED:
        raise ValueError(
            f"order must be shipped to receive, got {order['status']!r}"
        )

    target_item = next(
        (it for it in order["order_items"] if int(it["id"]) == int(order_item_id)),
        None,
    )
    if target_item is None:
        raise ValueError(
            f"order_item_id={order_item_id} 不属于 order_id={order_id}"
        )

    remaining = parse_qty(target_item["quantity"]) - parse_qty(
        target_item["fulfilled_quantity"]
    )
    if qty > remaining + 1e-9:
        raise ValueError(
            f"收货数量 {qty} 超过待收 {remaining}"
        )

    # 1. write receipt row.
    ts = now()
    cur = master_conn.execute(
        """INSERT INTO store_order_receipts
           (order_id, order_item_id, quantity, received_by, note, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (order_id, order_item_id, qty, actor_id, note, ts),
    )
    receipt_id = int(cur.lastrowid)

    # 2. open storefront warehouse and update stock + write movement.
    store_code = order["store_warehouse_code"]
    canonical_id = int(target_item["canonical_id"])
    canon = master_conn.execute(
        "SELECT name, category_code FROM canonical_items WHERE id=?",
        (canonical_id,),
    ).fetchone()
    if canon is None:
        raise ValueError(f"canonical_id={canonical_id} not found")
    canonical_category_code = canon["category_code"]

    store_conn = open_warehouse_db(store_code)
    try:
        store_conn.row_factory = sqlite3.Row
        store_item = store_conn.execute(
            "SELECT * FROM items WHERE canonical_id=?",
            (canonical_id,),
        ).fetchone()
        if store_item is None:
            # Auto-create: pick category by canonical_code → categories.name.
            cat_id: int | None = None
            if canonical_category_code:
                row = store_conn.execute(
                    "SELECT id FROM categories WHERE canonical_code=?",
                    (canonical_category_code,),
                ).fetchone()
                if row is not None:
                    cat_id = int(row["id"])
                else:
                    # Fallback: pick by Chinese name from canonical_categories.
                    cc = master_conn.execute(
                        "SELECT name FROM canonical_categories WHERE code=?",
                        (canonical_category_code,),
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
                # Last-resort: first category row (init seeds all 9).
                row = store_conn.execute(
                    "SELECT id FROM categories ORDER BY id LIMIT 1"
                ).fetchone()
                if row is None:
                    raise ValueError(
                        f"门店仓 {store_code} 尚未初始化品类，"
                        f"无法为 canonical_id={canonical_id} 自动创建 items"
                    )
                cat_id = int(row["id"])
            sku = f"AUTO-RECEIVE-{canonical_id}"
            cur = store_conn.execute(
                """INSERT INTO items
                   (sku, name, category_id, quantity, safety_stock,
                    unit, unit_cost, gram_per_unit, aux_unit, aux_rate,
                    canonical_id, updated_at)
                   VALUES (?, ?, ?, 0, 0, ?, 0, 0, NULL, 0, ?, ?)""",
                (sku, str(canon["name"]), cat_id, "件", canonical_id, ts),
            )
            new_id = int(cur.lastrowid)
            store_conn.commit()
            # Re-open a fresh read so the row is visible to subsequent reads
            # (in case the same connection's read transaction snapshots the
            # state before the insert).
            store_conn.close()
            store_conn = open_warehouse_db(store_code)
            store_conn.row_factory = sqlite3.Row
            store_item = store_conn.execute(
                "SELECT * FROM items WHERE id=?", (new_id,)
            ).fetchone()
        assert store_item is not None
        new_qty = parse_qty(float(store_item["quantity"]) + qty)
        store_conn.execute(
            "UPDATE items SET quantity=?, updated_at=? WHERE id=?",
            (new_qty, ts, store_item["id"]),
        )
        store_conn.execute(
            """INSERT INTO stock_movements
               (item_id, action, delta, note, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                store_item["id"], RECEIPT_ACTION, qty,
                f"{RECEIPT_ACTION} #{order['order_no']}", ts,
            ),
        )
        store_conn.commit()
        # Backfill store_item_id on the order item so subsequent receipts can
        # locate it directly.
        master_conn.execute(
            "UPDATE store_order_items SET store_item_id=? WHERE id=?",
            (store_item["id"], order_item_id),
        )
    except Exception:
        store_conn.rollback()
        store_conn.close()
        raise
    else:
        store_conn.close()

    # 3. update order item fulfilled_quantity + status.
    new_fulfilled = parse_qty(float(target_item["fulfilled_quantity"]) + qty)
    if abs(new_fulfilled - float(target_item["quantity"])) < 1e-9:
        new_item_status = ORDER_ITEM_STATUS_FULFILLED
    elif new_fulfilled > 0:
        new_item_status = ORDER_ITEM_STATUS_PARTIAL
    else:
        new_item_status = ORDER_ITEM_STATUS_PENDING
    master_conn.execute(
        """UPDATE store_order_items
           SET fulfilled_quantity=?, status=?
           WHERE id=?""",
        (new_fulfilled, new_item_status, order_item_id),
    )

    # 4. decide whether to flip the order to delivered.
    new_order_status = order["status"]
    is_fully_received = False
    # Reload items because we just updated one.
    refreshed = get_order_detail(master_conn, order_id)
    if refreshed is not None:
        all_done = all(
            abs(parse_qty(it["fulfilled_quantity"]) - parse_qty(it["quantity"])) < 1e-9
            for it in refreshed["order_items"]
        )
        if all_done and refreshed["status"] == ORDER_STATUS_SHIPPED:
            master_conn.execute(
                """UPDATE store_orders
                   SET status=?, delivered_at=?, updated_at=?
                   WHERE id=?""",
                (ORDER_STATUS_DELIVERED, ts, ts, order_id),
            )
            master_conn.execute(
                "UPDATE store_order_deliveries SET delivered_at=? WHERE order_id=?",
                (ts, order_id),
            )
            _insert_status_history(
                master_conn, order_id, ORDER_STATUS_SHIPPED,
                ORDER_STATUS_DELIVERED, actor_id, note or "门店收货自动完成",
            )
            new_order_status = ORDER_STATUS_DELIVERED
            is_fully_received = True
    master_conn.commit()

    return {
        "order_id": order_id,
        "order_item_id": order_item_id,
        "receipt_id": receipt_id,
        "fulfilled_quantity": new_fulfilled,
        "new_order_status": new_order_status,
        "is_fully_received": is_fully_received,
    }


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
    """Cancel a pending or approved order (P1 placeholder)."""
    master_conn.row_factory = sqlite3.Row
    order = get_order_detail(master_conn, order_id)
    if order is None:
        raise ValueError(f"order_id={order_id} not found")
    _assert_transition(order["status"], ORDER_STATUS_CANCELLED)
    ts = now()
    master_conn.execute(
        """UPDATE store_orders
           SET status=?, cancelled_by=?, cancelled_at=?, cancel_reason=?, updated_at=?
           WHERE id=?""",
        (ORDER_STATUS_CANCELLED, cancelled_by, ts, reason, ts, order_id),
    )
    _insert_status_history(
        master_conn, order_id, order["status"], ORDER_STATUS_CANCELLED, cancelled_by, reason,
    )
    master_conn.commit()
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
    if event_type == EVENT_ORDER_SUBMITTED:
        user_ids = _recipients_for_dc_reviewers(master_conn, order)
        user_ids = _exclude_self(user_ids, actor_user_id)
        summary = f"门店订货单 {order['order_no']} 已提交"
    elif event_type == EVENT_ORDER_APPROVED:
        user_ids = _recipients_for_store_users(master_conn, order)
        user_ids = _exclude_self(user_ids, actor_user_id)
        summary = f"订货单 {order['order_no']} 已审批通过"
    elif event_type == EVENT_ORDER_REJECTED:
        user_ids = _recipients_for_store_users(master_conn, order)
        user_ids = _exclude_self(user_ids, actor_user_id)
        summary = f"订货单 {order['order_no']} 已被拒绝"
    elif event_type == EVENT_ORDER_SHIPPED:
        user_ids = _recipients_for_store_users(master_conn, order)
        user_ids = _exclude_self(user_ids, actor_user_id)
        summary = f"订货单 {order['order_no']} 已出库配送"
    elif event_type == EVENT_ORDER_DELIVERED:
        user_ids = _recipients_for_store_users(master_conn, order)
        user_ids = _exclude_self(user_ids, actor_user_id)
        summary = f"订货单 {order['order_no']} 已送达"
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
