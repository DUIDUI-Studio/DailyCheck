"""Test unit of work for MCP Server."""
import pytest
from mcp_server.data.unit_of_work import master_connection, warehouse_connection


def test_master_connection_returns_row():
    """Test that master_connection returns a connection with Row factory."""
    with master_connection() as conn:
        row = conn.execute("SELECT 1 AS val").fetchone()
        assert row["val"] == 1


def test_warehouse_connection_returns_row():
    """Test that warehouse_connection returns a connection with Row factory."""
    # Use an in-memory database for testing
    with warehouse_connection(":memory:") as conn:
        conn.execute("CREATE TABLE test (val INTEGER)")
        conn.execute("INSERT INTO test (val) VALUES (42)")
        row = conn.execute("SELECT val FROM test").fetchone()
        assert row["val"] == 42


def test_master_connection_multiple_queries():
    """Test that master_connection can handle multiple queries."""
    with master_connection() as conn:
        result1 = conn.execute("SELECT 1 AS a").fetchone()
        result2 = conn.execute("SELECT 2 AS b").fetchone()
        assert result1["a"] == 1
        assert result2["b"] == 2


def test_warehouse_connection_multiple_queries():
    """Test that warehouse_connection can handle multiple queries."""
    with warehouse_connection(":memory:") as conn:
        conn.execute("CREATE TABLE test (a INTEGER, b INTEGER)")
        conn.execute("INSERT INTO test (a, b) VALUES (1, 2)")
        result1 = conn.execute("SELECT a FROM test").fetchone()
        result2 = conn.execute("SELECT b FROM test").fetchone()
        assert result1["a"] == 1
        assert result2["b"] == 2
