"""Procurement 服务测试。"""
import pytest
from unittest.mock import patch, MagicMock
from mcp_server.service import procurement as procurement_module
from mcp_server.service.procurement import procurement_store, procurement_hub
from mcp_server.service.auth import AuthContext
from mcp_server.infra.errors import ForbiddenError, ValidationError, NotFoundError


class TestProcurementStore:
    def test_procurement_store_requires_warehouse_code(self):
        """测试空仓库代码抛出验证错误"""
        ctx = AuthContext(1, [], [], None)
        with pytest.raises(ValidationError):
            procurement_store("", ctx)

    def test_procurement_store_forbidden_warehouse(self):
        """测试访问未授权仓库被拒绝"""
        ctx = AuthContext(1, [], [], ["WH001"])
        with pytest.raises(ForbiddenError):
            procurement_store("WH999", ctx)

    @patch("mcp_server.service.procurement.check_warehouse")
    @patch("mcp_server.service.procurement._get_store_procurement_json")
    def test_procurement_store_success(self, mock_get_func, mock_check):
        """测试成功获取采购建议"""
        mock_check.return_value = True
        mock_store_func = MagicMock()
        mock_store_func.return_value = {
            "warehouse_code": "WH001",
            "items": [
                {"item_id": 1, "item_name": "Item 1", "suggested_qty": 10}
            ]
        }
        mock_get_func.return_value = mock_store_func

        ctx = AuthContext(1, [], [], None)
        result = procurement_store("WH001", ctx)

        assert result["warehouse_code"] == "WH001"
        assert len(result["items"]) == 1

    @patch("mcp_server.service.procurement.check_warehouse")
    @patch("mcp_server.service.procurement._get_store_procurement_json")
    def test_procurement_store_warehouse_not_found(self, mock_get_func, mock_check):
        """测试仓库不存在"""
        mock_check.return_value = True
        mock_store_func = MagicMock()
        mock_store_func.return_value = None
        mock_get_func.return_value = mock_store_func

        ctx = AuthContext(1, [], [], None)
        with pytest.raises(NotFoundError):
            procurement_store("WH999", ctx)

    @patch("mcp_server.service.procurement.check_warehouse")
    def test_procurement_store_not_available(self, mock_check):
        """测试服务不可用"""
        mock_check.return_value = True

        with patch("mcp_server.service.procurement._get_store_procurement_json", return_value=None):
            ctx = AuthContext(1, [], [], None)
            with pytest.raises(NotFoundError) as exc_info:
                procurement_store("WH001", ctx)
            assert "procurement_not_available" in str(exc_info.value)


class TestProcurementHub:
    def test_procurement_hub_forbidden_warehouse(self):
        """测试访问未授权仓库被拒绝"""
        ctx = AuthContext(1, [], [], ["WH001"])
        with pytest.raises(ForbiddenError):
            procurement_hub(ctx, "WH999")

    @patch("mcp_server.service.procurement.check_warehouse")
    @patch("mcp_server.service.procurement.list_all_warehouses")
    @patch("mcp_server.service.procurement._get_store_procurement_json")
    @patch("mcp_server.service.procurement.aggregate_hub")
    def test_procurement_hub_all_warehouses(
        self, mock_aggregate, mock_get_func, mock_list, mock_check
    ):
        """测试获取所有仓库的采购汇总"""
        mock_check.return_value = True
        mock_list.return_value = [
            {"code": "WH001", "db_path": "/tmp/wh1.db"},
            {"code": "WH002", "db_path": "/tmp/wh2.db"},
        ]
        mock_store_func = MagicMock()
        mock_store_func.side_effect = [
            {"warehouse_code": "WH001", "items": [{"item_id": 1, "suggested_qty": 10}]},
            {"warehouse_code": "WH002", "items": [{"item_id": 1, "suggested_qty": 5}]},
        ]
        mock_get_func.return_value = mock_store_func
        mock_aggregate.return_value = [
            {"item_id": 1, "total_suggested_qty": 15, "stores_needing": 2}
        ]

        ctx = AuthContext(1, [], [], None)
        result = procurement_hub(ctx)

        assert len(result) == 1
        mock_aggregate.assert_called_once()

    @patch("mcp_server.service.procurement.check_warehouse")
    @patch("mcp_server.service.procurement.resolve_warehouse")
    @patch("mcp_server.service.procurement._get_store_procurement_json")
    @patch("mcp_server.service.procurement.aggregate_hub")
    def test_procurement_hub_specific_warehouse(
        self, mock_aggregate, mock_get_func, mock_resolve, mock_check
    ):
        """测试获取特定仓库的采购汇总"""
        mock_check.return_value = True
        mock_resolve.return_value = {"code": "WH001", "db_path": "/tmp/wh1.db"}
        mock_store_func = MagicMock()
        mock_store_func.return_value = {
            "warehouse_code": "WH001",
            "items": [{"item_id": 1, "suggested_qty": 10}]
        }
        mock_get_func.return_value = mock_store_func
        mock_aggregate.return_value = [
            {"item_id": 1, "total_suggested_qty": 10, "stores_needing": 1}
        ]

        ctx = AuthContext(1, [], [], None)
        result = procurement_hub(ctx, "WH001")

        assert len(result) == 1
        mock_resolve.assert_called_once_with("WH001")

    @patch("mcp_server.service.procurement.check_warehouse")
    @patch("mcp_server.service.procurement.resolve_warehouse")
    @patch("mcp_server.service.procurement._get_store_procurement_json")
    @patch("mcp_server.service.procurement.aggregate_hub")
    def test_procurement_hub_specific_warehouse_not_found(
        self, mock_aggregate, mock_get_func, mock_resolve, mock_check
    ):
        """测试特定仓库不存在"""
        mock_check.return_value = True
        mock_resolve.return_value = None
        mock_aggregate.return_value = []

        ctx = AuthContext(1, [], [], None)
        result = procurement_hub(ctx, "WH999")

        # 空列表，因为仓库不存在
        assert result == []

    @patch("mcp_server.service.procurement.check_warehouse")
    def test_procurement_hub_not_available(self, mock_check):
        """测试服务不可用"""
        mock_check.return_value = True

        with patch("mcp_server.service.procurement._get_store_procurement_json", return_value=None):
            ctx = AuthContext(1, [], [], None)
            result = procurement_hub(ctx)
            assert result == []
