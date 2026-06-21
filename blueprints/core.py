"""Dashboard, summary, and category redirect (read-only summary screens)."""
from __future__ import annotations

from datetime import datetime

from flask import Blueprint, redirect, render_template, request, url_for

from db import get_warehouse_db
from permissions import require_login, require_role
from ._helpers import fixed_categories_in_clause, render


bp = Blueprint("core", __name__)


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

    # 口径:进货金额 = 全部 restock_requests(被删除的不算) × 单价
    # 用 restock_requests 而不是 stock_movements,因为后者把
    # '补货删除回滚' 写为独立 action,会与 '补货入库' 抵消混乱。
    # restock_requests 是用户意图的真理源(被删除就不在表里)。
    total_inbound_value = db.execute(
        """SELECT COALESCE(SUM(r.requested_quantity * i.unit_cost), 0) AS c
           FROM restock_requests r
           JOIN items i ON i.id = r.item_id"""
    ).fetchone()["c"]

    # 口径:消耗金额 = 出库(rolled_back=0, 排除生产领料) + 生产消耗(pr.rolled_back=0)
    # outbound_requests 已经双写了生产领料(reason='生产领料(run=#X)'),所以这里要排除,
    # 否则会被生产消耗重复计算。
    consumed_outbound = db.execute(
        """SELECT COALESCE(SUM(o.requested_quantity * i.unit_cost), 0) AS c
           FROM outbound_requests o
           JOIN items i ON i.id = o.item_id
           WHERE o.rolled_back = 0
             AND (o.reason IS NULL OR o.reason NOT LIKE '生产领料(run=#%')"""
    ).fetchone()["c"]
    consumed_production = db.execute(
        """SELECT COALESCE(SUM(pri.actual_qty * i.unit_cost), 0) AS c
           FROM production_run_items pri
           JOIN production_runs pr ON pr.id = pri.run_id
           JOIN items i ON i.id = pri.item_id
           WHERE pr.rolled_back = 0"""
    ).fetchone()["c"]
    total_consumed_value = float(consumed_outbound) + float(consumed_production)

    # 口径:库存金额 = 当前 quantity × unit_cost(账面)
    total_stock_value = db.execute(
        "SELECT COALESCE(SUM(quantity * unit_cost), 0) AS c FROM items"
    ).fetchone()["c"]

    total_revenue = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS c FROM daily_revenue"
    ).fetchone()["c"]

    # 口径:按品类统计 — 同样基于 restock_requests / outbound_requests
    cat_data = db.execute(
        """SELECT
              c.name AS category_name,
              COALESCE(SUM(item_vals.restock_value), 0) AS restock_value,
              COALESCE(SUM(item_vals.consumed_value), 0) AS consumed_value,
              COALESCE(SUM(item_vals.current_stock_value), 0) AS stock_value
           FROM categories c
           LEFT JOIN (
              SELECT
                  i.category_id,
                  COALESCE(r.total_restock, 0) * i.unit_cost AS restock_value,
                  COALESCE(o.total_outbound, 0) * i.unit_cost AS consumed_value,
                  i.quantity * i.unit_cost AS current_stock_value
              FROM items i
              LEFT JOIN (
                  SELECT item_id, SUM(requested_quantity) AS total_restock
                  FROM restock_requests GROUP BY item_id
              ) r ON r.item_id = i.id
              LEFT JOIN (
                  SELECT item_id, SUM(requested_quantity) AS total_outbound
                  FROM outbound_requests WHERE rolled_back = 0 GROUP BY item_id
              ) o ON o.item_id = i.id
           ) item_vals ON item_vals.category_id = c.id
           GROUP BY c.id, c.name ORDER BY c.id"""
    ).fetchall()

    enriched_stats = []
    for row in cat_data:
        enriched_stats.append({
            "category_name": row["category_name"],
            # backward-compat: old code returned a single "inbound_value"
            # combining init_value + restock_value. init_value is 0
            # since the column was dropped (commit 940cc22), so the sum
            # equals restock_value. Keep the same key.
            "inbound_value": round(row["restock_value"], 2),
            "consumed_value": round(row["consumed_value"], 2),
            "stock_value": round(row["stock_value"], 2),
        })

    top_consumed = db.execute(
        """SELECT i.name AS item_name, c.name AS category_name,
                  o.total_qty AS consumed_qty, i.unit,
                  ROUND(o.total_qty * i.unit_cost, 2) AS consumed_value
           FROM (
               SELECT item_id, SUM(requested_quantity) AS total_qty
               FROM outbound_requests WHERE rolled_back = 0
               GROUP BY item_id
           ) o
           JOIN items i ON i.id = o.item_id
           JOIN categories c ON c.id = i.category_id
           ORDER BY o.total_qty DESC"""
    ).fetchall()

    return render_template(
        "summary.html",
        total_inbound_value=round(total_inbound_value, 2),
        total_consumed_value=round(total_consumed_value, 2),
        total_stock_value=round(total_stock_value, 2),
        total_revenue=round(total_revenue, 2),
        category_stats=enriched_stats,
        top_consumed=top_consumed,
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
