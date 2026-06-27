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
               WHERE m.action IN ('出库', '生产消耗') ORDER BY m.created_at ASC"""
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
           WHERE m.action IN ('出库', '生产消耗') AND m.created_at LIKE ? || '%'
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
           WHERE m.action IN ('出库', '生产消耗')
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


# ---------------------------------------------------------------------------
# Summary export (CSV with three sections)
# ---------------------------------------------------------------------------

@bp.route("/summary/export")
@require_login
def export_summary():
    """CSV 三段导出:总体 / 按品类 / 消耗 Top。

    与 /summary 共享 range 参数(7d|month|all,默认 7d)。
    输出 UTF-8 BOM 兼容 Excel 中文。
    """
    import calendar
    import datetime as _dt

    db = get_warehouse_db()
    range_param = request.args.get("range", "7d")
    if range_param not in ("7d", "month", "all"):
        range_param = "7d"

    if range_param == "7d":
        time_clause_outbound = "created_at >= datetime('now','-7 days')"
        time_clause_production = "created_at >= datetime('now','-7 days')"
        time_clause_restock = "created_at >= datetime('now','-7 days')"
        window_days = 7
        range_label = "7 日"
    elif range_param == "month":
        ym = _dt.datetime.now().strftime("%Y-%m")
        time_clause_outbound = f"created_at LIKE '{ym}%'"
        time_clause_production = f"created_at LIKE '{ym}%'"
        time_clause_restock = f"created_at LIKE '{ym}%'"
        window_days = calendar.monthrange(_dt.datetime.now().year, _dt.datetime.now().month)[1]
        range_label = "当月"
    else:
        time_clause_outbound = "1=1"
        time_clause_production = "1=1"
        time_clause_restock = "1=1"
        first = db.execute("SELECT MIN(created_at) AS d FROM stock_movements").fetchone()["d"]
        if first:
            window_days = max(1, (_dt.datetime.now() - _dt.datetime.strptime(first[:10], "%Y-%m-%d")).days)
        else:
            window_days = 1
        range_label = "全部"

    # 段 1:总体
    total_inbound = float(db.execute(
        f"""SELECT COALESCE(SUM(r.requested_quantity * i.unit_cost), 0) AS c
            FROM restock_requests r
            JOIN items i ON i.id = r.item_id
            WHERE {time_clause_restock}"""
    ).fetchone()["c"])
    consumed_outbound = float(db.execute(
        f"""SELECT COALESCE(SUM(o.requested_quantity * i.unit_cost), 0) AS c
            FROM outbound_requests o
            JOIN items i ON i.id = o.item_id
            WHERE o.rolled_back = 0
              AND (o.reason IS NULL OR o.reason NOT LIKE '生产领料(run=#%')
              AND {time_clause_outbound}"""
    ).fetchone()["c"])
    consumed_production = float(db.execute(
        f"""SELECT COALESCE(SUM(pri.actual_qty * i.unit_cost), 0) AS c
            FROM production_run_items pri
            JOIN production_runs pr ON pr.id = pri.run_id
            JOIN items i ON i.id = pri.item_id
            WHERE pr.rolled_back = 0
              AND {time_clause_production}"""
    ).fetchone()["c"])
    total_consumed = consumed_outbound + consumed_production
    total_stock = float(db.execute(
        "SELECT COALESCE(SUM(quantity * unit_cost), 0) AS c FROM items"
    ).fetchone()["c"])
    if total_stock > 0 and window_days > 0 and total_consumed > 0:
        turnover = total_consumed / total_stock
        turnover_days = round(total_stock / (total_consumed / window_days), 1)
        turnover_str = f"{turnover:.2f}"
        days_str = f"{turnover_days}"
    else:
        turnover_str = "0.00"
        days_str = "—"

    # 段 2:按品类(outbound 无生产领料 + production_run_items,口径与 /summary 一致)
    cat_rows = db.execute(
        f"""SELECT c.name AS category_name,
                  COALESCE(SUM(r.total_restock * i.unit_cost), 0) AS inbound_value,
                  COALESCE(SUM((o.total_outbound + COALESCE(p.total_production, 0)) * i.unit_cost), 0) AS consumed_value,
                  COALESCE(SUM(i.quantity * i.unit_cost), 0) AS stock_value
           FROM categories c
           LEFT JOIN items i ON i.category_id = c.id
           LEFT JOIN (
               SELECT item_id, SUM(requested_quantity) AS total_restock
               FROM restock_requests WHERE {time_clause_restock}
               GROUP BY item_id
           ) r ON r.item_id = i.id
           LEFT JOIN (
               SELECT item_id, SUM(requested_quantity) AS total_outbound
               FROM outbound_requests
               WHERE rolled_back = 0
                 AND (reason IS NULL OR reason NOT LIKE '生产领料(run=#%')
                 AND {time_clause_outbound}
               GROUP BY item_id
           ) o ON o.item_id = i.id
           LEFT JOIN (
               SELECT pri.item_id, SUM(pri.actual_qty) AS total_production
               FROM production_run_items pri
               JOIN production_runs pr ON pr.id = pri.run_id
               WHERE pr.rolled_back = 0
                 AND {time_clause_production}
               GROUP BY pri.item_id
           ) p ON p.item_id = i.id
           GROUP BY c.id, c.name ORDER BY c.id"""
    ).fetchall()

    # 段 3:消耗 Top 10
    top_rows = db.execute(
        f"""SELECT i.name AS item_name, c.name AS category_name,
                  o.total_qty AS consumed_qty, i.unit,
                  ROUND(o.total_qty * i.unit_cost, 2) AS consumed_value
           FROM (
               SELECT item_id, SUM(requested_quantity) AS total_qty
               FROM outbound_requests
               WHERE rolled_back = 0
                 AND (reason IS NULL OR reason NOT LIKE '生产领料(run=#%')
                 AND {time_clause_outbound}
               GROUP BY item_id
           ) o
           JOIN items i ON i.id = o.item_id
           JOIN categories c ON c.id = i.category_id
           ORDER BY o.total_qty DESC LIMIT 10"""
    ).fetchall()

    # 写 CSV(三段以空行分隔)
    output = io.StringIO()
    w = csv.writer(output)

    w.writerow(["范围", "进货金额", "消耗金额", "当前库存金额", "周转率", "可售天数"])
    w.writerow([range_label, f"{total_inbound:.2f}", f"{total_consumed:.2f}",
                f"{total_stock:.2f}", turnover_str, days_str])
    w.writerow([])

    w.writerow(["品类", "进货金额", "消耗金额", "库存金额", "周转率"])
    for cat in cat_rows:
        cat_consumed = float(cat["consumed_value"])
        cat_stock = float(cat["stock_value"])
        # 注:CSV 这里用的是 consumed / stock_value(当前库存金额),
        # /summary 页用的是 consumed / avg(start+end 平均)。
        # 两者口径不同(CSV 简化未反推起点),v2 统一为 avg。
        if cat_stock > 0 and cat_consumed > 0:
            cat_turnover = f"{cat_consumed / cat_stock:.2f}"
        else:
            cat_turnover = "—"
        w.writerow([
            cat["category_name"],
            f"{float(cat['inbound_value']):.2f}",
            f"{cat_consumed:.2f}",
            f"{cat_stock:.2f}",
            cat_turnover,
        ])
    w.writerow([])

    w.writerow(["品类", "品项", "消耗数量", "单位", "消耗金额"])
    for r in top_rows:
        w.writerow([
            r["category_name"], r["item_name"],
            fmt_qty(r["consumed_qty"]), r["unit"],
            f"{float(r['consumed_value']):.2f}",
        ])

    filename = f"summary-{_dt.datetime.now().strftime('%Y-%m-%d')}-{range_param}.csv"
    from flask import current_app
    return current_app.response_class(
        output.getvalue().encode("utf-8-sig"),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename={filename}"},
    )


def fmt_qty(value):
    """简化版 fmt_qty,避免循环导入。"""
    if value is None:
        return "0"
    s = f"{float(value):.2f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"
