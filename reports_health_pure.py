"""库存健康报表纯函数层（/report/health）。

设计定稿：docs/2026-10-15-inventory-health-report-plan.md。
本模块只做计算，不碰 Flask（g/request/render）；db 参数是 sqlite3.Connection
（仓库库或主库），由调用方（blueprints/reports.py）提供。

口径铁律：
- 时间过滤一律用 Python 侧 datetime 绑定参数，禁止 SQL datetime('now')——
  生产 CST / SQLite UTC 时区坑（生产 unit 未设 TZ），dev 不可复现、生产必现。
- 时间列一律走 `parse_event_ts()`，禁止裸 `datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")`——
  生产存在无秒形态（Issue #38），严格匹配即 500。
- 所有阈值集中在模块级常量，改口径只动一处。
- 呆滞 = FIFO 账龄重放（补货入库压层 / 负 delta 先进先出消层 / 其余事件按比例
  缩放层总和对齐 running）；重放终值 ≠ items.quantity 时该 SKU 回退
  「近 STALE_DAYS 天零消耗」判定；首条流水前的库存按「最老」计（宁误报不漏报）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 口径常量（Eric 2026-10-15 拍板）
# ---------------------------------------------------------------------------
STALE_DAYS = 7            # 呆滞账龄阈值（天）
DOS_THRESHOLD_DAYS = 7    # 超储可售天数阈值（天）
DOS_WINDOW_DAYS = 30      # DOS 日均消耗回看窗口（天），与页面 range 解耦
STOCKTAKE_MAX_GAP = 30    # 超期未盘阈值（天）
BOM_OVERUSE_PCT = 10      # BOM 超耗预警阈值（%）

# ---------------------------------------------------------------------------
# stock_movements.action 语义分组（以生产实际数据为准，2026-10-15 全 6 仓核对）
# ---------------------------------------------------------------------------
# 正向入库事件：压新层（date=created_at, qty=delta）
INBOUND_ACTIONS = frozenset({"补货入库", "门店订货入库"})
# 消耗事件（负 delta）：SKU 级日均消耗 / 最后出库日均统计这两类
CONSUME_ACTIONS = frozenset({"出库", "调整出库", "门店订货出库", "生产消耗"})
# 其余全部按「调整」处理：running += delta 后按比例缩放所有层对齐 running
# （盘点修正/库存调整为强设数量型事件，无法定位动的是哪批货，按比例缩放；
#   各类回退（出库回退/生产消耗回退/补货删除回滚负向消层）同样走通用分支）

# restock_requests 状态域（blueprints/restock.py 核对：仅 提交/入库 两态，
# 无审批/拒绝环节——补货健康卡不产「拒绝率」）
RESTOCK_PENDING_STATUS = "提交"

REPLAY_MISMATCH_TOL = 0.01  # 重放终值 vs items.quantity 容差（浮点累计误差）

# ---------------------------------------------------------------------------
# 时间戳解析（Issue #38）
# ---------------------------------------------------------------------------
# 生产实测（2026-10-15 全 6 仓 + master 普查）：**只有 wh_000.db 的
# stock_movements.created_at 与 restock_requests.created_at 各有 54 行**是
# 16 字符形态「YYYY-MM-DD HH:MM」（无秒），跨度 2025-11-28 → 2026-10-13，
# 即仍在产生（应用层全部走 _helpers.now() 的 19 字符格式，故源头是人工/ops SQL）。
# 严格 "%Y-%m-%d %H:%M:%S" 撞上它即 ValueError → /report/health 500。
#
# 顺序即优先级：先试覆盖生产已知形态的格式，全部失败才抛错。
_TS_FORMATS = (
    "%Y-%m-%d %H:%M:%S",      # 规范形态（应用层 now()，19 字符）
    "%Y-%m-%d %H:%M",         # 生产存量：人工/ops SQL 无秒形态（16 字符，Issue #38）
    "%Y-%m-%d %H:%M:%S.%f",   # 微秒（ISO-8601 子集）
    "%Y-%m-%d",               # 纯日期 → 当日 00:00
)


def parse_event_ts(s: object) -> datetime:
    """容错解析时间列，兼容带秒 / 无秒两种生产形态（Issue #38）。

    只容忍上表列出的形态；其余一律抛 ValueError 且**消息带原始值**——原始值
    进 traceback 正是本次事故能在 prod 一眼定位的原因。

    **刻意不在调用点大面积 try/except 吞异常**：FIFO 账龄重放的正确性依赖
    每一条流水的时间，静默跳过一条事件会把呆滞金额算错（错得无声无息，
    比 500 更难发现）。未知形态应当响亮失败，由 DataQuality 层面单独处置。
    唯一豁免是**聚类型指标**（均值 / 占比，丢样本只会让分母变小而不会让
    结果错位）——见 `compute_fulfillment`，那里丢样本时留 WARNING 不静默。

    入参标注为 `object` 而非 `str`：值直接来自 SQLite 列（可能为 NULL /
    数字），实现主动把非 str、空白串一律归一成 ValueError，而不是让
    `strptime` 抛 TypeError。

    另：不使用 `datetime.fromisoformat` 兜底——3.11+ 它会接受带时区偏移的
    字符串并返回 aware datetime，与调用方的 naive `now` 相减会 TypeError；
    本模块的时间语义是「本地 naive 字符串字典序」，不引入 aware 值。
    """
    if isinstance(s, str):
        raw = s.strip()
        if raw:
            for fmt in _TS_FORMATS:
                try:
                    return datetime.strptime(raw, fmt)
                except ValueError:
                    continue
    raise ValueError(
        f"无法解析时间戳: {s!r}（支持 {' / '.join(_TS_FORMATS)}）"
    )


def fmt_date(ts: str | None) -> str | None:
    """取 created_at 的日期部分（YYYY-MM-DD）。"""
    return ts[:10] if ts else None


# ---------------------------------------------------------------------------
# FIFO 账龄重放
# ---------------------------------------------------------------------------

def replay_item_layers(movements: list[tuple[str, str, float]], final_qty: float,
                       now: datetime):
    """按时间顺序重放单 SKU 流水，返回账龄层信息。

    movements: [(created_at, action, delta)]，调用方保证按 created_at, id 升序。
    final_qty: items.quantity（重放失配判定基准）。
    返回 dict:
      ok: bool            重放终值是否与 final_qty 一致（容差内）
      stale_qty: float    账龄 > STALE_DAYS 天的层数量合计（ok=False 时为 None）
      oldest_date: str|None  最老层入库日（YYYY-MM-DD）
    """
    layers: list[list] = []  # [datetime, qty]，最老在前
    running = 0.0
    for ts, action, delta in movements:
        delta = float(delta)
        event_dt = parse_event_ts(ts)
        if delta > 0 and action in INBOUND_ACTIONS:
            layers.append([event_dt, delta])
            running += delta
        elif delta < 0:
            # 消耗/回滚：从最老层开始先进先出消减
            running += delta
            need = -delta
            while need > 1e-9 and layers:
                head = layers[0]
                if head[1] <= need + 1e-9:
                    need -= head[1]
                    layers.pop(0)
                else:
                    head[1] -= need
                    need = 0.0
        else:
            # 正向非入库事件（库存调整/盘点修正增益/出库回退等）：
            # 按比例缩放所有层对齐新 running（层空则新建当日层）
            running += delta
            if layers:
                scale = running / sum(x[1] for x in layers)
                for x in layers:
                    x[1] *= scale
            elif running > 1e-9:
                layers.append([event_dt, running])

    total = sum(x[1] for x in layers)
    ok = abs(total - float(final_qty)) <= REPLAY_MISMATCH_TOL
    if not ok:
        return {"ok": False, "stale_qty": None, "oldest_date": None}

    stale = sum(q for (dt, q) in layers if (now - dt).days > STALE_DAYS)
    oldest = layers[0][0].strftime("%Y-%m-%d") if layers else None
    return {"ok": True, "stale_qty": stale, "oldest_date": oldest}


def compute_stale_and_dos(db, now: datetime) -> dict:
    """逐 SKU 呆滞/超储分档 + SKU 明细表所需的流水派生字段。

    返回 {item_id: {
        tier: 'stale'|'overstock'|'normal',   # 分档（断货/低安全在 levels 里）
        stale_qty, stale_value, oldest_date,
        dos: float|None,                       # 可售天数（d<=0 或呆滞时 None）
        last_outbound, last_inbound,           # 最后出库/入库日期或 None
    }}
    """
    items = db.execute(
        """SELECT id, name, quantity, unit_cost, is_active FROM items ORDER BY id"""
    ).fetchall()
    movements: dict[int, list] = {}
    for r in db.execute(
        """SELECT item_id, action, delta, created_at FROM stock_movements
           ORDER BY created_at ASC, id ASC"""
    ).fetchall():
        movements.setdefault(r["item_id"], []).append(
            (r["created_at"], r["action"], r["delta"]))

    # 口径：日均消耗 d = 近 DOS_WINDOW_DAYS 天（出库系+生产消耗）÷ 窗口天数
    d_start = now - timedelta(days=DOS_WINDOW_DAYS)
    recent_start = now - timedelta(days=STALE_DAYS)
    consume_qty: dict[int, float] = {}
    recent_consume: dict[int, bool] = {}
    for r in db.execute(
        """SELECT item_id, delta, created_at FROM stock_movements
           WHERE action IN (?, ?, ?, ?) AND created_at >= ?""",
        (*sorted(CONSUME_ACTIONS), d_start.strftime("%Y-%m-%d %H:%M:%S")),
    ).fetchall():
        qty = -float(r["delta"])
        consume_qty[r["item_id"]] = consume_qty.get(r["item_id"], 0.0) + qty
        if r["created_at"] >= recent_start.strftime("%Y-%m-%d %H:%M:%S"):
            recent_consume[r["item_id"]] = True

    last_out = {r["item_id"]: r["d"] for r in db.execute(
        f"""SELECT item_id, MAX(created_at) AS d FROM stock_movements
            WHERE action IN ({','.join('?' * len(CONSUME_ACTIONS))})
            GROUP BY item_id""", sorted(CONSUME_ACTIONS)).fetchall()}
    last_in = {r["item_id"]: r["d"] for r in db.execute(
        """SELECT item_id, MAX(created_at) AS d FROM stock_movements
           WHERE action = '补货入库' GROUP BY item_id""").fetchall()}

    result = {}
    for it in items:
        qty = float(it["quantity"])
        if not it["is_active"] or qty <= 0:
            continue
        mv = movements.get(it["id"], [])
        replay = replay_item_layers(mv, qty, now)
        if replay["ok"]:
            stale_qty = replay["stale_qty"]
            oldest = replay["oldest_date"]
        else:
            # 兜底：重放失配 → 近 STALE_DAYS 天零消耗则全额呆滞
            stale_qty = qty if not recent_consume.get(it["id"]) else 0.0
            oldest = fmt_date(mv[0][0]) if mv else None

        d = consume_qty.get(it["id"], 0.0) / DOS_WINDOW_DAYS
        dos = None
        tier = "normal"
        if stale_qty and stale_qty > 1e-9:
            tier = "stale"  # 红呆滞优先于黄超储
        elif d > 0:
            dos = qty / d
            if recent_consume.get(it["id"]) and dos > DOS_THRESHOLD_DAYS:
                tier = "overstock"

        result[it["id"]] = {
            "tier": tier,
            "stale_qty": stale_qty or 0.0,
            "stale_value": (stale_qty or 0.0) * float(it["unit_cost"]),
            "oldest_date": oldest,
            "dos": dos,
            "last_outbound": fmt_date(last_out.get(it["id"])),
            "last_inbound": fmt_date(last_in.get(it["id"])),
        }
    return result


# ---------------------------------------------------------------------------
# ABC 分类（窗口内消耗金额，口径同 /summary：outbound 非生产 + production）
# ---------------------------------------------------------------------------

def compute_abc(db, cutoff: str) -> dict:
    """返回 {item_id: {'cls': 'A'|'B'|'C', 'consumed_value': float}}。

    口径：outbound_requests.rolled_back=0 且 reason 非生产领料 + 未回滚 run 的
    actual_qty，按 unit_cost 折金额，降序累计 ≤80% A / ≤95% B / 其余 C。
    """
    rows = db.execute(
        """SELECT item_id, ABS(SUM(q * cost)) AS v FROM (
               SELECT o.item_id, o.requested_quantity AS q, i.unit_cost AS cost
               FROM outbound_requests o JOIN items i ON i.id = o.item_id
               WHERE o.rolled_back = 0
                 AND (o.reason IS NULL OR o.reason NOT LIKE '生产领料(run=#%')
                 AND o.created_at >= ?
               UNION ALL
               SELECT pri.item_id, pri.actual_qty AS q, i.unit_cost AS cost
               FROM production_run_items pri
               JOIN production_runs pr ON pr.id = pri.run_id
               JOIN items i ON i.id = pri.item_id
               WHERE pr.rolled_back = 0 AND pr.created_at >= ?
           ) GROUP BY item_id""",
        (cutoff, cutoff),
    ).fetchall()
    ranked = sorted(rows, key=lambda r: -float(r["v"]))
    total = sum(float(r["v"]) for r in ranked)
    result, cum = {}, 0.0
    for r in ranked:
        v = float(r["v"])
        cum += v
        share = cum / total if total > 0 else 0.0
        cls = "A" if share <= 0.80 else ("B" if share <= 0.95 else "C")
        result[r["item_id"]] = {"cls": cls, "consumed_value": v}
    return result


# ---------------------------------------------------------------------------
# 消耗趋势（近 14 天日消耗金额，缺失日补 0）
# ---------------------------------------------------------------------------

def compute_trend(db, now: datetime, points: int = 14) -> list[dict]:
    """返回 [{date: 'MM-DD', value: float}]，最后 points 天（含今天）。"""
    start = (now - timedelta(days=points - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    start_s = start.strftime("%Y-%m-%d %H:%M:%S")
    daily: dict[str, float] = {}
    rows = db.execute(
        """SELECT date(created_at) AS d, ABS(SUM(q * cost)) AS v FROM (
               SELECT o.created_at, o.requested_quantity AS q, i.unit_cost AS cost
               FROM outbound_requests o JOIN items i ON i.id = o.item_id
               WHERE o.rolled_back = 0
                 AND (o.reason IS NULL OR o.reason NOT LIKE '生产领料(run=#%')
                 AND o.created_at >= ?
               UNION ALL
               SELECT pr.created_at, pri.actual_qty AS q, i.unit_cost AS cost
               FROM production_run_items pri
               JOIN production_runs pr ON pr.id = pri.run_id
               JOIN items i ON i.id = pri.item_id
               WHERE pr.rolled_back = 0 AND pr.created_at >= ?
           ) GROUP BY date(created_at)""",
        (start_s, start_s),
    ).fetchall()
    for r in rows:
        daily[r["d"]] = float(r["v"])
    out = []
    for i in range(points):
        day = start + timedelta(days=i)
        key = day.strftime("%Y-%m-%d")
        out.append({"date": day.strftime("%m-%d"), "value": daily.get(key, 0.0)})
    return out


# ---------------------------------------------------------------------------
# 补货健康（状态域核对：仅 提交/入库，无审批拒绝环节）
# ---------------------------------------------------------------------------

def compute_restock_health(db, cutoff: str) -> dict:
    """返回 {pending, stocked_in, window_count}。

    口径：pending = status='提交'（待入库，全量快照，不限窗口）；
    stocked_in / window_count = 窗口内 created_at 的已入库 / 全部单数。
    """
    pending = db.execute(
        "SELECT COUNT(*) AS c FROM restock_requests WHERE status = ?",
        (RESTOCK_PENDING_STATUS,)).fetchone()["c"]
    # 口径：已入库 = 窗口内非「提交」状态（当前状态域仅 提交/入库 两态）
    row = db.execute(
        """SELECT SUM(CASE WHEN status != ? THEN 1 ELSE 0 END) AS done, COUNT(*) AS total
           FROM restock_requests WHERE created_at >= ?""",
        (RESTOCK_PENDING_STATUS, cutoff)).fetchone()
    return {"pending": pending, "stocked_in": row["done"] or 0,
            "window_count": row["total"]}


# ---------------------------------------------------------------------------
# 门店履约（master.db store_orders，按当前仓 = dc_warehouse_code 过滤）
# ---------------------------------------------------------------------------

def compute_fulfillment(master, wh_code: str | None, now: datetime) -> dict:
    """返回 {backlog, in_transit, avg_cycle_days}；wh_code 为 None 时全 None。

    口径：本仓参与（dc_warehouse_code 或 store_warehouse_code 命中）的
    store_orders——DC 是履约方、门店是下单方，两视角都看自己的单。
    积压 = status IN (pending, approved)；在途 = shipped；
    平均履约周期 = delivered 且 delivered_at/created_at 齐全订单的均值（天，1 位小数）；
    时间戳形态无法解析的行不计入均值（丢样本）并留 WARNING——聚类型指标豁免
    见 `parse_event_ts` docstring。
    """
    if wh_code is None:
        return {"backlog": None, "in_transit": None, "avg_cycle_days": None}
    row = master.execute(
        """SELECT SUM(CASE WHEN status IN ('pending','approved') THEN 1 ELSE 0 END) AS backlog,
                  SUM(CASE WHEN status = 'shipped' THEN 1 ELSE 0 END) AS in_transit
           FROM store_orders
           WHERE dc_warehouse_code = ? OR store_warehouse_code = ?""",
        (wh_code, wh_code)).fetchone()
    cycle: list[float] = []
    for r in master.execute(
        """SELECT created_at, delivered_at FROM store_orders
           WHERE (dc_warehouse_code = ? OR store_warehouse_code = ?)
             AND status = 'delivered' AND delivered_at IS NOT NULL""",
        (wh_code, wh_code)).fetchall():
        try:
            c = parse_event_ts(r["created_at"])
            d = parse_event_ts(r["delivered_at"])
            cycle.append(max((d - c).total_seconds(), 0) / 86400.0)
        except ValueError as exc:
            # 口径豁免（与 §13.8「账龄重放不许静默跳过」相区分）：平均履约
            # 周期是**聚类型**指标——丢一个样本只让分母变小，不会像 FIFO 重放
            # 那样让整体结果错位。已知的 19/16 字符形态都能解析，此处只兜真正
            # 陌生的数据；不静默，留 WARNING 供排查（logger 名即模块名）。
            logger.warning("履约周期样本跳过（时间戳无法解析）: %s", exc)
            continue
    avg = round(sum(cycle) / len(cycle), 1) if cycle else None
    return {"backlog": row["backlog"] or 0, "in_transit": row["in_transit"] or 0,
            "avg_cycle_days": avg}


# ---------------------------------------------------------------------------
# 盘点质量
# ---------------------------------------------------------------------------

def compute_stocktake_quality(db, now: datetime) -> dict:
    """返回 {diff_rate, unstale_count, last_stocktake_days, unstale_items}。

    口径：diff_rate = diff≠0 行 ÷ 总行（全表）；unstale = 活跃 SKU 最后盘点日
    距今 > STOCKTAKE_MAX_GAP 天或从未盘；last_stocktake_days = MAX(created_at) 距今。
    """
    row = db.execute(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN diff != 0 THEN 1 ELSE 0 END) AS diff_rows
           FROM stocktakes""").fetchone()
    last_by_item = {r["item_id"]: r["d"] for r in db.execute(
        "SELECT item_id, MAX(created_at) AS d FROM stocktakes GROUP BY item_id"
    ).fetchall()}
    gap_cutoff = (now - timedelta(days=STOCKTAKE_MAX_GAP)).strftime("%Y-%m-%d %H:%M:%S")
    unstale_items = []
    for it in db.execute(
        "SELECT id, name FROM items WHERE is_active = 1").fetchall():
        d = last_by_item.get(it["id"])
        if d is None or d < gap_cutoff:
            unstale_items.append({"name": it["name"],
                                  "last_date": fmt_date(d)})
    last_all = db.execute("SELECT MAX(created_at) AS d FROM stocktakes").fetchone()["d"]
    last_days = None
    if last_all:
        last_days = (now - parse_event_ts(last_all)).days
    total = row["total"] or 0
    diff_rate = round((row["diff_rows"] or 0) / total * 100, 1) if total else None
    return {"diff_rate": diff_rate, "unstale_count": len(unstale_items),
            "unstale_items": unstale_items, "last_stocktake_days": last_days}


