"""为新天地店 (wh_003) 填充约两个月的演示数据。

时间窗口: 2026-05-21 ~ 2026-07-20
覆盖场景:
    - 每日入库 (restock) 和出库 (outbound),集中于早/午高峰
    - 每日 1~2 次生产 (production_runs),按 BOM 真实消耗原料
    - 6/30 月末盘点 + 7/20 月末盘点 (approved,模拟少量盘亏/盘盈)

设计原则:
    - 写入前清理脚本自己历史生成的行(通过 note 前缀 'seed:' 识别),
      可重复运行。
    - 严格按蓝图里的 INSERT 顺序写 (主表 + stock_movements 双写),
      保证 /summary、/consumption、MCP item_consumption 等报表能正常
      统计。
    - 盘点走 approve 流程 (写盘亏 synthetic outbound_requests + 盘盈
      库存调整),贴合真实业务。
    - 不修改 master.db, 不创建用户/角色, 不调整 items 表结构。
    - 假定脚本由仓库管理员手动运行一次,后续若需重新填充可直接再跑。

运行:
    python3 scripts/seed_wh003_demo_data.py
"""
from __future__ import annotations

import os
import random
import sqlite3
from datetime import datetime, timedelta

# ---- 路径配置 -----------------------------------------------------------

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
WH_DB_PATH = os.path.join(REPO_ROOT, "db", "warehouses", "wh_003.db")

# 时间窗口 (含两端)
WINDOW_START = datetime(2026, 5, 21, 0, 0, 0)
WINDOW_END = datetime(2026, 7, 20, 23, 59, 59)

# 种子脚本自己写入的 note 前缀,用于识别并清理历史
SEED_TAG = "[seed:wh003]"

# 库存品元数据: (item_id, name, unit, daily_outbound_target, daily_restock_target)
#   daily_outbound_target: 期望每日出库量 (含生产消耗 + 普通出库)
#   daily_restock_target: 期望每日入库量
ITEM_PROFILES = {
    81: {"name": "茶叶-红茶",     "unit": "g",   "out_per_day": 65.0,  "in_per_day": 80.0},
    82: {"name": "茶叶-绿茶",     "unit": "g",   "out_per_day": 95.0,  "in_per_day": 120.0},
    83: {"name": "木樨子油",       "unit": "瓶", "out_per_day": 0.25,  "in_per_day": 0.4},
    84: {"name": "柠檬浓缩液",     "unit": "瓶", "out_per_day": 1.2,   "in_per_day": 1.5},
    85: {"name": "白砂糖",         "unit": "kg", "out_per_day": 0.75,  "in_per_day": 1.5},
    86: {"name": "冰块",           "unit": "kg", "out_per_day": 0.0,   "in_per_day": 0.0},  # 冰块不入库存
    87: {"name": "珍珠",           "unit": "g",   "out_per_day": 570.0, "in_per_day": 800.0},
    88: {"name": "奶精粉",         "unit": "g",   "out_per_day": 380.0, "in_per_day": 500.0},
}

# 产品 BOM: 经典柠檬茶 (id=13) / 珍珠奶茶 (id=14)
PRODUCT_BOM = {
    13: [  # 经典柠檬茶
        (81, 5.0),    # 红茶 5g
        (83, 0.02),   # 木樨子油 0.02 瓶
        (84, 0.10),   # 柠檬浓缩液 0.1 瓶
        (85, 30.0),   # 白砂糖 30g
    ],
    14: [  # 珍珠奶茶
        (82, 5.0),    # 绿茶 5g
        (85, 20.0),   # 白砂糖 20g
        (87, 30.0),   # 珍珠 30g
        (88, 20.0),   # 奶精粉 20g
    ],
}

PRODUCT_NAME = {13: "经典柠檬茶", 14: "珍珠奶茶"}

# 每天各产品的生产杯数 (闭店前批量生产)
DAILY_PRODUCTION = {13: (15, 35), 14: (25, 55)}


