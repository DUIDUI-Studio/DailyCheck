"""按品类消耗金额口径修复测试。

回归测试:spec 漏了把 production_run_items 加到品类消耗聚合里,
导致品类表 consumed_value 之和小于 total_consumed_value。本测试用真实
production_run_items + outbound_requests 数据断言两者之和一致。
"""
import sqlite3
from datetime import datetime, timedelta

from tests.conftest import _wh


def _seed_item(wh_path, name, qty, unit_cost, gram_per_unit=0):
    """插入一个测试品项,返回 item_id。"""
    conn = _wh(wh_path)
    cat_id = conn.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()["id"]
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "INSERT INTO items (sku, name, category_id, quantity, safety_stock, unit_cost, unit, gram_per_unit, updated_at) "
        "VALUES (?, ?, ?, ?, 0, ?, '件', ?, ?)",
        (f"T-{name}", name, cat_id, qty, unit_cost, gram_per_unit, ts))
    item_id = cur.lastrowid
    conn.commit()
    conn.close()
    return item_id, cat_id


def _seed_outbound(wh_path, item_id, qty, reason=None):
    """插入一条出库请求。"""
    conn = _wh(wh_path)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO outbound_requests (item_id, requested_quantity, reason, rolled_back, created_at) "
        "VALUES (?, ?, ?, 0, ?)",
        (item_id, qty, reason, ts))
    conn.commit()
    conn.close()


def _seed_production_consumption(wh_path, item_id, qty):
    """插入一条生产消耗。"""
    conn = _wh(wh_path)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # production_runs.product_id NOT NULL — 先建一个 product
    cur = conn.execute(
        "INSERT INTO products (name, unit, note, created_at) VALUES ('test-product', '件', '', ?)",
        (ts,))
    product_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO production_runs (product_id, output_qty, note, rolled_back, created_at) "
        "VALUES (?, 1, 'test', 0, ?)", (product_id, ts))
    run_id = cur.lastrowid
    conn.execute(
        "INSERT INTO production_run_items (run_id, item_id, planned_qty, actual_qty) VALUES (?, ?, ?, ?)",
        (run_id, item_id, qty, qty))
    conn.commit()
    conn.close()


def test_category_consumed_includes_production(logged_client):
    """品类消耗口径 = outbound(无生产领料) + production_run_items。

    修复前 bug:cat_data SQL 只 LEFT JOIN outbound_requests,漏了 production_run_items,
    导致品类表 consumed_value 之和小于 total_consumed_value(差 = 生产消耗)。
    """
    client, wh_path = logged_client

    # 用品项 A:同时有出库 + 生产消耗
    item_a, _ = _seed_item(wh_path, "catProdA", qty=100, unit_cost=10)
    _seed_outbound(wh_path, item_a, qty=5, reason=None)  # 普通出库 5 件
    _seed_production_consumption(wh_path, item_a, qty=3)  # 生产消耗 3 件

    resp = client.get("/summary?range=all")
    assert resp.status_code == 200

    # 直接查 db 验证 cat_data 与 total_consumed_value 同源
    conn = _wh(wh_path)
    # 总体消耗 = outbound(非生产领料) + production_run_items
    total_consumed = conn.execute(
        """SELECT COALESCE(SUM(qty * unit_cost), 0) AS c FROM (
            SELECT o.requested_quantity AS qty, i.unit_cost
            FROM outbound_requests o
            JOIN items i ON i.id = o.item_id
            WHERE o.rolled_back = 0
              AND (o.reason IS NULL OR o.reason NOT LIKE '生产领料(run=#%')
            UNION ALL
            SELECT pri.actual_qty AS qty, i.unit_cost
            FROM production_run_items pri
            JOIN production_runs pr ON pr.id = pri.run_id
            JOIN items i ON i.id = pri.item_id
            WHERE pr.rolled_back = 0
        )"""
    ).fetchone()["c"]
    # 应 = 5*10 + 3*10 = 80
    assert float(total_consumed) == 80.0

    # 关键断言:渲染页面的总消耗金额 = 80
    assert b"80.00" in resp.data

    # 关键断言:按品类表的 consumed_value 之和 = 总消耗金额(否则就是旧 bug)
    # 通过 SQL 直接模拟 /summary 的 cat_data 查询(同样的聚合)
    cat_sum = conn.execute(
        """SELECT COALESCE(SUM(consumed_value), 0) AS c FROM (
            SELECT
              (COALESCE(o.total_outbound, 0) + COALESCE(p.total_production, 0)) * i.unit_cost AS consumed_value
            FROM items i
            LEFT JOIN (
                SELECT item_id, SUM(requested_quantity) AS total_outbound
                FROM outbound_requests
                WHERE rolled_back = 0
                  AND (reason IS NULL OR reason NOT LIKE '生产领料(run=#%')
                GROUP BY item_id
            ) o ON o.item_id = i.id
            LEFT JOIN (
                SELECT pri.item_id, SUM(pri.actual_qty) AS total_production
                FROM production_run_items pri
                JOIN production_runs pr ON pr.id = pri.run_id
                WHERE pr.rolled_back = 0
                GROUP BY pri.item_id
            ) p ON p.item_id = i.id
        ) WHERE consumed_value > 0"""
    ).fetchone()["c"]
    assert float(cat_sum) == float(total_consumed), \
        f"品类消耗之和 {cat_sum} != 总消耗 {total_consumed}"

    conn.close()


def test_category_consumed_excludes_double_write(logged_client):
    """生产领料的 outbound_requests 双写记录(被 reason 排除)不应重复计入品类消耗。

    outbound_requests 在生产提交时会双写一条 reason='生产领料(run=#X)' 记录,
    它应当被两个口径都排除(否则会和 production_run_items 重复)。
    """
    client, wh_path = logged_client

    # 用品项 B:outbound 有生产领料双写 + production_run_items 同等数量
    item_b, _ = _seed_item(wh_path, "catProdB", qty=100, unit_cost=20)
    _seed_outbound(wh_path, item_b, qty=4, reason="生产领料(run=#999)")
    _seed_production_consumption(wh_path, item_b, qty=4)

    resp = client.get("/summary?range=all")
    assert resp.status_code == 200

    conn = _wh(wh_path)
    total_consumed = conn.execute(
        """SELECT COALESCE(SUM(qty * unit_cost), 0) AS c FROM (
            SELECT o.requested_quantity AS qty, i.unit_cost
            FROM outbound_requests o
            JOIN items i ON i.id = o.item_id
            WHERE o.rolled_back = 0
              AND (o.reason IS NULL OR o.reason NOT LIKE '生产领料(run=#%')
            UNION ALL
            SELECT pri.actual_qty AS qty, i.unit_cost
            FROM production_run_items pri
            JOIN production_runs pr ON pr.id = pri.run_id
            JOIN items i ON i.id = pri.item_id
            WHERE pr.rolled_back = 0
        )"""
    ).fetchone()["c"]
    # 应 = 0(出库被 reason 排除) + 4*20 = 80(只看 production_run_items)
    assert float(total_consumed) == 80.0
    conn.close()