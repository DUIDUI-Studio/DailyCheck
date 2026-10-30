"""补货看板的数据装配层（无路由、无模板）。

职责边界
--------
只做「跨仓取数 + 装配」，公式全部在 `replenishment_pure.py`，HTTP 在
`blueprints/forecast.py`。拆开的理由：装配层要直连多个 sqlite 文件，
不适合塞进蓝图模块；单测也可以直接喂 dict 而不是起 Flask。

为什么不用 g.warehouse_db
-------------------------
看板是**品牌级**视图，一次要读齐 DC + 全部门店 + 研发中心，而
`get_warehouse_db()` 只绑定当前请求选中的那一个仓。这里按 master.db 的
`warehouses.db_path` 逐个开只读连接 —— 与 `procurement.procurement_hub`
既有做法一致。

需求口径（关键，见 docs/2026-10-20-replenishment-board.md §2）
------------------------------------------------------------
- 门店 = 「生产消耗」+「出库」，剔除 note 含盘亏/盘盈/盘点的盘点调整行。
  盘点盈亏是账实差异不是需求：生产数据里 wh_003 盘亏累计 2965，真实消耗
  只有 334，混在一起会让日均虚高近 10 倍。
- DC = 「出库」+「门店订货出库」+「总仓调货出库」，同样剔除盘亏。
- DC 的需求优先用**下游门店消耗汇总**（DRP 拉动），只有 DC 独有品项才
  退回本仓出库史。原因：DC 是整周成批发货，直接拿发货记录算日需求会得到
  巨大 σ，安全库存被算成十几天的量（实测 8.5/日 的需求配出 107 袋 SS）。
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path

import config
from db import init_master_db

from . import replenishment_pure as R
from .canonical_pure import store_selectable_clause


def _loss_filter(prefix: str = "") -> str:
    """盘点盈亏不是需求 —— note 形如 "盘点审核#24盘亏 (req#1545)"。

    `prefix` 用于 JOIN 场景（`"m."`），避免两处各写一份口径、改一处漏一处。
    """
    col = f"{prefix}note" if prefix else "note"
    return (
        f"COALESCE({col},'') NOT LIKE '%盘亏%' "
        f"AND COALESCE({col},'') NOT LIKE '%盘盈%' "
        f"AND COALESCE({col},'') NOT LIKE '%盘点%'"
    )


_LOSS_FILTER = _loss_filter()

_STORE_CONSUME_ACTIONS = "('生产消耗','出库')"
_DC_CONSUME_ACTIONS = "('出库','门店订货出库','总仓调货出库')"
_ROLLBACK_ACTIONS = "('生产消耗回退','出库回退','补货删除回滚','调整出库回滚')"

_STORE_ORDER_OPEN = "('pending','approved','shipped')"
_RESTOCK_CLOSED = "('已到货','已取消','入库')"

# 入库动作：盘点锚定消耗的恒等式 C = I_start + 入库 − I_end 需要把它们加回来。
_INBOUND_ACTIONS = "('补货入库','门店订货入库','生产入库')"

DC_TYPE = "distribution_center"

# 供应方式（owner 2026-10-20 确认口径）：
#   central = 配送中心统配备货（items.is_orderable=1）
#   self    = 门店自行采购（items.is_orderable=0，DC 不再备货）
# 看板用它做全局筛选；两种方式的建议采购额是**两个不同的预算**，不能相加
# 当成一张采购单看。
SUPPLY_ALL = "all"
SUPPLY_CENTRAL = "central"
SUPPLY_SELF = "self"
SUPPLY_MODES = (SUPPLY_ALL, SUPPLY_CENTRAL, SUPPLY_SELF)
SUPPLY_LABELS = {
    SUPPLY_ALL: "全部",
    SUPPLY_CENTRAL: "统配",
    SUPPLY_SELF: "自采",
}

# 视角（owner 2026-10-20）：看板上要能切换「站在谁的立场看该订什么」。
#   all     = 全链（所有仓汇总，默认）
#   <code>  = 单个仓：DC 看全局拉动需求，门店看本店向 DC 的订货量
# 二者语义不同，不能直接相加：DC 的量是「向供应商采购」，门店的量是
# 「向 DC 订货」，同一批货会在两个视角各出现一次。
SCOPE_ALL = "all"


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------


def load_params() -> R.PolicyParams:
    """从 master.replenishment_config 读参数，表不存在/为空时用默认值。"""
    params = R.DEFAULT_PARAMS
    try:
        init_master_db()
        with closing(sqlite3.connect(config.MASTER_DB)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM replenishment_config WHERE id=1").fetchone()
    except sqlite3.Error:
        return params
    if row is None:
        return params

    changes: dict = {}
    for key in row.keys():
        if key == "id" or key not in R.PolicyParams.__dataclass_fields__:
            continue
        if row[key] is None:
            continue
        current = getattr(params, key)
        changes[key] = int(row[key]) if isinstance(current, int) else float(row[key])
    return params.replace(**changes) if changes else params


def params_from_overrides(params: R.PolicyParams, raw: dict) -> R.PolicyParams:
    """把 ?lead=3&review=7 之类的探索性参数叠上去（不落库）。"""
    changes: dict = {}
    for arg, field, caster in (
        ("window", "window_days", int),
        ("lead", "dc_lead_days", int),
        ("store_lead", "store_lead_days", int),
        ("review", "dc_review_days", int),
        ("store_review", "store_review_days", int),
        ("overstock", "overstock_days", float),
    ):
        if arg not in raw:
            continue
        try:
            changes[field] = caster(raw[arg])
        except (TypeError, ValueError):
            continue
    return params.replace(**changes) if changes else params


# ---------------------------------------------------------------------------
# 取数
# ---------------------------------------------------------------------------


# 不参与补货计算的仓类型：研发中心没有实体库存流转（实测 125 个品项
# 全部 0 库存 0 动账），把它算进来只会稀释「无消耗品项」的分母。
_EXCLUDED_TYPES = ("rd",)


def _scan_warehouses() -> dict:
    """一次扫表，把「能算的 / 库文件缺失的 / 按类型不参与的」分开。

    分开的理由：研发中心（type=rd）本来就不做补货，把它报成「库文件缺失」
    是指错原因；而真缺失的仓要显式告警，不能悄悄少算一个仓库的库存。
    页面上一律用**名字**，不暴露 wh_000 这种内部代号。
    """
    init_master_db()
    with closing(sqlite3.connect(config.MASTER_DB)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT code, name, db_path, warehouse_type FROM warehouses ORDER BY "
            "CASE warehouse_type WHEN 'distribution_center' THEN 0 "
            "WHEN 'storefront' THEN 1 ELSE 2 END, code"
        ).fetchall()

    live: list[dict] = []
    missing: list[dict] = []
    excluded: list[dict] = []
    for r in rows:
        wh_type = r["warehouse_type"] or "storefront"
        label = {"code": r["code"], "name": r["name"] or r["code"], "warehouse_type": wh_type}
        if wh_type in _EXCLUDED_TYPES:
            excluded.append(label)
            continue
        path = Path(r["db_path"] or "")
        if not path.is_absolute():
            path = config.BASE_DIR / path
        if not path.exists():
            # 登记了但库文件缺失（蓝绿切换窗口 / 误删）——跳过而不是崩。
            missing.append(label)
            continue
        live.append({**label, "db_path": str(path)})
    return {"live": live, "missing": missing, "excluded": excluded}


def list_warehouses() -> list[dict]:
    """参与补货计算的仓（DC 排最前，然后门店）。"""
    return _scan_warehouses()["live"]


def _read_items(wh: dict, params: R.PolicyParams, today: date) -> tuple[dict, dict]:
    """返回 (items by id, 日消耗序列 by item id)。

    整仓一条 GROUP BY，而不是逐品项查询 —— 旧实现每 GET 要打 ~50 条 SQL。
    """
    visible = store_selectable_clause("i")
    actions = _DC_CONSUME_ACTIONS if wh["warehouse_type"] == DC_TYPE else _STORE_CONSUME_ACTIONS
    start = (today - timedelta(days=params.window_days - 1)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")

    with closing(sqlite3.connect(wh["db_path"])) as conn:
        conn.row_factory = sqlite3.Row
        item_rows = conn.execute(
            f"""SELECT i.id, i.sku, i.name, i.unit, i.quantity, i.unit_cost,
                       i.canonical_id, i.is_orderable,
                       COALESCE(c.name, '未分类') AS category
                FROM items i
                LEFT JOIN categories c ON c.id = i.category_id
                WHERE {visible}"""
        ).fetchall()

        move_rows = conn.execute(
            f"""SELECT item_id, date(created_at) AS d, SUM(-delta) AS qty
                FROM stock_movements
                WHERE action IN {actions} AND {_LOSS_FILTER}
                  AND date(created_at) >= ? AND date(created_at) <= ?
                GROUP BY item_id, d""",
            (start, end),
        ).fetchall()

        # 回退是正 delta（回加库存），从当日消耗里扣掉，避免高估。
        rollback_rows = conn.execute(
            f"""SELECT item_id, date(created_at) AS d, SUM(delta) AS qty
                FROM stock_movements
                WHERE action IN {_ROLLBACK_ACTIONS}
                  AND date(created_at) >= ? AND date(created_at) <= ?
                GROUP BY item_id, d""",
            (start, end),
        ).fetchall()

        # 老流程的补货在途（restock_requests 未到货部分）
        transit_rows = conn.execute(
            f"""SELECT item_id, SUM(requested_quantity) AS qty
                FROM restock_requests
                WHERE status NOT IN {_RESTOCK_CLOSED}
                GROUP BY item_id"""
        ).fetchall()

    items = {int(r["id"]): dict(r) for r in item_rows}

    events: dict[int, list[tuple[str, float]]] = defaultdict(list)
    for r in move_rows:
        events[int(r["item_id"])].append((str(r["d"]), float(r["qty"] or 0.0)))
    for r in rollback_rows:
        events[int(r["item_id"])].append((str(r["d"]), -abs(float(r["qty"] or 0.0))))

    series = {
        iid: R.build_daily_series(events.get(iid, []), today, params.window_days)
        for iid in items
    }
    for r in transit_rows:
        iid = int(r["item_id"])
        if iid in items:
            items[iid]["on_order_restock"] = float(r["qty"] or 0.0)
    return items, series


# ---------------------------------------------------------------------------
# 三口径取数：盘点实测 / 产量推算
# ---------------------------------------------------------------------------


def _cumulative_inbound(conn, start: str, end: str) -> dict[int, list[tuple[str, float]]]:
    """每个品项的入库累计序列 [(ts, cum_qty), ...]，供区间二分查值。"""
    rows = conn.execute(
        f"""SELECT item_id, created_at, delta
            FROM stock_movements
            WHERE action IN {_INBOUND_ACTIONS}
              AND created_at > ? AND created_at <= ?
            ORDER BY item_id, created_at""",
        (start, end),
    ).fetchall()
    out: dict[int, list[tuple[str, float]]] = defaultdict(list)
    running: dict[int, float] = defaultdict(float)
    for r in rows:
        iid = int(r["item_id"])
        running[iid] += float(r["delta"] or 0.0)
        out[iid].append((str(r["created_at"]), running[iid]))
    return out


def _inbound_between(
    cum: dict[int, list[tuple[str, float]]], item_id: int, t0: str, t1: str
) -> float:
    """区间 (t0, t1] 内的入库量；用累计序列差值，避免每个区间再打一次 SQL。"""
    series = cum.get(item_id)
    if not series:
        return 0.0

    def _at(ts: str) -> float:
        # 取最后一个 created_at <= ts 的累计值
        lo, hi, val = 0, len(series) - 1, 0.0
        while lo <= hi:
            mid = (lo + hi) // 2
            if series[mid][0] <= ts:
                val = series[mid][1]
                lo = mid + 1
            else:
                hi = mid - 1
        return val

    return _at(t1) - _at(t0)


def _read_count_channel(
    conn, today: date, params: R.PolicyParams
) -> tuple[dict[int, R.ChannelRate], dict[str, float]]:
    """盘点锚定口径：C = I_start + 入库 − I_end，逐盘点周期累加。

    这是整套方案里唯一**不依赖领料/出库记录**的消耗估计。它只在两次盘点
    之间成立，所以频率低（每周一次），但无系统性偏差 —— 领料漏记、盘亏
    混记都不会影响它。

    返回 (per-item ChannelRate, {item_id: 累计消失量})。
    """
    start = (today - timedelta(days=params.window_days - 1)).strftime("%Y-%m-%d 00:00:00")
    end = today.strftime("%Y-%m-%d 23:59:59")
    batches = conn.execute(
        """SELECT id, created_at FROM stocktake_batches
           WHERE status = 'approved' AND rolled_back = 0
             AND created_at >= ? AND created_at <= ?
           ORDER BY created_at""",
        (start, end),
    ).fetchall()
    if len(batches) < 2:
        return {}, {}

    ids = [b["id"] for b in batches]
    placeholders = ",".join("?" * len(ids))
    counts: dict[int, dict[int, float]] = defaultdict(dict)
    for r in conn.execute(
        f"SELECT batch_id, item_id, actual_quantity FROM stocktakes "
        f"WHERE batch_id IN ({placeholders})",
        ids,
    ):
        counts[int(r["batch_id"])][int(r["item_id"])] = float(r["actual_quantity"] or 0.0)

    cum = _cumulative_inbound(conn, start, end)

    qty: dict[int, float] = defaultdict(float)
    days: dict[int, float] = defaultdict(float)
    samples: dict[int, int] = defaultdict(int)
    outliers: dict[int, int] = defaultdict(int)

    for prev, cur in zip(batches, batches[1:], strict=False):
        span = (
            datetime.fromisoformat(str(cur["created_at"]))
            - datetime.fromisoformat(str(prev["created_at"]))
        ).total_seconds() / 86400.0
        if span < 1.0:
            # 不足 1 天的区间不是经营周期：往往是同日/次日的盘盈盘亏冲销对，
            # 相减会得出一个数量级错误。真实周盘间隔中位数是 7 天。
            continue
        a = counts.get(int(prev["id"]), {})
        b = counts.get(int(cur["id"]), {})
        for item_id in a.keys() & b.keys():
            consumed = (
                a[item_id]
                + _inbound_between(cum, item_id, str(prev["created_at"]), str(cur["created_at"]))
                - b[item_id]
            )
            if consumed < -1e-9:
                # 负消耗 = 入库漏记或盘点错。整段丢弃而不是截断到 0：
                # 截断会把「数据坏了」伪装成「没消耗」。
                outliers[item_id] += 1
                continue
            qty[item_id] += consumed
            days[item_id] += span
            samples[item_id] += 1

    # 刻意不在这里做「单区间跳变剔除」：本数据集里大量品项是间歇消耗，
    # 中位区间速率为 0，任何按中位数倍数设定的阈值都会把真实的大额领用
    # 一并删掉。异常由 fuse_demand() 的**跨口径数量级校验**负责 ——
    # 一个品项的盘点速率若达到领料速率的 10 倍以上，整条盘点通道弃用，
    # 这比逐区间猜测更稳，也不会把长尾品项的真实消耗误杀。
    rates_out = {
        item_id: R.channel_rate(
            qty[item_id], days[item_id], samples[item_id], R.METHOD_COUNT, outliers[item_id]
        )
        for item_id in qty
        if days[item_id] > 0
    }
    return rates_out, dict(qty)


def _read_production_channel(
    conn, today: date, params: R.PolicyParams
) -> dict[int, dict]:
    """产量口径：rate = 产量/天 × 标准单耗。

    注意：`production_run_items.actual_qty` 与 `生产消耗` 动账是同一本账
    （逐品项集合与数量完全一致），所以**不能**把 actual_qty 当成独立信号。
    独立的是 `production_runs.output_qty`（产量）—— 成品不做库存管理，
    生产即销售，产量可以当销量代理。

    标准单耗取自 `product_bom.qty_per_unit`，需按 `items.gram_per_unit`
    折算到库存单位；BOM 是可变的当前态表，历史 run 可能已对不上，
    因此只在 BOM 行存在时计入 std 分母，其余只计产量。
    """
    start = (today - timedelta(days=params.window_days - 1)).strftime("%Y-%m-%d 00:00:00")
    end = today.strftime("%Y-%m-%d 23:59:59")
    rows = conn.execute(
        """SELECT pri.item_id                       AS item_id,
                  SUM(r.output_qty)                 AS out_qty,
                  SUM(pri.actual_qty)               AS used_qty,
                  SUM(CASE WHEN b.qty_per_unit IS NOT NULL AND r.output_qty > 0
                           THEN r.output_qty * b.qty_per_unit
                                / (CASE WHEN i.gram_per_unit > 0 THEN i.gram_per_unit ELSE 1 END)
                      END)                          AS std_qty,
                  SUM(CASE WHEN b.qty_per_unit IS NOT NULL THEN r.output_qty ELSE 0 END) AS std_out
           FROM production_run_items pri
           JOIN production_runs r ON r.id = pri.run_id
           JOIN items i ON i.id = pri.item_id
           LEFT JOIN product_bom b
                  ON b.product_id = r.product_id AND b.item_id = pri.item_id
           WHERE r.rolled_back = 0 AND r.created_at >= ? AND r.created_at <= ?
           GROUP BY pri.item_id""",
        (start, end),
    ).fetchall()

    days = float(params.window_days)
    out: dict[int, dict] = {}
    for r in rows:
        item_id = int(r["item_id"])
        out_qty = float(r["out_qty"] or 0.0)
        used = float(r["used_qty"] or 0.0)
        std_qty = float(r["std_qty"] or 0.0)
        std_out = float(r["std_out"] or 0.0)
        if out_qty <= 0:
            continue
        unit_std = std_qty / std_out if std_out > 0 else 0.0
        unit_actual = used / out_qty
        out[item_id] = {
            "rate": R.channel_rate(out_qty * unit_std, days, int(out_qty), R.METHOD_PRODUCTION),
            "output_qty": R._q(out_qty, 4),
            "unit_usage_std": R._q(unit_std, 6),
            "unit_usage_actual": R._q(unit_actual, 6),
            "yield_ratio": R.yield_ratio(unit_actual, unit_std),
        }
    return out


def _read_channels(wh: dict, params: R.PolicyParams, today: date) -> dict[int, dict]:
    """一路仓的三口径包：{issue, count, production, yield_ratio, production_qty}。"""
    try:
        with closing(sqlite3.connect(wh["db_path"])) as conn:
            conn.row_factory = sqlite3.Row
            count_rates, count_qty = _read_count_channel(conn, today, params)
            prod = _read_production_channel(conn, today, params)
    except sqlite3.Error:
        return {}

    out: dict[int, dict] = {}
    for item_id in set(count_rates) | set(prod):
        p = prod.get(item_id, {})
        out[item_id] = {
            "count": count_rates.get(item_id),
            "count_qty": count_qty.get(item_id),
            "production": p.get("rate"),
            "production_qty": p.get("output_qty", 0.0),
            "yield_ratio": p.get("yield_ratio"),
        }
    return out


def _category_bias(
    channels: dict[int, dict], items: dict, categories: dict[int, str]
) -> dict[str, float]:
    """品类级口径修正系数：Σ盘点消失量 ÷ Σ领料量。

    用于「本仓/本品类还没有足够盘点覆盖」时给领料速率兜底修正。用品类而不是
    用全局，是因为不同品类的损耗结构差别很大（酱料按桶称量 vs 包材按箱领用）。
    """
    per_cat: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for item_id, ch in channels.items():
        count = ch.get("count")
        if count is None or count.qty <= 0:
            continue
        cat = categories.get(item_id, "未分类")
        issue = ch.get("issue_qty") or 0.0
        per_cat[cat][0] += float(count.qty)
        per_cat[cat][1] += float(issue)
    return {
        cat: R.bias_factor(total_count, total_issue)
        for cat, (total_count, total_issue) in per_cat.items()
        if total_count > 0 and total_issue > 0
    }


def _store_open_orders() -> dict[tuple[str, int], float]:
    """{(store_code, canonical_id): 已下单未收货数量}。

    不算进去就会重复下单 —— 这是补货看板最常见的错。
    """
    out: dict[tuple[str, int], float] = {}
    try:
        with closing(sqlite3.connect(config.MASTER_DB)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""SELECT o.store_warehouse_code AS store_code,
                           oi.canonical_id AS canonical_id,
                           SUM(oi.quantity - COALESCE(oi.fulfilled_quantity, 0)) AS qty
                    FROM store_orders o
                    JOIN store_order_items oi ON oi.order_id = o.id
                    WHERE o.status IN {_STORE_ORDER_OPEN}
                      AND oi.status != 'cancelled'
                    GROUP BY o.store_warehouse_code, oi.canonical_id"""
            ).fetchall()
    except sqlite3.Error:
        return out
    for r in rows:
        qty = float(r["qty"] or 0.0)
        if qty > 0 and r["canonical_id"] is not None:
            key = (str(r["store_code"]), int(r["canonical_id"]))
            out[key] = out.get(key, 0.0) + qty
    return out


