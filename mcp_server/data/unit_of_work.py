"""Unit of Work，管理 SQLite 连接生命周期。"""
from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager

import config
from mcp_server.config import MASTER_DB
from warehouse_paths import resolve_warehouse_path


@contextmanager
def master_connection(*, read_only: bool = False) -> Generator[sqlite3.Connection, None, None]:
    """master.db 连接，row_factory = Row。"""
    conn = sqlite3.connect(f"file:{MASTER_DB}?mode=ro", uri=True) if read_only else sqlite3.connect(MASTER_DB)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def warehouse_connection(
    db_path: str, *, read_only: bool = False, base_dir: str | None = None,
) -> Generator[sqlite3.Connection, None, None]:
    """warehouse DB 连接，row_factory = Row。"""
    from db import migrate_warehouse_db_columns
    from db.warehouse_migrate import connect_existing

    path = resolve_warehouse_path(
        db_path, base_dir, db_dir=config.DB_DIR if base_dir is None else None).resolve()
    if read_only:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        migrate_warehouse_db_columns(path)
        conn = connect_existing(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()
