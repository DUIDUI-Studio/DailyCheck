"""Reports and exports.

All report SQL lives here, with one-line 口径注释 above each query so
future maintainers don't drift the metric definition. The SQL was lifted
verbatim from the pre-refactor app.py (commit 940cc22) and audited
against the bug-fix commits (be67465, ecbf509, 2b39862).
"""
from __future__ import annotations

import csv
import datetime as _dt
import io
from datetime import datetime

from flask import Blueprint, g, render_template, request

import reports_health_pure as hp
from db import get_master_db, get_warehouse_db
from permissions import current_permissions, require_login, require_permission, require_role

from .auth import audit
from .core import _compute_category_stats, _compute_summary_metrics
from .cost_redaction_pure import mask_cost_csv_row

bp = Blueprint("reports", __name__)

# 成本列索引（0 基）。无 report.cost.view 时这些列在 CSV 里留空。
_CONSUMPTION_COST_COLS = {4, 5}          # 单价 / 消耗金额
_SUMMARY_TOTAL_COST_COLS = {1, 2, 3, 4}  # 进货/消耗/库存金额 + 周转率
_SUMMARY_CAT_COST_COLS = {1, 2, 3, 4, 5}  # 品类段：全部金额列 + 周转率
_SUMMARY_TOP_COST_COLS = {4}             # 消耗 Top 段：消耗金额


def _cost_visible() -> bool:
    """调用者是否可见成本金额：平台超管 或 持 report.cost.view。"""
    return bool(g.user["is_admin"] or "report.cost.view" in current_permissions())


# ---------------------------------------------------------------------------
# Outbound report
# ---------------------------------------------------------------------------