def store_stock_and_consumption(
    days: int = 7, today: date | None = None
) -> tuple[dict[int, dict[str, float]], dict[int, float], list[dict], list[str]]:
    """门店侧按 canonical_id 汇总的「当前库存」与「近 N 天实际消耗」。

    返回 ``(stock_by_cid, consumed_by_cid, stores, failed_store_codes)``：

    * ``stock_by_cid[canonical_id][store_code] = 数量``
    * ``consumed_by_cid[canonical_id] = 近 N 天全部门店消耗合计``
    * ``stores = [{"code", "name"}, ...]``（门店顺序，供邮件列固定列序）
    * ``failed_store_codes``：整库读取失败的门店 —— 调用方必须把这些店渲染成
      「—」而不是 0。「这家店没货」和「这家店读不出来」显示成一样的 0，
      会让采购员按缺货下单，这是比不发邮件更糟的错误。

    为什么单独取一遍而不是从 `build_board()` 的 rows 里凑
    ---------------------------------------------------
    看板行里的 ``rate`` 是**加窗估计的日均**（含近期加权），不是「最近 7 天
    实际消耗」。Issue #145 要的是后者 —— 用 ``rate * 7`` 会在消耗波动时给出
    明显偏离现实的数字（门店消耗是周内脉冲式的），所以这里直接按日聚合动账。
    另外看板 rows 只含库存，不含「按 canonical 维度跨店合并」的口径。

    口径与看板一致：门店消耗 = 「生产消耗」+「出库」，剔除盘点盈亏/盘点调整，
    扣掉回退（正 delta）。可见性用 `store_selectable_clause`，与看板同源。
    """
    day = today or date.today()
    span = max(1, int(days))
    start = (day - timedelta(days=span - 1)).strftime("%Y-%m-%d")
    end = day.strftime("%Y-%m-%d")

    stock_by_cid: dict[int, dict[str, float]] = defaultdict(dict)
    consumed_by_cid: dict[int, float] = defaultdict(float)
    stores: list[dict] = []
    failed_stores: list[str] = []

    visible = store_selectable_clause("i")
    for wh in list_warehouses():
        if wh["warehouse_type"] != "storefront":
            continue
        stores.append({"code": wh["code"], "name": wh["name"]})
        try:
            with closing(sqlite3.connect(wh["db_path"])) as conn:
                conn.row_factory = sqlite3.Row
                stock_rows = conn.execute(
                    f"""SELECT i.canonical_id AS cid, SUM(i.quantity) AS qty
                        FROM items i
                        WHERE {visible} AND i.canonical_id IS NOT NULL
                        GROUP BY i.canonical_id"""
                ).fetchall()
                move_rows = conn.execute(
                    f"""SELECT i.canonical_id AS cid, SUM(-m.delta) AS qty
                        FROM stock_movements m
                        JOIN items i ON i.id = m.item_id
                        WHERE m.action IN {_STORE_CONSUME_ACTIONS}
                          AND {_loss_filter("m.")}
                          AND date(m.created_at) >= ? AND date(m.created_at) <= ?
                          AND {visible} AND i.canonical_id IS NOT NULL
                        GROUP BY i.canonical_id""",
                    (start, end),
                ).fetchall()
                rollback_rows = conn.execute(
                    f"""SELECT i.canonical_id AS cid, SUM(m.delta) AS qty
                        FROM stock_movements m
                        JOIN items i ON i.id = m.item_id
                        WHERE m.action IN {_ROLLBACK_ACTIONS}
                          AND date(m.created_at) >= ? AND date(m.created_at) <= ?
                          AND {visible} AND i.canonical_id IS NOT NULL
                        GROUP BY i.canonical_id""",
                    (start, end),
                ).fetchall()
        except sqlite3.Error:
            # 单仓坏库不影响其它门店汇总，但必须**记名**上报：静默跳过会让
            # 邮件里那一列显示成 0，看起来像「这家店确实没货」——采购员据此
            # 下单就是错单。这一列要显示「—」并附告警。
            failed_stores.append(wh["code"])
            continue

        for r in stock_rows:
            stock_by_cid[int(r["cid"])][wh["code"]] = float(r["qty"] or 0.0)
        for r in move_rows:
            consumed_by_cid[int(r["cid"])] += float(r["qty"] or 0.0)
        for r in rollback_rows:
            consumed_by_cid[int(r["cid"])] -= abs(float(r["qty"] or 0.0))

    # 回退超过消耗会算出负数（历史数据错账）。截到 0：邮件里「近 7 天消耗 -3」
    # 只会让人怀疑数字，不会提供任何决策价值。
    return (
        dict(stock_by_cid),
        {cid: max(0.0, qty) for cid, qty in consumed_by_cid.items()},
        stores,
        failed_stores,
    )


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------


