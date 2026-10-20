"""Pure supply-chain model behind the replenishment dashboard (/forecast).

设计原则
--------
1. **纯函数**：不 import flask / sqlite / 任何 I/O，便于单测与复用（沿用
   forecast_pure.py / procurement_pure.py 的既有约定）。
2. **零幻觉参数**：每个常数都写明来历（业务口径 / 行业默认），并通过
   `PolicyParams` 暴露出去，路由层可从配置覆盖。
3. **三态口径**：无消耗记录 ≠ 消耗为 0。所有「算不出来」的指标返回
   `None` 而不是 0，由模板渲染成「—」（口径见 docs/2026-10-20-...md §数据质量）。

模型选型（详见同目录设计文档）
----------------------------
- 需求速率：连续需求用简单指数平滑 SES；间歇需求用 Croston（区分「需求大小」
  与「需求间隔」），避免把零星补货当成日均消耗。
- 安全库存：**周期盘点**口径 `SS = z · σ_d · sqrt(L + R)`。因为门店每周才订一次，
  暴露期是「提前期 + 复盘周期」，不是连续盘点下的 sqrt(L)。
- 目标库存：`S = d̄ · (L + R) + SS`，订货量 `Q = max(0, S - 在手 - 在途)`，
  即 (R, S) 定期定量订货策略（order-up-to）。
- EOQ：仅作**经济批量参考**与积压倾向提示，不直接决定订货量（见 §EOQ 说明）。
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

# ---------------------------------------------------------------------------
# Policy parameters
# ---------------------------------------------------------------------------

# 正态分布分位数（单侧服务水平）。0.95 → 1.645，0.90 → 1.282，0.85 → 1.036。
Z_BY_SERVICE = {0.90: 1.282, 0.95: 1.645, 0.975: 1.960, 0.85: 1.036}


@dataclass(frozen=True)
class PolicyParams:
    """补货策略参数。

    业务口径（Eric 2026-10-20 给出）：
      - 门店每周订货一次 → store_review_days = 7
      - 部分原料到货需要 2-3 天 → dc_lead_days 取 3（保守值，宁可早到）
      - 尽量减少库存积压 → overstock_days 收紧到 45 天，A 类服务水平取 95% 而非 99%
    """

    # --- 需求估计 ---
    window_days: int = 56          # 8 周 = 8 个订货周期，足够覆盖周内节律
    continuous_min_ratio: float = 0.30  # 有需求天数占比 ≥30% 视为连续需求
    # 近 / 远半段加权：权重 2:1。刻意不用 SES —— 断货日的零消耗会把 SES
    # 拉向 0（实测 α=0.3 连续 28 天零后速率衰减到 1e-5），补货系统据此
    # 判定「不用订」，结果是永久断货。加权均值不会崩，但仍偏向近期。
    recent_weight: float = 2.0
    stale_after_days: int = 21     # 超过 21 天无消耗 → 标记 stale（可能已停售）

    # --- 订货周期与提前期（天）---
    store_review_days: int = 7
    store_lead_days: int = 1       # DC → 门店，次日达
    dc_review_days: int = 7
    dc_lead_days: int = 3          # 供应商 → DC，2-3 天，取上界

    # --- 服务水平（按 ABC 分层）---
    service_a: float = 0.95
    service_b: float = 0.90
    service_c: float = 0.85

    # --- ABC 分层阈值（累计消耗金额占比）---
    abc_a_cum: float = 0.80
    abc_b_cum: float = 0.95

    # --- 积压判定 ---
    overstock_days: float = 45.0

    # --- EOQ 参考值参数 ---
    order_cost: float = 80.0       # 每次下单的行政/物流固定成本（¥）
    holding_rate: float = 0.22     # 年持有成本率（资金 + 仓储 + 损耗）

    def lead_days(self, warehouse_type: str) -> int:
        return self.dc_lead_days if warehouse_type == "distribution_center" else self.store_lead_days

    def review_days(self, warehouse_type: str) -> int:
        return self.dc_review_days if warehouse_type == "distribution_center" else self.store_review_days

    def service_for(self, abc: str) -> float:
        return {"A": self.service_a, "B": self.service_b}.get(abc, self.service_c)

    def replace(self, **kwargs) -> PolicyParams:
        return replace(self, **kwargs)


DEFAULT_PARAMS = PolicyParams()


# ---------------------------------------------------------------------------
# Small numeric helpers
# ---------------------------------------------------------------------------


def _q(value: float, places: int = 4) -> float:
    """Round half-up-ish to N places, killing float noise like 0.30000000000000004.

    Uses round() (banker's rounding is irrelevant at 4dp for our magnitudes)
    and normalises -0.0 to 0.0 so the UI never shows "-0.00".
    """
    out = round(float(value), places)
    return 0.0 if out == 0 else out


def _sample_stdev(values: Sequence[float]) -> float:
    """Sample standard deviation (ddof=1). Returns 0.0 for n < 2."""
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return math.sqrt(max(var, 0.0))


# ---------------------------------------------------------------------------
# Demand estimation
# ---------------------------------------------------------------------------

METHOD_CONTINUOUS = "continuous"
METHOD_INTERMITTENT = "intermittent"
METHOD_NONE = "none"

STATUS_OK = "ok"
STATUS_SPARSE = "sparse"          # 有需求但样本稀疏，估计可信度低
STATUS_STALE = "stale"            # 曾经有需求，近期断档
STATUS_NO_SIGNAL = "no_signal"    # 窗口内零消耗记录（≠ 没消耗）


@dataclass(frozen=True)
class DemandEstimate:
    rate: float                 # 日均需求（基础单位 / 天）
    sigma: float                # 日需求标准差
    method: str                 # continuous | intermittent | none
    nonzero_days: int           # 窗口内有消耗的天数
    window_days: int
    days_since_last: int | None
    status: str
    total: float                # 窗口内累计消耗

    @property
    def has_signal(self) -> bool:
        return self.status in (STATUS_OK, STATUS_SPARSE, STATUS_STALE)


def build_daily_series(
    events: Iterable[tuple[str, float]],
    today: date,
    window_days: int,
) -> list[float]:
    """Fold (date_str, qty) events into a length-`window_days` daily vector.

    The vector is oldest → newest, and **includes zero-demand days** — that is
    the whole point: the previous implementation averaged over *events*, which
    silently drops quiet days and inflates the rate for bursty items.

    Events outside the window, unparseable dates and non-positive qty are
    ignored (a negative qty would be a rollback row, handled upstream).
    """
    if window_days <= 0:
        return []
    buckets: dict[str, float] = {}
    for day, qty in events:
        try:
            q = float(qty)
        except (TypeError, ValueError):
            continue
        if q <= 0:
            continue
        key = str(day)[:10]
        buckets[key] = buckets.get(key, 0.0) + q

    series: list[float] = []
    start = today - timedelta(days=window_days - 1)
    for offset in range(window_days):
        key = (start + timedelta(days=offset)).strftime("%Y-%m-%d")
        series.append(_q(buckets.get(key, 0.0), 6))
    return series


def estimate_demand(
    series: Sequence[float],
    params: PolicyParams = DEFAULT_PARAMS,
    today: date | None = None,
) -> DemandEstimate:
    """Estimate daily demand rate + sigma from a zero-inclusive daily series.

    Continuous branch (>= `continuous_min_ratio` of days have demand):
        rate = weighted mean of the two half-windows (recent : prior = 2 : 1)
        sigma = sample stdev of the full daily series (captures zero days)

    Intermittent branch:
        Croston-style decomposition with a compound-Poisson variance:
            rate  = mean(size) / mean(interval)
            sigma = sqrt(lambda * (var(size) + mean(size)^2)),  lambda = 1/mean(interval)
        Rationale: for an item consumed 3 times in 56 days, a plain average
        divides by 56 and predicts near-zero forever; averaging only over the
        3 event days over-predicts by ~19x. Croston splits the two.

    为什么不用 SES：本系统里「零消耗」有相当比例是**断货造成的**
    （stockout-censored demand），不是真没需求。SES 会被这些零拉死 ——
    实测 α=0.3 连续 28 个零日后速率衰减到 1e-5，补货建议随之变成 0，
    于是永久断货。加权均值保留近期倾斜，但不会崩。
    """
    n = len(series)
    if n == 0:
        return DemandEstimate(0.0, 0.0, METHOD_NONE, 0, 0, None, STATUS_NO_SIGNAL, 0.0)

    nonzero = [(i, float(v)) for i, v in enumerate(series) if float(v) > 0]
    total = _q(sum(v for _, v in nonzero), 6)
    if not nonzero:
        return DemandEstimate(0.0, 0.0, METHOD_NONE, 0, n, None, STATUS_NO_SIGNAL, 0.0)

    days_since_last = n - 1 - nonzero[-1][0]
    ratio = len(nonzero) / n

    if ratio >= params.continuous_min_ratio:
        rate = _weighted_mean_rate(series, params.recent_weight)
        sigma = _sample_stdev([float(v) for v in series])
        method = METHOD_CONTINUOUS
        status = STATUS_OK
        # A continuous item that suddenly went quiet for 3+ weeks is trending
        # to zero; flag it rather than quietly keeping the old rate.
        if days_since_last > params.stale_after_days:
            status = STATUS_STALE
    else:
        sizes = [v for _, v in nonzero]
        positions = [i for i, _ in nonzero]
        intervals: list[float] = []
        for prev, cur in zip(positions, positions[1:], strict=False):
            intervals.append(float(cur - prev))
        # First event contributes a (censored) interval from the window start so
        # a single event still yields a sane denominator instead of dividing by
        # zero or by the whole window.
        intervals.insert(0, float(positions[0] + 1))
        mean_interval = sum(intervals) / len(intervals)
        mean_size = sum(sizes) / len(sizes)
        rate = mean_size / mean_interval if mean_interval > 0 else 0.0
        lam = 1.0 / mean_interval if mean_interval > 0 else 0.0
        size_var = _sample_stdev(sizes) ** 2 if len(sizes) >= 2 else 0.0
        sigma = math.sqrt(max(lam * (size_var + mean_size**2), 0.0))
        method = METHOD_INTERMITTENT
        status = STATUS_SPARSE
        if days_since_last > params.stale_after_days:
            status = STATUS_STALE

    return DemandEstimate(
        rate=_q(rate, 4),
        sigma=_q(sigma, 4),
        method=method,
        nonzero_days=len(nonzero),
        window_days=n,
        days_since_last=days_since_last,
        status=status,
        total=total,
    )


def _weighted_mean_rate(series: Sequence[float], recent_weight: float = 2.0) -> float:
    """近半段权重 `recent_weight`、远半段权重 1 的加权均值。

    比 SES 稳健：不会因为一段断货期把速率拉到 0；比纯均值灵敏：
    近 4 周的节律占 2/3 权重。
    """
    n = len(series)
    if n == 0:
        return 0.0
    split = n // 2
    prior = [float(v) for v in series[:split]]
    recent = [float(v) for v in series[split:]]
    mean_prior = sum(prior) / len(prior) if prior else 0.0
    mean_recent = sum(recent) / len(recent) if recent else 0.0
    if not prior:
        return mean_recent
    if not recent:
        return mean_prior
    w = max(float(recent_weight), 0.0)
    return (w * mean_recent + mean_prior) / (w + 1.0)


def aggregate_series(series_list: Sequence[Sequence[float]]) -> list[float]:
    """Element-wise sum of daily series (DRP pull aggregation).

    Summing the *series* (not just the rates) is what lets us measure the
    downstream aggregate's true variability: if two stores sell on the same
    days, their combined sigma is larger than sqrt(a² + b²), and the empirical
    sum captures that correlation for free.
    """
    if not series_list:
        return []
    length = min(len(s) for s in series_list)
    out = [0.0] * length
    for s in series_list:
        for i in range(length):
            out[i] += float(s[i])
    return [_q(v, 6) for v in out]


# ---------------------------------------------------------------------------
# Inventory policy: safety stock / target level / order quantity
# ---------------------------------------------------------------------------


def safety_stock(
    sigma: float,
    lead_days: int,
    review_days: int,
    z: float,
) -> float:
    """Periodic-review safety stock: z · σ_d · sqrt(L + R).

    Why sqrt(L + R) and not sqrt(L): under a weekly review the stock position
    is only corrected once every R days, so demand risk accrues over the whole
    lead-time-plus-review window. Using sqrt(L) would understate it by ~2.8x
    for L=1, R=7 — the classic "we ordered weekly but sized the buffer for
    daily replenishment" error.
    """
    exposure = max(int(lead_days), 0) + max(int(review_days), 0)
    if exposure <= 0 or sigma <= 0 or z <= 0:
        return 0.0
    return _q(z * float(sigma) * math.sqrt(exposure), 2)


def target_level(rate: float, lead_days: int, review_days: int, ss: float) -> float:
    """Order-up-to level S = d̄·(L + R) + SS.

    d̄·(L+R) covers expected demand until the *next* order arrives; SS absorbs
    the forecast error over the same window. Summing L+R (rather than only L)
    is exactly what makes weekly ordering safe without extra cycle stock.
    """
    exposure = max(int(lead_days), 0) + max(int(review_days), 0)
    if rate <= 0:
        return _q(ss, 2)
    return _q(float(rate) * exposure + float(ss), 2)


def net_requirement(target: float, on_hand: float, on_order: float = 0.0) -> float:
    """Unrounded shortage against the order-up-to level: max(0, S − on_hand − on_order)."""
    raw = float(target) - float(on_hand) - float(on_order)
    return _q(raw, 4) if raw > 0 else 0.0


def order_quantity(
    target: float,
    on_hand: float,
    on_order: float = 0.0,
    pack_size: float = 1.0,
    min_order: float = 0.0,
) -> float:
    """Net requirement, rounded up to the pack multiple.

    Q = max(0, S − on_hand − on_order), then rounded UP to `pack_size` so the
    number is orderable. `on_order` must include everything already committed
    (open store orders + in-transit restock requests) — forgetting it is the
    single most common cause of double-ordering.
    """
    raw = net_requirement(target, on_hand, on_order)
    if raw <= 0:
        return 0.0
    pack = float(pack_size) if pack_size and pack_size > 0 else 1.0
    qty = math.ceil(raw / pack - 1e-9) * pack
    if qty < min_order:
        qty = float(min_order)
    return _q(qty, 4)


def days_of_cover(on_hand: float, rate: float) -> float | None:
    """How many days current stock lasts at the estimated daily rate.

    Returns None (rendered as 「—」) when there is no demand signal — a 0-day
    cover would wrongly read as "stocked out".
    """
    if rate is None or rate <= 0:
        return None
    return _q(max(float(on_hand), 0.0) / float(rate), 1)


# ---------------------------------------------------------------------------
# Status classification
# ---------------------------------------------------------------------------

STATUS_CRITICAL = "critical"    # 覆盖 < 提前期 → 到货前就会断
STATUS_DUE = "due"              # 覆盖 < 提前期 + 复盘周期 → 本周必须订
STATUS_HEALTHY = "healthy"
STATUS_OVERSTOCK = "overstock"
STATUS_DEAD = "dead"            # 无消耗记录且无库存


def classify_stock_status(
    on_hand: float,
    on_order: float,
    rate: float,
    lead_days: int,
    review_days: int,
    sig_status: str,
    overstock_days: float = 45.0,
) -> str:
    """Map a stock position to one of six mutually exclusive states.

    `sig_status` is the demand-estimate status; when there is no demand signal
    we must not pretend the item is healthy — but we also must not claim it
    needs ordering. Hence STATUS_DEAD / 'no_signal' handled by the caller.
    """
    if sig_status == STATUS_NO_SIGNAL:
        return STATUS_DEAD if float(on_hand) <= 0 else "no_signal"
    if rate <= 0:
        return "no_signal"

    cover = (float(on_hand) + float(on_order)) / float(rate)
    if cover < float(lead_days):
        return STATUS_CRITICAL
    if cover < float(lead_days) + float(review_days):
        return STATUS_DUE
    if cover > float(overstock_days):
        return STATUS_OVERSTOCK
    return STATUS_HEALTHY


# ---------------------------------------------------------------------------
# ABC classification (value-based)
# ---------------------------------------------------------------------------


def abc_classify(
    annual_values: Sequence[float],
    params: PolicyParams = DEFAULT_PARAMS,
) -> list[str]:
    """Return ['A'|'B'|'C'] aligned with the input, by cumulative value share.

    Standard Pareto split: A = items making up the first 80% of annual
    consumption value, B = next 15%, C = remainder. Zero-value items are C.
    """
    n = len(annual_values)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: annual_values[i], reverse=True)
    total = sum(max(float(v), 0.0) for v in annual_values)
    out = ["C"] * n
    if total <= 0:
        return out
    cum = 0.0
    for idx in order:
        value = max(float(annual_values[idx]), 0.0)
        if value <= 0:
            continue
        # Classify by the share *before* adding this item: the item that pushes
        # the cumulative share past the threshold still belongs to the lower tier.
        share_before = cum / total
        if share_before < params.abc_a_cum:
            out[idx] = "A"
        elif share_before < params.abc_b_cum:
            out[idx] = "B"
        cum += value
    return out


# ---------------------------------------------------------------------------
# EOQ reference
# ---------------------------------------------------------------------------


def economic_order_qty(
    rate: float,
    unit_cost: float,
    params: PolicyParams = DEFAULT_PARAMS,
) -> float | None:
    """EOQ = sqrt(2·K·D / (h·c)). Returns None when c ≤ 0 (cost unknown).

    `rate` is per day; D is annualised (×365). This is a *reference* only:
    the ordering policy is (R, S) because the business reviews weekly. EOQ is
    used to flag "your weekly order is 3x the economic batch" (over-stocking
    tendency) or "you're ordering below 1/3 EOQ" (too many small orders).
    """
    if rate is None or rate <= 0 or unit_cost is None or unit_cost <= 0:
        return None
    annual_demand = float(rate) * 365.0
    denom = params.holding_rate * float(unit_cost)
    if denom <= 0:
        return None
    return _q(math.sqrt(2.0 * params.order_cost * annual_demand / denom), 2)


def order_frequency_per_year(rate: float, qty: float) -> float | None:
    """How many orders per year the current quantity implies (for EOQ compare)."""
    if rate is None or rate <= 0 or qty is None or qty <= 0:
        return None
    return _q(float(rate) * 365.0 / float(qty), 1)


# ---------------------------------------------------------------------------
# Overstock quantification
# ---------------------------------------------------------------------------


def excess_quantity(on_hand: float, target: float) -> float:
    """Units above the order-up-to level — i.e. the over-stocked portion."""
    excess = float(on_hand) - float(target)
    return _q(excess, 2) if excess > 0 else 0.0


# ---------------------------------------------------------------------------
# Aggregation helpers (category / warehouse roll-ups)
# ---------------------------------------------------------------------------


@dataclass
class Rollup:
    key: str
    stock_value: float = 0.0
    monthly_value: float = 0.0
    suggested_value: float = 0.0
    ss_value: float = 0.0
    sku_count: int = 0
    action_count: int = 0        # 需订货（且确实可订）
    critical_count: int = 0
    overstock_count: int = 0
    no_signal_count: int = 0
    valued_sku: int = 0          # 有 unit_cost 的品项数（金额可信度）
    # 政策目标水位金额 Σ(target×cost)：周转率的分母基准，来自同一套 (R,S) 模型，
    # 不是外部拍脑袋的行业值。
    target_value: float = 0.0
    # 零消耗但有结存的金额（呆滞）：周转率被它拖慢时，卡片要能说清是谁拖的。
    idle_value: float = 0.0

    @property
    def coverage_days(self) -> float | None:
        """Value-weighted days of cover = stock value / daily consumption value."""
        daily = self.monthly_value / 30.0
        if daily <= 0:
            return None
        return _q(self.stock_value / daily, 1)

    @property
    def value_coverage(self) -> float:
        if self.sku_count <= 0:
            return 1.0
        return _q(self.valued_sku / self.sku_count, 3)


def rollup(
    key: str,
    rows: Iterable[dict],
    qty_key: str = "quantity",
) -> Rollup:
    """Aggregate item dicts into a Rollup.

    Rows are expected to carry: `quantity`, `unit_cost`, `rate`,
    `suggested_qty`, `safety_stock`, `target`, `status`. Missing keys degrade to 0.
    """
    out = Rollup(key=key)
    for r in rows:
        cost = float(r.get("unit_cost") or 0.0)
        qty = float(r.get(qty_key) or 0.0)
        rate = float(r.get("rate") or 0.0)
        out.sku_count += 1
        if cost > 0:
            out.valued_sku += 1
            out.stock_value += qty * cost
            out.monthly_value += rate * 30.0 * cost
            out.suggested_value += float(r.get("suggested_qty") or 0.0) * cost
            out.ss_value += float(r.get("safety_stock") or 0.0) * cost
            out.target_value += float(r.get("target") or 0.0) * cost
            if rate <= 0 and qty > 0:
                out.idle_value += qty * cost
        status = r.get("status")
        if status in (STATUS_CRITICAL, STATUS_DUE):
            out.action_count += 1
        if status == STATUS_CRITICAL:
            out.critical_count += 1
        if status == STATUS_OVERSTOCK:
            out.overstock_count += 1
        if status in (STATUS_DEAD, "no_signal"):
            out.no_signal_count += 1

    out.stock_value = _q(out.stock_value, 2)
    out.monthly_value = _q(out.monthly_value, 2)
    out.suggested_value = _q(out.suggested_value, 2)
    out.ss_value = _q(out.ss_value, 2)
    out.target_value = _q(out.target_value, 2)
    out.idle_value = _q(out.idle_value, 2)
    return out


# ---------------------------------------------------------------------------
# 库存周转率
# ---------------------------------------------------------------------------
#
# 口径：周转率（次/年）= 年化消耗金额 ÷ 时点库存金额。
#
# 两处必须说清楚，否则数字会被误读：
#   1) 分母是**时点**库存，不是期间平均库存 —— 本系统不存历史库存快照，
#      拿不到真正的平均值。所以这是「按今天的库存结构年化」，不是会计口径的
#      存货周转率，跨月比较才有意义，不能拿去和财报口径对账。
#   2) 年化用 365 天，且与库存天数严格互逆：turns = 365 ÷ days。卡片上同时
#      出现「可支撑 N 天」和「周转 M 次/年」时，两者必须能对上，否则读者会
#      怀疑其中一个算错了。
#
# 目标值不套行业基准，而是用**同一套补货政策**反推：
#   目标库存金额 = Σ(target_i × cost_i)，其中 target = d̄(L+R) + SS
# 实际周转慢于目标 = 手里压着超过政策要求的货；快于目标 = 备货偏紧、有断货
# 风险。这比「行业平均 6 次/年」有意义得多 —— 后者对不上 L+R+SS 这些参数。

TURNOVER_SLOW_GAP = -0.15   # 慢于政策目标 15% 以上 → 库存偏高
TURNOVER_FAST_GAP = 0.30    # 快于政策目标 30% 以上 → 备货偏紧
DAYS_PER_YEAR = 365.0


@dataclass
class Turnover:
    days: float | None = None          # 库存天数（时点库存 ÷ 日均消耗额）
    turns: float | None = None         # 年周转次数
    target_days: float | None = None   # 政策目标库存天数 = L+R+SS 折算
    target_turns: float | None = None
    gap: float | None = None           # (turns − target_turns) ÷ target_turns
    tone: str = "muted"                # ok / warn / info / muted
    verdict: str = "—"                 # 短标签，卡片徽章用
    note: str = ""                     # 一句话解释，title 提示用

    def as_dict(self) -> dict:
        return {
            "days": self.days,
            "turns": self.turns,
            "target_days": self.target_days,
            "target_turns": self.target_turns,
            "gap": self.gap,
            "tone": self.tone,
            "verdict": self.verdict,
            "note": self.note,
        }


def inventory_turnover(
    stock_value: float,
    monthly_value: float,
    target_value: float,
) -> Turnover:
    """Compute turnover for one aggregation bucket.

    All three inputs are already value-weighted sums, so mixed units are fine —
    the ratio is dimensionless (¥ out ÷ ¥ held).
    """
    stock = float(stock_value or 0.0)
    monthly = float(monthly_value or 0.0)
    target = float(target_value or 0.0)
    daily = monthly / 30.0

    if daily <= 0:
        if stock <= 0:
            return Turnover(note="无库存、无消耗，周转率无意义")
        # 有货却算不出消耗：周转停在 0，这是呆滞，不是「算不出来」。
        return Turnover(
            days=None,
            turns=0.0,
            target_days=None,
            target_turns=None,
            gap=None,
            tone="warn",
            verdict="零消耗",
            note="窗口内无消耗记录，库存周转为 0",
        )

    if stock <= 0:
        return Turnover(
            days=0.0,
            turns=None,
            target_days=_q(target / daily, 1) if target > 0 else None,
            target_turns=None,
            gap=None,
            tone="info",
            verdict="零库存",
            note="当前无库存，周转率无上界",
        )

    days = stock / daily
    turns = DAYS_PER_YEAR / days
    target_days = target / daily if target > 0 else None
    target_turns = DAYS_PER_YEAR / target_days if target_days else None

    gap = None
    if target_turns:
        gap = (turns - target_turns) / target_turns

    if gap is None:
        return Turnover(
            days=_q(days, 1),
            turns=_q(turns, 2),
            tone="muted",
            verdict="—",
            note="缺政策目标水位，无法判断快慢",
        )

    if gap <= TURNOVER_SLOW_GAP:
        tone, verdict = "warn", "偏高"
        note = (
            f"周转比政策目标慢 {abs(gap) * 100:.0f}%："
            f"当前可支撑 {days:.0f} 天，政策只要求 {target_days:.0f} 天"
        )
    elif gap >= TURNOVER_FAST_GAP:
        tone, verdict = "info", "偏紧"
        note = (
            f"周转比政策目标快 {gap * 100:.0f}%："
            f"当前仅 {days:.0f} 天，政策要求 {target_days:.0f} 天，断货风险偏高"
        )
    else:
        tone, verdict = "ok", "合理"
        note = f"周转接近政策目标（实际 {days:.0f} 天 / 目标 {target_days:.0f} 天）"

    return Turnover(
        days=_q(days, 1),
        turns=_q(turns, 2),
        target_days=_q(target_days, 1),
        target_turns=_q(target_turns, 2),
        gap=_q(gap, 3),
        tone=tone,
        verdict=verdict,
        note=note,
    )


# ---------------------------------------------------------------------------
# 三口径融合：产量 / 领料 / 盘点
# ---------------------------------------------------------------------------
#
# 业务事实（2026-10-20 用生产快照核对）：
#   1) `production_run_items.actual_qty` 与 `stock_movements(生产消耗)` 是**同一本账**
#      —— 逐品项集合完全一致、数量一致（wh_002 的少量差异来自回退）。所以
#      「生产领料」不是独立于库存消耗的第二信号，它本身就是消耗记录。
#      独立于账面的生产信号是 `production_runs.output_qty`（产量）。
#   2) 成品不做库存管理（冰激凌成品仅 1 个 SKU 有零星出入库），生产即销售
#      → 产量可以当销量代理。
#   3) 盘点**每周一次**（中位间隔 7 天），给出账面之外的实盘水位。
#      `I_start + 入库 − I_end` 就是这段期间真实消失量，不依赖任何领料记录。
#
# 因此三口径的分工是：
#   P 产量口径：rate_p = 产量 × 经验单耗 ÷ 天数      —— 独立的「应该用多少」
#   I 领料口径：rate_i = (生产消耗 + 出库) ÷ 天数    —— 高频、有偏（漏记/混入）
#   S 盘点口径：rate_s = (I_start + 入库 − I_end) ÷ 天数 —— 低频、无偏

METHOD_COUNT = "count"
METHOD_ISSUE = "issue"
METHOD_PRODUCTION = "production"
METHOD_NONE_RATE = "none"

CHANNEL_LABELS = {
    METHOD_COUNT: "盘点实测",
    METHOD_ISSUE: "领料/出库",
    METHOD_PRODUCTION: "产量推算",
}

# 盘点口径最少要覆盖两个盘点周期：单周期无法判断差异是趋势还是噪声。
MIN_COUNT_INTERVALS = 2
# 口径修正系数的合理区间。超出说明数据本身坏了，宁可用 1.0 也不外推。
BIAS_FACTOR_MIN = 0.5
BIAS_FACTOR_MAX = 2.0
# 盘点速率 ÷ 领料速率 的合理上限。超过一个数量级判为盘点录入异常并回退。
# 阈值取 10 而不是 3：本数据集里长尾品项的领料速率常是 0.002/天量级，
# 3 倍门槛会把大量噪声级差异也标成异常，反而淹没真正的手误。
MAX_COUNT_RATIO = 10.0


@dataclass(frozen=True)
class ChannelRate:
    """一路需求信号的速率估计。"""

    rate: float
    days: float
    qty: float
    samples: int
    method: str
    outliers: int = 0          # 被剔除的异常区间数（盘点跳变 / 录入错误）

    @property
    def available(self) -> bool:
        return self.days > 0 and (self.qty > 0 or self.samples > 0)

    @property
    def coverage(self) -> float:
        """样本覆盖度：盘点看周期数，其余看有效天数。0..1。"""
        if self.method == METHOD_COUNT:
            return _q(min(self.samples / MIN_COUNT_INTERVALS, 1.0), 3)
        return _q(min(self.days / 28.0, 1.0), 3)


def channel_rate(
    qty: float, days: float, samples: int, method: str, outliers: int = 0
) -> ChannelRate:
    """把「期间总量 / 期间天数」包成 ChannelRate，天数非正时速率记 0。"""
    days = float(days or 0.0)
    qty = float(qty or 0.0)
    rate = qty / days if days > 0 else 0.0
    return ChannelRate(rate=_q(rate, 4), days=_q(days, 2), qty=_q(qty, 4),
                       samples=int(samples), method=method, outliers=int(outliers))


@dataclass(frozen=True)
class FusedRate:
    """融合后的需求速率 + 溯源信息（看板要能解释「这个数从哪来」）。"""

    rate: float
    method: str
    bias_factor: float          # 领料口径的修正系数（count/issue）
    gap_ratio: float | None     # (领料 − 盘点) / 盘点，正数=领料多记
    yield_ratio: float | None   # 实际单耗 / 标准单耗，>1 = 实际更费料
    shrink_rate: float          # 盘点揭示的日均损耗（已含在 count 口径内）
    confidence: str             # high | medium | low
    note: str
    suspect: bool = False       # 盘点口径与领料口径相差一个数量级 → 判为录入异常

    @property
    def has_signal(self) -> bool:
        return self.rate > 0


def bias_factor(
    count_rate: float,
    issue_rate: float,
    prior: float = 1.0,
) -> float:
    """口径修正系数 k = 盘点实测速率 ÷ 领料速率。

    k > 1 表示账面扣得比实际用得少（有未记录损耗，也就是盘亏）；
    k < 1 表示账面扣多了（重复记账 / 盘盈）。夹在 [0.5, 2.0]：
    超出这个范围基本是数据坏了，外推只会放大错误。
    """
    if issue_rate is None or issue_rate <= 0 or count_rate is None or count_rate <= 0:
        return prior
    raw = float(count_rate) / float(issue_rate)
    return _q(min(max(raw, BIAS_FACTOR_MIN), BIAS_FACTOR_MAX), 4)


def gap_ratio(issue_rate: float, count_rate: float) -> float | None:
    """领料口径相对盘点口径的偏离比例。None = 缺一路信号，不做判断。"""
    if not count_rate or count_rate <= 0 or issue_rate is None:
        return None
    return _q((float(issue_rate) - float(count_rate)) / float(count_rate), 4)


def yield_ratio(actual_unit_usage: float, standard_unit_usage: float) -> float | None:
    """实际单耗 ÷ 标准单耗（BOM 单位用量）。

    >1 = 实际比配方更费料（称量超量、报废）。这是配方漂移的早期信号，
    和「领料 vs 盘点」的差异是两件事，必须分开看。
    """
    if not standard_unit_usage or standard_unit_usage <= 0:
        return None
    return _q(float(actual_unit_usage) / float(standard_unit_usage), 4)


def fuse_demand(
    *,
    count: ChannelRate | None = None,
    issue: ChannelRate | None = None,
    production: ChannelRate | None = None,
    category_bias: float = 1.0,
) -> FusedRate:
    """按「可信度优先」融合三路信号。

    优先级：盘点 > 领料 × k > 产量推算。

    为什么不按固定权重加权平均：三路的偏差**不是零均值噪声**，而是系统性
    偏差（领料永远 ≤ 真实消耗，因为损耗不进领料）。对系统性偏差做加权平均
    只会得到一个「稳定的错数」。正确做法是：用无偏的低频信号定水平、
    用有偏的高频信号定形状（在数据层做保形缩放），并把偏差本身作为指标暴露。

    - 盘点可用（≥2 个周期）：直接取盘点速率，k 只用来解释偏差来源。
    - 盘点不可用：领料速率 × k。k 优先用品项自身历史，其次品类中位数，
      再次 1.0（不外推）。
    - 两路都没有：用产量推算兜底（标记 low，因为单耗是历史均值）。
    """
    usable_count = count if (count and count.available and count.samples >= MIN_COUNT_INTERVALS) else None
    usable_issue = issue if (issue and issue.available and issue.rate > 0) else None
    usable_prod = production if (production and production.available and production.rate > 0) else None

    # 交叉校验：盘点口径与领料口径相差一个数量级，九成是盘点数据异常。
    # 实证 wh_003「牛轧糖味」：09-07 盘盈 +2208.35、09-08 盘亏 −2208.06 是一对
    # 冲销记录（两次盘点相隔 21h、中间无出入库，净影响只有 +0.29 桶），但夹在
    # 中间的区间会算出 2208 桶/天的消耗。宁可回退领料口径，也不用一个明显
    # 是垃圾的数去定水位。
    suspect = False
    if usable_count is not None and usable_issue is not None and usable_issue.rate > 0:
        ratio = usable_count.rate / usable_issue.rate
        if ratio > MAX_COUNT_RATIO or ratio < 1.0 / MAX_COUNT_RATIO:
            suspect = True
            usable_count = None

    if usable_count is not None:
        k = bias_factor(usable_count.rate, usable_issue.rate if usable_issue else 0.0, prior=1.0)
        if usable_issue is None:
            k = 1.0
        gap = gap_ratio(usable_issue.rate, usable_count.rate) if usable_issue else None
        confidence = "high" if usable_count.coverage >= 1.0 else "medium"
        note = (
            "盘点实测定水平" + ("，领料口径偏差已识别" if gap is not None and abs(gap) > 0.10 else "")
        )
        return FusedRate(
            rate=usable_count.rate,
            method=METHOD_COUNT,
            bias_factor=k,
            gap_ratio=gap,
            yield_ratio=None,
            shrink_rate=_q(max(usable_count.rate - (usable_issue.rate if usable_issue else 0.0), 0.0), 4),
            confidence=confidence,
            note=note,
        )

    if usable_issue is not None:
        k = bias_factor(0.0, 0.0, prior=category_bias)
        k = _q(min(max(k, BIAS_FACTOR_MIN), BIAS_FACTOR_MAX), 4)
        rate = _q(usable_issue.rate * k, 4)
        return FusedRate(
            rate=rate,
            method=METHOD_ISSUE,
            bias_factor=k,
            gap_ratio=None,
            yield_ratio=None,
            shrink_rate=_q(rate - usable_issue.rate, 4),
            confidence="low" if suspect else ("medium" if usable_issue.coverage >= 1.0 else "low"),
            note=(
                "盘点口径与领料相差超过 3 倍，判为录入异常，已回退领料口径"
                if suspect
                else "无盘点覆盖，用领料速率 × 品类偏差系数"
            ),
            suspect=suspect,
        )

    if usable_prod is not None:
        return FusedRate(
            rate=usable_prod.rate,
            method=METHOD_PRODUCTION,
            bias_factor=1.0,
            gap_ratio=None,
            yield_ratio=None,
            shrink_rate=0.0,
            confidence="low",
            note="无领料也无盘点，用产量×经验单耗兜底",
        )

    return FusedRate(
        rate=0.0,
        method=METHOD_NONE_RATE,
        bias_factor=1.0,
        gap_ratio=None,
        yield_ratio=None,
        shrink_rate=0.0,
        confidence="low",
        note="三路信号都不可用",
    )


def scale_series_by_total(series: Sequence[float], target_total: float) -> list[float]:
    """保形缩放：把日序列按比例放大/缩小到指定总量。

    用盘点定水平、用领料定日形状时使用。**按比例缩放而不是整体加常数**，
    因为偏差（损耗）通常与用量成正比，不是每天固定加一个数。
    全零序列无法定形状 → 均摊，保证总量守恒。
    """
    total = sum(float(v) for v in series)
    n = len(series)
    if n == 0:
        return []
    if total <= 0:
        flat = float(target_total) / n if n else 0.0
        return [_q(flat, 6)] * n
    factor = float(target_total) / total
    return [_q(float(v) * factor, 6) for v in series]


def cusum_alarm(
    deviations: Sequence[float],
    slack: float,
    threshold: float,
) -> int | None:
    """单侧 CUSUM：返回首次越限的下标，无告警返回 None。

    用来抓「缓慢漂移」—— 单周 ±5% 的差异会淹没在噪声里，但连续 5 周同向
    就说明口径系统性变了（例如开始按新配方领料、或某台秤坏了）。
    slack 是允许的免罚带宽（用 σ 的量级），threshold 是告警线。
    """
    if not deviations:
        return None
    acc = 0.0
    for idx, dev in enumerate(deviations):
        acc = max(0.0, acc + abs(float(dev)) - max(float(slack), 0.0))
        if acc > float(threshold):
            return idx
    return None


def backtest_scores(actual: Sequence[float], predicted: Sequence[float]) -> dict:
    """回测指标：WAPE / bias / MAE / 覆盖天数偏差。

    WAPE（按量加权绝对百分比误差）作为主指标而不是 MAPE：本数据集里大量
    品项目均是 0.001 量级，MAPE 会被这些小分母炸掉，指标失去意义。
    bias 单独给出，因为「系统性偏低」比「随机波动」危险得多 —— 前者会
    稳定欠订、持续断货，而 WAPE 看不出方向。
    """
    pairs = [
        (float(a), float(p)) for a, p in zip(actual, predicted, strict=False)
        if a is not None and p is not None
    ]
    if not pairs:
        return {"n": 0, "wape": None, "bias": None, "mae": None}
    abs_err = sum(abs(p - a) for a, p in pairs)
    signed = sum(p - a for a, p in pairs)
    scale = sum(abs(a) for a, _ in pairs)
    n = len(pairs)
    return {
        "n": n,
        "wape": _q(abs_err / scale, 4) if scale > 0 else None,
        "bias": _q(signed / n, 4),
        "bias_pct": _q(signed / scale, 4) if scale > 0 else None,
        "mae": _q(abs_err / n, 4),
    }


# ---------------------------------------------------------------------------
# Top-level per-item evaluation
# ---------------------------------------------------------------------------


@dataclass
class ItemPlan:
    """Everything the dashboard needs for one (warehouse, item) pair."""

    warehouse_code: str
    warehouse_name: str
    warehouse_type: str
    item_id: int
    name: str
    sku: str
    unit: str
    category: str
    canonical_id: int | None
    quantity: float
    unit_cost: float
    on_order: float
    rate: float
    sigma: float
    method: str
    sig_status: str
    nonzero_days: int
    days_since_last: int | None
    abc: str
    service_level: float
    z: float
    lead_days: int
    review_days: int
    overstock_days: float
    safety_stock: float
    target: float
    suggested_qty: float
    cover_days: float | None
    status: str
    eoq: float | None
    excess_qty: float
    total_demand: float
    # 需求来源：downstream = 下游门店消耗汇总（DRP 拉动）；own = 本仓出库史兜底
    demand_source: str = "own"
    # 净需求（未按起订单位取整）；与 suggested_qty 的差额即取整放大
    raw_need: float = 0.0
    # --- 三口径溯源（2026-10-20 增补）---
    rate_issue: float = 0.0          # 领料/出库口径速率
    rate_count: float | None = None  # 盘点实测口径速率（None = 无盘点覆盖）
    rate_production: float | None = None  # 产量×经验单耗速率
    fuse_method: str = METHOD_ISSUE
    bias_factor: float = 1.0         # 盘点÷领料 的口径修正系数
    gap_ratio: float | None = None   # 领料相对盘点的偏离比例
    yield_ratio: float | None = None  # 实际单耗 ÷ 标准单耗
    shrink_rate: float = 0.0         # 日均损耗（已含在盘点速率内）
    production_qty: float = 0.0      # 窗口内产量（成品单位）
    count_qty: float | None = None   # 窗口内盘点实测消失量
    fuse_confidence: str = "low"
    fuse_note: str = ""
    count_suspect: bool = False      # 盘点口径被判为录入异常，已回退领料口径
    count_outliers: int = 0          # 剔除的异常盘点区间数
    orderable: bool = True           # 该仓订货目录是否开放（items.is_orderable）

    @property
    def suggested_value(self) -> float:
        return _q(self.suggested_qty * self.unit_cost, 2)

    @property
    def stock_value(self) -> float:
        return _q(self.quantity * self.unit_cost, 2)

    @property
    def monthly_value(self) -> float:
        return _q(self.rate * 30.0 * self.unit_cost, 2)

    @property
    def excess_value(self) -> float:
        return _q(self.excess_qty * self.unit_cost, 2)

    @property
    def round_up_extra(self) -> float:
        """Extra units created purely by the pack-size rounding."""
        return _q(max(self.suggested_qty - self.raw_need, 0.0), 4)

    @property
    def cover_after_order(self) -> float | None:
        """Days of cover after receiving the suggested order — overstock sanity check."""
        if self.rate <= 0:
            return None
        return _q((self.quantity + self.on_order + self.suggested_qty) / self.rate, 1)

    @property
    def over_rounding(self) -> bool:
        """True when the pack-size rounding alone pushes cover past the overstock line.

        长尾品项（日均 0.001 桶）按 1 桶起订 = 1000 天库存。这类品项不是
        「不该订」，而是「订一次管三年」——看板要把它和正常补货区分开，
        否则建议采购额里一半是取整放大出来的（实测 49%）。
        """
        cover = self.cover_after_order
        return bool(self.suggested_qty > 0 and cover is not None and cover > self.overstock_days)

    def as_dict(self) -> dict:
        return {
            "warehouse_code": self.warehouse_code,
            "warehouse_name": self.warehouse_name,
            "warehouse_type": self.warehouse_type,
            "item_id": self.item_id,
            "name": self.name,
            "sku": self.sku,
            "unit": self.unit,
            "category": self.category,
            "canonical_id": self.canonical_id,
            "quantity": self.quantity,
            "unit_cost": self.unit_cost,
            "on_order": self.on_order,
            "rate": self.rate,
            "sigma": self.sigma,
            "method": self.method,
            "sig_status": self.sig_status,
            "nonzero_days": self.nonzero_days,
            "days_since_last": self.days_since_last,
            "abc": self.abc,
            "service_level": self.service_level,
            "z": self.z,
            "lead_days": self.lead_days,
            "review_days": self.review_days,
            "safety_stock": self.safety_stock,
            "target": self.target,
            "suggested_qty": self.suggested_qty,
            "suggested_value": self.suggested_value,
            "cover_days": self.cover_days,
            "status": self.status,
            "eoq": self.eoq,
            "excess_qty": self.excess_qty,
            "excess_value": self.excess_value,
            "stock_value": self.stock_value,
            "monthly_value": self.monthly_value,
            "total_demand": self.total_demand,
            "demand_source": self.demand_source,
            "raw_need": self.raw_need,
            "round_up_extra": self.round_up_extra,
            "cover_after_order": self.cover_after_order,
            "over_rounding": self.over_rounding,
            "rate_issue": self.rate_issue,
            "rate_count": self.rate_count,
            "rate_production": self.rate_production,
            "fuse_method": self.fuse_method,
            "bias_factor": self.bias_factor,
            "gap_ratio": self.gap_ratio,
            "yield_ratio": self.yield_ratio,
            "shrink_rate": self.shrink_rate,
            "production_qty": self.production_qty,
            "count_qty": self.count_qty,
            "fuse_confidence": self.fuse_confidence,
            "fuse_note": self.fuse_note,
            "count_suspect": self.count_suspect,
            "count_outliers": self.count_outliers,
            "orderable": self.orderable,
        }


def plan_item(
    *,
    warehouse: dict,
    item: dict,
    series: Sequence[float],
    abc: str,
    params: PolicyParams = DEFAULT_PARAMS,
    pack_size: float = 1.0,
    min_order: float = 0.0,
    demand_source: str = "own",
    channels: dict | None = None,
    category_bias: float = 1.0,
    orderable: bool = True,
) -> ItemPlan:
    """Evaluate one warehouse×item pair end to end.

    `warehouse` needs code/name/type; `item` needs id/name/sku/unit/category/
    canonical_id/quantity/unit_cost and optionally on_order.

    `channels` （可选）三口径数据，键为 `count` / `issue` / `production`，
    值为 `ChannelRate` 或 None。传了就走融合速率；不传保持单一领料口径
    （向后兼容，也是没有盘点数据时的降级路径）。

    为什么融合后要同步缩放 σ：盘点口径把总量放大 k 倍，波动也按比例放大
    k 倍。只改均值不改波动，会把安全库存算小 —— 这正是「账面偏低 → 订得
    偏少 → 又断货」的闭环。
    """
    wh_type = warehouse.get("warehouse_type") or "storefront"
    lead = params.lead_days(wh_type)
    review = params.review_days(wh_type)
    service = params.service_for(abc)
    z = Z_BY_SERVICE.get(service, 1.645)

    est = estimate_demand(series, params)
    rate = est.rate
    sigma = est.sigma
    sig_status = est.status
    fuse = None
    rate_count = rate_prod = None

    if channels:
        rate_count = channels["count"].rate if channels.get("count") else None
        rate_prod = channels["production"].rate if channels.get("production") else None
        fuse = fuse_demand(
            count=channels.get("count"),
            issue=channels.get("issue"),
            production=channels.get("production"),
            category_bias=category_bias,
        )
        if fuse.rate > 0:
            rate = fuse.rate
            if est.rate > 0:
                # 保形缩放等价于把 σ 乘以同一比例
                sigma = _q(est.sigma * (fuse.rate / est.rate), 4)
            else:
                # 有盘点/产量信号但无日形状：退化为泊松假设 σ=√λ
                sigma = _q(math.sqrt(fuse.rate), 4)
            if sig_status == STATUS_NO_SIGNAL:
                sig_status = STATUS_SPARSE

    ss = safety_stock(sigma, lead, review, z)
    target = target_level(rate, lead, review, ss)
    on_hand = float(item.get("quantity") or 0.0)
    on_order = float(item.get("on_order") or 0.0)
    qty = order_quantity(target, on_hand, on_order, pack_size=pack_size, min_order=min_order)
    status = classify_stock_status(
        on_hand, on_order, rate, lead, review, sig_status, params.overstock_days
    )
    if status not in (STATUS_CRITICAL, STATUS_DUE):
        # 本周期不触发订货（覆盖 >= 提前期+复盘周期）。
        # 这时 order_quantity 仍可能为正（水位在 L+R 与目标水位之间，
        # 差额就是安全库存），但既然这一轮不下单，建议量就应当是 0 ——
        # 否则明细表会出现「状态正常 / 建议订 6 桶」这种自相矛盾的行。
        qty = 0.0
    cost = float(item.get("unit_cost") or 0.0)

    return ItemPlan(
        warehouse_code=str(warehouse.get("code") or ""),
        warehouse_name=str(warehouse.get("name") or ""),
        warehouse_type=wh_type,
        item_id=int(item.get("id") or 0),
        name=str(item.get("name") or ""),
        sku=str(item.get("sku") or ""),
        unit=str(item.get("unit") or ""),
        category=str(item.get("category") or "未分类"),
        canonical_id=item.get("canonical_id"),
        quantity=_q(on_hand, 4),
        unit_cost=_q(cost, 2),
        on_order=_q(on_order, 4),
        rate=_q(rate, 4),
        sigma=_q(sigma, 4),
        method=est.method,
        sig_status=sig_status,
        nonzero_days=est.nonzero_days,
        days_since_last=est.days_since_last,
        abc=abc,
        service_level=service,
        z=z,
        lead_days=lead,
        review_days=review,
        overstock_days=params.overstock_days,
        safety_stock=ss,
        target=target,
        suggested_qty=qty,
        cover_days=days_of_cover(on_hand, rate),
        status=status,
        eoq=economic_order_qty(rate, cost, params),
        excess_qty=excess_quantity(on_hand, target),
        total_demand=est.total,
        demand_source=demand_source,
        raw_need=net_requirement(target, on_hand, on_order),
        rate_issue=est.rate,
        rate_count=rate_count,
        rate_production=rate_prod,
        fuse_method=fuse.method if fuse else METHOD_ISSUE,
        bias_factor=fuse.bias_factor if fuse else 1.0,
        gap_ratio=fuse.gap_ratio if fuse else None,
        yield_ratio=channels.get("yield_ratio") if channels else None,
        shrink_rate=fuse.shrink_rate if fuse else 0.0,
        production_qty=float(channels.get("production_qty") or 0.0) if channels else 0.0,
        count_qty=float(channels["count"].qty) if channels and channels.get("count") else None,
        fuse_confidence=fuse.confidence if fuse else "low",
        fuse_note=fuse.note if fuse else "单口径（无盘点/产量数据）",
        count_suspect=bool(fuse.suspect) if fuse else False,
        count_outliers=int(channels["count"].outliers) if channels and channels.get("count") else 0,
        orderable=orderable,
    )


# ---------------------------------------------------------------------------
# Presentation helpers
# ---------------------------------------------------------------------------

STATUS_LABELS = {
    STATUS_CRITICAL: "断货风险",
    STATUS_DUE: "本周需订",
    STATUS_HEALTHY: "正常",
    STATUS_OVERSTOCK: "积压",
    STATUS_DEAD: "无库存无消耗",
    "no_signal": "无消耗记录",
}

STATUS_ORDER = [STATUS_CRITICAL, STATUS_DUE, STATUS_OVERSTOCK, STATUS_HEALTHY, "no_signal", STATUS_DEAD]

# 每档状态的展示色（与 style.css 的 status-* 类对应）。
STATUS_TONE = {
    STATUS_CRITICAL: "danger",
    STATUS_DUE: "warn",
    STATUS_HEALTHY: "ok",
    STATUS_OVERSTOCK: "info",
    "no_signal": "muted",
    STATUS_DEAD: "muted",
}


def fmt_qty(value: float | None, places: int = 2) -> str:
    """Quantities are usually small decimals (0.08 箱); keep 2dp, strip trailing zeros."""
    if value is None:
        return "—"
    text = f"{float(value):,.{places}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def parse_events(rows: Iterable[tuple]) -> list[tuple[str, float]]:
    """Normalise SQL rows (created_at, qty) into (YYYY-MM-DD, qty) tuples."""
    out: list[tuple[str, float]] = []
    for row in rows:
        try:
            ts, qty = row
            day = str(ts)[:10]
            datetime.strptime(day, "%Y-%m-%d")
            out.append((day, float(qty)))
        except (TypeError, ValueError):
            continue
    return out


__all__ = [
    "DEFAULT_PARAMS",
    "DemandEstimate",
    "ItemPlan",
    "PolicyParams",
    "Rollup",
    "STATUS_CRITICAL",
    "STATUS_DEAD",
    "STATUS_DUE",
    "STATUS_HEALTHY",
    "STATUS_LABELS",
    "STATUS_ORDER",
    "STATUS_OVERSTOCK",
    "STATUS_TONE",
    "CHANNEL_LABELS",
    "ChannelRate",
    "FusedRate",
    "abc_classify",
    "aggregate_series",
    "backtest_scores",
    "bias_factor",
    "build_daily_series",
    "channel_rate",
    "classify_stock_status",
    "cusum_alarm",
    "days_of_cover",
    "economic_order_qty",
    "estimate_demand",
    "excess_quantity",
    "fmt_qty",
    "fuse_demand",
    "gap_ratio",
    "order_frequency_per_year",
    "order_quantity",
    "parse_events",
    "plan_item",
    "rollup",
    "safety_stock",
    "scale_series_by_total",
    "target_level",
    "yield_ratio",
]
