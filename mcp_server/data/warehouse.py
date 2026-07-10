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


def create_restock(
    conn: sqlite3.Connection,
    item_id: int,
    quantity: int,
    reason: str | None,
) -> int:
    """创建入库记录，返回新行 id。"""
    cursor = conn.execute(
        "INSERT INTO stock_movements (item_id, delta, action, created_at) "
        "VALUES (?, ?, ?, datetime('now'))",
        (item_id, quantity, reason or "restock"),
    )
    conn.commit()
    # 更新 items quantity
    conn.execute(
        "UPDATE items SET quantity = quantity + ?, updated_at = datetime('now') "
        "WHERE id = ?",
        (quantity, item_id),
    )
    conn.commit()
    return cursor.lastrowid


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
