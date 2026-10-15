"""库存健康报表（/report/health）测试。

覆盖（设计定稿 docs/2026-10-15-inventory-health-report-plan.md §8）：
- FIFO 重放三事件：压层 / 先进先出消层 / 盘点比例缩放；
- 账龄边界（第 7 天当天入库不算呆滞）、重放失配回退；
- DOS 边界（=7 天不算超储）、呆滞优先于超储；
- BOM 超耗（planned=0 兜底、阈值边界 =10% 不标红）；
- ABC 累计较性（80/95% 边界归属）；
- SKU 表排序白名单（非法 sort 回退默认）；
- 路由权限（staff 403 / manager 200）、range 白名单、CSV BOM；
- 导航三载体：manager 侧栏与 /land 均含新链接，staff 两侧均不含；
- Issue #38：时间戳容错（生产无秒形态「YYYY-MM-DD HH:MM」不再 500）。
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta

import pytest

import reports_health_pure as hp

NOW = datetime(2026, 10, 15, 12, 0, 0)
TS = "%Y-%m-%d %H:%M:%S"


def ts(days_ago: int, hour: int = 8) -> str:
    return (NOW - timedelta(days=days_ago)).replace(hour=hour).strftime(TS)


# ---------------------------------------------------------------------------
# 纯函数层：内存库 schema（只建被查询的表/列）
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE categories (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE items (
    id INTEGER PRIMARY KEY, name TEXT, quantity REAL DEFAULT 0,
    safety_stock REAL DEFAULT 0, unit TEXT DEFAULT '件', unit_cost REAL DEFAULT 0,
    gram_per_unit REAL, updated_at TEXT, is_active INTEGER DEFAULT 1,
    category_id INTEGER REFERENCES categories(id));
CREATE TABLE stock_movements (
    id INTEGER PRIMARY KEY, item_id INTEGER, action TEXT, delta REAL,
    note TEXT, created_at TEXT);
CREATE TABLE restock_requests (
    id INTEGER PRIMARY KEY, item_id INTEGER, requested_quantity REAL,
    reason TEXT, status TEXT DEFAULT '提交', created_at TEXT);
CREATE TABLE outbound_requests (
    id INTEGER PRIMARY KEY, item_id INTEGER, requested_quantity REAL,
    reason TEXT, status TEXT DEFAULT '提交', rolled_back INTEGER DEFAULT 0,
    created_at TEXT);
CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT, unit TEXT DEFAULT '件',
    note TEXT, created_at TEXT);
CREATE TABLE production_runs (
    id INTEGER PRIMARY KEY, product_id INTEGER, output_qty REAL,
    note TEXT, rolled_back INTEGER DEFAULT 0, created_by TEXT, created_at TEXT);
CREATE TABLE production_run_items (
    id INTEGER PRIMARY KEY, run_id INTEGER, item_id INTEGER,
    planned_qty REAL, actual_qty REAL);
CREATE TABLE stocktakes (
    id INTEGER PRIMARY KEY, item_id INTEGER, previous_quantity REAL,
    actual_quantity REAL, diff REAL, batch_id INTEGER, created_at TEXT, note TEXT);
"""


@pytest.fixture
def pdb():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    yield conn
    conn.close()


def add_item(pdb, name="椰浆", qty=0.0, cost=10.0, safety=0.0, active=1,
             category="原料", cat_id=1):
    pdb.execute("INSERT OR IGNORE INTO categories VALUES (?, ?)", (cat_id, category))
    cur = pdb.execute(
        "INSERT INTO items (name, quantity, safety_stock, unit_cost, is_active,"
        " category_id) VALUES (?, ?, ?, ?, ?, ?)",
        (name, qty, safety, cost, active, cat_id))
    pdb.commit()
    return cur.lastrowid


def add_mv(pdb, item_id, action, delta, days_ago):
    pdb.execute(
        "INSERT INTO stock_movements (item_id, action, delta, created_at)"
        " VALUES (?, ?, ?, ?)", (item_id, action, delta, ts(days_ago)))
    pdb.commit()


# ---------------------------------------------------------------------------
# FIFO 重放
# ---------------------------------------------------------------------------

