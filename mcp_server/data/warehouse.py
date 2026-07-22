"""warehouse DB 数据访问：items, movements, restock 等。"""
from __future__ import annotations

import datetime
import sqlite3


def list_items(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT i.id, i.sku, i.name, i.category_id, "
        "i.quantity AS current_stock, i.safety_stock, "
        "i.unit, i.unit_cost, i.gram_per_unit, i.updated_at, "
        "c.name AS category_name "
        "FROM items i "
        "LEFT JOIN categories c ON c.id = i.category_id "
        "ORDER BY i.id"
    ).fetchall()
    return [dict(r) for r in rows]


def get_item(conn: sqlite3.Connection, item_id: int) -> dict | None:
    r = conn.execute(
        "SELECT id, sku, name, category_id, "
        "quantity AS current_stock, safety_stock, "
        "unit, unit_cost, gram_per_unit, updated_at, "
        "(SELECT name FROM categories WHERE id = items.category_id) AS category_name "
        "FROM items WHERE id = ?",
        (item_id,),
    ).fetchone()
    return dict(r) if r else None


def item_exists(conn: sqlite3.Connection, item_id: int) -> bool:
    return (
        conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone()
        is not None
    )


def list_movements(conn: sqlite3.Connection, limit: int = 200) -> list[dict]:
    out_rows = conn.execute(
        """SELECT o.id, o.item_id, i.name AS item_name,
                  o.requested_quantity AS qty, o.reason, o.created_at,
                  'outbound' AS type
           FROM outbound_requests o
           JOIN items i ON i.id = o.item_id
           WHERE o.rolled_back = 0
           ORDER BY o.created_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    sm_rows = conn.execute(
        """SELECT s.id, s.item_id, i.name AS item_name,
                  s.delta AS qty, s.action AS reason, s.created_at,
                  'stock_movement' AS type
           FROM stock_movements s
           JOIN items i ON i.id = s.item_id
           ORDER BY s.created_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    movements = [
        {
            "id": r["id"],
            "type": r["type"],
            "item_id": r["item_id"],
            "item_name": r["item_name"],
            "qty": r["qty"],
            "reason": r["reason"],
            "created_at": r["created_at"],
        }
        for r in list(out_rows) + list(sm_rows)
    ]
    movements.sort(key=lambda m: (m["created_at"], m["id"]), reverse=True)
    return movements[:limit]


# ---------------------------------------------------------------------------
# 入库 (restock)
# ---------------------------------------------------------------------------

def create_restock(
    conn: sqlite3.Connection,
    item_id: int,
    quantity: float,
    reason: str | None,
) -> dict:
    """Create inbound restock: INSERT restock_requests + update stock.

    Mirrors blueprints/restock.py restock_submit() exactly.
    """
    import datetime
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        """INSERT INTO restock_requests
           (item_id, requested_quantity, reason, status, created_at)
           VALUES (?, ?, ?, '入库', ?)""",
        (item_id, quantity, reason or "", now),
    )
    req_id = int(cur.lastrowid)
    conn.execute(
        "UPDATE items SET quantity = quantity + ?, updated_at = ? WHERE id = ?",
        (quantity, now, item_id),
    )
    cur2 = conn.execute(
        """INSERT INTO stock_movements (item_id, action, delta, note, created_at)
           VALUES (?, '补货入库', ?, ?, ?)""",
        (item_id, quantity, f"补货记录#{req_id}入库", now),
    )
    conn.commit()
    return {"id": req_id, "movement_id": cur2.lastrowid}


