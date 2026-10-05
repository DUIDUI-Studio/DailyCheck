"""Database wiring for the multi-warehouse model.

master.db stores users, warehouses, and per-warehouse role bindings.
Each warehouse has its own SQLite file under db/warehouses/<code>.db
that holds the full business schema (categories, items, movements, ...).

get_warehouse_db() routes to the db bound to g.warehouse_db_path, which
is set by the before_request hook in auth.py based on session.
"""
from __future__ import annotations

import fcntl
import sqlite3
import tempfile
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
    warehouse_type TEXT NOT NULL DEFAULT 'storefront',
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

-- Cross-warehouse recipe publishing (R&D → storefronts).
-- Versioned with draft → published lifecycle.
CREATE TABLE IF NOT EXISTS recipe_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_type TEXT NOT NULL,            -- 'ic_recipe' | 'recipe'
    recipe_id INTEGER NOT NULL,           -- id in source warehouse
    source_warehouse_code TEXT NOT NULL,
    version INTEGER NOT NULL,             -- 1, 2, 3...
    status TEXT NOT NULL DEFAULT 'draft', -- 'draft' | 'published' | 'superseded'
    snapshot_json TEXT NOT NULL,           -- frozen copy of recipe + BOM + items
    notes TEXT,
    created_by INTEGER,
    created_at TEXT NOT NULL,
    published_at TEXT,
    -- One version number per recipe (within a source warehouse). Without
    -- recipe_id this constraint blocks every other recipe from reaching
    -- the same version number, even though they live in different rows.
    UNIQUE(recipe_type, recipe_id, version)
);
CREATE INDEX IF NOT EXISTS idx_recipe_versions_lookup
    ON recipe_versions(recipe_type, recipe_id, status);

CREATE TABLE IF NOT EXISTS recipe_publish_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_version_id INTEGER NOT NULL,
    summary TEXT,
    status TEXT NOT NULL DEFAULT 'pending',  -- 'pending'|'partial'|'complete'|'failed'
    started_by INTEGER,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    target_warehouse_codes_json TEXT NOT NULL,
    FOREIGN KEY (recipe_version_id) REFERENCES recipe_versions(id)
);

CREATE TABLE IF NOT EXISTS recipe_publish_event_warehouses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    publish_event_id INTEGER NOT NULL,
    warehouse_code TEXT NOT NULL,
    status TEXT NOT NULL,                  -- 'success'|'failed'
    error_message TEXT,
    applied_at TEXT,
    FOREIGN KEY (publish_event_id) REFERENCES recipe_publish_events(id),
    UNIQUE(publish_event_id, warehouse_code)
);

-- Cross-warehouse item / category publishing (R&D → storefronts).
CREATE TABLE IF NOT EXISTS item_publish_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    started_by INTEGER,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    target_warehouse_codes_json TEXT NOT NULL,
    item_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS item_publish_event_warehouses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    publish_event_id INTEGER NOT NULL,
    warehouse_code TEXT NOT NULL,
    status TEXT NOT NULL,
    error_message TEXT,
    applied_at TEXT,
    FOREIGN KEY (publish_event_id) REFERENCES item_publish_events(id)
);

CREATE TABLE IF NOT EXISTS item_publish_event_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    publish_event_id INTEGER NOT NULL,
    item_id INTEGER NOT NULL,
    source_warehouse_code TEXT NOT NULL,
    target_warehouse_code TEXT NOT NULL,
    status TEXT NOT NULL,                  -- 'success'|'failed'|'skipped'
    error_message TEXT,
    action TEXT NOT NULL DEFAULT 'overwrite',  -- 'overwrite'|'keep'|'merge'
    FOREIGN KEY (publish_event_id) REFERENCES item_publish_events(id)
);

-- ─────────────────────────────────────────────────────────────────────
-- Canonical items (master-data) — Spec docs/2026-10-03-canonical-item-design.md
-- Q1=deny / Q2=open / Q3=deactivate_only / Q4=freeze / Q7=strict
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS canonical_categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,             -- 跨仓稳定键（如 PRODUCE_CONSUMABLE）
    name TEXT NOT NULL,                    -- 标准名（如 生产消耗品）
    description TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_canonical_categories_code
    ON canonical_categories(code);