# ---- 工具 ---------------------------------------------------------------


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(WH_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def cleanup(conn: sqlite3.Connection) -> None:
    """清理脚本之前写入的所有行 (按 note/前缀识别)。"""
    # stock_movements 是唯一权威的 activity 凭证,先看它即可。
    print(f"[cleanup] 移除 note 含 '{SEED_TAG}' 的历史行 ...")
    cur = conn.execute(
        "SELECT id, item_id, action, note FROM stock_movements WHERE note LIKE ?",
        (f"%{SEED_TAG}%",),
    )
    sm_rows = cur.fetchall()
    print(f"  stock_movements: {len(sm_rows)} 行")

    # 删除顺序: 子表 -> 主表 -> stock_movements
    # 先 SELECT 出 seed 期间的 production run / stocktake batch ids,
    # 再 DELETE 关联的子表和主表 (SQLite 子查询 DELETE 在某些情形
    # 下 rowcount 报告正常但实际未删干净,因此走 explicit-id 路径)。
    seed_run_ids = [
        int(r[0]) for r in conn.execute(
            "SELECT id FROM production_runs WHERE created_at BETWEEN ? AND ? "
            "AND note LIKE ?",
            (fmt(WINDOW_START), fmt(WINDOW_END), f"%{SEED_TAG}%"),
        ).fetchall()
    ]
    seed_batch_ids = [
        int(r[0]) for r in conn.execute(
            "SELECT id FROM stocktake_batches WHERE note LIKE ?",
            (f"%{SEED_TAG}%",),
        ).fetchall()
    ]
    print(f"  发现历史 seed: {len(seed_run_ids)} 个生产批次, {len(seed_batch_ids)} 个盘点批次")

    if seed_run_ids:
        ph = ",".join("?" * len(seed_run_ids))
        n_pri = conn.execute(
            f"DELETE FROM production_run_items WHERE run_id IN ({ph})",
            seed_run_ids,
        ).rowcount
        n_pr = conn.execute(
            f"DELETE FROM production_runs WHERE id IN ({ph})", seed_run_ids,
        ).rowcount
        # 同步删除 seed 期间生产产生的 outbound_requests (reason 含 run=#id)
        # 注意 outbound_requests 没有 FK 引用 production_runs,需要按
        # reason 里的 "run=#N" 字符串反向定位。
        for run_id in seed_run_ids:
            conn.execute(
                "DELETE FROM outbound_requests WHERE reason = ?",
                (f"{SEED_TAG}生产领料(run=#{run_id})",),
            )
        print(f"  production_run_items: -{n_pri}, production_runs: -{n_pr}")
    # 同步删除 seed 期间生产同步生成的 outbound_requests (reason 含 SEED_TAG)
    conn.execute(
        "DELETE FROM outbound_requests WHERE reason LIKE ?",
        (f"%{SEED_TAG}%production%",),
    )
    # 同步删除 seed 期间盘点 approve 写入的 synthetic 盘亏 outbound_requests
    conn.execute(
        "DELETE FROM outbound_requests WHERE reason LIKE ?",
        (f"%盘点审核%{SEED_TAG}%",),
    )
    # 盘点 batch 关联 (用 explicit-id,避免子查询 DELETE 不可靠)
    if seed_batch_ids:
        ph = ",".join("?" * len(seed_batch_ids))
        conn.execute(
            f"DELETE FROM stocktakes WHERE batch_id IN ({ph})", seed_batch_ids,
        )
        conn.execute(
            f"DELETE FROM stocktake_batches WHERE id IN ({ph})", seed_batch_ids,
        )
    # 普通 outbound / restock (note 含 SEED_TAG)
    conn.execute(
        "DELETE FROM outbound_requests WHERE reason LIKE ? AND created_at BETWEEN ? AND ?",
        (f"%{SEED_TAG}%outbound%", fmt(WINDOW_START), fmt(WINDOW_END)),
    )
    conn.execute(
        "DELETE FROM restock_requests WHERE reason LIKE ? AND created_at BETWEEN ? AND ?",
        (f"%{SEED_TAG}%", fmt(WINDOW_START), fmt(WINDOW_END)),
    )
    conn.execute(
        "DELETE FROM stock_movements WHERE note LIKE ?",
        (f"%{SEED_TAG}%",),
    )
    conn.commit()
    print("  历史 seed 行清理完毕")


def day_list() -> list[datetime]:
    days = []
    cur = WINDOW_START.date()
    end = WINDOW_END.date()
    while cur <= end:
        days.append(datetime(cur.year, cur.month, cur.day, 0, 0, 0))
        cur += timedelta(days=1)
    return days


def insert_outbound(
    conn: sqlite3.Connection,
    item_id: int,
    qty: float,
    ts: datetime,
    reason: str,
) -> int:
    """插入普通出库,按蓝图 outbound_create 路径双写 stock_movements。"""
    if qty <= 0:
        return -1
    cur = conn.execute(
        """INSERT INTO outbound_requests
           (item_id, requested_quantity, reason, status, rolled_back, created_at)
           VALUES (?, ?, ?, '出库', 0, ?)""",
        (item_id, qty, reason, fmt(ts)),
    )
    req_id = int(cur.lastrowid)
    conn.execute(
        """INSERT INTO stock_movements (item_id, action, delta, note, created_at)
           VALUES (?, '出库', ?, ?, ?)""",
        (item_id, -qty, f"{reason} #{req_id}", fmt(ts)),
    )
    return req_id


def insert_restock(
    conn: sqlite3.Connection,
    item_id: int,
    qty: float,
    ts: datetime,
    reason: str,
) -> int:
    """插入补货入库,直接 status='入库' (蓝图 restock.submit 路径)。"""
    if qty <= 0:
        return -1
    cur = conn.execute(
        """INSERT INTO restock_requests
           (item_id, requested_quantity, reason, status, created_at)
           VALUES (?, ?, ?, '入库', ?)""",
        (item_id, qty, reason, fmt(ts)),
    )
    req_id = int(cur.lastrowid)
    conn.execute(
        """INSERT INTO stock_movements (item_id, action, delta, note, created_at)
           VALUES (?, '补货入库', ?, ?, ?)""",
        (item_id, qty, f"{reason} #{req_id}", fmt(ts)),
    )
    return req_id


def insert_production_run(
    conn: sqlite3.Connection,
    product_id: int,
    output_qty: float,
    ts: datetime,
    note_suffix: str,
) -> int:
    """插入生产批次,按 BOM 双写 outbound_requests + stock_movements。"""
    if output_qty <= 0:
        return -1
    bom = PRODUCT_BOM[product_id]
    plan = []
    for item_id, qty_per in bom:
        planned = qty_per * output_qty
        # 模拟 ~5% 的轻微损耗 (actual < planned),让报表更真实
        actual = round(planned * random.uniform(0.95, 1.02), 2)
        plan.append((item_id, planned, actual))

    note = f"{SEED_TAG}production:{PRODUCT_NAME[product_id]}{note_suffix}"
    cur = conn.execute(
        """INSERT INTO production_runs
           (product_id, output_qty, note, rolled_back, created_by, created_at)
           VALUES (?, ?, ?, 0, 'seed-script', ?)""",
        (product_id, output_qty, note, fmt(ts)),
    )
    run_id = int(cur.lastrowid)

    for item_id, planned, actual in plan:
        conn.execute(
            """INSERT INTO production_run_items
               (run_id, item_id, planned_qty, actual_qty) VALUES (?, ?, ?, ?)""",
            (run_id, item_id, planned, actual),
        )
        # 生产同步生成一条 outbound_requests (蓝图 production.submit 路径)
        conn.execute(
            """INSERT INTO outbound_requests
               (item_id, requested_quantity, reason, status, rolled_back, created_at)
               VALUES (?, ?, ?, '出库', 0, ?)""",
            (item_id, actual, f"{SEED_TAG}生产领料(run=#{run_id})", fmt(ts)),
        )
        conn.execute(
            """INSERT INTO stock_movements (item_id, action, delta, note, created_at)
               VALUES (?, '生产消耗', ?, ?, ?)""",
            (item_id, -actual, f"{SEED_TAG}production:run#{run_id}", fmt(ts)),
        )
    return run_id


def insert_stocktake_batch(
    conn: sqlite3.Connection,
    ts: datetime,
    items_qty: dict[int, float],
    loss_pct: float = 0.02,
) -> int:
    """插入一次月末盘点 + 立刻 approve,模拟少量盘亏/盘盈。

    items_qty: {item_id: actual_quantity}
    loss_pct:  盘亏比例 (用于在 actual 基础上小幅随机浮动)。
    """
    cur = conn.execute(
        """INSERT INTO stocktake_batches
           (created_at, note, status, rolled_back) VALUES (?, ?, 'pending', 0)""",
        (fmt(ts), f"{SEED_TAG}stocktake:月末盘点"),
    )
    batch_id = int(cur.lastrowid)

    # previous_quantity 直接取 items.quantity 当时的快照
    rows = []
    for item_id, actual_qty in items_qty.items():
        prev_row = conn.execute(
            "SELECT quantity FROM items WHERE id = ?", (item_id,),
        ).fetchone()
        if prev_row is None:
            continue
        prev = float(prev_row["quantity"])
        # 在 actual 附近浮动 ±loss_pct,模拟真实盘点误差
        actual = max(0.0, actual_qty)
        diff = round(actual - prev, 3)
        rows.append((item_id, prev, actual, diff))

    for item_id, prev, actual, diff in rows:
        conn.execute(
            """INSERT INTO stocktakes
               (item_id, previous_quantity, actual_quantity, diff, batch_id,
                created_at, note)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (item_id, prev, actual, diff, batch_id, fmt(ts),
             f"{SEED_TAG}stocktake:月末盘点"),
        )

    # ---- 立即 approve (蓝图 stocktake.approve 路径) ----
    loss_items, gain_items = [], []
    for item_id, _prev, _actual, diff in rows:
        if diff == 0:
            continue
        (loss_items if diff < 0 else gain_items).append((item_id, diff))

    # 应用 quantity 调整
    for item_id, diff in loss_items + gain_items:
        conn.execute(
            "UPDATE items SET quantity = quantity + ?, updated_at = ? WHERE id = ?",
            (diff, fmt(ts), item_id),
        )

    # 盘亏 → synthetic outbound_requests + stock_movements(action='出库')
    loss_req_ids = []
    for item_id, diff in loss_items:
        loss_qty = abs(diff)
        cur2 = conn.execute(
            """INSERT INTO outbound_requests
               (item_id, requested_quantity, reason, status, rolled_back, created_at)
               VALUES (?, ?, ?, '出库', 0, ?)""",
            (item_id, loss_qty,
             f"盘点审核#{batch_id}盘亏 {SEED_TAG}",
             fmt(ts)),
        )
        rid = int(cur2.lastrowid)
        loss_req_ids.append(rid)
        conn.execute(
            """INSERT INTO stock_movements (item_id, action, delta, note, created_at)
               VALUES (?, '出库', ?, ?, ?)""",
            (item_id, diff, f"{SEED_TAG}stocktake:approve#{batch_id}盘亏 req#{rid}",
             fmt(ts)),
        )
    # 盘盈 → 仅库存调整
    for item_id, diff in gain_items:
        conn.execute(
            """INSERT INTO stock_movements (item_id, action, delta, note, created_at)
               VALUES (?, '库存调整', ?, ?, ?)""",
            (item_id, diff, f"{SEED_TAG}stocktake:approve#{batch_id}盘盈",
             fmt(ts)),
        )

    conn.execute(
        """UPDATE stocktake_batches SET status='approved', loss_req_ids=?
           WHERE id = ?""",
        (",".join(str(i) for i in loss_req_ids), batch_id),
    )
    return batch_id


# ---- 主流程 -------------------------------------------------------------


def seed_daily_business(conn: sqlite3.Connection) -> dict[str, int]:
    """为窗口内每一天生成当日的入库、出库、生产数据。"""
    rng = random.Random(20260521)  # 固定种子,可复现
    random.seed(20260521)

    counts = {"restock": 0, "outbound": 0, "production": 0}

    for day_start in day_list():
        # 早上 9:00 入库 (按比例模拟,冰块跳过)
        for item_id, prof in ITEM_PROFILES.items():
            if prof["in_per_day"] <= 0:
                continue
            base = prof["in_per_day"]
            qty = round(base * rng.uniform(0.6, 1.4), 2)
            insert_restock(
                conn,
                item_id,
                qty,
                day_start.replace(hour=9, minute=rng.randint(0, 40)),
                f"{SEED_TAG}早班补货",
            )
            counts["restock"] += 1

        # 白天 10~20 点多次出库 (随机 3~5 次)
        n_outbound = rng.randint(3, 5)
        for _ in range(n_outbound):
            hour = rng.randint(10, 19)
            minute = rng.randint(0, 59)
            ts = day_start.replace(hour=hour, minute=minute)
            for item_id, prof in ITEM_PROFILES.items():
                if prof["out_per_day"] <= 0:
                    continue
                # 每笔出库量 = 每日总量 / 出库次数 * 随机浮动
                qty = round(
                    (prof["out_per_day"] / n_outbound) * rng.uniform(0.5, 1.6),
                    2,
                )
                if qty <= 0:
                    continue
                insert_outbound(
                    conn,
                    item_id,
                    qty,
                    ts,
                    f"{SEED_TAG}outbound:门店领用",
                )
                counts["outbound"] += 1

        # 下午 18:30 批量生产
        prod_ts = day_start.replace(hour=18, minute=30)
        for product_id, (lo, hi) in DAILY_PRODUCTION.items():
            qty = round(rng.uniform(lo, hi), 1)
            insert_production_run(
                conn, product_id, qty, prod_ts, f"#{day_start.strftime('%m%d')}",
            )
            counts["production"] += 1

    return counts


def seed_monthly_stocktakes(conn: sqlite3.Connection) -> list[int]:
    """两次月末盘点,actual = items.quantity 当前值的 ±2%。"""
    batch_ids = []
    for ts in (
        datetime(2026, 6, 30, 21, 0, 0),
        datetime(2026, 7, 20, 21, 0, 0),
    ):
        rows = conn.execute(
            "SELECT id, quantity FROM items",
        ).fetchall()
        actuals: dict[int, float] = {}
        for r in rows:
            qty = float(r["quantity"])
            # 85(白砂糖)、84(柠檬浓缩液)、86(冰块)可能为 0,跳过
            if qty <= 0:
                continue
            # 在当前 quantity 基础上浮动 ±2% (模拟盘点误差)
            drift = random.uniform(0.97, 1.03)
            actuals[int(r["id"])] = round(qty * drift, 2)
        batch_id = insert_stocktake_batch(conn, ts, actuals)
        batch_ids.append(batch_id)
        print(f"  月末盘点写入 batch_id={batch_id} at {ts.date()}")
    return batch_ids


def main() -> None:
    if not os.path.exists(WH_DB_PATH):
        raise SystemExit(f"找不到 wh_003 数据库: {WH_DB_PATH}")

    print(f"==> seed wh_003 demo data ({WINDOW_START.date()} ~ {WINDOW_END.date()})")
    conn = connect()

    cleanup(conn)

    print("[1/2] 写入每日业务数据 (入库/出库/生产) ...")
    counts = seed_daily_business(conn)
    conn.commit()
    for k, v in counts.items():
        print(f"  {k}: {v} 条/批")

    print("[2/2] 写入两次月末盘点 (approved) ...")
    seed_monthly_stocktakes(conn)
    conn.commit()

    # ---- 摘要 ----
    print("\n=== 写入结果摘要 ===")
    seed_predicates = {
        "restock_requests": "reason LIKE ?",
        "outbound_requests": "reason LIKE ?",
        "production_runs": "note LIKE ?",
        "production_run_items": "1=0",  # 没有 note 列,数 production_runs 即可
        "stocktake_batches": "note LIKE ?",
        "stocktakes": "note LIKE ?",
        "stock_movements": "note LIKE ?",
    }
    for table in seed_predicates:
        n_total = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
        if seed_predicates[table] == "1=0":
            n_seed = "—"
        else:
            n_seed = conn.execute(
                f"SELECT COUNT(*) AS c FROM {table} WHERE {seed_predicates[table]}",
                (f"%{SEED_TAG}%",),
            ).fetchone()["c"]
        print(f"  {table:<25} 总计 {n_total:>5}  (seed 新增 {n_seed})")

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
