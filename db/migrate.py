"""Migration: convert legacy single-db inventory.db into wh_001.db.

The legacy file kept the full business schema in inventory.db. Under the
multi-warehouse model each warehouse owns its own db file. We:

1. Open the legacy db in place to add audit_log (if not present) and
   any post-launch column additions (defensive — should already exist).
2. Copy the resulting db to db/warehouses/wh_001.db.
3. Register that db in master.db as warehouse code=wh_001.
4. Leave the original inventory.db untouched on disk so a manual rollback
   is possible. New writes go to wh_001.db only.
"""
from __future__ import annotations

import sqlite3
import shutil
from contextlib import closing
from datetime import datetime
from pathlib import Path

from config import BASE_DIR, MASTER_DB, WAREHOUSE_DB_DIR
from db import WAREHOUSE_SCHEMA, init_master_db, init_warehouse_db


LEGACY_DB = BASE_DIR / "inventory.db"
SEED_WAREHOUSE_CODE = "wh_001"
SEED_WAREHOUSE_NAME = "中央仓"


def _ensure_audit_log(db_path: Path) -> None:
    """Add audit_log to a db that lacks it."""
    with closing(sqlite3.connect(db_path)) as conn:
        cols = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if "audit_log" not in cols:
            conn.executescript(WAREHOUSE_SCHEMA)
            conn.commit()


def migrate_legacy_inventory() -> Path:
    """Copy inventory.db into the warehouses directory as wh_001.db.

    Returns the path of the new warehouse db.
    """
    if not LEGACY_DB.exists():
        raise FileNotFoundError(f"Legacy db not found: {LEGACY_DB}")

    init_master_db()
    WAREHOUSE_DB_DIR.mkdir(parents=True, exist_ok=True)

    target = WAREHOUSE_DB_DIR / f"{SEED_WAREHOUSE_CODE}.db"

    # Make sure audit_log exists in legacy db before copying.
    _ensure_audit_log(LEGACY_DB)

    shutil.copy2(LEGACY_DB, target)
    # Re-run ensure on the copy in case the destination was older than
    # the schema dict at copy time.
    _ensure_audit_log(target)
    init_warehouse_db(target)

    # Register in master.db.
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rel_path = str(target.relative_to(BASE_DIR))
    with closing(sqlite3.connect(MASTER_DB)) as conn:
        existing = conn.execute(
            "SELECT id FROM warehouses WHERE code=?", (SEED_WAREHOUSE_CODE,)
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO warehouses (code, name, db_path, created_at) VALUES (?, ?, ?, ?)",
                (SEED_WAREHOUSE_CODE, SEED_WAREHOUSE_NAME, rel_path, now),
            )
            conn.commit()

    return target


if __name__ == "__main__":
    out = migrate_legacy_inventory()
    print(f"Migrated to {out}")
