"""v3.1 模拟用户测试 — 三功能端到端走查。

不污染 dev 数据：用 tmp_path 临时 db，跑全流程并逐步打印结果。
覆盖路径：
  A. admin 配置运费规则 → DB 验证
  B. DC manager 切换品项可订 → 门店 catalog 过滤 → submit 拦截
  C. storefront 下单 → submit 算运费 → detail 显示运费
  D. admin 报表 KPI 5 张卡 + 维度含运费
"""
from __future__ import annotations

import sys
import sqlite3
from pathlib import Path
from datetime import datetime

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def section(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def step(label: str) -> None:
    print(f"\n▶ {label}")


def assert_eq(label: str, got, want) -> bool:
    ok = got == want
    mark = "✓" if ok else "✗"
    print(f"  {mark} {label}: got={got!r}, want={want!r}")
    return ok


def main() -> int:
    import config as config_module
    import db as db_module
    from db import init_master_db, init_warehouse_db
    import blueprints.store_ordering_pure as sop

    # -------- 1. 临时 db + 基础数据 --------
    section("Step 0: 准备临时环境")
    import uuid
    tmp = Path(f"/tmp/dc_sim_v3_1_{uuid.uuid4().hex[:8]}")
    tmp.mkdir(parents=True, exist_ok=True)
    wh_dir = tmp / "warehouses"
    try:
        wh_dir.mkdir(exist_ok=True)
    except (FileExistsError, PermissionError):
        # WorkBuddy sandbox 偶发误报 EEXIST，目录实际已建好
        if not wh_dir.exists():
            raise
    master_path = tmp / "master.db"
    # 文件名必须与 warehouse_code 对应（open_warehouse_db 按 code 拼路径）
    dc_path = wh_dir / "sim_dc.db"
    store_path = wh_dir / "sim_store.db"

    db_module.MASTER_DB = master_path
    db_module.WAREHOUSE_DB_DIR = wh_dir
    config_module.MASTER_DB = master_path
    config_module.WAREHOUSE_DB_DIR = wh_dir
    init_master_db()
    init_warehouse_db(dc_path)
    init_warehouse_db(store_path)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    m = sqlite3.connect(str(master_path))
    m.row_factory = sqlite3.Row

    # Users
    m.execute("INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (1, 'admin', 'x', 1, ?)", (ts,))
    m.execute("INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (2, 'store_mgr', 'x', 0, ?)", (ts,))
    m.execute("INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (3, 'dc_mgr', 'x', 0, ?)", (ts,))

    # Warehouses: DC + store
    m.execute("INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) VALUES (1, 'sim_dc', '测试DC', ?, 'distribution_center', ?)", (str(dc_path), ts))
    m.execute("INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) VALUES (2, 'sim_store', '测试门店', ?, 'storefront', ?)", (str(store_path), ts))

    # Roles
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (1, 1, 'admin')")  # admin on DC
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (1, 2, 'admin')")  # admin on store
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (3, 1, 'manager')")  # dc_mgr
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (2, 2, 'manager')")  # store_mgr

    # Canonical category + items
    m.execute("INSERT INTO canonical_categories (code, name, description, created_at, updated_at) VALUES ('PACKAGING', '包材', '', ?, ?)", (ts, ts))
    m.execute("""INSERT INTO canonical_items
       (id, canonical_sku, name, category_code, unit, gram_per_unit, aux_unit, aux_rate,
        status, created_from, created_at, updated_at)
       VALUES (101, 'IC-000101', '测试包材A', 'PACKAGING', '件', 0, NULL, 0, 'active', 'rd_manual', ?, ?)""", (ts, ts))
    m.execute("""INSERT INTO canonical_items
       (id, canonical_sku, name, category_code, unit, gram_per_unit, aux_unit, aux_rate,
        status, created_from, created_at, updated_at)
       VALUES (102, 'IC-000102', '测试包材B', 'PACKAGING', '件', 0, NULL, 0, 'active', 'rd_manual', ?, ?)""", (ts, ts))
    m.commit()
    m.close()

    # DC items with selling_price
    dc = sqlite3.connect(str(dc_path))
    dc.row_factory = sqlite3.Row
    cat_id = dc.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()["id"]
    dc.execute("INSERT INTO items (sku, name, category_id, quantity, unit, canonical_id, selling_price, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
               ("DC-A", "测试包材A", cat_id, 100.0, "件", 101, 5.00, ts))
    dc.execute("INSERT INTO items (sku, name, category_id, quantity, unit, canonical_id, selling_price, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
               ("DC-B", "测试包材B", cat_id, 100.0, "件", 102, 3.00, ts))
    dc.commit()
    dc.close()

    # Store items (bound)
    store = sqlite3.connect(str(store_path))
    store.row_factory = sqlite3.Row
    cat_id = store.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()["id"]
    store.execute("INSERT INTO items (sku, name, category_id, quantity, unit, canonical_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  ("ST-A", "测试包材A", cat_id, 0.0, "件", 101, ts))
    store.execute("INSERT INTO items (sku, name, category_id, quantity, unit, canonical_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  ("ST-B", "测试包材B", cat_id, 0.0, "件", 102, ts))
    store.commit()
    store.close()

    print("  ✓ 临时环境就绪：1 DC + 1 门店 + 3 用户（admin/store_mgr/dc_mgr）")

    # -------- 2. 起 Flask app --------
    from app import create_app
    app = create_app()
    app.config["TESTING"] = True

    failures = 0

    def login(client, user_id: int, warehouse_id: int) -> None:
        with client.session_transaction() as s:
            s["user_id"] = user_id
            s["warehouse_id"] = warehouse_id

    # ============================================================
    # A. 运费规则：admin 配置 + DB 验证
    # ============================================================
    section("A. 运费规则配置（admin）")
    step("A1: admin GET /admin/shipping")
    client_admin = app.test_client()
    login(client_admin, 1, 1)  # admin on DC
    resp = client_admin.get("/store-ordering/admin/shipping")
    failures += not assert_eq("status_code", resp.status_code, 200)
    body = resp.data.decode()
    failures += not assert_eq("含「基础运费」", "基础运费" in body, True)
    failures += not assert_eq("含「服务费比例」", "服务费比例" in body, True)
    failures += not assert_eq("含实时示例", "实时示例" in body, True)

    step("A2: admin POST 配置 base=10, pct=2%")
    resp = client_admin.post(
        "/store-ordering/admin/shipping",
        data={"base_fee": "10", "pct_fee": "2", "active": "1"},
        follow_redirects=True,
    )
    failures += not assert_eq("status_code", resp.status_code, 200)
    rule = sop.get_active_shipping_rule(db_module.MASTER_DB if False else sqlite3.connect(str(master_path)))
    # ^ careful: use fresh conn
    m2 = sqlite3.connect(str(master_path))
    m2.row_factory = sqlite3.Row
    rule = sop.get_active_shipping_rule(m2)
    m2.close()
    failures += not assert_eq("rule.base_fee", float(rule["base_fee"]), 10.0)
    failures += not assert_eq("rule.pct_fee", float(rule["pct_fee"]), 0.02)
    failures += not assert_eq("rule.active", rule["active"], 1)
    print(f"  → DB 规则：base=¥{rule['base_fee']}, pct={float(rule['pct_fee'])*100}%")

    # ============================================================
    # B. 品项可订开关：DC manager + 门店 catalog 过滤 + submit 拦截
    # ============================================================
    section("B. 品项可订开关")
    step("B1: dc_mgr GET /dc/items 看品项列表")
    client_dc = app.test_client()
    login(client_dc, 3, 1)  # dc_mgr on DC
    resp = client_dc.get("/store-ordering/dc/items")
    failures += not assert_eq("status_code", resp.status_code, 200)
    body = resp.data.decode()
    failures += not assert_eq("含「DC 品项可订管理」", "DC 品项可订管理" in body, True)
    failures += not assert_eq("测试包材A 在列表", "测试包材A" in body, True)
    failures += not assert_eq("测试包材B 在列表", "测试包材B" in body, True)
    failures += not assert_eq("初始两个都可订", body.count(">可订<") >= 2, True)

    step("B2: dc_mgr POST 关闭 102")
    resp = client_dc.post(
        "/store-ordering/dc/items",
        data={"canonical_id": "102", "is_orderable": "0"},
        follow_redirects=True,
    )
    failures += not assert_eq("status_code", resp.status_code, 200)
    # DB 验证
    dc_chk = sqlite3.connect(str(dc_path))
    is_o = dc_chk.execute("SELECT is_orderable FROM items WHERE canonical_id=102").fetchone()[0]
    dc_chk.close()
    failures += not assert_eq("102 is_orderable=0", int(is_o), 0)

    step("B3: 门店 catalog 不再显示 102")
    client_store = app.test_client()
    login(client_store, 2, 2)  # store_mgr on store
    resp = client_store.get("/store-ordering/catalog?dc=sim_dc")
    failures += not assert_eq("status_code", resp.status_code, 200)
    body = resp.data.decode()
    failures += not assert_eq("测试包材A 仍在 catalog", "测试包材A" in body, True)
    failures += not assert_eq("测试包材B 已从 catalog 消失", "测试包材B" not in body, True)

    step("B4: 加购 102（先打开再加购，模拟「先订后关」场景需绕过）")
    # 这里为了测拦截，先加购 102，再关掉
    # 实际上：先打开 102，加购，再关闭 102
    sop.set_dc_item_orderable(sqlite3.connect(str(master_path)), "sim_dc", 102, True)
    client_store.post(
        "/store-ordering/cart/add-batch",
        data={"dc": "sim_dc", "selected[]": "102", "qty[]": "2", "unit[]": "件"},
    )
    # 现在关掉 102
    sop.set_dc_item_orderable(sqlite3.connect(str(master_path)), "sim_dc", 102, False)

    # 加购 101（可订）
    client_store.post(
        "/store-ordering/cart/add-batch",
        data={"dc": "sim_dc", "selected[]": "101", "qty[]": "3", "unit[]": "件"},
    )

    step("B5: 访问 submit 页 — 应提示 102 已下架")
    resp = client_store.get("/store-ordering/cart/submit")
    failures += not assert_eq("status_code", resp.status_code, 200)
    body = resp.data.decode()
    failures += not assert_eq("含「已被配送中心下架」提示", "已被配送中心下架" in body, True)
    failures += not assert_eq("含 102 名称", "测试包材B" in body, True)
    failures += not assert_eq("发送按钮 disabled", "disabled" in body, True)

    step("B6: 从购物车移除 102，submit 应通过")
    # 拿 cart_id
    m_chk = sqlite3.connect(str(master_path))
    m_chk.row_factory = sqlite3.Row
    cart = m_chk.execute("SELECT id FROM store_order_carts WHERE user_id=2 AND store_warehouse_code='sim_store'").fetchone()
    cart_id = int(cart["id"])
    # 拿 cart_item id for 102
    item_102 = m_chk.execute("SELECT id FROM store_order_cart_items WHERE cart_id=? AND canonical_id=102", (cart_id,)).fetchone()
    if item_102:
        cart_item_id = int(item_102["id"])
        client_store.post(f"/store-ordering/cart/remove/{cart_item_id}")
    m_chk.close()

    # ============================================================
    # C. 门店下单 → submit 算运费 → detail 显示运费
    # ============================================================
    section("C. 门店下单（运费计算 + 详情展示）")
    step("C1: submit 订单 — 应显示运费预估")
    resp = client_store.post(
        "/store-ordering/cart/submit",
        data={
            "expected_delivery_date": datetime.now().strftime("%Y-%m-%d"),
            "note": "模拟用户测试",
        },
        follow_redirects=True,
    )
    failures += not assert_eq("status_code", resp.status_code, 200)
    # 应跳到 order_detail
    order_detail_url = resp.request.path if hasattr(resp, "request") else None
    # follow_redirects=True 后 resp 是终页
    body = resp.data.decode()
    failures += not assert_eq("跳到订单详情", "/store-ordering/orders/" in body, True)

    # 提取 order_id from DB
    m_chk = sqlite3.connect(str(master_path))
    m_chk.row_factory = sqlite3.Row
    order = m_chk.execute("SELECT * FROM store_orders ORDER BY id DESC LIMIT 1").fetchone()
    order_id = int(order["id"])
    m_chk.close()

    # 预期：3 件 × ¥5 = ¥15 订单金额 + base=10 + pct=2%×15=0.3 → 运费 = 10.3
    expected_subtotal = 15.0
    expected_shipping = 10.0 + 0.02 * 15.0  # 10.3
    failures += not assert_eq("store_orders.shipping_fee", float(order["shipping_fee"]), expected_shipping)
    print(f"  → 订单金额 ¥{expected_subtotal:.2f} → 运费 ¥{expected_shipping:.2f}")

    step("C2: GET /orders/<id> — 详情页含「运费」「订单总计」")
    resp = client_store.get(f"/store-ordering/orders/{order_id}")
    failures += not assert_eq("status_code", resp.status_code, 200)
    body = resp.data.decode()
    failures += not assert_eq("含「运费」label", "运费" in body, True)
    failures += not assert_eq("含「订单总计」label", "订单总计" in body, True)
    # 含具体数值
    failures += not assert_eq(f"含运费 ¥{expected_shipping:.2f}", f"¥ {expected_shipping:.2f}" in body, True)

    # ============================================================
    # D. admin 报表：5 张 KPI + 维度含运费
    # ============================================================
    section("D. admin 报表（KPI + 维度含运费）")
    resp = client_admin.get("/store-ordering/admin/report")
    failures += not assert_eq("status_code", resp.status_code, 200)
    body = resp.data.decode()
    failures += not assert_eq("KPI 「总订货量」", "总订货量" in body, True)
    failures += not assert_eq("KPI 「总运费」", "总运费" in body, True)
    failures += not assert_eq("KPI 「欠收率」", "欠收率" in body, True)
    # by_store 表头含「运费」
    failures += not assert_eq("by_store 含运费列", ">运费</th>" in body or ">运费<" in body, True)
    # 总运费数值（订单级别）
    failures += not assert_eq(
        f"报表 total_shipping_fee = ¥{expected_shipping:.2f}",
        f"¥ {expected_shipping:.2f}" in body,
        True,
    )

    # ============================================================
    # E. 导航入口
    # ============================================================
    section("E. nav 入口验证")
    resp = client_admin.get("/")
    body = resp.data.decode()
    failures += not assert_eq("admin nav 含「运费规则」或「运费」", "运费规则" in body or ">运费<" in body, True)
    failures += not assert_eq("admin nav 含「订货报表」", "订货报表" in body or ">报表<" in body, True)

    resp = client_dc.get("/")
    body = resp.data.decode()
    failures += not assert_eq("dc_mgr nav 含「品项管理」或「品项」", "品项管理" in body or ">品项<" in body, True)

    # ============================================================
    # 总结
    # ============================================================
    section("总结")
    if failures == 0:
        print(f"  ✓ 全部通过（0 失败）")
        return 0
    print(f"  ✗ {failures} 个断言失败")
    return 1


if __name__ == "__main__":
    sys.exit(main())
