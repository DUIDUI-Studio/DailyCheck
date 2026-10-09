"""品项主数据统一改造的策略测试（方案 §7.2）。

覆盖：
  #1 已绑定行：编辑页主数据字段 + 价格只读（服务端强制）
  #2 已绑定行：禁止物理删除；未绑定行仍可删
  #6 主数据维护权限收紧：/canonical/edit 需 platform admin
  #9 价格收归主数据：批量改价 + 存量价格回填（全仓最高价）
  #10 单仓启停：is_active 可停用/启用、扇出不覆盖、选择点排除
  #11 主数据批量统一修改：字段白名单 + 复用单条写入路径
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from tests.conftest import _seed_item, _wh


def _bind_canonical(wh_path, item_id: int, canonical_id: int = 1) -> None:
    conn = _wh(wh_path)
    conn.execute(
        "UPDATE items SET canonical_id=? WHERE id=?", (canonical_id, item_id)
    )
    conn.commit()
    conn.close()


def _item_row(wh_path, item_id: int):
    conn = _wh(wh_path)
    row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    conn.close()
    return row


def _seed_canonical(master_path, name, unit="件"):
    """在主数据表插一行，返回 canonical_id。"""
    conn = sqlite3.connect(master_path)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "INSERT INTO canonical_items (canonical_sku, name, unit, status, "
        "created_from, created_at, updated_at) VALUES (?, ?, ?, 'active', "
        "'rd_manual', ?, ?)",
        (f"IC-TEST-{name}", name, unit, ts, ts),
    )
    new_id = cur.lastrowid
    conn.commit()
    conn.close()
    return new_id


@pytest.fixture
def manager_client(tmp_path, monkeypatch):
    """admin=0 且仓库角色=manager 的客户端（conftest 只提供 admin/staff）。"""
    import db as db_module
    from db import init_master_db, init_warehouse_db

    master_path = tmp_path / "master.db"
    wh_dir = tmp_path / "warehouses"
    wh_dir.mkdir()
    wh_path = wh_dir / "wh_test.db"

    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_dir)
    import config as config_module
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", wh_dir)

    init_master_db()
    init_warehouse_db(wh_path)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    m = sqlite3.connect(master_path)
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (1, 'mgr1', 'x', 0, ?)", (ts,))
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, created_at) "
        "VALUES (1, 'wh_test', '测试仓', ?, ?)", (str(wh_path), ts))
    m.execute(
        "INSERT INTO warehouse_users (user_id, warehouse_id, role) "
        "VALUES (1, 1, 'manager')")
    m.commit()
    m.close()

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = 1
        s["warehouse_id"] = 1
    return client, wh_path


# ─────────────── P0-1 编辑页主数据字段只读 ───────────────

def test_edit_bound_item_ignores_master_fields(logged_client):
    """已绑定行：名称/品类/单位等主数据字段服务端强制忽略表单值。"""
    client, wh_path = logged_client
    item_id, cat_id = _seed_item(wh_path, "主数据品项", qty=0, unit_cost=5)
    _bind_canonical(wh_path, item_id)

    resp = client.post(
        f"/items/{item_id}/edit",
        data={
            "name": "被篡改的名字", "category_id": str(cat_id),
            "safety_stock": "999", "unit_cost": "888",
            "selling_price": "777", "unit": "篡改单位",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302

    row = _item_row(wh_path, item_id)
    assert row["name"] == "主数据品项"      # 未被改写
    assert row["unit"] == "件"              # 未被改写
    assert float(row["safety_stock"]) == 0  # 系统托管，不采信表单
    # P0-9：价格收归主数据 → 本页也不采信表单里的价格
    assert float(row["unit_cost"]) == 5     # 维持原值（种子 unit_cost=5）
    assert float(row["selling_price"]) == 0


def test_edit_bound_item_page_is_readonly(logged_client):
    client, wh_path = logged_client
    item_id, _ = _seed_item(wh_path, "只读品项", qty=0, unit_cost=5)
    _bind_canonical(wh_path, item_id)

    resp = client.get(f"/items/{item_id}/edit")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "由「品项主数据」统一维护" in body
    assert "去主数据" in body                  # 提供去主数据的跳转
    assert 'name="name"' not in body          # 没有可提交的名称输入框
    assert 'name="unit"' not in body          # 单位不可提交
    assert 'name="unit_cost"' not in body     # P0-9：价格不再可提交
    assert 'name="selling_price"' not in body
    assert "未定价" in body or "0" in body      # 价格以只读文本展示


def test_edit_unbound_item_still_updates(logged_client):
    """未绑定行维持原有可编辑行为（回归保护）。"""
    client, wh_path = logged_client
    item_id, cat_id = _seed_item(wh_path, "自由品项", qty=0, unit_cost=5)

    resp = client.post(
        f"/items/{item_id}/edit",
        data={
            "name": "改名后", "category_id": str(cat_id),
            "safety_stock": "3", "unit_cost": "9",
            "selling_price": "12", "unit": "箱",
        },
    )
    assert resp.status_code == 302

    row = _item_row(wh_path, item_id)
    assert row["name"] == "改名后"
    assert float(row["unit_cost"]) == 9
    assert float(row["selling_price"]) == 12
    # safety_stock 系统托管：即使表单传了也不采纳
    assert float(row["safety_stock"]) == 0


# ─────────────── P0-2 已绑定行禁止删除 ───────────────

def test_delete_bound_item_blocked(logged_client):
    client, wh_path = logged_client
    item_id, _ = _seed_item(wh_path, "不可删品项", qty=0, unit_cost=1)
    _bind_canonical(wh_path, item_id)

    resp = client.post(f"/items/{item_id}/delete")
    assert resp.status_code == 302
    assert _item_row(wh_path, item_id) is not None


def test_delete_unbound_item_without_usage_allowed(logged_client):
    client, wh_path = logged_client
    item_id, _ = _seed_item(wh_path, "可删品项", qty=0, unit_cost=1)

    resp = client.post(f"/items/{item_id}/delete")
    assert resp.status_code == 302
    assert _item_row(wh_path, item_id) is None


# ─────────────── P0-6 主数据维护权限收紧 ───────────────

def test_canonical_edit_blocked_for_warehouse_manager(manager_client):
    client, _ = manager_client
    assert client.get("/canonical/edit").status_code == 403
    assert client.post("/canonical/edit", data={
        "name": "越权新建", "unit": "件",
    }).status_code == 403


def test_canonical_edit_allowed_for_platform_admin(logged_client):
    client, _ = logged_client
    assert client.get("/canonical/edit").status_code == 200


# ─────────────── P0-10 单仓品项启停 ───────────────

def test_toggle_active_disables_item_and_hides_from_pickers(logged_client):
    """停用后：is_active=0、库存不动、出库选择点不再出现该品项。"""
    client, wh_path = logged_client
    item_id, _ = _seed_item(wh_path, "停用品项", qty=7, unit_cost=1)
    _bind_canonical(wh_path, item_id)

    assert "停用品项" in client.get("/outbound/session").data.decode()

    resp = client.post(f"/items/{item_id}/toggle-active", data={"active": "0"})
    assert resp.status_code == 302

    row = _item_row(wh_path, item_id)
    assert row["is_active"] == 0
    assert float(row["quantity"]) == 7        # 库存保留（Q7 零丢失）

    assert "停用品项" not in client.get("/outbound/session").data.decode()

    # 重新启用 → 回到选择点
    client.post(f"/items/{item_id}/toggle-active", data={"active": "1"})
    assert _item_row(wh_path, item_id)["is_active"] == 1
    assert "停用品项" in client.get("/outbound/session").data.decode()


def test_toggle_active_blocked_for_warehouse_manager(manager_client):
    client, wh_path = manager_client
    item_id, _ = _seed_item(wh_path, "经理不能停", qty=0, unit_cost=1)
    _bind_canonical(wh_path, item_id)

    resp = client.post(f"/items/{item_id}/toggle-active", data={"active": "0"})
    assert resp.status_code == 403
    assert _item_row(wh_path, item_id)["is_active"] == 1


def test_canonical_binding_active_route(logged_client):
    """主数据详情页按仓停用（P0-10 的第二个入口）。"""
    client, wh_path = logged_client
    item_id, _ = _seed_item(wh_path, "按仓停用", qty=0, unit_cost=1)
    _bind_canonical(wh_path, item_id, canonical_id=1)

    resp = client.post("/canonical/binding-active", data={
        "warehouse_code": "wh_test",
        "canonical_id": "1",
        "active": "0",
    })
    assert resp.status_code == 302
    assert _item_row(wh_path, item_id)["is_active"] == 0


def test_fanout_never_touches_is_active():
    """is_active 是本仓字段 —— 必须留在 NEVER_TOUCH_COLUMNS（扇出永不覆盖）。"""
    from blueprints import canonical_pure as cp

    assert "is_active" in cp.NEVER_TOUCH_COLUMNS
    assert "is_orderable" in cp.NEVER_TOUCH_COLUMNS
    with pytest.raises(cp.UpdateSqlViolation):
        cp.build_update_sql(
            conn=None,
            table="items",
            updates={"name": "x"},
            where_col="id",
            where_val=1,
            set_allowlist=("name", "is_active"),
        )


def test_is_active_migration_on_legacy_db(tmp_path):
    """旧库（user_version < 2）经幂等迁移补上 is_active 列。"""
    import db as db_module
    from db import WAREHOUSE_SCHEMA, migrate_warehouse_db_columns

    legacy_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy_path)
    conn.executescript(WAREHOUSE_SCHEMA)
    conn.commit()
    pre = {r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()}
    conn.close()
    assert "is_active" not in pre

    migrate_warehouse_db_columns(legacy_path)

    conn = sqlite3.connect(legacy_path)
    post = {r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()}
    assert "is_active" in post
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db_module.WAREHOUSE_SCHEMA_VERSION
    conn.close()


# ─────────────── P0-11 主数据批量统一修改 ───────────────

def _canonical_row(master_path, canonical_id):
    conn = sqlite3.connect(master_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM canonical_items WHERE id=?", (canonical_id,)
    ).fetchone()
    conn.close()
    return row


def test_batch_edit_unifies_field_and_redirects_to_fanout(logged_client):
    client, wh_path = logged_client
    master_path = wh_path.parent.parent / "master.db"
    c1 = _seed_canonical(master_path, "批量一", unit="件")
    c2 = _seed_canonical(master_path, "批量二", unit="件")

    resp = client.post("/canonical/batch-edit", data={
        "canonical_ids": [str(c1), str(c2)],
        "field": "unit",
        "value": "箱",
    }, follow_redirects=False)
    assert resp.status_code == 302
    assert "/canonical/fanout" in resp.headers["Location"]
    assert f"ids={c1}%2C{c2}" in resp.headers["Location"] or f"ids={c1},{c2}" in resp.headers["Location"]

    assert _canonical_row(master_path, c1)["unit"] == "箱"
    assert _canonical_row(master_path, c2)["unit"] == "箱"


def test_batch_edit_accepts_price_field(logged_client):
    """P0-9：价格收归主数据后，批量修改支持售价 / 进货价。"""
    client, wh_path = logged_client
    master_path = wh_path.parent.parent / "master.db"
    c1 = _seed_canonical(master_path, "批量改价", unit="件")

    resp = client.post("/canonical/batch-edit", data={
        "canonical_ids": [str(c1)],
        "field": "selling_price",
        "value": "99",
    }, follow_redirects=False)
    assert resp.status_code == 302
    assert float(_canonical_row(master_path, c1)["selling_price"]) == 99.0


def test_batch_edit_price_blank_clears_to_null(logged_client):
    """价格留空 = 主数据未定价（NULL），扇出时不下发。"""
    client, wh_path = logged_client
    master_path = wh_path.parent.parent / "master.db"
    c1 = _seed_canonical(master_path, "清空价", unit="件")
    conn = sqlite3.connect(master_path)
    conn.execute("UPDATE canonical_items SET unit_cost=12 WHERE id=?", (c1,))
    conn.commit()
    conn.close()

    resp = client.post("/canonical/batch-edit", data={
        "canonical_ids": [str(c1)], "field": "unit_cost", "value": "",
    })
    assert resp.status_code == 302
    assert _canonical_row(master_path, c1)["unit_cost"] is None


def test_batch_edit_requires_platform_admin(manager_client):
    client, _ = manager_client
    resp = client.post("/canonical/batch-edit", data={
        "canonical_ids": ["1"], "field": "unit", "value": "箱",
    })
    assert resp.status_code == 403


def test_backfill_prices_route_fills_canonical(logged_client):
    """P0-9 回填路由：各仓现行价 → canonical_items（全仓最高价）。"""
    client, wh_path = logged_client
    master_path = wh_path.parent.parent / "master.db"
    c1 = _seed_canonical(master_path, "回填路由品", unit="件")
    item_id, _ = _seed_item(wh_path, "回填路由品", qty=0, unit_cost=0)
    _bind_canonical(wh_path, item_id, canonical_id=c1)
    conn = sqlite3.connect(wh_path)
    conn.execute(
        "UPDATE items SET selling_price=42, unit_cost=21 WHERE id=?", (item_id,)
    )
    conn.commit()
    conn.close()

    resp = client.post("/canonical/backfill-prices")
    assert resp.status_code == 302
    row = _canonical_row(master_path, c1)
    assert float(row["selling_price"]) == 42.0
    assert float(row["unit_cost"]) == 21.0


def test_canonical_list_and_detail_render_prices(logged_client):
    """P0-9 模板渲染：列表出价格列 + 回填按钮，详情显示价格。"""
    client, wh_path = logged_client
    master_path = wh_path.parent.parent / "master.db"
    c1 = _seed_canonical(master_path, "渲染品", unit="件")
    conn = sqlite3.connect(master_path)
    conn.execute(
        "UPDATE canonical_items SET selling_price=9.5, unit_cost=4.25 WHERE id=?", (c1,)
    )
    conn.commit()
    conn.close()

    body = client.get("/canonical/list").data.decode()
    assert "售价" in body and "价格回填" in body

    detail = client.get(f"/canonical/detail/{c1}").data.decode()
    assert "9.500" in detail and "4.250" in detail


def test_backfill_prices_requires_platform_admin(manager_client):
    client, _ = manager_client
    assert client.post("/canonical/backfill-prices").status_code == 403


def test_batch_edit_aux_unit_clears_gram(logged_client):
    """派生关系复用单条编辑路径：辅单位非「克」时克重归零。"""
    client, wh_path = logged_client
    master_path = wh_path.parent.parent / "master.db"
    c1 = _seed_canonical(master_path, "派生关系", unit="件")
    conn = sqlite3.connect(master_path)
    conn.execute(
        "UPDATE canonical_items SET aux_unit='克', aux_rate=500, gram_per_unit=500 WHERE id=?",
        (c1,))
    conn.commit()
    conn.close()

    resp = client.post("/canonical/batch-edit", data={
        "canonical_ids": [str(c1)], "field": "aux_unit", "value": "袋",
    })
    assert resp.status_code == 302
    row = _canonical_row(master_path, c1)
    assert row["aux_unit"] == "袋"
    assert float(row["gram_per_unit"]) == 0.0
