"""The MCP fixture must reject accidental host DB access before SQLite opens it."""
from __future__ import annotations

import sqlite3

import pytest


@pytest.mark.parametrize("uri", [False, True], ids=["path", "read-only-uri"])
def test_database_outside_test_directory_is_rejected(tmp_path, uri):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.db"
    database = outside.as_uri() + "?mode=ro" if uri else outside
    with pytest.raises(AssertionError, match="non-test database"):
        sqlite3.connect(database, uri=uri)
    assert not outside.exists()


def test_symlink_cannot_escape_database_isolation(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.db"
    link = tmp_path / "alias.db"
    link.symlink_to(outside)
    with pytest.raises(AssertionError, match="non-test database"):
        sqlite3.connect(link)
    assert not outside.exists()


def test_master_is_initialized_in_test_directory(mcp_test_env):
    from mcp_server.data.unit_of_work import master_connection

    with master_connection() as conn:
        assert conn.execute("PRAGMA database_list").fetchone()["file"] == str(mcp_test_env["master"])
        assert conn.execute("SELECT COUNT(*) FROM warehouses").fetchone()[0] == 0
