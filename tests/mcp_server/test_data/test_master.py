"""Test master.py data access layer."""
import pytest
from unittest.mock import patch, MagicMock

from mcp_server.data import master


def test_resolve_warehouse_returns_dict():
    """Test resolve_warehouse returns dict when warehouse exists."""
    mock_row = {"code": "WH001", "name": "Test Warehouse", "db_path": "/path/to/db"}

    with patch("mcp_server.data.master.master_connection") as mock_conn_ctx:
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = mock_row
        mock_conn_ctx.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn_ctx.return_value.__exit__ = MagicMock(return_value=False)

        result = master.resolve_warehouse("WH001")

        assert result == mock_row
        mock_conn.execute.assert_called_once()


def test_resolve_warehouse_returns_none():
    """Test resolve_warehouse returns None when warehouse not found."""
    with patch("mcp_server.data.master.master_connection") as mock_conn_ctx:
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = None
        mock_conn_ctx.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn_ctx.return_value.__exit__ = MagicMock(return_value=False)

        result = master.resolve_warehouse("NONEXISTENT")

        assert result is None


def test_list_all_warehouses_returns_list():
    """Test list_all_warehouses returns list of warehouses."""
    mock_rows = [
        {"code": "WH001", "name": "Warehouse 1", "db_path": "/path/db1"},
        {"code": "WH002", "name": "Warehouse 2", "db_path": "/path/db2"},
    ]

    with patch("mcp_server.data.master.master_connection") as mock_conn_ctx:
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = mock_rows
        mock_conn_ctx.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn_ctx.return_value.__exit__ = MagicMock(return_value=False)

        result = master.list_all_warehouses()

        assert result == mock_rows
        assert len(result) == 2


def test_list_all_warehouses_empty():
    """Test list_all_warehouses returns empty list when no warehouses."""
    with patch("mcp_server.data.master.master_connection") as mock_conn_ctx:
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []
        mock_conn_ctx.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn_ctx.return_value.__exit__ = MagicMock(return_value=False)

        result = master.list_all_warehouses()

        assert result == []
