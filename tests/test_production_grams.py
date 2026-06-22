"""Layer 2: 生产录入克换算集成测试（跨边界）。"""
import sqlite3
from datetime import datetime


def _wh(wh_path):
    conn = sqlite3.connect(wh_path)
    conn.row_factory = sqlite3.Row
    return conn


def _seed_item(wh_path, name, unit, qty, gram_per_unit):
    """直接在仓库 db 建一个物品，返回其 id。category_id 取第一个固定品类。"""
    conn = _wh(wh_path)
    cat_id = conn.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()["id"]
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sku = f"T-{name}"
    cur = conn.execute(
        "INSERT INTO items (sku, name, category_id, quantity, safety_stock, unit_cost, unit, gram_per_unit, updated_at) "
        "VALUES (?, ?, ?, ?, 0, 0, ?, ?, ?)",
        (sku, name, cat_id, qty, unit, gram_per_unit, ts))
    item_id = cur.lastrowid
    conn.commit()
    conn.close()
    return item_id


def _seed_product_with_bom(wh_path, pname, bom):
    """建产品 + 配方。bom = [(item_id, qty_per_unit), ...]，返回 product_id。"""
    conn = _wh(wh_path)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "INSERT INTO products (name, unit, note, created_at) VALUES (?, '件', '', ?)",
        (pname, ts))
    pid = cur.lastrowid
    for item_id, qpu in bom:
        conn.execute(
            "INSERT INTO product_bom (product_id, item_id, qty_per_unit) VALUES (?, ?, ?)",
            (pid, item_id, qpu))
    conn.commit()
    conn.close()
    return pid


def test_gram_item_deducts_stock_units(logged_client):
    """启用克的原料：配方 480 克/件，产出 3 件 → 扣 1.44 袋（1 袋=1000 克）。"""
    client, wh_path = logged_client
    milk = _seed_item(wh_path, "牛乳", "袋", qty=50, gram_per_unit=1000)
    pid = _seed_product_with_bom(wh_path, "焦糖海盐", [(milk, 480)])

    resp = client.post("/production/submit", data={
        "product_id": str(pid),
        "output_qty": "3",
        "note": "",
        f"actual_{milk}": "",  # 留空 → 服务端用 planned
    }, follow_redirects=False)
    assert resp.status_code in (302, 303)

    conn = _wh(wh_path)
    # 库存：50 - 1.44 = 48.56 袋
    stock = conn.execute("SELECT quantity FROM items WHERE id=?", (milk,)).fetchone()["quantity"]
    assert round(stock, 2) == 48.56
    # production_run_items.actual_qty 是库存单位
    pri = conn.execute("SELECT actual_qty FROM production_run_items WHERE item_id=?", (milk,)).fetchone()
    assert round(pri["actual_qty"], 2) == 1.44
    # outbound_requests 写库存单位
    ob = conn.execute(
        "SELECT requested_quantity FROM outbound_requests WHERE item_id=? AND reason LIKE '生产领料%'",
        (milk,)).fetchone()
    assert round(ob["requested_quantity"], 2) == 1.44
    # stock_movements 写库存单位（负）
    sm = conn.execute(
        "SELECT delta FROM stock_movements WHERE item_id=? AND action='生产消耗'",
        (milk,)).fetchone()
    assert round(sm["delta"], 2) == -1.44
    conn.close()


def test_non_gram_item_unchanged(logged_client):
    """未启用克：配方 2 条/件，产出 3 件 → 扣 6 条（原逻辑不变）。"""
    client, wh_path = logged_client
    cone = _seed_item(wh_path, "甜筒", "条", qty=100, gram_per_unit=0)
    pid = _seed_product_with_bom(wh_path, "甜筒产品", [(cone, 2)])

    resp = client.post("/production/submit", data={
        "product_id": str(pid), "output_qty": "3", "note": "", f"actual_{cone}": "",
    })
    assert resp.status_code in (302, 303)

    conn = _wh(wh_path)
    stock = conn.execute("SELECT quantity FROM items WHERE id=?", (cone,)).fetchone()["quantity"]
    assert round(stock, 2) == 94.0  # 100 - 6
    conn.close()


def test_mixed_recipe(logged_client):
    """混合配方：克原料 + 非克原料各扣各的。"""
    client, wh_path = logged_client
    milk = _seed_item(wh_path, "牛乳2", "袋", qty=10, gram_per_unit=1000)
    cone = _seed_item(wh_path, "甜筒2", "条", qty=20, gram_per_unit=0)
    pid = _seed_product_with_bom(wh_path, "混合品", [(milk, 500), (cone, 1)])

    resp = client.post("/production/submit", data={
        "product_id": str(pid), "output_qty": "2", "note": "",
        f"actual_{milk}": "", f"actual_{cone}": "",
    })
    assert resp.status_code in (302, 303)

    conn = _wh(wh_path)
    milk_stock = conn.execute("SELECT quantity FROM items WHERE id=?", (milk,)).fetchone()["quantity"]
    cone_stock = conn.execute("SELECT quantity FROM items WHERE id=?", (cone,)).fetchone()["quantity"]
    assert round(milk_stock, 2) == 9.0   # 10 - (500*2/1000=1.0)
    assert round(cone_stock, 2) == 18.0  # 20 - 2
    conn.close()


def test_rollback_restores_stock(logged_client):
    """生产后回退：库存精确还原（验证零改动回退路径正确）。"""
    client, wh_path = logged_client
    milk = _seed_item(wh_path, "牛乳3", "袋", qty=5, gram_per_unit=1000)
    pid = _seed_product_with_bom(wh_path, "回退品", [(milk, 480)])

    client.post("/production/submit", data={
        "product_id": str(pid), "output_qty": "3", "note": "", f"actual_{milk}": "",
    })
    conn = _wh(wh_path)
    run_id = conn.execute("SELECT id FROM production_runs ORDER BY id DESC LIMIT 1").fetchone()["id"]
    after_submit = conn.execute("SELECT quantity FROM items WHERE id=?", (milk,)).fetchone()["quantity"]
    conn.close()
    assert round(after_submit, 2) == 3.56  # 5 - 1.44

    resp = client.post(f"/production/runs/{run_id}/rollback")
    assert resp.status_code in (302, 303)

    conn = _wh(wh_path)
    restored = conn.execute("SELECT quantity FROM items WHERE id=?", (milk,)).fetchone()["quantity"]
    assert round(restored, 2) == 5.0  # 精确还原
    conn.close()
