"""Check the rollback precondition after routing writers have been fenced."""
from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path


def active_holds(path: Path, *, pre_feature=False, bootstrap=False) -> int | None:
    path = path.resolve(strict=True)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
        conn.execute("BEGIN")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {name for name, in conn.execute(
            "SELECT name FROM sqlite_schema WHERE type='table'"
        )}
        hold_tables = {"item_routing_stockout_state", "item_routing_stockout_events"}
        if version == 9 and (pre_feature or bootstrap):
            # Exact pre-feature releases had the v9 policy/routing schema and
            # no stockout state. Candidate Web init can add both tables before
            # the explicit registered v10 migration is run.
            if not {"mcp_policy_presets", "mcp_policy_bindings", "item_routing"} <= tables:
                raise ValueError("unrecognized v9 master")
            if not tables & hold_tables and pre_feature:
                return None
        elif version != 10:
            raise ValueError("unrecognized routing schema version")
        if not hold_tables <= tables or version == 9 and not bootstrap:
            raise ValueError("missing or unexpected stockout schema")
        count = conn.execute(
            "SELECT COUNT(*) FROM item_routing_stockout_state WHERE pause_id IS NOT NULL"
        ).fetchone()[0]
        # A missing projection cannot turn an unclosed, durable pause into a clean check.
        unclosed = conn.execute("""
            SELECT COUNT(*) FROM item_routing_stockout_events AS p
            WHERE p.operation = 'pause' AND NOT EXISTS (
                SELECT 1 FROM item_routing_stockout_events AS r
                WHERE r.operation = 'restore' AND r.pause_id = p.id
                      AND r.tenant_id = p.tenant_id AND r.canonical_id = p.canonical_id
                      AND r.warehouse_code = p.warehouse_code
            )
        """).fetchone()[0]
        if count != unclosed:
            raise ValueError("hold projection and audit disagree")
        return count


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        count = active_holds(args.db)
    except (OSError, ValueError, sqlite3.Error):
        print("Web rollback blocked: hold state unavailable")
        return 2
    print(f"Active stockout holds: {count}")
    return int(bool(count))


if __name__ == "__main__":
    raise SystemExit(main())
