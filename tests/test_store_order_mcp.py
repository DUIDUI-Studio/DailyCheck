"""MCP 门店订单只读工具的集成测试。

覆盖 Issue #73 的验收点：
  - store_order_detail 返回「头 + items + deliveries + receipts + status_history」
    五段结构，items 含 canonical_name / quantity / fulfilled_quantity /
    dc_item_id 是否为 null；
  - 种子场景复现「一单 6 行明细、2 次发货、0 次收货」；
  - store_order_list 支持 warehouse_code / status / limit / offset 过滤；
  - ACL 三件套：范围外仓库订单不可见、非通配 read token 得 forbidden_path、
    非法 warehouse 得 forbidden_warehouse；
  - tools/list 可见两个新工具且 inputSchema 与实现一致。

种子数据构造方式参照 tests/test_store_ordering_pure.py 的 ordering_env
（真实 SQLite master.db，不依赖 Flask app）。
"""
from __future__ import annotations

import sqlite3

import pytest

from mcp_server.infra.errors import ForbiddenError, NotFoundError, ValidationError
from mcp_server.protocol.server import _tool_map
from mcp_server.protocol.tools import get_tools
from mcp_server.protocol.tools.store_order import (
    store_order_detail_impl,
    store_order_list_impl,
)
from mcp_server.service.auth import AuthContext, set_current_auth

TS = "2026-10-25 09:00:00"

# 仓库 code
WH_DC = "wh_dc"
WH_STORE = "wh_store"
WH_OTHER_DC = "wh_other_dc"
WH_OTHER_STORE = "wh_other_store"

# 订单号（验收种子单 = 6 行明细 / 2 次发货 / 0 次收货）
SEED_ORDER_NO = "SO-20261025-0001"


