"""多辅单位换算 — 单元测试与集成测试。"""
import sqlite3
from pathlib import Path

from blueprints._helpers import aux_to_base, base_to_aux, grams_to_stock

# --- 纯函数单元测试 ---

def test_aux_to_basic():
    assert aux_to_base(1440, 1000) == 1.44

def test_aux_to_base_disabled():
    assert aux_to_base(6, 0) == 6

def test_aux_to_base_negative_rate_disabled():
    assert aux_to_base(5, -1) == 5

def test_aux_to_base_another_rate():
    assert aux_to_base(24, 12) == 2.0  # 24 个 / (1 箱=12 个) = 2 箱

def test_aux_to_base_tiny():
    assert aux_to_base(10, 500) == 0.02

def test_aux_to_base_rounding():
    assert aux_to_base(1000, 3) == 333.33

def test_aux_to_base_zero():
    assert aux_to_base(0, 1000) == 0.0

def test_base_to_aux():
    assert base_to_aux(1.44, 1000) == 1440
def test_base_to_aux_pieces():
    assert base_to_aux(2, 12) == 24
def test_base_to_aux_disabled():
    assert base_to_aux(1.5, 0) == 1.5

def test_grams_to_stock_is_aux_to_base():
    assert grams_to_stock(1440, 1000) == 1.44

# --- 迁移幂等测试 ---


def test_migrate_adds_aux_columns_to_legacy_db(tmp_path):
    """旧 warehouse db（只有 gram_per_unit 列）跑迁移后应获得 aux_unit, aux_rate 列。"""
    from db import migrate_warehouse_db_columns

    wh_path = tmp_path / "old_wh.db"
    # 模拟旧 db：手动建表（缺 aux_*）
    conn = sqlite3.connect(wh_path)
    conn.executescript("""
        CREATE TABLE categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
            description TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
            category_id INTEGER NOT NULL,
            quantity REAL NOT NULL DEFAULT 0,
            safety_stock REAL NOT NULL DEFAULT 0,
            unit TEXT NOT NULL DEFAULT '件',
            unit_cost REAL NOT NULL DEFAULT 0,
            gram_per_unit REAL NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (category_id) REFERENCES categories(id)
        );
        CREATE TABLE stock_movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL,
            action TEXT NOT NULL, delta REAL NOT NULL, note TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (item_id) REFERENCES items(id)
        );
        CREATE TABLE stocktake_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL, note TEXT, rolled_back INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE stocktakes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL,
            previous_quantity REAL, actual_quantity REAL, diff REAL,
            batch_id INTEGER, created_at TEXT NOT NULL, note TEXT
        );
        INSERT INTO categories (name, created_at) VALUES ('包材', '2026-01-01 00:00:00');
        INSERT INTO items (sku, name, category_id, quantity, unit, gram_per_unit, updated_at)
            VALUES ('G1', '牛乳', 1, 10, '箱', 1000, '2026-01-01 00:00:00');
        INSERT INTO items (sku, name, category_id, quantity, unit, gram_per_unit, updated_at)
            VALUES ('G2', '黑椒汁', 2, 5, '箱', 0, '2026-01-01 00:00:00');
    """)
    # categories 插入不到一个对应给 G2，先补一个
    conn.execute("INSERT INTO categories (name, created_at) VALUES ('调味酱', '2026-01-01 00:00:00')")
    conn.commit()
    conn.close()

    # 跑迁移
    migrate_warehouse_db_columns(Path(wh_path))

    conn = sqlite3.connect(wh_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()}
    assert "aux_unit" in cols
    assert "aux_rate" in cols

    # 旧启用克的行被同步
    r = conn.execute("SELECT aux_unit, aux_rate, gram_per_unit FROM items WHERE sku='G1'").fetchone()
    assert r[0] == "克"
    assert r[1] == 1000
    # 原 gram_per_unit 列未被动
    assert r[2] == 1000

    # 旧未启用克的行不会被赋'克'
    r2 = conn.execute("SELECT aux_unit, aux_rate FROM items WHERE sku='G2'").fetchone()
    assert r2[0] is None
    assert r2[1] == 0
    conn.close()


def test_migrate_idempotent(tmp_path):
    """再次调 migrate 不应覆盖已有 aux_unit/aux_rate 值。"""
    from db import migrate_warehouse_db_columns

    wh_path = tmp_path / "old_wh2.db"
    conn = sqlite3.connect(wh_path)
    conn.executescript("""
        CREATE TABLE categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
            description TEXT, created_at TEXT NOT NULL);
        CREATE TABLE items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, sku TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL, category_id INTEGER NOT NULL,
            quantity REAL NOT NULL DEFAULT 0, safety_stock REAL NOT NULL DEFAULT 0,
            unit TEXT NOT NULL DEFAULT '件', unit_cost REAL NOT NULL DEFAULT 0,
            gram_per_unit REAL NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
            FOREIGN KEY (category_id) REFERENCES categories(id));
        CREATE TABLE stock_movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL,
            action TEXT NOT NULL, delta REAL NOT NULL, note TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (item_id) REFERENCES items(id));
        CREATE TABLE stocktake_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
            note TEXT, rolled_back INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE stocktakes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL,
            previous_quantity REAL, actual_quantity REAL, diff REAL,
            batch_id INTEGER, created_at TEXT NOT NULL, note TEXT);
        INSERT INTO categories (name, created_at) VALUES ('包材', '2026-01-01');
        INSERT INTO items (sku, name, category_id, quantity, unit, gram_per_unit, updated_at)
            VALUES ('G1', '牛乳', 1, 10, '箱', 1000, '2026-01-01');
    """)
    conn.commit()
    conn.close()

    # 第一次迁移
    migrate_warehouse_db_columns(Path(wh_path))
    # 手动改 aux_rate 模拟后续用户编辑
    conn = sqlite3.connect(wh_path)
    conn.execute("UPDATE items SET aux_rate=999, aux_unit='个' WHERE sku='G1'")
    conn.commit()
    conn.close()
    # 再跑迁移
    migrate_warehouse_db_columns(Path(wh_path))
    # 不应覆盖
    conn = sqlite3.connect(wh_path)
    r = conn.execute("SELECT aux_unit, aux_rate FROM items WHERE sku='G1'").fetchone()
    assert r[0] == "个"
    assert r[1] == 999
    conn.close()
