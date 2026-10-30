"""多仓履约：订货报表粒度（D11）与健康报表参与仓（spec §10 / Task 9）。

锁定三件事：

1. 明细数量按**行来源仓**分组，同一订单在多个参与仓各出现一次；
2. 订单数 = 去重订单集（全局一次、参与仓维度各计一次），不因多行翻倍；
3. 运费是**头级**事实：全局只计一次，仓维度显示全单值，禁止把仓维度相加
   当成全局（D11 / spec §10）。

健康报表（`reports_health_pure.compute_fulfillment`）在非主仓也要看到跨仓单，
用 EXISTS 谓词避免 JOIN 明细把订单数放大。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

import reports_health_pure as hp
from blueprints import store_ordering_pure as sop
from db import init_master_db, init_warehouse_db

NOW = datetime(2026, 10, 29, 12, 0, 0)
TS = NOW.strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture
def report_env(tmp_path, monkeypatch):
    import config as config_module
    import db as db_module

    master_path = tmp_path / "master.db"
    wh_dir = tmp_path / "warehouses"
    wh_dir.mkdir()
    paths = {code: wh_dir / f"{code}.db" for code in ("dc_alpha", "dc_beta", "store_one")}

    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_dir)
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", wh_dir)

    init_master_db()
    for path in paths.values():
        init_warehouse_db(path)

    master = sqlite3.connect(str(master_path))
    master.row_factory = sqlite3.Row
    master.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (1, 'admin', 'x', 1, ?)", (TS,))
    for wid, code, kind in (
        (1, "dc_alpha", "distribution_center"),
        (2, "dc_beta", "distribution_center"),
        (3, "store_one", "storefront"),
    ):
        master.execute(
            "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (wid, code, code, str(paths[code]), kind, TS))
    for code, name in (("PACKAGING", "包材"), ("SAUCE", "调味酱")):
        master.execute(
            "INSERT INTO canonical_categories (code, name, description, created_at, updated_at) "
            "VALUES (?, ?, '', ?, ?)", (code, name, TS, TS))
    for cid, name, category in (
        (201, "跨仓品A", "PACKAGING"),
        (202, "跨仓品B", "SAUCE"),
    ):
        master.execute(
            "INSERT INTO canonical_items (id, canonical_sku, name, category_code, unit, "
            "gram_per_unit, aux_unit, aux_rate, status, created_from, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, '件', 0, NULL, 0, 'active', 'rd_manual', ?, ?)",
            (cid, f"IC-{cid:06d}", name, category, TS, TS))
    master.commit()
    yield {"master": master, "master_path": master_path}
    master.close()


def _order(
    master: sqlite3.Connection,
    *,
    order_no: str,
    header_dc: str,
    fee: float,
    status: str = "approved",
    created_at: str = TS,
    delivered_at: str | None = None,
) -> int:
    cur = master.execute(
        """INSERT INTO store_orders (
               order_no, store_warehouse_code, dc_warehouse_code, status, requested_by,
               created_at, updated_at, shipping_fee, delivered_at)
           VALUES (?, 'store_one', ?, ?, 1, ?, ?, ?, ?)""",
        (order_no, header_dc, status, created_at, created_at, fee, delivered_at))
    return int(cur.lastrowid)


def _line(
    master: sqlite3.Connection,
    order_id: int,
    canonical_id: int,
    *,
    source: str | None,
    qty: float,
    shipped: float = 0.0,
    fulfilled: float = 0.0,
) -> None:
    master.execute(
        """INSERT INTO store_order_items (
               order_id, canonical_id, source_warehouse_code, quantity,
               shipped_quantity, fulfilled_quantity, unit, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, '件', 'pending', ?)""",
        (order_id, canonical_id, source, qty, shipped, fulfilled, TS))


# ---------------------------------------------------------------------------
# 订货报表粒度（D11 / spec §10）
# ---------------------------------------------------------------------------


def test_report_quantities_follow_line_source_warehouse(report_env):
    """同单 A 两行 + B 一行 + 头级运费 10：数量按来源仓分组，订单只算一次。"""
    master = report_env["master"]
    oid = _order(master, order_no="SO-1", header_dc="dc_alpha", fee=10.0)
    _line(master, oid, 201, source="dc_alpha", qty=3.0, shipped=3.0, fulfilled=2.0)
    _line(master, oid, 201, source="dc_beta", qty=2.0, shipped=2.0, fulfilled=2.0)
    _line(master, oid, 202, source="dc_alpha", qty=1.0, shipped=1.0, fulfilled=0.0)
    master.commit()

    report = sop.compute_store_order_report(master)
    summary = report["summary"]
    assert summary["ordered_qty"] == 6.0
    assert summary["order_count"] == 1  # 三行同属一单
    assert summary["shipping_fee"] == 10.0  # 头级值只计一次

    by_dc = {row["dc_code"]: row for row in report["by_dc"]}
    assert set(by_dc) == {"dc_alpha", "dc_beta"}
    assert by_dc["dc_alpha"]["ordered_qty"] == 4.0
    assert by_dc["dc_beta"]["ordered_qty"] == 2.0
    # 同仓两行只让订单数算一次
    assert by_dc["dc_alpha"]["order_count"] == 1
    assert by_dc["dc_beta"]["order_count"] == 1
    # D11：仓维度显示全单运费值，不是拆分后的份额
    assert by_dc["dc_alpha"]["shipping_fee"] == 10.0
    assert by_dc["dc_beta"]["shipping_fee"] == 10.0
    # 全局值不得等于「仓维度求和」（那正是被禁止的跨仓求和）
    assert sum(row["shipping_fee"] for row in report["by_dc"]) == 20.0
    assert summary["shipping_fee"] == 10.0

    assert len(report["by_store"]) == 1
    assert report["by_store"][0]["ordered_qty"] == 6.0
    assert report["by_store"][0]["order_count"] == 1
    assert report["by_store"][0]["shipping_fee"] == 10.0


def test_report_order_count_is_distinct_order_set(report_env):
    """两单（各自跨仓）→ 全局订单数 2；参与仓维度各计与自己相关的单。"""
    master = report_env["master"]
    first = _order(master, order_no="SO-1", header_dc="dc_alpha", fee=10.0)
    _line(master, first, 201, source="dc_alpha", qty=3.0)
    _line(master, first, 201, source="dc_beta", qty=2.0)
    second = _order(master, order_no="SO-2", header_dc="dc_beta", fee=5.0)
    _line(master, second, 202, source="dc_beta", qty=4.0)
    master.commit()

    report = sop.compute_store_order_report(master)
    assert report["summary"]["order_count"] == 2
    assert report["summary"]["shipping_fee"] == 15.0

    by_dc = {row["dc_code"]: row for row in report["by_dc"]}
    assert by_dc["dc_alpha"]["order_count"] == 1
    assert by_dc["dc_beta"]["order_count"] == 2
    # 仓维度运费是各自相关订单的全单值，仍不可相加
    assert by_dc["dc_alpha"]["shipping_fee"] == 10.0
    assert by_dc["dc_beta"]["shipping_fee"] == 15.0


def test_report_filters_agree_across_dimensions(report_env):
    """仓 / 品类 / 日期组合过滤在明细集合与订单集合上口径一致。"""
    master = report_env["master"]
    oid = _order(master, order_no="SO-1", header_dc="dc_alpha", fee=10.0)
    _line(master, oid, 201, source="dc_alpha", qty=3.0, shipped=3.0, fulfilled=1.0)
    _line(master, oid, 201, source="dc_beta", qty=2.0, shipped=2.0, fulfilled=2.0)
    _line(master, oid, 202, source="dc_alpha", qty=1.0, shipped=1.0, fulfilled=0.0)
    master.commit()

    by_beta = sop.compute_store_order_report(master, dc_warehouse_code="dc_beta")
    assert by_beta["summary"]["ordered_qty"] == 2.0
    assert by_beta["summary"]["order_count"] == 1
    assert [row["dc_code"] for row in by_beta["by_dc"]] == ["dc_beta"]

    combo = sop.compute_store_order_report(
        master, dc_warehouse_code="dc_alpha", category_code="PACKAGING")
    assert combo["summary"]["ordered_qty"] == 3.0
    assert combo["summary"]["order_count"] == 1
    assert combo["summary"]["shipping_fee"] == 10.0

    sauce = sop.compute_store_order_report(master, category_code="SAUCE")
    assert sauce["summary"]["ordered_qty"] == 1.0
    assert [row["dc_code"] for row in sauce["by_dc"]] == ["dc_alpha"]

    today = NOW.strftime("%Y-%m-%d")
    yesterday = (NOW - timedelta(days=1)).strftime("%Y-%m-%d")
    assert sop.compute_store_order_report(
        master, start_date=today, end_date=today)["summary"]["ordered_qty"] == 6.0
    assert sop.compute_store_order_report(
        master, start_date=yesterday, end_date=yesterday)["summary"]["ordered_qty"] == 0.0


def test_null_source_falls_back_to_header_warehouse(report_env):
    """历史 NULL 来源行按头仓归属（迁移前数据与开关关闭时恒等）。"""
    master = report_env["master"]
    oid = _order(master, order_no="SO-LEGACY", header_dc="dc_alpha", fee=3.0)
    _line(master, oid, 201, source=None, qty=5.0, shipped=5.0, fulfilled=5.0)
    master.commit()

    report = sop.compute_store_order_report(master)
    assert report["summary"]["ordered_qty"] == 5.0
    assert report["summary"]["order_count"] == 1
    assert report["summary"]["shipping_fee"] == 3.0
    assert [(row["dc_code"], row["ordered_qty"]) for row in report["by_dc"]] == [
        ("dc_alpha", 5.0)]


# ---------------------------------------------------------------------------
# 健康报表：积压 / 在途 / 履约周期按参与仓
# ---------------------------------------------------------------------------


def test_health_fulfillment_counts_participating_warehouse(report_env):
    """跨仓单在非主仓不再漏算；同一订单多行不把单数放大。"""
    master = report_env["master"]
    approved = _order(master, order_no="SO-1", header_dc="dc_alpha", fee=0.0,
                      status="approved", created_at=TS)
    _line(master, approved, 201, source="dc_alpha", qty=3.0)
    _line(master, approved, 201, source="dc_beta", qty=2.0)
    shipped = _order(master, order_no="SO-2", header_dc="dc_alpha", fee=0.0,
                     status="shipped", created_at=TS)
    _line(master, shipped, 201, source="dc_alpha", qty=1.0)
    _line(master, shipped, 201, source="dc_beta", qty=1.0)
    _line(master, shipped, 201, source="dc_beta", qty=1.0)
    three_days_ago = (NOW - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    one_day_ago = (NOW - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    delivered = _order(master, order_no="SO-3", header_dc="dc_alpha", fee=0.0,
                       status="delivered", created_at=three_days_ago,
                       delivered_at=one_day_ago)
    _line(master, delivered, 201, source="dc_alpha", qty=1.0)
    _line(master, delivered, 201, source="dc_beta", qty=1.0)
    master.commit()

    alpha = hp.compute_fulfillment(master, "dc_alpha", NOW)
    beta = hp.compute_fulfillment(master, "dc_beta", NOW)
    store = hp.compute_fulfillment(master, "store_one", NOW)

    # 非主仓（beta）看到跨仓单：积压 1、在途 1（两行只算一单）、周期 2 天
    assert beta["backlog"] == 1
    assert beta["in_transit"] == 1
    assert beta["avg_cycle_days"] == 2.0
    # 主仓视角保持现状
    assert alpha["backlog"] == 1
    assert alpha["in_transit"] == 1
    assert alpha["avg_cycle_days"] == 2.0
    # 门店视角不受影响
    assert store["backlog"] == 1
    assert store["in_transit"] == 1
    assert store["avg_cycle_days"] == 2.0


def test_health_fulfillment_ignores_non_participating_warehouse(report_env):
    """只属于 A 的单不得出现在 B 的健康指标里（避免谓词写成恒真）。"""
    master = report_env["master"]
    oid = _order(master, order_no="SO-1", header_dc="dc_alpha", fee=0.0,
                 status="pending", created_at=TS)
    _line(master, oid, 201, source="dc_alpha", qty=1.0)
    master.commit()

    assert hp.compute_fulfillment(master, "dc_alpha", NOW)["backlog"] == 1
    assert hp.compute_fulfillment(master, "dc_beta", NOW)["backlog"] == 0