class TestReplay:
    def test_inbound_push_consume_fifo(self, pdb):
        """压层/消层：老层先出，账龄>7 天的部分计呆滞。"""
        iid = add_item(pdb, qty=15.0)
        add_mv(pdb, iid, "补货入库", 10, 20)   # 老层 10（呆滞）
        add_mv(pdb, iid, "补货入库", 10, 2)    # 新层 10
        add_mv(pdb, iid, "出库", -5, 1)        # 消老层 5
        r = hp.replay_item_layers(
            pdb.execute("SELECT created_at, action, delta FROM stock_movements"
                        " ORDER BY created_at, id").fetchall(),
            15.0, NOW)
        assert r["ok"] is True
        assert r["stale_qty"] == pytest.approx(5)
        assert r["oldest_date"] == ts(20)[:10]

    def test_consume_oldest_layer_first(self, pdb):
        """先进先出：消耗优先吃最老层。"""
        iid = add_item(pdb, qty=10.0)
        add_mv(pdb, iid, "补货入库", 10, 20)
        add_mv(pdb, iid, "补货入库", 10, 2)
        add_mv(pdb, iid, "出库", -12, 1)  # 吃光老层 10 + 新层 2
        r = hp.replay_item_layers(
            pdb.execute("SELECT created_at, action, delta FROM stock_movements"
                        " ORDER BY created_at, id").fetchall(),
            8.0, NOW)
        assert r["ok"] is True
        assert r["stale_qty"] == pytest.approx(0)

    def test_stocktake_proportional_scale(self, pdb):
        """盘点修正：强设数量型事件按比例缩放层。"""
        iid = add_item(pdb, qty=10.0)
        add_mv(pdb, iid, "补货入库", 10, 20)
        add_mv(pdb, iid, "出库", -8, 10)      # 剩 2（老）
        add_mv(pdb, iid, "盘点修正", 8, 5)    # 调到 10，层全部等比放大
        r = hp.replay_item_layers(
            pdb.execute("SELECT created_at, action, delta FROM stock_movements"
                        " ORDER BY created_at, id").fetchall(),
            10.0, NOW)
        assert r["ok"] is True
        assert r["stale_qty"] == pytest.approx(10.0)

    def test_age_boundary_day7_not_stale(self, pdb):
        """账龄边界：恰为第 7 天入库的层不算呆滞（>7 天才呆滞）。"""
        iid = add_item(pdb, qty=10.0)
        add_mv(pdb, iid, "补货入库", 10, 7)
        r = hp.replay_item_layers(
            pdb.execute("SELECT created_at, action, delta FROM stock_movements").fetchall(),
            10.0, NOW)
        assert r["ok"] is True
        assert r["stale_qty"] == pytest.approx(0)

    def test_mismatch_returns_not_ok(self, pdb):
        """重放终值 ≠ items.quantity → ok=False，调用方走回退。"""
        add_item(pdb, qty=999.0)  # 无任何流水也数量对不上
        r = hp.replay_item_layers([], 999.0, NOW)
        assert r["ok"] is False
        assert r["stale_qty"] is None


