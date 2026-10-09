"""P0-9 价格收归主数据 —— 策略 / 扇出 / 回填 测试。

方案：docs/2026-10-09-item-master-unify-plan.md §3 P0-9、§4 风险第一行。

覆盖：
  1. 策略常量：Q6=canonical_managed 时价格进可下发集、离开门禁/门店自治集
  2. 扇出：主数据价下发到门店；主数据为空(NULL)时保留门店现价
  3. 扇出：门店价漂移 → Q4=freeze 进冲突队列，门店价不被覆盖
  4. 回填：全仓最高价写入 canonical_items；已有值默认不覆盖
  5. create / update canonical_item 的价格参数与校验
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    """建好 master.db + 一个仓 db，返回 (master_path, wh_path)。"""
    import config as config_module
    import db as db_module
    from db import init_master_db, init_warehouse_db

    master_path = tmp_path / "master.db"
    wh_dir = tmp_path / "warehouses"
    wh_dir.mkdir()
    wh_path = wh_dir / "wh_test.db"

    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_dir)
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", wh_dir)

    init_master_db()
    init_warehouse_db(wh_path)
    return master_path, wh_path


def _m(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _seed_bound_item(wh_path, canonical_id, *, selling_price=0.0, unit_cost=0.0,
                     synced_json=None):
    """在仓 db 插一个已绑定 canonical 的品项，返回 item_id。"""
    conn = _m(wh_path)
    cat_id = conn.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()["id"]
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "INSERT INTO items (sku, name, category_id, quantity, safety_stock, "
        "unit_cost, selling_price, unit, gram_per_unit, updated_at, "
        "canonical_id, canonical_synced_json) "
        "VALUES (?, ?, ?, 0, 0, ?, ?, '件', 0, ?, ?, ?)",
        ("SKU-1", "主数据品项", cat_id, unit_cost, selling_price, ts,
         canonical_id, json.dumps(synced_json) if synced_json else None),
    )
    item_id = cur.lastrowid
    conn.commit()
    conn.close()
    return item_id


def _read_item(wh_path, item_id):
    conn = _m(wh_path)
    row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    conn.close()
    return row


# ─────────────── 1. 策略常量 ───────────────

def test_price_in_syncable_and_not_never_touch():
    from blueprints import canonical_pure as cp

    assert cp.is_syncable_field("selling_price")
    assert cp.is_syncable_field("unit_cost")
    assert "selling_price" not in cp.NEVER_TOUCH_COLUMNS
    assert "unit_cost" not in cp.NEVER_TOUCH_COLUMNS
    assert "selling_price" not in cp.STOREFRONT_OWNED_FIELDS
    assert "unit_cost" not in cp.STOREFRONT_OWNED_FIELDS
    assert "selling_price" in cp.BATCH_EDITABLE_FIELDS
    assert "unit_cost" in cp.BATCH_EDITABLE_FIELDS


# ─────────────── 2. 扇出：写入 / 空值守卫 ───────────────

def test_fanout_writes_price_when_set(env):
    from blueprints import canonical_pure as cp

    master_path, wh_path = env
    m = _m(master_path)
    created = cp.create_canonical_item(
        m, name="价品", unit="件", selling_price=99.0, unit_cost=12.0,
    )
    cid = created["id"]
    item_id = _seed_bound_item(wh_path, cid, selling_price=0.0, unit_cost=0.0)

    wh = _m(wh_path)
    try:
        result = cp.fanout_canonical_items(
            m, {"wh_test": wh}, canonical_ids=[cid], warehouse_codes=["wh_test"],
        )
    finally:
        wh.close()
        m.close()

    assert result["total_frozen"] == 0
    row = _read_item(wh_path, item_id)
    assert float(row["selling_price"]) == 99.0
    assert float(row["unit_cost"]) == 12.0


def test_fanout_null_price_preserves_store_price(env):
    """主数据未定价(NULL) → 不下发、保留门店现行价（2026-10-09 决策）。"""
    from blueprints import canonical_pure as cp

    master_path, wh_path = env
    m = _m(master_path)
    created = cp.create_canonical_item(m, name="无价品", unit="件")  # price NULL
    cid = created["id"]
    item_id = _seed_bound_item(wh_path, cid, selling_price=55.0, unit_cost=7.0)

    wh = _m(wh_path)
    try:
        result = cp.fanout_canonical_items(
            m, {"wh_test": wh}, canonical_ids=[cid], warehouse_codes=["wh_test"],
        )
    finally:
        wh.close()
        m.close()

    assert result["total_frozen"] == 0
    row = _read_item(wh_path, item_id)
    assert float(row["selling_price"]) == 55.0   # 未被清 0
    assert float(row["unit_cost"]) == 7.0


def test_fanout_insert_writes_price_for_new_row(env):
    """目标仓缺行 → INSERT 新行也应带主数据价（且不得因 NULL 撞 NOT NULL）。"""
    from blueprints import canonical_pure as cp

    master_path, wh_path = env
    m = _m(master_path)
    cp.seed_canonical_categories(m)
    cat = m.execute(
        "SELECT code, name FROM canonical_categories ORDER BY code LIMIT 1"
    ).fetchone()
    created = cp.create_canonical_item(
        m, name="新品", unit="件", category_code=cat["code"],
        selling_price=33.0, unit_cost=11.0,
    )
    cid = created["id"]

    wh = _m(wh_path)
    wh.execute(
        "UPDATE categories SET canonical_code=? WHERE name=?",
        (cat["code"], cat["name"]),
    )
    wh.commit()
    try:
        result = cp.fanout_canonical_items(
            m, {"wh_test": wh}, canonical_ids=[cid], warehouse_codes=["wh_test"],
        )
    finally:
        wh.close()
        m.close()

    # 不能有 failed/error（回归：NULL 价撞 NOT NULL 曾在此处炸）
    per_wh = result["per_canonical"][0]["per_warehouse"][0]
    assert "error" not in per_wh, per_wh
    assert per_wh["inserted"] is True and result["total_frozen"] == 0

    conn = _m(wh_path)
    row = conn.execute(
        "SELECT selling_price, unit_cost FROM items WHERE canonical_id=?", (cid,)
    ).fetchone()
    conn.close()
    assert float(row["selling_price"]) == 33.0
    assert float(row["unit_cost"]) == 11.0


def test_fanout_price_zero_is_pushed(env):
    """主数据显式 0 是「定价为零」，与 NULL 不同 —— 应下发。"""
    from blueprints import canonical_pure as cp

    master_path, wh_path = env
    m = _m(master_path)
    created = cp.create_canonical_item(
        m, name="零价品", unit="件", selling_price=0.0, unit_cost=0.0,
    )
    cid = created["id"]
    item_id = _seed_bound_item(wh_path, cid, selling_price=55.0, unit_cost=7.0)

    wh = _m(wh_path)
    try:
        cp.fanout_canonical_items(
            m, {"wh_test": wh}, canonical_ids=[cid], warehouse_codes=["wh_test"],
        )
    finally:
        wh.close()
        m.close()

    row = _read_item(wh_path, item_id)
    assert float(row["selling_price"]) == 0.0
    assert float(row["unit_cost"]) == 0.0


# ─────────────── 3. 扇出：价漂移进 freeze ───────────────

def test_fanout_price_conflict_frozen(env):
    from blueprints import canonical_pure as cp

    master_path, wh_path = env
    m = _m(master_path)
    created = cp.create_canonical_item(
        m, name="冲突品", unit="件", selling_price=100.0,
    )
    cid = created["id"]
    # 上次下发价 = 100，门店改成 80 → 属「门店已偏离」→ 冻结
    item_id = _seed_bound_item(
        wh_path, cid, selling_price=80.0,
        synced_json={
            "name": "冲突品", "unit": "件", "gram_per_unit": 0,
            "aux_unit": None, "aux_rate": 0, "selling_price": 100.0,
        },
    )

    wh = _m(wh_path)
    try:
        result = cp.fanout_canonical_items(
            m, {"wh_test": wh}, canonical_ids=[cid], warehouse_codes=["wh_test"],
        )
    finally:
        wh.close()

    assert result["total_frozen"] >= 1
    row = _read_item(wh_path, item_id)
    assert float(row["selling_price"]) == 80.0    # 门店价未被覆盖

    conflicts = [
        dict(r) for r in m.execute(
            "SELECT * FROM canonical_conflicts WHERE status='open'"
        ).fetchall()
    ]
    assert any(c["field"] == "selling_price" for c in conflicts)
    m.close()


# ─────────────── 4. 回填：全仓最高价 ───────────────

def test_backfill_takes_max_across_warehouses(env):
    from blueprints import canonical_pure as cp

    master_path, wh_path = env
    m = _m(master_path)
    created = cp.create_canonical_item(m, name="回填品", unit="件")
    cid = created["id"]

    wh2_path = wh_path.parent / "wh_two.db"
    from db import init_warehouse_db
    init_warehouse_db(wh2_path)

    _seed_bound_item(wh_path, cid, selling_price=10.0, unit_cost=5.0)
    _seed_bound_item(wh2_path, cid, selling_price=30.0, unit_cost=25.0)

    wh = _m(wh_path)
    wh2 = _m(wh2_path)
    try:
        result = cp.backfill_prices_from_warehouses(
            m, {"wh_test": wh, "wh_two": wh2},
        )
    finally:
        wh.close()
        wh2.close()

    assert result["filled_count"] == 2
    row = m.execute(
        "SELECT selling_price, unit_cost FROM canonical_items WHERE id=?", (cid,)
    ).fetchone()
    assert float(row["selling_price"]) == 30.0    # 全仓最高价
    assert float(row["unit_cost"]) == 25.0
    m.close()


def test_backfill_does_not_overwrite_existing_by_default(env):
    from blueprints import canonical_pure as cp

    master_path, wh_path = env
    m = _m(master_path)
    created = cp.create_canonical_item(
        m, name="已有价", unit="件", selling_price=200.0,
    )
    cid = created["id"]
    _seed_bound_item(wh_path, cid, selling_price=30.0, unit_cost=25.0)

    wh = _m(wh_path)
    try:
        result = cp.backfill_prices_from_warehouses(m, {"wh_test": wh})
    finally:
        wh.close()

    assert result["skipped_existing"] >= 1
    row = m.execute(
        "SELECT selling_price, unit_cost FROM canonical_items WHERE id=?", (cid,)
    ).fetchone()
    assert float(row["selling_price"]) == 200.0   # 未被覆盖
    assert float(row["unit_cost"]) == 25.0        # NULL → 填入


def test_backfill_ignores_zero_and_blank(env):
    """各仓全 0 / 无绑定 → 无可回填值，保持 NULL。"""
    from blueprints import canonical_pure as cp

    master_path, wh_path = env
    m = _m(master_path)
    created = cp.create_canonical_item(m, name="全零品", unit="件")
    cid = created["id"]
    _seed_bound_item(wh_path, cid, selling_price=0.0, unit_cost=0.0)

    wh = _m(wh_path)
    try:
        result = cp.backfill_prices_from_warehouses(m, {"wh_test": wh})
    finally:
        wh.close()

    assert result["filled_count"] == 0
    row = m.execute(
        "SELECT selling_price, unit_cost FROM canonical_items WHERE id=?", (cid,)
    ).fetchone()
    assert row["selling_price"] is None
    assert row["unit_cost"] is None
    m.close()


# ─────────────── 5. create / update canonical_item ───────────────

def test_create_and_update_canonical_price_and_validation(env):
    from blueprints import canonical_pure as cp

    master_path, _ = env
    m = _m(master_path)
    created = cp.create_canonical_item(
        m, name="校验品", unit="件", selling_price=5.0, unit_cost=3.0,
    )
    cid = created["id"]

    updated = cp.update_canonical_item(
        m, cid, {"selling_price": "12.5", "unit_cost": None},
    )
    assert float(updated["selling_price"]) == 12.5
    assert updated["unit_cost"] is None           # 空 → NULL

    with pytest.raises(ValueError):
        cp.update_canonical_item(m, cid, {"selling_price": -1})
    with pytest.raises(ValueError):
        cp.update_canonical_item(m, cid, {"unit_cost": "abc"})
    m.close()
