"""One-shot migration: add warehouse_type column to warehouses + create rd_001.

Safe to run multiple times — ALTER TABLE is skipped if column already present,
and INSERT is skipped if rd_001 already exists.

Run via: /opt/dailycheck/.venv/bin/python scripts/migrate_add_warehouse_type.py
"""
from __future__ import annotations

import datetime
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MASTER_DB = ROOT / "db" / "master.db"
WAREHOUSE_DB_DIR = ROOT / "db" / "warehouses"


def main() -> int:
    if not MASTER_DB.exists():
        print(f"master.db not found at {MASTER_DB}; skipping (probably a fresh dev env)")
        return 0

    conn = sqlite3.connect(MASTER_DB)
    conn.row_factory = sqlite3.Row
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(warehouses)").fetchall()]
        if "warehouse_type" not in cols:
            conn.execute(
                "ALTER TABLE warehouses ADD COLUMN warehouse_type TEXT "
                "NOT NULL DEFAULT 'storefront'"
            )
            print("ALTER: added warehouses.warehouse_type column")
        else:
            print("ALTER: warehouse_type already present (fresh CREATE TABLE)")

        # Create rd_001 if missing
        existing = conn.execute(
            "SELECT id FROM warehouses WHERE code = ?", ("rd_001",)
        ).fetchone()
        if existing:
            print("rd_001 already exists; skipping INSERT")
        else:
            db_path = WAREHOUSE_DB_DIR / "rd_001.db"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            # Lazy import to avoid path issues
            sys.path.insert(0, str(ROOT))
            from db import init_warehouse_db
            init_warehouse_db(db_path)
            rel_path = str(db_path.relative_to(ROOT))
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "INSERT INTO warehouses (code, name, db_path, warehouse_type, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("rd_001", "研发中心", rel_path, "rd", now),
            )
            print(f"INSERT: created rd_001 (研发中心) at {rel_path}")

        conn.commit()
        print("---warehouses after migration---")
        for r in conn.execute(
            "SELECT id, code, name, warehouse_type FROM warehouses ORDER BY id"
        ):
            print(f"  {r['id']:>2}  {r['code']:<8} {r['warehouse_type']:<10} {r['name']}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
