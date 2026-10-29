"""MCP outbound_list / restock_list 分页验收测试（Issue #74）。

覆盖验收口径：
  - 种子 120 条 outbound_requests，用 offset 逐页翻完，拼接结果与全量一致、
    无重复无遗漏；
  - total 恒等于总行数；
  - truncated 首页 True、末页 False；
  - restock_list 同样验证；
  - service 层 clamp：limit 默认 100 / 上限 500 / 下限 1，offset 默认 0 / 负值归 0；
  - 两个工具的 inputSchema 暴露 limit/offset，description 声明分页信封。

用真实临时 master.db + warehouse.db（与 tests/test_store_order_mcp.py 同构），
不走 mock —— 分页边界只有真 SQL LIMIT/OFFSET 才能证明。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from mcp_server.data import warehouse
from mcp_server.infra.errors import ValidationError
from mcp_server.protocol.tools import get_tools
from mcp_server.protocol.tools.inbound import restock_list_impl
from mcp_server.protocol.tools.outbound import outbound_list_impl
from mcp_server.service.auth import AuthContext, set_current_auth
from mcp_server.service.inbound import list_restock
from mcp_server.service.outbound import list_outbound

TS = "2026-10-25 09:00:00"
WAREHOUSE_CODE = "WH001"
SEED_ROWS = 120
SEED_ITEM_ID = 1
# B1/B5（Prod 复审）：种 5 条孤儿 outbound_requests（item_id 指向不存在的 item）。
# 库存品物理删除后历史出库申请不会被清理，生产 wh_003 实测有 17 条孤儿行。
ORPHAN_ITEM_ID = 9999
ORPHAN_ROWS = 5
EXPECTED_OUTBOUND_ROWS = SEED_ROWS + ORPHAN_ROWS


def _input_schema(tool) -> dict:
    """取 Tool 的 inputSchema（兼容 mcp 1.x / 2.x 的字段命名）。"""
    return getattr(tool, "inputSchema", None) or tool.input_schema


@pytest.fixture
def pagination_env(tmp_path, monkeypatch, request):
    """真实 master.db + warehouse.db，种 120 条出库 / 120 条入库流水。"""
    import config as config_module
    import db as db_module
    import mcp_server.data.unit_of_work as uow

    seed_rows, orphan_rows = getattr(request, "param", (SEED_ROWS, ORPHAN_ROWS))
    master_path = tmp_path / "master.db"
    wh_dir = tmp_path / "warehouses"
    wh_dir.mkdir()

    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_dir)
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", wh_dir)
    monkeypatch.setattr(uow, "MASTER_DB", str(master_path))

    db_module.init_master_db()
    wh_path = wh_dir / f"{WAREHOUSE_CODE}.db"
    db_module.init_warehouse_db(wh_path)

    conn = sqlite3.connect(wh_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO items (id, sku, name, category_id, quantity, updated_at) "
        "VALUES (?, 'SKU-1', '测试品项', 1, 10000, ?)",
        (SEED_ITEM_ID, TS),
    )
    # 刻意让 120 条共享同一 created_at：分页若没有 id 作为稳定 tie-break，
    # 逐页拼接就可能重复或遗漏。
    for i in range(1, seed_rows + 1):
        conn.execute(
            "INSERT INTO outbound_requests "
            "(item_id, requested_quantity, reason, status, rolled_back, created_at) "
            "VALUES (?, ?, ?, '出库', 0, ?)",
            (SEED_ITEM_ID, float(i), f"outbound-{i}", TS),
        )
        # Issue #72 / PR #76 起 restock 列表读 restock_requests（入库单实体），
        # 不再读 stock_movements。分页用例因此种到新数据源，语义等价。
        conn.execute(
            "INSERT INTO restock_requests "
            "(item_id, requested_quantity, reason, status, created_at) "
            "VALUES (?, ?, ?, '入库', ?)",
            (SEED_ITEM_ID, float(i), f"restock-{i}", TS),
        )
    # 孤儿行：item_id 无对应 items 记录（模拟库存品物理删除后的历史出库申请）。
    for i in range(1, orphan_rows + 1):
        conn.execute(
            "INSERT INTO outbound_requests "
            "(item_id, requested_quantity, reason, status, rolled_back, created_at) "
            "VALUES (?, ?, ?, '出库', 0, ?)",
            (ORPHAN_ITEM_ID, float(i), f"orphan-outbound-{i}", TS),
        )
    conn.commit()
    conn.close()

    master = sqlite3.connect(master_path)
    master.execute(
        "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) "
        "VALUES (1, ?, '测试仓', ?, 'storefront', ?)",
        (WAREHOUSE_CODE, str(wh_path), TS),
    )
    master.commit()
    master.close()

    yield {"master_path": master_path, "wh_path": wh_path}


@pytest.fixture
def ctx():
    """安装请求级 AuthContext（工具层 resolve_ctx 依赖它）。"""
    auth = AuthContext(
        token_id=1,
        allowed_read_paths=["/api/v1/outbound", "/api/v1/restock"],
        allowed_write_paths=[],
        allowed_warehouses=[WAREHOUSE_CODE],
    )
    set_current_auth(auth)
    yield auth
    set_current_auth(None)


def _walk_pages(fetch_page, page_size: int = 25) -> tuple[list[list[dict]], list[dict]]:
    """按 `truncated` 逐页翻完，返回（各页 items，拼接后的 items）。

    终止条件必须是信封的 `truncated=False`，不能用「拿到空页」判停 ——
    后者会在 total 口径偏大时先多请求一个空页，把契约 bug 吞掉。
    """
    pages: list[list[dict]] = []
    combined: list[dict] = []
    offset = 0
    expected_total = None
    while True:
        page = fetch_page(limit=page_size, offset=offset)
        if expected_total is None:
            expected_total = page["total"]
        assert page["total"] == expected_total
        items = page["items"]
        pages.append(items)
        combined.extend(items)
        assert len(items) <= page_size
        if not page["truncated"]:
            break
        assert items, "truncated=True 的页不应为空（total 与可翻页行数口径不一致？）"
        offset += page_size
        if offset > expected_total:
            raise AssertionError("pagination did not terminate")
    assert len(combined) == expected_total
    assert pages[-1] or expected_total == 0, "truncated 不应导致尾随空页"
    return pages, combined


# ---------------------------------------------------------------------------
# outbound_list
# ---------------------------------------------------------------------------

class TestOutboundListPagination:
    @pytest.mark.parametrize("pagination_env", [(1630, 17)], indirect=True)
    def test_production_shaped_orphans_have_no_trailing_empty_page(self, pagination_env, ctx):
        """生产 wh_003 同形：1630 条有效关联 + 17 条孤儿，选 LEFT JOIN 共 1647 条。"""
        envelopes = []

        def fetch_page(**kwargs):
            page = list_outbound(WAREHOUSE_CODE, ctx, **kwargs)
            envelopes.append(page)
            return page

        pages, combined = _walk_pages(fetch_page, page_size=100)
        assert len(pages) == 17
        assert len(pages[-1]) == 47
        assert all(envelope["truncated"] for envelope in envelopes[:-1])
        assert envelopes[-1]["truncated"] is False
        assert {envelope["total"] for envelope in envelopes} == {1647}
        assert len(combined) == len({row["id"] for row in combined}) == 1647
        assert sum(row["item_name"] is None for row in combined) == 17

    def test_pages_cover_full_set_without_duplicates_or_gaps(self, pagination_env, ctx):
        full = list_outbound(WAREHOUSE_CODE, ctx, limit=500, offset=0)
        assert full["total"] == EXPECTED_OUTBOUND_ROWS
        assert len(full["items"]) == EXPECTED_OUTBOUND_ROWS

        pages, combined = _walk_pages(
            lambda limit, offset: list_outbound(
                WAREHOUSE_CODE, ctx, limit=limit, offset=offset
            )
        )

        full_ids = [row["id"] for row in full["items"]]
        combined_ids = [row["id"] for row in combined]
        assert combined_ids == full_ids
        assert len(combined_ids) == len(set(combined_ids)) == EXPECTED_OUTBOUND_ROWS

    def test_total_is_constant_and_truncated_flips_on_last_page(self, pagination_env, ctx):
        pages, _ = _walk_pages(
            lambda limit, offset: list_outbound(
                WAREHOUSE_CODE, ctx, limit=limit, offset=offset
            ),
            page_size=50,
        )
        envelopes = [
            list_outbound(WAREHOUSE_CODE, ctx, limit=50, offset=offset)
            for offset in range(0, EXPECTED_OUTBOUND_ROWS, 50)
        ]
        assert [p["total"] for p in envelopes] == [EXPECTED_OUTBOUND_ROWS] * len(envelopes)
        assert envelopes[0]["truncated"] is True
        assert envelopes[-1]["truncated"] is False
        # 首页/末页条数也钉住，避免 truncated 恒 True/False 的假绿
        assert len(pages[0]) == 50
        assert len(pages[-1]) == EXPECTED_OUTBOUND_ROWS % 50 == 25

    def test_defaults_and_clamps(self, pagination_env, ctx):
        default = list_outbound(WAREHOUSE_CODE, ctx)
        assert (default["limit"], default["offset"]) == (100, 0)
        assert len(default["items"]) == 100
        assert default["truncated"] is True

        lower = list_outbound(WAREHOUSE_CODE, ctx, limit=0, offset=-5)
        assert (lower["limit"], lower["offset"]) == (1, 0)
        assert len(lower["items"]) == 1

        upper = list_outbound(WAREHOUSE_CODE, ctx, limit=501, offset=0)
        assert upper["limit"] == 500
        assert len(upper["items"]) == EXPECTED_OUTBOUND_ROWS

    def test_data_layer_count_matches_rows(self, pagination_env):
        conn = sqlite3.connect(pagination_env["wh_path"])
        conn.row_factory = sqlite3.Row
        try:
            assert warehouse.count_outbound(conn) == EXPECTED_OUTBOUND_ROWS
            rows = warehouse.list_outbound(conn, limit=10, offset=5)
            assert len(rows) == 10
            assert warehouse.count_outbound(conn) == EXPECTED_OUTBOUND_ROWS
            # B1 回归：COUNT 与列表同口径，total == 实际可翻页行数（含孤儿行）。
            all_rows = warehouse.list_outbound(conn, limit=500, offset=0)
            assert len(all_rows) == warehouse.count_outbound(conn) == EXPECTED_OUTBOUND_ROWS
        finally:
            conn.close()

    def test_orphan_outbound_rows_are_counted_and_pageable(self, pagination_env, ctx):
        """B1 回归：孤儿 outbound 行计入 total，且能被逐页翻到（item_name=null）。"""
        full = list_outbound(WAREHOUSE_CODE, ctx, limit=500, offset=0)
        orphan_rows = [r for r in full["items"] if r["item_id"] == ORPHAN_ITEM_ID]
        assert len(orphan_rows) == ORPHAN_ROWS
        assert {r["id"] for r in orphan_rows} == set(
            range(SEED_ROWS + 1, SEED_ROWS + ORPHAN_ROWS + 1)
        )
        assert all(r["item_name"] is None for r in orphan_rows)
        assert full["total"] == len(full["items"]) == EXPECTED_OUTBOUND_ROWS
        assert full["truncated"] is False


# ---------------------------------------------------------------------------
# restock_list
# ---------------------------------------------------------------------------

class TestRestockListPagination:
    def test_pages_cover_full_set_without_duplicates_or_gaps(self, pagination_env, ctx):
        full = list_restock(WAREHOUSE_CODE, ctx, limit=500, offset=0)
        assert full["total"] == SEED_ROWS
        assert len(full["items"]) == SEED_ROWS

        pages, combined = _walk_pages(
            lambda limit, offset: list_restock(
                WAREHOUSE_CODE, ctx, limit=limit, offset=offset
            )
        )

        full_ids = [row["id"] for row in full["items"]]
        combined_ids = [row["id"] for row in combined]
        assert combined_ids == full_ids
        assert len(combined_ids) == len(set(combined_ids)) == SEED_ROWS

    def test_total_is_constant_and_truncated_flips_on_last_page(self, pagination_env, ctx):
        pages, _ = _walk_pages(
            lambda limit, offset: list_restock(
                WAREHOUSE_CODE, ctx, limit=limit, offset=offset
            ),
            page_size=50,
        )
        envelopes = [
            list_restock(WAREHOUSE_CODE, ctx, limit=50, offset=offset)
            for offset in range(0, SEED_ROWS, 50)
        ]
        assert [p["total"] for p in envelopes] == [SEED_ROWS] * len(envelopes)
        assert envelopes[0]["truncated"] is True
        assert envelopes[-1]["truncated"] is False
        # truncated 终止后末页即最后一页，不再有尾随空页。
        assert len(pages[0]) == 50
        assert len(pages[-1]) == 20

    def test_defaults_and_clamps(self, pagination_env, ctx):
        default = list_restock(WAREHOUSE_CODE, ctx)
        assert (default["limit"], default["offset"]) == (100, 0)
        assert len(default["items"]) == 100
        assert default["truncated"] is True

        lower = list_restock(WAREHOUSE_CODE, ctx, limit=-1, offset=-9)
        assert (lower["limit"], lower["offset"]) == (1, 0)
        assert len(lower["items"]) == 1

        upper = list_restock(WAREHOUSE_CODE, ctx, limit=999, offset=0)
        assert upper["limit"] == 500
        assert len(upper["items"]) == SEED_ROWS

    def test_data_layer_count_matches_rows(self, pagination_env):
        conn = sqlite3.connect(pagination_env["wh_path"])
        conn.row_factory = sqlite3.Row
        try:
            assert warehouse.count_restock_movements(conn) == SEED_ROWS
            rows = warehouse.list_restock_movements(conn, limit=10, offset=5)
            assert len(rows) == 10
            # 新数据源的 row 形状：quantity / status 来自 restock_requests。
            assert rows[0]["quantity"] == 115.0
            assert rows[0]["status"] == "入库"
            assert warehouse.count_restock_movements(conn) == SEED_ROWS
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# B2：limit/offset 类型守卫 → MCP 结构化 ValidationError
# ---------------------------------------------------------------------------

class TestPaginationTypeGuards:
    @pytest.mark.parametrize("name", ["outbound_list", "restock_list"])
    @pytest.mark.parametrize("field,value", [("limit", "abc"), ("limit", None), ("offset", "x")])
    def test_protocol_serializes_validation_error(
        self, pagination_env, ctx, monkeypatch, name, field, value
    ):
        import mcp_server.protocol.server as protocol

        class CaptureServer:
            """只替换 SDK 装饰器注册；执行真实 call_tool、工具、service 与 SQL。"""
            def __init__(self, name):
                pass

            def list_tools(self):
                return lambda handler: handler

            def call_tool(self):
                def register(handler):
                    self.handler = handler
                    return handler
                return register

        monkeypatch.setattr(protocol, "Server", CaptureServer)
        server = protocol.build_server()
        result = asyncio.run(server.handler(name, {"warehouse_code": WAREHOUSE_CODE, field: value}))
        assert json.loads(result[0].text) == {
            "error": "validation_error", "message": f"{field} must be an integer"
        }

    @pytest.mark.parametrize("bad_limit", ["abc", None, 1.5, "1.5", True])
    def test_outbound_rejects_non_integer_limit(self, pagination_env, ctx, bad_limit):
        with pytest.raises(ValidationError) as excinfo:
            list_outbound(WAREHOUSE_CODE, ctx, limit=bad_limit)
        assert "limit" in str(excinfo.value)

    @pytest.mark.parametrize("bad_offset", ["x", None, 0.5])
    def test_outbound_rejects_non_integer_offset(self, pagination_env, ctx, bad_offset):
        with pytest.raises(ValidationError) as excinfo:
            list_outbound(WAREHOUSE_CODE, ctx, offset=bad_offset)
        assert "offset" in str(excinfo.value)

    @pytest.mark.parametrize("bad_limit", ["abc", None, 1.5])
    def test_restock_rejects_non_integer_limit(self, pagination_env, ctx, bad_limit):
        with pytest.raises(ValidationError) as excinfo:
            list_restock(WAREHOUSE_CODE, ctx, limit=bad_limit)
        assert "limit" in str(excinfo.value)

    @pytest.mark.parametrize("bad_offset", ["x", None, 0.5])
    def test_restock_rejects_non_integer_offset(self, pagination_env, ctx, bad_offset):
        with pytest.raises(ValidationError) as excinfo:
            list_restock(WAREHOUSE_CODE, ctx, offset=bad_offset)
        assert "offset" in str(excinfo.value)

    def test_tool_layer_surfaces_validation_error(self, pagination_env, ctx):
        """工具层不吞 ValidationError，协议层 call_tool 才能转成结构化错误。"""
        with pytest.raises(ValidationError):
            outbound_list_impl({"warehouse_code": WAREHOUSE_CODE, "limit": "abc"})
        with pytest.raises(ValidationError):
            restock_list_impl({"warehouse_code": WAREHOUSE_CODE, "offset": "x"})


# ---------------------------------------------------------------------------
# 工具层：schema + 端到端透传
# ---------------------------------------------------------------------------

class TestToolSchemas:
    @pytest.mark.parametrize("name", ["outbound_list", "restock_list"])
    def test_external_contract_matches_registered_pagination(self, name):
        path = Path(__file__).resolve().parents[1] / "docs/integrations/dailycheck-mcp/tools.json"
        external = {tool["name"]: tool for tool in json.loads(path.read_text())["tools"]}[name]
        registered = next(tool for tool in get_tools() if tool.name == name)
        for field in ("limit", "offset"):
            for key, value in _input_schema(registered)["properties"][field].items():
                if key != "description":
                    assert external["inputSchema"]["properties"][field][key] == value
        for field in ("items", "total", "limit", "offset", "truncated"):
            assert field in external["description"]
        assert "Returns: {items:" in external["description"]

    @pytest.mark.parametrize("name", ["outbound_list", "restock_list"])
    def test_schema_exposes_limit_and_offset(self, name):
        schemas = {t.name: _input_schema(t) for t in get_tools()}
        props = schemas[name]["properties"]
        assert props["limit"]["type"] == "integer"
        assert props["limit"]["minimum"] == 1
        assert props["limit"]["maximum"] == 500
        assert props["limit"]["default"] == 100
        assert props["offset"]["type"] == "integer"
        assert props["offset"]["minimum"] == 0
        assert props["offset"]["default"] == 0

    @pytest.mark.parametrize("name", ["outbound_list", "restock_list"])
    def test_description_documents_envelope(self, name):
        tools = {t.name: t for t in get_tools()}
        description = tools[name].description
        for token in ("items", "total", "limit", "offset", "truncated"):
            assert token in description
        assert "{items" in description


class TestToolImpls:
    def test_outbound_list_impl_passes_pagination(self, pagination_env, ctx):
        result = outbound_list_impl(
            {"warehouse_code": WAREHOUSE_CODE, "limit": 10, "offset": 5}
        )
        assert result["total"] == EXPECTED_OUTBOUND_ROWS
        assert result["limit"] == 10
        assert result["offset"] == 5
        assert len(result["items"]) == 10
        assert result["truncated"] is True

    def test_restock_list_impl_passes_pagination(self, pagination_env, ctx):
        result = restock_list_impl(
            {"warehouse_code": WAREHOUSE_CODE, "limit": 10, "offset": 5}
        )
        assert result["total"] == SEED_ROWS
        assert result["limit"] == 10
        assert result["offset"] == 5
        assert len(result["items"]) == 10
        assert result["truncated"] is True
