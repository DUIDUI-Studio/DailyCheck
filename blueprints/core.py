"""Dashboard, summary, and category redirect (read-only summary screens)."""
from __future__ import annotations

import calendar
import datetime as _dt
from datetime import datetime

from flask import Blueprint, redirect, render_template, request, url_for

from db import get_warehouse_db
from permissions import require_login, require_role
from ._helpers import render


bp = Blueprint("core", __name__)


def _time_clauses(range_param):
    """根据 range 参数生成 SQL 时间子句 + window_days。

    子句不带表别名(只写 `created_at`),由调用方拼前缀(如 `o.created_at` /
    `created_at`)以适配不同查询上下文。
    返回 (time_clause_outbound, time_clause_production, time_clause_restock, window_days)。
    """
    if range_param == "7d":
        return (
            "created_at >= datetime('now','-7 days')",
            "created_at >= datetime('now','-7 days')",
            "created_at >= datetime('now','-7 days')",
            7,
        )
    if range_param == "month":
        ym = _dt.datetime.now().strftime("%Y-%m")
        days = calendar.monthrange(_dt.datetime.now().year, _dt.datetime.now().month)[1]
        return (
            f"created_at LIKE '{ym}%'",
            f"created_at LIKE '{ym}%'",
            f"created_at LIKE '{ym}%'",
            days,
        )
    # "all" — 窗口天数 = 距第一条 stock_movements 的天数(最少 1)
    # 子句返 "1=1"(无列名),调用方拼前缀时跳过(r.1=1 会报 syntax error)
    return ("1=1", "1=1", "1=1", 1)


def _where(clause, alias):
    """把 _time_clauses 返回的子句拼到 WHERE 里。

    子句以「1=1」开头(对应 range=all)时不拼表前缀;
    否则加上 `<alias>.` 前缀,避免与 items.created_at 类列冲突。
    """
    if clause.startswith("1=1"):
        return clause
    return f"{alias}.{clause}"

def _compute_summary_metrics(db, range_param):
    """总体段:进货 / 消耗 / 库存金额 + 反推起点 + 周转率 + 可售天数。

    返回 dict,字段含义见 plan doc。
    与 /summary 共享;被 reports.py 的 CSV 导出复用。
    """
    tco, tcp, tcr, window_days = _time_clauses(range_param)

    total_inbound_value = float(db.execute(
        f"""SELECT COALESCE(SUM(r.requested_quantity * i.unit_cost), 0) AS c
            FROM restock_requests r
            JOIN items i ON i.id = r.item_id
            WHERE {_where(tcr, 'r')}"""
    ).fetchone()["c"])

    consumed_outbound = float(db.execute(
        f"""SELECT COALESCE(SUM(o.requested_quantity * i.unit_cost), 0) AS c
            FROM outbound_requests o
            JOIN items i ON i.id = o.item_id
            WHERE o.rolled_back = 0
              AND (o.reason IS NULL OR o.reason NOT LIKE '生产领料(run=#%')
              AND {_where(tco, 'o')}"""
    ).fetchone()["c"])
    consumed_production = float(db.execute(
        f"""SELECT COALESCE(SUM(pri.actual_qty * i.unit_cost), 0) AS c
            FROM production_run_items pri
            JOIN production_runs pr ON pr.id = pri.run_id
            JOIN items i ON i.id = pri.item_id
            WHERE pr.rolled_back = 0
              AND {_where(tcp, 'pr')}"""
    ).fetchone()["c"])
    total_consumed_value = consumed_outbound + consumed_production

    end_value = float(db.execute(
        "SELECT COALESCE(SUM(quantity * unit_cost), 0) AS c FROM items"
    ).fetchone()["c"])

    # 反推窗口起始库存金额
    if range_param == "7d":
        start_filter = "m.created_at >= datetime('now','-7 days')"
    elif range_param == "month":
        ym = _dt.datetime.now().strftime("%Y-%m")
        start_filter = f"m.created_at LIKE '{ym}%'"
    else:
        first = db.execute(
            "SELECT MIN(created_at) AS d FROM stock_movements"
        ).fetchone()["d"]
        start_filter = f"m.created_at >= '{first}'" if first else None

    if start_filter is None:
        start_value = end_value
    else:
        rows = db.execute(
            f"""SELECT i.id, i.quantity, i.unit_cost,
                       COALESCE(SUM(m.delta), 0) AS d
                FROM items i
                LEFT JOIN stock_movements m
                  ON m.item_id = i.id AND {start_filter}
                GROUP BY i.id"""
        ).fetchall()
        start_value = 0.0
        for r in rows:
            qty_start = float(r["quantity"]) - float(r["d"])
            if qty_start < 0:
                qty_start = 0
            start_value += qty_start * float(r["unit_cost"])

    avg_stock_value = (start_value + end_value) / 2

    if avg_stock_value > 0 and window_days > 0:
        turnover = round(total_consumed_value / avg_stock_value, 2)
        daily_consume = total_consumed_value / window_days
        turnover_days = round(avg_stock_value / daily_consume, 1) if daily_consume > 0 else None
    else:
        turnover = 0.0
        turnover_days = None

    return {
        "total_inbound_value": total_inbound_value,
        "total_consumed_value": total_consumed_value,
        "total_stock_value": end_value,
        "start_value": start_value,
        "end_value": end_value,
        "avg_stock_value": avg_stock_value,
        "turnover": turnover,
        "turnover_days": turnover_days,
        "window_days": window_days,
    }