class TestStaleAndDos:
    def test_mismatch_fallback_zero_consume_full_stale(self, pdb):
        """回退：重放失配 + 近 7 天零消耗 → 全额呆滞。"""
        iid = add_item(pdb, qty=999.0)
        add_mv(pdb, iid, "出库", -3, 30)  # 有流水但与 quantity 对不上
        result = hp.compute_stale_and_dos(pdb, NOW)
        assert result[iid]["tier"] == "stale"
        assert result[iid]["stale_qty"] == pytest.approx(999.0)

    def test_mismatch_fallback_recent_consume_no_stale(self, pdb):
        """回退：重放失配 + 近 7 天有消耗 → 不计呆滞（可能转入超储档）。"""
        iid = add_item(pdb, qty=999.0)
        add_mv(pdb, iid, "出库", -3, 1)
        result = hp.compute_stale_and_dos(pdb, NOW)
        assert result[iid]["stale_qty"] == pytest.approx(0)
        assert result[iid]["tier"] == "overstock"  # 库存巨大 + 近期有消耗 → 黄档

    def test_dos_boundary_equal_7_not_overstock(self, pdb):
        """DOS 边界：=7 天不算超储（>7 才算）。"""
        # 消耗 300/30 天 → d=10，qty=70 → DOS=7.0
        iid = add_item(pdb, qty=70.0)
        add_mv(pdb, iid, "补货入库", 300, 29)
        add_mv(pdb, iid, "出库", -300, 1)
        result = hp.compute_stale_and_dos(pdb, NOW)
        assert result[iid]["dos"] == pytest.approx(7.0)
        assert result[iid]["tier"] == "normal"

    def test_overstock_requires_recent_consume(self, pdb):
        """超储必须近 7 天有消耗；DOS 大但消耗都在 7 天外 → 不标超储。"""
        iid = add_item(pdb, qty=10.0)
        add_mv(pdb, iid, "补货入库", 20, 29)
        add_mv(pdb, iid, "出库", -20, 10)   # 消耗在 7 天外（d=20/30）
        add_mv(pdb, iid, "补货入库", 10, 6)  # 补回 10，层新鲜（6 天）
        result = hp.compute_stale_and_dos(pdb, NOW)
        assert result[iid]["dos"] == pytest.approx(15.0)  # 10 ÷ (20/30)
        assert result[iid]["tier"] == "normal"

    def test_overstock_positive(self, pdb):
        iid = add_item(pdb, qty=70.0)
        add_mv(pdb, iid, "补货入库", 300, 29)
        add_mv(pdb, iid, "出库", -30, 1)  # d=1 → DOS=70
        result = hp.compute_stale_and_dos(pdb, NOW)
        assert result[iid]["tier"] == "overstock"

    def test_stale_priority_over_overstock(self, pdb):
        """红呆滞优先于黄超储。"""
        iid = add_item(pdb, qty=50.0)
        add_mv(pdb, iid, "补货入库", 100, 30)
        add_mv(pdb, iid, "出库", -50, 1)  # 层 50@30天前 → 呆滞；同时 DOS 也大
        result = hp.compute_stale_and_dos(pdb, NOW)
        assert result[iid]["tier"] == "stale"

    def test_inactive_item_skipped(self, pdb):
        iid = add_item(pdb, qty=50.0, active=0)
        result = hp.compute_stale_and_dos(pdb, NOW)
        assert iid not in result


# ---------------------------------------------------------------------------
# ABC / 超耗 / 排序白名单
# ---------------------------------------------------------------------------

class TestAbc:
    def test_cumulative_boundary_strict(self, pdb):
        """累计 80/95% 边界：恰在边界上的归上一档。"""
        iid_a = add_item(pdb, "A", cat_id=1)
        iid_b = add_item(pdb, "B", cat_id=1)
        iid_c = add_item(pdb, "C", cat_id=1)
        # 消耗金额 = 数量 × unit_cost(10)
        for iid, qty, day in ((iid_a, 8.0, 3), (iid_b, 1.5, 3), (iid_c, 0.5, 3)):
            pdb.execute(
                "INSERT INTO outbound_requests (item_id, requested_quantity,"
                " created_at) VALUES (?, ?, ?)", (iid, qty, ts(day)))
        pdb.commit()
        abc = hp.compute_abc(pdb, "1970-01-01 00:00:00")
        assert abc[iid_a]["cls"] == "A"      # 累计 80%
        assert abc[iid_b]["cls"] == "B"      # 累计 95%
        assert abc[iid_c]["cls"] == "C"

    def test_production_consumption_counts(self, pdb):
        """生产消耗计入 ABC 消耗口径。"""
        iid = add_item(pdb, "X", cat_id=1)
        pdb.execute("INSERT INTO products VALUES (1, '蛋糕', '个', '', '')")
        pdb.execute("INSERT INTO production_runs (product_id, output_qty, created_at)"
                    " VALUES (1, 10, ?)", (ts(2),))
        pdb.execute("INSERT INTO production_run_items (run_id, item_id, planned_qty,"
                    " actual_qty) VALUES (1, ?, 5, 5)", (iid,))
        pdb.commit()
        abc = hp.compute_abc(pdb, "1970-01-01 00:00:00")
        assert abc[iid]["consumed_value"] == pytest.approx(50.0)


