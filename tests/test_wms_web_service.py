"""Verified WMS schema, synthetic data, SQLite SQL-dialect adapter; never live WMS."""
from __future__ import annotations

import re
import sqlite3

import pytest

from mcp_server.service import wms
from mcp_server.service import wms_web as web


@pytest.fixture
def web_sql(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
    CREATE TABLE sy_warehouse(warehouseID INTEGER PRIMARY KEY, warehouse TEXT);
    CREATE TABLE sy_customer(customerID INTEGER PRIMARY KEY, customerCode TEXT, customerName TEXT);
    CREATE TABLE sy_goods(goodsID INTEGER PRIMARY KEY, customerID INT, goodsCode TEXT, goodsName TEXT, unit TEXT);
    CREATE TABLE sy_position(positionID INTEGER PRIMARY KEY, warehouseID INT, zone TEXT, position TEXT);
    CREATE TABLE biz_goodsBalance(goodsBalanceID INTEGER PRIMARY KEY, warehouseID INT, positionID INT,
        goodsID INT, qty NUMERIC, lotNo TEXT, inactive INT);
    INSERT INTO sy_warehouse VALUES (1,'朴宇冷库'),(2,'测试仓'),(3,'朴宇仓'),(4,'畅饮仓');
    INSERT INTO sy_customer VALUES (155,'A','夹具货主甲'),(200,'B','夹具货主乙'),(999,'X','范围外货主');
    INSERT INTO sy_goods VALUES (1,155,'SAME','甲的商品','箱'),(2,200,'SAME','乙的商品','袋'),(3,999,'OTHER','范围外商品','件');
    INSERT INTO sy_position VALUES (1,1,'A区','A01'),(2,1,'B区','B02');
    """)
    for direction, party in (("In", "supplierName"), ("Out", "receiverName")):
        conn.executescript(f"""
        CREATE TABLE biz_store{direction}(store{direction}ID INTEGER PRIMARY KEY, store{direction}No TEXT,
            bizDate TEXT, warehouseID INT, customerID INT, qty NUMERIC, {party} TEXT, issusedBy TEXT);
        CREATE TABLE biz_store{direction}Detail(store{direction}DetailID INTEGER PRIMARY KEY,
            store{direction}ID INT, goodsID INT, qty NUMERIC, lotNo TEXT, storeLotNo TEXT);
        CREATE TABLE biz_store{direction}Position(store{direction}PositionID INTEGER PRIMARY KEY,
            store{direction}DetailID INT, positionID INT, qty NUMERIC, lotNo TEXT);
        INSERT INTO biz_store{direction} VALUES
            (7,'SAME-DISPLAY-NUMBER','2026-11-01 12:00:00',1,200,10,'夹具往来方','夹具经办人'),
            (8,'SAME-DISPLAY-NUMBER','2026-11-01 12:00:00',4,999,500,'隐藏','隐藏'),
            (9,'EMPTY-LINES','2026-11-01 12:00:00',3,155,0,'夹具往来方','夹具经办人'),
            (10,'OTHER-WH-OWN','2026-11-01 12:00:00',4,155,1,'夹具往来方','夹具经办人');
        INSERT INTO biz_store{direction}Detail VALUES
            (70,7,2,10,'LOT-A','STORE-A'),(80,8,3,500,'SECRET','SECRET');
        INSERT INTO biz_store{direction}Position VALUES (1,70,1,4,'LOT-A'),(2,70,2,6,'LOT-A');
        """)
    conn.executemany("INSERT INTO biz_goodsBalance VALUES (?,1,1,?,1,?,0)",
                     [(i, 1 if i % 2 else 2, f"LOT-{i}") for i in range(1, 608)])
    conn.executemany("INSERT INTO biz_goodsBalance VALUES (?,4,1,?,1,'OTHER',0)", [(608, 3), (609, 1)])
    for key in ("HOST", "USER", "PASSWORD"):
        monkeypatch.setenv("DAILYCHECK_WMS_" + key, "fixture.invalid")
    monkeypatch.setenv("DAILYCHECK_WMS_CUSTOMER_ID", "155")
    cfg = wms.load_config()
    calls = []

    def run(sql, params, cfg=None):
        calls.append((sql, params))
        assert sql.lstrip().split()[0] in {"SELECT", "WITH"}
        translated = sql.replace("%s", "?").replace("ISNULL(", "IFNULL(").replace("COUNT_BIG(", "COUNT(")
        translated = translated.replace("CONVERT(varchar(10),s.bizDate,120)", "substr(s.bizDate,1,10)")
        top = re.search(r"SELECT TOP (\d+)", translated)
        if top:
            translated = translated.replace(top[0], "SELECT", 1) + " LIMIT " + top[1]
        return [dict(row) for row in conn.execute(translated, params)]

    monkeypatch.setattr(wms, "_run", run)
    yield cfg, conn, calls
    conn.close()


def default_scope(cfg):
    return web.resolve_scope(None, web.list_warehouses(cfg), cfg)


def test_default_complete_stock_pagination_and_owner_identity(web_sql):
    cfg, _, calls = web_sql
    scope = default_scope(cfg)
    seen = []
    for n in range(1, 8):
        page = web.inventory_page(scope, cfg, size=100, number=n, goods_code="SAME")
        seen.extend(r["balance_id"] for r in page.rows)
        assert page.total == 607 and page.sku_count == 2
        assert page.has_next == (n < 7)
        assert {row["owner_id"] for row in page.rows} == {155, 200}
        assert {row["unit"] for row in page.rows} == {"箱", "袋"}
    assert seen == list(range(1, 608))
    sql, params = calls[-1]
    assert "b.warehouseID IN (%s, %s)" in sql and params[:2] == (1, 3)
    assert "balance_id" in sql and "ROW_NUMBER()" in sql
    assert "SUM(" not in sql


def test_owner_and_empty_page_counts_use_same_scope(web_sql):
    cfg, _, _ = web_sql
    scope = default_scope(cfg)
    page = web.inventory_page(scope, cfg, owner_id=200, size=500)
    assert page.total == 303 and page.sku_count == 1
    assert all(row["owner_id"] == 200 for row in page.rows)
    page = web.inventory_page(scope, cfg, owner_id=200, number=99)
    assert page.rows == [] and page.total == 303
    page = web.inventory_page(scope, cfg, goods_code="no match")
    assert page.total == page.sku_count == 0 and page.rows == []


def test_scope_choices_empty_warehouse_and_unchanged_other_owner_boundary(web_sql):
    cfg, _, _ = web_sql
    warehouses = web.list_warehouses(cfg)
    assert {row["warehouse_id"] for row in warehouses} == {1, 3, 4}
    assert {row["owner_id"] for row in web.list_owners(default_scope(cfg), cfg)} == {155, 200}
    empty = web.resolve_scope(3, warehouses, cfg)
    assert web.inventory_page(empty, cfg).total == 0
    assert web.documents_page(empty, cfg, web.Direction.IN).total == 1
    other = web.resolve_scope(4, warehouses, cfg)
    assert other.customer_id == 155
    page = web.inventory_page(other, cfg)
    assert page.total == 1 and page.rows[0]["owner_id"] == 155
    assert {row["owner_id"] for row in web.list_owners(other, cfg)} == {155}
    with pytest.raises(ValueError):
        web.WebScope((4,), None)
    with pytest.raises(Exception, match="outside available scope"):
        web.resolve_scope(2, warehouses, cfg)


@pytest.mark.parametrize("direction", list(web.Direction))
def test_document_identity_scope_and_allocations_do_not_multiply_qty(web_sql, direction):
    cfg, _, calls = web_sql
    scope = default_scope(cfg)
    page = web.documents_page(scope, cfg, direction, date_from="2026-11-01", size=1)
    assert page.total == 2 and page.has_next
    assert page.rows[0]["doc_id"] == 9
    detail = web.document_detail(scope, cfg, web.DocumentKey(direction, 7))
    assert detail.header["owner_id"] == 200 and detail.header["doc_no"] == "SAME-DISPLAY-NUMBER"
    assert len(detail.lines) == 1 and detail.lines[0]["qty"] == 10
    assert [p["qty"] for p in detail.lines[0]["locations"]] == [4, 6]
    assert detail.lines[0]["store_lot_no"] == "STORE-A"
    assert detail.state == web.DetailState.COMPLETE
    assert web.document_detail(scope, cfg, web.DocumentKey(direction, 8)) is None
    assert web.document_detail(scope, cfg, web.DocumentKey(direction, 123456)) is None
    empty = web.document_detail(scope, cfg, web.DocumentKey(direction, 9))
    assert empty.state == web.DetailState.EMPTY and empty.lines == []
    for sql, params in calls[-6:]:
        assert "s.warehouseID IN (%s, %s)" in sql and params[:2] == (1, 3)


def test_other_warehouse_detail_cannot_read_other_owner(web_sql):
    cfg, _, _ = web_sql
    scope = web.resolve_scope(4, web.list_warehouses(cfg), cfg)
    assert web.document_detail(scope, cfg, web.DocumentKey(web.Direction.OUT, 8)) is None
    assert web.document_detail(scope, cfg, web.DocumentKey(web.Direction.OUT, 10)).header["owner_id"] == 155


def test_literals_are_bound_and_truncation_is_explicit(web_sql, monkeypatch):
    cfg, _, calls = web_sql
    scope = default_scope(cfg)
    hostile = "x' OR 1=1 --"
    assert web.inventory_page(scope, cfg, goods_code=hostile).total == 0
    assert hostile in calls[-1][1] and hostile not in calls[-1][0]
    monkeypatch.setattr(web, "MAX_LOCATION_ROWS", 1)
    detail = web.document_detail(scope, cfg, web.DocumentKey(web.Direction.IN, 7))
    assert detail.state == web.DetailState.TRUNCATED and detail.lines[0]["qty"] == 10


@pytest.mark.parametrize("direction,identity", [("in", 1), (web.Direction.IN, 0), (web.Direction.OUT, 2147483648)])
def test_invalid_document_identity(direction, identity):
    with pytest.raises(ValueError):
        web.DocumentKey(direction, identity)
