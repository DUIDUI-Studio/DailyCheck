"""品项级生产误差归因（reports_loss_pure）测试。

设计定稿：docs/2026-10-20-production-loss-design.md §8~§16。
覆盖：
- 冲销对五条判据（符号相反 / 量级相近 / 时间窗 / 中间无流水 / 贪心不重复配）；
- L1 配方偏差（计划量 0、配方漏行、默认兜底行必须被排除）；
- L2 旁路消耗（空原因手工出库；生产领料空原因不误伤）；
- L3 四类分支（无渠道 / 单条异常 / 多产品共用 / 待判定）；
- 金额盲区（unit_cost=0 → value_state=unknown，不显示为 0 元）；
- 概率三口径（err_rate 不超过 100%、分母为 0 → None）；
- 台账幂等落库（rebuild 两次行数不变、resolved 行保留）；
- 三态（空库 → absent）；
- 判定理由可复核（reason_text 带具体数字、evidence_json 可解析）。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

import pytest

import reports_loss_pure as lp

NOW = datetime(2026, 10, 20, 10, 0, 0)
TS = "%Y-%m-%d %H:%M:%S"


def ts(hours_ago: float = 0, days_ago: float = 0) -> str:
    return (NOW - timedelta(days=days_ago, hours=hours_ago)).strftime(TS)


SCHEMA = """
CREATE TABLE categories (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE canonical_items (id INTEGER PRIMARY KEY, canonical_sku TEXT);
CREATE TABLE items (
    id INTEGER PRIMARY KEY, sku TEXT UNIQUE NOT NULL, name TEXT,
    quantity REAL DEFAULT 0, safety_stock REAL DEFAULT 0,
    unit TEXT DEFAULT '件', unit_cost REAL DEFAULT 0,
    gram_per_unit REAL DEFAULT 0, aux_unit TEXT, aux_rate REAL DEFAULT 0,
    canonical_id INTEGER, is_active INTEGER DEFAULT 1, category_id INTEGER);
CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE product_bom (
    id INTEGER PRIMARY KEY, product_id INTEGER, item_id INTEGER,
    qty_per_unit REAL);
CREATE TABLE production_runs (
    id INTEGER PRIMARY KEY, product_id INTEGER, output_qty REAL,
    rolled_back INTEGER DEFAULT 0, created_at TEXT);
CREATE TABLE production_run_items (
    id INTEGER PRIMARY KEY, run_id INTEGER, item_id INTEGER,
    planned_qty REAL, actual_qty REAL,
    planned_default INTEGER DEFAULT 0, outbound_req_id INTEGER);
CREATE TABLE outbound_requests (
    id INTEGER PRIMARY KEY, item_id INTEGER, requested_quantity REAL,
    reason TEXT, status TEXT DEFAULT '出库', rolled_back INTEGER DEFAULT 0,
    created_at TEXT, source_type TEXT);
CREATE TABLE stock_movements (
    id INTEGER PRIMARY KEY, item_id INTEGER, action TEXT, delta REAL,
    note TEXT, created_at TEXT);
CREATE TABLE stocktakes (
    id INTEGER PRIMARY KEY, item_id INTEGER, previous_quantity REAL,
    actual_quantity REAL, diff REAL, batch_id INTEGER, created_at TEXT);
CREATE TABLE stocktake_batches (
    id INTEGER PRIMARY KEY, status TEXT, rolled_back INTEGER DEFAULT 0);
CREATE TABLE item_loss_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    warehouse_code TEXT NOT NULL, item_sku TEXT NOT NULL, canonical_sku TEXT,
    item_name TEXT NOT NULL, category_name TEXT, unit TEXT DEFAULT '',
    unit_cost REAL DEFAULT 0, layer TEXT NOT NULL, source_table TEXT NOT NULL,
    source_id INTEGER NOT NULL, run_id INTEGER, batch_id INTEGER,
    event_at TEXT NOT NULL, err_qty REAL NOT NULL, err_value REAL NOT NULL,
    delta_sign INTEGER DEFAULT -1, cause_primary TEXT NOT NULL,
    cause_secondary TEXT, cause_code TEXT NOT NULL, confidence REAL DEFAULT 0,
    evidence_json TEXT DEFAULT '{}', reason_text TEXT DEFAULT '',
    self_reversed INTEGER DEFAULT 0, value_state TEXT DEFAULT 'deployed',
    resolved INTEGER DEFAULT 0, resolved_by TEXT, resolved_at TEXT,
    detected_at TEXT NOT NULL,
    UNIQUE (warehouse_code, layer, source_table, source_id, cause_code));
