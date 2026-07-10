"""Unit of Work，管理 SQLite 连接生命周期。"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Generator

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
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()
