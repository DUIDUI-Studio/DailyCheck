"""Unit of Work，管理 SQLite 连接生命周期。"""
from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import config
from mcp_server.config import MASTER_DB
from warehouse_paths import resolve_warehouse_path


def _select_only(action: int, _arg1, _arg2, _db, _source) -> int:
    allowed = (
        action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ)
        or (action == sqlite3.SQLITE_FUNCTION and _arg2 == "count")
        or (action == sqlite3.SQLITE_TRANSACTION and _arg1 == "BEGIN")
    )
    return sqlite3.SQLITE_OK if allowed else sqlite3.SQLITE_DENY


@contextmanager
def policy_read_connection(path: Path) -> Generator[sqlite3.Connection, None, None]:
    """Open an existing rollback-journal database with read-only SQL access."""
    if not path.is_file() or path.resolve(strict=True) != path:
        raise sqlite3.OperationalError("MCP policy database unavailable")
    # WAL readers can create sidecars or update read marks even with mode=ro.
    # Check the on-disk journal format before SQLite can open any sidecars.
    with path.open("rb") as source:
        header = source.read(20)
        if header[:16] != b"SQLite format 3\x00" or header[18:20] != b"\x01\x01":
            raise sqlite3.OperationalError("MCP policy requires a rollback-journal database")
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.set_authorizer(_select_only)
        yield conn
    finally:
        conn.close()


def policy_master_connection():
    return policy_read_connection(Path(MASTER_DB).resolve())


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
        db_path, config.BASE_DIR if base_dir is None else base_dir,
        db_dir=config.DB_DIR if base_dir is None else None).resolve()
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


@contextmanager
def routing_write_connection() -> Generator[sqlite3.Connection, None, None]:
    """Dedicated write path that can never initialize or migrate a database."""
    path = Path(MASTER_DB).resolve(strict=True)
    conn = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    finally:
        conn.close()