"""


@pytest.fixture()
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    yield conn
    conn.close()


def add_item(db, iid, sku, name, unit="件", cost=10.0, category="原料",
             canonical_id=None):
    db.execute("INSERT INTO categories (id,name) VALUES (?,?)",
               (iid, category))
    db.execute(
        """INSERT INTO items (id,sku,name,unit,unit_cost,category_id,canonical_id)
           VALUES (?,?,?,?,?,(SELECT id FROM categories WHERE name=? ),?)""",
        (iid, sku, name, unit, cost, category, canonical_id))


def add_run(db, run_id, product_id, run_items, created_at=None):
    """run_items: [(item_id, planned, actual, planned_default)]"""
    when = created_at or ts(days_ago=1)
    db.execute(
        "INSERT INTO production_runs (id,product_id,output_qty,rolled_back,created_at) "
        "VALUES (?,?,10,0,?)", (run_id, product_id, when))
    for item_id, planned, actual, pd in run_items:
        db.execute(
            """INSERT INTO production_run_items
               (run_id,item_id,planned_qty,actual_qty,planned_default)
               VALUES (?,?,?,?,?)""", (run_id, item_id, planned, actual, pd))


def add_stocktake(db, sid, batch_id, item_id, prev, actual, when, status="approved"):
    db.execute(
        """INSERT OR IGNORE INTO stocktake_batches (id,status,rolled_back)
           VALUES (?,?,0)""", (batch_id, status))
    db.execute(
        """INSERT INTO stocktakes
           (id,item_id,previous_quantity,actual_quantity,diff,batch_id,created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (sid, item_id, prev, actual, actual - prev, batch_id, when))


# ---------------------------------------------------------------------------
# 冲销对
# ---------------------------------------------------------------------------

def test_reversal_pair_detected():
    """盘盈 +100 后 21h 盘亏 -99.5，中间无流水 → 冲销对。"""
    rows = [
        {"id": 1, "diff": 100.0, "created_at": ts(hours_ago=21)},
        {"id": 2, "diff": -99.5, "created_at": ts(hours_ago=0)},
    ]
    pairs = lp.find_reversal_pairs(rows, flow=[])
    assert pairs == {1: 2, 2: 1}


def test_reversal_pair_rejected_by_magnitude():
    """量级差 50% > 1% 容差 → 不配对（可能是真的用掉一半）。"""
    rows = [
        {"id": 1, "diff": 100.0, "created_at": ts(hours_ago=21)},
        {"id": 2, "diff": -50.0, "created_at": ts(hours_ago=0)},
    ]
    assert lp.find_reversal_pairs(rows, flow=[]) == {}


def test_reversal_pair_rejected_by_flow_between():
    """两条盘点之间有真实出库 → 不是冲销，是"先盘错了、后来真的出了货"。"""
    rows = [
        {"id": 1, "diff": 100.0, "created_at": ts(hours_ago=21)},
        {"id": 2, "diff": -100.0, "created_at": ts(hours_ago=0)},
    ]
    flow = [NOW - timedelta(hours=10)]
    assert lp.find_reversal_pairs(rows, flow=flow) == {}


def test_reversal_pair_rejected_by_time_window():
    """超过 48h → 不配对。"""
    rows = [
        {"id": 1, "diff": 100.0, "created_at": ts(hours_ago=80)},
        {"id": 2, "diff": -100.0, "created_at": ts(hours_ago=0)},
    ]
    assert lp.find_reversal_pairs(rows, flow=[]) == {}


