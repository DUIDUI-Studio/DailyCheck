"""外仓页面：真实 Web 鉴权、查询边界与上游故障降级；不连接第三方库。"""
from __future__ import annotations

import sqlite3

import pytest

from mcp_server.infra.errors import McpError
from mcp_server.service import wms


@pytest.fixture
def upstream(monkeypatch):
    calls = []
    monkeypatch.setattr(wms, "list_warehouses", lambda: [{"warehouse": "冷库甲"}, {"warehouse": "冷库乙"}])
    monkeypatch.setattr(wms, "summarize_stock", lambda **kw: [{"warehouse": "冷库乙", "sku_count": 2, "total_qty": 120}])

    def stock(**kw):
        calls.append(("stock", kw))
        return [{"goods_code": "ICE-01", "goods_name": "香草冰激凌", "unit": "箱",
                 "qty": 12.5, "warehouse": "冷库乙", "zone": "A区", "position": "A-01", "lot_no": "LOT-01"}]

    def documents(**kw):
        calls.append(("documents", kw))
        return [{"doc_no": "WMS-001", "biz_date": "2026-11-01", "warehouse": "冷库乙",
                 "qty": 10, "supplier": "供应方甲", "receiver": "收货方乙", "issued_by": "经办人"}]

    monkeypatch.setattr(wms, "list_stock", stock)
    monkeypatch.setattr(wms, "list_documents", documents)
    return calls


@pytest.mark.parametrize("warehouse_type,is_admin,allowed", [
    ("distribution_center", False, True), ("storefront", False, False),
    ("rd", False, False), ("storefront", True, True),
])
def test_scope_and_both_navigation_carriers(nav_client_factory, upstream, warehouse_type, is_admin, allowed):
    client, _ = nav_client_factory(warehouse_type, is_admin=is_admin)
    response = client.get("/wms")
    assert response.status_code == (200 if allowed else 403)
    for path, start, end in [("/dashboard", '<aside class="sidebar', "</aside>"),
                             ("/land", '<div class="land-grid">', "</main>")]:
        html = client.get(path).get_data(as_text=True)
        carrier = html.split(start, 1)[1].split(end, 1)[0]
        assert ('href="/wms"' in carrier) == allowed
    if not allowed:
        assert not upstream


def test_anonymous_requires_login(logged_client, upstream):
    client, _ = logged_client
    with client.session_transaction() as session:
        session.clear()
    response = client.get("/wms?warehouse=冷库乙")
    assert response.status_code == 302
    assert "/login?next=" in response.location
    assert not upstream