CREATE TABLE IF NOT EXISTS canonical_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_sku TEXT NOT NULL UNIQUE,    -- IC-000001，业务上只读（两阶段写入）
    name TEXT NOT NULL,                    -- 标准名
    category_code TEXT,                    -- 指向 canonical_categories.code（不存 id）
    unit TEXT NOT NULL,                    -- 锁死
    gram_per_unit REAL NOT NULL DEFAULT 0, -- 锁死
    aux_unit TEXT,                         -- 锁死
    aux_rate REAL NOT NULL DEFAULT 0,      -- 锁死
    selling_price REAL,                    -- Q6=canonical_managed 时使用
    unit_cost REAL,                        -- Q6=canonical_managed 时使用
    barcode TEXT,                          -- P0 不下发，留在主数据侧
    status TEXT NOT NULL DEFAULT 'active', -- 'active'|'inactive' (Q3)
    created_from TEXT NOT NULL DEFAULT 'rd_manual', -- 'rd_manual'|'rd_publish'|'claim_merge'
    created_by INTEGER,                    -- users.id
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_canonical_items_status
    ON canonical_items(status);
CREATE INDEX IF NOT EXISTS idx_canonical_items_category
    ON canonical_items(category_code);

CREATE TABLE IF NOT EXISTS canonical_item_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_id INTEGER NOT NULL,
    alias TEXT NOT NULL,                   -- 归一化前的原始写法
    normalized_alias TEXT,                 -- 归一化后，用于查重
    warehouse_code TEXT,                   -- 空=全局别名；非空=某仓独有
    source TEXT NOT NULL DEFAULT 'system_suggested', -- 'rd_manual'|'storefront_claim'|'system_suggested'
    created_at TEXT NOT NULL,
    FOREIGN KEY (canonical_id) REFERENCES canonical_items(id)
);
CREATE INDEX IF NOT EXISTS idx_canonical_item_aliases_canon
    ON canonical_item_aliases(canonical_id);

CREATE TABLE IF NOT EXISTS canonical_claim_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    warehouse_code TEXT NOT NULL,          -- 申请方
    local_item_id INTEGER,                 -- 申请方仓内 items.id（new_item 时为 NULL）
    local_sku TEXT,                        -- 申请方仓内 sku
    local_name TEXT,                       -- 申请方拟建或拟认领的品项名
    canonical_id INTEGER,                  -- 目标主数据（new_item 时为 NULL）
    local_keep_name TEXT,                  -- 申请方想保留的叫法，空=跟随主数据
    proposed_category_code TEXT,           -- new_item 时建议的品类 code
    proposed_unit TEXT,                    -- new_item 时建议的单位
    request_type TEXT NOT NULL DEFAULT 'claim',  -- 'claim'|'new_item'|'exempt'
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'pending',-- 'pending'|'approved'|'rejected'|'cancelled'
    submitted_by INTEGER,
    reviewed_by INTEGER,
    reviewed_at TEXT,
    review_note TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (canonical_id) REFERENCES canonical_items(id)
);
CREATE INDEX IF NOT EXISTS idx_canonical_claim_requests_status
    ON canonical_claim_requests(status);

CREATE TABLE IF NOT EXISTS canonical_conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_id INTEGER NOT NULL,
    warehouse_code TEXT NOT NULL,
    local_item_id INTEGER NOT NULL,
    publish_event_id INTEGER,
    field TEXT NOT NULL,                   -- 冲突字段名
    conflict_type TEXT NOT NULL,           -- 'value'|'unit_conversion'|'double_bind'
    canonical_value TEXT,                  -- 下发值
    local_value TEXT,                      -- 门店本地值
    last_synced_value TEXT,                -- 上次下发值
    status TEXT NOT NULL DEFAULT 'open',   -- 'open'|'resolved_keep_local'|'resolved_accept_canonical'|'waived'
    resolution_note TEXT,
    resolved_by INTEGER,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY (canonical_id) REFERENCES canonical_items(id)
);
CREATE INDEX IF NOT EXISTS idx_canonical_conflicts_status
    ON canonical_conflicts(status, warehouse_code);

