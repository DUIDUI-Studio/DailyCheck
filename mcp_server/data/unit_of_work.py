"""Unit of Work，管理 SQLite 连接生命周期。"""
from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from mcp_server.config import MASTER_DB


@contextmanager
def master_connection() -> Generator[sqlite3.Connection, None, None]:
    """master.db 连接，row_factory = Row。"""
    conn = sqlite3.connect(MASTER_DB)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def warehouse_connection(db_path: str) -> Generator[sqlite3.Connection, None, None]:
    """warehouse DB 连接，row_factory = Row。"""
    from db import migrate_warehouse_db_columns
    from db.warehouse_migrate import connect_existing

    path = Path(db_path).resolve()
    migrate_warehouse_db_columns(path)
    conn = connect_existing(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()
