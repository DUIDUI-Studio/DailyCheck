"""Reports and exports.

All report SQL lives here, with one-line 口径注释 above each query so
future maintainers don't drift the metric definition. The SQL was lifted
verbatim from the pre-refactor app.py (commit 940cc22) and audited
against the bug-fix commits (be67465, ecbf509, 2b39862).
"""
from __future__ import annotations

import csv
import io
from datetime import datetime

from flask import Blueprint, render_template, request

from db import get_warehouse_db
from permissions import require_login
from .auth import audit


bp = Blueprint("reports", __name__)


# ---------------------------------------------------------------------------
# Outbound report
# ---------------------------------------------------------------------------

@bp.route("/report/outbound")
@require_login
def outbound():
    """scope=today (default): 出库 today's totals per item.
    scope=all: 出库 daily grid per item across all dates.

    口径:action='出库' 的 stock_movements 累加(取绝对值,因为 delta<0)。
    """
    db = get_warehouse_db()
    scope = request.args.get("scope", "today")
    today = datetime.now().strftime("%Y-%m-%d")

    if scope == "all":
        raw = db.execute(
            """SELECT i.name AS item_name, i.unit, ABS(m.delta) AS qty, m.created_at
               FROM stock_movements m JOIN items i ON i.id = m.item_id
               WHERE m.action = '出库' ORDER BY m.created_at ASC"""
        ).fetchall()
        # 口径:每个品项 × 每个日期的出库量,缺失日补 0
        daily: dict[tuple[str, str], int] = {}
        for r in raw:
            d = r["created_at"][:10]
            key = (r["item_name"], d)
            daily[key] = daily.get(key, 0) + r["qty"]
        all_items_rows = db.execute("SELECT name, unit FROM items ORDER BY id").fetchall()
        item_names_order = [r["name"] for r in all_items_rows]
        all_items = {r["name"]: r["unit"] for r in all_items_rows}
        all_dates = sorted({r["created_at"][:10] for r in raw})
        records = []
        for item in item_names_order:
            row: dict = {"item_name": item, "unit": all_items[item]}
            for d in all_dates:
                row[d] = daily.get((item, d), 0)
            records.append(row)
        return render_template(
            "report_outbound.html", records=records, date=today, scope=scope,
            dates=all_dates,
        )

    records = db.execute(
        """SELECT m.item_id, i.name AS item_name, i.unit,
                  ABS(SUM(m.delta)) AS total_qty, COUNT(*) AS times,
                  MAX(m.created_at) AS last_time
           FROM stock_movements m JOIN items i ON i.id = m.item_id
           WHERE m.action = '出库' AND m.created_at LIKE ? || '%'
           GROUP BY m.item_id ORDER BY last_time DESC""",
        (today,),
    ).fetchall()
    return render_template("report_outbound.html", records=records, date=today, scope=scope)


# ---------------------------------------------------------------------------
# Inbound report
# ---------------------------------------------------------------------------

