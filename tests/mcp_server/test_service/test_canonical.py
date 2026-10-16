"""Canonical service-layer tests.

Targets mcp_server.service.canonical — pure unit tests with mocks.
We do NOT exercise blueprints.canonical_pure directly; we verify the
service layer raises the right MCP errors and delegates correctly.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from mcp_server.infra.errors import (
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from mcp_server.service import canonical as svc
from mcp_server.service.auth import AuthContext

# ─────────────────────────────────────────────────────────────────────
# list_canonical
# ─────────────────────────────────────────────────────────────────────

class TestListCanonical:
    @patch("mcp_server.service.canonical.master_connection")
    def test_returns_items_total_and_categories(self, mock_conn):
        ctx_mock = MagicMock()
        ctx_mock.__enter__ = MagicMock(return_value=ctx_mock)
        ctx_mock.__exit__ = MagicMock(return_value=False)
        mock_conn.return_value = ctx_mock

        # Two list_cp.list_canonical_items calls + count + categories
        # are routed through the same connection; fake them all.
        ctx_mock.execute.return_value.fetchall.return_value = []
        with patch(
            "mcp_server.service.canonical.cp.list_canonical_items",
            return_value=[{"id": 1, "canonical_sku": "IC-000001"}],
        ), patch(
            "mcp_server.service.canonical.cp.count_canonical_items",
            return_value=1,
        ), patch(
            "mcp_server.service.canonical.cp.list_canonical_categories",
            return_value=[{"code": "PRODUCE_CONSUMABLE", "name": "生产消耗品", "item_count": 1}],
        ):
            result = svc.list_canonical(q="萝卜", limit=50)
        assert result["items"] == [{"id": 1, "canonical_sku": "IC-000001"}]
        assert result["total"] == 1
        assert result["limit"] == 50
        assert result["offset"] == 0
        assert result["truncated"] is False
        assert result["categories"][0]["code"] == "PRODUCE_CONSUMABLE"

    @patch("mcp_server.service.canonical.master_connection")
    def test_limit_clamped_to_max(self, mock_conn):
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
        mock_ctx.__exit__ = MagicMock(return_value=False)
        mock_conn.return_value = mock_ctx
        with patch(
            "mcp_server.service.canonical.cp.list_canonical_items",
            return_value=[],
        ) as ilist, patch(
            "mcp_server.service.canonical.cp.count_canonical_items",
            return_value=0,
        ), patch(
            "mcp_server.service.canonical.cp.list_canonical_categories",
            return_value=[],
        ):
            svc.list_canonical(limit=9999)
        # The service caps limit at 500 before delegating.
        assert ilist.call_args.kwargs["limit"] == 500


# ─────────────────────────────────────────────────────────────────────
# get_canonical_detail
# ─────────────────────────────────────────────────────────────────────

class TestCanonicalDetail:
    @patch("mcp_server.service.canonical.cp.collect_bindings", return_value={})
    @patch("mcp_server.service.canonical.master_connection")
    def test_returns_canonical_with_aliases_and_bindings(self, mock_conn, _b):
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=ctx)
        ctx.__exit__ = MagicMock(return_value=False)
        mock_conn.return_value = ctx
        row = {
            "id": 1, "canonical_sku": "IC-000001", "name": "萝卜",
            "status": "active", "aliases": [],
        }
        with patch(
            "mcp_server.service.canonical.cp.get_canonical_item_detail",
            return_value=row,
        ):
            result = svc.get_canonical_detail(1)
        assert result["canonical"] == row
        assert result["bindings"] == []
        assert result["bound_warehouse_count"] == 0

    @patch("mcp_server.service.canonical.cp.collect_bindings", return_value={
        "wh_001": [{"canonical_id": 1, "id": 11, "sku": "WP01", "name": "A",
                    "unit": "kg", "is_alias": 0, "canonical_status": "active",
                    "is_store_exclusive": 0, "is_active": 1,
                    "warehouse_type": "storefront"}],
        "wh_000": [{"canonical_id": 1, "id": 21, "sku": "AUTO-IC-000001",
                    "name": "A", "unit": "kg", "is_alias": 0,
                    "canonical_status": "active",
                    "is_store_exclusive": 0, "is_active": 1,
                    "warehouse_type": "distribution_center"}],
    })
    @patch("mcp_server.service.canonical.master_connection")
    def test_filters_bindings_by_canonical_id(self, mock_conn, _b):
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=ctx)
        ctx.__exit__ = MagicMock(return_value=False)
        mock_conn.return_value = ctx
        with patch(
            "mcp_server.service.canonical.cp.get_canonical_item_detail",
            return_value={"id": 1, "name": "X", "aliases": []},
        ):
            result = svc.get_canonical_detail(1)
        assert len(result["bindings"]) == 2
        assert result["bound_warehouse_count"] == 2

    @patch("mcp_server.service.canonical.master_connection")
    def test_unknown_canonical_id_raises_not_found(self, mock_conn):
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=ctx)
        ctx.__exit__ = MagicMock(return_value=False)
        mock_conn.return_value = ctx
        with patch(
            "mcp_server.service.canonical.cp.get_canonical_item_detail",
            side_effect=ValueError("canonical_id=999 不存在"),
        ):
            with pytest.raises(NotFoundError):
                svc.get_canonical_detail(999)


# ─────────────────────────────────────────────────────────────────────
# create_canonical
# ─────────────────────────────────────────────────────────────────────

class TestCreateCanonical:
    def test_missing_name_rejected(self):
        with pytest.raises(ValidationError):
            svc.create_canonical(name="", unit="kg")

    def test_missing_unit_rejected(self):
        with pytest.raises(ValidationError):
            svc.create_canonical(name="X", unit="")

    def test_negative_price_rejected(self):
        with pytest.raises(ValidationError):
            svc.create_canonical(name="X", unit="kg", selling_price=-1)

    def test_negative_unit_cost_rejected(self):
        with pytest.raises(ValidationError):
            svc.create_canonical(name="X", unit="kg", unit_cost=-0.5)

    @patch("mcp_server.service.canonical.master_connection")
    def test_delegates_with_normalized_args(self, mock_conn):
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=ctx)
        ctx.__exit__ = MagicMock(return_value=False)
        mock_conn.return_value = ctx
        with patch(
            "mcp_server.service.canonical.cp.create_canonical_item",
            return_value={"id": 7, "canonical_sku": "IC-000007"},
        ) as create_fn:
            result = svc.create_canonical(
                name="  萝卜  ", unit="  kg  ",
                category_code="PRODUCE_CONSUMABLE",
                selling_price=10.0, actor_id=42,
            )
        assert result["canonical_sku"] == "IC-000007"
        kwargs = create_fn.call_args.kwargs
        assert kwargs["name"] == "萝卜"
        assert kwargs["unit"] == "kg"
        assert kwargs["category_code"] == "PRODUCE_CONSUMABLE"
        assert kwargs["selling_price"] == 10.0
        assert kwargs["created_from"] == "mcp"
        assert kwargs["created_by"] == 42

    @patch("mcp_server.service.canonical.master_connection")
    def test_pure_value_error_becomes_validation_error(self, mock_conn):
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=ctx)
        ctx.__exit__ = MagicMock(return_value=False)
        mock_conn.return_value = ctx
        # cp.create_canonical_item validates status internally.
        with patch(
            "mcp_server.service.canonical.cp.create_canonical_item",
            side_effect=ValueError("status 非法"),
        ):
            with pytest.raises(ValidationError, match="status 非法"):
                svc.create_canonical(name="X", unit="kg", status="weird")


# ─────────────────────────────────────────────────────────────────────
# update_canonical
# ─────────────────────────────────────────────────────────────────────

class TestUpdateCanonical:
    def test_no_fields_raises(self):
        with pytest.raises(ValidationError, match="no fields to update"):
            svc.update_canonical(1)

    @patch("mcp_server.service.canonical.master_connection")
    def test_unknown_id_raises_not_found(self, mock_conn):
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=ctx)
        ctx.__exit__ = MagicMock(return_value=False)
        mock_conn.return_value = ctx
        with patch(
            "mcp_server.service.canonical.cp.update_canonical_item",
            side_effect=ValueError("canonical_id=999 不存在"),
        ):
            with pytest.raises(NotFoundError):
                svc.update_canonical(999, name="X")

    @patch("mcp_server.service.canonical.master_connection")
    def test_q3_deleted_status_rejected(self, mock_conn):
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=ctx)
        ctx.__exit__ = MagicMock(return_value=False)
        mock_conn.return_value = ctx
        with patch(
            "mcp_server.service.canonical.cp.update_canonical_item",
            side_effect=ValueError("Q3=deactivate_only: 不允许物理删除"),
        ):
            with pytest.raises(ValidationError):
                svc.update_canonical(1, status="deleted")


# ─────────────────────────────────────────────────────────────────────
# claim_item
# ─────────────────────────────────────────────────────────────────────

class TestClaimItem:
    def test_warehouse_not_whitelisted_forbidden(self):
        ctx = AuthContext(1, [], [], ["wh_001"])  # only wh_001
        with pytest.raises(ForbiddenError):
            svc.claim_item(
                warehouse_code="wh_002", local_item_id=1, canonical_id=1,
                ctx=ctx,
            )

    @patch("mcp_server.service.canonical.resolve_warehouse", return_value=None)
    def test_unknown_warehouse_not_found(self, _rw):
        ctx = AuthContext(1, [], [], None)
        with pytest.raises(NotFoundError):
            svc.claim_item(
                warehouse_code="WH-NOPE", local_item_id=1, canonical_id=1,
                ctx=ctx,
            )

    @patch("mcp_server.service.canonical.cp.claim_item")
    @patch("mcp_server.service.canonical._open_warehouse")
    @patch("mcp_server.service.canonical.resolve_warehouse",
           return_value={"code": "wh_001", "db_path": "/tmp/wh_001.db"})
    @patch("mcp_server.service.canonical.master_connection")
    def test_pure_value_error_propagates_as_validation(
        self, _m, _rw, _ow, mock_claim
    ):
        ctx = AuthContext(1, [], [], None)
        m_ctx = MagicMock()
        m_ctx.__enter__ = MagicMock(return_value=m_ctx)
        m_ctx.__exit__ = MagicMock(return_value=False)
        _m.return_value = m_ctx
        wh_conn = MagicMock()
        _ow.return_value = wh_conn
        mock_claim.side_effect = ValueError("该行已被认领到 canonical_id=5")

        with pytest.raises(ValidationError, match="已被认领"):
            svc.claim_item(
                warehouse_code="wh_001", local_item_id=11, canonical_id=1,
                ctx=ctx,
            )
        wh_conn.close.assert_called_once()


# ─────────────────────────────────────────────────────────────────────
# unclaim_item
# ─────────────────────────────────────────────────────────────────────

class TestUnclaimItem:
    def test_warehouse_not_whitelisted_forbidden(self):
        ctx = AuthContext(1, [], [], ["wh_001"])
        with pytest.raises(ForbiddenError):
            svc.unclaim_item(warehouse_code="wh_002", local_item_id=1, ctx=ctx)


# ─────────────────────────────────────────────────────────────────────
# fanout_canonical
# ─────────────────────────────────────────────────────────────────────

class TestFanoutCanonical:
    def test_no_canonical_ids_rejected(self):
        with pytest.raises(ValidationError, match="canonical_ids required"):
            svc.fanout_canonical(
                canonical_ids=[], warehouse_codes=["wh_000"],
            )

    def test_no_warehouse_codes_rejected(self):
        with pytest.raises(ValidationError, match="warehouse_codes required"):
            svc.fanout_canonical(
                canonical_ids=[1], warehouse_codes=[],
            )

    def test_invalid_action_rejected(self):
        with pytest.raises(ValidationError, match="action must be"):
            svc.fanout_canonical(
                canonical_ids=[1], warehouse_codes=["wh_001"],
                action="explode",
            )

    @patch("mcp_server.service.canonical.master_connection")
    def test_must_include_distribution_center(self, mock_conn):
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=ctx)
        ctx.__exit__ = MagicMock(return_value=False)
        ctx.row_factory = None
        ctx.execute.return_value.fetchone.return_value = {"c": 0}
        mock_conn.return_value = ctx
        with pytest.raises(ValidationError, match="distribution_center"):
            svc.fanout_canonical(
                canonical_ids=[1],
                warehouse_codes=["wh_001", "wh_002"],
            )

    @patch("mcp_server.service.canonical.master_connection")
    def test_whitelist_with_dc_passes(self, mock_conn):
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=ctx)
        ctx.__exit__ = MagicMock(return_value=False)
        ctx.row_factory = None
        # first call = DC check (count 1)
        ctx.execute.return_value.fetchone.return_value = {"c": 1}
        # second call = load wh rows for opening conns (we close them right after)
        row = MagicMock()
        row.__getitem__.side_effect = lambda k: {"code": "wh_000",
                                                 "db_path": "/tmp/x",
                                                 "warehouse_type": "distribution_center"}[k]
        ctx.execute.return_value.fetchall.return_value = [row]
        mock_conn.return_value = ctx
        with patch(
            "mcp_server.service.canonical.cp.fanout_canonical_items",
            return_value={"event_id": 1, "status": "complete",
                          "total_written": 5, "total_frozen": 0,
                          "warehouses": [], "items": []},
        ) as fn:
            result = svc.fanout_canonical(
                canonical_ids=[1], warehouse_codes=["wh_000"],
                actor_id=99,
            )
        assert result["event_id"] == 1
        assert fn.call_args.kwargs["started_by"] == 99


# ─────────────────────────────────────────────────────────────────────
# resolve_conflict
# ─────────────────────────────────────────────────────────────────────

class TestResolveConflict:
    def test_invalid_decision_rejected(self):
        with pytest.raises(ValidationError, match="decision must be"):
            svc.resolve_conflict(conflict_id=1, decision="approve")

    def test_accept_canonical_accepted(self):
        # Just verify accepted values don't raise from the service layer
        # before hitting the pure layer.
        with patch("mcp_server.service.canonical.master_connection") as mock_conn:
            ctx = MagicMock()
            ctx.__enter__ = MagicMock(return_value=ctx)
            ctx.__exit__ = MagicMock(return_value=False)
            mock_conn.return_value = ctx
            with patch(
                "mcp_server.service.canonical.cp.resolve_conflict",
                return_value={"conflict_id": 1, "status": "resolved_accept_canonical"},
            ):
                result = svc.resolve_conflict(
                    conflict_id=1, decision="accept_canonical",
                )
        assert result["status"] == "resolved_accept_canonical"


# ─────────────────────────────────────────────────────────────────────
# set_binding_active
# ─────────────────────────────────────────────────────────────────────

class TestSetBindingActive:
    def test_warehouse_not_whitelisted_forbidden(self):
        ctx = AuthContext(1, [], [], ["wh_001"])
        with pytest.raises(ForbiddenError):
            svc.set_binding_active(
                warehouse_code="wh_002", canonical_id=1, active=False,
                ctx=ctx,
            )