def _compute_category_stats(db, range_param):
    """品类段:进货 / 消耗 / 库存金额 + 反推起点 + 周转率(与 /summary 共享)。"""
    tco, tcp, tcr, window_days = _time_clauses(range_param)

    cat_data = db.execute(
        f"""SELECT
              c.id AS category_id,
              c.name AS category_name,
              COALESCE(SUM(item_vals.restock_value), 0) AS restock_value,
              COALESCE(SUM(item_vals.consumed_value), 0) AS consumed_value,
              COALESCE(SUM(item_vals.current_stock_value), 0) AS stock_value
           FROM categories c
           LEFT JOIN (
              SELECT
                  i.category_id,
                  COALESCE(r.total_restock, 0) * i.unit_cost AS restock_value,
                  (COALESCE(o.total_outbound, 0) + COALESCE(p.total_production, 0)) * i.unit_cost AS consumed_value,
                  i.quantity * i.unit_cost AS current_stock_value
              FROM items i
              LEFT JOIN (
                  SELECT item_id, SUM(requested_quantity) AS total_restock
                  FROM restock_requests r
                  WHERE {_where(tcr, 'r')}
                  GROUP BY item_id
              ) r ON r.item_id = i.id
              LEFT JOIN (
                  SELECT item_id, SUM(requested_quantity) AS total_outbound
                  FROM outbound_requests o
                  WHERE o.rolled_back = 0
                    AND (o.reason IS NULL OR o.reason NOT LIKE '生产领料(run=#%')
                    AND {_where(tco, 'o')}
                  GROUP BY item_id
              ) o ON o.item_id = i.id
              LEFT JOIN (
                  SELECT pri.item_id, SUM(pri.actual_qty) AS total_production
                  FROM production_run_items pri
                  JOIN production_runs pr ON pr.id = pri.run_id
                  WHERE pr.rolled_back = 0
                    AND {_where(tcp, 'pr')}
                  GROUP BY pri.item_id
              ) p ON p.item_id = i.id
           ) item_vals ON item_vals.category_id = c.id
           GROUP BY c.id, c.name ORDER BY c.id"""
    ).fetchall()

    # 反推品类起点库存金额
    if range_param == "7d":
        cat_start_filter = "sm.created_at >= datetime('now','-7 days')"
    elif range_param == "month":
        ym = _dt.datetime.now().strftime("%Y-%m")
        cat_start_filter = f"sm.created_at LIKE '{ym}%'"
    else:
        first = db.execute(
            "SELECT MIN(created_at) AS d FROM stock_movements"
        ).fetchone()["d"]
        cat_start_filter = f"sm.created_at >= '{first}'" if first else None

    if cat_start_filter:
        cat_start_rows = db.execute(
            f"""SELECT i.category_id AS cid,
                       COALESCE(SUM((i.quantity - sm.delta) * i.unit_cost), 0) AS start_value
                FROM items i
                LEFT JOIN stock_movements sm
                  ON sm.item_id = i.id AND {cat_start_filter}
                GROUP BY i.category_id"""
        ).fetchall()
        cat_start_map = {r["cid"]: float(r["start_value"]) for r in cat_start_rows}
    else:
        cat_start_map = {}

    enriched = []
    for row in cat_data:
        consumed_v = round(float(row["consumed_value"]), 2)
        stock_v = round(float(row["stock_value"]), 2)
        cid = row["category_id"]
        start_v = cat_start_map.get(cid, stock_v)
        if start_v < 0:
            start_v = 0
        avg = (start_v + stock_v) / 2
        cat_turnover = round(consumed_v / avg, 2) if avg > 0 and consumed_v > 0 else None
        enriched.append({
            "category_id": cid,
            "category_name": row["category_name"],
            "inbound_value": round(float(row["restock_value"]), 2),
            "consumed_value": consumed_v,
            "stock_value": stock_v,
            "start_value": round(start_v, 2),
            "avg_stock_value": round(avg, 2),
            "turnover": cat_turnover,
        })
    return enriched