@bp.route("/report/inbound")
@require_login
def inbound():
    """scope=today: 补货入库 today's totals per item.
    scope=all: daily grid including initial stock (0 by default since
    the initial_quantity column was dropped — see commit 940cc22).

    口径:action='补货入库' 的 stock_movements 累加(取正值,因为 delta>0)。
    """
    db = get_warehouse_db()
    scope = request.args.get("scope", "today")
    today = datetime.now().strftime("%Y-%m-%d")

    if scope == "all":
        raw = db.execute(
            """SELECT i.name AS item_name, i.unit, m.delta AS qty, m.created_at
               FROM stock_movements m JOIN items i ON i.id = m.item_id
               WHERE m.action = '补货入库' ORDER BY m.created_at ASC"""
        ).fetchall()
        # 口径:每个品项 × 每个日期的入库量,缺失日补 0;所有品项都列出
        daily: dict[tuple[str, str], int] = {}
        for r in raw:
            d = r["created_at"][:10]
            key = (r["item_name"], d)
            daily[key] = daily.get(key, 0) + r["qty"]
        all_items_rows = db.execute("SELECT name, unit FROM items ORDER BY id").fetchall()
        item_names_order = [r["name"] for r in all_items_rows]
        all_items = {r["name"]: r["unit"] for r in all_items_rows}
        # 口径:initial_quantity 已从表中移除,初始库存固定为 0
        init_stock = {}
        all_dates = sorted({r["created_at"][:10] for r in raw})
        records = []
        for item in item_names_order:
            row: dict = {"item_name": item, "unit": all_items[item]}
            row["初始库存"] = init_stock.get(item, 0)
            for d in all_dates:
                row[d] = daily.get((item, d), 0)
            records.append(row)
        return render_template(
            "report_inbound.html", records=records, date=today, scope=scope,
            dates=all_dates,
        )

    records = db.execute(
        """SELECT m.item_id, i.name AS item_name, i.unit,
                  SUM(m.delta) AS total_qty, COUNT(*) AS times,
                  MAX(m.created_at) AS last_time
           FROM stock_movements m JOIN items i ON i.id = m.item_id
           WHERE m.action = '补货入库' AND m.created_at LIKE ? || '%'
           GROUP BY m.item_id ORDER BY last_time DESC""",
        (today,),
    ).fetchall()
    return render_template("report_inbound.html", records=records, date=today, scope=scope)


# ---------------------------------------------------------------------------
# Consumption export (CSV with UTF-8 BOM for Excel CN compatibility)
# ---------------------------------------------------------------------------

@bp.route("/export/consumption")
@require_login
def export_consumption():
    """CSV download of consumption report.

    口径:action='出库' 的 stock_movements,按品项聚合消耗数量和金额。
    输出加 UTF-8 BOM(commit 469be4d)以避免 Excel 打开中文乱码。
    """
    db = get_warehouse_db()
    rows = db.execute(
        """SELECT i.name AS item_name, c.name AS category_name,
                  ABS(SUM(m.delta)) AS consumed_qty, i.unit,
                  ROUND(i.unit_cost, 3) AS unit_cost,
                  ROUND(ABS(SUM(m.delta)) * i.unit_cost, 2) AS consumed_value
           FROM stock_movements m
           JOIN items i ON i.id = m.item_id
           JOIN categories c ON c.id = i.category_id
           WHERE m.action = '出库'
           GROUP BY m.item_id ORDER BY c.name, i.name"""
    ).fetchall()
    audit("report.consumption_export", "report", None, {"rows": len(rows)})

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["品类", "品项", "消耗数量", "单位", "单价", "消耗金额"])
    for r in rows:
        writer.writerow([
            r["category_name"], r["item_name"], r["consumed_qty"],
            r["unit"], r["unit_cost"], r["consumed_value"],
        ])

    from flask import current_app
    response = current_app.response_class(
        output.getvalue().encode("utf-8-sig"),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=consumption.csv"},
    )
    return response


# ---------------------------------------------------------------------------
# Revenue upload (daily_revenue)
# ---------------------------------------------------------------------------

@bp.route("/api/revenue", methods=["POST"])
def api_upload_revenue():
    """Accepts date + amount + token via form data.

    Token is read from REVENUE_TOKEN env var. Empty token disables auth —
    this is intentional for the curl-from-Cron use case the original
    app.py shipped.
    """
    import os
    token = os.getenv("REVENUE_TOKEN", "")
    expected = request.form.get("token", "")
    if token and expected != token:
        return "Unauthorized", 401
    date_str = request.form.get("date", "").strip()
    amount_str = request.form.get("amount", "0").strip()
    if not date_str:
        return "Missing date", 400
    try:
        amount = float(amount_str)
    except ValueError:
        return "Invalid amount", 400
    db = get_warehouse_db()
    db.execute(
        """INSERT INTO daily_revenue (date, amount, created_at)
           VALUES (?, ?, ?)
           ON CONFLICT(date) DO UPDATE SET amount = excluded.amount, created_at = excluded.created_at""",
        (date_str, amount, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    db.commit()
    return f"OK {date_str}={amount}"
