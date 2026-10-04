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
    """Return cart items enriched with canonical info and dc/store stock/binding."""
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


# ---------------------------------------------------------------------------
# Available DC items
# ---------------------------------------------------------------------------

def list_available_dc_items(
    master_conn: sqlite3.Connection,
    dc_warehouse_code: str,
    category_code: str | None = None,
    keyword: str | None = None,
) -> list[dict[str, Any]]:
    """List DC items that have a non-null canonical_id and active canonical item."""
    master_conn.row_factory = sqlite3.Row
    # Resolve canonical_ids in master first so category filtering does not
    # require cross-database subqueries.
    where_parts = ["status = 'active'"]
    params: list[Any] = []
    if category_code:
        where_parts.append("category_code = ?")
        params.append(category_code)
    where = " AND ".join(where_parts)
    active_canonical_ids = {
        r["id"] for r in master_conn.execute(
            f"SELECT id FROM canonical_items WHERE {where}", params
        ).fetchall()
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
            if canonical_id not in active_canonical_ids:
                continue
            canon = master_conn.execute(
                "SELECT name, category_code, unit FROM canonical_items WHERE id=?",
                (canonical_id,),
            ).fetchone()
            if canon is None:
                continue
            item["canonical_name"] = canon["name"]
            item["category_code"] = canon["category_code"]
            item["canonical_unit"] = canon["unit"]
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
    order["items"] = [
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
        for item in order["items"]:
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
        for item in order["items"]:
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
        for item in order["items"]:
            requested = parse_qty(item["quantity"])
            master_conn.execute(
                """UPDATE store_order_items
                   SET dc_item_id=?, fulfilled_quantity=?,
                       status=?, created_at=?
                   WHERE id=?""",
                (
                    dc_item_id_map[item["canonical_id"]], requested,
                    ORDER_ITEM_STATUS_FULFILLED, ts, item["id"],
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
    """Mark order as delivered. P0 does not increase store inventory."""
    master_conn.row_factory = sqlite3.Row
    order = get_order_detail(master_conn, order_id)
    if order is None:
        raise ValueError(f"order_id={order_id} not found")
    _assert_transition(order["status"], ORDER_STATUS_DELIVERED)
    if order["status"] != ORDER_STATUS_SHIPPED:
        raise ValueError(f"order must be shipped to deliver, got {order['status']}")
    ts = now()
    master_conn.execute(
        "UPDATE store_orders SET status=?, delivered_at=?, updated_at=? WHERE id=?",
        (ORDER_STATUS_DELIVERED, ts, ts, order_id),
    )
    master_conn.execute(
        "UPDATE store_order_deliveries SET delivered_at=? WHERE order_id=?",
        (ts, order_id),
    )
    _insert_status_history(
        master_conn, order_id, order["status"], ORDER_STATUS_DELIVERED, actor_id, None,
    )
    master_conn.commit()
    return get_order_detail(master_conn, order_id)


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
