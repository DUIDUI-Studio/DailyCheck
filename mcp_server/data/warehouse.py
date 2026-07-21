"""warehouse DB 数据访问：items, movements, restock 等。"""
from __future__ import annotations

import sqlite3


def list_items(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, sku, name, category_id, quantity, safety_stock, "
        "unit, unit_cost, gram_per_unit, updated_at "
        "FROM items ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def get_item(conn: sqlite3.Connection, item_id: int) -> dict | None:
    r = conn.execute(
        "SELECT id, sku, name, category_id, quantity, safety_stock, "
        "unit, unit_cost, gram_per_unit, updated_at "
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
) -> list[dict]:
    """Return per-item consumption summary for the warehouse.

    Mirrors blueprints/items.py inventory_view() exactly.
    Includes: 7d/30d consumption, daily_avg, turnover_rate, ranking.
    """
    order_map = {
        "qty": "consume_qty DESC",
        "value": "consume_value DESC",
        "turnover": "turnover_rate DESC",
        "name": "name ASC",
    }
    order_col = order_map.get(sort_by, "consume_qty DESC")

    rows = conn.execute(f"""
        SELECT
            i.id, i.sku, i.name, i.quantity, i.safety_stock,
            i.unit, i.unit_cost,
            c.name AS category_name,
            COALESCE(c7.qty, 0) AS consume_qty,
            COALESCE(c7.days, 0) AS active_days,
            CASE WHEN i.quantity > 0
                 THEN ROUND(COALESCE(c7.qty, 0) / i.quantity, 2)
                 ELSE 0 END AS turnover_rate,
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
        ORDER BY {order_col}
        LIMIT ?
    """, (limit,)).fetchall()

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
    for rank, r in enumerate(rows, 1):
        r = dict(r)
        qty = float(r["consume_qty"])
        result.append({
            "rank": rank,
            "item_id": r["id"],
            "sku": r["sku"],
            "name": r["name"],
            "category": r["category_name"],
            "unit": r["unit"],
            "current_stock": r["quantity"],
            "safety_stock": r["safety_stock"],
            "consume_qty": qty,
            "active_days": r["active_days"],
            "daily_avg": round(qty / days, 2) if days > 0 else 0.0,
            "turnover_rate": r["turnover_rate"],
            "consume_pct": round(qty / total_qty * 100, 1),
            "first_date": r["first_date"],
            "last_date": r["last_date"],
        })
    return result


def get_item_consumption(
    conn: sqlite3.Connection,
    item_id: int,
) -> dict:
    """Return consumption stats for a single item: 7d/30d/monthly + weekly breakdown."""
    win_7d = _window_sum(conn, item_id, 7)
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
        "unit": item["unit"] if item else None,
        "current_stock": item["quantity"] if item else None,
        "safety_stock": item["safety_stock"] if item else None,
        "consume_7d": _fmt(win_7d),
        "consume_30d": _fmt(win_30d),
        "consume_monthly": _fmt(win_monthly),
        "weekly": weekly,
    }
