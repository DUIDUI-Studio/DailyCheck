"""Real Flask auth/rendering with synthetic SQL fixtures; no third-party WMS access."""
from __future__ import annotations

import re
import sqlite3
from html import unescape
from urllib.parse import parse_qs, urlsplit

import pytest

from mcp_server.infra.errors import McpError
from mcp_server.service import wms, wms_web
from tests.test_wms_web_service import web_sql as web_sql


@pytest.mark.parametrize("warehouse_type,is_admin,allowed", [
    ("distribution_center", False, True), ("storefront", False, False),
    ("rd", False, False), ("storefront", True, True),
])
def test_scope_navigation_and_detail_auth(nav_client_factory, web_sql, warehouse_type, is_admin, allowed):
    client, _ = nav_client_factory(warehouse_type, role="viewer", is_admin=is_admin)
    _, _, calls = web_sql
    for path in ("/wms", "/wms/documents/in/7", "/wms/documents/out/7"):
        response = client.get(path)
        assert response.status_code == (200 if allowed else 403)
        assert response.headers["Cache-Control"] == "no-store"
    for path, start, end in [("/dashboard", '<aside class="sidebar', "</aside>"),
                             ("/land", '<div class="land-grid">', "</main>")]:
        html = client.get(path).get_data(as_text=True)
        carrier = html.split(start, 1)[1].split(end, 1)[0]
        assert ('href="/wms"' in carrier) == allowed
    if not allowed:
        assert not calls


@pytest.mark.parametrize("path", ["/wms", "/wms/documents/in/7", "/wms/documents/out/7"])
def test_anonymous_requires_login(nav_client_factory, web_sql, path):
    client, _ = nav_client_factory("distribution_center")
    with client.session_transaction() as session:
        session.clear()
    response = client.get(path)
    assert response.status_code == 302 and "/login?next=" in response.location
    assert response.headers["Cache-Control"] == "no-store"
    assert not web_sql[2]


def _links(html):
    return [unescape(url) for url in re.findall('href="([^"]+)"', html)]


