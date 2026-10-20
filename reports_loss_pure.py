"""品项级生产误差归因纯函数层（库存健康报表扩展）。

设计定稿：docs/2026-10-20-production-loss-design.md（§8~§16）。
本模块只做计算与判定，不碰 Flask（g/request/render）；db 是 sqlite3.Connection。

四层归因里本模块负责**能落到单个品项**的部分：
- L1 配方偏差：`planned_qty` vs `actual_qty`，只认真实填报行
- L2 旁路消耗：出库未填原因（`source_type='manual'` 且 reason 空）
- L3 账实损耗：盘点 `diff < 0`，含**盘盈盘亏冲销对**识别

判定铁律（§10.1）：
**机制证据（M）优先于统计证据（S）。** `confidence >= 0.8` 的判定必须至少含一条
M 级证据（BOM 系数为 0、reason 为空、品项不在任何 BOM、unit_cost=0）。
仅靠"数值接近""方向一致"这类 S 级信号时置信度封顶 0.65，并进待复核队列。

这条规矩来自实测教训：PP091 牛轧糖味出库 2208.18 与盘亏 2208.06 相差 0.005%，
看似"重复计量"，实际是先盘盈 +2208.35、次日盘亏 -2208.06 的冲销对，
出库那 2208.06 只是盘亏的副产物。数值巧合不能当因果。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta

from reports_health_pure import parse_event_ts

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 口径常量（改口径只动一处）
# ---------------------------------------------------------------------------
LOSS_LOOKBACK_DAYS = 180      # 归因回看窗口（天），与页面 range 解耦
REVERSAL_PAIR_HOURS = 48      # 冲销对两条盘点的最大间隔（小时）
REVERSAL_PAIR_TOL = 0.01      # 冲销对相对差容差（1%）
BOM_ZERO_EPS = 1e-9           # 计划量视为 0 的阈值
SYSTEMATIC_MIN_SAMPLES = 3    # 系统性同向偏差最少样本数
SYSTEMATIC_DIRECTION = 0.80   # 同向占比阈值
SYSTEMATIC_CV_MAX = 0.15      # 变异系数上限（越小越像固定系数错）
SPIKE_MULTIPLE = 3.0          # 单条盘亏超过该品项窗口出库量的倍数 → 异常
MULTI_PRODUCT_MIN = 5         # 共用产品数 ≥ 此值才提示归集难
LOSS_TOP_N = 10               # 页面展示条数上限

# 影响库存的流水动作：冲销对判定要排除这些，
# 防止把"先盘盈 10、后来真的用掉 10"误判成冲销。
#
# ⚠️ 不含 '库存调整'：该动作只由盘点审核（盘盈）产生，本身就是盘点的副产物。
# 同时下面按 note 排除 '盘点%' 前缀 —— 盘亏会写一条 action='出库' 的流水
# （stocktake.py:576），不排除它会让每一对盘盈盘亏都因"中间有出库"而配不上，
# 冲销对识别直接失效（实测踩过）。
_FLOW_ACTIONS = ("补货入库", "出库", "生产消耗", "调整出库", "门店订货出库",
                 "门店订货入库", "出库回退", "生产消耗回退")

# 原因码 → (大类, 中文名, 基准置信度)。数值即规则置信度（§10.3 / §10.4）
CAUSE_CODES: dict[str, tuple[str, str, float]] = {
    "H-ASYNC-ROLLBACK":   ("human", "回退不对称", 0.95),
    "H-REVERSAL-PAIR":    ("human", "盘盈盘亏冲销对", 0.90),
    "H-SPIKE-ENTRY":      ("human", "单条数量异常", 0.85),
    "H-REASON-BLANK":     ("human", "出库未填原因", 0.60),
    "H-OPERATOR-CLUSTER": ("human", "误差集中操作员", 0.65),
    "R-BOM-ZERO":         ("recipe", "配方系数为 0", 0.90),
    "R-BOM-MISSING":      ("recipe", "配方漏行", 0.90),
    "R-SYSTEMATIC":       ("recipe", "系统性同向偏差", 0.80),
    "R-UNIT-FACTOR":      ("recipe", "换算倍数错", 0.80),
    "P-NO-CHANNEL":       ("process", "无消耗登记渠道", 0.75),
    "P-MULTI-PRODUCT":    ("process", "多产品共用无法归集", 0.65),
    "P-ZERO-COST":        ("process", "无成本无法核算", 0.85),
    "P-STOCKTAKE-GAP":    ("process", "盘点周期过长", 0.70),
    "P-UNIT-UNCONVERT":   ("process", "单位换算缺失", 0.80),
    "unattributed":       ("unattributed", "待人工判定", 0.00),
}

CAUSE_LABELS = {"human": "人为操作", "recipe": "配方问题",
                "process": "流程设计", "unattributed": "待判定"}

_EMPTY = {"totals": {}, "by_cause": {}, "by_layer": {}, "top_rows": [],
          "self_reversed": [], "item_rates": [], "state": "absent",
          "blind_spots": []}


def _cause(code: str) -> tuple[str, str, float]:
    """原因码 → (大类, 中文名, 置信度)。未知码回退待判定。"""
    return CAUSE_CODES.get(code, ("unattributed", "待判定", 0.0))


# ---------------------------------------------------------------------------
# 数据装载
# ---------------------------------------------------------------------------

def _load_items(db) -> dict[int, dict]:
    """品项字典。canonical_sku 缺失时降级为 None，不让报表 500。

    本地快照库可能还没跑 canonical 迁移（`canonical_items` 表不存在），
    生产库有。跨仓汇总需要它，单仓展示不需要。
    """
    try:
        rows = db.execute(
            """SELECT i.id, i.sku, i.name, i.unit, i.unit_cost,
                      c.name AS category, ci.canonical_sku
               FROM items i
               LEFT JOIN categories c ON c.id = i.category_id
               LEFT JOIN canonical_items ci ON ci.id = i.canonical_id"""
        ).fetchall()
    except sqlite3.OperationalError:
        rows = db.execute(
            """SELECT i.id, i.sku, i.name, i.unit, i.unit_cost,
                      c.name AS category, NULL AS canonical_sku
               FROM items i
               LEFT JOIN categories c ON c.id = i.category_id"""
        ).fetchall()
    return {
        int(r["id"]): {
            "sku": r["sku"], "name": r["name"], "unit": r["unit"] or "",
            "unit_cost": float(r["unit_cost"] or 0.0),
            "category": r["category"], "canonical_sku": r["canonical_sku"],
        }
        for r in rows
    }


def _load_bom(db) -> dict[int, set[int]]:
    out: dict[int, set[int]] = {}
    for r in db.execute("SELECT product_id, item_id FROM product_bom").fetchall():
        out.setdefault(int(r["product_id"]), set()).add(int(r["item_id"]))
    return out


def _load_usage(db) -> dict[int, int]:
    """item_id → 被多少个产品引用（P-MULTI-PRODUCT 证据）。"""
    return {
        int(r["item_id"]): int(r["n"])
        for r in db.execute(
            "SELECT item_id, COUNT(DISTINCT product_id) AS n "
            "FROM product_bom GROUP BY item_id").fetchall()
    }


def _load_run_items(db, since_s: str) -> list[dict]:
    rows = db.execute(
        """SELECT pri.id, pri.run_id, pri.item_id, pri.planned_qty,
                  pri.actual_qty, pri.planned_default, pri.outbound_req_id,
                  pr.product_id, pr.created_at
           FROM production_run_items pri
           JOIN production_runs pr ON pr.id = pri.run_id
           WHERE pr.rolled_back = 0 AND pr.created_at >= ?
           ORDER BY pr.created_at ASC, pri.id ASC""",
        (since_s,),
    ).fetchall()
    return [dict(r) for r in rows]


def _load_stocktakes(db, since_s: str) -> list[dict]:
    """窗口内已审核、未回滚的盘点差异行。审核前看到的是半成品指标。"""
    rows = db.execute(
        """SELECT s.id, s.item_id, s.previous_quantity, s.actual_quantity,
                  s.diff, s.batch_id, s.created_at,
                  COALESCE(b.status, 'approved') AS batch_status,
                  COALESCE(b.rolled_back, 0) AS batch_rolled_back
           FROM stocktakes s
           LEFT JOIN stocktake_batches b ON b.id = s.batch_id
           WHERE s.created_at >= ?
           ORDER BY s.item_id ASC, s.created_at ASC, s.id ASC""",
        (since_s,),
    ).fetchall()
    return [
        dict(r) for r in rows
        if int(r["batch_rolled_back"] or 0) == 0
        and (r["batch_status"] or "approved") == "approved"
    ]


def _load_blank_outbound(db, since_s: str) -> list[dict]:
    """窗口内未回滚、未填原因的**手工**出库（L2 旁路消耗）。

    生产领料/盘点盘亏即使 reason 为空也不计入：那两类有独立单据，
    空原因不构成"去向不明"。
    """
    rows = db.execute(
        """SELECT o.id, o.item_id, o.requested_quantity, o.reason, o.created_at
           FROM outbound_requests o
           WHERE o.rolled_back = 0 AND o.created_at >= ?
             AND (o.reason IS NULL OR TRIM(o.reason) = '')
             AND COALESCE(o.source_type, 'manual') = 'manual'
           ORDER BY o.created_at ASC, o.id ASC""",
        (since_s,),
    ).fetchall()
    return [dict(r) for r in rows]


def _load_flow_times(db, since_s: str) -> dict[int, list[datetime]]:
    """item_id → 真实出入库流水时间（冲销对判定的第 5 条依据）。

    排除 `note LIKE '盘点%'` 的流水：盘盈写 '库存调整'、盘亏写 '出库'
    （note='盘点审核#N盘亏'），两者都是盘点自身的副产物。不排除的话，
    每对盘盈盘亏之间都会"存在出入库流水"，冲销对永远配不上。
    """
    marks = ",".join("?" * len(_FLOW_ACTIONS))
    out: dict[int, list[datetime]] = {}
    for r in db.execute(
        f"""SELECT item_id, created_at FROM stock_movements
            WHERE action IN ({marks}) AND created_at >= ?
              AND (note IS NULL OR note NOT LIKE '盘点%')
            ORDER BY created_at ASC""",
        (*_FLOW_ACTIONS, since_s),
    ).fetchall():
        try:
            dt = parse_event_ts(r["created_at"])
        except ValueError as exc:
            logger.warning("流水时间戳跳过（无法解析）: %s", exc)
            continue
        out.setdefault(int(r["item_id"]), []).append(dt)
    return out


def _load_exposure(db, since_s: str) -> dict[int, dict]:
    """概率口径的分母：item_id → {exposure, inspections, losses, ob_qty, ob_total, ob_blank}。

    exposure = 出库次数 + 生产投料次数 + 盘点次数。
    **盘点必须计入**：L3 盘亏事件本身就是盘点产生的，分母漏掉盘点会让
    误差率算成 120% 这种超过 100% 的荒谬值（实测 PP056 踩过）。

    inspections = 盘点次数（loss_rate 的检验机会）
    losses      = 盘亏次数（loss_rate 的检验失败）
    """
    out: dict[int, dict] = {}

    def slot(item_id: int) -> dict:
        return out.setdefault(int(item_id), {
            "exposure": 0, "inspections": 0, "losses": 0,
            "ob_qty": 0.0, "ob_total": 0, "ob_blank": 0,
        })

    for r in db.execute(
        """SELECT item_id, COUNT(*) AS n, SUM(requested_quantity) AS q,
                  SUM(CASE WHEN reason IS NULL OR TRIM(reason) = ''
                           THEN 1 ELSE 0 END) AS b
           FROM outbound_requests WHERE rolled_back = 0 AND created_at >= ?
           GROUP BY item_id""", (since_s,),
    ).fetchall():
        s = slot(r["item_id"])
        s["exposure"] += int(r["n"] or 0)
        s["ob_qty"] = float(r["q"] or 0.0)
        s["ob_total"] = int(r["n"] or 0)
        s["ob_blank"] = int(r["b"] or 0)

    for r in db.execute(
        """SELECT pri.item_id, COUNT(*) AS n
           FROM production_run_items pri
           JOIN production_runs pr ON pr.id = pri.run_id
           WHERE pr.rolled_back = 0 AND pr.created_at >= ?
           GROUP BY pri.item_id""", (since_s,),
    ).fetchall():
        slot(r["item_id"])["exposure"] += int(r["n"] or 0)

    for r in db.execute(
        """SELECT item_id, COUNT(*) AS n,
                  SUM(CASE WHEN diff < 0 THEN 1 ELSE 0 END) AS loss_n
           FROM stocktakes WHERE created_at >= ? GROUP BY item_id""", (since_s,),
    ).fetchall():
        s = slot(r["item_id"])
        s["inspections"] = int(r["n"] or 0)
        s["losses"] = int(r["loss_n"] or 0)
        s["exposure"] += s["inspections"]
    return out


# ---------------------------------------------------------------------------
# 冲销对识别（L3 最关键的一步）
# ---------------------------------------------------------------------------

def _has_flow_between(flow: list[datetime], a: datetime, b: datetime) -> bool:
    """(a, b) 开区间内是否存在真实出入库流水。"""
    lo, hi = (a, b) if a <= b else (b, a)
    return any(lo < t < hi for t in flow)


def find_reversal_pairs(rows: list[dict], flow: list[datetime],
                        pair_hours: float = REVERSAL_PAIR_HOURS,
                        tol: float = REVERSAL_PAIR_TOL) -> dict[int, int]:
    """识别「先盘错、隔批改回」的冲销对，返回 {stocktake_id: paired_id}。

    判据（§12.1，全部满足）：
      1. 同一品项（调用方已按 item 分组传入）
      2. 两条 diff 符号相反
      3. 量级相对差 < tol
      4. 时间间隔 <= pair_hours
      5. 两条之间该品项无真实出入库流水 —— 防"先盘盈 10、后真用掉 10"误判

    贪心匹配，一条只能配一次；同一条负差有多个候选时取时间最近者。
    """
    parsed = []
    for row in rows:
        diff = float(row["diff"] or 0)
        if abs(diff) <= BOM_ZERO_EPS:
            continue
        try:
            dt = parse_event_ts(row["created_at"])
        except ValueError as exc:
            logger.warning("盘点时间戳跳过（无法解析）: %s", exc)
            continue
        parsed.append((dt, int(row["id"]), diff))

    pairs: dict[int, int] = {}
    used: set[int] = set()
    window = timedelta(hours=pair_hours)

    for dt, sid, diff in parsed:
        if diff > 0 or sid in used:
            continue
        best: tuple[float, int] | None = None
        for dt2, sid2, diff2 in parsed:
            if diff2 <= 0 or sid2 in used or sid2 == sid:
                continue
            if abs(dt2 - dt) > window:
                continue
            mag = max(abs(diff), abs(diff2))
            if mag <= 0 or abs(abs(diff) - abs(diff2)) / mag > tol:
                continue
            if _has_flow_between(flow, dt, dt2):
                continue
            gap = abs((dt2 - dt).total_seconds())
            if best is None or gap < best[0]:
                best = (gap, sid2)
        if best is not None:
            pairs[sid] = best[1]
            pairs[best[1]] = sid
            used.update({sid, best[1]})
    return pairs


# ---------------------------------------------------------------------------
# 台账行构造
# ---------------------------------------------------------------------------

def _fmt_qty(v: float) -> str:
    s = f"{float(v):.4f}".rstrip("0").rstrip(".")
    return s or "0"


def _ledger_row(item: dict, *, item_id: int, layer: str, source_table: str,
                source_id: int, event_at: str, err_qty: float, delta_sign: int,
                cause_code: str, evidence: dict, reason: str,
                run_id: int | None = None, batch_id: int | None = None,
                self_reversed: bool = False, secondary: str | None = None,
                conf: float | None = None) -> dict:
    """组装一条台账行。

    `value_state` 只由成本决定（unknown = 该品项无 unit_cost，金额不可核算），
    与成因是否判定得出无关 —— 未判定的误差同样可能有明确金额。
    """
    cat, _label, base_conf = _cause(cause_code)
    value = float(err_qty) * float(item["unit_cost"])
    return {
        "item_id": item_id,                       # 仅内部聚合用，不落库
        "item_sku": item["sku"],
        "canonical_sku": item["canonical_sku"],
        "item_name": item["name"],
        "category_name": item["category"],
        "unit": item["unit"],
        "unit_cost": item["unit_cost"],
        "layer": layer,
        "source_table": source_table,
        "source_id": int(source_id),
        "run_id": run_id,
        "batch_id": batch_id,
        "event_at": event_at,
        "err_qty": round(float(err_qty), 6),
        "err_value": round(value, 2),
        "delta_sign": delta_sign,
        "cause_primary": cat,
        "cause_secondary": secondary,
        "cause_code": cause_code,
        "confidence": round(min(0.95, base_conf if conf is None else conf), 2),
        "evidence_json": json.dumps(evidence, ensure_ascii=False, default=str),
        "reason_text": reason,
        "self_reversed": 1 if self_reversed else 0,
        "value_state": "deployed" if item["unit_cost"] > 0 else "unknown",
    }


# ---------------------------------------------------------------------------
# L1 配方偏差
# ---------------------------------------------------------------------------

def _l1_zero_plan_rows(run_items: list[dict], items: dict[int, dict],
                       bom: dict[int, set[int]], usage: dict[int, int]) -> list[dict]:
    """计划量 = 0 但实际投料 > 0 → 配方系数没配（R-BOM-ZERO）。

    这批行**无论 planned_default 取值都成立**：兜底逻辑是 actual = planned，
    计划量 0 只会兜出 0，不可能出现 actual > 0，所以 actual 必然是人填的。
    """
    rows = []
    for ri in run_items:
        item = items.get(int(ri["item_id"]))
        if item is None:
            continue
        planned = float(ri["planned_qty"] or 0.0)
        actual = float(ri["actual_qty"] or 0.0)
        if planned > BOM_ZERO_EPS or actual <= BOM_ZERO_EPS:
            continue

        in_bom = int(ri["item_id"]) in bom.get(int(ri["product_id"]), set())
        signals = ["R-BOM-ZERO"] + ([] if in_bom else ["R-BOM-MISSING"])
        conf = 0.90 + (0.05 if not in_bom else 0.0)
        secondary = "P-ZERO-COST" if item["unit_cost"] <= 0 else None
        ev = {
            "signals": signals,
            "bom_qty_per_unit": 0,
            "planned_qty": 0,
            "actual_qty": round(actual, 6),
            "in_bom": in_bom,
            "used_in_products": usage.get(int(ri["item_id"]), 0),
            "planned_default": int(ri.get("planned_default") or 0),
            "run_id": int(ri["run_id"]),
        }
        bom_desc = ("存在但 qty_per_unit=0" if in_bom
                    else "缺失（配方漏行）")
        reason = (
            f"{item['name']}（{item['sku']}）在 {ri['created_at'][:10]} 的生产批次"
            f"#{ri['run_id']} 中计划量为 0、实际投料 {_fmt_qty(actual)}{item['unit']}。"
            f"判定为「配方系数为 0」（置信度 {min(0.95, conf):.2f}）。"
            f"依据：该产品 BOM 中此品项{bom_desc}；实际投料为非零值，"
            f"不存在「未填实际用量则沿用计划量」的可能（那种情况只会得到 0）。"
            f"建议动作：修正 product_bom.qty_per_unit 后重算历史 L1。"
        )
        if secondary:
            reason += "该品项 unit_cost=0，金额不可核算，仅计量。"
        rows.append(_ledger_row(
            item, item_id=int(ri["item_id"]), layer="L1",
            source_table="production_run_items", source_id=int(ri["id"]),
            event_at=ri["created_at"], err_qty=actual, delta_sign=-1,
            cause_code="R-BOM-ZERO", evidence=ev, reason=reason,
            run_id=int(ri["run_id"]), secondary=secondary, conf=conf))
    return rows


def _l1_systematic_rows(run_items: list[dict], items: dict[int, dict]) -> list[dict]:
    """同品项在同产品上重复同向偏差 → 配方系数偏大/偏小（S 级证据）。

    只对 `planned_default=0`（真实填报）的行生效。判据：
      - 样本 >= SYSTEMATIC_MIN_SAMPLES
      - 同向占比 >= SYSTEMATIC_DIRECTION
      - 变异系数 CV < SYSTEMATIC_CV_MAX（偏差率稳定 → 像系数错，不像手抖）
    """
    series: dict[tuple[int, int], list[dict]] = {}
    for ri in run_items:
        if int(ri.get("planned_default") or 0) != 0:
            continue
        planned = float(ri["planned_qty"] or 0.0)
        if planned <= BOM_ZERO_EPS:
            continue
        key = (int(ri["item_id"]), int(ri["product_id"]))
        series.setdefault(key, []).append(ri)

    rows = []
    for (item_id, _product_id), samples in series.items():
        if len(samples) < SYSTEMATIC_MIN_SAMPLES:
            continue
        item = items.get(item_id)
        if item is None:
            continue
        rates = [
            (float(s["actual_qty"]) - float(s["planned_qty"])) / float(s["planned_qty"])
            for s in samples
        ]
        pos = sum(1 for r in rates if r > 0)
        neg = sum(1 for r in rates if r < 0)
        direction = max(pos, neg) / len(rates)
        mean = sum(rates) / len(rates)
        if abs(mean) <= BOM_ZERO_EPS:
            continue
        var = sum((r - mean) ** 2 for r in rates) / len(rates)
        cv = (var ** 0.5) / abs(mean)
        if direction < SYSTEMATIC_DIRECTION or cv >= SYSTEMATIC_CV_MAX:
            continue

        last = samples[-1]
        total_planned = sum(float(s["planned_qty"]) for s in samples)
        total_actual = sum(float(s["actual_qty"]) for s in samples)
        err_qty = abs(total_actual - total_planned)
        if err_qty <= BOM_ZERO_EPS:
            continue
        conf = 0.80 + (0.05 if cv < 0.05 and direction >= 0.95 else 0.0)
        ev = {
            "signals": ["R-SYSTEMATIC"],
            "samples": len(rates),
            "direction_share": round(direction, 3),
            "mean_rate_pct": round(mean * 100, 2),
            "cv": round(cv, 4),
            "total_planned": round(total_planned, 4),
            "total_actual": round(total_actual, 4),
        }
        reason = (
            f"{item['name']}（{item['sku']}）在 {len(rates)} 个真实填报批次上"
            f"持续{'超' if mean > 0 else '欠'}耗，平均偏差 {mean * 100:.1f}%，"
            f"同向占比 {direction * 100:.0f}%、变异系数 {cv:.3f}。"
            f"判定为「系统性同向偏差」（置信度 {conf:.2f}）。"
            f"依据：偏差方向一致且波动极小，符合配方系数配置偏差的特征；"
            f"若为随机手误，偏差应正负交替、离散度显著更大。"
            f"建议动作：复核该产品 BOM 中此品项的 qty_per_unit。"
        )
        rows.append(_ledger_row(
            item, item_id=item_id, layer="L1",
            source_table="production_run_items", source_id=int(last["id"]),
            event_at=last["created_at"], err_qty=err_qty,
            delta_sign=1 if mean > 0 else -1, cause_code="R-SYSTEMATIC",
            evidence=ev, reason=reason, run_id=int(last["run_id"]), conf=conf))
    return rows


def _build_l1_rows(run_items, items, bom, usage) -> list[dict]:
    """L1 全部行。默认兜底行（planned_default=1 且 planned>0）一律跳过 ——
    它们偏差恒为 0，进分母只会稀释指标。"""
    return (_l1_zero_plan_rows(run_items, items, bom, usage)
            + _l1_systematic_rows(run_items, items))


# ---------------------------------------------------------------------------
# L2 旁路消耗
# ---------------------------------------------------------------------------

def _build_l2_rows(blank_rows: list[dict], items: dict[int, dict],
                   exposure: dict[int, dict]) -> list[dict]:
    """出库未填原因。记录完整性问题，不是物料损失，不进损耗率分子。"""
    rows = []
    for r in blank_rows:
        item = items.get(int(r["item_id"]))
        if item is None:
            continue
        qty = float(r["requested_quantity"] or 0)
        if qty <= BOM_ZERO_EPS:
            continue
        exp = exposure.get(int(r["item_id"]), {})
        total = exp.get("ob_total") or 0
        ratio = (exp.get("ob_blank") or 0) / total if total else 0.0
        ev = {"signals": ["H-REASON-BLANK"], "blank_ratio": round(ratio, 3),
              "outbound_id": int(r["id"]), "reason_raw": r["reason"]}
        reason = (
            f"{item['name']}（{item['sku']}）于 {r['created_at'][:10]} 出库 "
            f"{_fmt_qty(qty)}{item['unit']}，未填写原因。"
            f"判定为「出库未填原因」（置信度 0.60）。"
            f"依据：该品项窗口内无原因出库 {exp.get('ob_blank') or 0}/{total} 次"
            f"（{ratio * 100:.0f}%），这批料是生产领用、报废还是调拨无法判断。"
            f"注意：这是记录完整性问题（管理信号），不是物料损失，不计入损耗率。"
            f"建议动作：出库原因设为必填。"
        )
        rows.append(_ledger_row(
            item, item_id=int(r["item_id"]), layer="L2",
            source_table="outbound_requests", source_id=int(r["id"]),
            event_at=r["created_at"], err_qty=qty, delta_sign=-1,
            cause_code="H-REASON-BLANK", evidence=ev, reason=reason))
    return rows


# ---------------------------------------------------------------------------
# L3 账实损耗
# ---------------------------------------------------------------------------

def _build_l3_rows(stocktakes: list[dict], items: dict[int, dict],
                   flow_by_item: dict[int, list[datetime]],
                   exposure: dict[int, dict], usage: dict[int, int]) -> list[dict]:
    """盘亏行逐条定性，优先级：冲销对 > 无渠道 > 单条异常 > 多产品共用 > 待判定。"""
    by_item: dict[int, list[dict]] = {}
    for r in stocktakes:
        by_item.setdefault(int(r["item_id"]), []).append(r)

    rows: list[dict] = []
    for item_id, item_rows in by_item.items():
        item = items.get(item_id)
        if item is None:
            continue
        pairs = find_reversal_pairs(item_rows, flow_by_item.get(item_id, []))
        losses = [r for r in item_rows if float(r["diff"] or 0) < -BOM_ZERO_EPS]
        total_loss_qty = sum(-float(r["diff"]) for r in losses)
        exp = exposure.get(item_id, {})
        ob_qty = float(exp.get("ob_qty") or 0.0)
        used_in_products = usage.get(item_id, 0)

        for r in losses:
            qty = -float(r["diff"])
            sid = int(r["id"])
            base = {
                "previous_qty": round(float(r["previous_quantity"] or 0), 4),
                "actual_qty": round(float(r["actual_quantity"] or 0), 4),
                "batch_id": r["batch_id"],
                "used_in_products": used_in_products,
                "total_loss_qty": round(total_loss_qty, 4),
            }

            paired = pairs.get(sid)
            if paired is not None:
                ev = dict(base, signals=["H-REVERSAL-PAIR"],
                          paired_stocktake_id=paired,
                          net_effect="配对后净影响接近 0，不计入损耗率")
                reason = (
                    f"{item['name']}（{item['sku']}）在盘点批次#{r['batch_id']} 盘亏 "
                    f"{_fmt_qty(qty)}{item['unit']}，与相邻盘盈构成冲销对。"
                    f"判定为「盘盈盘亏冲销对」（置信度 0.90）。"
                    f"依据：同一品项 {REVERSAL_PAIR_HOURS} 小时内出现量级相差 "
                    f"<{REVERSAL_PAIR_TOL * 100:.0f}% 的反向盘盈，且两次盘点之间没有"
                    f"任何真实出入库流水 —— 属录入错误后隔批更正，不是真实损耗。"
                    f"建议动作：盘点录入增加量级校验（偏离账面超过阈值即阻断）。"
                    f"本行保留在台账用于追踪录入质量，但不计入损耗率。"
                )
                rows.append(_ledger_row(
                    item, item_id=item_id, layer="L3",
                    source_table="stocktakes", source_id=sid,
                    event_at=r["created_at"], err_qty=qty, delta_sign=-1,
                    cause_code="H-REVERSAL-PAIR", evidence=ev, reason=reason,
                    batch_id=r["batch_id"], self_reversed=True))
                continue

            if used_in_products == 0:
                ev = dict(base, signals=["P-NO-CHANNEL"])
                reason = (
                    f"{item['name']}（{item['sku']}）在盘点批次#{r['batch_id']} 盘亏 "
                    f"{_fmt_qty(qty)}{item['unit']}。判定为「无消耗登记渠道」"
                    f"（置信度 0.75）。依据：该品项不属于任何产品配方，"
                    f"窗口内累计盘亏 {_fmt_qty(total_loss_qty)}{item['unit']} —— "
                    f"消耗只能靠盘点倒推，没有日常出库/领用登记路径。"
                    f"建议动作：为其建立出库或领用登记；若确属非生产物料，"
                    f"建议从损耗考核范围剔除。"
                )
                rows.append(_ledger_row(
                    item, item_id=item_id, layer="L3",
                    source_table="stocktakes", source_id=sid,
                    event_at=r["created_at"], err_qty=qty, delta_sign=-1,
                    cause_code="P-NO-CHANNEL", evidence=ev, reason=reason,
                    batch_id=r["batch_id"]))
                continue

            if ob_qty > BOM_ZERO_EPS and qty > ob_qty * SPIKE_MULTIPLE:
                ev = dict(base, signals=["H-SPIKE-ENTRY"],
                          window_outbound_qty=round(ob_qty, 4),
                          multiple=round(qty / ob_qty, 2))
                reason = (
                    f"{item['name']}（{item['sku']}）在盘点批次#{r['batch_id']} 盘亏 "
                    f"{_fmt_qty(qty)}{item['unit']}，是该品项窗口出库量"
                    f"（{_fmt_qty(ob_qty)}{item['unit']}）的 {qty / ob_qty:.1f} 倍。"
                    f"判定为「单条数量异常」（置信度 0.85）。"
                    f"依据：单条盘亏量级与正常消耗严重脱节，超过 "
                    f"{SPIKE_MULTIPLE:.0f} 倍阈值，且未找到可配对的冲销盘盈。"
                    f"建议动作：人工核对盘点原始单据，确认数量或单位是否录错。"
                )
                rows.append(_ledger_row(
                    item, item_id=item_id, layer="L3",
                    source_table="stocktakes", source_id=sid,
                    event_at=r["created_at"], err_qty=qty, delta_sign=-1,
                    cause_code="H-SPIKE-ENTRY", evidence=ev, reason=reason,
                    batch_id=r["batch_id"]))
                continue

            if used_in_products >= MULTI_PRODUCT_MIN:
                ev = dict(base, signals=["P-MULTI-PRODUCT"])
                reason = (
                    f"{item['name']}（{item['sku']}）在盘点批次#{r['batch_id']} 盘亏 "
                    f"{_fmt_qty(qty)}{item['unit']}，该品项被 {used_in_products} 个"
                    f"产品共用，无法按产品归集。判定为「多产品共用无法归集」"
                    f"（置信度 0.65，待复核）。"
                    f"依据：共用产品数 ≥{MULTI_PRODUCT_MIN}，盘亏定位不到具体配方；"
                    f"该证据只能说明「值得查」，不能直接定罪。"
                    f"建议动作：抽查领用记录与配方用量，或对共用产品做分批试产对账。"
                )
                rows.append(_ledger_row(
                    item, item_id=item_id, layer="L3",
                    source_table="stocktakes", source_id=sid,
                    event_at=r["created_at"], err_qty=qty, delta_sign=-1,
                    cause_code="P-MULTI-PRODUCT", evidence=ev, reason=reason,
                    batch_id=r["batch_id"]))
                continue

            ev = dict(base, signals=[])
            reason = (
                f"{item['name']}（{item['sku']}）在盘点批次#{r['batch_id']} 盘亏 "
                f"{_fmt_qty(qty)}{item['unit']}。未命中任何归因规则，"
                f"判定为「待人工判定」（置信度 0.00）。"
                f"依据：品项属正常配方原料、盘亏量级在正常范围内、无冲销对、"
                f"无异常倍数 —— 现有数据不足以区分操作损耗与配方偏差。"
                f"建议动作：结合领用记录人工判断，或在下个盘点周期加密观察。"
            )
            rows.append(_ledger_row(
                item, item_id=item_id, layer="L3",
                source_table="stocktakes", source_id=sid,
                event_at=r["created_at"], err_qty=qty, delta_sign=-1,
                cause_code="unattributed", evidence=ev, reason=reason,
                batch_id=r["batch_id"]))
    return rows


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def compute_item_loss_ledger(db, now: datetime,
                             lookback_days: int = LOSS_LOOKBACK_DAYS) -> list[dict]:
    """窗口内的品项级误差台账（不落库）。"""
    since_s = (now - timedelta(days=lookback_days)).strftime("%Y-%m-%d %H:%M:%S")
    items = _load_items(db)
    if not items:
        return []

    bom = _load_bom(db)
    usage = _load_usage(db)
    exposure = _load_exposure(db, since_s)
    run_items = _load_run_items(db, since_s)
    stocktakes = _load_stocktakes(db, since_s)
    blank_rows = _load_blank_outbound(db, since_s)
    flow_by_item = _load_flow_times(db, since_s)

    return (_build_l1_rows(run_items, items, bom, usage)
            + _build_l2_rows(blank_rows, items, exposure)
            + _build_l3_rows(stocktakes, items, flow_by_item, exposure, usage))


_LEDGER_INSERT = """
INSERT OR IGNORE INTO item_loss_ledger
   (warehouse_code, item_sku, canonical_sku, item_name, category_name,
    unit, unit_cost, layer, source_table, source_id, run_id, batch_id,
    event_at, err_qty, err_value, delta_sign, cause_primary,
    cause_secondary, cause_code, confidence, evidence_json,
    reason_text, self_reversed, value_state, detected_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def rebuild_item_loss_ledger(db, warehouse_code: str, now: datetime,
                             lookback_days: int = LOSS_LOOKBACK_DAYS) -> dict:
    """重算并落库。幂等：窗口内未处置的行先删后插，已处置（resolved=1）保留。"""
    since_s = (now - timedelta(days=lookback_days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = compute_item_loss_ledger(db, now, lookback_days)

    cur = db.execute(
        "DELETE FROM item_loss_ledger "
        "WHERE warehouse_code = ? AND event_at >= ? AND resolved = 0",
        (warehouse_code, since_s))
    deleted = max(cur.rowcount or 0, 0)

    detected = now.strftime("%Y-%m-%d %H:%M:%S")
    inserted = 0
    for r in rows:
        cur = db.execute(_LEDGER_INSERT, (
            warehouse_code, r["item_sku"], r["canonical_sku"], r["item_name"],
            r["category_name"], r["unit"], r["unit_cost"], r["layer"],
            r["source_table"], r["source_id"], r["run_id"], r["batch_id"],
            r["event_at"], r["err_qty"], r["err_value"], r["delta_sign"],
            r["cause_primary"], r["cause_secondary"], r["cause_code"],
            r["confidence"], r["evidence_json"], r["reason_text"],
            r["self_reversed"], r["value_state"], detected))
        inserted += max(cur.rowcount or 0, 0)
    db.commit()
    return {"inserted": inserted, "deleted": deleted, "computed": len(rows)}


# ---------------------------------------------------------------------------
# 汇总（页面直接用）
# ---------------------------------------------------------------------------

def summarize(rows: list[dict], exposure: dict[int, dict] | None = None,
              top_n: int = LOSS_TOP_N) -> dict:
    """台账行 → 页面上下文。

    `exposure` 提供概率分母；缺失时概率字段为 None（页面显示「—」，不显示 0）。
    """
    if not rows:
        return dict(_EMPTY)

    live = [r for r in rows if not r["self_reversed"]]
    healed = [r for r in rows if r["self_reversed"]]

    by_cause: dict[str, dict] = {}
    by_layer: dict[str, dict] = {}
    for r in live:
        cat = r["cause_primary"]
        slot = by_cause.setdefault(cat, {"count": 0, "value": 0.0, "unknown": 0})
        slot["count"] += 1
        if r["value_state"] == "unknown":
            slot["unknown"] += 1
        else:
            slot["value"] += float(r["err_value"])
        layer = by_layer.setdefault(r["layer"], {"count": 0, "value": 0.0})
        layer["count"] += 1
        layer["value"] += float(r["err_value"])

    per_item: dict[str, dict] = {}
    for r in live:
        slot = per_item.setdefault(r["item_sku"], {
            "sku": r["item_sku"], "name": r["item_name"],
            "unit": r["unit"], "unit_cost": r["unit_cost"],
            "category": r["category_name"], "causes": {},
            "events": 0, "value": 0.0, "unknown_events": 0,
            "last_event": r["event_at"], "_item_id": r["item_id"],
        })
        slot["events"] += 1
        if r["value_state"] == "unknown":
            slot["unknown_events"] += 1
        else:
            slot["value"] += float(r["err_value"])
        slot["causes"][r["cause_primary"]] = (
            slot["causes"].get(r["cause_primary"], 0) + 1)
        if r["event_at"] > slot["last_event"]:
            slot["last_event"] = r["event_at"]

    for slot in per_item.values():
        exp = (exposure or {}).get(slot["_item_id"], {})
        exposure_n = exp.get("exposure") or 0
        inspections = exp.get("inspections") or 0
        losses = exp.get("losses") or 0
        slot["exposure"] = exposure_n
        slot["inspections"] = inspections
        slot["losses"] = losses
        # 误差率 = 台账事件数 / 操作暴露次数；分母为 0 → None（显示「—」）
        slot["err_rate"] = (round(slot["events"] / exposure_n * 100, 1)
                            if exposure_n else None)
        slot["loss_rate"] = (round(losses / inspections * 100, 1)
                             if inspections else None)
        total = sum(slot["causes"].values()) or 1
        slot["dominant_cause"] = max(slot["causes"], key=slot["causes"].get)
        slot["dominant_share"] = round(
            slot["causes"][slot["dominant_cause"]] / total * 100)
        slot["dominant_label"] = CAUSE_LABELS.get(slot["dominant_cause"], "—")
        slot.pop("_item_id", None)

    item_rates = sorted(per_item.values(), key=lambda x: -x["value"])
    live_sorted = sorted(live, key=lambda r: -float(r["err_value"]))
    blind = [r for r in live if r["value_state"] == "unknown"]

    return {
        "totals": {
            "count": len(live),
            # 金额只统计可计价的行；无法计价的条数单列，绝不当 0 元混进去
            "value": round(sum(float(r["err_value"]) for r in live
                               if r["value_state"] != "unknown"), 2),
            "unknown_count": len(blind),
            "self_reversed_count": len(healed),
            "self_reversed_value": round(
                sum(float(r["err_value"]) for r in healed), 2),
        },
        "by_cause": by_cause,
        "by_layer": by_layer,
        "top_rows": live_sorted[:top_n],
        "self_reversed": sorted(healed, key=lambda r: -float(r["err_value"]))[:top_n],
        "item_rates": item_rates,
        "state": "deployed",
        "blind_spots": sorted({r["item_sku"] for r in blind}),
    }


def build_ledger_context(db, warehouse_code: str, now: datetime,
                         lookback_days: int = LOSS_LOOKBACK_DAYS) -> dict:
    """页面入口：计算 + 落库 + 汇总。

    归因失败**不抛**：报表不允许因一个分析模块挂掉整页 500（health 页是
    manager 每天要看的）。降级为 state='unknown'，页面显示「—」而非 0。
    """
    try:
        since_s = (now - timedelta(days=lookback_days)).strftime(
            "%Y-%m-%d %H:%M:%S")
        rows = compute_item_loss_ledger(db, now, lookback_days)
        stats = rebuild_item_loss_ledger(db, warehouse_code, now, lookback_days)
        exposure = _load_exposure(db, since_s)
    except Exception as exc:  # noqa: BLE001
        logger.warning("品项误差台账失败，降级为 unknown: %s", exc, exc_info=True)
        ctx = dict(_EMPTY)
        ctx["state"] = "unknown"
        ctx["stats"] = {"error": str(exc)}
        ctx["labels"] = CAUSE_LABELS
        ctx["lookback_days"] = lookback_days
        return ctx

    ctx = summarize(rows, exposure)
    ctx["stats"] = stats
    ctx["labels"] = CAUSE_LABELS
    ctx["lookback_days"] = lookback_days
    return ctx
