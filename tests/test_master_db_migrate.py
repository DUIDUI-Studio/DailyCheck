"""Regression: init_master_db() must auto-add warehouse_type to legacy master.db.

Background: a freshly-deployed prod VPS may have a master.db created by
an older release (warehouses table missing the warehouse_type column).
Without this auto-migration, the first authenticated request would crash
with 'no such column: warehouse_type'. init_master_db() runs from
create_app() on every app start, so it must be safe + idempotent.
"""
from datetime import datetime


def _legacy_master(path) -> None:
    """Build a master.db that mirrors the pre-CostReview schema."""
    import sqlite3
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            last_login_at TEXT
        );
        CREATE TABLE warehouses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            db_path TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE warehouse_users (
            user_id INTEGER NOT NULL,
            warehouse_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            PRIMARY KEY (user_id, warehouse_id),
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (warehouse_id) REFERENCES warehouses(id)
        );
        INSERT INTO warehouses (code, name, db_path, created_at)
        VALUES ('wh_001', '中央仓', 'db/warehouses/wh_001.db', '2026-01-01 00:00:00');
        INSERT INTO warehouses (code, name, db_path, created_at)
        VALUES ('wh_002', '新世界店', 'db/warehouses/wh_002.db', '2026-01-01 00:00:00');
    """)
    conn.commit()
    conn.close()


def test_init_master_db_adds_warehouse_type_to_legacy_master(tmp_path, monkeypatch):
    """Legacy master.db (no warehouse_type column) gets patched on init."""
    import db as db_module
    import config as config_module
    master = tmp_path / "master.db"
    _legacy_master(master)
    monkeypatch.setattr(db_module, "MASTER_DB", master)
    monkeypatch.setattr(config_module, "MASTER_DB", master)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", tmp_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", tmp_path)

    from db import init_master_db
    init_master_db()

    import sqlite3
    conn = sqlite3.connect(master)
    conn.row_factory = sqlite3.Row
    cols = {r[1] for r in conn.execute("PRAGMA table_info(warehouses)").fetchall()}
    assert "warehouse_type" in cols, "warehouse_type column not added"
    # Existing rows must default to 'storefront'.
    types = {r["code"]: r["warehouse_type"]
             for r in conn.execute(
                 "SELECT code, warehouse_type FROM warehouses"
             ).fetchall()}
    assert types == {"wh_001": "storefront", "wh_002": "storefront"}, \
        f"existing rows should default to storefront, got {types}"
    conn.close()


def test_init_master_db_is_idempotent(tmp_path, monkeypatch):
    """Calling init_master_db() twice doesn't error and doesn't re-add."""
    import db as db_module
    import config as config_module
    master = tmp_path / "master.db"
    _legacy_master(master)
    monkeypatch.setattr(db_module, "MASTER_DB", master)
    monkeypatch.setattr(config_module, "MASTER_DB", master)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", tmp_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", tmp_path)

    from db import init_master_db
    init_master_db()
    init_master_db()  # second call must be a no-op

    import sqlite3
    conn = sqlite3.connect(master)
    wh_cols = {r[1] for r in conn.execute("PRAGMA table_info(warehouses)").fetchall()}
    # warehouse_type column appears exactly once.
    assert sum(1 for c in wh_cols if c == "warehouse_type") == 1
    conn.close()