def load_routing_map() -> dict[int, str]:
    """canonical_id → 履约来源仓（item_routing，租户 1）。

    开关关闭或路由表不可读时返回空 dict：调用方据此回落到现状算法
    （所有 DC 都按下游拉动），保证开关关时行为恒等。路由存在但某个
    canonical 未配置 → 该 canonical 同样维持现状算法，不静默归零。
    """
    if not getattr(config, "MULTI_WAREHOUSE_FULFILLMENT", False):
        return {}
    try:
        init_master_db()
        with closing(sqlite3.connect(config.MASTER_DB)) as conn:
            rows = conn.execute(
                "SELECT canonical_id, fulfillment_warehouse_code FROM item_routing "
                "WHERE tenant_id = 1"
            ).fetchall()
    except sqlite3.Error:
        return {}
    return {int(cid): str(code) for cid, code in rows}


def build_board(
    today: date | None = None,
    params: R.PolicyParams | None = None,
    supply: str = SUPPLY_ALL,
    scope: str = SCOPE_ALL,
) -> dict:
    """跑完整个看板计算，返回可直接喂模板的 dict。

    1. 逐仓取 items + 日消耗序列
    2. 按 canonical_id 汇总门店消耗序列 → DC 的拉动需求
    3. 全链 ABC（按年消耗金额），保证 A/B/C 在各仓同义
    4. 逐 (仓, 品项) 出补货计划
    5. 按品类 / 仓库 / 状态聚合

    `scope` 只影响**展示与汇总**，不影响任何一行的计算：DC 的日需求永远
    取「全部门店消耗之和」（下游拉动），所以切到 DC 视角时看到的仍然是
    全局需求 —— 这正是「我作为配送中心要考虑各门店」的含义。

    多仓履约（spec §10）：门店需求只拉动 `item_routing` 指向的来源仓；
    同 canonical 的非来源仓镜像保留自己的水位逻辑（用本仓出库史），不承接
    门店需求。未配置路由的 canonical 保持上面这套现状算法不变。
    """
    params = params or load_params()
    today = today or date.today()
    scan = _scan_warehouses()
    warehouses = scan["live"]
    skipped: list[dict] = scan["missing"]
    excluded: list[dict] = scan["excluded"]
    routing = load_routing_map()

    raw: dict[str, tuple[dict, dict]] = {}
    failed: list[dict] = []
    for wh in warehouses:
        try:
            raw[wh["code"]] = _read_items(wh, params, today)
        except sqlite3.Error as exc:  # 单仓坏库不能拖垮整板
            failed.append({"code": wh["code"], "name": wh["name"], "error": str(exc)})

    # --- 下游门店消耗序列（按 canonical_id 汇总，含零需求日）---
    downstream: dict[int, list[list[float]]] = defaultdict(list)
    for wh in warehouses:
        if wh["warehouse_type"] != "storefront":
            continue
        items, series = raw.get(wh["code"], ({}, {}))
        for iid, item in items.items():
            cid = item.get("canonical_id")
            if cid is None:
                continue
            downstream[int(cid)].append(series[iid])
    downstream_series = {cid: R.aggregate_series(s) for cid, s in downstream.items()}

    # --- 全链 ABC ---
    annual_by_cid: dict[int, float] = defaultdict(float)
    annual_by_pair: dict[tuple[str, int], float] = {}
    for wh in warehouses:
        items, series = raw.get(wh["code"], ({}, {}))
        for iid, item in items.items():
            est = R.estimate_demand(series[iid], params)
            annual = est.rate * 365.0 * float(item.get("unit_cost") or 0.0)
            annual_by_pair[(wh["code"], iid)] = annual
            cid = item.get("canonical_id")
            if cid is not None:
                annual_by_cid[int(cid)] += annual

    cid_keys = list(annual_by_cid)
    cid_abc = dict(zip(cid_keys, R.abc_classify([annual_by_cid[c] for c in cid_keys], params), strict=True))

    # canonical 未绑定的品项按本仓金额单独分层
    unbound_keys = [
        key for key in annual_by_pair
        if (raw.get(key[0], ({}, {}))[0].get(key[1]) or {}).get("canonical_id") is None
    ]
    pair_abc = dict(zip(
        unbound_keys,
        R.abc_classify([annual_by_pair[k] for k in unbound_keys], params),
        strict=True,
    ))

    open_orders = _store_open_orders()

    # 订货目录开关：门店能不能订某个品项，取决于 **DC** 的 is_orderable
    # （`store_ordering_pure.list_available_dc_items` 只放行 DC 侧 =1 的品项），
    # 跟门店自己那行无关。DC 行才用它自己的标记。
    #
    # 多仓（spec §10）：有路由时开关从**来源仓**解析。旧实现按仓遍历 last-wins，
    # 双 DC 镜像同 canonical 时会被后一个 DC 覆盖，来源仓自采（=0）的品项会被
    # 错误当成统配。未配置路由的 canonical 维持 last-wins 现状。
    dc_orderable: dict[int, bool] = {}
    orderable_by_dc: dict[str, dict[int, bool]] = {}
    for wh in warehouses:
        if wh["warehouse_type"] != DC_TYPE:
            continue
        flags: dict[int, bool] = {}
        for _iid, _item in raw.get(wh["code"], ({}, {}))[0].items():
            _cid = _item.get("canonical_id")
            if _cid is not None:
                value = bool(_item.get("is_orderable", 1))
                flags[int(_cid)] = value
                dc_orderable[int(_cid)] = value
        orderable_by_dc[wh["code"]] = flags
    for _cid, _source_code in routing.items():
        source_flags = orderable_by_dc.get(_source_code)
        if source_flags is not None and _cid in source_flags:
            dc_orderable[_cid] = source_flags[_cid]
    # 只认「来源仓确实绑定该 canonical」的路由：过期/脏路由不生效，
    # 避免把需求静默判给一个没有这行的仓（=静默归零）。
    routed_source: dict[int, str] = {
        cid: code for cid, code in routing.items()
        if cid in orderable_by_dc.get(code, {})
    }

    # --- 三口径：盘点实测 + 产量推算（领料口径在各仓循环里从 use_series 现算）---
    channel_raw: dict[str, dict[int, dict]] = {}
    for wh in warehouses:
        channel_raw[wh["code"]] = _read_channels(wh, params, today)

    # 品类偏差系数：需要「盘点消失量 ÷ 领料量」，所以先算每个品项的领料总量
    cat_bias_by_wh: dict[str, dict[str, float]] = {}
    for wh in warehouses:
        items, series = raw.get(wh["code"], ({}, {}))
        issue_qty = {iid: sum(series[iid]) for iid in items}
        cats = {iid: str(items[iid].get("category") or "未分类") for iid in items}
        merged = {
            iid: dict(ch, issue_qty=issue_qty.get(iid, 0.0))
            for iid, ch in channel_raw.get(wh["code"], {}).items()
        }
        cat_bias_by_wh[wh["code"]] = _category_bias(merged, items, cats)

    plans: list[R.ItemPlan] = []
    dc_leftover: list[dict] = []
    for wh in warehouses:
        items, series = raw.get(wh["code"], ({}, {}))
        is_dc = wh["warehouse_type"] == DC_TYPE
        wh_channels = channel_raw.get(wh["code"], {})
        wh_bias = cat_bias_by_wh.get(wh["code"], {})
        for iid, item in items.items():
            cid = item.get("canonical_id")
            use_series = series[iid]
            source = "own"
            if is_dc and cid is not None and int(cid) in downstream_series:
                # 有路由时只有来源仓承接门店需求；镜像仓留在自己那条出库史
                # 上算水位（不承接、也不归零）。无路由 → 现状（全部承接）。
                routed = routed_source.get(int(cid))
                if routed is None or routed == wh["code"]:
                    use_series = downstream_series[int(cid)]
                    source = "downstream"

            on_order = float(item.get("on_order_restock") or 0.0)
            if not is_dc and cid is not None:
                on_order += open_orders.get((wh["code"], int(cid)), 0.0)

            abc = pair_abc.get((wh["code"], iid))
            if abc is None and cid is not None:
                abc = cid_abc.get(int(cid), "C")

            # 门店侧以 DC 的目录开关为准；DC 侧用本仓自己的标记。
            # 业务含义（owner 2026-10-20 确认）：is_orderable=0 = 该品项已改为
            # **门店自采**，不再由配送中心统配备货，不是"暂时下不了单"。
            if is_dc:
                orderable = bool(item.get("is_orderable", 1))
            elif cid is not None and int(cid) in dc_orderable:
                orderable = dc_orderable[int(cid)]
            else:
                orderable = True

            if is_dc and not orderable:
                # DC 不再统配的品项：DC 这一行不参与补货计算。
                # 否则会把「门店消耗」算成「DC 该采购多少」——这正是
                # 门店自采品项曾经虚增 DC 采购额的原因。
                # 结存单独记账，用于清库提示，不进订单。
                qty_left = float(item.get("quantity") or 0.0)
                if qty_left > 0:
                    dc_leftover.append({
                        "warehouse_name": wh["name"],
                        "name": item.get("name") or "",
                        "sku": item.get("sku") or "",
                        "quantity": R._q(qty_left, 2),
                        "unit": item.get("unit") or "",
                        "value": R._q(qty_left * float(item.get("unit_cost") or 0.0), 2),
                    })
                continue

            # 领料通道 = 当前模型使用的那条序列本身（DC 是下游汇总），
            # 保证融合时「修正系数」比较的是同一口径，不会把 DC 的拉动速率
            # 悄悄换成 DC 自己的发货速率。
            issue = R.channel_rate(
                sum(use_series),
                params.window_days,
                sum(1 for v in use_series if v > 0),
                R.METHOD_ISSUE,
            )
            ch = wh_channels.get(iid)
            channels = None
            if ch:
                channels = {
                    "issue": issue,
                    "count": ch.get("count"),
                    "count_qty": ch.get("count_qty"),
                    "production": ch.get("production"),
                    "production_qty": ch.get("production_qty", 0.0),
                    "yield_ratio": ch.get("yield_ratio"),
                }

            plans.append(R.plan_item(
                warehouse=wh,
                item=dict(item, on_order=on_order),
                series=use_series,
                abc=abc or "C",
                params=params,
                demand_source=source,
                channels=channels,
                category_bias=wh_bias.get(str(item.get("category") or "未分类"), 1.0),
                orderable=orderable,
            ))

    return _assemble(
        [p.as_dict() for p in plans],
        warehouses,
        params,
        skipped=skipped,
        excluded=excluded,
        failed=failed,
        supply=supply,
        scope=scope,
        dc_leftover=dc_leftover,
    )


