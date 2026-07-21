"""库存服务测试。"""
import pytest
from unittest.mock import patch, MagicMock
from mcp_server.service.inventory import list_items, get_item, list_movements
from mcp_server.service.auth import AuthContext
from mcp_server.infra.errors import ForbiddenError, ValidationError, NotFoundError


class TestListItems:
    def test_list_items_rejects_missing_warehouse(self):
        """测试访问未授权仓库被拒绝"""
        ctx = AuthContext(1, [], [], ["WH001"])
        with pytest.raises(ForbiddenError):
            list_items("WH999", ctx)

    def test_list_items_requires_warehouse_code(self):
        """测试空仓库代码抛出验证错误"""
        ctx = AuthContext(1, [], [], None)
        with pytest.raises(ValidationError):
            list_items("", ctx)

    @patch("mcp_server.service.inventory.check_warehouse")
    @patch("mcp_server.service.inventory.resolve_warehouse")
    @patch("mcp_server.service.inventory.warehouse_connection")
    def test_list_items_success(
        self, mock_conn, mock_resolve, mock_check
    ):
        """测试成功列出物品"""
        mock_check.return_value = True
        mock_resolve.return_value = {"code": "WH001", "db_path": "/tmp/test.db"}

        mock_context = MagicMock()
        mock_context.__enter__ = MagicMock(return_value=mock_context)
        mock_context.__exit__ = MagicMock(return_value=False)
        mock_context.execute.return_value.fetchall.return_value = [
            {"id": 1, "sku": "SKU001", "name": "Item 1", "category_id": 1,
             "current_stock": 10, "safety_stock": 5, "unit": "pcs",
             "unit_cost": 100, "gram_per_unit": 0, "updated_at": "2024-01-01",
             "category_name": "调味酱"}
        ]
        mock_conn.return_value = mock_context

        ctx = AuthContext(1, [], [], None)
        result = list_items("WH001", ctx)

        assert len(result) == 1
        assert result[0]["sku"] == "SKU001"
        assert result[0]["category_name"] == "调味酱"


class TestGetItem:
    @patch("mcp_server.service.inventory.check_warehouse")
    @patch("mcp_server.service.inventory.resolve_warehouse")
    @patch("mcp_server.service.inventory.warehouse_connection")
    def test_get_item_not_found(self, mock_conn, mock_resolve, mock_check):
        """测试物品不存在"""
        mock_check.return_value = True
        mock_resolve.return_value = {"code": "WH001", "db_path": "/tmp/test.db"}

        mock_context = MagicMock()
        mock_context.__enter__ = MagicMock(return_value=mock_context)
        mock_context.__exit__ = MagicMock(return_value=False)
        mock_context.execute.return_value.fetchone.return_value = None
        mock_conn.return_value = mock_context

        ctx = AuthContext(1, [], [], None)
        with pytest.raises(NotFoundError):
            get_item(999, "WH001", ctx)


class TestListMovements:
    def test_list_movements_rejects_missing_warehouse(self):
        """测试访问未授权仓库被拒绝"""
        ctx = AuthContext(1, [], [], ["WH001"])
        with pytest.raises(ForbiddenError):
            list_movements("WH999", ctx)
