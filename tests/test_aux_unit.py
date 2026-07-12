"""多辅单位换算 — 单元测试与集成测试。"""
import sqlite3
from pathlib import Path

import pytest

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


# --- 品项编辑保存测试 ---


def test_edit_item_switches_from_gram_to_piece_blocked_if_bom(logged_client):
    """被 product_bom 引用且原启用克时禁止切走克。"""
    client, wh_path = logged_client
    # seed item 启用克
    from tests.conftest import _seed_item, _wh
    item_id, _ = _seed_item(wh_path, "test-milk", qty=10, unit_cost=2.0, gram_per_unit=1000)
    conn = _wh(wh_path)
    # 同步写入 aux_unit（旧 _seed_item 不带这俩列；手动补）
    conn.execute("UPDATE items SET aux_unit='克', aux_rate=1000 WHERE id=?", (item_id,))
    # 建 product + product_bom 引用该 item
    from datetime import datetime
    ts2 = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute("INSERT INTO products (name, unit, note, created_at) VALUES ('p1','件','',?)", (ts2,))
    pid = cur.lastrowid
    conn.execute("INSERT INTO product_bom (product_id, item_id, qty_per_unit) VALUES (?, ?, 100)", (pid, item_id))
    conn.commit()
    conn.close()

    # 尝试切到 '个' → 应被拒绝
    client.post(f"/items/{item_id}/edit", data={
        "name": "test-milk", "category_id": "1",
        "aux_unit": "个", "aux_rate": "12",
        "safety_stock": "0", "unit_cost": "2", "unit": "箱",
    }, follow_redirects=True)

    conn = _wh(wh_path)
    r = conn.execute("SELECT aux_unit, aux_rate, gram_per_unit FROM items WHERE id=?", (item_id,)).fetchone()
    # 应保留为克
    assert r["aux_unit"] == "克"
    assert r["aux_rate"] == 1000
    assert r["gram_per_unit"] == 1000
    conn.close()


def test_edit_item_switches_from_gram_ok_if_no_bom(logged_client):
    """无 product_bom 引用时允许切走克，gram_per_unit 落为 0。"""
    client, wh_path = logged_client
    from tests.conftest import _seed_item, _wh

    item_id, _ = _seed_item(wh_path, "test-shake", qty=5, unit_cost=2.0, gram_per_unit=1000)
    conn = _wh(wh_path)
    conn.execute("UPDATE items SET aux_unit='克', aux_rate=1000 WHERE id=?", (item_id,))
    conn.commit()
    conn.close()

    client.post(f"/items/{item_id}/edit", data={
        "name": "test-shake", "category_id": "1",
        "aux_unit": "个", "aux_rate": "12",
        "safety_stock": "0", "unit_cost": "2", "unit": "箱",
    }, follow_redirects=True)

    conn = _wh(wh_path)
    r = conn.execute("SELECT aux_unit, aux_rate, gram_per_unit FROM items WHERE id=?", (item_id,)).fetchone()
    assert r["aux_unit"] == "个"
    assert r["aux_rate"] == 12
    assert r["gram_per_unit"] == 0
    conn.close()


def test_create_item_with_game_syncs_gram_per_unit(logged_client):
    """新建 item aux_unit='克' 同步写 gram_per_unit。"""
    client, wh_path = logged_client
    from tests.conftest import _wh

    client.post("/items", data={
        "name": "牛乳1L", "category_id": "1",
        "quantity": "10", "safety_stock": "2", "unit_cost": "100",
        "unit": "箱", "aux_unit": "克", "aux_rate": "1000",
    }, follow_redirects=True)

    conn = _wh(wh_path)
    r = conn.execute("SELECT aux_unit, aux_rate, gram_per_unit FROM items WHERE name='牛乳1L'").fetchone()
    assert r["aux_unit"] == "克"
    assert r["aux_rate"] == 1000
    assert r["gram_per_unit"] == 1000
    conn.close()