def list_restock_movements(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        """SELECT s.id, s.item_id, i.name AS item_name,
                  s.delta AS qty, s.action AS reason, s.created_at
           FROM stock_movements s
           JOIN items i ON i.id = s.item_id
           WHERE s.action = 'restock'
           ORDER BY s.created_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 出库 (outbound)
# ---------------------------------------------------------------------------

def create_outbound(
    conn: sqlite3.Connection,
    item_id: int,
    quantity: float,
    reason: str | None,
) -> int:
    """Create an outbound request and deduct from stock.

    Mirrors blueprints/outbound.py outbound_submit() exactly.
    """
    import datetime
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        """INSERT INTO outbound_requests
           (item_id, requested_quantity, reason, status, rolled_back, created_at)
           VALUES (?, ?, ?, '出库', 0, ?)""",
        (item_id, quantity, reason or "", now),
    )
    req_id = int(cur.lastrowid)
    conn.execute(
        "UPDATE items SET quantity = quantity - ?, updated_at = ? WHERE id = ?",
        (quantity, now, item_id),
    )
    conn.execute(
        """INSERT INTO stock_movements (item_id, action, delta, note, created_at)
           VALUES (?, '出库', ?, ?, ?)""",
        (item_id, -quantity, f"出库记录#{req_id}出库", now),
    )
    conn.commit()
    return req_id


def list_outbound(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        """SELECT o.id, o.item_id, i.name AS item_name,
                  o.requested_quantity AS quantity, o.reason,
                  o.status, o.rolled_back, o.created_at
           FROM outbound_requests o
           JOIN items i ON i.id = o.item_id
           ORDER BY o.created_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def rollback_outbound(conn: sqlite3.Connection, req_id: int) -> dict:
    """Roll back an outbound request: return quantity to stock.

    Mirrors blueprints/outbound.py rollback() exactly.
    """
    import datetime
    req = conn.execute(
        """SELECT item_id, requested_quantity, rolled_back
           FROM outbound_requests WHERE id = ?""",
        (req_id,),
    ).fetchone()
    if req is None:
        raise ValueError("outbound_request_not_found")
    if int(req["rolled_back"]) == 1:
        raise ValueError("already_rolled_back")
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    qty = float(req["requested_quantity"])
    conn.execute(
        "UPDATE items SET quantity = quantity + ?, updated_at = ? WHERE id = ?",
        (qty, now, int(req["item_id"])),
    )
    conn.execute(
        """INSERT INTO stock_movements (item_id, action, delta, note, created_at)
           VALUES (?, '出库回退', ?, ?, ?)""",
        (int(req["item_id"]), qty, f"回退出库记录#{req_id}", now),
    )
    conn.execute(
        "UPDATE outbound_requests SET rolled_back = 1 WHERE id = ?",
        (req_id,),
    )
    conn.commit()
    return dict(req)


# ---------------------------------------------------------------------------
# 库存周转率 (inventory turnover) — stocktake-anchored
# ---------------------------------------------------------------------------

def get_inventory_turnover(
    conn: sqlite3.Connection,
    item_id: int,
    days: int = 30,
    _now: datetime.datetime | None = None,
) -> dict:
    """Estimate avg inventory and COGS-based turnover from stocktake anchors.

    Anchors are taken from `stocktakes.previous_quantity` joined to
    `stocktake_batches.created_at` (the snapshot timestamp). Batches with
    `rolled_back = 1` are excluded.

    Method: weighted-by-gap average — between two adjacent anchors, the
    earlier anchor's quantity is assumed to hold for the whole gap. This is
    a simplified "step" approximation; with only 1 anchor inside the
    window, we can't average, so we return `avg_inventory = None`.

    COGS uses the item's current `unit_cost` as a proxy for historical
    cost (the schema doesn't snapshot cost per movement).

    Returns:
        {
            "window_days": int,
            "avg_inventory": float | None,    # None if <2 anchors
            "current_inventory": float,
            "cogs_value": float,             # window consume_qty × unit_cost
            "turnover_value": float | None,  # None if avg_inventory is None
            "anchors_in_window": int,
            "anchors_total": int,            # all-time, for context
            "data_quality": "high" | "medium" | "low" | "none",
            "method": "stocktake_weighted_avg",
        }
    """

    item = conn.execute(
        "SELECT quantity, COALESCE(unit_cost, 0) AS unit_cost "
        "FROM items WHERE id = ?",
        (item_id,),
    ).fetchone()
    if item is None:
        return None  # caller is expected to validate item exists

    current_qty = float(item["quantity"])
    unit_cost = float(item["unit_cost"])

    # Precondition: only compute turnover if last restock > 30 days ago.
    end_ts = _now if _now is not None else datetime.datetime.now()
    last_restock = conn.execute(
        "SELECT MAX(created_at) AS last_at FROM restock_requests WHERE item_id = ?",
        (item_id,),
    ).fetchone()
    if last_restock and last_restock["last_at"]:
        try:
            last_dt = datetime.datetime.strptime(
                last_restock["last_at"], "%Y-%m-%d %H:%M:%S"
            )
            if (end_ts - last_dt).days < 30:
                return {
                    "window_days": days,
                    "avg_inventory": None,
                    "current_inventory": current_qty,
                    "cogs_value": 0.0,
                    "turnover_value": None,
                    "anchors_in_window": 0,
                    "anchors_total": 0,
                    "data_quality": "too_new",
                    "method": "stocktake_weighted_avg",
                }
        except ValueError:
            pass

    # 1) Fetch all non-rolled-back stocktake anchors for this item, oldest first.
    anchor_rows = conn.execute(
        """
        SELECT b.created_at AS ts, s.previous_quantity AS qty
        FROM stocktakes s
        JOIN stocktake_batches b ON b.id = s.batch_id
        WHERE b.rolled_back = 0
          AND s.item_id = ?
        ORDER BY b.created_at
        """,
        (item_id,),
    ).fetchall()

    anchors_total = len(anchor_rows)
    if anchors_total == 0:
        return {
            "window_days": days,
            "avg_inventory": None,
            "current_inventory": current_qty,
            "cogs_value": 0.0,
            "turnover_value": None,
            "anchors_in_window": 0,
            "anchors_total": 0,
            "data_quality": "none",
            "method": "stocktake_weighted_avg",
        }

    # 2) Window bounds.
    start_ts = end_ts - datetime.timedelta(days=days)

    # Add boundary anchors: window start (qty interpolated) and window end
    # (current qty). Each boundary anchor helps clip gaps at the window edge.
    def _parse(ts_str: str) -> datetime.datetime:
        return datetime.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")

    pts: list[tuple[datetime.datetime, float]] = []
    for r in anchor_rows:
        pts.append((_parse(r["ts"]), float(r["qty"])))

    # 3) Compute cogs = window consume_qty × unit_cost (same source as
    # _window_sum, but cheaper: we already know it from get_item_consumption
    # path; here we sum directly). The window cutoff is parameterized so
    # tests can pin time deterministically.
    cutoff = (end_ts - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    consume_row = conn.execute("""
        SELECT COALESCE(SUM(qty), 0) AS qty
        FROM (
            SELECT o.requested_quantity AS qty, o.created_at
            FROM outbound_requests o
            WHERE o.item_id = ? AND o.rolled_back = 0
              AND (o.reason IS NULL OR o.reason NOT LIKE '生产领料(run=#%%')
              AND o.created_at >= ?
            UNION ALL
            SELECT pri.actual_qty AS qty, pr.created_at
            FROM production_run_items pri
            JOIN production_runs pr ON pr.id = pri.run_id
            WHERE pri.item_id = ? AND pr.rolled_back = 0
              AND pr.created_at >= ?
        )
    """, (item_id, cutoff, item_id, cutoff)).fetchone()
    cogs_qty = float(dict(consume_row)["qty"])
    cogs_value = cogs_qty * unit_cost

    # 4) Clip anchors to window: if first anchor is after window start, prepend
    # a synthetic boundary anchor using current qty (back-projected). If last
    # anchor is before window end, append current qty at window end.
    in_window = [(ts, q) for ts, q in pts if start_ts <= ts <= end_ts]
    anchors_in_window = len(in_window)

    if anchors_in_window < 2:
        # Need at least 2 anchors in the window to compute a meaningful
        # weighted average. With 0 anchors (or only 1 outside the window)
        # we have no signal — fall back to None for avg/turnover.
        quality = "none" if anchors_in_window == 0 else "medium"
        return {
            "window_days": days,
            "avg_inventory": None,
            "current_inventory": current_qty,
            "cogs_value": round(cogs_value, 2),
            "turnover_value": None,
            "anchors_in_window": anchors_in_window,
            "anchors_total": anchors_total,
            "data_quality": quality,
            "method": "stocktake_weighted_avg",
        }

    # Build the segment list. Sort + dedupe timestamps.
    segments: list[tuple[datetime.datetime, float]] = sorted(in_window, key=lambda x: x[0])

    # Prepend window-start boundary: use earliest in-window anchor's qty
    # (conservative — assumes that level held back to window start).
    if segments[0][0] > start_ts:
        segments.insert(0, (start_ts, segments[0][1]))

    # Append window-end boundary: current quantity.
    if segments[-1][0] < end_ts:
        segments.append((end_ts, current_qty))
    else:
        # Last in-window anchor coincides with end — still need an end point
        # for the last segment to have a length; reuse current_qty at end_ts.
        segments.append((end_ts, current_qty))

    # 5) Weighted average: each anchor's qty covers until the next anchor.
    #    weight_i = gap_days_i (julianday of next - julianday of current)
    #    weighted_avg = Σ(qty_i * gap_i) / Σ(gap_i)
    weighted_sum = 0.0
    gap_sum = 0.0
    for i in range(len(segments) - 1):
        ts_i, qty_i = segments[i]
        ts_next = segments[i + 1][0]
        gap_days = (ts_next - ts_i).total_seconds() / 86400.0
        if gap_days <= 0:
            continue
        weighted_sum += qty_i * gap_days
        gap_sum += gap_days

    if gap_sum <= 0:
        avg_inventory: float | None = None
    else:
        avg_inventory = round(weighted_sum / gap_sum, 2)

    if avg_inventory is None or avg_inventory <= 0:
        turnover_value: float | None = None
    else:
        turnover_value = round(cogs_value / avg_inventory, 2)

    if anchors_in_window >= 2:
        data_quality = "high"
    else:
        data_quality = "medium"

    return {
        "window_days": days,
        "avg_inventory": avg_inventory,
        "current_inventory": current_qty,
        "cogs_value": round(cogs_value, 2),
        "turnover_value": turnover_value,
        "anchors_in_window": anchors_in_window,
        "anchors_total": anchors_total,
        "data_quality": data_quality,
        "method": "stocktake_weighted_avg",
    }


def get_warehouse_inventory_turnover(
    conn: sqlite3.Connection,
    days: int = 30,
    _now: datetime.datetime | None = None,
) -> dict:
    """Aggregate per-item turnover into a single warehouse-level figure.

    For each item with non-null avg_inventory:
        avg_inventory_value = avg_inventory × current unit_cost
        cogs_value          = window consumption × current unit_cost
    Then:
        warehouse_avg_inventory_value = Σ per-item avg_inventory_value
        warehouse_cogs                = Σ per-item cogs_value
        warehouse_turnover_value      = warehouse_cogs / warehouse_avg_inventory_value

    Caveats (inherited from get_inventory_turnover, applies per item):
      - unit_cost is current, not historical → COGS distortion on price changes
      - items with <2 anchors in window contribute nothing to the weighted sum
      - items with no anchors contribute nothing (data_quality='none')

    `_now` is a test-injection hook so unit tests can pin wall-clock time.
    """
    item_rows = conn.execute(
        "SELECT id FROM items"
    ).fetchall()

    warehouse_cogs = 0.0
    warehouse_avg_inventory_value = 0.0
    items_with_turnover = 0
    items_total = len(item_rows)

    for r in item_rows:
        t = get_inventory_turnover(conn, int(r["id"]), days=days, _now=_now)
        # Items with avg_inventory=None contribute nothing.
        if t["avg_inventory"] is None:
            continue
        # Get the item's unit_cost to convert avg_inventory (units → value).
        cost_row = conn.execute(
            "SELECT COALESCE(unit_cost, 0) AS unit_cost FROM items WHERE id = ?",
            (int(r["id"]),),
        ).fetchone()
        unit_cost = float(cost_row["unit_cost"])
        if unit_cost <= 0:
            # Can't convert units to value → skip (would be misleading to mix).
            continue
        warehouse_cogs += float(t["cogs_value"])
        warehouse_avg_inventory_value += float(t["avg_inventory"]) * unit_cost
        items_with_turnover += 1

    if warehouse_avg_inventory_value <= 0:
        turnover_value: float | None = None
    else:
        turnover_value = round(warehouse_cogs / warehouse_avg_inventory_value, 2)

    if items_with_turnover == 0:
        quality = "none"
    elif items_with_turnover < items_total:
        quality = "medium"
    else:
        quality = "high"

    return {
        "window_days": days,
        "warehouse_cogs_value": round(warehouse_cogs, 2),
        "warehouse_avg_inventory_value": round(warehouse_avg_inventory_value, 2),
        "turnover_value": turnover_value,
        "items_with_turnover": items_with_turnover,
        "items_total": items_total,
        "data_quality": quality,
        "method": "stocktake_weighted_sum",
    }


# ---------------------------------------------------------------------------
# 消耗统计 (consumption)
# ---------------------------------------------------------------------------

def _window_sum(
    conn: sqlite3.Connection,
    item_id: int,
    days: int,
) -> dict:
    """Return {qty, active_days, window_days} for an item's consumption over <days> window."""
    row = conn.execute(f"""
        SELECT
            COALESCE(SUM(qty), 0) AS qty,
            COUNT(DISTINCT substr(created_at, 1, 10)) AS active_days
        FROM (
            SELECT o.requested_quantity AS qty, o.created_at
            FROM outbound_requests o
            WHERE o.item_id = ? AND o.rolled_back = 0
              AND (o.reason IS NULL OR o.reason NOT LIKE '生产领料(run=#%%')
              AND o.created_at >= datetime('now', '-{days} days')
            UNION ALL
            SELECT pri.actual_qty AS qty, pr.created_at
            FROM production_run_items pri
            JOIN production_runs pr ON pr.id = pri.run_id
            WHERE pri.item_id = ? AND pr.rolled_back = 0
              AND pr.created_at >= datetime('now', '-{days} days')
        )""", (item_id, item_id)).fetchone()
    return {**(dict(row) if row else {"qty": 0, "active_days": 0}), "window_days": days}


def _weekly_breakdown(
    conn: sqlite3.Connection,
    item_id: int,
    weeks: int = 4,
) -> list[dict]:
    """Return weekly consumption for the last N weeks."""
    result = []
    for w in range(weeks):
        start = (w + 1) * 7
        end = w * 7
        row = conn.execute(f"""
            SELECT COALESCE(SUM(qty), 0) AS qty
            FROM (
                SELECT o.requested_quantity AS qty
                FROM outbound_requests o
                WHERE o.item_id = ? AND o.rolled_back = 0
                  AND (o.reason IS NULL OR o.reason NOT LIKE '生产领料(run=#%%')
                  AND o.created_at >= datetime('now', '-{start} days')
                  AND o.created_at < datetime('now', '-{end} days')
                UNION ALL
                SELECT pri.actual_qty AS qty
                FROM production_run_items pri
                JOIN production_runs pr ON pr.id = pri.run_id
                WHERE pri.item_id = ? AND pr.rolled_back = 0
                  AND pr.created_at >= datetime('now', '-{start} days')
                  AND pr.created_at < datetime('now', '-{end} days')
            )""", (item_id, item_id)).fetchone()
        result.append({
            "week_label": f"第{w + 1}周({start}天~{end}天)",
            "qty": float(dict(row)["qty"]) if row else 0.0,
        })
    return result


def list_consumption_summary(
    conn: sqlite3.Connection,
    days: int = 7,
    sort_by: str = "qty",
    limit: int = 100,
) -> dict:
    """Return per-item consumption summary for the warehouse + warehouse-level turnover.

    Value-based turnover per item (consumed_value / avg_stock_value),
    plus warehouse-level turnover via stocktake-weighted-sum.

    Return shape:
        {
            "items": [ {... per-item ...}, ... ],
            "warehouse_turnover": { ... },
        }
    """
    rows = conn.execute(f"""
        SELECT
            i.id, i.sku, i.name, i.quantity, i.safety_stock,
            i.unit, i.unit_cost,
            c.name AS category_name,
            COALESCE(c7.qty, 0) AS consume_qty,
            COALESCE(c7.days, 0) AS active_days,
            COALESCE(r7.qty, 0) AS inbound_qty,
            st.avg_stocktake_qty,
            rr.last_restock_at,
            c7.first_date,
            c7.last_date
        FROM items i
        JOIN categories c ON c.id = i.category_id
        LEFT JOIN (
            SELECT
                item_id,
                SUM(qty) AS qty,
                COUNT(DISTINCT substr(created_at, 1, 10)) AS days,
                MIN(created_at) AS first_date,
                MAX(created_at) AS last_date
            FROM (
                SELECT o.item_id, o.requested_quantity AS qty, o.created_at
                FROM outbound_requests o
                WHERE o.rolled_back = 0
                  AND (o.reason IS NULL OR o.reason NOT LIKE '生产领料(run=#%%')
                  AND o.created_at >= datetime('now', '-{days} days')
                UNION ALL
                SELECT pri.item_id, pri.actual_qty AS qty, pr.created_at
                FROM production_run_items pri
                JOIN production_runs pr ON pr.id = pri.run_id
                WHERE pr.rolled_back = 0
                  AND pr.created_at >= datetime('now', '-{days} days')
            )
            GROUP BY item_id
        ) c7 ON c7.item_id = i.id
        LEFT JOIN (
            SELECT item_id, SUM(requested_quantity) AS qty
            FROM restock_requests
            WHERE created_at >= datetime('now', '-{days} days')
            GROUP BY item_id
        ) r7 ON r7.item_id = i.id
        LEFT JOIN (
            SELECT item_id, AVG(actual_quantity) AS avg_stocktake_qty
            FROM stocktakes
            WHERE created_at >= datetime('now', '-{days} days')
            GROUP BY item_id
        ) st ON st.item_id = i.id
        LEFT JOIN (
            SELECT item_id, MAX(created_at) AS last_restock_at
            FROM restock_requests
            GROUP BY item_id
        ) rr ON rr.item_id = i.id
    """).fetchall()

    # Total for percentage calculation
    total_row = conn.execute(f"""
        SELECT COALESCE(SUM(qty), 0) AS total
        FROM (
            SELECT o.item_id, o.requested_quantity AS qty
            FROM outbound_requests o
            WHERE o.rolled_back = 0
              AND (o.reason IS NULL OR o.reason NOT LIKE '生产领料(run=#%%')
              AND o.created_at >= datetime('now', '-{days} days')
            UNION ALL
            SELECT pri.item_id, pri.actual_qty AS qty
            FROM production_run_items pri
            JOIN production_runs pr ON pr.id = pri.run_id
            WHERE pr.rolled_back = 0
              AND pr.created_at >= datetime('now', '-{days} days')
        )""").fetchone()
    total_qty = float(dict(total_row)["total"]) or 1.0

    result = []
    for r in rows:
        r = dict(r)
        qty = float(r["consume_qty"] or 0)
        unit_cost = float(r["unit_cost"] or 0)
        consume_value = round(qty * unit_cost, 2)

        current_stock = float(r["quantity"] or 0)
        avg_stocktake_qty = r["avg_stocktake_qty"]
        if avg_stocktake_qty is not None:
            avg_qty = float(avg_stocktake_qty)
        else:
            inbound_qty = float(r["inbound_qty"] or 0)
            start_qty = current_stock + qty - inbound_qty
            if start_qty < 0:
                start_qty = 0
            avg_qty = (start_qty + current_stock) / 2
        avg_stock_value = round(avg_qty * unit_cost, 2)

        # Precondition: only compute turnover if last restock > 30 days ago
        now = datetime.datetime.now()
        last_restock_str = r["last_restock_at"]
        stock_age_ok = True
        if last_restock_str is not None:
            try:
                last_dt = datetime.datetime.strptime(last_restock_str, "%Y-%m-%d %H:%M:%S")
                if (now - last_dt).days < 30:
                    stock_age_ok = False
            except ValueError:
                pass

        if stock_age_ok and avg_stock_value > 0:
            turnover_rate = round(consume_value / avg_stock_value, 2)
        else:
            turnover_rate = None

        days_active = int(r["active_days"] or 0)
        daily_avg = round(qty / days_active, 2) if days_active > 0 else 0.0

        item = {
            "item_id": r["id"],
            "sku": r["sku"],
            "name": r["name"],
            "category_name": r["category_name"],
            "unit": r["unit"],
            "current_stock": current_stock,
            "safety_stock": r["safety_stock"],
            "consume_qty": qty,
            "consume_value": consume_value,
            "active_days": days_active,
            "daily_avg": daily_avg,
            "avg_stock_value": avg_stock_value,
            "turnover_rate": turnover_rate,
            "consume_pct": round(qty / total_qty * 100, 1),
            "first_date": r["first_date"],
            "last_date": r["last_date"],
        }
        if turnover_rate is None:
            item["turnover_note"] = "库存未满30天"
        result.append(item)

    # Sort per-item results
    sort_key = {
        "qty": lambda x: x["consume_qty"],
        "value": lambda x: x["consume_value"],
        "turnover": lambda x: x["turnover_rate"] if x["turnover_rate"] is not None else -1,
        "name": lambda x: x["name"],
    }.get(sort_by, lambda x: x["consume_qty"])
    reverse = sort_by != "name"
    result.sort(key=sort_key, reverse=reverse)

    for rank, item in enumerate(result[:limit], 1):
        item["rank"] = rank
    items = result[:limit]

    # Warehouse-level turnover — always 30-day window per current spec.
    warehouse_turnover = get_warehouse_inventory_turnover(conn, days=30)

    return {
        "items": items,
        "warehouse_turnover": warehouse_turnover,
    }


def get_item_consumption(
    conn: sqlite3.Connection,
    item_id: int,
) -> dict:
    """Return consumption stats for a single item: 7d/14d/30d/monthly + weekly breakdown."""
    win_7d = _window_sum(conn, item_id, 7)
    win_14d = _window_sum(conn, item_id, 14)
    win_30d = _window_sum(conn, item_id, 30)
    # Monthly: 28 days approximation
    win_monthly = _window_sum(conn, item_id, 28)
    weekly = _weekly_breakdown(conn, item_id, 4)

    item = get_item(conn, item_id)

    def _fmt(win: dict) -> dict:
        qty = float(win.get("qty", 0) or 0)
        window_days = int(win.get("window_days", 0) or 0)
        active_days = int(win.get("active_days", 0) or 0)
        return {
            "qty": qty,
            "active_days": active_days,
            "window_days": window_days,
            "daily_avg": round(qty / window_days, 2) if window_days > 0 else 0.0,
        }

    return {
        "item_id": item_id,
        "sku": item["sku"] if item else None,
        "name": item["name"] if item else None,
        "category_name": item["category_name"] if item else None,
        "unit": item["unit"] if item else None,
        "current_stock": item["current_stock"] if item else None,
        "safety_stock": item["safety_stock"] if item else None,
        "consume_7d": _fmt(win_7d),
        "consume_14d": _fmt(win_14d),
        "consume_30d": _fmt(win_30d),
        "consume_monthly": _fmt(win_monthly),
        "weekly": weekly,
    }
