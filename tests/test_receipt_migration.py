"""Task 6 migration: ``store_order_receipts`` op_no/applied columns.

The Task 1 migration and its assertions are frozen; this file only covers the
new registered migration (v5) and the synchronized new-database DDL.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from db import MASTER_SCHEMA
from migrations import MIGRATIONS, receipt_op_protocol
from migrations_runner import migrate_up

LEGACY_RECEIPTS = """
CREATE TABLE store_order_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    order_item_id INTEGER NOT NULL,
    quantity REAL NOT NULL,
    received_by INTEGER NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);
INSERT INTO store_order_receipts
    (order_id, order_item_id, quantity, received_by, note, created_at)
VALUES (1, 1, 2.5, 7, 'legacy', '2026-10-01 00:00:00');
"""


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_registered_migration_adds_columns_and_backfills_applied():
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.executescript(LEGACY_RECEIPTS)

        receipt_op_protocol.registered_up(conn)

        assert {"op_no", "applied"} <= _columns(conn, "store_order_receipts")
        row = conn.execute(
            "SELECT quantity, op_no, applied FROM store_order_receipts"
        ).fetchone()
        assert row[0] == 2.5
        assert row[1] is None
        assert row[2] == 1
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO store_order_receipts"
                " (order_id, order_item_id, quantity, received_by, created_at, op_no)"
                " VALUES (1, 1, 1, 7, 'now', 'dup')"
            )
            conn.execute(
                "INSERT INTO store_order_receipts"
                " (order_id, order_item_id, quantity, received_by, created_at, op_no)"
                " VALUES (1, 1, 1, 7, 'now', 'dup')"
            )


def test_registered_migration_is_idempotent():
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.executescript(LEGACY_RECEIPTS)
        receipt_op_protocol.registered_up(conn)
        receipt_op_protocol.registered_up(conn)
        assert {"op_no", "applied"} <= _columns(conn, "store_order_receipts")
        assert conn.execute(
            "SELECT applied FROM store_order_receipts"
        ).fetchone()[0] == 1


def test_registered_migration_noops_without_receipts_table():
    with closing(sqlite3.connect(":memory:")) as conn:
        receipt_op_protocol.registered_up(conn)
        assert "store_order_receipts" not in {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master")
        }


def test_registered_migration_runs_through_runner(tmp_path):
    db_path = tmp_path / "master.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(LEGACY_RECEIPTS)
        conn.commit()
    migrate_up(db_path, MIGRATIONS, tmp_path / "backups")
    with closing(sqlite3.connect(db_path)) as conn:
        assert {"op_no", "applied"} <= _columns(conn, "store_order_receipts")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == max(
            v for v, *_ in MIGRATIONS
        )


def test_new_database_ddl_has_op_no_unique_and_applied_default():
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.executescript(MASTER_SCHEMA)
        columns = {
            r[1]: r for r in conn.execute("PRAGMA table_info(store_order_receipts)")
        }
        assert "op_no" in columns
        assert "applied" in columns
        # applied defaults to 1 so pre-protocol INSERTs (no column) stay "done".
        assert columns["applied"][4] == "1"
        conn.execute(
            "INSERT INTO store_order_receipts"
            " (order_id, order_item_id, quantity, received_by, created_at)"
            " VALUES (1, 1, 1, 7, 'now')"
        )
        assert conn.execute(
            "SELECT applied FROM store_order_receipts"
        ).fetchone()[0] == 1
        conn.execute(
            "INSERT INTO store_order_receipts"
            " (order_id, order_item_id, quantity, received_by, created_at, op_no)"
            " VALUES (1, 1, 1, 7, 'now', 'k1')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO store_order_receipts"
                " (order_id, order_item_id, quantity, received_by, created_at, op_no)"
                " VALUES (1, 1, 1, 7, 'now', 'k1')"
            )