class TestOveruse:
    def test_planned_zero_full_overuse(self, pdb):
        assert hp._overuse_pct(0, 5) == 100.0

    def test_boundary_10pct_not_overuse(self):
        """阈值边界：=10% 不标红（严格大于）。"""
        assert hp._overuse_pct(100, 110) == pytest.approx(10.0)
        rows = self._run_with((100, 110))
        assert rows == []

    def test_over_10pct_flagged(self):
        rows = self._run_with((100, 120))
        assert len(rows) == 1
        assert rows[0]["pct"] == pytest.approx(20.0)

    @staticmethod
    def _run_with(planned_actual):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        conn.execute("INSERT INTO categories VALUES (1, '原料')")
        conn.execute("INSERT INTO items (id, name, unit_cost, category_id, is_active)"
                     " VALUES (1, '面粉', 1.0, 1, 1)")
        conn.execute("INSERT INTO products VALUES (1, '面包', '个', '', '')")
        conn.execute("INSERT INTO production_runs (id, product_id, output_qty,"
                     " created_at) VALUES (1, 1, 10, ?)", (ts(2),))
        planned, actual = planned_actual
        conn.execute("INSERT INTO production_run_items (run_id, item_id,"
                     " planned_qty, actual_qty) VALUES (1, 1, ?, ?)",
                     (planned, actual))
        conn.commit()
        result = hp.compute_production(conn, "1970-01-01 00:00:00")
        conn.close()
        return result["overuse_rows"]


class TestSkuTableSort:
    def test_whitelist_and_fallback(self, pdb):
        iid1 = add_item(pdb, "贵", qty=10.0, cost=100.0)   # 金额 1000
        iid2 = add_item(pdb, "便宜", qty=10.0, cost=1.0)   # 金额 10
        rows = hp.build_sku_table(pdb, {}, {}, "1970-01-01 00:00:00", "bogus")
        assert rows[0]["name"] == "贵"       # 非法 sort 回退 stock_value 降序
        stale_dos = {iid2: {"stale_qty": 5.0, "stale_value": 5.0,
                            "oldest_date": None, "dos": None,
                            "last_outbound": None, "last_inbound": None},
                     iid1: {"stale_qty": 0.0, "stale_value": 0.0,
                            "oldest_date": None, "dos": None,
                            "last_outbound": None, "last_inbound": None}}
        rows = hp.build_sku_table(pdb, stale_dos, {}, "1970-01-01 00:00:00", "stale")
        assert rows[0]["name"] == "便宜"     # 按呆滞量降序

    def test_none_dos_sorted_last(self, pdb):
        add_item(pdb, "有DOS", qty=1.0, cost=1.0)
        add_item(pdb, "无DOS", qty=1.0, cost=2.0)
        stale_dos = {1: {"stale_qty": 0, "stale_value": 0, "oldest_date": None,
                         "dos": 3.0, "last_outbound": None, "last_inbound": None},
                     2: {"stale_qty": 0, "stale_value": 0, "oldest_date": None,
                         "dos": None, "last_outbound": None, "last_inbound": None}}
        rows = hp.build_sku_table(pdb, stale_dos, {}, "1970-01-01 00:00:00", "dos")
        assert rows[0]["name"] == "有DOS"
        assert rows[1]["dos"] is None