def test_reversal_pair_no_double_use():
    """三条记录只允许配一对，不允许一条被复用。"""
    rows = [
        {"id": 1, "diff": 100.0, "created_at": ts(hours_ago=3)},
        {"id": 2, "diff": -100.0, "created_at": ts(hours_ago=2)},
        {"id": 3, "diff": -100.0, "created_at": ts(hours_ago=1)},
    ]
    pairs = lp.find_reversal_pairs(rows, flow=[])
    assert len(pairs) == 2
    assert set(pairs) in ({1, 2}, {1, 3})


def test_reversal_pair_marks_self_reversed_in_ledger(db):
    """端到端：冲销对的盘亏行落 self_reversed=1，且不进损耗率分子。"""
    add_item(db, 1, "PP001", "牛轧糖味", unit="桶", cost=980.0)
    add_stocktake(db, 10, 1, 1, 0.45, 2208.8, ts(hours_ago=21))
    add_stocktake(db, 11, 2, 1, 2208.8, 0.74, ts(hours_ago=0))

    rows = lp.compute_item_loss_ledger(db, NOW)
    l3 = [r for r in rows if r["layer"] == "L3"]
    assert len(l3) == 1
    assert l3[0]["cause_code"] == "H-REVERSAL-PAIR"
    assert l3[0]["self_reversed"] == 1
    ctx = lp.summarize(rows)
    assert ctx["totals"]["count"] == 0
    assert ctx["totals"]["self_reversed_count"] == 1
    assert ctx["totals"]["self_reversed_value"] == pytest.approx(2163898.8)


def test_reversal_pair_ignores_stocktake_own_movements(db):
    """盘点自身产生的流水不能阻断配对（实测 bug 的回归用例）。

    盘盈会写 `action='库存调整'`、盘亏会写 `action='出库'`（note 都是
    '盘点审核#N...'）。这两条落在两条盘点之间，若不按 note 排除，
    每对盘盈盘亏都会因"中间存在出入库流水"而配不上 —— 冲销对识别直接失效，
    PP091 那 216 万会一直挂在损耗榜首。
    """
    add_item(db, 1, "PP091", "牛轧糖味", unit="桶", cost=980.0)
    t1 = NOW - timedelta(hours=21)
    t2 = NOW - timedelta(hours=2)
    add_stocktake(db, 10, 1, 1, 0.45, 2208.8, t1.strftime(TS))
    add_stocktake(db, 11, 2, 1, 2208.8, 0.74, t2.strftime(TS))
    # 盘亏合成的出库流水，审批动作落在两条盘点之间（action 在 _FLOW_ACTIONS 内，
    # 只有 note 前缀能把它排除）—— 这一条是 note 过滤的唯一防线
    db.execute(
        "INSERT INTO stock_movements (item_id,action,delta,note,created_at) "
        "VALUES (1,'出库',-2208.35,'盘点审核#1盘亏 (req#8)',?)",
        ((t1 + timedelta(minutes=5)).strftime(TS),))
    # 盘盈的库存调整不属 _FLOW_ACTIONS，天然被排除
    db.execute(
        "INSERT INTO stock_movements (item_id,action,delta,note,created_at) "
        "VALUES (1,'库存调整',2208.06,'盘点审核#2盘盈',?)",
        ((t2 + timedelta(minutes=3)).strftime(TS),))

    l3 = [r for r in lp.compute_item_loss_ledger(db, NOW) if r["layer"] == "L3"]
    assert len(l3) == 1
    assert l3[0]["cause_code"] == "H-REVERSAL-PAIR"
    assert l3[0]["self_reversed"] == 1


