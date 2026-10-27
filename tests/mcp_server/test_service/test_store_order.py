"""Store-order service-layer tests.

Targets mcp_server.service.store_order — pure unit tests with mocks.
We do NOT exercise blueprints.store_ordering_pure directly; we verify the
service layer raises the right MCP errors, translates the OR-shaped
warehouse scope into the AND-shaped filters sop.list_orders understands,
and shapes the returned rows into the MCP contract.
"""
from __future__ import annotations

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
# _scope_pairs —— OR 可见性 → AND 过滤器组合
# ─────────────────────────────────────────────────────────────────────

class TestScopePairs:
    def test_unrestricted_token_needs_no_filter(self):
        assert svc._scope_pairs(None, None) == [(None, None)]

    def test_unrestricted_token_with_warehouse_filter(self):
        assert svc._scope_pairs(None, "WH001") == [
            ("WH001", None),
            (None, "WH001"),
        ]

    def test_store_or_dc_expands_to_two_calls_per_code(self):
        assert svc._scope_pairs(["WH001", "WH002"], None) == [
            ("WH001", None),
            (None, "WH001"),
            ("WH002", None),
            (None, "WH002"),
        ]

    def test_warehouse_filter_crosses_with_scope(self):
        """(store∈V ∪ dc∈V) ∧ (store=W ∪ dc=W) 展开成 4 个 AND 组合。"""
        assert svc._scope_pairs(["WH001", "WH002"], "WH001") == [
            ("WH001", None),
            (None, "WH001"),
            ("WH002", "WH001"),
            ("WH001", "WH002"),
        ]


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
            "deliveries": [{"id": 21, "delivery_no": "DN-1"}],
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
        assert result["deliveries"] == [{"id": 21, "delivery_no": "DN-1"}]
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
        conn = _mock_conn()
        mock_conn.return_value = conn
        detail = {
            "id": 4,
            "store_warehouse_code": "WH001",
            "dc_warehouse_code": "WH999",
        }
        with patch(
            "mcp_server.service.store_order.sop.get_order_detail",
            return_value=detail,
        ):
            # token 只授权 WH003：store 侧 WH001、dc 侧 WH999 都不在范围内 → 不可见
            with pytest.raises(NotFoundError):
                svc.get_store_order_detail(_ctx(["WH003"]), 4)

    @patch("mcp_server.service.store_order.master_connection")
    def test_dc_side_hit_makes_order_visible(self, mock_conn):
        conn = _mock_conn()
        mock_conn.return_value = conn
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

    def test_invalid_order_id_raises_validation_error(self):
        with pytest.raises(ValidationError):
            svc.get_store_order_detail(_ctx(None), "not-a-number")
