"""品项域纯逻辑（无 Flask 依赖，可单独单测）。

P0-12 安全库存自动计算（2026-10-09 Eric 拍板）：

    safety_stock = Σ 近 N 天消耗量 × FACTOR

消耗口径与 `/inventory` 页（`blueprints/items.py::inventory_view`）保持一致：
  1. `outbound_requests` 中 `rolled_back = 0` 的行，且排除
     `reason LIKE '生产领料(run=#%'`（该类出库是生产领料的镜像，消耗已由
     `production_run_items` 统计，不排除会重复计数）；
  2. `production_run_items JOIN production_runs` 中 `rolled_back = 0` 的行。

时间口径：SQLite 的 `datetime('now')` 为 UTC，`created_at` 由应用以
同一时区写入（见 `blueprints/_helpers.py::now`），字符串可直接比较。

覆盖判定：某品项的**最早一笔非回退消耗**晚于 `now - N days` 时，认为消耗
历史不足 N 天窗口，安全库存写 0（数据不足以代表一周的真实用量）。
"""
from __future__ import annotations

from decimal import Decimal

from config import (
    SAFETY_STOCK_FACTOR,
    SAFETY_STOCK_ROUND_DIGITS,
    SAFETY_STOCK_WINDOW_DAYS,
)

# 消耗口径 UNION（与 inventory_view 的 c7 子查询同源）。列：item_id, qty, ts
CONSUMPTION_UNION_SQL = """
    SELECT o.item_id AS item_id,
           o.requested_quantity AS qty,
           o.created_at AS ts
      FROM outbound_requests o
     WHERE o.rolled_back = 0
       AND (o.reason IS NULL OR o.reason NOT LIKE '生产领料(run=#%')
    UNION ALL
    SELECT pri.item_id AS item_id,
           pri.actual_qty AS qty,
           pr.created_at AS ts
      FROM production_run_items pri
      JOIN production_runs pr ON pr.id = pri.run_id
     WHERE pr.rolled_back = 0
"""


def _window_offset(window_days: int) -> str:
    return f"-{int(window_days)} days"


def round_qty(value: float, digits: int = SAFETY_STOCK_ROUND_DIGITS) -> float:
    """按 digits 位小数四舍五入（Decimal 避免二进制浮点尾差）。"""
    quant = Decimal(1).scaleb(-int(digits))
    return float(Decimal(str(value)).quantize(quant))


def compute_safety_stock(
    window_total_qty: float,
    factor: float = SAFETY_STOCK_FACTOR,
) -> float:
    """安全库存 = 窗口内消耗合计 × 系数。"""
    return round_qty(float(window_total_qty) * float(factor))


def consumption_history_covers_window(
    conn,
    item_id: int,
    window_days: int = SAFETY_STOCK_WINDOW_DAYS,
) -> bool:
    """该品项在本仓的消耗历史是否覆盖满 window_days 窗口。

    判定：存在非回退消耗记录，且最早一笔的时间 <= now - window_days。
    无任何消耗记录 → False（写 0）。
    """
    row = conn.execute(
        f"""SELECT MIN(ts) AS first_ts, datetime('now', ?) AS cutoff
              FROM ({CONSUMPTION_UNION_SQL}) t
             WHERE item_id = ?""",
        (_window_offset(window_days), item_id),
    ).fetchone()
    if row is None:
        return False
    first_ts = row[0]
    cutoff = row[1]
    if not first_ts or not cutoff:
        return False
    return str(first_ts) <= str(cutoff)


def recompute_warehouse_safety_stocks(
    conn,
    window_days: int = SAFETY_STOCK_WINDOW_DAYS,
    factor: float = SAFETY_STOCK_FACTOR,
) -> dict:
    """重算整仓 `items.safety_stock`（就地 UPDATE，不 commit）。

    返回统计：{total, updated, zeroed, window_days, factor}。
    `zeroed` = 因消耗历史不足窗口而写入 0 的品项数。

    不触碰 `updated_at`（该列语义为品项主数据/业务字段的更新时间，安全库存
    由系统批量刷新，改它会让全表 updated_at 失去参考价值）。
    """
    offset = _window_offset(window_days)

    totals = {
        r[0]: float(r[1] or 0)
        for r in conn.execute(
            f"""SELECT item_id, SUM(qty) AS qty
                  FROM ({CONSUMPTION_UNION_SQL}) t
                 WHERE ts >= datetime('now', ?)
                 GROUP BY item_id""",
            (offset,),
        ).fetchall()
    }
    firsts = {
        r[0]: r[1]
        for r in conn.execute(
            f"SELECT item_id, MIN(ts) AS first_ts FROM ({CONSUMPTION_UNION_SQL}) t "
            "GROUP BY item_id"
        ).fetchall()
    }
    cutoff_row = conn.execute(
        "SELECT datetime('now', ?) AS cutoff", (offset,)
    ).fetchone()
    cutoff = str(cutoff_row[0]) if cutoff_row else ""

    rows = conn.execute("SELECT id FROM items").fetchall()
    updates: list[tuple[float, int]] = []
    zeroed = 0
    for row in rows:
        item_id = row[0]
        first_ts = firsts.get(item_id)
        if not first_ts or str(first_ts) > cutoff:
            new_value = 0.0
            zeroed += 1
        else:
            new_value = compute_safety_stock(totals.get(item_id, 0.0), factor)
        updates.append((new_value, item_id))

    if updates:
        conn.executemany(
            "UPDATE items SET safety_stock = ? WHERE id = ?", updates
        )
    return {
        "total": len(rows),
        "updated": len(updates),
        "zeroed": zeroed,
        "window_days": int(window_days),
        "factor": float(factor),
    }