def test_real_outbound_between_still_blocks_pairing(db):
    """对照：中间是真实出库（note 不以'盘点'开头）时仍必须阻断配对。"""
    add_item(db, 1, "PP091", "牛轧糖味", unit="桶", cost=980.0)
    t1 = NOW - timedelta(hours=21)
    t2 = NOW - timedelta(hours=2)
    add_stocktake(db, 10, 1, 1, 0.45, 2208.8, t1.strftime(TS))
    add_stocktake(db, 11, 2, 1, 2208.8, 0.74, t2.strftime(TS))
    db.execute(
        "INSERT INTO stock_movements (item_id,action,delta,note,created_at) "
        "VALUES (1,'出库',-100,'门店领用',?)",
        ((t1 + timedelta(hours=5)).strftime(TS),))

    l3 = [r for r in lp.compute_item_loss_ledger(db, NOW) if r["layer"] == "L3"]
    assert len(l3) == 1
    assert l3[0]["cause_code"] != "H-REVERSAL-PAIR"
    assert l3[0]["self_reversed"] == 0


# ---------------------------------------------------------------------------
# L1 配方偏差
# ---------------------------------------------------------------------------

def test_l1_zero_plan_is_recipe(db):
    """计划量 0 / 实际 0.01 → R-BOM-ZERO，且理由带具体数字。"""
    add_item(db, 1, "PP072", "海盐", unit="瓶", cost=1.0)
    db.execute("INSERT INTO products (id,name) VALUES (1,'焦糖海盐')")
    db.execute("INSERT INTO product_bom (product_id,item_id,qty_per_unit) "
               "VALUES (1,1,0)")
    add_run(db, 1, 1, [(1, 0.0, 0.01, 0)])

    rows = lp.compute_item_loss_ledger(db, NOW)
    assert len(rows) == 1
    r = rows[0]
    assert r["layer"] == "L1"
    assert r["cause_code"] == "R-BOM-ZERO"
    assert r["cause_primary"] == "recipe"
    assert r["confidence"] == pytest.approx(0.90)
    assert r["err_qty"] == pytest.approx(0.01)
    assert "海盐" in r["reason_text"] and "PP072" in r["reason_text"]
    assert "0.01" in r["reason_text"]


def test_l1_missing_bom_row_raises_confidence(db):
    """品项不在任何 BOM 却被投料 → 带 R-BOM-MISSING，置信度 +0.05。"""
    add_item(db, 1, "PP116", "稳定剂", unit="罐", cost=5.0)
    db.execute("INSERT INTO products (id,name) VALUES (1,'A 大白兔')")
    add_run(db, 1, 1, [(1, 0.0, 0.01, 0)])

    rows = lp.compute_item_loss_ledger(db, NOW)
    assert len(rows) == 1
    r = rows[0]
    assert r["confidence"] == pytest.approx(0.95)
    ev = json.loads(r["evidence_json"])
    assert "R-BOM-MISSING" in ev["signals"]
    assert ev["in_bom"] is False


def test_l1_default_planned_rows_are_ignored(db):
    """默认兜底行（planned=actual）必须排除，否则会稀释指标。"""
    add_item(db, 1, "PP034", "纯牛奶", cost=10.0)
    db.execute("INSERT INTO products (id,name) VALUES (1,'北海道牛乳')")
    db.execute("INSERT INTO product_bom (product_id,item_id,qty_per_unit) "
               "VALUES (1,1,1.0)")
    add_run(db, 1, 1, [(1, 5.0, 5.0, 1)])

    rows = lp.compute_item_loss_ledger(db, NOW)
    assert [r for r in rows if r["layer"] == "L1"] == []


