"""Store-order service-layer tests.

Targets mcp_server.service.store_order — unit tests with a mix of mocks and
real in-memory SQLite. We verify the service layer raises the right MCP
errors, uses the shared "participating warehouse" SQL predicate for list /
count / detail, and shapes the returned rows into the MCP contract.
"""
from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from mcp_server.infra.errors import (
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from mcp_server.service import store_order as svc
from mcp_server.service.auth import AuthContext


def _ctx(warehouses: list[str] | None) -> AuthContext:
    return AuthContext(
        token_id=1,
        allowed_read_paths=["/api/v1/store-orders"],
        allowed_write_paths=[],
        allowed_warehouses=warehouses,
    )


def _mock_conn():
    """A master_connection() context manager whose conn is a MagicMock."""
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn


class _RealConnCtx:
    """Expose a real sqlite connection through the master_connection ctx API."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __enter__(self) -> sqlite3.Connection:
        return self._conn

    def __exit__(self, *exc: object) -> bool:
        return False


def _visibility_db(
    rows: list[tuple[int, str | None, str | None, list[str | None]]],
) -> sqlite3.Connection:
    """In-memory subset of master.db: store_orders + store_order_items."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE store_orders (
            id INTEGER PRIMARY KEY,
            store_warehouse_code TEXT,
            dc_warehouse_code TEXT
        );
        CREATE TABLE store_order_items (
            order_id INTEGER,
            source_warehouse_code TEXT
        );
        """
    )
    for order_id, store_code, dc_code, sources in rows:
        conn.execute(
            "INSERT INTO store_orders VALUES (?, ?, ?)",
            (order_id, store_code, dc_code),
        )
        for source in sources:
            conn.execute(
                "INSERT INTO store_order_items VALUES (?, ?)",
                (order_id, source),
            )
    return conn


def _order(order_id: int, created_at: str, **extra) -> dict:
    row = {
        "id": order_id,
        "order_no": f"SO-{order_id}",
        "created_at": created_at,
        "status": "pending",
    }
    row.update(extra)
    return row


# ─────────────────────────────────────────────────────────────────────
# _scope_filters —— 可见性 → list_orders 调用参数
# ─────────────────────────────────────────────────────────────────────

class TestScopeFilters:
    def test_unrestricted_token_needs_no_filter(self):
        assert svc._scope_filters(None, None) == [{}]

    def test_warehouse_filter_uses_store_and_participant_scopes(self):
        assert svc._scope_filters(None, "WH001") == [
            {"store_warehouse_code": "WH001"},
            {"participant_warehouse_code": "WH001"},
        ]

    def test_each_allowed_code_expands_to_store_and_participant(self):
        assert svc._scope_filters(["WH002", "WH001"], None) == [
            {"store_warehouse_code": "WH001"},
            {"participant_warehouse_code": "WH001"},
            {"store_warehouse_code": "WH002"},
            {"participant_warehouse_code": "WH002"},
        ]

    def test_empty_scope_has_no_scopes(self):
        assert svc._scope_filters([], None) == []


# ─────────────────────────────────────────────────────────────────────
# _visibility_sql —— 列表/计数/详情共用的参与仓谓词
# ─────────────────────────────────────────────────────────────────────

class TestVisibilityPredicate:
    @pytest.fixture
    def conn(self):
        conn = _visibility_db([
            # 跨仓单：门店 S、头仓 HEAD，行来源 A + B
            (1, "S", "HEAD", ["A", "B"]),
            # 迁移前历史行：来源 NULL → COALESCE 头仓
            (2, "S", "HEAD", [None]),
            # 无明细行存量单：只有头仓可用
            (3, "S", "HEAD", []),
        ])
        yield conn
        conn.close()

    def _visible(self, conn, order_id: int, code: str) -> bool:
        row = conn.execute(
            f"SELECT 1 FROM store_orders so WHERE so.id = ? "
            f"AND {svc._visibility_sql('so', 1)}",
            [order_id, *svc._visibility_params([code])],
        ).fetchone()
        return row is not None

    @pytest.mark.parametrize(
        ("order_id", "code", "expected"),
        [
            (1, "S", True),
            (1, "A", True),      # 参与仓 A 命中即整单可见
            (1, "B", True),
            (1, "HEAD", False),  # 有明细行时头仓不再兜底
            (1, "C", False),
            (2, "HEAD", True),   # NULL 来源回落头仓
            (2, "A", False),
            (3, "HEAD", True),   # 无明细行存量单
            (3, "A", False),
        ],
    )
    def test_predicate_membership(self, conn, order_id, code, expected):
        assert self._visible(conn, order_id, code) is expected

    def test_multi_line_same_warehouse_counts_once(self, conn):
        count = conn.execute(
            f"SELECT COUNT(*) FROM store_orders so WHERE "
            f"{svc._visibility_sql('so', 1)}",
            svc._visibility_params(["A"]),
        ).fetchone()[0]
        assert count == 1


# ─────────────────────────────────────────────────────────────────────
# list_store_orders
# ─────────────────────────────────────────────────────────────────────

class TestListStoreOrders:
    @patch("mcp_server.service.store_order.master_connection")
    def test_returns_orders_total_limit_offset(self, mock_conn):
        conn = _mock_conn()
        mock_conn.return_value = conn
        with patch(
            "mcp_server.service.store_order.sop.list_orders",
            return_value=[_order(1, "2026-10-25 09:00:00")],
        ), patch(
            "mcp_server.service.store_order._count_orders", return_value=1
        ):
            result = svc.list_store_orders(_ctx(None))
        assert [o["id"] for o in result["orders"]] == [1]
        assert result["total"] == 1
        assert result["limit"] == 200
        assert result["offset"] == 0
        assert result["truncated"] is False

    @patch("mcp_server.service.store_order.master_connection")
    def test_limit_is_clamped_to_max(self, mock_conn):
        conn = _mock_conn()
        mock_conn.return_value = conn
        with patch(
            "mcp_server.service.store_order.sop.list_orders", return_value=[]
        ) as ilist, patch(
            "mcp_server.service.store_order._count_orders", return_value=0
        ):
            svc.list_store_orders(_ctx(None), limit=9999)
        # 每个子集各取 limit + offset 行
        assert ilist.call_args.kwargs["limit"] == svc.MAX_LIMIT
        assert ilist.call_args.kwargs["offset"] == 0

    @patch("mcp_server.service.store_order.master_connection")
    def test_limit_minimum_is_one(self, mock_conn):
        conn = _mock_conn()
        mock_conn.return_value = conn
        with patch(
            "mcp_server.service.store_order.sop.list_orders", return_value=[]
        ) as ilist, patch(
            "mcp_server.service.store_order._count_orders", return_value=0
        ):
            svc.list_store_orders(_ctx(None), limit=0)
        assert ilist.call_args.kwargs["limit"] == 1

    @patch("mcp_server.service.store_order.master_connection")
    def test_offset_is_applied_after_merge(self, mock_conn):
        conn = _mock_conn()
        mock_conn.return_value = conn
        rows = [
            _order(3, "2026-10-18 09:00:00"),
            _order(2, "2026-10-20 09:00:00"),
            _order(1, "2026-10-25 09:00:00"),
        ]
        with patch(
            "mcp_server.service.store_order.sop.list_orders", return_value=rows
        ), patch(
            "mcp_server.service.store_order._count_orders", return_value=3
        ):
            result = svc.list_store_orders(_ctx(None), limit=1, offset=1)
        # 合并后按 created_at DESC, id DESC 排序，再切片
        assert [o["id"] for o in result["orders"]] == [2]
        assert result["offset"] == 1
        assert result["truncated"] is True

    @patch("mcp_server.service.store_order.master_connection")
    def test_duplicate_rows_across_scope_subsets_are_deduped(self, mock_conn):
        conn = _mock_conn()
        mock_conn.return_value = conn
        row = _order(1, "2026-10-25 09:00:00")
        with patch(
            "mcp_server.service.store_order.sop.list_orders", return_value=[row]
        ) as ilist, patch(
            "mcp_server.service.store_order._count_orders", return_value=1
        ):
            result = svc.list_store_orders(_ctx(["WH001"]))
        assert len(ilist.call_args_list) == 2  # store 侧 + dc 侧
        assert len(result["orders"]) == 1

    def test_warehouse_out_of_scope_is_forbidden(self):
        with pytest.raises(ForbiddenError) as exc:
            svc.list_store_orders(_ctx(["WH001"]), warehouse_code="WH999")
        assert exc.value.message == "forbidden_warehouse"

    @patch("mcp_server.service.store_order.resolve_warehouse", return_value=None)
    def test_unknown_warehouse_is_not_found(self, _mock_resolve):
        with pytest.raises(NotFoundError):
            svc.list_store_orders(_ctx(None), warehouse_code="WH404")

    @patch("mcp_server.service.store_order.master_connection")
    def test_status_filter_is_forwarded(self, mock_conn):
        conn = _mock_conn()
        mock_conn.return_value = conn
        with patch(
            "mcp_server.service.store_order.sop.list_orders", return_value=[]
        ) as ilist, patch(
            "mcp_server.service.store_order._count_orders", return_value=0
        ):
            svc.list_store_orders(_ctx(None), status="shipped")
        assert ilist.call_args.kwargs["status"] == "shipped"


# ─────────────────────────────────────────────────────────────────────
# get_store_order_detail
# ─────────────────────────────────────────────────────────────────────

class TestStoreOrderDetail:
    @patch("mcp_server.service.store_order.master_connection")
    def test_returns_five_sections(self, mock_conn):
        conn = _mock_conn()
        mock_conn.return_value = conn
        detail = {
            "id": 1,
            "order_no": "SO-20261025-0001",
            "store_warehouse_code": "WH001",
            "dc_warehouse_code": "WH002",
            "order_items": [
                {
                    "id": 11,
                    "canonical_id": 101,
                    "canonical_name": "包材A",
                    "dc_item_id": None,
                    "quantity": 10.0,
                    "fulfilled_quantity": 0.0,
                }
            ],
            "deliveries": [{"id": 21, "delivery_no": "DN-1", "warehouse_code": "WH002"}],
            "receipts": [],
            "status_history": [{"id": 31, "to_status": "shipped"}],
        }
        with patch(
            "mcp_server.service.store_order.sop.get_order_detail",
            return_value=detail,
        ):
            result = svc.get_store_order_detail(_ctx(None), 1)
        assert set(result) == {
            "order", "items", "deliveries", "receipts", "status_history",
        }
        assert result["order"]["order_no"] == "SO-20261025-0001"
        assert "order_items" not in result["order"]
        assert result["items"][0]["canonical_name"] == "包材A"
        assert result["items"][0]["dc_item_id"] is None
        assert result["items"][0]["dc_item_bound"] is False
        assert result["items"][0]["quantity"] == 10.0
        assert result["items"][0]["fulfilled_quantity"] == 0.0
        assert result["deliveries"] == [
            {"id": 21, "delivery_no": "DN-1", "warehouse_code": "WH002"}
        ]
        assert result["receipts"] == []
        assert result["status_history"] == [{"id": 31, "to_status": "shipped"}]

    @patch("mcp_server.service.store_order.master_connection")
    def test_bound_dc_item_is_flagged(self, mock_conn):
        conn = _mock_conn()
        mock_conn.return_value = conn
        detail = {
            "id": 1,
            "store_warehouse_code": "WH001",
            "dc_warehouse_code": "WH002",
            "order_items": [
                {"id": 11, "dc_item_id": 501, "quantity": 1, "fulfilled_quantity": 0}
            ],
        }
        with patch(
            "mcp_server.service.store_order.sop.get_order_detail",
            return_value=detail,
        ):
            result = svc.get_store_order_detail(_ctx(None), 1)
        assert result["items"][0]["dc_item_bound"] is True
        assert result["items"][0]["dc_item_id"] == 501

    @patch("mcp_server.service.store_order.master_connection")
    def test_missing_order_raises_not_found(self, mock_conn):
        conn = _mock_conn()
        mock_conn.return_value = conn
        with patch(
            "mcp_server.service.store_order.sop.get_order_detail",
            return_value=None,
        ):
            with pytest.raises(NotFoundError):
                svc.get_store_order_detail(_ctx(None), 999)

    @patch("mcp_server.service.store_order.master_connection")
    def test_out_of_scope_order_is_invisible(self, mock_conn):
        conn = _visibility_db([(4, "WH001", "WH999", [])])
        mock_conn.return_value = _RealConnCtx(conn)
        detail = {
            "id": 4,
            "store_warehouse_code": "WH001",
            "dc_warehouse_code": "WH999",
        }
        with patch(
            "mcp_server.service.store_order.sop.get_order_detail",
            return_value=detail,
        ):
            # token 只授权 WH003：门店 WH001、头仓 WH999 都不在范围内 → 不可见
            with pytest.raises(NotFoundError):
                svc.get_store_order_detail(_ctx(["WH003"]), 4)

    @patch("mcp_server.service.store_order.master_connection")
    def test_legacy_header_hit_makes_order_visible(self, mock_conn):
        conn = _visibility_db([(3, "WH999", "WH002", [])])
        mock_conn.return_value = _RealConnCtx(conn)
        detail = {
            "id": 3,
            "store_warehouse_code": "WH999",
            "dc_warehouse_code": "WH002",
        }
        with patch(
            "mcp_server.service.store_order.sop.get_order_detail",
            return_value=detail,
        ):
            result = svc.get_store_order_detail(_ctx(["WH002"]), 3)
        assert result["order"]["id"] == 3

    @patch("mcp_server.service.store_order.master_connection")
    def test_any_participant_grants_the_full_order(self, mock_conn):
        """B-only token 读跨仓单：详情返回他仓行、金额与收货历史。"""
        conn = _visibility_db([(1, "STORE", "WH_A", ["WH_A", "WH_B"])])
        mock_conn.return_value = _RealConnCtx(conn)
        detail = {
            "id": 1,
            "store_warehouse_code": "STORE",
            "dc_warehouse_code": "WH_A",
            "order_items": [
                {"id": 11, "source_warehouse_code": "WH_A", "quantity": 1.0},
                {"id": 12, "source_warehouse_code": "WH_B", "quantity": 2.0},
            ],
            "deliveries": [{"id": 21, "warehouse_code": "WH_A"}],
            "receipts": [{"id": 31, "quantity": 1.0}],
            "status_history": [],
        }
        with patch(
            "mcp_server.service.store_order.sop.get_order_detail",
            return_value=detail,
        ):
            result = svc.get_store_order_detail(_ctx(["WH_B"]), 1)
        assert {i["source_warehouse_code"] for i in result["items"]} == {
            "WH_A", "WH_B",
        }
        assert result["deliveries"][0]["warehouse_code"] == "WH_A"
        assert result["receipts"] == [{"id": 31, "quantity": 1.0}]

    def test_invalid_order_id_raises_validation_error(self):
        with pytest.raises(ValidationError):
            svc.get_store_order_detail(_ctx(None), "not-a-number")