def _seed_master(master_path) -> None:
    """写入仓库 / 用户 / 主数据 / 门店订单五张表。"""
    conn = sqlite3.connect(str(master_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (1, 'admin', 'x', 1, ?)",
        (TS,),
    )
    conn.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (2, 'store_staff', 'x', 0, ?)",
        (TS,),
    )
    conn.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (3, 'dc_staff', 'x', 0, ?)",
        (TS,),
    )
    for wid, code, name, wtype in (
        (1, WH_DC, "配送中心", "distribution_center"),
        (2, WH_STORE, "门店A", "storefront"),
        (3, WH_OTHER_DC, "其他总仓", "distribution_center"),
        (4, WH_OTHER_STORE, "门店B", "storefront"),
    ):
        conn.execute(
            "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (wid, code, name, f"/tmp/{code}.db", wtype, TS),
        )
    conn.execute(
        "INSERT INTO canonical_categories (code, name, description, created_at, updated_at) "
        "VALUES ('PACKAGING', '包材', '', ?, ?)",
        (TS, TS),
    )
    for cid in range(101, 107):
        conn.execute(
            """INSERT INTO canonical_items
               (id, canonical_sku, name, category_code, unit, gram_per_unit, aux_unit,
                aux_rate, status, created_from, created_at, updated_at)
               VALUES (?, ?, ?, 'PACKAGING', '件', 0, NULL, 0, 'active', 'rd_manual', ?, ?)""",
            (cid, f"IC-{cid:06d}", f"测试包材{cid - 100}", TS, TS),
        )

    # ── 订单 1：验收种子单 —— 6 行明细、2 次发货、0 次收货 ─────────────
    conn.execute(
        """INSERT INTO store_orders
           (id, order_no, store_warehouse_code, dc_warehouse_code, flow_type, status,
            requested_by, expected_delivery_date, created_at, updated_at, shipped_by, shipped_at)
           VALUES (1, ?, ?, ?, 'store_order', 'shipped', 2, '2026-10-28', ?, ?, 3, ?)""",
        (SEED_ORDER_NO, WH_STORE, WH_DC, TS, TS, TS),
    )
    # 多仓履约 Task 10：行级来源仓让验收种子单成为跨仓单
    # （前 3 行由头仓 wh_dc 履约，后 3 行由 wh_other_dc 履约）。
    for canonical_id, qty, dc_item_id, source in [
        (101, 10.0, 501, WH_DC),
        (102, 20.0, 502, WH_DC),
        (103, 30.0, 503, WH_DC),
        (104, 40.0, 504, WH_OTHER_DC),
        (105, 50.0, None, WH_OTHER_DC),   # 尚未回填配送中心 items.id
        (106, 60.0, None, WH_OTHER_DC),
    ]:
        conn.execute(
            """INSERT INTO store_order_items
               (order_id, canonical_id, dc_item_id, store_item_id,
                source_warehouse_code, quantity, unit, fulfilled_quantity, status, created_at)
               VALUES (1, ?, ?, NULL, ?, ?, '件', 0, 'pending', ?)""",
            (canonical_id, dc_item_id, source, qty, TS),
        )
    for no, shipped_at, wh in (
        ("DN-20261025-0001", TS, WH_DC),
        ("DN-20261025-0002", TS, WH_OTHER_DC),
    ):
        conn.execute(
            """INSERT INTO store_order_deliveries
               (order_id, delivery_no, warehouse_code, shipped_by, shipped_at,
                tracking_note, created_at)
               VALUES (1, ?, ?, 3, ?, '首批', ?)""",
            (no, wh, shipped_at, TS),
        )
    for from_status, to_status in ((None, "pending"), ("pending", "approved"),
                                    ("approved", "shipped")):
        conn.execute(
            """INSERT INTO store_order_status_history
               (order_id, from_status, to_status, actor_id, note, created_at)
               VALUES (1, ?, ?, 2, '', ?)""",
            (from_status, to_status, TS),
        )

    # ── 订单 2：已收货 —— 用来验证 fulfilled_quantity / receipts ───────
    conn.execute(
        """INSERT INTO store_orders
           (id, order_no, store_warehouse_code, dc_warehouse_code, flow_type, status,
            requested_by, created_at, updated_at)
           VALUES (2, 'SO-20261020-0002', ?, ?, 'store_order', 'delivered', 2, ?, ?)""",
        (WH_STORE, WH_DC, "2026-10-20 09:00:00", "2026-10-20 09:00:00"),
    )
    for canonical_id, qty, fulfilled in ((101, 10.0, 10.0), (102, 5.0, 5.0)):
        conn.execute(
            """INSERT INTO store_order_items
               (order_id, canonical_id, dc_item_id, quantity, unit, fulfilled_quantity,
                status, created_at)
               VALUES (2, ?, 601, ?, '件', ?, 'fulfilled', ?)""",
            (canonical_id, qty, fulfilled, TS),
        )
        conn.execute(
            """INSERT INTO store_order_receipts
               (order_id, order_item_id, quantity, received_by, note, created_at)
               VALUES (2, (SELECT id FROM store_order_items WHERE order_id = 2 AND canonical_id = ?),
                       ?, 2, '', ?)""",
            (canonical_id, fulfilled, TS),
        )
    conn.execute(
        """INSERT INTO store_order_deliveries
           (order_id, delivery_no, shipped_by, shipped_at, created_at)
           VALUES (2, 'DN-20261020-0001', 3, ?, ?)""",
        (TS, TS),
    )

    # ── 订单 3：只命中 DC 侧（store 侧不可见） ─────────────────────────
    conn.execute(
        """INSERT INTO store_orders
           (id, order_no, store_warehouse_code, dc_warehouse_code, flow_type, status,
            requested_by, created_at, updated_at)
           VALUES (3, 'SO-20261018-0003', ?, ?, 'store_order', 'pending', 2, ?, ?)""",
        (WH_OTHER_STORE, WH_DC, "2026-10-18 09:00:00", "2026-10-18 09:00:00"),
    )
    # ── 订单 4：只命中 store 侧（DC 侧不可见） ─────────────────────────
    conn.execute(
        """INSERT INTO store_orders
           (id, order_no, store_warehouse_code, dc_warehouse_code, flow_type, status,
            requested_by, created_at, updated_at)
           VALUES (4, 'SO-20261017-0004', ?, ?, 'store_order', 'pending', 2, ?, ?)""",
        (WH_STORE, WH_OTHER_DC, "2026-10-17 09:00:00", "2026-10-17 09:00:00"),
    )
    # ── 订单 5：两侧都不可见 ───────────────────────────────────────────
    conn.execute(
        """INSERT INTO store_orders
           (id, order_no, store_warehouse_code, dc_warehouse_code, flow_type, status,
            requested_by, created_at, updated_at)
           VALUES (5, 'SO-20261016-0005', ?, ?, 'store_order', 'pending', 2, ?, ?)""",
        (WH_OTHER_STORE, WH_OTHER_DC, "2026-10-16 09:00:00", "2026-10-16 09:00:00"),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def store_order_env(tmp_path, monkeypatch):
    """真实 master.db 环境：门店订单只读域的种子数据。"""
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
    _seed_master(master_path)
    yield {"master_path": master_path}


@pytest.fixture
def auth_ctx():
    """安装请求级 AuthContext（工具层 resolve_ctx 依赖它）。"""
    installed: list[AuthContext] = []

    def _install(ctx: AuthContext) -> AuthContext:
        set_current_auth(ctx)
        installed.append(ctx)
        return ctx

    yield _install
    set_current_auth(None)


def _ctx(warehouses, read_paths=("/api/v1/store-orders",)) -> AuthContext:
    return AuthContext(
        token_id=1,
        allowed_read_paths=list(read_paths),
        allowed_write_paths=[],
        allowed_warehouses=warehouses,
    )


def _input_schema(tool) -> dict:
    """取 Tool 的 inputSchema（兼容 mcp 1.x / 2.x 的字段命名）。"""
    return getattr(tool, "inputSchema", None) or tool.input_schema


# ─────────────────────────────────────────────────────────────────────
# store_order_list
# ─────────────────────────────────────────────────────────────────────

class TestStoreOrderList:
    def test_visible_orders_respect_warehouse_scope(self, store_order_env, auth_ctx):
        """store 侧或 dc 侧任一命中 allowed_warehouses 即可见。"""
        auth_ctx(_ctx([WH_STORE, WH_DC]))
        result = store_order_list_impl({})
        order_ids = [o["id"] for o in result["orders"]]
        assert order_ids == [1, 2, 3, 4]
        assert 5 not in order_ids
        assert result["total"] == 4
        assert result["limit"] == 200
        assert result["offset"] == 0
        assert result["truncated"] is False

    def test_dc_side_hit_is_enough(self, store_order_env, auth_ctx):
        """只授权总仓时，通过 dc 侧命中的门店订单可见。"""
        auth_ctx(_ctx([WH_DC]))
        result = store_order_list_impl({})
        assert [o["id"] for o in result["orders"]] == [1, 2, 3]

    def test_store_side_hit_is_enough(self, store_order_env, auth_ctx):
        """只授权门店时，通过 store 侧命中的门店订单可见。"""
        auth_ctx(_ctx([WH_STORE]))
        result = store_order_list_impl({})
        assert [o["id"] for o in result["orders"]] == [1, 2, 4]

    def test_unrestricted_token_sees_everything(self, store_order_env, auth_ctx):
        """allowed_warehouses=None → 全可见。"""
        auth_ctx(_ctx(None))
        result = store_order_list_impl({})
        assert [o["id"] for o in result["orders"]] == [1, 2, 3, 4, 5]
        assert result["total"] == 5

    def test_warehouse_code_filter_matches_both_sides(self, store_order_env, auth_ctx):
        """warehouse_code 命中门店侧或总仓侧都算命中。"""
        auth_ctx(_ctx(None))
        by_dc = store_order_list_impl({"warehouse_code": WH_DC})
        assert [o["id"] for o in by_dc["orders"]] == [1, 2, 3]
        by_store = store_order_list_impl({"warehouse_code": WH_STORE})
        assert [o["id"] for o in by_store["orders"]] == [1, 2, 4]

    def test_warehouse_code_filter_still_obeys_scope(self, store_order_env, auth_ctx):
        """仓库过滤与 token 范围取交集：命中在范围内的仓 → 该单必然可见。"""
        auth_ctx(_ctx([WH_DC]))
        result = store_order_list_impl({"warehouse_code": WH_DC})
        assert [o["id"] for o in result["orders"]] == [1, 2, 3]
        assert result["total"] == 3
        # 未授权的仓不是「查不到」，而是直接 forbidden_warehouse。
        with pytest.raises(ForbiddenError):
            store_order_list_impl({"warehouse_code": WH_STORE})

    def test_status_filter(self, store_order_env, auth_ctx):
        auth_ctx(_ctx(None))
        result = store_order_list_impl({"status": "shipped"})
        assert [o["id"] for o in result["orders"]] == [1]
        assert result["total"] == 1

    def test_limit_offset_pagination(self, store_order_env, auth_ctx):
        auth_ctx(_ctx(None))
        page1 = store_order_list_impl({"limit": 2, "offset": 0})
        page2 = store_order_list_impl({"limit": 2, "offset": 2})
        page3 = store_order_list_impl({"limit": 2, "offset": 4})
        assert [o["id"] for o in page1["orders"]] == [1, 2]
        assert [o["id"] for o in page2["orders"]] == [3, 4]
        assert [o["id"] for o in page3["orders"]] == [5]
        assert page1["total"] == page2["total"] == page3["total"] == 5
        assert page1["truncated"] is True
        assert page3["truncated"] is False

    def test_order_rows_expose_header_fields(self, store_order_env, auth_ctx):
        auth_ctx(_ctx(None))
        result = store_order_list_impl({})
        order = result["orders"][0]
        assert order["order_no"] == SEED_ORDER_NO
        assert order["store_warehouse_code"] == WH_STORE
        assert order["dc_warehouse_code"] == WH_DC
        assert order["status"] == "shipped"
        assert order["flow_type"] == "store_order"


# ─────────────────────────────────────────────────────────────────────
# store_order_detail
# ─────────────────────────────────────────────────────────────────────

class TestStoreOrderDetail:
    def test_detail_returns_five_sections(self, store_order_env, auth_ctx):
        """头 + items + deliveries + receipts + status_history 五段结构。"""
        auth_ctx(_ctx(None))
        detail = store_order_detail_impl({"order_id": 1})
        assert set(detail) == {
            "order", "items", "deliveries", "receipts", "status_history",
        }
        assert detail["order"]["id"] == 1
        assert detail["order"]["order_no"] == SEED_ORDER_NO
        assert isinstance(detail["items"], list)
        assert isinstance(detail["deliveries"], list)
        assert isinstance(detail["receipts"], list)
        assert isinstance(detail["status_history"], list)

    def test_seed_order_shape(self, store_order_env, auth_ctx):
        """验收种子：一单 6 行明细、2 次发货、0 次收货可被完整读出。"""
        auth_ctx(_ctx(None))
        detail = store_order_detail_impl({"order_id": 1})
        assert len(detail["items"]) == 6
        assert len(detail["deliveries"]) == 2
        assert len(detail["receipts"]) == 0
        assert len(detail["status_history"]) == 3

    def test_items_expose_contract_fields(self, store_order_env, auth_ctx):
        """items 含 canonical_name / quantity / fulfilled_quantity / dc_item_id 空标记。"""
        auth_ctx(_ctx(None))
        detail = store_order_detail_impl({"order_id": 1})
        first = detail["items"][0]
        assert first["canonical_name"] == "测试包材1"
        assert first["quantity"] == 10.0
        assert first["fulfilled_quantity"] == 0.0
        assert first["dc_item_id"] == 501
        assert first["dc_item_bound"] is True
        # 未回填配送中心 items.id 的行：dc_item_id 为 null 且标记为 False
        unbound = [i for i in detail["items"] if i["dc_item_id"] is None]
        assert len(unbound) == 2
        assert all(i["dc_item_bound"] is False for i in unbound)

    def test_fulfilled_quantity_reflects_receipts(self, store_order_env, auth_ctx):
        auth_ctx(_ctx(None))
        detail = store_order_detail_impl({"order_id": 2})
        assert len(detail["receipts"]) == 2
        fulfilled = {i["canonical_id"]: i["fulfilled_quantity"] for i in detail["items"]}
        assert fulfilled == {101: 10.0, 102: 5.0}

    def test_out_of_scope_order_is_invisible(self, store_order_env, auth_ctx):
        """范围外仓库的订单不可见（与不存在同义）。"""
        auth_ctx(_ctx([WH_DC]))
        with pytest.raises(NotFoundError):
            store_order_detail_impl({"order_id": 4})

    def test_missing_order_raises_not_found(self, store_order_env, auth_ctx):
        auth_ctx(_ctx(None))
        with pytest.raises(NotFoundError):
            store_order_detail_impl({"order_id": 9999})

    def test_invalid_order_id_raises_validation_error(self, store_order_env, auth_ctx):
        auth_ctx(_ctx(None))
        with pytest.raises(ValidationError):
            store_order_detail_impl({"order_id": "abc"})
        with pytest.raises(ValidationError):
            store_order_detail_impl({})


# ─────────────────────────────────────────────────────────────────────
# ACL 三件套
# ─────────────────────────────────────────────────────────────────────

class TestStoreOrderAcl:
    def test_out_of_scope_orders_are_not_listed(self, store_order_env, auth_ctx):
        """ACL 用例一：范围外仓库的订单不出现在列表里。"""
        auth_ctx(_ctx([WH_OTHER_STORE]))
        result = store_order_list_impl({})
        order_ids = [o["id"] for o in result["orders"]]
        # 只剩 store 侧命中 wh_other_store 的两张单
        assert order_ids == [3, 5]
        assert 1 not in order_ids
        assert 2 not in order_ids
        assert 4 not in order_ids

    def test_non_wildcard_read_token_is_forbidden(self, store_order_env, auth_ctx):
        """ACL 用例二：非通配 read token 调新路径得 forbidden_path。"""
        auth_ctx(_ctx(None, read_paths=["/api/v1/items"]))
        with pytest.raises(ForbiddenError) as exc:
            store_order_list_impl({})
        assert exc.value.message == "forbidden_path"
        with pytest.raises(ForbiddenError) as exc:
            store_order_detail_impl({"order_id": 1})
        assert exc.value.message == "forbidden_path"

    def test_wildcard_read_path_is_allowed(self, store_order_env, auth_ctx):
        """通配 read path 覆盖新路径。"""
        auth_ctx(_ctx(None, read_paths=["/api/v1/store-orders/*"]))
        assert store_order_list_impl({})["total"] == 5
        assert store_order_detail_impl({"order_id": 1})["order"]["id"] == 1

    def test_illegal_warehouse_is_forbidden(self, store_order_env, auth_ctx):
        """ACL 用例三：非法 warehouse 得 forbidden_warehouse。"""
        auth_ctx(_ctx([WH_STORE]))
        with pytest.raises(ForbiddenError) as exc:
            store_order_list_impl({"warehouse_code": WH_OTHER_STORE})
        assert exc.value.message == "forbidden_warehouse"

    def test_unknown_warehouse_is_not_found(self, store_order_env, auth_ctx):
        """在授权范围内的未知仓库 → warehouse_not_found。"""
        auth_ctx(_ctx([WH_STORE, "wh_missing"]))
        with pytest.raises(NotFoundError):
            store_order_list_impl({"warehouse_code": "wh_missing"})


# ─────────────────────────────────────────────────────────────────────
# 跨仓可见性（Task 10 / spec §10）
# ─────────────────────────────────────────────────────────────────────

class TestCrossWarehouseVisibility:
    """ACL 语义扩展：命中任一参与仓 → 可读全单（含他仓行/收货历史）。"""

    def test_b_only_token_sees_cross_order_and_full_detail(self, store_order_env, auth_ctx):
        """只授权 wh_other_dc 也能看到 wh_dc 头仓的跨仓单，并读到全部 6 行。"""
        auth_ctx(_ctx([WH_OTHER_DC]))
        result = store_order_list_impl({})
        assert 1 in [o["id"] for o in result["orders"]]

        detail = store_order_detail_impl({"order_id": 1})
        assert detail["order"]["id"] == 1
        assert detail["order"]["dc_warehouse_code"] == WH_DC
        # 全单：包括由他仓 wh_dc 履约的行
        assert len(detail["items"]) == 6
        assert {i["source_warehouse_code"] for i in detail["items"]} == {
            WH_DC, WH_OTHER_DC,
        }
        # 批次仓库是物流事实，不按 token 范围裁剪
        assert {d["warehouse_code"] for d in detail["deliveries"]} == {
            WH_DC, WH_OTHER_DC,
        }

    def test_dual_authorization_dedupes_the_order(self, store_order_env, auth_ctx):
        """两个参与仓同时授权时，同一订单只出现一次。"""
        auth_ctx(_ctx([WH_DC, WH_OTHER_DC]))
        result = store_order_list_impl({})
        ids = [o["id"] for o in result["orders"]]
        assert ids.count(1) == 1
        assert result["total"] == len(ids)

    def test_explicit_participant_warehouse_filter(self, store_order_env, auth_ctx):
        """warehouse_code 以「参与仓」口径过滤，而不是只看头仓。"""
        auth_ctx(_ctx([WH_DC, WH_OTHER_DC]))
        by_beta = store_order_list_impl({"warehouse_code": WH_OTHER_DC})
        ids = [o["id"] for o in by_beta["orders"]]
        assert 1 in ids, "wh_other_dc 是订单 1 的参与仓（3 行来源仓）"
        assert 2 not in ids, "订单 2 只有 wh_dc 参与"
        assert by_beta["total"] == len(ids)

    def test_empty_scope_returns_nothing(self, store_order_env, auth_ctx):
        auth_ctx(_ctx([]))
        result = store_order_list_impl({})
        assert result == {
            "orders": [], "total": 0, "limit": 200, "offset": 0, "truncated": False,
        }

    def test_pagination_and_total_are_consistent(self, store_order_env, auth_ctx):
        """逐页拼接结果 == 一次取全；total/truncated 与页边界一致。"""
        auth_ctx(_ctx([WH_OTHER_DC]))
        full = store_order_list_impl({})
        assert full["truncated"] is False

        collected: list[int] = []
        offset = 0
        while True:
            page = store_order_list_impl({"limit": 1, "offset": offset})
            assert page["total"] == full["total"]
            collected.extend(o["id"] for o in page["orders"])
            if not page["truncated"]:
                break
            offset += 1
        assert collected == [o["id"] for o in full["orders"]]

    def test_list_count_and_detail_agree_on_membership(self, store_order_env, auth_ctx):
        """列表/计数/详情三处可见性收敛到同一谓词。"""
        auth_ctx(_ctx([WH_OTHER_DC]))
        result = store_order_list_impl({"limit": 500})
        assert result["total"] == len(result["orders"])
        for order in result["orders"]:
            detail = store_order_detail_impl({"order_id": order["id"]})
            assert detail["order"]["id"] == order["id"]


# ─────────────────────────────────────────────────────────────────────
# 工具注册（tools/list）
# ─────────────────────────────────────────────────────────────────────

class TestStoreOrderToolRegistry:
    def test_both_tools_are_listed(self):
        names = [t.name for t in get_tools()]
        assert "store_order_list" in names
        assert "store_order_detail" in names

    def test_tools_are_routable(self):
        assert _tool_map["store_order_list"] is store_order_list_impl
        assert _tool_map["store_order_detail"] is store_order_detail_impl

    def test_input_schema_matches_implementation(self):
        schemas = {t.name: _input_schema(t) for t in get_tools()}
        list_schema = schemas["store_order_list"]
        assert list_schema["required"] == []
        assert set(list_schema["properties"]) == {
            "warehouse_code", "status", "limit", "offset",
        }
        detail_schema = schemas["store_order_detail"]
        assert detail_schema["required"] == ["order_id"]
        assert set(detail_schema["properties"]) == {"order_id"}

    def test_descriptions_declare_participant_acl_expansion(self):
        """ACL 扩权语义必须写进 description（spec §10）。"""
        descriptions = {t.name: t.description for t in get_tools()}
        list_desc = descriptions["store_order_list"].lower()
        assert "participating warehouse" in list_desc
        assert "full order" in list_desc
        detail_desc = descriptions["store_order_detail"].lower()
        assert "participating warehouse" in detail_desc
        assert "full order" in detail_desc
