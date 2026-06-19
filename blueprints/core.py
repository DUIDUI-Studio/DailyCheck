"""Dashboard, summary, and category redirect (read-only summary screens)."""
from __future__ import annotations

from datetime import datetime

from flask import Blueprint, redirect, render_template, request, url_for

from db import get_warehouse_db
from permissions import require_login, require_role
from ._helpers import fixed_categories_in_clause


bp = Blueprint("core", __name__)


@bp.route("/")
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


@bp.route("/summary")
@require_login
def summary():
    db = get_warehouse_db()

    # 口径:进货金额 = 补货入库金额(initial_quantity 历史遗留字段已移除)
    total_inbound_value = db.execute(
        """SELECT COALESCE(SUM(i.unit_cost * sub.inbound_qty), 0) AS c
           FROM items i
           LEFT JOIN (
               SELECT m.item_id, SUM(m.delta) AS inbound_qty
               FROM stock_movements m WHERE m.action = '补货入库' GROUP BY m.item_id
           ) sub ON sub.item_id = i.id"""
    ).fetchone()["c"]

    # 口径:消耗金额 = 出库流水数量 × 单价(取绝对值)
    total_consumed_value = db.execute(
        """SELECT COALESCE(SUM(ABS(m.delta) * i.unit_cost), 0) AS c
           FROM stock_movements m
           JOIN items i ON i.id = m.item_id
           WHERE m.action = '出库'"""
    ).fetchone()["c"]

    # 口径:库存金额 = 当前 quantity × unit_cost(账面)
    total_stock_value = db.execute(
        "SELECT COALESCE(SUM(quantity * unit_cost), 0) AS c FROM items"
    ).fetchone()["c"]

    total_revenue = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS c FROM daily_revenue"
    ).fetchone()["c"]

    # 口径:按品类统计 — 先按 item 聚合再 join 品类,避免多对多行重复
    cat_data = db.execute(
        """SELECT
              c.name AS category_name,
              COALESCE(SUM(item_vals.init_value), 0) AS init_value,
              COALESCE(SUM(item_vals.restock_value), 0) AS restock_value,
              COALESCE(SUM(item_vals.consumed_value), 0) AS consumed_value,
              COALESCE(SUM(item_vals.current_stock_value), 0) AS stock_value
           FROM categories c
           LEFT JOIN (
              SELECT
                  i.category_id,
                  0 AS init_value,
                  COALESCE(sub.inbound_qty, 0) * i.unit_cost AS restock_value,
                  COALESCE(out.consumed_qty, 0) * i.unit_cost AS consumed_value,
                  i.quantity * i.unit_cost AS current_stock_value
              FROM items i
              LEFT JOIN (SELECT item_id, SUM(delta) AS inbound_qty
                         FROM stock_movements WHERE action = '补货入库' GROUP BY item_id) sub
                  ON sub.item_id = i.id
              LEFT JOIN (SELECT item_id, SUM(ABS(delta)) AS consumed_qty
                         FROM stock_movements WHERE action = '出库' GROUP BY item_id) out
                  ON out.item_id = i.id
           ) item_vals ON item_vals.category_id = c.id
           GROUP BY c.id, c.name ORDER BY c.id"""
    ).fetchall()

    enriched_stats = []
    for row in cat_data:
        enriched_stats.append({
            "category_name": row["category_name"],
            "inbound_value": round(row["init_value"] + row["restock_value"], 2),
            "consumed_value": round(row["consumed_value"], 2),
            "stock_value": round(row["stock_value"], 2),
        })

    top_consumed = db.execute(
        """SELECT i.name AS item_name, c.name AS category_name,
                  ABS(SUM(m.delta)) AS consumed_qty, i.unit,
                  ROUND(ABS(SUM(m.delta)) * i.unit_cost, 2) AS consumed_value
           FROM stock_movements m
           JOIN items i ON i.id = m.item_id
           JOIN categories c ON c.id = i.category_id
           WHERE m.action = '出库'
           GROUP BY m.item_id ORDER BY consumed_qty DESC"""
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
@require_role("manager")
def delete_category(category_id: int):
    from flask import flash
    flash("品类为系统固定项，不支持删除")
    return redirect(url_for("items.items_list") + "#categories")
