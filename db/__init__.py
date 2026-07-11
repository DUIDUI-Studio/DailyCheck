"""Database wiring for the multi-warehouse model.

master.db stores users, warehouses, and per-warehouse role bindings.
Each warehouse has its own SQLite file under db/warehouses/<code>.db
that holds the full business schema (categories, items, movements, ...).

get_warehouse_db() routes to the db bound to g.warehouse_db_path, which
is set by the before_request hook in auth.py based on session.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from flask import current_app, g

from config import MASTER_DB, WAREHOUSE_DB_DIR


def get_master_db() -> sqlite3.Connection:
    """Get a connection to the platform-level master.db."""
    if "master_db" not in g:
        conn = sqlite3.connect(MASTER_DB)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.master_db = conn
    return g.master_db


def get_warehouse_db() -> sqlite3.Connection:
    """Get a connection to the currently selected warehouse db.

    Raises RuntimeError if no warehouse is selected (caller is responsible
    for redirecting to /login or /select-warehouse before this fires).
    """
    if "wh_db" not in g:
        path = g.get("warehouse_db_path")
        if not path:
            raise RuntimeError("No warehouse selected")
        # Idempotent column migrations for legacy dbs. Cheap when
        # already up-to-date (just a PRAGMA lookup).
        migrate_warehouse_db_columns(Path(path))
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.wh_db = conn
    return g.wh_db


def close_dbs(_: Any) -> None:
    """Tear down both per-request connections."""
    for key in ("wh_db", "master_db"):
        db = g.pop(key, None)
        if db is not None:
            db.close()


# ---------------------------------------------------------------------------
# Schema management
# ---------------------------------------------------------------------------

MASTER_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS warehouses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    db_path TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS warehouse_users (
    user_id INTEGER NOT NULL,
    warehouse_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    PRIMARY KEY (user_id, warehouse_id),
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (warehouse_id) REFERENCES warehouses(id)
);

CREATE TABLE IF NOT EXISTS forecast_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    items_processed INTEGER NOT NULL DEFAULT 0,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_forecast_runs_status ON forecast_runs(status, started_at);

CREATE TABLE IF NOT EXISTS procurement_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    cover_days INTEGER NOT NULL DEFAULT 14,
    min_absolute REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS procurement_cache (
    item_id INTEGER NOT NULL,
    warehouse_code TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    daily_avg REAL NOT NULL,
    current_qty REAL NOT NULL,
    in_transit_qty REAL NOT NULL,
    safety_stock REAL NOT NULL,
    suggested_qty INTEGER NOT NULL,
    invalid INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (item_id, warehouse_code)
);

CREATE INDEX IF NOT EXISTS idx_procurement_cache_invalid ON procurement_cache(invalid);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    target_url TEXT,
    created_at TEXT NOT NULL,
    read_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_notif_user_created ON notifications(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notif_user_unread ON notifications(user_id, read_at);

CREATE TABLE IF NOT EXISTS notification_prefs (
    user_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    channel TEXT NOT NULL,
    muted INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, event_type, channel),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS agent_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    allowed_read_paths_json TEXT NOT NULL DEFAULT '[]',
    allowed_write_paths_json TEXT NOT NULL DEFAULT '[]',
    allowed_warehouse_codes_json TEXT NOT NULL DEFAULT '[]'
);
"""

# Mirrors the schema that app.py shipped pre-refactor. Audit_log is new.
WAREHOUSE_SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    category_id INTEGER NOT NULL,
    quantity REAL NOT NULL DEFAULT 0,
    safety_stock REAL NOT NULL DEFAULT 0,
    unit TEXT NOT NULL DEFAULT '件',
    unit_cost REAL NOT NULL DEFAULT 0,
    gram_per_unit REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (category_id) REFERENCES categories(id)
);

CREATE TABLE IF NOT EXISTS stock_movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    delta REAL NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (item_id) REFERENCES items(id)
);

CREATE TABLE IF NOT EXISTS stocktakes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL,
    previous_quantity REAL NOT NULL,
    actual_quantity REAL NOT NULL,
    diff REAL NOT NULL,
    batch_id INTEGER,
    created_at TEXT NOT NULL,
    note TEXT,
    FOREIGN KEY (item_id) REFERENCES items(id)
);

CREATE TABLE IF NOT EXISTS stocktake_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    note TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    rolled_back INTEGER NOT NULL DEFAULT 0,
    loss_req_ids TEXT
);

CREATE TABLE IF NOT EXISTS restock_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL,
    requested_quantity REAL NOT NULL,
    reason TEXT,
    status TEXT NOT NULL DEFAULT '提交',
    created_at TEXT NOT NULL,
    FOREIGN KEY (item_id) REFERENCES items(id)
);