@bp.route("/report/outbound")
@require_permission("report.view")
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
@require_permission("report.view")
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
@require_permission("report.export")
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

    # 成本脱敏：无 report.cost.view 者，单价 / 消耗金额两列留空（列结构不变）。
    allow_cost = _cost_visible()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["品类", "品项", "消耗数量", "单位", "单价", "消耗金额"])
    for r in rows:
        writer.writerow(mask_cost_csv_row([
            r["category_name"], r["item_name"], r["consumed_qty"],
            r["unit"], r["unit_cost"], r["consumed_value"],
        ], _CONSUMPTION_COST_COLS, allow_cost))

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
@require_login
@require_role("manager")
def api_upload_revenue():
    """Upsert daily_revenue (date + amount) for the current warehouse.

    Mutates a business metric, so it requires an authenticated manager —
    plain staff must not be able to write it. If REVENUE_TOKEN is set in
    the environment it is additionally enforced for scripted callers, using
    a constant-time compare; when unset, the session role check above is the
    sole gate. Note: a token-only caller has no session, so the endpoint
    cannot be used directly from cron — use scripts/sync_revenue.py, which
    writes via SSH instead.
    """
    import hmac
    import os
    token = os.getenv("REVENUE_TOKEN", "")
    if token:
        expected = request.form.get("token", "")
        if not hmac.compare_digest(expected, token):
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
@require_permission("report.export")
def export_summary():
    """CSV 三段导出:总体 / 按品类 / 消耗 Top。

    与 /summary 共享 range 参数(7d|month|all,默认 7d)。
    输出 UTF-8 BOM 兼容 Excel 中文。
    """
    import datetime as _dt

    db = get_warehouse_db()
    range_param = request.args.get("range", "7d")
    if range_param not in ("7d", "month", "all"):
        range_param = "7d"
    range_label = {"7d": "7 日", "month": "当月", "all": "全部"}[range_param]

    # 总体段:复用 /summary 的同源函数,周转率与可售天数口径完全一致。
    metrics = _compute_summary_metrics(db, range_param)
    total_inbound = metrics["total_inbound_value"]
    total_consumed = metrics["total_consumed_value"]
    total_stock = metrics["total_stock_value"]
    turnover_str = f"{metrics['turnover']:.2f}" if metrics["turnover"] else "0.00"
    days_str = f"{metrics['turnover_days']}" if metrics["turnover_days"] is not None else "—"

    # 品类段:复用 /summary 的同源函数,周转率口径 = consumed / avg(start+end),
    # 与页面完全一致。
    cat_stats = _compute_category_stats(db, range_param)

    # 段 3:消耗 Top 10(历史口径:只取 outbound,与 /summary 同源;production 暂不参与 Top)
    from .core import _time_clauses, _where
    tco, _, _, _ = _time_clauses(range_param)
    top_rows = db.execute(
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

    # 写 CSV(三段以空行分隔)。成本脱敏：无 report.cost.view 者，全部金额列与
    # 成本派生的周转率留空（可售天数=天数，非成本，保留）。
    allow_cost = _cost_visible()

    output = io.StringIO()
    w = csv.writer(output)

    w.writerow(["范围", "进货金额", "消耗金额", "当前库存金额", "周转率", "可售天数"])
    w.writerow(mask_cost_csv_row(
        [range_label, f"{total_inbound:.2f}", f"{total_consumed:.2f}",
         f"{total_stock:.2f}", turnover_str, days_str],
        _SUMMARY_TOTAL_COST_COLS, allow_cost))
    w.writerow([])

    # 品类段:周转率与 /summary 同源(avg(start+end) 反推),
    # 增加「平均库存金额」列让消费者看到分母本身。
    w.writerow(["品类", "进货金额", "消耗金额", "库存金额", "平均库存金额", "周转率"])
    for cat in cat_stats:
        cat_turnover = f"{cat['turnover']:.2f}" if cat["turnover"] is not None else "—"
        w.writerow(mask_cost_csv_row([
            cat["category_name"],
            f"{cat['inbound_value']:.2f}",
            f"{cat['consumed_value']:.2f}",
            f"{cat['stock_value']:.2f}",
            f"{cat['avg_stock_value']:.2f}",
            cat_turnover,
        ], _SUMMARY_CAT_COST_COLS, allow_cost))
    w.writerow([])

    w.writerow(["品类", "品项", "消耗数量", "单位", "消耗金额"])
    for r in top_rows:
        w.writerow(mask_cost_csv_row([
            r["category_name"], r["item_name"],
            fmt_qty(r["consumed_qty"]), r["unit"],
            f"{float(r['consumed_value']):.2f}",
        ], _SUMMARY_TOP_COST_COLS, allow_cost))

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


# ---------------------------------------------------------------------------
# Inventory health dashboard (/report/health)
# ---------------------------------------------------------------------------

HEALTH_RANGES = {"7d": 7, "30d": 30, "all": None}


def _health_cutoff(now: datetime, range_param: str) -> tuple[str, int]:
    """range → (cutoff 时间串, 窗口天数)。all 的 cutoff 取远古，窗口按首条流水。

    口径铁律：Python 侧 now() 绑定参数，不用 SQL datetime('now')（时区坑）。
    """
    days = HEALTH_RANGES[range_param]
    if days is None:
        return "1970-01-01 00:00:00", None
    return (now - _dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S"), days


def _trend_polyline(trend: list[dict], width: int = 280, height: int = 70):
    """趋势点 → (polyline points 串, 是否全零)。服务端算好，模板零 JS。"""
    values = [t["value"] for t in trend]
    vmax = max(values) if values else 0
    if vmax <= 0:
        return "", True
    n = len(values)
    pts = []
    for i, v in enumerate(values):
        x = round(i * width / (n - 1), 1) if n > 1 else 0
        y = round(height - 6 - (v / vmax) * (height - 14), 1)
        pts.append(f"{x},{y}")
    return " ".join(pts), False


def _build_health_context(range_param: str, sort_key: str) -> dict:
    """汇总健康报表全部上下文（页面与 CSV 共用，保证两出口口径零漂移）。"""
    from flask import g

    now = datetime.now()
    if range_param not in HEALTH_RANGES:
        range_param = "7d"
    cutoff, window_days = _health_cutoff(now, range_param)

    db = get_warehouse_db()
    master = get_master_db()
    wh = g.get("warehouse")
    wh_code = wh["code"] if wh else None

    # KPI 周转/可售天数：复用 /summary 同源函数，零新口径
    metrics = _compute_summary_metrics(db, range_param)

    levels = hp.compute_inventory_levels(db)
    stale_dos = hp.compute_stale_and_dos(db, now)
    abc = hp.compute_abc(db, cutoff)
    trend = hp.compute_trend(db, now)
    restock_h = hp.compute_restock_health(db, cutoff)
    fulfill = hp.compute_fulfillment(master, wh_code, now)
    stocktake = hp.compute_stocktake_quality(db, now)
    production = hp.compute_production(db, cutoff)
    sku_rows = hp.build_sku_table(db, stale_dos, abc, cutoff, sort_key)

    stale_rows = [r for r in sku_rows if r["stale_qty"] > 1e-9]
    overstock_rows = [r for r in sku_rows
                      if r["stale_qty"] <= 1e-9 and r["dos"] is not None
                      and r["dos"] > hp.DOS_THRESHOLD_DAYS]
    stale_value = round(sum(r["stale_value"] for r in stale_rows), 2)
    overstock_value = round(sum(r["stock_value"] for r in overstock_rows), 2)
    abc_counts = {"A": 0, "B": 0, "C": 0}
    for info in abc.values():
        abc_counts[info["cls"]] += 1

    points, trend_empty = _trend_polyline(trend)
    trend_daily_avg = (round(sum(t["value"] for t in trend) / len(trend), 2)
                       if trend else None)
    trend_total = round(sum(t["value"] for t in trend), 2) if trend else None

    range_days = HEALTH_RANGES[range_param] or metrics["window_days"]
    return {
        "range": range_param,
        "range_label": {"7d": "近 7 日", "30d": "近 30 日", "all": "全部"}[range_param],
        "sort": sort_key,
        "metrics": metrics,
        "levels": levels,
        "abc": abc,
        "abc_counts": abc_counts,
        "trend": trend,
        "trend_points": points,
        "trend_empty": trend_empty,
        "trend_daily_avg": trend_daily_avg,
        "trend_total": trend_total,
        "restock_h": restock_h,
        "fulfill": fulfill,
        "stocktake": stocktake,
        "production": production,
        "stale_rows": stale_rows,
        "overstock_rows": overstock_rows,
        "stale_value": stale_value,
        "overstock_value": overstock_value,
        "sku_rows": sku_rows,
        "constants": {"stale": hp.STALE_DAYS, "dos": hp.DOS_THRESHOLD_DAYS,
                      "dos_win": hp.DOS_WINDOW_DAYS,
                      "st_gap": hp.STOCKTAKE_MAX_GAP,
                      "bom_pct": hp.BOM_OVERUSE_PCT},
        "window_days": range_days,
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
    }


@bp.route("/report/health")
@require_login
@require_role("manager")
def health():
    """库存健康仪表盘（manager+，单仓视角，纯只读）。

    口径见 docs/2026-10-15-inventory-health-report-plan.md；
    呆滞=FIFO 账龄>7 天，超储=DOS>7 天，超耗=BOM 超 10%。
    """
    range_param = request.args.get("range", "7d")
    sort_key = request.args.get("sort", "stock_value")
    ctx = _build_health_context(range_param, sort_key)
    audit("report.health_view", "report", None,
          {"range": ctx["range"], "sort": ctx["sort"]})
    return render_template("report_health.html", **ctx)


@bp.route("/report/health/export")
@require_login
@require_role("manager")
def health_export():
    """健康报表 CSV（5 段：总体 / 风险 / 盘点 / SKU 全表 / 生产明细）。

    与 /report/health 共用 _build_health_context，口径零漂移。
    UTF-8 BOM 兼容 Excel 中文。
    """
    import datetime as _dtmod

    from flask import current_app

    range_param = request.args.get("range", "7d")
    ctx = _build_health_context(range_param, "stock_value")
    audit("report.health_export", "report", None, {"range": ctx["range"]})

    output = io.StringIO()
    w = csv.writer(output)

    # 段 1:总体 KPI
    m = ctx["metrics"]
    w.writerow(["库存健康报表", ctx["range_label"], f"生成时间 {ctx['generated_at']}"])
    w.writerow(["库存金额", f"{m['total_stock_value']:.2f}"])
    w.writerow(["活跃 SKU 数", ctx["levels"]["active_skus"]])
    w.writerow(["周转率", f"{m['turnover']:.2f}" if m["turnover"] else "0.00"])
    w.writerow(["可售天数", m["turnover_days"] if m["turnover_days"] is not None else "—"])
    w.writerow(["低于安全库存 SKU 数", len(ctx["levels"]["below_safety"])])
    w.writerow(["断货 SKU 数", len(ctx["levels"]["out_of_stock"])])
    w.writerow([f"呆滞金额(账龄>{ctx['constants']['stale']}天)", ctx["stale_value"]])
    w.writerow([f"超储金额(DOS>{ctx['constants']['dos']}天)", ctx["overstock_value"]])
    w.writerow([])

    # 段 2:风险明细（断货/低安全 + 呆滞 + 超储 + 生产超耗）
    w.writerow(["断货/低于安全库存", "品类", "库存量", "安全库存", "库存金额"])
    for r in ctx["levels"]["out_of_stock"] + ctx["levels"]["below_safety"]:
        w.writerow([r["name"], r["category"], r["qty"], r["safety"], r["value"]])
    w.writerow([])
    w.writerow(["呆滞品项", "呆滞量", "呆滞金额", "最老层入库日"])
    for r in ctx["stale_rows"]:
        w.writerow([r["name"], r["stale_qty"], r["stale_value"], r["oldest_date"] or "—"])
    w.writerow([])
    w.writerow(["超储品项", "库存量", "DOS(天)", "占用金额"])
    for r in ctx["overstock_rows"]:
        w.writerow([r["name"], r["qty"], round(r["dos"], 1), r["stock_value"]])
    w.writerow([])
    w.writerow(["生产超耗明细", "批次日期", "产品", "品项", "计划量", "实际量", "超耗%", "超耗金额"])
    for r in (ctx["production"] or {}).get("overuse_rows", []):
        w.writerow([r["run_id"], r["run_date"], r["product"], r["item"],
                    r["planned"], r["actual"], r["pct"], r["over_value"]])
    w.writerow([])

    # 段 3:盘点质量
    st = ctx["stocktake"]
    w.writerow(["盘点差异率(%)", st["diff_rate"] if st["diff_rate"] is not None else "—"])
    w.writerow([f"超{ctx['constants']['st_gap']}天未盘 SKU 数", st["unstale_count"]])
    w.writerow(["上次盘点距今天数", st["last_stocktake_days"] if st["last_stocktake_days"] is not None else "—"])
    w.writerow([])

    # 段 4:SKU 明细全表
    w.writerow(["品项", "品类", "库存量", "单位", "库存金额", "呆滞量", "呆滞金额",
                "最老层入库日", "DOS(天)", "ABC", "窗口消耗金额", "最后出库", "最后入库", "最后盘点"])
    for r in ctx["sku_rows"]:
        w.writerow([r["name"], r["category"], r["qty"], r["unit"], r["stock_value"],
                    r["stale_qty"], r["stale_value"], r["oldest_date"] or "—",
                    round(r["dos"], 1) if r["dos"] is not None else "—",
                    r["abc"] or "—", r["consumed_value"], r["last_outbound"] or "—",
                    r["last_inbound"] or "—", r["last_stocktake"] or "—"])
    w.writerow([])

    # 段 5:生产明细（产品维度 + 原料品类维度）
    prod = ctx["production"]
    if prod:
        w.writerow(["产品", "批次数", "产出量", "消耗金额", "单位产出成本", "超耗%"])
        for r in prod["products"]:
            w.writerow([r["name"], r["runs"], r["output"], r["consumed"],
                        r["unit_cost"] if r["unit_cost"] is not None else "—",
                        r["overuse_pct"] if r["overuse_pct"] is not None else "—"])
        w.writerow([])
        w.writerow(["原料品类", "生产消耗金额", "消耗占比%", "品类超耗%"])
        for r in prod["categories"]:
            w.writerow([r["name"], r["consumed"], r["share"],
                        r["overuse_pct"] if r["overuse_pct"] is not None else "—"])

    filename = f"health-{_dtmod.datetime.now().strftime('%Y-%m-%d')}-{ctx['range']}.csv"
    return current_app.response_class(
        output.getvalue().encode("utf-8-sig"),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename={filename}"},
    )
