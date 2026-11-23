"""Check the rollback precondition after routing writers have been fenced."""
from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        path = args.db.resolve(strict=True)
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] < 10:
                raise ValueError("expected the current v10 master")
            count = conn.execute(
                "SELECT COUNT(*) FROM item_routing_stockout_state WHERE pause_id IS NOT NULL"
            ).fetchone()[0]
    except (OSError, ValueError, sqlite3.Error):
        print("Web rollback blocked: hold state unavailable")
        return 2
    print(f"Active stockout holds: {count}")
    return int(count > 0)


if __name__ == "__main__":
    raise SystemExit(main())