def _compute_top_consumed(db, range_param):
    """消耗 Top 10(只取 outbound,与原 /summary 同口径;production 暂不参与 Top)。"""
    tco, _, _, _ = _time_clauses(range_param)
    return db.execute(
        f"""SELECT i.name AS item_name, c.name AS category_name,
                  o.total_qty AS consumed_qty, i.unit,
                  ROUND(o.total_qty * i.unit_cost, 2) AS consumed_value
           FROM (
               SELECT item_id, SUM(requested_quantity) AS total_qty
               FROM outbound_requests o
               WHERE o.rolled_back = 0
                 AND (o.reason IS NULL OR o.reason NOT LIKE '生产领料(run=#%')
                 AND {_where(tco, 'o')}
               GROUP BY o.item_id
           ) o
           JOIN items i ON i.id = o.item_id
           JOIN categories c ON c.id = i.category_id
           ORDER BY o.total_qty DESC LIMIT 10"""
    ).fetchall()


@bp.route("/dashboard")
@require_login
def dashboard():
    db = get_warehouse_db()
    total_items = db.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"]
    total_categories = db.execute("SELECT COUNT(*) AS c FROM categories").fetchone()["c"]
    low_stock = db.execute(
        "SELECT COUNT(*) AS c FROM items WHERE quantity <= safety_stock"
    ).fetchone()["c"]
    pending_requests = db.execute(
        "SELECT COUNT(*) AS c FROM restock_requests WHERE status = '提交'"
    ).fetchone()["c"]
    today = datetime.now().strftime("%Y-%m-%d")
    outbound_today = db.execute(
        "SELECT COUNT(*) AS c FROM outbound_requests WHERE created_at LIKE ? || '%'",
        (today,),
    ).fetchone()["c"]
    inbound_today = db.execute(
        """SELECT COUNT(*) AS c FROM stock_movements
           WHERE action = '补货入库' AND created_at LIKE ? || '%'""",
        (today,),
    ).fetchone()["c"]
    latest_movements = db.execute(
        """SELECT m.created_at, m.action, m.delta, i.name AS item_name
           FROM stock_movements m
           JOIN items i ON i.id = m.item_id
           ORDER BY m.id DESC LIMIT 8"""
    ).fetchall()
    return render_template(
        "dashboard.html",
        total_items=total_items,
        total_categories=total_categories,
        low_stock=low_stock,
        pending_requests=pending_requests,
        outbound_today=outbound_today,
        inbound_today=inbound_today,
        latest_movements=latest_movements,
    )


@bp.route("/")
@bp.route("/land")
@require_login
def land():
    """Post-warehouse-pick landing page: choose between 库存管理 and 生产录入."""
    return render("land.html", no_sidebar=True)


@bp.route("/summary")
@require_login
def summary():
    db = get_warehouse_db()

    # range 参数:7d(默认)/ month / all
    range_param = request.args.get("range", "7d")
    if range_param not in ("7d", "month", "all"):
        range_param = "7d"

    metrics = _compute_summary_metrics(db, range_param)
    cat_stats = _compute_category_stats(db, range_param)
    top = _compute_top_consumed(db, range_param)

    total_revenue = float(db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS c FROM daily_revenue"
    ).fetchone()["c"])

    range_label = {"7d": "7 日滚动", "month": "当月", "all": "全部"}[range_param]
    return render_template(
        "summary.html",
        total_inbound_value=round(metrics["total_inbound_value"], 2),
        total_consumed_value=round(metrics["total_consumed_value"], 2),
        total_stock_value=round(metrics["total_stock_value"], 2),
        total_revenue=round(total_revenue, 2),
        category_stats=cat_stats,
        top_consumed=top,
        range=range_param,
        range_label=range_label,
        turnover=metrics["turnover"],
        turnover_days=metrics["turnover_days"],
    )


@bp.route("/categories", methods=["GET"])
@require_login
def categories():
    # Categories are now part of the merged /items page (admin only).
    return redirect(url_for("items.items_list") + "#categories")


@bp.route("/categories/<int:category_id>/delete", methods=["POST"])
@require_role("staff")
def delete_category(category_id: int):
    from flask import flash
    flash("品类为系统固定项，不支持删除")
    return redirect(url_for("items.items_list") + "#categories")
