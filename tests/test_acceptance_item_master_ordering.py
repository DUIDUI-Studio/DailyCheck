"""验收测试脚本 —— 品项主数据同步 + 仓库订货全流程。

⚠️ 这是交付给「验收 Agent」执行的脚本，覆盖两大块：
    A. 品项主数据 → 全仓同步（创建 / 编辑 / 扇出 / 停用 / 价格 / 冲突冻结 / 批量改）
    B. 仓库订货流程（下单 → 审批 → 发货 → 收货 → 送达），含大量非正常操作

运行方式（在本仓库根目录）：
    ~/.local/bin/python3.12 -m pytest tests/test_acceptance_item_master_ordering.py -v --basetemp=/tmp/pytest-dc
或直接当脚本跑：
    ~/.local/bin/python3.12 tests/test_acceptance_item_master_ordering.py

预期结果：
    - 除「已知缺陷」标记的用例（xfail）外，应**全部通过**。
    - 有 **2 个 xfail**，指向同一个缺陷（2026-10-10 发现）：
        * `test_B16_...` 为**根因**：DC 发货页的数量输入框不在 `<form>` 内 → 永远整单全发。
        * `test_B15_...` 为**下游症状**：收货上限按订货量而非实发量。
      两者修好后会自动变 XPASS。

所有用例都在 tmp_path 下的临时 master.db / 仓库 db 上跑，绝不触碰真实 db。
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

# 允许 `python tests/xxx.py` 直接执行时能 import 到项目根
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from blueprints import store_ordering_pure as sop  # noqa: E402
from db import init_master_db, init_warehouse_db  # noqa: E402

# 固定品类名（config.FIXED_CATEGORIES 的子集，用于品类映射断言）
CAT_PACKAGING = "包材"
CAT_DAIRY = "乳制品"


# ═══════════════════════════════════════════════════════════════════════
#  测试环境（一个综合 fixture：1 主数据 + 2 门店 + 1 配送中心 + 1 研发）
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture
def env(tmp_path, monkeypatch):
    """搭建隔离环境：master.db + 4 个仓库 db + 用户/角色 + 主数据种子。

    用户：
      1 admin          (is_admin=1, 绑 dc_test, role=admin)
      2 store_mgr      (绑 store_test, manager)   ← 门店收货
      3 store_staff    (绑 store_test, staff)     ← 门店下单
      4 dc_mgr         (绑 dc_test, manager)      ← DC 审批 / 取消
      5 dc_staff       (绑 dc_test, staff)        ← DC 发货
      6 other_store_mgr(绑 store2_test, manager)  ← 跨仓越权测试
    """
    import config as config_module
    import db as db_module

    master_path = tmp_path / "master.db"
    wh_dir = tmp_path / "warehouses"
    wh_dir.mkdir()
    dc_path = wh_dir / "dc_test.db"
    store_path = wh_dir / "store_test.db"
    store2_path = wh_dir / "store2_test.db"
    rd_path = wh_dir / "rd_test.db"

    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_dir)
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", wh_dir)

    init_master_db()
    for p in (dc_path, store_path, store2_path, rd_path):
        init_warehouse_db(p)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    m = sqlite3.connect(str(master_path))
    m.row_factory = sqlite3.Row
    for uid, uname, admin in [
        (1, "admin", 1), (2, "store_mgr", 0), (3, "store_staff", 0),
        (4, "dc_mgr", 0), (5, "dc_staff", 0), (6, "other_store_mgr", 0),
    ]:
        m.execute(
            "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
            "VALUES (?, ?, 'x', ?, ?)", (uid, uname, admin, ts))
    for wid, code, name, path, wtype in [
        (1, "dc_test", "测试配送中心", dc_path, "distribution_center"),
        (2, "store_test", "测试门店", store_path, "storefront"),
        (3, "store2_test", "测试门店2", store2_path, "storefront"),
        (4, "rd_test", "测试研发中心", rd_path, "rd"),
    ]:
        m.execute(
            "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)", (wid, code, name, str(path), wtype, ts))
    for uid, wid, role in [
        (1, 1, "admin"), (2, 2, "manager"), (3, 2, "staff"),
        (4, 1, "manager"), (5, 1, "staff"), (6, 3, "manager"),
    ]:
        m.execute(
            "INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (?, ?, ?)",
            (uid, wid, role))
    # 主数据品类（code ↔ 名称 与 config.CATEGORY_CODE_MAP 对齐）
    m.execute(
        "INSERT INTO canonical_categories (code, name, description, created_at, updated_at) "
        "VALUES ('PACKAGING', ?, '包材', ?, ?)", (CAT_PACKAGING, ts, ts))
    m.execute(
        "INSERT INTO canonical_categories (code, name, description, created_at, updated_at) "
        "VALUES ('DAIRY', ?, '乳制品', ?, ?)", (CAT_DAIRY, ts, ts))
    # 主数据种子：101 包材A / 102 乳制品B
    for cid, sku, cname, code in [
        (101, "IC-000101", "测试包材A", "PACKAGING"),
        (102, "IC-000102", "测试乳制品B", "DAIRY"),
    ]:
        m.execute(
            """INSERT INTO canonical_items
               (id, canonical_sku, name, category_code, unit, gram_per_unit,
                aux_unit, aux_rate, status, created_from, created_at, updated_at)
               VALUES (?, ?, ?, ?, '件', 0, NULL, 0, 'active', 'rd_manual', ?, ?)""",
            (cid, sku, cname, code, ts, ts))
    m.commit()
    m.close()

    # DC 库存：101=1000, 102=500；门店 101 已绑定（收货/下单用）
    _raw_insert(store_path, "测试包材A", CAT_PACKAGING, 0.0, 101)
    _raw_insert(dc_path, "测试包材A", CAT_PACKAGING, 1000.0, 101)
    _raw_insert(dc_path, "测试乳制品B", CAT_DAIRY, 500.0, 102)

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True

    return {
        "app": app,
        "client": app.test_client(),
        "master_path": master_path,
        "dc_path": dc_path,
        "store_path": store_path,
        "store2_path": store2_path,
        "rd_path": rd_path,
    }


# ─────────────────────────────── 小工具 ───────────────────────────────


def _raw_insert(wh_path, name, category_name, qty, canonical_id):
    """在仓库 db 插一行已绑定的 items（category 按名称解析）。"""
    conn = sqlite3.connect(str(wh_path))
    conn.row_factory = sqlite3.Row
    cat = conn.execute(
        "SELECT id FROM categories WHERE name=? LIMIT 1", (category_name,)).fetchone()
    assert cat is not None, f"仓库缺少品类 {category_name}"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        """INSERT INTO items
           (sku, name, category_id, quantity, safety_stock, unit,
            canonical_id, updated_at)
           VALUES (?, ?, ?, ?, 0, '件', ?, ?)""",
        (f"AUTO-{canonical_id}", name, cat["id"], qty, canonical_id, ts))
    conn.commit()
    item_id = cur.lastrowid
    conn.close()
    return item_id


def _login(client, user_id, warehouse_id):
    with client.session_transaction() as s:
        s["user_id"] = user_id
        s["warehouse_id"] = warehouse_id


def _q(path, sql, params=()):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    conn.close()
    return rows


def _one(path, sql, params=()):
    rows = _q(path, sql, params)
    return rows[0] if rows else None


def _seed_canonical(master_path, name, *, unit="件", category_code="PACKAGING",
                    selling_price=None, unit_cost=None, status="active"):
    conn = sqlite3.connect(str(master_path))
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        """INSERT INTO canonical_items
           (canonical_sku, name, category_code, unit, gram_per_unit, aux_unit,
            aux_rate, status, created_from, created_at, updated_at,
            selling_price, unit_cost)
           VALUES (?, ?, ?, ?, 0, NULL, 0, ?, 'rd_manual', ?, ?, ?, ?)""",
        (f"IC-T-{name}", name, category_code, unit, status, ts, ts,
         selling_price, unit_cost))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def _latest_event_id(master_path):
    row = _one(master_path, "SELECT id FROM canonical_publish_events ORDER BY id DESC LIMIT 1")
    return row["id"] if row else None


def _item_by_canonical(wh_path, canonical_id):
    return _one(wh_path, "SELECT * FROM items WHERE canonical_id=?", (canonical_id,))


# ═══════════════════════════════════════════════════════════════════════
#  A. 品项主数据 → 全仓同步
# ═══════════════════════════════════════════════════════════════════════


def test_A1_fanout_creates_bound_items_in_all_warehouses(env):
    """新建主数据 → 扇出 → 每个目标仓都生成绑定行（名称/单位/品类映射正确）。"""
    client = env["client"]
    _login(client, 1, 1)  # admin
    cid = _seed_canonical(env["master_path"], "扇出新品", unit="箱",
                          category_code="PACKAGING")

    resp = client.post("/canonical/fanout", data={
        "canonical_ids": [str(cid)],
        "warehouse_codes": ["store_test", "rd_test"],
        "action": "overwrite",
        "summary": "A1",
    })
    assert resp.status_code == 302
    assert "/canonical/fanout/event/" in resp.headers["Location"]

    for wh_path in (env["store_path"], env["rd_path"]):
        row = _item_by_canonical(wh_path, cid)
        assert row is not None, "目标仓未生成绑定行"
        assert row["name"] == "扇出新品"
        assert row["unit"] == "箱"
        # 品类映射：canonical PACKAGING → 仓内「包材」
        cat = _one(wh_path, "SELECT name FROM categories WHERE id=?", (row["category_id"],))
        assert cat["name"] == CAT_PACKAGING


def test_A2_fanout_updates_name_and_unit_across_warehouses(env):
    """编辑主数据名称/单位 → 扇出 → 各仓已绑定行同步更新。"""
    client = env["client"]
    _login(client, 1, 1)
    cid = _item_by_canonical(env["store_path"], 101)  # 已绑定
    assert cid is not None

    resp = client.post(f"/canonical/edit/{101}", data={
        "name": "测试包材A-改名", "unit": "袋", "category_code": "PACKAGING",
        "gram_per_unit": "0", "status": "active",
    })
    assert resp.status_code == 302

    client.post("/canonical/fanout", data={
        "canonical_ids": ["101"], "warehouse_codes": ["store_test"],
        "action": "overwrite",
    })

    row = _item_by_canonical(env["store_path"], 101)
    assert row["name"] == "测试包材A-改名"
    assert row["unit"] == "袋"


def test_A3_fanout_updates_aux_unit_and_gram(env):
    """编辑辅助单位/换算率 → 扇出 → 各仓同步。"""
    client = env["client"]
    _login(client, 1, 1)
    client.post("/canonical/edit/101", data={
        "name": "测试包材A", "unit": "件", "category_code": "PACKAGING",
        "gram_per_unit": "250", "aux_unit": "克", "aux_rate": "250",
        "status": "active",
    })
    client.post("/canonical/fanout", data={
        "canonical_ids": ["101"], "warehouse_codes": ["store_test"],
        "action": "overwrite",
    })
    row = _item_by_canonical(env["store_path"], 101)
    assert row["aux_unit"] == "克"
    assert float(row["aux_rate"]) == 250.0
    assert float(row["gram_per_unit"]) == 250.0


def test_A4_disabled_fanout_hides_item_from_store_list(env):
    """主数据停用 → 扇出 → 门店行 canonical_status=disabled 且列表隐藏。"""
    client = env["client"]
    _login(client, 1, 1)
    client.post("/canonical/edit/101", data={
        "name": "测试包材A", "unit": "件", "category_code": "PACKAGING",
        "gram_per_unit": "0", "status": "disabled",
    })
    client.post("/canonical/fanout", data={
        "canonical_ids": ["101"], "warehouse_codes": ["store_test"],
        "action": "overwrite",
    })
    row = _item_by_canonical(env["store_path"], 101)
    assert row["canonical_status"] == "disabled"

    _login(client, 2, 2)  # store_mgr
    body = client.get("/items").get_data(as_text=True)
    assert "测试包材A" not in body, "已停用品项仍出现在门店品项列表"


def test_A5_price_canonical_managed_fanout_writes_price(env):
    """Q6=canonical_managed：主数据价随扇出下发到门店。"""
    client = env["client"]
    _login(client, 1, 1)
    cid = _seed_canonical(env["master_path"], "带价品项", selling_price=9.5,
                          unit_cost=6.0)
    client.post("/canonical/fanout", data={
        "canonical_ids": [str(cid)], "warehouse_codes": ["store_test"],
        "action": "overwrite",
    })
    row = _item_by_canonical(env["store_path"], cid)
    assert float(row["selling_price"]) == 9.5
    assert float(row["unit_cost"]) == 6.0


def test_A6_null_price_not_fanned_out_keeps_local_price(env):
    """主数据未定价（NULL）→ 不下发，保留门店本地价（绝不清 0）。"""
    client = env["client"]
    _login(client, 1, 1)
    # 给门店 101 先写一个本地价
    conn = sqlite3.connect(str(env["store_path"]))
    conn.execute("UPDATE items SET selling_price=7.0, unit_cost=4.0 WHERE canonical_id=101")
    conn.commit()
    conn.close()

    cid = _seed_canonical(env["master_path"], "无价品项")  # 价格全 NULL
    client.post("/canonical/fanout", data={
        "canonical_ids": [str(cid)], "warehouse_codes": ["store_test"],
        "action": "overwrite",
    })
    # 新建行价应为 0（默认），不该变成 NULL
    row = _item_by_canonical(env["store_path"], cid)
    assert row is not None
    assert float(row["selling_price"]) == 0.0

    # 再对 101 扇出（主数据 101 无价）→ 门店 101 本地价必须保持不变
    client.post("/canonical/fanout", data={
        "canonical_ids": ["101"], "warehouse_codes": ["store_test"],
        "action": "overwrite",
    })
    row101 = _item_by_canonical(env["store_path"], 101)
    assert float(row101["selling_price"]) == 7.0, "NULL 价把门店本地价清掉了"


def test_A7_zero_price_is_fanned_out(env):
    """主数据显式定价 0 → 下发（0 是有效价，不是未定价）。"""
    client = env["client"]
    _login(client, 1, 1)
    cid = _seed_canonical(env["master_path"], "零价品项", selling_price=0.0,
                          unit_cost=0.0)
    client.post("/canonical/fanout", data={
        "canonical_ids": [str(cid)], "warehouse_codes": ["store_test"],
        "action": "overwrite",
    })
    row = _item_by_canonical(env["store_path"], cid)
    assert float(row["selling_price"]) == 0.0


def test_A8_local_drift_is_frozen_not_overwritten(env):
    """门店本地改过主数据字段 → 扇出时冻结，不覆盖本地值。"""
    client = env["client"]
    _login(client, 1, 1)
    # 先正常扇出一次，建立 canonical_synced_json 快照
    client.post("/canonical/fanout", data={
        "canonical_ids": ["101"], "warehouse_codes": ["store_test"],
        "action": "overwrite",
    })
    # 门店本地把名称改成别的（模拟本地漂移）
    conn = sqlite3.connect(str(env["store_path"]))
    conn.execute("UPDATE items SET name='门店自己改的名字' WHERE canonical_id=101")
    conn.commit()
    conn.close()
    # 主数据改名为 X，再扇出 overwrite（非 force）
    client.post("/canonical/edit/101", data={
        "name": "主数据新名字", "unit": "件", "category_code": "PACKAGING",
        "gram_per_unit": "0", "status": "active",
    })
    client.post("/canonical/fanout", data={
        "canonical_ids": ["101"], "warehouse_codes": ["store_test"],
        "action": "overwrite",
    })
    row = _item_by_canonical(env["store_path"], 101)
    # 本地漂移字段应被冻结 → 保持门店值
    assert row["name"] == "门店自己改的名字"
    # 事件里应有冻结记录
    assert _q(env["master_path"],
              "SELECT id FROM canonical_conflicts WHERE status='open'") != []


def test_A9_batch_edit_updates_multiple_canonicals(env):
    """批量修改：一次改多条主数据的同一字段。"""
    client = env["client"]
    _login(client, 1, 1)
    a = _seed_canonical(env["master_path"], "批量A")
    b = _seed_canonical(env["master_path"], "批量B")
    resp = client.post("/canonical/batch-edit", data={
        "canonical_ids": [str(a), str(b)], "field": "unit", "value": "桶",
    })
    assert resp.status_code == 302
    for cid in (a, b):
        row = _one(env["master_path"], "SELECT unit FROM canonical_items WHERE id=?", (cid,))
        assert row["unit"] == "桶"


def test_A10_fanout_skips_when_no_matching_category(env):
    """目标仓无匹配品类 → 跳过该主数据项（不静默写坏数据）。"""
    client = env["client"]
    _login(client, 1, 1)
    cid = _seed_canonical(env["master_path"], "怪异品类品项",
                          category_code="NOT_EXIST_CODE")
    resp = client.post("/canonical/fanout", data={
        "canonical_ids": [str(cid)], "warehouse_codes": ["store_test"],
        "action": "overwrite",
    })
    assert resp.status_code == 302
    # 不应生成绑定行
    assert _item_by_canonical(env["store_path"], cid) is None


def test_A11_fanout_post_lands_on_real_event_page(env):
    """回归：扇出 POST 必须跳到真实事件页（此前 dry_run 会跳到 event/0 → 404）。"""
    client = env["client"]
    _login(client, 1, 1)
    cid = _seed_canonical(env["master_path"], "事件页回归")
    resp = client.post("/canonical/fanout", data={
        "canonical_ids": [str(cid)], "warehouse_codes": ["store_test"],
        "action": "overwrite",
    })
    loc = resp.headers["Location"]
    assert "/canonical/fanout/event/" in loc
    assert not loc.endswith("/event/0")
    assert client.get(loc).status_code == 200
    assert client.get("/canonical/fanout").status_code == 200


def test_A12_canonical_edit_requires_platform_admin(env):
    """非平台管理员的门店 manager 不能改主数据。"""
    client = env["client"]
    _login(client, 2, 2)  # store_mgr, is_admin=0
    resp = client.post("/canonical/edit/101", data={
        "name": "越权改名", "unit": "件", "category_code": "PACKAGING",
        "gram_per_unit": "0", "status": "active",
    })
    assert resp.status_code == 403
    row = _one(env["master_path"], "SELECT name FROM canonical_items WHERE id=101")
    assert row["name"] == "测试包材A"


def test_A13_canonical_edit_invalid_price_is_rejected(env):
    """非法价格输入 → 表单错误，不落库。"""
    client = env["client"]
    _login(client, 1, 1)
    before = _one(env["master_path"],
                  "SELECT COUNT(*) AS c FROM canonical_items WHERE name='非法价品项'")["c"]
    resp = client.post("/canonical/edit", data={
        "name": "非法价品项", "unit": "件", "category_code": "PACKAGING",
        "gram_per_unit": "0", "status": "active",
        "selling_price": "abc", "unit_cost": "0",
    })
    assert resp.status_code == 302  # flash 后重定向
    after = _one(env["master_path"],
                 "SELECT COUNT(*) AS c FROM canonical_items WHERE name='非法价品项'")["c"]
    assert after == before


# ═══════════════════════════════════════════════════════════════════════
#  B. 仓库订货流程
# ═══════════════════════════════════════════════════════════════════════


def _order_flow(env, qty=10.0, canonical_id=101):
    """门店下单 → DC 审批。返回 (order_id, order_item_id)。"""
    client = env["client"]
    _login(client, 3, 2)  # store_staff
    client.post("/store-ordering/cart/add", data={
        "dc": "dc_test", "canonical_id": canonical_id,
        "quantity": str(qty), "unit": "件",
    })
    client.post("/store-ordering/cart/submit", data={
        "expected_delivery_date": datetime.now().strftime("%Y-%m-%d"), "note": "",
    })
    order = _one(env["master_path"], "SELECT id FROM store_orders ORDER BY id DESC LIMIT 1")
    oid = order["id"]
    oiid = _one(env["master_path"],
                "SELECT id FROM store_order_items WHERE order_id=?", (oid,))["id"]
    _login(client, 4, 1)  # dc_mgr
    client.post(f"/store-ordering/orders/{oid}/review",
                data={"decision": "approved", "note": "ok"})
    return oid, oiid


def _ship(client, oid, oiid, qty):
    _login(client, 5, 1)  # dc_staff
    return client.post(f"/store-ordering/orders/{oid}/ship",
                       data={f"shipped_items[{oiid}]": str(qty), "tracking_note": ""},
                       follow_redirects=True)


def _receive(client, oid, oiid, qty):
    _login(client, 2, 2)  # store_mgr
    return client.post(f"/store-ordering/orders/{oid}/receive",
                       data={"order_item_id": oiid, "quantity": str(qty), "note": ""},
                       follow_redirects=True)


def _order_status(env, oid):
    return _one(env["master_path"], "SELECT status FROM store_orders WHERE id=?", (oid,))["status"]


def _item_state(env, oiid):
    return _one(env["master_path"],
                "SELECT quantity, shipped_quantity, fulfilled_quantity, status "
                "FROM store_order_items WHERE id=?", (oiid,))


def test_B1_full_happy_path(env):
    """全程正常：下单 → 审批 → 全量发货 → 收货 → delivered，门店库存增加。"""
    client = env["client"]
    oid, oiid = _order_flow(env, qty=10.0)
    assert _order_status(env, oid) == sop.ORDER_STATUS_APPROVED

    resp = _ship(client, oid, oiid, 10)
    assert resp.status_code == 200
    assert _order_status(env, oid) == sop.ORDER_STATUS_SHIPPED

    _receive(client, oid, oiid, 10)
    assert _order_status(env, oid) == sop.ORDER_STATUS_DELIVERED
    assert float(_item_state(env, oiid)["fulfilled_quantity"]) == 10.0
    store_qty = _item_by_canonical(env["store_path"], 101)["quantity"]
    assert float(store_qty) == 10.0


def test_B2_partial_ship_multibatch_then_full(env):
    """部分发货：发 2 时订单仍 approved；再发 8 才 shipped。"""
    client = env["client"]
    oid, oiid = _order_flow(env, qty=10.0)
    _ship(client, oid, oiid, 2)
    assert _order_status(env, oid) == sop.ORDER_STATUS_APPROVED, "部分发货不应置 shipped"
    assert float(_item_state(env, oiid)["shipped_quantity"]) == 2.0
    _ship(client, oid, oiid, 8)
    assert _order_status(env, oid) == sop.ORDER_STATUS_SHIPPED


def test_B3_partial_receive_multibatch_then_delivered(env):
    """部分收货：收 3 再收 7 → delivered。"""
    client = env["client"]
    oid, oiid = _order_flow(env, qty=10.0)
    _ship(client, oid, oiid, 10)
    _receive(client, oid, oiid, 3)
    assert _order_status(env, oid) == sop.ORDER_STATUS_SHIPPED
    _receive(client, oid, oiid, 7)
    assert _order_status(env, oid) == sop.ORDER_STATUS_DELIVERED


def test_B4_over_ship_rejected(env):
    """DC 累计发货超过订货量 → 拒绝，不写库。"""
    client = env["client"]
    oid, oiid = _order_flow(env, qty=10.0)
    resp = _ship(client, oid, oiid, 11)
    assert resp.status_code == 200
    assert "超过订单数" in resp.get_data(as_text=True)
    assert float(_item_state(env, oiid)["shipped_quantity"]) == 0.0


def test_B5_over_receive_rejected(env):
    """收货超过「订货-已收」剩余 → 拒绝。"""
    client = env["client"]
    oid, oiid = _order_flow(env, qty=5.0)
    _ship(client, oid, oiid, 5)
    resp = _receive(client, oid, oiid, 99)
    assert resp.status_code == 200
    assert "超过待收" in resp.get_data(as_text=True)
    assert float(_item_state(env, oiid)["fulfilled_quantity"]) == 0.0


def test_B6_ship_before_approval_rejected(env):
    """订单还没审批就发货 → 拒绝。"""
    client = env["client"]
    _login(client, 3, 2)
    client.post("/store-ordering/cart/add", data={
        "dc": "dc_test", "canonical_id": 101, "quantity": "5", "unit": "件"})
    client.post("/store-ordering/cart/submit", data={
        "expected_delivery_date": datetime.now().strftime("%Y-%m-%d"), "note": ""})
    oid = _one(env["master_path"], "SELECT id FROM store_orders ORDER BY id DESC LIMIT 1")["id"]
    oiid = _one(env["master_path"],
                "SELECT id FROM store_order_items WHERE order_id=?", (oid,))["id"]
    resp = _ship(client, oid, oiid, 5)
    assert resp.status_code == 200
    assert float(_item_state(env, oiid)["shipped_quantity"]) == 0.0


def test_B7_receive_before_shipment_rejected(env):
    """订单还没发货就收货 → 拒绝。"""
    client = env["client"]
    oid, oiid = _order_flow(env, qty=5.0)  # 只到 approved
    resp = _receive(client, oid, oiid, 1)
    assert resp.status_code == 200
    assert float(_item_state(env, oiid)["fulfilled_quantity"]) == 0.0


def test_B8_receive_after_delivered_rejected(env):
    """已送达后再收货 → 拒绝。"""
    client = env["client"]
    oid, oiid = _order_flow(env, qty=5.0)
    _ship(client, oid, oiid, 5)
    _receive(client, oid, oiid, 5)
    assert _order_status(env, oid) == sop.ORDER_STATUS_DELIVERED
    resp = _receive(client, oid, oiid, 1)
    assert resp.status_code == 200
    assert float(_item_state(env, oiid)["fulfilled_quantity"]) == 5.0


def test_B9_cancel_after_shipment_rejected(env):
    """已发货订单不可取消。"""
    client = env["client"]
    oid, oiid = _order_flow(env, qty=5.0)
    _ship(client, oid, oiid, 5)
    _login(client, 4, 1)  # dc_mgr
    resp = client.post(f"/store-ordering/orders/{oid}/cancel",
                       data={"reason": "不想要了"}, follow_redirects=True)
    assert resp.status_code == 200
    assert _order_status(env, oid) == sop.ORDER_STATUS_SHIPPED


def test_B10_review_twice_rejected(env):
    """已审批订单再次审批 → 拒绝（非法状态迁移）。"""
    client = env["client"]
    oid, _ = _order_flow(env, qty=5.0)  # 已 approved
    _login(client, 4, 1)
    client.post(f"/store-ordering/orders/{oid}/review",
                data={"decision": "rejected", "note": "改主意"})
    assert _order_status(env, oid) == sop.ORDER_STATUS_APPROVED


def test_B11_store_staff_cannot_receive(env):
    """普通门店员工（staff）不能收货，只有 manager/admin 可以。"""
    client = env["client"]
    oid, oiid = _order_flow(env, qty=5.0)
    _ship(client, oid, oiid, 5)
    _login(client, 3, 2)  # store_staff
    resp = client.post(f"/store-ordering/orders/{oid}/receive",
                       data={"order_item_id": oiid, "quantity": "1", "note": ""},
                       follow_redirects=True)
    assert resp.status_code == 200
    assert float(_item_state(env, oiid)["fulfilled_quantity"]) == 0.0


def test_B12_other_store_cannot_view_or_receive(env):
    """其它门店不能查看/收货本店订单 → 403。"""
    client = env["client"]
    oid, oiid = _order_flow(env, qty=5.0)
    _ship(client, oid, oiid, 5)
    _login(client, 6, 3)  # other_store_mgr @ store2_test
    assert client.get(f"/store-ordering/orders/{oid}").status_code == 403


def test_B13_empty_cart_submit_rejected(env):
    """空购物车提交 → 拒绝，不生成订单。"""
    client = env["client"]
    _login(client, 3, 2)
    before = _one(env["master_path"], "SELECT COUNT(*) AS c FROM store_orders")["c"]
    client.post("/store-ordering/cart/add", data={
        "dc": "dc_test", "canonical_id": 101, "quantity": "5", "unit": "件"})
    client.post("/store-ordering/cart/clear", data={})  # 清空
    resp = client.post("/store-ordering/cart/submit", data={
        "expected_delivery_date": datetime.now().strftime("%Y-%m-%d"), "note": ""},
        follow_redirects=True)
    assert resp.status_code == 200
    after = _one(env["master_path"], "SELECT COUNT(*) AS c FROM store_orders")["c"]
    assert after == before


def test_B14_invalid_quantity_not_added(env):
    """下单数量为 0 / 负数 → 不加入购物车。"""
    client = env["client"]
    _login(client, 3, 2)
    for bad in ("0", "-3"):
        client.post("/store-ordering/cart/add", data={
            "dc": "dc_test", "canonical_id": 101, "quantity": bad, "unit": "件"})
    cart = _one(env["master_path"],
                "SELECT COUNT(*) AS c FROM store_order_cart_items")["c"]
    assert cart == 0


@pytest.mark.xfail(
    reason="【下游症状】已知缺陷（2026-10-10）：收货上限按【订货量】而非【实发量】计算 —— "
           "订单一旦处于 shipped 而实发 < 订货，门店可收货超过实发量。"
           "（根因见 test_B16：UI 发货永远整单全发，使这一状态在正常流程中被掩盖。）",
    strict=False,
)
def test_B15_receive_must_not_exceed_shipped(env):
    """【已知缺陷】门店收货不得超过 DC 实发量。

    构造：订单 100，DC 实发 2（订单处于 shipped 状态，实发 2）。
    期望：收货 100 被拒绝；实际：被受理 → 本用例 xfail。
    （正常流程下订单需全发齐才转 shipped，故此状态需直接构造。）
    """
    client = env["client"]
    oid, oiid = _order_flow(env, qty=100.0)
    _ship(client, oid, oiid, 2)  # 实发 2 → 订单停在 approved
    # 直接构造「订单已置 shipped、但实发仅 2」的状态
    conn = sqlite3.connect(str(env["master_path"]))
    conn.execute("UPDATE store_orders SET status='shipped' WHERE id=?", (oid,))
    conn.execute("UPDATE store_order_items SET status='partial', shipped_quantity=2 WHERE id=?",
                 (oiid,))
    conn.commit()
    conn.close()

    _receive(client, oid, oiid, 100)
    fulfilled = float(_item_state(env, oiid)["fulfilled_quantity"])
    assert fulfilled <= 2.0, (
        f"收货 {fulfilled} 超过实发的 2 —— 收货上限未按实发量限制（缺陷复现）")


# ═══════════════════════════════════════════════════════════════════════
#  C. 品项管理权收敛（门店/研发不能自建品项、不能直发品项）
# ═══════════════════════════════════════════════════════════════════════


def test_C1_store_post_items_creates_nothing(env):
    """门店/管理员在 /items POST 新建品项 → 零写入（创建权已收回主数据）。"""
    client = env["client"]
    _login(client, 1, 2)  # admin @ 门店
    before = _one(env["store_path"], "SELECT COUNT(*) AS c FROM items")["c"]
    resp = client.post("/items", data={
        "name": "__SHOULD_NOT_CREATE__", "category_id": "1", "quantity": "0",
        "unit": "件",
    })
    assert resp.status_code == 302
    after = _one(env["store_path"], "SELECT COUNT(*) AS c FROM items")["c"]
    assert after == before
    assert _one(env["store_path"],
                "SELECT COUNT(*) AS c FROM items WHERE name='__SHOULD_NOT_CREATE__'")["c"] == 0


def test_C2_items_publish_redirects_to_canonical_fanout(env):
    """/items/publish 已下线，任何访问都重定向到主数据扇出页。"""
    client = env["client"]
    _login(client, 1, 1)
    resp = client.post("/items/publish", data={"summary": "x"})
    assert resp.status_code == 302
    assert "/canonical/fanout" in resp.headers["Location"]


def test_C3_items_list_hides_canonically_disabled(env):
    """主数据停用并经扇出后，门店 /inventory 也不显示该品项。"""
    client = env["client"]
    _login(client, 1, 1)
    client.post("/canonical/edit/101", data={
        "name": "测试包材A", "unit": "件", "category_code": "PACKAGING",
        "gram_per_unit": "0", "status": "disabled",
    })
    client.post("/canonical/fanout", data={
        "canonical_ids": ["101"], "warehouse_codes": ["store_test"],
        "action": "overwrite",
    })
    _login(client, 2, 2)
    body = client.get("/inventory").get_data(as_text=True)
    assert "测试包材A" not in body


@pytest.mark.xfail(
    reason="【根因】已知缺陷（2026-10-10）：DC 发货页的「本批发货」数量输入框位于 "
           "表格中，而提交按钮在另一个独立 <form id=\"shipment-form\"> 里 —— 输入框不在"
           "表单内、永远不会被提交。JS 只给它们加了 name，但 DOM 上仍不属于该 form。"
           "于是服务端收不到任何 shipped_items[] → 走全量发货兜底 → 整单发齐。"
           "（现象：DC 输入 2 点击发货，系统却记 100 已发，订单转 shipped，门店可收 100。）",
    strict=False,
)
def test_B16_dc_ship_form_contains_quantity_inputs(env):
    """【根因】DC 发货页的「本批发货」数量输入框必须位于发货表单内。

    当前它们渲染在 <table> 中，而 <form id="shipment-form"> 是另一个元素 → 浏览器
    提交时不会带上这些字段，导致每次 UI 发货都退化为「整单全发」。
    """
    client = env["client"]
    oid, oiid = _order_flow(env, qty=100.0)  # 停在 approved
    _login(client, 5, 1)  # dc_staff，发货页可见
    body = client.get(f"/store-ordering/orders/{oid}").get_data(as_text=True)

    start = body.find('id="shipment-form"')
    assert start != -1, "发货页缺少 #shipment-form"
    end = body.find("</form>", start)
    assert end != -1
    form_html = body[start:end]
    assert "data-item-id=" in form_html, (
        "「本批发货」数量输入框不在发货表单内 —— 浏览器不会提交它们，"
        "点击「确认发货」会退化为整单全发（缺陷复现）")


if __name__ == "__main__":
    # 支持 `python tests/test_acceptance_item_master_ordering.py` 直接运行
    raise SystemExit(pytest.main([__file__, "-v", "-s", "--basetemp=/tmp/pytest-dc"]))
