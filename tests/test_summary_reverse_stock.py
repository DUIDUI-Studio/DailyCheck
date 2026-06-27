"""汇总页反推起点库存逻辑测试。"""
import sqlite3
import tempfile
import os
from datetime import datetime, timedelta


def _seed_history(path):
    """建一个有历史的临时 SQLite db,验证反推逻辑。"""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    # 建表(简化,不走 init_warehouse_db)
    conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT, quantity REAL, unit_cost REAL, category_id INTEGER)")
    conn.execute("CREATE TABLE stock_movements (id INTEGER PRIMARY KEY, item_id INTEGER, delta REAL, created_at TEXT)")
    conn.execute("CREATE TABLE categories (id INTEGER PRIMARY KEY, name TEXT)")

    conn.execute("INSERT INTO categories (id, name) VALUES (1, '包材')")
    conn.execute("INSERT INTO items (id, name, quantity, unit_cost, category_id) VALUES (1, '面粉', 100, 5, 1)")
    now = datetime.now()
    # 7 日窗口内:+30 入库,-50 出库(净 -20)
    conn.execute(
        "INSERT INTO stock_movements (item_id, delta, created_at) VALUES (1, 30, ?)",
        ((now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),)
    )
    conn.execute(
        "INSERT INTO stock_movements (item_id, delta, created_at) VALUES (1, -50, ?)",
        ((now - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S"),)
    )
    # 窗口外的变动(应被排除)
    conn.execute(
        "INSERT INTO stock_movements (item_id, delta, created_at) VALUES (1, 1000, ?)",
        ((now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S"),)
    )
    conn.commit()
    conn.close()


def _tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return path


def test_reverse_qty_start_7d():
    """7 日窗口:quantity=100, 窗口内 delta = +30 - 50 = -20,起点 = 100 - (-20) = 120。"""
    path = _tmp_db()
    try:
        _seed_history(path)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT i.id, i.quantity, i.unit_cost,
                      COALESCE(SUM(m.delta), 0) AS d
               FROM items i
               LEFT JOIN stock_movements m
                 ON m.item_id = i.id AND m.created_at >= datetime('now','-7 days')
               GROUP BY i.id"""
        ).fetchall()
        assert len(rows) == 1
        qty_start = float(rows[0]["quantity"]) - float(rows[0]["d"])
        assert qty_start == 120.0
        conn.close()
    finally:
        os.unlink(path)


def test_reverse_excludes_outside_window():
    """30 天前 +1000 应被排除,不影响 7 日窗口的反推。"""
    path = _tmp_db()
    try:
        _seed_history(path)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT COALESCE(SUM(m.delta), 0) AS d
               FROM items i
               LEFT JOIN stock_movements m
                 ON m.item_id = i.id AND m.created_at >= datetime('now','-7 days')
               GROUP BY i.id"""
        ).fetchone()
        # 窗口内只有 +30 -50 = -20;1000 在 30 天前被排除
        assert float(rows["d"]) == -20.0
        conn.close()
    finally:
        os.unlink(path)


def test_all_uses_min_created_at():
    """range=all:起点用 MIN(stock_movements.created_at)。"""
    path = _tmp_db()
    try:
        _seed_history(path)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        first = conn.execute(
            "SELECT MIN(created_at) AS d FROM stock_movements"
        ).fetchone()["d"]
        assert first is not None
        days_ago = (datetime.now() - datetime.strptime(first[:10], "%Y-%m-%d")).days
        # MIN 应该是 30 天前那条
        assert days_ago >= 28
        conn.close()
    finally:
        os.unlink(path)


def test_item_with_no_movements():
    """无任何 stock_movements 的 item:窗口内 delta=0,起点=当前 quantity。"""
    path = _tmp_db()
    try:
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT, quantity REAL, unit_cost REAL, category_id INTEGER)")
        conn.execute("CREATE TABLE stock_movements (id INTEGER PRIMARY KEY, item_id INTEGER, delta REAL, created_at TEXT)")
        conn.execute("CREATE TABLE categories (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO categories (id, name) VALUES (1, '包材')")
        conn.execute("INSERT INTO items (id, name, quantity, unit_cost, category_id) VALUES (1, '面粉', 100, 5, 1)")
        conn.commit()
        conn.close()

        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT i.id, i.quantity, i.unit_cost,
                      COALESCE(SUM(m.delta), 0) AS d
               FROM items i
               LEFT JOIN stock_movements m
                 ON m.item_id = i.id AND m.created_at >= datetime('now','-7 days')
               GROUP BY i.id"""
        ).fetchall()
        assert len(rows) == 1
        qty_start = float(rows[0]["quantity"]) - float(rows[0]["d"])
        assert qty_start == 100.0  # 没变动,起点=当前
        conn.close()
    finally:
        os.unlink(path)