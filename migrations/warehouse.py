"""Warehouse-only registry. Never pass this registry to the master runner."""

from __future__ import annotations

import sqlite3


def up_document_lines(conn: sqlite3.Connection) -> None:
    from db import migrate_warehouse_document_columns

    migrate_warehouse_document_columns(conn)


def down_document_lines(conn: sqlite3.Connection) -> None:
    raise RuntimeError("schema/data-preserving code rollback or verified backup restore required")


PRODUCTION_RUN_SNAPSHOT = {"output_unit_snapshot": "TEXT"}
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
    for table, fields in (
        ("production_runs", PRODUCTION_RUN_SNAPSHOT),
        ("production_run_items", PRODUCTION_LINE_SNAPSHOT),
        ("items", {"production_measurement_revision": "INTEGER NOT NULL DEFAULT 0"}),
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

    # A return to the old label or factor after an intervening edit does not
    # establish continuity of the stock unit or item identity.
    conn.execute("""CREATE TRIGGER IF NOT EXISTS items_production_measurement_revision
        AFTER UPDATE OF unit, sku, canonical_id, gram_per_unit ON items
        WHEN OLD.unit IS NOT NEW.unit OR OLD.sku IS NOT NEW.sku
          OR OLD.canonical_id IS NOT NEW.canonical_id
          OR OLD.gram_per_unit IS NOT NEW.gram_per_unit
        BEGIN
            UPDATE items SET production_measurement_revision = OLD.production_measurement_revision + 1
            WHERE id = NEW.id;
        END""")

    # Old Web/MCP writers may still submit after a compatibility restamp, but
    # their reversal paths have no snapshot validation. Abort their entire
    # transaction, including any earlier inventory UPDATE.
    for table, operation, predicate in (
        ("production_runs", "UPDATE OF rolled_back", "OLD.rolled_back <> NEW.rolled_back"),
        ("production_runs", "DELETE", "1"),
        ("outbound_requests", "UPDATE OF rolled_back", "OLD.rolled_back <> NEW.rolled_back AND OLD.source_type = 'production'"),
        ("outbound_requests", "DELETE", "OLD.source_type = 'production'"),
    ):
        verb = operation.split()[0].lower()
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_snapshot_compat_{verb}
            BEFORE {operation} ON {table}
            WHEN ({predicate}) AND (SELECT user_version FROM pragma_user_version) < 8
            BEGIN SELECT RAISE(ROLLBACK, 'production reversal requires snapshot-aware software'); END""")


def down_production_snapshots(conn: sqlite3.Connection) -> None:
    """The runner restamps the version after backing up; retain v8 facts and guards."""
    if "calculation_version" not in {row[1] for row in conn.execute("PRAGMA table_info(production_run_items)")}:
        raise RuntimeError("production snapshot schema missing")


WAREHOUSE_MIGRATIONS = [
    (7, "document line write contract", up_document_lines, down_document_lines),
    (8, "production measurement snapshots", up_production_snapshots, down_production_snapshots),
]