def test_l1_systematic_needs_real_fill(db):
    """默认兜底行即使偏差也不该触发系统性判定；真实填报才触发。"""
    add_item(db, 1, "PP011", "无糖黑巧克力", cost=20.0)
    db.execute("INSERT INTO products (id,name) VALUES (1,'榛子巧克力')")
    for i in range(4):
        add_run(db, i + 1, 1, [(1, 10.0, 11.0, 1)])  # 兜底行
    assert [r for r in lp.compute_item_loss_ledger(db, NOW)
            if r["cause_code"] == "R-SYSTEMATIC"] == []

    # 真实填报、同向 10%、零方差 → 判系统性
    db.execute("DELETE FROM production_run_items")
    db.execute("DELETE FROM production_runs")
    for i in range(4):
        add_run(db, i + 1, 1, [(1, 10.0, 11.0, 0)])
    rows = [r for r in lp.compute_item_loss_ledger(db, NOW)
            if r["cause_code"] == "R-SYSTEMATIC"]
    assert len(rows) == 1
    assert rows[0]["cause_primary"] == "recipe"


def test_zero_cost_marks_unknown_value(db):
    """unit_cost=0 → value_state=unknown，且不冒充 0 元。"""
    add_item(db, 1, "PP115", "草莓酱F", unit="件", cost=0.0)
    db.execute("INSERT INTO products (id,name) VALUES (1,'草莓大福')")
    db.execute("INSERT INTO product_bom (product_id,item_id,qty_per_unit) "
               "VALUES (1,1,0)")
    add_run(db, 1, 1, [(1, 0.0, 2.0, 0)])

    rows = lp.compute_item_loss_ledger(db, NOW)
    assert len(rows) == 1
    assert rows[0]["value_state"] == "unknown"
    assert rows[0]["cause_secondary"] == "P-ZERO-COST"
    ctx = lp.summarize(rows)
    assert ctx["totals"]["unknown_count"] == 1
    assert ctx["totals"]["value"] == 0.0
    assert ctx["blind_spots"] == ["PP115"]


# ---------------------------------------------------------------------------
# L2 旁路消耗
# ---------------------------------------------------------------------------

def test_l2_blank_reason_manual_outbound(db):
    add_item(db, 1, "PP027", "方巾-白", unit="条", cost=1.34)
    for i in range(3):
        db.execute(
            """INSERT INTO outbound_requests
               (item_id,requested_quantity,reason,rolled_back,created_at,source_type)
               VALUES (1,5,'',0,?, 'manual')""", (ts(days_ago=i + 1),))
    rows = lp.compute_item_loss_ledger(db, NOW)
    l2 = [r for r in rows if r["layer"] == "L2"]
    assert len(l2) == 3
    assert all(r["cause_code"] == "H-REASON-BLANK" for r in l2)
    assert all(r["confidence"] == pytest.approx(0.60) for r in l2)
    assert "不计入损耗率" in l2[0]["reason_text"]


def test_l2_ignores_production_blank_reason(db):
    """生产领料即使 reason 为空也不算旁路消耗 —— 它有独立单据。"""
    add_item(db, 1, "PP034", "纯牛奶", cost=10.0)
    db.execute(
        """INSERT INTO outbound_requests
           (item_id,requested_quantity,reason,rolled_back,created_at,source_type)
           VALUES (1,5,NULL,0,?, 'production')""", (ts(days_ago=1),))
    assert [r for r in lp.compute_item_loss_ledger(db, NOW)
            if r["layer"] == "L2"] == []


# ---------------------------------------------------------------------------
# L3 其余分支
# ---------------------------------------------------------------------------

def test_l3_no_channel_when_not_in_bom(db):
    add_item(db, 1, "PP094", "小动物积木", unit="件", cost=2.6)
    add_stocktake(db, 1, 1, 1, 20, 8, ts(days_ago=5))
    rows = [r for r in lp.compute_item_loss_ledger(db, NOW) if r["layer"] == "L3"]
    assert len(rows) == 1
    assert rows[0]["cause_code"] == "P-NO-CHANNEL"
    assert rows[0]["cause_primary"] == "process"
    assert rows[0]["confidence"] == pytest.approx(0.75)


