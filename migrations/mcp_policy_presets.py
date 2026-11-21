"""Versioned MCP policy presets and issued token bindings (master v9)."""
from __future__ import annotations

import sqlite3


def registered_up(conn: sqlite3.Connection) -> None:
    from db import MCP_POLICY_DDL

    for ddl in MCP_POLICY_DDL:
        conn.execute(ddl)


def registered_down(conn: sqlite3.Connection) -> None:
    raise RuntimeError("MCP policy storage retains issuance and revoke provenance; use code rollback")
