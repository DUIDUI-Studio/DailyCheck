"""Forecast 服务测试。"""
import pytest
from unittest.mock import patch, MagicMock
from mcp_server.service.forecast import get_forecast
from mcp_server.service.auth import AuthContext
from mcp_server.infra.errors import ForbiddenError, ValidationError, NotFoundError


class TestGetForecast:
    def test_get_forecast_requires_warehouse_code(self):
        """测试空仓库代码抛出验证错误"""
        ctx = AuthContext(1, [], [], None)
        with pytest.raises(ValidationError):
            get_forecast(1, "", None, ctx)

    def test_get_forecast_forbidden_warehouse(self):
        """测试访问未授权仓库被拒绝"""
        ctx = AuthContext(1, [], [], ["WH001"])
        with pytest.raises(ForbiddenError):
            get_forecast(1, "WH999", None, ctx)

    @patch("mcp_server.service.forecast.check_warehouse")
    @patch("mcp_server.service.forecast.resolve_warehouse")
    @patch("mcp_server.service.forecast.warehouse_connection")
    def test_get_forecast_warehouse_not_found(
        self, mock_conn, mock_resolve, mock_check
    ):
        """测试仓库不存在"""
        mock_check.return_value = True
        mock_resolve.return_value = None

        ctx = AuthContext(1, [], [], None)
        with pytest.raises(NotFoundError):
            get_forecast(1, "WH001", None, ctx)

    @patch("mcp_server.service.forecast.check_warehouse")
    @patch("mcp_server.service.forecast.resolve_warehouse")
    @patch("mcp_server.service.forecast.warehouse_connection")
    def test_get_forecast_invalid_horizon(
        self, mock_conn, mock_resolve, mock_check
    ):
        """测试无效的 horizon 值"""
        mock_check.return_value = True
        mock_resolve.return_value = {"code": "WH001", "db_path": "/tmp/test.db"}

        ctx = AuthContext(1, [], [], None)
        with pytest.raises(ValidationError):
            get_forecast(1, "WH001", 999, ctx)  # horizon 超出范围

    @patch("mcp_server.service.forecast.check_warehouse")
    @patch("mcp_server.service.forecast.resolve_warehouse")
    @patch("mcp_server.service.forecast.warehouse_connection")
    def test_get_forecast_item_not_found(
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
            get_forecast(999, "WH001", 14, ctx)

    @patch("mcp_server.service.forecast.check_warehouse")
    @patch("mcp_server.service.forecast.resolve_warehouse")
    @patch("mcp_server.service.forecast.warehouse_connection")
    @patch("mcp_server.service.forecast.fetch_item_movements_30d")
    def test_get_forecast_success(
        self, mock_movements, mock_conn, mock_resolve, mock_check
    ):
        """测试成功获取预测"""
        mock_check.return_value = True
        mock_resolve.return_value = {"code": "WH001", "db_path": "/tmp/test.db"}

        # Mock item_exists returns True
        mock_context = MagicMock()
        mock_context.__enter__ = MagicMock(return_value=mock_context)
        mock_context.__exit__ = MagicMock(return_value=False)
        mock_context.execute.return_value.fetchone.return_value = {"id": 1}
        mock_conn.return_value = mock_context

        # Mock movements - empty list = cold_start
        mock_movements.return_value = []

        ctx = AuthContext(1, [], [], None)
        result = get_forecast(1, "WH001", 14, ctx)

        assert result["item_id"] == 1
        assert result["warehouse_code"] == "WH001"
        assert result["horizon_days"] == 14
        assert result["confidence"] == "cold_start"
        assert result["data_status"] == "cold_start"
        assert result["forecast_total"] == 0.0

    @patch("mcp_server.service.forecast.check_warehouse")
    @patch("mcp_server.service.forecast.resolve_warehouse")
    @patch("mcp_server.service.forecast.warehouse_connection")
    @patch("mcp_server.service.forecast.fetch_item_movements_30d")
    @patch("mcp_server.service.forecast.classify_confidence")
    @patch("mcp_server.service.forecast.compute_daily_avg")
    def test_get_forecast_with_movements(
        self, mock_daily_avg, mock_classify, mock_movements, mock_conn, mock_resolve, mock_check
    ):
        """测试有消耗记录时成功获取预测"""
        from datetime import datetime
        mock_check.return_value = True
        mock_resolve.return_value = {"code": "WH001", "db_path": "/tmp/test.db"}

        mock_context = MagicMock()
        mock_context.__enter__ = MagicMock(return_value=mock_context)
        mock_context.__exit__ = MagicMock(return_value=False)
        mock_context.execute.return_value.fetchone.return_value = {"id": 1}
        mock_conn.return_value = mock_context

        # Mock movements - some recent consumption
        mock_movements.return_value = [
            (datetime(2024, 1, 1), 10.0),
            (datetime(2024, 1, 2), 15.0),
        ]
        mock_classify.return_value = "medium"
        mock_daily_avg.return_value = 12.5

        ctx = AuthContext(1, [], [], None)
        result = get_forecast(1, "WH001", 14, ctx)

        assert result["item_id"] == 1
        assert result["warehouse_code"] == "WH001"
        assert result["confidence"] == "medium"
        assert result["forecast_total"] >= 0.0


class TestParseHorizon:
    def test_parse_horizon_default(self):
        """测试默认 horizon"""
        from mcp_server.service.forecast import parse_horizon
        assert parse_horizon(None) == 14

    def test_parse_horizon_valid(self):
        """测试有效的 horizon 值"""
        from mcp_server.service.forecast import parse_horizon
        assert parse_horizon(7) == 7
        assert parse_horizon(30) == 30
        assert parse_horizon(90) == 90

    def test_parse_horizon_invalid(self):
        """测试无效的 horizon 值"""
        from mcp_server.service.forecast import parse_horizon
        assert parse_horizon(0) is None
        assert parse_horizon(-1) is None
        assert parse_horizon(91) is None
        assert parse_horizon("invalid") is None