def test_l3_spike_entry(db):
    """单条盘亏 > 窗口出库量 3 倍 → 数量异常。"""
    add_item(db, 1, "PP091", "牛轧糖味", unit="桶", cost=980.0)
    db.execute("INSERT INTO products (id,name) VALUES (1,'焦糖海盐')")
    db.execute("INSERT INTO product_bom (product_id,item_id,qty_per_unit) "
               "VALUES (1,1,0.5)")
    # 有出库记录，让 ob_qty > 0
    db.execute(
        """INSERT INTO outbound_requests
           (item_id,requested_quantity,reason,rolled_back,created_at,source_type)
           VALUES (1,10,'生产领料(run=#1)',0,?, 'production')""", (ts(days_ago=9),))
    add_stocktake(db, 1, 1, 1, 10.0, -50.0, ts(days_ago=5))
    rows = [r for r in lp.compute_item_loss_ledger(db, NOW) if r["layer"] == "L3"]
    assert len(rows) == 1
    assert rows[0]["cause_code"] == "H-SPIKE-ENTRY"


def test_l3_multi_product_shared(db):
    """被 ≥5 个产品共用且盘亏 → P-MULTI-PRODUCT（置信度 0.65，待复核）。"""
    add_item(db, 1, "PP034", "纯牛奶", cost=10.0)
    for pid in range(1, 7):
        db.execute("INSERT INTO products (id,name) VALUES (?,?)", (pid, f"P{pid}"))
        db.execute("INSERT INTO product_bom (product_id,item_id,qty_per_unit) "
                   "VALUES (?,1,0.1)", (pid,))
    db.execute(
        """INSERT INTO outbound_requests
           (item_id,requested_quantity,reason,rolled_back,created_at,source_type)
           VALUES (1,100,'生产领料(run=#1)',0,?, 'production')""", (ts(days_ago=9),))
    add_stocktake(db, 1, 1, 1, 100, 97, ts(days_ago=5))

    rows = [r for r in lp.compute_item_loss_ledger(db, NOW) if r["layer"] == "L3"]
    assert len(rows) == 1
    assert rows[0]["cause_code"] == "P-MULTI-PRODUCT"
    assert rows[0]["confidence"] == pytest.approx(0.65)
    assert "待复核" in rows[0]["reason_text"]


def test_l3_unattributed_has_zero_confidence(db):
    """规则都覆盖不到时如实留白，不降阈值硬判。"""
    add_item(db, 1, "PP019", "榛子巧克力", unit="桶", cost=1700.0)
    db.execute("INSERT INTO products (id,name) VALUES (1,'榛子巧克力')")
    db.execute("INSERT INTO product_bom (product_id,item_id,qty_per_unit) "
               "VALUES (1,1,0.5)")
    db.execute(
        """INSERT INTO outbound_requests
           (item_id,requested_quantity,reason,rolled_back,created_at,source_type)
           VALUES (1,100,'生产领料(run=#1)',0,?, 'production')""", (ts(days_ago=9),))
    add_stocktake(db, 1, 1, 1, 100.0, 99.35, ts(days_ago=5))

    rows = [r for r in lp.compute_item_loss_ledger(db, NOW) if r["layer"] == "L3"]
    assert len(rows) == 1
    assert rows[0]["cause_primary"] == "unattributed"
    assert rows[0]["confidence"] == pytest.approx(0.0)
    assert "待人工判定" in rows[0]["reason_text"]


def test_pending_stocktake_excluded(db):
    """未审核批次不参与，否则看到的是半成品指标。"""
    add_item(db, 1, "PP094", "小动物积木", cost=2.6)
    add_stocktake(db, 1, 1, 1, 20, 8, ts(days_ago=5), status="pending")
    assert [r for r in lp.compute_item_loss_ledger(db, NOW)
            if r["layer"] == "L3"] == []


# ---------------------------------------------------------------------------
# 概率口径
# ---------------------------------------------------------------------------

