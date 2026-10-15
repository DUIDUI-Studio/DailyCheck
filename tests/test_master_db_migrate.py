"""Regression: init_master_db() must auto-add warehouse_type to legacy master.db.

Background: a freshly-deployed prod VPS may have a master.db created by
an older release (warehouses table missing the warehouse_type column).
Without this auto-migration, the first authenticated request would crash
with 'no such column: warehouse_type'. init_master_db() runs from
create_app() on every app start, so it must be safe + idempotent.
"""


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
    import config as config_module
    import db as db_module
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
    import config as config_module
    import db as db_module
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
    import config as config_module
    import db as db_module
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


def _v2_master_with_store_orders(path) -> None:
    """Build a master.db that mirrors the pre-v3 store-ordering schema:
    store_order_items exists but is missing the shipped_quantity column."""
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
            warehouse_type TEXT NOT NULL DEFAULT 'storefront',
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
        CREATE TABLE store_order_carts (
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
        CREATE TABLE store_order_cart_items (
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
        CREATE TABLE store_orders (
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
        -- v2 schema: NO shipped_quantity column.
        CREATE TABLE store_order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            canonical_id INTEGER NOT NULL,
            dc_item_id INTEGER,
            store_item_id INTEGER,
            quantity REAL NOT NULL,
            unit TEXT NOT NULL,
            fulfilled_quantity REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            FOREIGN KEY (order_id) REFERENCES store_orders(id) ON DELETE CASCADE,
            FOREIGN KEY (canonical_id) REFERENCES canonical_items(id)
        );
        INSERT INTO store_orders (id, order_no, store_warehouse_code, dc_warehouse_code,
                                  status, requested_by, created_at, updated_at)
        VALUES (1, 'SO-20260101-0001', 'wh_002', 'wh_000', 'shipped', 2,
                '2026-01-01 00:00:00', '2026-01-01 00:00:00');
        INSERT INTO store_order_items (order_id, canonical_id, dc_item_id, store_item_id,
                                       quantity, unit, fulfilled_quantity, status, created_at)
        VALUES (1, 1, 5, NULL, 5.0, '件', 5.0, 'fulfilled', '2026-01-01 00:00:00');
    """)
    conn.close()


def test_init_master_db_adds_shipped_quantity_to_legacy_master(tmp_path, monkeypatch):
    """v3 P0: Legacy master.db (store_order_items missing shipped_quantity) gets
    patched on init via PRAGMA guard. Existing rows default to 0 so old shipped
    orders display shipped_quantity=0 (acceptable; receive flow already works)."""
    import config as config_module
    import db as db_module
    master = tmp_path / "master.db"
    _v2_master_with_store_orders(master)
    monkeypatch.setattr(db_module, "MASTER_DB", master)
    monkeypatch.setattr(config_module, "MASTER_DB", master)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", tmp_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", tmp_path)

    from db import init_master_db
    init_master_db()

    import sqlite3
    conn = sqlite3.connect(master)
    conn.row_factory = sqlite3.Row
    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(store_order_items)"
    ).fetchall()}
    assert "shipped_quantity" in cols, \
        "shipped_quantity column not added to legacy store_order_items"
    # Existing rows must default to 0.
    shipped = conn.execute(
        "SELECT shipped_quantity FROM store_order_items WHERE order_id=1"
    ).fetchone()["shipped_quantity"]
    assert shipped == 0.0
    conn.close()


def test_init_master_db_shipped_quantity_idempotent(tmp_path, monkeypatch):
    """Calling init_master_db() twice doesn't error and doesn't re-add."""
    import config as config_module
    import db as db_module
    master = tmp_path / "master.db"
    _v2_master_with_store_orders(master)
    monkeypatch.setattr(db_module, "MASTER_DB", master)
    monkeypatch.setattr(config_module, "MASTER_DB", master)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", tmp_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", tmp_path)

    from db import init_master_db
    init_master_db()
    init_master_db()  # second call must be a no-op

    import sqlite3
    conn = sqlite3.connect(master)
    soi_cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(store_order_items)"
    ).fetchall()}
    assert sum(1 for c in soi_cols if c == "shipped_quantity") == 1
    conn.close()


# ---------------------------------------------------------------------------
# 权限精细化（Phase 3）：RBAC 5 表 + 播种 + 启动断言
# ---------------------------------------------------------------------------

def _init_empty(tmp_path, monkeypatch):
    """空路径 init_master_db，返回 master 路径。"""
    import config as config_module
    import db as db_module
    master = tmp_path / "master.db"
    monkeypatch.setattr(db_module, "MASTER_DB", master)
    monkeypatch.setattr(config_module, "MASTER_DB", master)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", tmp_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", tmp_path)
    from db import init_master_db
    init_master_db()
    return master


def test_rbac_tables_created_on_empty_db(tmp_path, monkeypatch):
    """空库 init 后 5 张权限表存在。"""
    import sqlite3
    master = _init_empty(tmp_path, monkeypatch)
    conn = sqlite3.connect(master)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    need = {"permissions", "roles", "role_permissions",
            "permission_routes", "approval_policies"}
    assert need <= tables, f"缺失表：{need - tables}"
    conn.close()


def test_rbac_seed_row_counts(tmp_path, monkeypatch):
    """播种：permissions==25；内置 roles==5；各包权限数精确等于契约。"""
    import sqlite3

    from permissions_manifest import BUILTIN_PACKAGE_SIZES
    master = _init_empty(tmp_path, monkeypatch)
    conn = sqlite3.connect(master)
    assert conn.execute("SELECT COUNT(*) FROM permissions").fetchone()[0] == 25
    assert conn.execute(
        "SELECT COUNT(*) FROM roles WHERE is_builtin=1").fetchone()[0] == 5
    sizes = {r[0]: r[1] for r in conn.execute(
        """SELECT r.code, COUNT(rp.permission_code)
             FROM roles r LEFT JOIN role_permissions rp ON rp.role_id=r.id
            WHERE r.is_builtin=1 GROUP BY r.code""").fetchall()}
    assert sizes == BUILTIN_PACKAGE_SIZES, sizes
    conn.close()


def test_rbac_seed_idempotent(tmp_path, monkeypatch):
    """连续两次 init → 行数不变（无重复播种）。"""
    import sqlite3
    master = _init_empty(tmp_path, monkeypatch)
    import config as config_module
    import db as db_module
    from db import init_master_db
    monkeypatch.setattr(db_module, "MASTER_DB", master)
    monkeypatch.setattr(config_module, "MASTER_DB", master)
    init_master_db()
    conn = sqlite3.connect(master)
    perms = conn.execute("SELECT COUNT(*) FROM permissions").fetchone()[0]
    rp = conn.execute("SELECT COUNT(*) FROM role_permissions").fetchone()[0]
    roles = conn.execute("SELECT COUNT(*) FROM roles WHERE is_builtin=1").fetchone()[0]
    conn.close()
    assert perms == 25 and roles == 5 and rp == 52, (perms, roles, rp)


def test_agent_tokens_permission_codes_json_column_added(tmp_path, monkeypatch):
    """旧 master.db（agent_tokens 无该列）→ init 后 PRAGMA 含 permission_codes_json。"""
    import sqlite3
    master = _init_empty(tmp_path, monkeypatch)
    conn = sqlite3.connect(master)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_tokens)").fetchall()}
    assert "permission_codes_json" in cols
    conn.close()


def test_verify_builtin_packages_detects_empty(tmp_path, monkeypatch):
    """清空任一包（含旁支 viewer）→ 返回「空包」告警。"""
    import sqlite3

    from db import _verify_builtin_packages
    master = _init_empty(tmp_path, monkeypatch)
    conn = sqlite3.connect(master)
    rid = conn.execute("SELECT id FROM roles WHERE code='viewer'").fetchone()[0]
    conn.execute("DELETE FROM role_permissions WHERE role_id=?", (rid,))
    warnings = _verify_builtin_packages(conn)
    assert any("空权限包" in w and "viewer" in w for w in warnings), warnings
    conn.close()


def test_verify_builtin_packages_detects_broken_ladder(tmp_path, monkeypatch):
    """破坏 staff⊆manager → 返回「阶梯破坏」告警。"""
    import sqlite3

    from db import _verify_builtin_packages
    master = _init_empty(tmp_path, monkeypatch)
    conn = sqlite3.connect(master)
    rid = conn.execute("SELECT id FROM roles WHERE code='manager'").fetchone()[0]
    conn.execute(
        "DELETE FROM role_permissions WHERE role_id=? AND permission_code='order.create'",
        (rid,))
    warnings = _verify_builtin_packages(conn)
    assert any("阶梯破坏" in w for w in warnings), warnings
    conn.close()


def test_verify_builtin_packages_detects_size_drift(tmp_path, monkeypatch):
    """某包权限数 ≠ 契约 → 返回「权限数漂移」告警。"""
    import sqlite3

    from db import _verify_builtin_packages
    master = _init_empty(tmp_path, monkeypatch)
    conn = sqlite3.connect(master)
    rid = conn.execute("SELECT id FROM roles WHERE code='staff'").fetchone()[0]
    conn.execute(
        "INSERT OR IGNORE INTO role_permissions (role_id, permission_code) "
        "VALUES (?, 'order.approve')", (rid,))
    warnings = _verify_builtin_packages(conn)
    assert any("权限数漂移" in w and "staff" in w for w in warnings), warnings
    conn.close()

