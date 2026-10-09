"""品项主数据统一改造的策略测试（方案 §7.2）。

覆盖：
  #1 已绑定行：编辑页主数据字段/价格/安全库存只读（服务端强制）
  #2 已绑定行：禁止物理删除；未绑定行仍可删
  #6 主数据维护权限收紧：/canonical/edit 需 platform admin
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

def test_edit_bound_item_ignores_form_and_keeps_values(logged_client):
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
    assert float(row["unit_cost"]) == 5
    assert float(row["safety_stock"]) == 0
    assert row["unit"] == "件"


def test_edit_bound_item_page_is_readonly(logged_client):
    client, wh_path = logged_client
    item_id, _ = _seed_item(wh_path, "只读品项", qty=0, unit_cost=5)
    _bind_canonical(wh_path, item_id)

    resp = client.get(f"/items/{item_id}/edit")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "仅供查看" in body
    assert "去主数据查看" in body
    assert 'name="name"' not in body          # 没有可提交的名称输入框


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
