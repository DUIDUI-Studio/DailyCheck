#!/usr/bin/env python3
"""补货预测滚动回测：四种口径模型 vs 盘点实测真值。

用法
----
    python3.12 scripts/forecast_backtest.py --db-dir /path/to/db [--weeks 6]

为什么这样评估
--------------
1. **真值取盘点锚定的消耗量**，不取任何账面记录 —— 否则就是拿账面对账账面。
   `C = I_期初实盘 + 入库 − I_期末实盘`，两期盘点相减即得。
2. **滚动原点（rolling origin）**：每次只用原点之前的数据拟合，预测下一个
   盘点周期的消耗量，避免未来信息泄漏。
3. **主指标用 WAPE 而不是 MAPE**：本数据集大量品项日均 0.001 量级，
   MAPE 会被小分母炸掉。同时给出 bias（方向），因为系统性偏低比随机波动危险。

四个对比模型
------------
  M0 旧口径    outbound_requests 30 天线性加权（含盘亏记录，原样复刻旧实现）
  M1 领料口径  stock_movements(生产消耗+出库，剔除盘亏) 56 天均值
  M2 品类校正  M1 × 品类口径系数 k（用原点之前的盘点历史估计）
  M3 三口径   盘点优先 + 数量级校验 + 无盘点时回退 M2
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from contextlib import closing
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from blueprints import replenishment_pure as R  # noqa: E402

WINDOW_DAYS = 56
INBOUND = "('补货入库','门店订货入库','生产入库')"
ISSUE = "('生产消耗','出库')"
LOSS = (
    "COALESCE(note,'') NOT LIKE '%盘亏%' AND COALESCE(note,'') NOT LIKE '%盘盈%' "
    "AND COALESCE(note,'') NOT LIKE '%盘点%'"
)


def _days(a: str, b: str) -> float:
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 86400.0


def load_batches(conn) -> list[dict]:
    return [
        {"id": int(r[0]), "created_at": str(r[1])}
        for r in conn.execute(
            """SELECT id, created_at FROM stocktake_batches
               WHERE status='approved' AND rolled_back=0 ORDER BY created_at"""
        )
    ]


def load_counts(conn) -> dict[int, dict[int, float]]:
    out: dict[int, dict[int, float]] = defaultdict(dict)
    for r in conn.execute("SELECT batch_id, item_id, actual_quantity FROM stocktakes"):
        out[int(r["batch_id"])][int(r["item_id"])] = float(r["actual_quantity"] or 0)
    return out


def inbound_between(conn, item_id: int, t0: str, t1: str) -> float:
    row = conn.execute(
        f"""SELECT COALESCE(SUM(delta),0) FROM stock_movements
            WHERE item_id=? AND action IN {INBOUND} AND created_at>? AND created_at<=?""",
        (item_id, t0, t1),
    ).fetchone()
    return float(row[0] or 0)


def issue_qty(conn, item_id: int, t_end: str, days: int) -> float:
    row = conn.execute(
        f"""SELECT COALESCE(SUM(-delta),0) FROM stock_movements
            WHERE item_id=? AND action IN {ISSUE} AND {LOSS}
              AND created_at > datetime(?, ?) AND created_at <= ?""",
        (item_id, t_end, f"-{days} days", t_end),
    ).fetchone()
    return float(row[0] or 0)


def legacy_qty(conn, item_id: int, t_end: str, days: int = 30) -> float:
    """旧实现：outbound_requests 上按 (30 - days_ago) 线性加权求日均，再 × 天数。

    rowfactory 必须是默认 tuple 模式（本函数只取两列）。
    """
    rows = conn.execute(
        """SELECT requested_quantity, created_at FROM outbound_requests
           WHERE item_id=? AND rolled_back=0 AND created_at > datetime(?, ?)
             AND created_at <= ?""",
        (item_id, t_end, f"-{days} days", t_end),
    ).fetchall()
    if not rows:
        return 0.0
    end = datetime.fromisoformat(t_end)
    wsum = wqty = 0.0
    for qty, ts in rows:
        ago = (end - datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S")).days
        if ago < 0 or ago >= days:
            continue
        w = days - ago
        wsum += w
        wqty += w * float(qty)
    if wsum <= 0:
        return 0.0
    return wqty / wsum  # 日均


def count_history(conn, items: list[int], batches: list[dict], counts, upto_idx: int):
    """返回 [(item_id, consumed, span_days), ...]，覆盖原点之前的所有盘点区间。"""
    out: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for i in range(upto_idx):
        t0 = batches[i]["created_at"]
        t1 = batches[i + 1]["created_at"]
        span = _days(t0, t1)
        if span <= 0.5:
            continue
        a = counts.get(batches[i]["id"], {})
        b = counts.get(batches[i + 1]["id"], {})
        for iid in a.keys() & b.keys():
            consumed = a[iid] + inbound_between(conn, iid, t0, t1) - b[iid]
            if consumed >= -1e-9:
                out[iid].append((consumed, span))
    return out


def score(pairs: list[tuple[float, float]]) -> dict:
    """pairs = [(truth, pred)]"""
    if not pairs:
        return {"n": 0}
    abs_err = sum(abs(p - t) for t, p in pairs)
    signed = sum(p - t for t, p in pairs)
    scale = sum(abs(t) for t, _ in pairs)
    hit = sum(1 for t, p in pairs if t > 0 and abs(p - t) / t <= 0.2)
    return {
        "n": len(pairs),
        "wape": abs_err / scale if scale else None,
        "bias_pct": signed / scale if scale else None,
        "hit20": hit / len(pairs),
        "mae": abs_err / len(pairs),
    }


def backtest(db_dir: Path, weeks: int) -> dict:
    results: dict[str, list[tuple[float, float]]] = {k: [] for k in ("M0", "M1", "M2", "M3", "M4")}
    per_wh: dict[str, dict[str, list]] = {}

    for wh_path in sorted(db_dir.glob("*.db")):
        with closing(sqlite3.connect(wh_path)) as conn:
            conn.row_factory = sqlite3.Row  # 名字索引 + 元组解包都可用
            items = [int(r[0]) for r in conn.execute(
                "SELECT id FROM items WHERE COALESCE(is_active,1)=1"
            )]
            batches = load_batches(conn)
            if len(batches) < 3:
                continue
            counts = load_counts(conn)

            # 只回测最近 `weeks` 个可评估区间
            start_idx = max(2, len(batches) - 1 - weeks)
            wh_pairs: dict[str, list] = {k: [] for k in results}

            for idx in range(start_idx, len(batches) - 1):
                t0 = batches[idx]["created_at"]
                t1 = batches[idx + 1]["created_at"]
                span = _days(t0, t1)
                if span <= 0.5:
                    continue
                hist = count_history(conn, items, batches, counts, idx)

                a = counts.get(batches[idx]["id"], {})
                b = counts.get(batches[idx + 1]["id"], {})
                truth: dict[int, float] = {}
                for iid in a.keys() & b.keys():
                    consumed = a[iid] + inbound_between(conn, iid, t0, t1) - b[iid]
                    if consumed >= 0:
                        truth[iid] = consumed
                if not truth:
                    continue

                # 品类级 k（只用原点之前的盘点历史）
                cat_of = {int(r[0]): str(r[1] or "未分类") for r in conn.execute(
                    """SELECT i.id, COALESCE(c.name,'未分类') FROM items i
                       LEFT JOIN categories c ON c.id=i.category_id"""
                )}
                cat_pairs: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
                for iid, intervals in hist.items():
                    cq = sum(c for c, _ in intervals)
                    dq = sum(s for _, s in intervals)
                    if dq <= 0:
                        continue
                    iq = issue_qty(conn, iid, t0, WINDOW_DAYS)
                    if iq > 0:
                        cat_pairs[cat_of.get(iid, "未分类")][0] += cq
                        cat_pairs[cat_of.get(iid, "未分类")][1] += iq
                cat_k = {
                    c: R.bias_factor(v[0], v[1]) for c, v in cat_pairs.items() if v[1] > 0
                }

                for iid, actual in truth.items():
                    issue_rate = issue_qty(conn, iid, t0, WINDOW_DAYS) / WINDOW_DAYS
                    m0 = legacy_qty(conn, iid, t0) * span
                    m1 = issue_rate * span
                    k = cat_k.get(cat_of.get(iid, "未分类"), 1.0)
                    m2 = m1 * k

                    intervals = hist.get(iid, [])
                    m3 = m2
                    if len(intervals) >= 2:
                        cq = sum(c for c, _ in intervals)
                        cd = sum(s for _, s in intervals)
                        cr = cq / cd if cd > 0 else 0.0
                        ratio = cr / issue_rate if issue_rate > 0 else 0.0
                        if issue_rate <= 0 or 1 / R.MAX_COUNT_RATIO <= ratio <= R.MAX_COUNT_RATIO:
                            m3 = cr * span
                        else:
                            m3 = m2  # 盘点口径异常 → 回退
                    # M4：盘点速率（中位数，抗单周跳变）× 2 与领料×k 各半
                    m4 = m3
                    if len(intervals) >= 2:
                        rates = sorted(c / s for c, s in intervals if s > 0)
                        if rates:
                            mid = len(rates) // 2
                            med = rates[mid] if len(rates) % 2 else (rates[mid - 1] + rates[mid]) / 2
                            m4 = 0.5 * med * span + 0.5 * m2

                    for key, pred in (("M0", m0), ("M1", m1), ("M2", m2), ("M3", m3), ("M4", m4)):
                        if actual > 0:
                            results[key].append((actual, pred))
                            wh_pairs[key].append((actual, pred))

            per_wh[wh_path.stem] = {k: score(v) for k, v in wh_pairs.items()}

    return {
        "overall": {k: score(v) for k, v in results.items()},
        "per_warehouse": per_wh,
        "pairs": {k: len(v) for k, v in results.items()},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-dir", required=True, help="包含各仓 .db 的目录")
    ap.add_argument("--weeks", type=int, default=6, help="回测的最近区间数")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rep = backtest(Path(args.db_dir), args.weeks)
    if args.json:
        import json
        print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
        return 0

    labels = {
        "M0": "旧口径（outbound 30d 加权，含盘亏）",
        "M1": "领料/出库口径（剔除盘亏）",
        "M2": "领料 × 品类口径系数 k",
        "M3": "三口径融合（盘点优先 + 数量级校验）",
        "M4": "盘点中位速率与领料各半",
    }
    print(f"\n样本：{rep['pairs']['M3']} 个 (品项 × 盘点区间) 观测\n")
    print(f"{'模型':<38}{'WAPE':>8}{'偏差':>9}{'±20%命中':>10}{'MAE':>10}")
    print("-" * 75)
    base = rep["overall"]["M0"].get("wape") or 0
    for key in ("M0", "M1", "M2", "M3", "M4"):
        s = rep["overall"][key]
        wape = s.get("wape")
        gain = f"{((base - wape) / base * 100):+.1f}%" if wape is not None and base else ""
        print(
            f"{labels[key]:<38}"
            f"{(wape * 100 if wape is not None else 0):>7.1f}%"
            f"{(s.get('bias_pct') or 0) * 100:>8.1f}%"
            f"{(s.get('hit20') or 0) * 100:>9.0f}%"
            f"{(s.get('mae') or 0):>10.3f}"
            f"   {gain}"
        )
    print("\n按仓库（M3）：")
    for wh, s in rep["per_warehouse"].items():
        m0 = s["M0"].get("wape")
        m3 = s["M3"].get("wape")
        if m3 is None:
            continue
        print(f"  {wh:<10} n={s['M3']['n']:>4}  M0 {m0 * 100 if m0 else 0:>6.1f}%  →  M3 {m3 * 100:>6.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