class TestLevelsAndOthers:
    def test_levels_boundaries(self, pdb):
        add_item(pdb, "断货", qty=0.0, safety=5.0, cost=1.0)
        add_item(pdb, "低安全", qty=3.0, safety=5.0, cost=2.0, cat_id=2)
        add_item(pdb, "正常", qty=10.0, safety=5.0, cost=3.0, cat_id=2)
        levels = hp.compute_inventory_levels(pdb)
        assert len(levels["out_of_stock"]) == 1
        assert len(levels["below_safety"]) == 1
        assert levels["active_skus"] == 3
        assert levels["stock_value"] == pytest.approx(0 + 6 + 30)

    def test_restock_health(self, pdb):
        iid = add_item(pdb)
        for status, day in (("提交", 1), ("提交", 2), ("入库", 3), ("入库", 40)):
            pdb.execute("INSERT INTO restock_requests (item_id, requested_quantity,"
                        " status, created_at) VALUES (?, 1, ?, ?)",
                        (iid, status, ts(day)))
        pdb.commit()
        h = hp.compute_restock_health(pdb, ts(30))
        assert h["pending"] == 2          # 全量快照
        assert h["stocked_in"] == 1       # 窗口内已入库
        assert h["window_count"] == 3     # 窗口内总单数

    def test_fulfillment_cycle(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE store_orders (
                id INTEGER PRIMARY KEY, status TEXT, created_at TEXT,
                delivered_at TEXT, dc_warehouse_code TEXT,
                store_warehouse_code TEXT);
        """)
        conn.execute("INSERT INTO store_orders (status, created_at, delivered_at,"
                     " dc_warehouse_code) VALUES ('pending', ?, NULL, 'wh_x')",
                     (ts(1),))
        conn.execute("INSERT INTO store_orders (status, created_at, delivered_at,"
                     " dc_warehouse_code) VALUES ('shipped', ?, NULL, 'wh_x')",
                     (ts(1),))
        conn.execute("INSERT INTO store_orders (status, created_at, delivered_at,"
                     " dc_warehouse_code) VALUES ('delivered', ?, ?, 'wh_x')",
                     (ts(4), ts(2)))
        conn.commit()
        f = hp.compute_fulfillment(conn, "wh_x", NOW)
        assert f["backlog"] == 1
        assert f["in_transit"] == 1
        assert f["avg_cycle_days"] == 2.0
        conn.close()

    def test_fulfillment_none_code(self):
        f = hp.compute_fulfillment(sqlite3.connect(":memory:"), None, NOW)
        assert f == {"backlog": None, "in_transit": None, "avg_cycle_days": None}

    def test_trend_zero_fill(self, pdb):
        iid = add_item(pdb, cost=1.0)
        pdb.execute("INSERT INTO outbound_requests (item_id, requested_quantity,"
                    " created_at) VALUES (?, 2, ?)", (iid, ts(0, 9)))
        pdb.commit()
        trend = hp.compute_trend(pdb, NOW, points=5)
        assert len(trend) == 5
        assert trend[-1]["value"] == pytest.approx(2.0)   # 今天
        assert trend[0]["value"] == 0.0                    # 缺失日补 0

    def test_stocktake_quality(self, pdb):
        iid1 = add_item(pdb)
        iid2 = add_item(pdb, cat_id=2)
        pdb.execute("INSERT INTO stocktakes (item_id, previous_quantity,"
                    " actual_quantity, diff, created_at) VALUES (?, 10, 12, 2, ?)",
                    (iid1, ts(5)))
        pdb.execute("INSERT INTO stocktakes (item_id, previous_quantity,"
                    " actual_quantity, diff, created_at) VALUES (?, 10, 10, 0, ?)",
                    (iid2, ts(5)))
        pdb.commit()
        q = hp.compute_stocktake_quality(pdb, NOW)
        assert q["diff_rate"] == 50.0
        assert q["unstale_count"] == 0        # 5 天前盘过
        assert q["last_stocktake_days"] == 5

    def test_production_none_when_no_runs(self, pdb):
        assert hp.compute_production(pdb, "1970-01-01 00:00:00") is None


# ---------------------------------------------------------------------------
# 路由与导航
# ---------------------------------------------------------------------------

class TestRoutes:
    def test_staff_forbidden(self, staff_client):
        client, _ = staff_client
        resp = client.get("/report/health")
        assert resp.status_code == 403

    def test_manager_ok(self, logged_client):
        client, _ = logged_client  # admin（平台管理员 bypass manager 门）
        resp = client.get("/report/health")
        assert resp.status_code == 200
        assert "库存健康".encode() in resp.data

    def test_manager_role_via_factory(self, nav_client_factory):
        client, _ = nav_client_factory("storefront", "manager")
        resp = client.get("/report/health")
        assert resp.status_code == 200

    def test_range_whitelist_fallback(self, logged_client):
        client, _ = logged_client
        resp = client.get("/report/health?range=<script>")
        assert resp.status_code == 200
        resp = client.get("/report/health?range=30d")
        assert resp.status_code == 200

    def test_csv_export_bom_and_header(self, logged_client):
        client, _ = logged_client
        resp = client.get("/report/health/export")
        assert resp.status_code == 200
        assert resp.headers["Content-Type"].startswith("text/csv")
        assert resp.data[:3] == b"\xef\xbb\xbf"
        assert "库存健康报表".encode() in resp.data

    def test_csv_staff_forbidden(self, staff_client):
        client, _ = staff_client
        assert client.get("/report/health/export").status_code == 403


class TestNavCarriers:
    def _sidebar(self, body):
        start = body.index('<aside class="sidebar')
        end = body.index("</aside>", start)
        return body[start:end]

    def _land_grid(self, body):
        start = body.index('<div class="land-grid">')
        end = body.index("</main>", start)
        return body[start:end]

    def test_manager_sees_link_in_both_carriers(self, nav_client_factory):
        client, _ = nav_client_factory("storefront", "manager")
        sidebar = self._sidebar(client.get("/dashboard").get_data(as_text=True))
        land = self._land_grid(client.get("/land").get_data(as_text=True))
        assert "/report/health" in sidebar
        assert "/report/health" in land

    def test_staff_sees_link_nowhere(self, nav_client_factory):
        client, _ = nav_client_factory("storefront", "staff")
        sidebar = self._sidebar(client.get("/dashboard").get_data(as_text=True))
        land = self._land_grid(client.get("/land").get_data(as_text=True))
        assert "/report/health" not in sidebar
        assert "/report/health" not in land

    def test_carrier_sets_stay_bidirectional(self, nav_client_factory):
        """铁律回归：manager 视角侧栏链接集 == /land 链接集。"""
        import re as _re
        client, _ = nav_client_factory("storefront", "manager")
        sidebar = self._sidebar(client.get("/dashboard").get_data(as_text=True))
        land = self._land_grid(client.get("/land").get_data(as_text=True))
        sidebar_links = set(_re.findall(r'href="([^"]+)"', sidebar))
        land_links = set(_re.findall(r'href="([^"]+)"', land))
        assert sidebar_links == land_links


# ---------------------------------------------------------------------------
# Issue #38：时间戳容错（生产存量含无秒形态）
# ---------------------------------------------------------------------------
# 证据（2026-10-15 全 6 仓 + master 普查）：只有 wh_000.db 的
# stock_movements.created_at 与 restock_requests.created_at 各 54 行是
# 16 字符无秒形态「YYYY-MM-DD HH:MM」，跨度 2025-11-28 → 2026-10-13（仍在产生，
# 应用层全部走 _helpers.now() 的 19 字符格式，故源头是人工 / ops SQL）。
# 严格 "%Y-%m-%d %H:%M:%S" 撞上它 → ValueError → /report/health 500。
SHORT_TS = "%Y-%m-%d %H:%M"


def ts_short(days_ago: int, hour: int = 8) -> str:
    """生产存量形态：无秒（16 字符）。"""
    return (NOW - timedelta(days=days_ago)).replace(hour=hour).strftime(SHORT_TS)


def add_mv_raw(pdb, item_id, action, delta, raw_ts):
    """按原始字符串写流水（add_mv 只产带秒形态，够不到本 issue 的边界）。"""
    pdb.execute(
        "INSERT INTO stock_movements (item_id, action, delta, created_at)"
        " VALUES (?, ?, ?, ?)", (item_id, action, delta, raw_ts))
    pdb.commit()


class TestTimestampTolerance:
    def test_parse_event_ts_accepts_both_production_forms(self):
        """带秒（19 字符）与无秒（16 字符）解析为同一时刻。"""
        assert hp.parse_event_ts("2026-08-12 14:00:00") == datetime(2026, 8, 12, 14, 0)
        assert hp.parse_event_ts("2026-08-12 14:00") == datetime(2026, 8, 12, 14, 0)

    def test_parse_event_ts_strips_and_handles_precision(self):
        """首尾空白 / 微秒 / 纯日期都兼容，且不引入 aware datetime。"""
        assert hp.parse_event_ts("  2026-08-12 14:00  ") == datetime(2026, 8, 12, 14, 0)
        assert hp.parse_event_ts("2026-08-12 14:00:00.123") == \
            datetime(2026, 8, 12, 14, 0, 0, 123000)
        assert hp.parse_event_ts("2026-08-12") == datetime(2026, 8, 12)
        assert hp.parse_event_ts("2026-08-12 14:00").tzinfo is None

    @pytest.mark.parametrize(
        "bad", [None, "", "   ", "not-a-ts", 12345, "2026/08/12 14:00"])
    def test_parse_event_ts_raises_with_raw_value(self, bad):
        """未知形态抛 ValueError（不吞异常），消息带原始值便于 prod 定位。"""
        with pytest.raises(ValueError) as ei:
            hp.parse_event_ts(bad)
        assert repr(bad) in str(ei.value)

    def test_replay_handles_short_timestamp(self, pdb):
        """Issue #38 复现用例：无秒流水不再抛 ValueError，且账龄正确。"""
        iid = add_item(pdb, qty=15.0)
        add_mv_raw(pdb, iid, "补货入库", 10, ts_short(20))  # 老层（呆滞）
        add_mv_raw(pdb, iid, "补货入库", 10, ts_short(2))   # 新层
        add_mv_raw(pdb, iid, "出库", -5, ts_short(1))       # 消老层 5
        rows = pdb.execute(
            "SELECT created_at, action, delta FROM stock_movements"
            " ORDER BY created_at, id").fetchall()
        r = hp.replay_item_layers(rows, 15.0, NOW)
        assert r["ok"] is True
        assert r["stale_qty"] == pytest.approx(5)
        assert r["oldest_date"] == ts_short(20)[:10]

    def test_replay_mixed_width_equivalent_to_all_long(self):
        """同一批事件：无秒形态与带秒形态的重放结果必须逐字段一致。

        反向保证：若 helper 把无秒形态误解析成别的时刻（例如兜底成固定日期 /
        当日零点），层账龄会错位、呆滞量对不上 → 本用例失败。
        """
        long_mv = [("2026-09-25 08:00:00", "补货入库", 10.0),
                   ("2026-10-13 08:00:00", "补货入库", 10.0),
                   ("2026-10-14 08:00:00", "出库", -5.0)]
        short_mv = [(t[:16], a, d) for t, a, d in long_mv]
        assert hp.replay_item_layers(short_mv, 15.0, NOW) == \
            hp.replay_item_layers(long_mv, 15.0, NOW)

    def test_replay_propagates_unknown_timestamp(self, pdb):
        """未知形态必须冒泡 ValueError（原始值带上），不许静默跳过该事件。

        这是「宁可 500 也不要静默算错」的契约：跳过一条事件会让层账龄整体
        错位，呆滞金额错得无声无息。变异验证：把 replay 里的解析改成
        `except ValueError: continue` → 本用例变红。
        """
        with pytest.raises(ValueError) as ei:
            hp.replay_item_layers([("not-a-ts", "补货入库", 10.0)], 10.0, NOW)
        assert "not-a-ts" in str(ei.value)

    def test_compute_stale_and_dos_short_timestamps(self, pdb):
        """整条链路不再 500，且分档 / 金额正确。"""
        iid = add_item(pdb, qty=50.0, cost=2.0)
        add_mv_raw(pdb, iid, "补货入库", 100, ts_short(30))
        add_mv_raw(pdb, iid, "出库", -50, ts_short(1))
        result = hp.compute_stale_and_dos(pdb, NOW)
        assert result[iid]["tier"] == "stale"
        assert result[iid]["stale_qty"] == pytest.approx(50.0)
        assert result[iid]["stale_value"] == pytest.approx(100.0)

    def test_fulfillment_logs_warning_on_unknown_timestamp(self, caplog):
        """丢样本必须留 WARNING（不静默）——变异：把 except 改成裸 pass 即变红。"""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE store_orders (
                id INTEGER PRIMARY KEY, status TEXT, created_at TEXT,
                delivered_at TEXT, dc_warehouse_code TEXT,
                store_warehouse_code TEXT);
        """)
        conn.execute("INSERT INTO store_orders (status, created_at, delivered_at,"
                     " dc_warehouse_code) VALUES ('delivered', ?, ?, 'wh_x')",
                     ("not-a-ts", "2026-10-03 08:00:00"))
        conn.commit()
        with caplog.at_level(logging.WARNING, logger="reports_health_pure"):
            f = hp.compute_fulfillment(conn, "wh_x", NOW)
        conn.close()
        assert f["avg_cycle_days"] is None      # 唯一样本被丢 → 无均值
        assert any("履约周期样本跳过" in r.getMessage() for r in caplog.records), \
            "丢样本必须在 WARNING 里留痕（静默丢数据不可接受）"

    def test_stocktake_quality_short_timestamp(self, pdb):
        """同缺陷类另一站点：stocktakes.created_at 无秒也不再抛错。"""
        iid = add_item(pdb)
        pdb.execute("INSERT INTO stocktakes (item_id, previous_quantity,"
                    " actual_quantity, diff, created_at) VALUES (?, 10, 12, 2, ?)",
                    (iid, ts_short(5)))
        pdb.commit()
        q = hp.compute_stocktake_quality(pdb, NOW)
        assert q["last_stocktake_days"] == 5
        assert q["diff_rate"] == 100.0

    def test_fulfillment_mixed_width_counts_both(self):
        """带秒与无秒的订单都要计入均值。

        旧实现（try/except 吞 ValueError）会把无秒行静默丢弃 → 均值失真。
        构造 2 天（带秒）+ 10 天（无秒）两单，正确均值 = 6.0，丢一单则 = 2.0。
        """
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE store_orders (
                id INTEGER PRIMARY KEY, status TEXT, created_at TEXT,
                delivered_at TEXT, dc_warehouse_code TEXT,
                store_warehouse_code TEXT);
        """)
        conn.execute("INSERT INTO store_orders (status, created_at, delivered_at,"
                     " dc_warehouse_code) VALUES ('delivered', ?, ?, 'wh_x')",
                     ("2026-10-01 08:00:00", "2026-10-03 08:00:00"))
        conn.execute("INSERT INTO store_orders (status, created_at, delivered_at,"
                     " dc_warehouse_code) VALUES ('delivered', ?, ?, 'wh_x')",
                     ("2026-10-01 08:00", "2026-10-11 08:00"))
        conn.commit()
        f = hp.compute_fulfillment(conn, "wh_x", NOW)
        conn.close()
        assert f["avg_cycle_days"] == pytest.approx(6.0)