def _assemble(
    rows: list[dict],
    warehouses: list[dict],
    params: R.PolicyParams,
    skipped: list[dict] | None = None,
    excluded: list[dict] | None = None,
    failed: list[dict] | None = None,
    supply: str = SUPPLY_ALL,
    scope: str = SCOPE_ALL,
    dc_leftover: list[dict] | None = None,
) -> dict:
    """把逐品项计划滚成 KPI / 仓库 / 品类 / 矩阵视图。

    两道滤镜，正交：
      * `supply` 统配 / 自采 —— 这笔钱由谁出；
      * `scope`  全链 / 某仓 —— 站在谁的立场看。

    两者都放在**聚合之前**。只过滤表格会让 KPI 和表对不上 ——
    这是这类看板最容易出的自相矛盾（本期已踩过两次）。
    各自的小计都按「另一维当前取值」算，所以两条不变式同时成立：
      统配 + 自采 = 全部（同一视角内）；各仓之和 = 全链（同一供应方式内）。
    """
    supply = supply if supply in SUPPLY_MODES else SUPPLY_ALL
    codes = {wh["code"] for wh in warehouses}
    scope = scope if (scope == SCOPE_ALL or scope in codes) else SCOPE_ALL
    action_status = (R.STATUS_CRITICAL, R.STATUS_DUE)
    for r in rows:
        r["supply"] = SUPPLY_CENTRAL if r.get("orderable") else SUPPLY_SELF

    # 供应方式小计：按当前视角算，否则「统配+自采=全部」在切到单仓后不成立
    supply_modes = []
    for mode in SUPPLY_MODES:
        subset = rows if mode == SUPPLY_ALL else [r for r in rows if r["supply"] == mode]
        if scope != SCOPE_ALL:
            subset = [r for r in subset if r["warehouse_code"] == scope]
        supply_modes.append({
            "key": mode,
            "label": SUPPLY_LABELS[mode],
            "item_count": len(subset),
            "action_items": sum(1 for r in subset if r["status"] in action_status),
            "suggested_value": R._q(
                sum(float(r["suggested_value"]) for r in subset), 2
            ),
        })

    # 先按供应方式过滤，再在「全部仓」上算仓库小计 —— 视角标签需要各仓
    # 自己的数，不能先砍掉未选中的仓
    if supply != SUPPLY_ALL:
        rows = [r for r in rows if r["supply"] == supply]

    by_wh_all: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_wh_all[r["warehouse_code"]].append(r)

    wh_rollups = []
    for wh in warehouses:
        ru = R.rollup(wh["code"], by_wh_all.get(wh["code"], []))
        wh_rollups.append({
            "code": wh["code"],
            "name": wh["name"],
            "type": wh["warehouse_type"],
            "is_dc": wh["warehouse_type"] == DC_TYPE,
            "stock_value": ru.stock_value,
            "monthly_value": ru.monthly_value,
            "suggested_value": ru.suggested_value,
            "ss_value": ru.ss_value,
            "coverage_days": ru.coverage_days,
            "sku_count": ru.sku_count,
            "action_count": ru.action_count,
            "critical_count": ru.critical_count,
            "overstock_count": ru.overstock_count,
            "no_signal_count": ru.no_signal_count,
            "value_coverage": ru.value_coverage,
            # 周转率：分子是消耗，分母是时点库存，基准是本仓自己的政策水位。
            # 每个仓的 L+R 不同（DC 3+7 / 门店 1+7），所以目标值必须按仓算，
            # 不能用全链均值套到每个仓上。
            "turnover": R.inventory_turnover(
                ru.stock_value, ru.monthly_value, ru.target_value
            ).as_dict(),
            "idle_value": ru.idle_value,
            "target_value": ru.target_value,
        })

    chain = R.rollup("__chain__", rows)
    scopes = [{
        "key": SCOPE_ALL,
        "label": "全链",
        "is_dc": False,
        "is_store": False,
        "sku_count": chain.sku_count,
        "action_items": sum(1 for r in rows if r["status"] in action_status),
        "suggested_value": R._q(sum(float(r["suggested_value"]) for r in rows if r["status"] in action_status), 2),
        "coverage_days": chain.coverage_days,
    }] + [{
        "key": w["code"],
        "label": w["name"],
        "is_dc": w["is_dc"],
        "is_store": not w["is_dc"],
        "sku_count": w["sku_count"],
        "action_items": w["action_count"],
        "suggested_value": w["suggested_value"],
        "coverage_days": w["coverage_days"],
    } for w in wh_rollups]

    # 门店需求分布：DC 视角下「各门店在拉多少」，也是门店视角的横向对照
    stores = [w for w in wh_rollups if not w["is_dc"]]
    store_total = R._q(sum(float(w["monthly_value"]) for w in stores), 2)
    store_demand = [{
        "code": w["code"],
        "name": w["name"],
        "monthly_value": w["monthly_value"],
        "share": R._q(float(w["monthly_value"]) / store_total, 4) if store_total > 0 else 0.0,
        "action_items": w["action_count"],
    } for w in sorted(stores, key=lambda x: -float(x["monthly_value"]))]

    if scope != SCOPE_ALL:
        rows = [r for r in rows if r["warehouse_code"] == scope]

    scope_meta = next((s for s in scopes if s["key"] == scope), scopes[0])

    actions = [r for r in rows if r["status"] in action_status]
    overstock = [r for r in rows if r["status"] == R.STATUS_OVERSTOCK]
    # 自采品项里需要补货的部分：不进 DC 采购单，但要让人看见
    # "这些是门店自己安排的量"，否则会误以为看板漏算。
    self_items = [r for r in rows if r["supply"] == SUPPLY_SELF and r["status"] in action_status]

    by_cat: dict[str, list[dict]] = defaultdict(list)
    by_wh: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_cat[r["category"]].append(r)
        by_wh[r["warehouse_code"]].append(r)

    total = R.rollup("__all__", rows)

    categories = []
    for name, items in by_cat.items():
        ru = R.rollup(name, items)
        unit_totals: dict[str, float] = defaultdict(float)
        for it in items:
            unit_totals[it["unit"]] += float(it["quantity"] or 0.0)
        categories.append({
            "name": name,
            "stock_value": ru.stock_value,
            "monthly_value": ru.monthly_value,
            "suggested_value": ru.suggested_value,
            "ss_value": ru.ss_value,
            "coverage_days": ru.coverage_days,
            "sku_count": ru.sku_count,
            "action_count": ru.action_count,
            "critical_count": ru.critical_count,
            "overstock_count": ru.overstock_count,
            "no_signal_count": ru.no_signal_count,
            "value_coverage": ru.value_coverage,
            # 单位异构（同一品类里有桶/箱/瓶/袋），数量不能跨品项相加，
            # 因此按单位分组给出，主口径用金额。
            "unit_totals": [
                {"unit": u, "qty": R._q(q, 2)}
                for u, q in sorted(unit_totals.items(), key=lambda kv: -kv[1])
                if q > 0
            ],
        })
    categories.sort(key=lambda c: -c["stock_value"])

    # 当前视角下要展示的仓：单仓视角只留自己，避免整块分布图全是空行
    wh_out = wh_rollups if scope == SCOPE_ALL else [
        w for w in wh_rollups if w["code"] == scope
    ]

    # 仓库 × 品类 金额矩阵（数量跨单位不可加，故用金额）
    matrix: dict[str, dict[str, float]] = {w["code"]: {} for w in wh_out}
    for r in rows:
        bucket = matrix.setdefault(r["warehouse_code"], {})
        cost = float(r["unit_cost"] or 0.0)
        bucket[r["category"]] = round(
            bucket.get(r["category"], 0.0) + float(r["quantity"] or 0.0) * cost, 2
        )

    status_counts: dict[str, int] = defaultdict(int)
    method_counts: dict[str, int] = defaultdict(int)
    source_counts: dict[str, int] = defaultdict(int)
    fuse_counts: dict[str, int] = defaultdict(int)
    for r in rows:
        status_counts[r["status"]] += 1
        method_counts[r["method"]] += 1
        source_counts[r["demand_source"]] += 1
        fuse_counts[r.get("fuse_method") or R.METHOD_ISSUE] += 1

    channels = _channel_summary(rows, by_cat)

    suggested_value = R._q(sum(float(r["suggested_value"]) for r in actions), 2)
    # 积压只统计「有需求信号且覆盖超阈值」的品项；无信号品项的库存单独记为
    # 呆滞（idle），否则 45 天阈值会把所有 0 需求的库存都算成"超量"，指标失真。
    excess_value = R._q(sum(float(r["excess_value"]) for r in overstock), 2)
    idle_value = R._q(sum(
        float(r["stock_value"]) for r in rows
        if r["status"] in (R.STATUS_DEAD, "no_signal")
    ), 2)
    # 整包装取整带来的额外金额：真实存在的"因为不能拆零，被迫多订"的成本。
    round_up_value = R._q(sum(
        float(r["round_up_extra"]) * float(r["unit_cost"]) for r in actions
    ), 2)
    raw_need_value = R._q(sum(
        float(r["raw_need"]) * float(r["unit_cost"]) for r in actions
    ), 2)
    over_rounding = [r for r in actions if r["over_rounding"]]
    over_rounding_value = R._q(
        sum(float(r["suggested_value"]) for r in over_rounding), 2
    )

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "params": {
            "window_days": params.window_days,
            "recent_weight": params.recent_weight,
            "store_review_days": params.store_review_days,
            "store_lead_days": params.store_lead_days,
            "dc_review_days": params.dc_review_days,
            "dc_lead_days": params.dc_lead_days,
            "overstock_days": params.overstock_days,
            "service_a": params.service_a,
            "service_b": params.service_b,
            "service_c": params.service_c,
            "order_cost": params.order_cost,
            "holding_rate": params.holding_rate,
        },
        "exposure": {
            "store": params.store_lead_days + params.store_review_days,
            "dc": params.dc_lead_days + params.dc_review_days,
        },
        "kpi": {
            "stock_value": total.stock_value,
            "monthly_value": total.monthly_value,
            "coverage_days": total.coverage_days,
            # 当前视角整体的库存周转（次/年），口径与仓库卡片一致
            "turnover": R.inventory_turnover(
                total.stock_value, total.monthly_value, total.target_value
            ).as_dict(),
            "target_value": total.target_value,
            "suggested_value": suggested_value,
            "ss_value": total.ss_value,
            "excess_value": excess_value,
            "idle_value": idle_value,
            "round_up_value": round_up_value,
            "raw_need_value": raw_need_value,
            "over_rounding_count": len(over_rounding),
            "over_rounding_value": over_rounding_value,
            "action_count": total.action_count,
            "critical_count": total.critical_count,
            "overstock_count": total.overstock_count,
            "no_signal_count": total.no_signal_count,
            "item_count": total.sku_count,
            "sku_valued": total.valued_sku,
            "warehouse_count": len(wh_out),
            "store_count": sum(1 for w in warehouses if w["warehouse_type"] == "storefront"),
            "action_items": len(actions),
            "self_count": len(self_items),
            "self_value": R._q(sum(float(r["suggested_value"]) for r in self_items), 2),
            "central_count": sum(
                1 for r in actions if r["supply"] == SUPPLY_CENTRAL
            ),
            "central_value": R._q(sum(
                float(r["suggested_value"]) for r in actions
                if r["supply"] == SUPPLY_CENTRAL
            ), 2),
            "value_coverage": total.value_coverage,
        },
        "categories": categories,
        "warehouses": wh_out,
        "matrix": matrix,
        # 视角（站在谁的立场看）
        "scope": scope,
        "scope_label": scope_meta["label"],
        "scope_is_dc": bool(scope_meta.get("is_dc")),
        "scope_is_store": bool(scope_meta.get("is_store")),
        "scopes": scopes,
        # 各门店在拉多少（DC 视角用来看「全局需求从哪来」）
        "store_demand": store_demand,
        "category_names": [c["name"] for c in categories],
        "actions": sorted(
            actions,
            key=lambda r: (-_urgency(r), bool(r["over_rounding"]), -float(r["suggested_value"])),
        ),
        "overstock": sorted(overstock, key=lambda r: -float(r["excess_value"])),
        "self_items": sorted(self_items, key=lambda r: -float(r["suggested_value"])),
        "supply": supply,
        "supply_label": SUPPLY_LABELS[supply],
        "supply_modes": supply_modes,
        # DC 上已改自采、但仍有结存的品项：不进订单，仅提示清库。
        "dc_leftover": dc_leftover or [],
        "rows": sorted(
            rows,
            key=lambda r: (-_urgency(r), bool(r["over_rounding"]), -float(r["stock_value"])),
        ),
        "status_counts": dict(status_counts),
        # 明细表渲染上限：456 行全渲会让页面到 390KB，移动端首屏明显变慢。
        # 完整数据走 /forecast/board.json。
        "detail_limit": 120,
        "status_labels": R.STATUS_LABELS,
        "status_tone": R.STATUS_TONE,
        "status_order": R.STATUS_ORDER,
        "method_counts": dict(method_counts),
        "source_counts": dict(source_counts),
        "fuse_counts": dict(fuse_counts),
        "channel_labels": R.CHANNEL_LABELS,
        "channels": channels,
        "data_quality": {
            "skipped_warehouses": skipped or [],     # 库文件缺失
            "excluded_warehouses": excluded or [],   # 按类型不参与（研发中心）
            "failed_warehouses": failed or [],
        },
    }