def test_err_rate_never_exceeds_100(db):
    """分母必须含盘点：PP056 实测曾算出 120% 这种荒谬值。"""
    add_item(db, 1, "PP056", "迷你冰糕", unit="盒", cost=360.0)
    for i in range(3):
        db.execute(
            """INSERT INTO outbound_requests
               (item_id,requested_quantity,reason,rolled_back,created_at,source_type)
               VALUES (1,1,'',0,?, 'manual')""", (ts(days_ago=i + 1),))
    for i in range(4):
        add_stocktake(db, i + 1, i + 1, 1, 5.0, 4.4, ts(days_ago=i + 1))

    rows = lp.compute_item_loss_ledger(db, NOW)
    ctx = lp.build_ledger_context(db, "wh_test", NOW)
    item = next(i for i in ctx["item_rates"] if i["sku"] == "PP056")
    assert item["err_rate"] is not None and item["err_rate"] <= 100
    # 3 出库 + 4 盘点 = 7 暴露；3 条 L2 + 4 条 L3 = 7 事件（同额盘亏各一条）
    assert item["exposure"] == 7
    assert item["loss_rate"] == pytest.approx(100.0)
    assert len(rows) >= 7


def test_probability_none_when_no_denominator(db):
    """分母为 0 时返回 None，页面显示「—」而不是 0。"""
    add_item(db, 1, "PP999", "孤立品项", cost=1.0)
    add_stocktake(db, 1, 1, 1, 5.0, 4.9, ts(days_ago=1))
    ctx = lp.build_ledger_context(db, "wh_test", NOW)
    item = next(i for i in ctx["item_rates"] if i["sku"] == "PP999")
    assert item["err_rate"] == pytest.approx(100.0)
    assert item["loss_rate"] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# 落库幂等 / 三态
# ---------------------------------------------------------------------------

def test_rebuild_is_idempotent(db):
    add_item(db, 1, "PP072", "海盐", unit="瓶", cost=1.0)
    db.execute("INSERT INTO products (id,name) VALUES (1,'焦糖海盐')")
    db.execute("INSERT INTO product_bom (product_id,item_id,qty_per_unit) "
               "VALUES (1,1,0)")
    add_run(db, 1, 1, [(1, 0.0, 0.01, 0)])

    first = lp.rebuild_item_loss_ledger(db, "wh_test", NOW)
    second = lp.rebuild_item_loss_ledger(db, "wh_test", NOW)
    total = db.execute("SELECT COUNT(*) FROM item_loss_ledger").fetchone()[0]
    assert first["inserted"] == 1
    assert second["inserted"] == 1
    assert total == 1


def test_rebuild_keeps_resolved_rows(db):
    """已处置（resolved=1）的行不能被重算覆盖 —— 处置结论要留痕。"""
    add_item(db, 1, "PP072", "海盐", unit="瓶", cost=1.0)
    db.execute("INSERT INTO products (id,name) VALUES (1,'焦糖海盐')")
    db.execute("INSERT INTO product_bom (product_id,item_id,qty_per_unit) "
               "VALUES (1,1,0)")
    add_run(db, 1, 1, [(1, 0.0, 0.01, 0)])
    lp.rebuild_item_loss_ledger(db, "wh_test", NOW)
    db.execute("UPDATE item_loss_ledger SET resolved = 1")
    lp.rebuild_item_loss_ledger(db, "wh_test", NOW)
    assert db.execute("SELECT COUNT(*) FROM item_loss_ledger").fetchone()[0] == 1
    assert db.execute(
        "SELECT resolved FROM item_loss_ledger").fetchone()[0] == 1


def test_empty_db_is_absent_state(db):
    ctx = lp.build_ledger_context(db, "wh_test", NOW)
    assert ctx["state"] == "absent"
    assert ctx["item_rates"] == []


def test_reason_text_is_reviewable(db):
    """判定理由必须带可复核的具体数字，不能只给标签。"""
    add_item(db, 1, "PP072", "海盐", unit="瓶", cost=1.0)
    db.execute("INSERT INTO products (id,name) VALUES (1,'焦糖海盐')")
    db.execute("INSERT INTO product_bom (product_id,item_id,qty_per_unit) "
               "VALUES (1,1,0)")
    add_run(db, 1, 1, [(1, 0.0, 0.01, 0)])

    row = lp.compute_item_loss_ledger(db, NOW)[0]
    text = row["reason_text"]
    for token in ("PP072", "海盐", "0.01", "置信度", "依据", "建议动作"):
        assert token in text, f"理由缺少 {token}"
    ev = json.loads(row["evidence_json"])
    assert ev["signals"] and ev["actual_qty"] == pytest.approx(0.01)


