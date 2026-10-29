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

import sqlite3

import pytest

from mcp_server.data import warehouse
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


def _input_schema(tool) -> dict:
    """取 Tool 的 inputSchema（兼容 mcp 1.x / 2.x 的字段命名）。"""
    return getattr(tool, "inputSchema", None) or tool.input_schema


@pytest.fixture
def pagination_env(tmp_path, monkeypatch):
    """真实 master.db + warehouse.db，种 120 条出库 / 120 条入库流水。"""
    import config as config_module
    import db as db_module
    import mcp_server.data.unit_of_work as uow

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
    for i in range(1, SEED_ROWS + 1):
        conn.execute(
            "INSERT INTO outbound_requests "
            "(item_id, requested_quantity, reason, status, rolled_back, created_at) "
            "VALUES (?, ?, ?, '出库', 0, ?)",
            (SEED_ITEM_ID, float(i), f"outbound-{i}", TS),
        )
        conn.execute(
            "INSERT INTO stock_movements (item_id, action, delta, note, created_at) "
            "VALUES (?, 'restock', ?, ?, ?)",
            (SEED_ITEM_ID, float(i), f"restock-{i}", TS),
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
    """用 offset 逐页翻完，返回（各页 items，拼接后的 items）。"""
    pages: list[list[dict]] = []
    combined: list[dict] = []
    offset = 0
    while True:
        page = fetch_page(limit=page_size, offset=offset)
        items = page["items"]
        pages.append(items)
        combined.extend(items)
        if not items:
            break
        offset += page_size
        if offset > SEED_ROWS * 2:  # 防死循环护栏，正常远到不了
            raise AssertionError("pagination did not terminate")
    return pages, combined


# ---------------------------------------------------------------------------
# outbound_list
# ---------------------------------------------------------------------------

class TestOutboundListPagination:
    def test_pages_cover_full_set_without_duplicates_or_gaps(self, pagination_env, ctx):
        full = list_outbound(WAREHOUSE_CODE, ctx, limit=500, offset=0)
        assert full["total"] == SEED_ROWS
        assert len(full["items"]) == SEED_ROWS

        pages, combined = _walk_pages(
            lambda limit, offset: list_outbound(
                WAREHOUSE_CODE, ctx, limit=limit, offset=offset
            )
        )

        full_ids = [row["id"] for row in full["items"]]
        combined_ids = [row["id"] for row in combined]
        assert combined_ids == full_ids
        assert len(combined_ids) == len(set(combined_ids)) == SEED_ROWS

    def test_total_is_constant_and_truncated_flips_on_last_page(self, pagination_env, ctx):
        pages, _ = _walk_pages(
            lambda limit, offset: list_outbound(
                WAREHOUSE_CODE, ctx, limit=limit, offset=offset
            ),
            page_size=50,
        )
        envelopes = [
            list_outbound(WAREHOUSE_CODE, ctx, limit=50, offset=offset)
            for offset in range(0, SEED_ROWS, 50)
        ]
        assert [p["total"] for p in envelopes] == [SEED_ROWS] * len(envelopes)
        assert envelopes[0]["truncated"] is True
        assert envelopes[-1]["truncated"] is False
        # 首页/末页条数也钉住，避免 truncated 恒 True/False 的假绿
        assert len(pages[0]) == 50
        assert len(pages[-2]) == 20

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
        assert len(upper["items"]) == SEED_ROWS

    def test_data_layer_count_matches_rows(self, pagination_env):
        conn = sqlite3.connect(pagination_env["wh_path"])
        conn.row_factory = sqlite3.Row
        try:
            assert warehouse.count_outbound(conn) == SEED_ROWS
            rows = warehouse.list_outbound(conn, limit=10, offset=5)
            assert len(rows) == 10
            assert warehouse.count_outbound(conn) == SEED_ROWS
        finally:
            conn.close()


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
        assert len(pages[0]) == 50
        assert len(pages[-2]) == 20

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
            assert warehouse.count_restock_movements(conn) == SEED_ROWS
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 工具层：schema + 端到端透传
# ---------------------------------------------------------------------------

class TestToolSchemas:
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
        assert result["total"] == SEED_ROWS
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
