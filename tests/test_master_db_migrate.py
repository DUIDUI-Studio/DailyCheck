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


def test_recipe_versions_unique_includes_recipe_id(tmp_path, monkeypatch):
    """recipe_versions UNIQUE must include recipe_id, not source_warehouse_code.

    Regression: the original schema had UNIQUE(recipe_type,
    source_warehouse_code, version) which blocked creating a draft for
    any second recipe in the same source warehouse. The fix replaces it
    with UNIQUE(recipe_type, recipe_id, version) via init_master_db's
    rebuild branch.
    """
    import db as db_module
    import config as config_module
    master = tmp_path / "master.db"
    monkeypatch.setattr(db_module, "MASTER_DB", master)
    monkeypatch.setattr(config_module, "MASTER_DB", master)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", tmp_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", tmp_path)

    # Seed a "legacy" master with the wrong unique constraint.
    import sqlite3
    conn = sqlite3.connect(master)
    conn.executescript("""
        CREATE TABLE recipe_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipe_type TEXT NOT NULL,
            recipe_id INTEGER NOT NULL,
            source_warehouse_code TEXT NOT NULL,
            version INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            snapshot_json TEXT NOT NULL,
            notes TEXT,
            created_by INTEGER,
            created_at TEXT NOT NULL,
            published_at TEXT,
            UNIQUE(recipe_type, source_warehouse_code, version)
        );
        INSERT INTO recipe_versions (recipe_type, recipe_id, source_warehouse_code, version,
                                      status, snapshot_json, created_at)
        VALUES ('ic_recipe', 3, 'rd_001', 1, 'published', '{}', '2026-09-03 10:00:00');
    """)
    conn.commit()
    conn.close()

    from db import init_master_db
    init_master_db()

    conn = sqlite3.connect(master)
    # After rebuild, recipe_versions should have the correct unique index.
    idx_cols = [r[2] for r in conn.execute(
        "PRAGMA index_info('sqlite_autoindex_recipe_versions_1')"
    ).fetchall()]
    assert idx_cols == ["recipe_type", "recipe_id", "version"], \
        f"unique index must be on (recipe_type, recipe_id, version), got {idx_cols}"

    # Legacy row should still be present (rebuild preserves data).
    cnt = conn.execute("SELECT COUNT(*) FROM recipe_versions").fetchone()[0]
    assert cnt == 1

    # Now two recipes in the same source warehouse can share version 1.
    conn.execute("""INSERT INTO recipe_versions
        (recipe_type, recipe_id, source_warehouse_code, version,
         status, snapshot_json, created_at)
        VALUES ('ic_recipe', 4, 'rd_001', 1, 'draft', '{}', '2026-09-03 11:00:00')""")
    conn.commit()
    cnt = conn.execute("SELECT COUNT(*) FROM recipe_versions").fetchone()[0]
    assert cnt == 2
    conn.close()