def test_create_item_no_aux_unit(logged_client):
    """新建 item 不填 aux_unit → aux_unit=NULL, aux_rate=0。"""
    client, wh_path = logged_client
    from tests.conftest import _wh

    client.post("/items", data={
        "name": "无辅品项", "category_id": "1",
        "quantity": "5", "safety_stock": "0", "unit_cost": "50",
        "unit": "包", "aux_unit": "", "aux_rate": "0",
    }, follow_redirects=True)

    conn = _wh(wh_path)
    r = conn.execute("SELECT aux_unit, aux_rate, gram_per_unit FROM items WHERE name='无辅品项'").fetchone()
    assert r["aux_unit"] is None
    assert r["aux_rate"] == 0
    assert r["gram_per_unit"] == 0
    conn.close()


def test_create_item_negative_aux_rate_rejected(logged_client):
    """负 aux_rate 应被 flash 报错，不写入。"""
    client, wh_path = logged_client
    from tests.conftest import _wh

    client.post("/items", data={
        "name": "负品项", "category_id": "1",
        "quantity": "5", "safety_stock": "0", "unit_cost": "50",
        "unit": "包", "aux_unit": "个", "aux_rate": "-3",
    }, follow_redirects=True)

    conn = _wh(wh_path)
    r = conn.execute("SELECT id FROM items WHERE name='负品项'").fetchone()
    assert r is None
    conn.close()


# --- 出库 submit 使用辅单位 ---

def test_outbound_submit_with_aux_unit(logged_client):
    """24 个 (aux_unit='个', aux_rate=12) 出库 = 2 箱扣减。"""
    client, wh_path = logged_client
    from tests.conftest import _seed_item, _wh

    item_id, _ = _seed_item(wh_path, "test-shoyu", qty=10, unit_cost=10)
    conn = _wh(wh_path)
    conn.execute("UPDATE items SET aux_unit='个', aux_rate=12 WHERE id=?", (item_id,))
    conn.commit()
    conn.close()

    client.post("/outbound/submit", data={
        f"outbound_{item_id}": "24",
        f"outbound_{item_id}_unit": "aux",
        "reason": "test",
    }, follow_redirects=True)

    conn = _wh(wh_path)
    r = conn.execute("SELECT quantity FROM items WHERE id=?", (item_id,)).fetchone()
    # 10 箱 - 2 箱 = 8 箱
    assert r["quantity"] == pytest.approx(8.0)
    req = conn.execute(
        "SELECT requested_quantity FROM outbound_requests WHERE item_id=? ORDER BY id DESC LIMIT 1",
        (item_id,)).fetchone()
    assert req["requested_quantity"] == pytest.approx(2.0)
    conn.close()


def test_outbound_submit_with_base_unit_default(logged_client):
    """不传 _unit 字段，默认 base（基础单位），保持向后兼容。"""
    client, wh_path = logged_client
    from tests.conftest import _seed_item, _wh

    item_id, _ = _seed_item(wh_path, "test-plain", qty=5, unit_cost=10)
    client.post("/outbound/submit", data={
        f"outbound_{item_id}": "1",
        "reason": "test",
    }, follow_redirects=True)

    conn = _wh(wh_path)
    r = conn.execute("SELECT quantity FROM items WHERE id=?", (item_id,)).fetchone()
    assert r["quantity"] == pytest.approx(4.0)
    conn.close()


# --- 入库 submit 使用辅单位 ---

def test_restock_submit_with_aux_unit(logged_client):
    """24 个 (aux_unit='个', aux_rate=12) 入库 = 2 箱新增。"""
    client, wh_path = logged_client
    from tests.conftest import _seed_item, _wh

    item_id, _ = _seed_item(wh_path, "test-sauce", qty=5, unit_cost=10)
    conn = _wh(wh_path)
    conn.execute("UPDATE items SET aux_unit='个', aux_rate=12 WHERE id=?", (item_id,))
    conn.commit()
    conn.close()

    client.post("/restock/submit", data={
        f"restock_{item_id}": "24",
        f"restock_{item_id}_unit": "aux",
        "reason": "test",
    }, follow_redirects=True)

    conn = _wh(wh_path)
    r = conn.execute("SELECT quantity FROM items WHERE id=?", (item_id,)).fetchone()
    # 5 箱 + 2 箱 = 7 箱
    assert r["quantity"] == pytest.approx(7.0)
    req = conn.execute(
        "SELECT requested_quantity FROM restock_requests WHERE item_id=? ORDER BY id DESC LIMIT 1",
        (item_id,)).fetchone()
    assert req["requested_quantity"] == pytest.approx(2.0)
    conn.close()