class TestHealthRouteTimestampRegression:
    """端到端回归：库里真有无秒流水时 /report/health 仍须 200（Issue #38 现场）。"""

    def _seed_short_ts_rows(self, wh_path):
        conn = sqlite3.connect(wh_path)
        cat_id = conn.execute(
            "SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()[0]
        iid = conn.execute(
            "INSERT INTO items (sku, name, category_id, quantity, safety_stock,"
            " unit_cost, unit, updated_at) VALUES ('T-SHORT', '无秒流水品', ?,"
            " 5, 0, 10, '件', '2026-10-15 08:00:00')", (cat_id,)).lastrowid
        for action, delta, raw in (("补货入库", 10.0, "2026-08-12 14:00"),
                                   ("出库", -5.0, "2026-10-14 09:00")):
            conn.execute(
                "INSERT INTO stock_movements (item_id, action, delta, created_at)"
                " VALUES (?, ?, ?, ?)", (iid, action, delta, raw))
        conn.commit()
        conn.close()
        return iid

    def test_health_route_returns_200(self, logged_client):
        client, wh_path = logged_client
        self._seed_short_ts_rows(wh_path)
        resp = client.get("/report/health")
        assert resp.status_code == 200
        assert "库存健康".encode() in resp.data

    def test_health_export_returns_200(self, logged_client):
        """CSV 导出与页面共用 _build_health_context，同样不能 500。"""
        client, wh_path = logged_client
        self._seed_short_ts_rows(wh_path)
        resp = client.get("/report/health/export")
        assert resp.status_code == 200
        assert resp.headers["Content-Type"].startswith("text/csv")