# ---------------------------------------------------------------------------
# 生产分析（BOM 超耗 / 产品维度 / 原料品类维度）
# ---------------------------------------------------------------------------

def _overuse_pct(planned: float, actual: float) -> float | None:
    """行级超耗%。planned<=0 且 actual>0 → 100（BOM 缺计划兜底）；其余 None/负值不超。"""
    if planned > 1e-9:
        return (actual - planned) / planned * 100
    if actual > 1e-9:
        return 100.0
    return None


def compute_production(db, cutoff: str) -> dict | None:
    """窗口内未回滚 run 的生产分析。无 run 时返回 None（页面显示 —）。

    返回 {run_count, output_qty, consumed_value, overuse_pct, unit_cost,
          overuse_rows(top10), products(按产品), categories(按原料品类)}
    """
    runs = db.execute(
        """SELECT id, product_id, output_qty, created_at FROM production_runs
           WHERE rolled_back = 0 AND created_at >= ?""", (cutoff,)).fetchall()
    if not runs:
        return None
    run_ids = [r["id"] for r in runs]
    ph = ",".join("?" * len(run_ids))

    items = {r["id"]: r for r in db.execute(
        "SELECT id, name, unit_cost, category_id FROM items").fetchall()}
    products = {r["id"]: r["name"] for r in db.execute(
        "SELECT id, name FROM products").fetchall()}
    cats = {r["id"]: r["name"] for r in db.execute(
        "SELECT id, name FROM categories").fetchall()}

    rows = db.execute(
        f"""SELECT run_id, item_id, planned_qty, actual_qty
            FROM production_run_items WHERE run_id IN ({ph})""",
        run_ids).fetchall()

    consumed_value = 0.0
    planned_total = 0.0
    overuse_rows = []
    prod_agg: dict[int, dict] = {}
    cat_agg: dict[int, dict] = {}
    for r in rows:
        item = items.get(r["item_id"])
        if item is None:
            continue
        planned = float(r["planned_qty"] or 0)
        actual = float(r["actual_qty"] or 0)
        cost = float(item["unit_cost"])
        consumed_value += actual * cost
        planned_total += planned * cost
        pct = _overuse_pct(planned, actual)
        over_amt = max(actual - planned, 0.0) * cost
        # 产品维度聚合
        run = next(x for x in runs if x["id"] == r["run_id"])
        pa = prod_agg.setdefault(run["product_id"], {
            "name": products.get(run["product_id"], f"#{run['product_id']}"),
            "runs": set(), "output": 0.0, "consumed": 0.0,
            "plan_cost": 0.0, "over_amt": 0.0})
        pa["runs"].add(r["run_id"])
        pa["consumed"] += actual * cost
        pa["plan_cost"] += planned * cost
        pa["over_amt"] += over_amt
        # 原料品类维度聚合
        ca = cat_agg.setdefault(item["category_id"], {
            "name": cats.get(item["category_id"], f"#{item['category_id']}"),
            "consumed": 0.0, "plan_cost": 0.0, "over_amt": 0.0})
        ca["consumed"] += actual * cost
        ca["plan_cost"] += planned * cost
        ca["over_amt"] += over_amt
        if pct is not None and pct > BOM_OVERUSE_PCT:
            overuse_rows.append({
                "run_id": r["run_id"], "run_date": fmt_date(run["created_at"]),
                "product": pa["name"], "item": item["name"],
                "planned": planned, "actual": actual, "pct": round(pct, 1),
                "over_value": round(over_amt, 2)})

    for pa in prod_agg.values():
        pa["runs"] = len(pa["runs"])
    output_total = sum(float(r["output_qty"] or 0) for r in runs)
    overuse_pct = round((planned_total and
                         sum(x["over_amt"] for x in prod_agg.values())
                         / planned_total * 100) or 0.0, 1) if planned_total > 0 else None

    overuse_rows.sort(key=lambda x: -x["over_value"])
    # 产品产出量按 run 聚合（产出在 production_runs 上，不在 run_items）
    out_by_prod: dict[int, float] = {}
    for r in runs:
        out_by_prod[r["product_id"]] = out_by_prod.get(r["product_id"], 0.0) \
            + float(r["output_qty"] or 0)
    product_rows = []
    for pid, pa in prod_agg.items():
        output = round(out_by_prod.get(pid, 0.0), 2)
        product_rows.append({
            "name": pa["name"], "runs": pa["runs"], "output": output,
            "consumed": round(pa["consumed"], 2),
            "unit_cost": round(pa["consumed"] / output, 2) if output else None,
            "overuse_pct": round(pa["over_amt"] / pa["plan_cost"] * 100, 1)
            if pa["plan_cost"] > 0 else None})
    product_rows.sort(key=lambda x: -x["consumed"])

    cat_consumed_total = sum(c["consumed"] for c in cat_agg.values()) or 1.0
    category_rows = sorted(
        ({"name": c["name"], "consumed": round(c["consumed"], 2),
          "share": round(c["consumed"] / cat_consumed_total * 100, 1),
          "overuse_pct": round(c["over_amt"] / c["plan_cost"] * 100, 1)
          if c["plan_cost"] > 0 else None}
         for c in cat_agg.values()),
        key=lambda x: -x["consumed"])

    return {
        "run_count": len(runs),
        "output_qty": round(output_total, 2),
        "consumed_value": round(consumed_value, 2),
        "overuse_pct": overuse_pct,
        "unit_cost": round(consumed_value / output_total, 2) if output_total else None,
        "overuse_rows": overuse_rows[:10],
        "products": product_rows,
        "categories": category_rows,
    }