CREATE TABLE IF NOT EXISTS outbound_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL,
    requested_quantity REAL NOT NULL,
    reason TEXT,
    status TEXT NOT NULL DEFAULT '提交',
    rolled_back INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (item_id) REFERENCES items(id)
);

CREATE TABLE IF NOT EXISTS adjustment_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL,
    adjusted_quantity REAL NOT NULL,
    reason TEXT,
    rolled_back INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (item_id) REFERENCES items(id)
);

CREATE TABLE IF NOT EXISTS daily_revenue (
    date TEXT NOT NULL PRIMARY KEY,
    amount REAL NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    username TEXT,
    action TEXT NOT NULL,
    target_type TEXT,
    target_id INTEGER,
    detail TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    unit TEXT NOT NULL DEFAULT '件',
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS product_bom (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    item_id INTEGER NOT NULL,
    qty_per_unit REAL NOT NULL,
    UNIQUE(product_id, item_id),
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE,
    FOREIGN KEY (item_id) REFERENCES items(id)
);

CREATE TABLE IF NOT EXISTS production_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    output_qty REAL NOT NULL,
    note TEXT,
    rolled_back INTEGER NOT NULL DEFAULT 0,
    created_by TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (product_id) REFERENCES products(id)
);

CREATE TABLE IF NOT EXISTS production_run_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    item_id INTEGER NOT NULL,
    planned_qty REAL NOT NULL,
    actual_qty REAL NOT NULL,
    FOREIGN KEY (run_id) REFERENCES production_runs(id) ON DELETE CASCADE,
    FOREIGN KEY (item_id) REFERENCES items(id)
);

CREATE INDEX IF NOT EXISTS idx_prun_created ON production_runs(created_at);
CREATE INDEX IF NOT EXISTS idx_pruni_run ON production_run_items(run_id);
"""


def init_master_db() -> None:
    """Create the master.db schema if it does not exist yet."""
    WAREHOUSE_DB_DIR.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(MASTER_DB)) as conn:
        conn.executescript(MASTER_SCHEMA)
        # Seed single-row procurement_config if missing (id=1 is the only row).
        row = conn.execute("SELECT 1 FROM procurement_config WHERE id=1").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO procurement_config (id, cover_days, min_absolute) VALUES (1, 14, 0)"
            )
        conn.commit()


def init_warehouse_db(db_path: Path, seed_categories=None) -> None:
    """Create the schema for one warehouse db if missing, and seed fixed categories.

    seed_categories=None 时退回 config.FIXED_CATEGORIES(默认行为不变)。
    传入自定义 tuple 可让新建仓库用别的品类集合(本次 spec 不调用,留作未来)。

    Also runs idempotent column-add migrations for tables that pre-date
    some columns (CREATE TABLE IF NOT EXISTS is a no-op for existing
    tables, so missing columns must be added separately).
    """
    from datetime import datetime
    if seed_categories is None:
        from config import FIXED_CATEGORIES
        seed_categories = FIXED_CATEGORIES

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(WAREHOUSE_SCHEMA)
        # Defensive column-add migrations for legacy warehouse dbs that
        # were created before the column was introduced.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(stocktake_batches)").fetchall()}
        if "status" not in cols:
            conn.execute(
                "ALTER TABLE stocktake_batches ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'"
            )
        existing = {r[0] for r in conn.execute("SELECT name FROM categories").fetchall()}
        for name in seed_categories:
            if name not in existing:
                conn.execute(
                    "INSERT INTO categories (name, description, created_at) VALUES (?, ?, ?)",
                    (name, "系统固定品类", ts),
                )
        conn.commit()


def migrate_warehouse_db_columns(db_path: Path) -> None:
    """Run idempotent column-add migrations on an EXISTING warehouse db.

    Safe to call on every request — every check is gated by a
    PRAGMA table_info lookup so the ALTER only runs when needed.

    Called from get_warehouse_db() so legacy dbs get patched on the
    fly without requiring a separate init step.
    """
    if not db_path.exists():
        return
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(WAREHOUSE_SCHEMA)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(stocktake_batches)").fetchall()}
        if "status" not in cols:
            conn.execute(
                "ALTER TABLE stocktake_batches ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'"
            )
        if "loss_req_ids" not in cols:
            conn.execute(
                "ALTER TABLE stocktake_batches ADD COLUMN loss_req_ids TEXT"
            )
        item_cols = {r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()}
        if "gram_per_unit" not in item_cols:
            conn.execute(
                "ALTER TABLE items ADD COLUMN gram_per_unit REAL NOT NULL DEFAULT 0"
            )
        conn.commit()