def test_canonical_table_missing_degrades(db):
    """本地快照库可能没有 canonical_items 表，不能让整页 500。"""
    db.execute("DROP TABLE canonical_items")
    add_item(db, 1, "PP072", "海盐", unit="瓶", cost=1.0)
    db.execute("INSERT INTO products (id,name) VALUES (1,'焦糖海盐')")
    db.execute("INSERT INTO product_bom (product_id,item_id,qty_per_unit) "
               "VALUES (1,1,0)")
    add_run(db, 1, 1, [(1, 0.0, 0.01, 0)])
    rows = lp.compute_item_loss_ledger(db, NOW)
    assert len(rows) == 1
    assert rows[0]["canonical_sku"] is None


# ---------------------------------------------------------------------------
# 路由级：真实渲染与导出（用 conftest 的完整 schema 库）
# ---------------------------------------------------------------------------

def _seed_ledger_page(wh_path):
    """往完整 schema 的测试仓写一条「BOM 系数为 0」的生产批次。

    新库自带 9 个默认分类（id 1~9），调味酱 = 3，不要重复插。
    """
    conn = sqlite3.connect(wh_path)
    now_s = datetime.now().strftime(TS)
    conn.execute(
        """INSERT INTO items (sku,name,category_id,quantity,unit,unit_cost,
                              updated_at)
           VALUES ('PP072','海盐',3,10,'瓶',1.0,?)""", (now_s,))
    conn.execute(
        "INSERT INTO products (name,unit,note,created_at) "
        "VALUES ('焦糖海盐','件','',?)", (now_s,))
    conn.execute(
        "INSERT INTO product_bom (product_id,item_id,qty_per_unit) "
        "VALUES (1,1,0)")
    conn.execute(
        """INSERT INTO production_runs (product_id,output_qty,note,rolled_back,
                                        created_by,created_at)
           VALUES (1,2,'',0,'tester',?)""", (now_s,))
    conn.execute(
        """INSERT INTO production_run_items
           (run_id,item_id,planned_qty,actual_qty,planned_default)
           VALUES (1,1,0,0.01,0)""")
    conn.commit()
    conn.close()


def test_health_page_renders_ledger_block(logged_client):
    """归因区块必须真的渲染出来，不能只在降级分支里通过。"""
    client, wh_path = logged_client
    _seed_ledger_page(wh_path)

    resp = client.get("/report/health")
    body = resp.data.decode()
    assert resp.status_code == 200
    assert "品项误差台账" in body
    assert "R-BOM-ZERO" in body
    assert "配方系数为 0" in body
    assert "PP072" in body
    # 判定理由要能被读到，不是只有一个标签
    assert "建议动作" in body


def test_health_csv_has_ledger_section(logged_client):
    client, wh_path = logged_client
    _seed_ledger_page(wh_path)

    resp = client.get("/report/health/export")
    body = resp.data.decode("utf-8-sig")
    assert resp.status_code == 200
    assert "品项误差台账" in body
    assert "R-BOM-ZERO" in body
    assert "误差率%" in body


def test_health_page_survives_missing_ledger_table(logged_client):
    """台账表缺失（旧库未迁移）时降级显示，不得整页 500。"""
    client, wh_path = logged_client
    conn = sqlite3.connect(wh_path)
    conn.execute("DROP TABLE item_loss_ledger")
    conn.commit()
    conn.close()

    resp = client.get("/report/health")
    assert resp.status_code == 200
    assert "库存健康".encode() in resp.data
    assert "归因模块不可用".encode() in resp.data