# ---------------------------------------------------------------------------
# 低于安全库存 / 断货（KPI + 风险段 1）
# ---------------------------------------------------------------------------

def compute_inventory_levels(db) -> dict:
    """返回 {stock_value, active_skus, out_of_stock[], below_safety[]}。

    口径：断货 = is_active=1 AND quantity=0；低于安全 = is_active=1 AND
    0 < quantity < safety_stock；库存金额 = SUM(quantity × unit_cost) 全表。
    """
    stock_value = float(db.execute(
        "SELECT COALESCE(SUM(quantity * unit_cost), 0) AS c FROM items"
    ).fetchone()["c"])
    out_of_stock, below_safety, active = [], [], 0
    for it in db.execute(
        """SELECT i.id, i.name, i.quantity, i.safety_stock, i.unit_cost, i.unit,
                  c.name AS category_name
           FROM items i JOIN categories c ON c.id = i.category_id
           WHERE i.is_active = 1 ORDER BY (i.quantity * i.unit_cost) DESC"""
    ).fetchall():
        active += 1
        qty = float(it["quantity"])
        safety = float(it["safety_stock"])
        row = {"name": it["name"], "category": it["category_name"], "qty": qty,
               "safety": safety, "unit": it["unit"],
               "value": round(qty * float(it["unit_cost"]), 2)}
        if qty <= 1e-9:
            out_of_stock.append(row)
        elif safety > 0 and qty < safety:
            below_safety.append(row)
    return {"stock_value": round(stock_value, 2), "active_skus": active,
            "out_of_stock": out_of_stock, "below_safety": below_safety}