def test_stock_filters_and_readonly_page(nav_client_factory, upstream):
    client, wh_path = nav_client_factory("distribution_center", role="viewer")
    with sqlite3.connect(wh_path) as conn:
        before = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    response = client.get("/wms?warehouse=冷库乙&goods_code=ICE-01&limit=500&customer_id=999")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert upstream == [("stock", {"warehouse": "冷库乙", "goods_code": "ICE-01", "limit": 500})]
    html = response.get_data(as_text=True)
    for value in ("外仓信息", "只读", "香草冰激凌", "LOT-01", "A-01", "冷库甲", "冷库乙"):
        assert value in html
    assert '<form method="get"' in html
    assert 'method="post"' not in html.split('<section class="wms-board"', 1)[1].split("</section>", 1)[0]
    with sqlite3.connect(wh_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == before


@pytest.mark.parametrize("view,direction,party", [("in", "in", "供应方甲"), ("out", "out", "收货方乙")])
def test_documents_filters(nav_client_factory, upstream, view, direction, party):
    client, _ = nav_client_factory("distribution_center")
    response = client.get(f"/wms?view={view}&warehouse=冷库乙&date_from=2026-10-01&limit=100")
    assert response.status_code == 200
    assert upstream == [("documents", {"direction": direction, "warehouse": "冷库乙", "date_from": "2026-10-01", "limit": 100})]
    assert party in response.get_data(as_text=True)
    assert "WMS-001" in response.get_data(as_text=True)


@pytest.mark.parametrize("query", ["view=delete", "limit=abc", "limit=0", "limit=501", "date_from=2026-02-30", "date_from=abc"])
def test_bad_filters_never_query_upstream(nav_client_factory, upstream, query):
    client, _ = nav_client_factory("distribution_center")
    assert client.get("/wms?" + query).status_code == 400
    assert not upstream


@pytest.mark.parametrize("code", ["wms_not_configured", "wms_driver_missing", "wms_unavailable", "wms_query_failed"])
def test_failure_is_not_presented_as_empty_stock(nav_client_factory, monkeypatch, code):
    def unavailable():
        raise McpError(code, "secret-password-and-host", 503)
    monkeypatch.setattr(wms, "list_warehouses", unavailable)
    client, _ = nav_client_factory("distribution_center")
    response = client.get("/wms")
    html = response.get_data(as_text=True)
    assert response.status_code == 200  # 导航可达，故障在页面显式展示
    assert code in html and "暂时无法查询" in html
    assert "secret-password-and-host" not in html
    assert "暂无库存" not in html


def test_empty_and_limit_states(nav_client_factory, upstream, monkeypatch):
    client, _ = nav_client_factory("distribution_center")
    monkeypatch.setattr(wms, "list_stock", lambda **kw: [])
    assert "暂无库存" in client.get("/wms").get_data(as_text=True)
    monkeypatch.setattr(wms, "list_stock", lambda **kw: [{"qty": 1}] * kw["limit"])
    assert "可能还有更多" in client.get("/wms?limit=100").get_data(as_text=True)


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_no_write_methods(nav_client_factory, upstream, method):
    client, _ = nav_client_factory("distribution_center")
    assert getattr(client, method)("/wms").status_code == 405
    assert not upstream


@pytest.mark.parametrize("kind", ["stock", "summary", "in", "out"])
def test_warehouse_filter_is_bound_before_limit(monkeypatch, kind):
    for key, value in {"HOST": "example.invalid", "USER": "u", "PASSWORD": "secret", "CUSTOMER_ID": "155"}.items():
        monkeypatch.setenv("DAILYCHECK_WMS_" + key, value)
    calls = []
    monkeypatch.setattr(wms, "_run", lambda sql, params, cfg: calls.append((sql, params)) or [])
    warehouse = "冷库乙' OR 1=1 --"
    if kind == "stock":
        wms.list_stock(limit=5, goods_code="ICE-01", warehouse=warehouse)
    elif kind == "summary":
        wms.summarize_stock(warehouse=warehouse)
    else:
        wms.list_documents(direction=kind, limit=5, warehouse=warehouse)
    sql, params = calls[0]
    assert params[0] == 155
    assert warehouse in params and warehouse not in sql
    assert "AND w.warehouse = %s" in sql
    assert sql.index("AND w.warehouse = %s") < sql.index("ORDER BY")
    if kind != "summary":
        assert "TOP 5" in sql


def test_warehouse_options_include_document_only_warehouses(monkeypatch):
    for key in ("HOST", "USER", "PASSWORD"):
        monkeypatch.setenv("DAILYCHECK_WMS_" + key, "test")
    monkeypatch.setenv("DAILYCHECK_WMS_CUSTOMER_ID", "155")
    calls = []
    monkeypatch.setattr(wms, "_run", lambda sql, params, cfg: calls.append((sql, params)) or [])
    wms.list_warehouses()
    sql, params = calls[0]
    assert "biz_storeIn" in sql and "biz_storeOut" in sql and "biz_goodsBalance" in sql
    assert params == (155, 155, 155)
    assert "SELECT DISTINCT" in sql and "TOP" not in sql


def test_invalid_query_does_not_even_list_warehouses(nav_client_factory, monkeypatch):
    def forbidden():
        pytest.fail("invalid filters must be rejected before any upstream query")
    monkeypatch.setattr(wms, "list_warehouses", forbidden)
    client, _ = nav_client_factory("distribution_center")
    response = client.get("/wms?date_from=20261101")
    assert response.status_code == 400
    assert response.headers["Cache-Control"] == "no-store"


def test_unknown_warehouse_rejected(nav_client_factory, upstream):
    client, _ = nav_client_factory("distribution_center")
    response = client.get("/wms?warehouse=其他货主仓库")
    assert response.status_code == 400
    assert "不在当前可查询范围" in response.get_data(as_text=True)
    assert not upstream


def test_document_only_warehouse_is_selectable(nav_client_factory, upstream, monkeypatch):
    monkeypatch.setattr(wms, "summarize_stock", lambda **kw: [])
    monkeypatch.setattr(wms, "list_stock", lambda **kw: [])
    client, _ = nav_client_factory("distribution_center")
    assert "暂无库存" in client.get("/wms?warehouse=冷库甲").get_data(as_text=True)
    assert client.get("/wms?warehouse=冷库甲&view=in").status_code == 200
    assert upstream[-1][1]["warehouse"] == "冷库甲"


def test_partial_failure_hides_summary(nav_client_factory, upstream, monkeypatch):
    def failure(**kw):
        raise McpError("wms_query_failed", "SELECT password FROM secret-host", 502)
    monkeypatch.setattr(wms, "list_stock", failure)
    client, _ = nav_client_factory("distribution_center")
    html = client.get("/wms").get_data(as_text=True)
    assert "暂时无法查询" in html
    assert "个在库品项" not in html
    assert "本次显示" not in html
    assert "SELECT password" not in html


def test_summary_has_no_mixed_unit_or_cross_warehouse_totals(nav_client_factory, upstream, monkeypatch):
    monkeypatch.setattr(wms, "summarize_stock", lambda **kw: [
        {"warehouse": "冷库甲", "sku_count": 3, "total_qty": 123456},
        {"warehouse": "冷库乙", "sku_count": 3, "total_qty": 987654},
    ])
    client, _ = nav_client_factory("distribution_center")
    html = client.get("/wms?goods_code=ICE-01").get_data(as_text=True)
    assert "123456" not in html and "987654" not in html
    assert "跨仓同品项可能重复" in html
    assert "不受货品编码筛选影响" in html


def test_tabs_keep_filters_and_upstream_strings_are_escaped(nav_client_factory, upstream, monkeypatch):
    from html import unescape
    from urllib.parse import parse_qs, urlsplit

    monkeypatch.setattr(wms, "list_stock", lambda **kw: [
        {"goods_name": '<script>alert("xss")</script>', "qty": 1},
    ])
    client, _ = nav_client_factory("distribution_center")
    html = client.get("/wms?warehouse=冷库乙&goods_code=ICE-01&date_from=2026-11-01&limit=5").get_data(as_text=True)
    assert '<script>alert("xss")</script>' not in html
    assert "&lt;script&gt;" in html
    import re
    tabs = html.split('<nav class="wms-tabs"', 1)[1].split("</nav>", 1)[0]
    for link in re.findall('href="([^"]+)"', tabs):
        query = parse_qs(urlsplit(unescape(link)).query)
        assert query["warehouse"] == ["冷库乙"]
        assert query["goods_code"] == ["ICE-01"]
        assert query["date_from"] == ["2026-11-01"]
        assert query["limit"] == ["5"]


def test_query_failure_closes_connection_without_exposing_sql(monkeypatch):
    cfg = wms.WmsConfig("secret-host", 1234, "u", "secret-password", "db", "7.0", 42)
    closed = []

    class Connection:
        def cursor(self):
            return self

        def execute(self, sql, params):
            assert params == (42,)
            raise RuntimeError(sql + cfg.host + cfg.password)

        def close(self):
            closed.append(True)

    monkeypatch.setattr(wms, "_connect", lambda config: Connection())
    with pytest.raises(McpError) as exc:
        wms._run("SELECT private FROM sensitive", (42,), cfg)
    assert exc.value.code == "wms_query_failed"
    assert "secret" not in str(exc.value) and "SELECT" not in str(exc.value)
    assert closed == [True]


def test_upstream_failure_preserves_selected_warehouse(nav_client_factory, monkeypatch):
    def failure():
        raise McpError("wms_unavailable", "private upstream details", 503)
    monkeypatch.setattr(wms, "list_warehouses", failure)
    client, _ = nav_client_factory("distribution_center")
    html = client.get("/wms?warehouse=冷库乙&goods_code=ICE-01&limit=5").get_data(as_text=True)
    assert '<option value="冷库乙" selected>' in html
    assert 'value="ICE-01"' in html
    assert 'value="5"' in html
