"""Warehouse-only registry. Never pass this registry to the master runner."""

from __future__ import annotations

import sqlite3


def up_document_lines(conn: sqlite3.Connection) -> None:
    from db import migrate_warehouse_document_columns

    migrate_warehouse_document_columns(conn)


def down_document_lines(conn: sqlite3.Connection) -> None:
    raise RuntimeError("schema/data-preserving code rollback or verified backup restore required")


PRODUCTION_RUN_SNAPSHOT = {"output_unit_snapshot": "TEXT"}
PRODUCTION_ITEM_REVISIONS = "production_item_measurement_revisions"
PRODUCTION_LINE_SNAPSHOT = {
    "unit_snapshot": "TEXT",
    "sku_snapshot": "TEXT",
    "canonical_id_snapshot": "INTEGER",
    "bom_row_id_snapshot": "INTEGER",
    "bom_qty_per_unit_snapshot": "REAL",
    "conversion_mode_snapshot": "TEXT",
    "grams_per_unit_snapshot": "REAL",
    "calculation_version": "INTEGER",
    "measurement_revision_snapshot": "INTEGER",
}


def up_production_snapshots(conn: sqlite3.Connection) -> None:
    if "production_measurement_revision" in {row[1] for row in conn.execute("PRAGMA table_info(items)")}:
        raise RuntimeError("recreate the unreleased production snapshot preview from its v7 backup")
    _create_production_item_revisions(conn)
    for table, fields in (
        ("production_runs", PRODUCTION_RUN_SNAPSHOT),
        ("production_run_items", PRODUCTION_LINE_SNAPSHOT),
    ):
        existing = {row[1]: row for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, kind in fields.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
            elif (existing[column][2].upper(), bool(existing[column][3])) != (
                kind.split()[0], "NOT NULL" in kind,
            ):
                raise RuntimeError(f"incompatible production snapshot column: {table}.{column}")

    for table, fields in (
        ("production_runs", PRODUCTION_RUN_SNAPSHOT | {"product_id": "INTEGER"}),
        ("production_run_items", PRODUCTION_LINE_SNAPSHOT | {"run_id": "INTEGER", "item_id": "INTEGER"}),
    ):
        changed = " OR ".join(f"OLD.{column} IS NOT NEW.{column}" for column in fields)
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_snapshot_immutable
            BEFORE UPDATE OF {', '.join(fields)} ON {table}
            WHEN {changed}
            BEGIN SELECT RAISE(ABORT, 'production snapshot is immutable'); END""")

    conn.execute("""CREATE TRIGGER IF NOT EXISTS production_snapshot_outbound_immutable
        BEFORE UPDATE OF outbound_req_id ON production_run_items
        WHEN OLD.calculation_version IS NOT NULL AND OLD.outbound_req_id IS NOT NEW.outbound_req_id
        BEGIN SELECT RAISE(ABORT, 'production snapshot association is immutable'); END""")
    _production_compatibility_guards(conn, enable=False)


def _create_production_item_revisions(conn: sqlite3.Connection) -> None:
    table = PRODUCTION_ITEM_REVISIONS
    if conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (table,)).fetchone() is None:
        if "measurement_revision_snapshot" in {
            row[1] for row in conn.execute("PRAGMA table_info(production_run_items)")
        }:
            raise RuntimeError("production item revision history is missing")
        # Keep retired item IDs so reusing an ID or REPLACE cannot resurrect
        # an earlier measurement revision. No foreign key cascades these rows.
        conn.execute(f"""CREATE TABLE {table} (
            item_id INTEGER PRIMARY KEY,
            revision INTEGER NOT NULL CHECK (typeof(revision) = 'integer' AND revision >= 0)
        )""")
        conn.execute(f"INSERT INTO {table}(item_id, revision) SELECT id, 0 FROM items")

    advance = f"""INSERT INTO {table}(item_id, revision) VALUES (NEW.id, 0)
                 ON CONFLICT(item_id) DO UPDATE SET revision = revision + 1;"""
    conn.execute(f"""CREATE TRIGGER IF NOT EXISTS items_production_measurement_insert
        AFTER INSERT ON items BEGIN {advance} END""")
    conn.execute(f"""CREATE TRIGGER IF NOT EXISTS items_production_measurement_update
        AFTER UPDATE OF id, unit, sku, canonical_id, gram_per_unit ON items
        WHEN OLD.id IS NOT NEW.id OR OLD.unit IS NOT NEW.unit OR OLD.sku IS NOT NEW.sku
          OR OLD.canonical_id IS NOT NEW.canonical_id OR OLD.gram_per_unit IS NOT NEW.gram_per_unit
        BEGIN
            SELECT RAISE(ABORT, 'production item revision history is missing')
            WHERE NOT EXISTS (SELECT 1 FROM {table} WHERE item_id = OLD.id);
            {advance}
        END""")


def _production_compatibility_guards(conn: sqlite3.Connection, *, enable: bool) -> None:
    # Old Web/MCP writers may still submit after a compatibility restamp, but
    # their reversal paths have no snapshot validation. Abort their entire
    # transaction, including any earlier inventory UPDATE. Install the guards
    # in the restamp transaction so they work even in an attached warehouse.
    production = """COALESCE(OLD.source_type = 'production', EXISTS (
        SELECT 1 FROM production_run_items WHERE outbound_req_id = OLD.id
    ))"""
    for table, operation, predicate in (
        ("production_runs", "UPDATE OF rolled_back", "OLD.rolled_back <> NEW.rolled_back"),
        ("production_runs", "DELETE", "1"),
        ("outbound_requests", "UPDATE OF rolled_back", f"OLD.rolled_back <> NEW.rolled_back AND {production}"),
        ("outbound_requests", "DELETE", production),
    ):
        verb = operation.split()[0].lower()
        name = f"{table}_snapshot_compat_{verb}"
        if enable:
            conn.execute(f"""CREATE TRIGGER {name} BEFORE {operation} ON {table}
                WHEN {predicate}
                BEGIN SELECT RAISE(ROLLBACK, 'production reversal requires snapshot-aware software'); END""")
        else:
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")


def down_production_snapshots(conn: sqlite3.Connection) -> None:
    """The runner restamps the version after backing up; retain v8 facts and guards."""
    if "calculation_version" not in {row[1] for row in conn.execute("PRAGMA table_info(production_run_items)")}:
        raise RuntimeError("production snapshot schema missing")
    _production_compatibility_guards(conn, enable=True)


WAREHOUSE_MIGRATIONS = [
    (7, "document line write contract", up_document_lines, down_document_lines),
    (8, "production measurement snapshots", up_production_snapshots, down_production_snapshots),
]