# ---------------------------------------------------------------------------
# SKU 明细总表（⑨）
# ---------------------------------------------------------------------------

SKU_SORT_WHITELIST = {"stock_value": ("stock_value", True),
                      "consumed": ("consumed_value", True),
                      "dos": ("dos", True),
                      "stale": ("stale_qty", True)}


def build_sku_table(db, stale_dos: dict, abc: dict, cutoff: str,
                    sort_key: str = "stock_value") -> list[dict]:
    """⑨ 表行集：全活跃 SKU，白名单排序（默认库存金额降序）。

    白名单：stock_value / consumed / dos / stale；非法值回退默认。
    排序均为降序，None 值排最后。
    """
    field, _desc = SKU_SORT_WHITELIST.get(sort_key, SKU_SORT_WHITELIST["stock_value"])
    last_st = {r["item_id"]: r["d"] for r in db.execute(
        "SELECT item_id, MAX(created_at) AS d FROM stocktakes GROUP BY item_id"
    ).fetchall()}
    rows = []
    items = db.execute(
        """SELECT i.id, i.name, i.quantity, i.safety_stock, i.unit_cost, i.unit,
                  c.name AS category_name
           FROM items i JOIN categories c ON c.id = i.category_id
           WHERE i.is_active = 1""").fetchall()
    for it in items:
        qty = float(it["quantity"])
        sd = stale_dos.get(it["id"], {})
        abc_info = abc.get(it["id"], {})
        rows.append({
            "id": it["id"], "name": it["name"], "category": it["category_name"],
            "qty": qty, "unit": it["unit"], "safety": float(it["safety_stock"]),
            "stock_value": round(qty * float(it["unit_cost"]), 2),
            "stale_qty": sd.get("stale_qty", 0.0),
            "stale_value": round(sd.get("stale_value", 0.0), 2),
            "oldest_date": sd.get("oldest_date"),
            "dos": sd.get("dos"),
            "abc": abc_info.get("cls"),
            "consumed_value": round(abc_info.get("consumed_value", 0.0), 2),
            "last_outbound": sd.get("last_outbound"),
            "last_inbound": sd.get("last_inbound"),
            "last_stocktake": fmt_date(last_st.get(it["id"])),
        })

    def sort_value(r):
        v = r.get(field)
        return (v is None, -(v or 0))

    rows.sort(key=sort_value)
    return rows