def test_stock_all_owners_more_than_500_and_readonly(nav_client_factory, web_sql):
    client, wh_path = nav_client_factory("distribution_center", role="viewer")
    with sqlite3.connect(wh_path) as conn:
        before = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    seen = []
    url = "/wms?limit=500&goods_code=SAME&customer_id=999"
    for expected in (500, 107):
        response = client.get(url)
        assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
        html = response.get_data(as_text=True)
        ids = re.findall(r'data-stock-id="(\d+)"', html)
        assert len(ids) == expected
        seen.extend(map(int, ids))
        assert "夹具货主甲" in html and "夹具货主乙" in html
        assert "共 607 条" in html and "不同单位的数量不合计" in html
        assert "范围外商品" not in html
        next_links = re.findall('rel="next" href="([^"]+)"', html)
        if next_links:
            url = unescape(next_links[0])
            query = parse_qs(urlsplit(url).query)
            assert query["goods_code"] == ["SAME"] and query["limit"] == ["500"]
    assert seen == list(range(1, 608))
    with sqlite3.connect(wh_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == before


def test_owner_filter_empty_warehouse_and_unrelated_boundary(nav_client_factory, web_sql):
    client, _ = nav_client_factory("distribution_center")
    html = client.get("/wms?warehouse=1&owner=200&limit=500").get_data(as_text=True)
    assert "共 303 条" in html and "乙的商品" in html and "甲的商品" not in html
    assert 'value="3"' in html
    assert "暂无库存" in client.get("/wms?warehouse=3").get_data(as_text=True)
    html = client.get("/wms?warehouse=4&limit=500").get_data(as_text=True)
    assert "仅配置货主" in html and "共 1 条" in html and "范围外商品" not in html
    for query in ("warehouse=2", "warehouse=999", "warehouse=4&owner=999", "owner=999"):
        assert client.get("/wms?" + query).status_code == 400


@pytest.mark.parametrize("direction", ["in", "out"])
def test_real_document_links_detail_and_original_filters(nav_client_factory, web_sql, direction):
    client, _ = nav_client_factory("distribution_center")
    response = client.get(f"/wms?view={direction}&warehouse=1&owner=200&date_from=2026-10-01&goods_code=SAME&limit=5&page=1")
    assert response.status_code == 200
    links = [url for url in _links(response.get_data(as_text=True)) if f"/wms/documents/{direction}/7?" in url]
    assert len(links) == 1
    detail = client.get(links[0])
    assert detail.status_code == 200 and detail.headers["Cache-Control"] == "no-store"
    html = detail.get_data(as_text=True)
    for text in ("SAME-DISPLAY-NUMBER", "夹具货主乙", "乙的商品", "袋", "LOT-A", "STORE-A", "A01", "B02"):
        assert text in html
    assert "不重复累加" in html
    assert f'<td class="wms-qty">{10 if direction == "in" else 20}</td>' in html
    back = next(url for url in _links(html) if url.startswith("/wms?view="))
    assert parse_qs(urlsplit(back).query) == parse_qs(urlsplit(links[0]).query)
    assert client.get(back).status_code == 200
    assert client.get(f"/wms/documents/{direction}/8").status_code == 404
    assert client.get(f"/wms/documents/{direction}/7?warehouse=3").status_code == 404
    assert client.get(f"/wms/documents/{direction}/123456").status_code == 404
    assert "单据无明细行" in client.get(f"/wms/documents/{direction}/9?warehouse=3").get_data(as_text=True)


@pytest.mark.parametrize("query", ["view=delete", "limit=abc", "limit=0", "limit=501", "page=0", "page=-1", "page=abc",
                                  "date_from=2026-02-30", "date_from=20261101", "owner=-1", "owner=x", "warehouse=1 OR 1=1"])
def test_bad_filters_never_query_upstream(nav_client_factory, web_sql, query):
    client, _ = nav_client_factory("distribution_center")
    response = client.get("/wms?" + query)
    assert response.status_code == 400 and response.headers["Cache-Control"] == "no-store"
    assert not web_sql[2]


@pytest.mark.parametrize("path", ["/wms/documents/sideways/7", "/wms/documents/in/0", "/wms/documents/out/abc", "/wms/documents/in/2147483648"])
def test_invalid_document_identity_never_queries(nav_client_factory, web_sql, path):
    client, _ = nav_client_factory("distribution_center")
    assert client.get(path).status_code == 400
    assert not web_sql[2]


@pytest.mark.parametrize("code", ["wms_not_configured", "wms_driver_missing", "wms_unavailable", "wms_query_failed", "wms_scope_unavailable"])
@pytest.mark.parametrize("path", ["/wms", "/wms/documents/in/7"])
def test_failure_is_distinct_safe_and_uncached(nav_client_factory, web_sql, monkeypatch, code, path):
    def failure(*args, **kwargs):
        raise McpError(code, "SELECT password FROM secret-host", 503)
    monkeypatch.setattr(wms_web, "list_warehouses", failure)
    client, _ = nav_client_factory("distribution_center")
    response = client.get(path)
    html = response.get_data(as_text=True)
    assert response.status_code in (502, 503)
    assert code in html and response.headers["Cache-Control"] == "no-store"
    assert "SELECT password" not in html and "secret-host" not in html
    assert "暂无库存" not in html and "单据无明细行" not in html


def test_partial_failure_hides_header_and_inventory(nav_client_factory, web_sql, monkeypatch):
    original = wms._run
    def failure(sql, params, cfg=None):
        if "ROW_NUMBER()" in sql or "TOP 2001" in sql:
            raise McpError("wms_query_failed", "private raw error", 502)
        return original(sql, params, cfg)
    monkeypatch.setattr(wms, "_run", failure)
    client, _ = nav_client_factory("distribution_center")
    for path in ("/wms", "/wms/documents/in/7"):
        response = client.get(path)
        assert response.status_code == 502
        html = response.get_data(as_text=True)
        for private in ("private raw error", "SAME-DISPLAY-NUMBER", "匹配在库品项", "本次显示"):
            assert private not in html


def test_hostile_content_escapes_and_back_link_is_local(nav_client_factory, web_sql):
    client, _ = nav_client_factory("distribution_center")
    conn = web_sql[1]
    hostile = '<script>alert("xss")</script>'
    conn.execute("UPDATE sy_goods SET goodsName=? WHERE goodsID=2", (hostile,))
    conn.execute("UPDATE sy_customer SET customerName=? WHERE customerID=200", (hostile,))
    conn.execute("UPDATE biz_storeIn SET storeInNo=? WHERE storeInID=7", (hostile,))
    for path in ("/wms?owner=200", "/wms?view=in", "/wms/documents/in/7?next=https://evil.invalid"):
        html = client.get(path).get_data(as_text=True)
        assert hostile not in html and "&lt;script&gt;" in html
        assert "https://evil.invalid" not in html


def test_out_of_range_page_and_truncation(nav_client_factory, web_sql, monkeypatch):
    client, _ = nav_client_factory("distribution_center")
    html = client.get("/wms?page=99").get_data(as_text=True)
    assert "本页没有记录" in html and "共 607 条" in html and "第一页" in html
    monkeypatch.setattr(wms_web, "MAX_LOCATION_ROWS", 1)
    html = client.get("/wms/documents/out/7").get_data(as_text=True)
    assert "本次结果不完整" in html and "乙的商品" in html


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
@pytest.mark.parametrize("path", ["/wms", "/wms/documents/in/7", "/wms/documents/out/7"])
def test_no_write_methods(nav_client_factory, web_sql, method, path):
    client, _ = nav_client_factory("distribution_center")
    assert getattr(client, method)(path).status_code == 405
    assert not web_sql[2]