CREATE TABLE IF NOT EXISTS canonical_publish_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary TEXT,
    status TEXT NOT NULL DEFAULT 'pending', -- 'pending'|'partial'|'complete'|'failed'
    started_by INTEGER,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    target_warehouse_codes_json TEXT NOT NULL,
    item_count INTEGER NOT NULL DEFAULT 0,
    backup_paths_json TEXT                -- 措施①：记录本事件触发的备份路径
);
CREATE INDEX IF NOT EXISTS idx_canonical_publish_events_status
    ON canonical_publish_events(status, started_at);

CREATE TABLE IF NOT EXISTS canonical_publish_event_warehouses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    publish_event_id INTEGER NOT NULL,
    warehouse_code TEXT NOT NULL,
    status TEXT NOT NULL,                  -- 'success'|'failed'|'partial_conflict'
    error_message TEXT,
    applied_at TEXT,
    FOREIGN KEY (publish_event_id) REFERENCES canonical_publish_events(id),
    UNIQUE(publish_event_id, warehouse_code)
);

CREATE TABLE IF NOT EXISTS canonical_publish_event_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    publish_event_id INTEGER NOT NULL,
    canonical_id INTEGER NOT NULL,
    target_warehouse_code TEXT NOT NULL,
    local_item_id INTEGER,                 -- 命中/新建的仓内 items.id
    status TEXT NOT NULL,                  -- 'success'|'failed'|'conflict'|'skipped'
    applied_fields_json TEXT,              -- 实际写入的字段
    skipped_fields_json TEXT,              -- 因冲突冻结的字段
    error_message TEXT,
    FOREIGN KEY (publish_event_id) REFERENCES canonical_publish_events(id)
);
CREATE INDEX IF NOT EXISTS idx_canonical_publish_event_items_event
    ON canonical_publish_event_items(publish_event_id);

-- ============================================================
-- 门店订货（Store Ordering）跨仓数据模型
-- ============================================================

-- 购物车：每个门店用户在同一门店下只有一个购物车
CREATE TABLE IF NOT EXISTS store_order_carts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    store_warehouse_code TEXT NOT NULL,
    dc_warehouse_code TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (user_id, store_warehouse_code),
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (store_warehouse_code) REFERENCES warehouses(code),
    FOREIGN KEY (dc_warehouse_code) REFERENCES warehouses(code)
);

CREATE INDEX IF NOT EXISTS idx_store_order_carts_user
    ON store_order_carts(user_id, store_warehouse_code);

-- 购物车明细：以 canonical_id 为跨仓统一键
CREATE TABLE IF NOT EXISTS store_order_cart_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cart_id INTEGER NOT NULL,
    canonical_id INTEGER NOT NULL,
    quantity REAL NOT NULL,
    unit TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (cart_id) REFERENCES store_order_carts(id) ON DELETE CASCADE,
    FOREIGN KEY (canonical_id) REFERENCES canonical_items(id)
);

CREATE INDEX IF NOT EXISTS idx_store_order_cart_items_cart
    ON store_order_cart_items(cart_id);