def _channel_summary(rows: list[dict], by_cat: dict[str, list[dict]]) -> dict:
    """三口径对账汇总。

    两条口径纪律：

    1. **同口径比较**。领料金额和盘点金额必须来自**同一批品项**，否则就是拿
       100 个品项的领料去比 60 个品项的盘点，差值毫无意义（实测这种错误
       会得出「领料比盘点高 107%」的假结论）。所以这里只统计两边都可用、
       且盘点通道被采纳（fuse_method=count）的品项。
    2. **金额而非数量**。同一品类内桶/箱/袋混用，数量不可加；把各口径日速率
       折算成月消耗金额才可比。偏差率本身无量纲，用来看结构。
    """

    trusted = [r for r in rows if r.get("fuse_method") == R.METHOD_COUNT]
    with_count = [r for r in rows if (r.get("rate_count") or 0) > 0]
    suspect = [r for r in rows if r.get("count_suspect")]
    signal_items = [r for r in rows if (r.get("rate") or 0) > 0]
    prod_items = [r for r in rows if (r.get("rate_production") or 0) > 0]

    def _sum_value(items: list[dict], rate_key: str) -> float:
        return R._q(sum(
            float(r.get(rate_key) or 0.0) * 30.0 * float(r.get("unit_cost") or 0.0)
            for r in items
        ), 2)

    issue_value = _sum_value(trusted, "rate_issue")
    count_value = _sum_value(trusted, "rate_count")
    prod_value = _sum_value(trusted, "rate_production")
    gaps = sorted(float(r["gap_ratio"]) for r in trusted if r.get("gap_ratio") is not None)

    def _median(values: list[float]) -> float | None:
        if not values:
            return None
        mid = len(values) // 2
        return R._q(values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2, 4)

    recon = []
    for name, items in by_cat.items():
        cat_trusted = [r for r in items if r.get("fuse_method") == R.METHOD_COUNT]
        cat_issue = sum(
            float(r.get("rate_issue") or 0.0) * 30.0 * float(r.get("unit_cost") or 0.0)
            for r in cat_trusted
        )
        cat_count = sum(
            float(r.get("rate_count") or 0.0) * 30.0 * float(r.get("unit_cost") or 0.0)
            for r in cat_trusted
        )
        cat_gaps = [float(r["gap_ratio"]) for r in cat_trusted if r.get("gap_ratio") is not None]
        recon.append({
            "name": name,
            "issue_value": R._q(cat_issue, 2),
            "count_value": R._q(cat_count, 2),
            "gap_value": R._q(cat_issue - cat_count, 2),
            "gap_pct": R._q((cat_issue - cat_count) / cat_count, 4) if cat_count > 0 else None,
            "median_gap": _median(sorted(cat_gaps)),
            "count_items": len(cat_trusted),
            "sku_count": len(items),
            "drift_items": sum(1 for g in cat_gaps if abs(g) > 0.10),
        })
    recon.sort(key=lambda c: -abs(c["gap_value"]))

    return {
        "issue_value": issue_value,
        "count_value": count_value,
        "production_value": prod_value,
        "gap_value": R._q(issue_value - count_value, 2),
        "gap_pct": R._q((issue_value - count_value) / count_value, 4) if count_value > 0 else None,
        "median_gap": _median(gaps),
        "drift_items": sum(1 for g in gaps if abs(g) > 0.10),
        "gap_items": len(gaps),
        "count_items": len(with_count),
        "trusted_items": len(trusted),
        "suspect_items": len(suspect),
        "production_items": len(prod_items),
        "signal_items": len(signal_items),
        "coverage": R._q(len(with_count) / len(signal_items), 3) if signal_items else 0.0,
        "recon": recon,
    }


def _urgency(row: dict) -> int:
    """排序权重：断货风险 > 本周需订 > 积压/不可订 > 其它。"""
    return {
        R.STATUS_CRITICAL: 3,
        R.STATUS_DUE: 2,
        R.STATUS_OVERSTOCK: 1,
    }.get(row.get("status"), 0)
