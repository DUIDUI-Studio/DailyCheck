"""入库服务测试。"""
import pytest
from unittest.mock import patch, MagicMock
from mcp_server.service.inbound import create_restock, list_restock
from mcp_server.service.auth import AuthContext
from mcp_server.infra.errors import ForbiddenError, ValidationError, NotFoundError


class TestCreateRestock:
    def test_create_restock_requires_warehouse_code(self):
        """测试空仓库代码抛出验证错误"""
        ctx = AuthContext(1, [], [], None)
        with pytest.raises(ValidationError):
            create_restock(1, 10, "", ctx)

    def test_create_restock_rejects_missing_warehouse(self):
        """测试访问未授权仓库被拒绝"""
        ctx = AuthContext(1, [], [], ["WH001"])
        with pytest.raises(ForbiddenError):
            create_restock(1, 10, "WH999", ctx)

    def test_create_restock_requires_positive_quantity(self):
        """测试非正数量抛出验证错误"""
        ctx = AuthContext(1, [], [], None)
        with pytest.raises(ValidationError):
            create_restock(1, 0, "WH001", ctx)
        with pytest.raises(ValidationError):
            create_restock(1, -5, "WH001", ctx)

    @patch("mcp_server.service.inbound.check_warehouse")
    @patch("mcp_server.service.inbound.resolve_warehouse")
    @patch("mcp_server.service.inbound.warehouse_connection")
    def test_create_restock_item_not_found(
        self, mock_conn, mock_resolve, mock_check
    ):
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
            create_restock(999, 10, "WH001", ctx)

    @patch("mcp_server.service.inbound.check_warehouse")
    @patch("mcp_server.service.inbound.resolve_warehouse")
    @patch("mcp_server.service.inbound.warehouse_connection")
    def test_create_restock_success(
        self, mock_conn, mock_resolve, mock_check
    ):
        """测试成功创建入库记录"""
        mock_check.return_value = True
        mock_resolve.return_value = {"code": "WH001", "db_path": "/tmp/test.db"}

        mock_context = MagicMock()
        mock_context.__enter__ = MagicMock(return_value=mock_context)
        mock_context.__exit__ = MagicMock(return_value=False)
        mock_context.execute.return_value.fetchone.return_value = True
        mock_context.execute.return_value.lastrowid = 123
        mock_conn.return_value = mock_context

        ctx = AuthContext(1, [], [], None)
        result = create_restock(1, 10, "WH001", ctx, reason="test restock")

        assert result["id"] == 123
        assert result["item_id"] == 1
        assert result["quantity"] == 10
        assert result["warehouse_code"] == "WH001"


class TestListRestock:
    def test_list_restock_requires_warehouse_code(self):
        """测试空仓库代码抛出验证错误"""
        ctx = AuthContext(1, [], [], None)
        with pytest.raises(ValidationError):
            list_restock("", ctx)

    def test_list_restock_rejects_missing_warehouse(self):
        """测试访问未授权仓库被拒绝"""
        ctx = AuthContext(1, [], [], ["WH001"])
        with pytest.raises(ForbiddenError):
            list_restock("WH999", ctx)

    @patch("mcp_server.service.inbound.check_warehouse")
    @patch("mcp_server.service.inbound.resolve_warehouse")
    @patch("mcp_server.service.inbound.warehouse_connection")
    def test_list_restock_success(
        self, mock_conn, mock_resolve, mock_check
    ):
        """测试成功列出入库记录"""
        mock_check.return_value = True
        mock_resolve.return_value = {"code": "WH001", "db_path": "/tmp/test.db"}

        mock_context = MagicMock()
        mock_context.__enter__ = MagicMock(return_value=mock_context)
        mock_context.__exit__ = MagicMock(return_value=False)
        mock_context.execute.return_value.fetchall.return_value = [
            {"id": 1, "item_id": 1, "item_name": "Item 1",
             "qty": 10, "reason": "restock", "created_at": "2024-01-01"}
        ]
        mock_conn.return_value = mock_context

        ctx = AuthContext(1, [], [], None)
        result = list_restock("WH001", ctx)

        assert len(result) == 1
        assert result[0]["qty"] == 10