-- 订单主表
CREATE TABLE IF NOT EXISTS store_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no TEXT NOT NULL UNIQUE,
    store_warehouse_code TEXT NOT NULL,
    dc_warehouse_code TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    requested_by INTEGER NOT NULL,
    expected_delivery_date TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    approved_by INTEGER,
    approved_at TEXT,
    approved_note TEXT,
    shipped_by INTEGER,
    shipped_at TEXT,
    delivered_at TEXT,
    cancelled_by INTEGER,
    cancelled_at TEXT,
    cancel_reason TEXT,
    FOREIGN KEY (store_warehouse_code) REFERENCES warehouses(code),
    FOREIGN KEY (dc_warehouse_code) REFERENCES warehouses(code),
    FOREIGN KEY (requested_by) REFERENCES users(id),
    FOREIGN KEY (approved_by) REFERENCES users(id),
    FOREIGN KEY (shipped_by) REFERENCES users(id),
    FOREIGN KEY (cancelled_by) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_store_orders_store
    ON store_orders(store_warehouse_code, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_store_orders_dc
    ON store_orders(dc_warehouse_code, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_store_orders_created
    ON store_orders(created_at DESC);

-- 订单明细
CREATE TABLE IF NOT EXISTS store_order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    canonical_id INTEGER NOT NULL,
    dc_item_id INTEGER,                 -- 配送中心仓内 items.id（出库时回填）
    store_item_id INTEGER,              -- 门店仓内 items.id（用于收货/入库，P0 仅记录）
    quantity REAL NOT NULL,             -- 订货数量（基础单位）
    unit TEXT NOT NULL,
    fulfilled_quantity REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending', -- pending / fulfilled / partial / cancelled
    created_at TEXT NOT NULL,
    FOREIGN KEY (order_id) REFERENCES store_orders(id) ON DELETE CASCADE,
    FOREIGN KEY (canonical_id) REFERENCES canonical_items(id)
);

CREATE INDEX IF NOT EXISTS idx_store_order_items_order
    ON store_order_items(order_id);

-- 订单状态历史
CREATE TABLE IF NOT EXISTS store_order_status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    actor_id INTEGER,
    note TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (order_id) REFERENCES store_orders(id) ON DELETE CASCADE,
    FOREIGN KEY (actor_id) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_store_order_status_history_order
    ON store_order_status_history(order_id, created_at DESC);

-- 配送记录
CREATE TABLE IF NOT EXISTS store_order_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    delivery_no TEXT NOT NULL UNIQUE,
    shipped_by INTEGER,
    shipped_at TEXT NOT NULL,
    delivered_at TEXT,
    tracking_note TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (order_id) REFERENCES store_orders(id) ON DELETE CASCADE,
    FOREIGN KEY (shipped_by) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_store_order_deliveries_order
    ON store_order_deliveries(order_id);

-- ============================================================
-- 门店订货收货（Store Order Receiving）—— v2 P0
-- 多次部分收货：fulfilled_quantity 累计；全部收齐 → delivered
-- ============================================================
CREATE TABLE IF NOT EXISTS store_order_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    order_item_id INTEGER NOT NULL,
    quantity REAL NOT NULL,
    received_by INTEGER NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (order_id) REFERENCES store_orders(id) ON DELETE CASCADE,
    FOREIGN KEY (order_item_id) REFERENCES store_order_items(id) ON DELETE CASCADE,
    FOREIGN KEY (received_by) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_store_order_receipts_order
    ON store_order_receipts(order_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_store_order_receipts_item
    ON store_order_receipts(order_item_id, created_at DESC);
"""

# v3 P0 增量：ALTER 必须在 MASTER_SCHEMA 之外，否则 executescript 在已迁移的
# master.db 上会因列已存在而崩溃。沿用 warehouses.warehouse_type 的迁移模式：
# 由 init_master_db() 的 PRAGMA 守卫负责幂等 ALTER。

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
    aux_unit TEXT,
    aux_rate REAL NOT NULL DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS ic_recipes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    note TEXT,
    output_unit TEXT NOT NULL DEFAULT 'g',
    output_qty REAL NOT NULL DEFAULT 100,
    sale_price REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ic_recipe_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ic_recipe_id INTEGER NOT NULL,
    item_id INTEGER NOT NULL,
    qty_per_unit REAL NOT NULL,
    UNIQUE(ic_recipe_id, item_id),
    FOREIGN KEY (ic_recipe_id) REFERENCES ic_recipes(id) ON DELETE CASCADE,
    FOREIGN KEY (item_id) REFERENCES items(id)
);

CREATE TABLE IF NOT EXISTS recipes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    note TEXT,
    output_unit TEXT NOT NULL DEFAULT '件',
    output_qty REAL NOT NULL DEFAULT 1,
    sale_price REAL NOT NULL DEFAULT 0,
    sale_price_updated_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recipe_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id INTEGER NOT NULL,
    source_type TEXT NOT NULL,
    item_id INTEGER,
    ic_recipe_id INTEGER,
    qty_per_unit REAL NOT NULL,
    CHECK (source_type IN ('item', 'ic_recipe')),
    CHECK (
        (source_type = 'item' AND item_id IS NOT NULL AND ic_recipe_id IS NULL)
     OR (source_type = 'ic_recipe' AND ic_recipe_id IS NOT NULL AND item_id IS NULL)
    ),
    FOREIGN KEY (recipe_id) REFERENCES recipes(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_recipe_items_recipe ON recipe_items(recipe_id);
CREATE INDEX IF NOT EXISTS idx_ic_recipe_items_ic_recipe ON ic_recipe_items(ic_recipe_id);
"""


def init_master_db() -> None:
    """Create the master.db schema if it does not exist yet.

    Also runs idempotent column-add migrations on pre-existing tables
    (CREATE TABLE IF NOT EXISTS is a no-op for an existing table, so
    any columns added in newer releases must be ALTERed here).
    Safe to call on every app startup — every check is gated by a
    PRAGMA table_info lookup so the ALTER only runs when needed.

    The whole body is serialized behind an advisory file lock (fcntl
    flock). gunicorn boots multiple workers concurrently, each of which
    calls this from create_app(); without the lock, two workers can both
    pass the PRAGMA table_info check and then both issue the same
    ALTER TABLE ADD COLUMN, crashing the loser with
    "duplicate column name" (issue #9). The lock is held only for the
    milliseconds it takes to run the migration, so it has no impact on
    steady-state runtime.
    """
    WAREHOUSE_DB_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = Path(tempfile.gettempdir()) / "dailycheck-init-master.lock"
    with lock_path.open("a") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            with closing(sqlite3.connect(MASTER_DB)) as conn:
                conn.executescript(MASTER_SCHEMA)
                # Idempotent column migrations for tables that pre-date these columns.
                wh_cols = {r[1] for r in conn.execute("PRAGMA table_info(warehouses)").fetchall()}
                if "warehouse_type" not in wh_cols:
                    conn.execute(
                        "ALTER TABLE warehouses ADD COLUMN warehouse_type TEXT "
                        "NOT NULL DEFAULT 'storefront'"
                    )

                # v3 P0: store_order_items.shipped_quantity (partial-ship accumulator).
                # SQLite 没有 ADD COLUMN IF NOT EXISTS，用 PRAGMA 守卫做幂等迁移。
                soi_cols = {
                    r[1] for r in conn.execute(
                        "PRAGMA table_info(store_order_items)"
                    ).fetchall()
                }
                if "shipped_quantity" not in soi_cols:
                    conn.execute(
                        "ALTER TABLE store_order_items ADD COLUMN "
                        "shipped_quantity REAL NOT NULL DEFAULT 0"
                    )

                # recipe_versions UNIQUE constraint fix.
                # The original schema had UNIQUE(recipe_type,
                # source_warehouse_code, version) — missing recipe_id — so
                # two recipes in the same source warehouse could never share a
                # version number, which blocked creating a draft for any
                # recipe after the first. The correct constraint is
                # (recipe_type, recipe_id, version). SQLite can't DROP a
                # UNIQUE in-place, so we rebuild the table when the wrong
                # auto-index exists.
                rv_indexes = [
                    r[0] for r in conn.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='index' AND tbl_name='recipe_versions'"
                    ).fetchall()
                ]
                if "sqlite_autoindex_recipe_versions_1" in rv_indexes:
                    # Find which columns the auto-index covers.
                    auto_idx_info = conn.execute(
                        "SELECT sql FROM sqlite_master "
                        "WHERE type='index' AND name='sqlite_autoindex_recipe_versions_1'"
                    ).fetchone()
                    # sql is None for auto-indexes (defined by UNIQUE clause);
                    # query the columns directly via pragma_index_info.
                    auto_cols = [
                        r[2] for r in conn.execute(
                            "PRAGMA index_info('sqlite_autoindex_recipe_versions_1')"
                        ).fetchall()
                    ]
                    needs_rebuild = auto_cols != ["recipe_type", "recipe_id", "version"]
                    if needs_rebuild:
                        # Backup → rebuild → swap. Safe because the new
                        # table is the same shape; we just change the UNIQUE.
                        conn.executescript("""
                            CREATE TABLE IF NOT EXISTS recipe_versions_new (
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
                                UNIQUE(recipe_type, recipe_id, version)
                            );
                            INSERT INTO recipe_versions_new
                                SELECT * FROM recipe_versions;
                            DROP TABLE recipe_versions;
                            ALTER TABLE recipe_versions_new RENAME TO recipe_versions;
                            CREATE INDEX IF NOT EXISTS idx_recipe_versions_lookup
                                ON recipe_versions(recipe_type, recipe_id, status);
                        """)

                # Seed single-row procurement_config if missing (id=1 is the only row).
                row = conn.execute("SELECT 1 FROM procurement_config WHERE id=1").fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO procurement_config (id, cover_days, min_absolute) VALUES (1, 14, 0)"
                    )
                conn.commit()
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


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
    # Ensure new warehouse dbs also receive all column migrations that
    # are normally applied lazily by get_warehouse_db().
    migrate_warehouse_db_columns(db_path)


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
        if "aux_unit" not in item_cols:
            conn.execute("ALTER TABLE items ADD COLUMN aux_unit TEXT")
        if "aux_rate" not in item_cols:
            conn.execute(
                "ALTER TABLE items ADD COLUMN aux_rate REAL NOT NULL DEFAULT 0"
            )
        # 一次性把旧启用克的行同步到 aux_unit/aux_rate（幂等：仅当 aux_rate=0
        # 且 gram_per_unit>0 且 aux_unit IS NULL 时执行）
        conn.execute(
            """UPDATE items SET aux_rate = gram_per_unit, aux_unit = '克'
               WHERE aux_rate = 0
                 AND gram_per_unit > 0
                 AND aux_unit IS NULL"""
        )
        if "selling_price" not in item_cols:
            conn.execute(
                "ALTER TABLE items ADD COLUMN selling_price REAL NOT NULL DEFAULT 0"
            )
        if "selling_price_updated_at" not in item_cols:
            conn.execute(
                "ALTER TABLE items ADD COLUMN selling_price_updated_at TEXT"
            )
        # ─────────────────────────────────────────────────────────────────
        # Canonical-item columns (Spec §2.2). All idempotent via PRAGMA.
        # Safe with SQLite weak typing: NOT NULL DEFAULT 0 for booleans,
        # nullable for foreign keys and JSON snapshots.
        # ─────────────────────────────────────────────────────────────────
        if "canonical_id" not in item_cols:
            conn.execute("ALTER TABLE items ADD COLUMN canonical_id INTEGER")
        if "is_alias" not in item_cols:
            conn.execute(
                "ALTER TABLE items ADD COLUMN is_alias INTEGER NOT NULL DEFAULT 0"
            )
        if "canonical_status" not in item_cols:
            conn.execute("ALTER TABLE items ADD COLUMN canonical_status TEXT")
        if "canonical_synced_json" not in item_cols:
            conn.execute("ALTER TABLE items ADD COLUMN canonical_synced_json TEXT")
        if "is_store_exclusive" not in item_cols:
            conn.execute(
                "ALTER TABLE items ADD COLUMN is_store_exclusive INTEGER NOT NULL DEFAULT 0"
            )
        # Spec §2.2: future-proofing for Q6=canonical_managed (M1 keeps default 0)
        if "price_follow_canonical" not in item_cols:
            conn.execute(
                "ALTER TABLE items ADD COLUMN price_follow_canonical INTEGER NOT NULL DEFAULT 0"
            )
        # Index for canonical_id lookups (used by fanout engine + claim verification)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_items_canonical_id "
            "ON items(canonical_id)"
        )
        # categories.canonical_code (Spec §2.3): nullable — empty means "纯本仓自定义"
        cat_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(categories)").fetchall()
        }
        if "canonical_code" not in cat_cols:
            conn.execute("ALTER TABLE categories ADD COLUMN canonical_code TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_categories_canonical_code "
            "ON categories(canonical_code)"
        )
        conn.commit()
