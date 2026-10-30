"""Warehouse-only registry. Never pass this registry to the master runner."""

from __future__ import annotations

import sqlite3


def up_document_lines(conn: sqlite3.Connection) -> None:
    from db import migrate_warehouse_document_columns

    migrate_warehouse_document_columns(conn)


def down_document_lines(conn: sqlite3.Connection) -> None:
    raise RuntimeError("schema/data-preserving code rollback or verified backup restore required")


WAREHOUSE_MIGRATIONS = [(7, "document line write contract", up_document_lines, down_document_lines)]
