"""汇总 CSV 导出测试。"""
import sqlite3
from datetime import datetime


def _login_as_admin(tmp_path, monkeypatch):
    import db as db_module
    from db import init_master_db, init_warehouse_db
    master_path = tmp_path / "master.db"
    wh_path = tmp_path / "wh.db"
    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", tmp_path)
    init_master_db()
    init_warehouse_db(wh_path)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    m = sqlite3.connect(master_path)
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (1, 'admin', 'x', 1, ?)", (ts,))
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, created_at) "
        "VALUES (1, 'wh_t', 'T', ?, ?)", (str(wh_path), ts))
    m.execute(
        "INSERT INTO warehouse_users (user_id, warehouse_id, role) "
        "VALUES (1, 1, 'admin')")
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


def test_export_has_utf8_bom(tmp_path, monkeypatch):
    client, _ = _login_as_admin(tmp_path, monkeypatch)
    resp = client.get("/summary/export?range=7d")
    assert resp.status_code == 200
    assert resp.data.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM


def test_export_filename(tmp_path, monkeypatch):
    client, _ = _login_as_admin(tmp_path, monkeypatch)
    resp = client.get("/summary/export?range=7d")
    cd = resp.headers.get("Content-Disposition", "")
    assert "summary-" in cd
    assert "-7d.csv" in cd


def test_export_three_sections(tmp_path, monkeypatch):
    client, _ = _login_as_admin(tmp_path, monkeypatch)
    resp = client.get("/summary/export?range=7d")
    body = resp.data.decode("utf-8-sig")
    assert "范围" in body
    assert "进货金额" in body
    assert "消耗金额" in body


def test_export_category_section_has_avg_stock_value_column(tmp_path, monkeypatch):
    """CSV 品类段表头必须含「平均库存金额」列(算法与口径同步)。

    算法从 cat_consumed / cat_stock 改为 cat_consumed / avg(start+end),
    没有这列数据,CSV 消费者看不到分母。
    """
    client, _ = _login_as_admin(tmp_path, monkeypatch)
    resp = client.get("/summary/export?range=7d")
    body = resp.data.decode("utf-8-sig")
    assert "平均库存金额" in body


def test_export_category_turnover_uses_avg_stock_value(tmp_path, monkeypatch):
    """CSV 品类段周转率 = cat_consumed / avg(start+end),与 /summary 同口径。

    用已知数据反推:造一个品项,起点库存 200 件(值 2000),窗口内消耗 100,
    终点库存 100(值 1000)。avg = (2000+1000)/2 = 1500。
    turnover = 1000 / 1500 ≈ 0.67。
    """
    client, wh_path = _login_as_admin(tmp_path, monkeypatch)
    conn = sqlite3.connect(wh_path)
    cat_id = conn.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()[0]
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # item: 起始 200,消耗 100(出库 100),单价 10 → 终值 1000
    conn.execute(
        "INSERT INTO items (sku, name, category_id, quantity, safety_stock, unit_cost, unit, gram_per_unit, updated_at) "
        "VALUES ('CSV-AVG', 'avgItem', ?, 100, 0, 10, '件', 0, ?)", (cat_id, ts))
    item_id = conn.execute("SELECT id FROM items WHERE name='avgItem'").fetchone()[0]
    # 让 stock_movements 体现窗口内 delta = -100,这样反推起点 = 100 - (-100) = 200
    conn.execute(
        "INSERT INTO stock_movements (item_id, delta, action, created_at) "
        "VALUES (?, -100, 'outbound', ?)", (item_id, ts))
    conn.execute(
        "INSERT INTO outbound_requests (item_id, requested_quantity, reason, rolled_back, created_at) "
        "VALUES (?, 100, NULL, 0, ?)", (item_id, ts))
    conn.commit()
    conn.close()

    resp = client.get("/summary/export?range=7d")
    body = resp.data.decode("utf-8-sig")
    # 找品类行:取「avgItem」所在品类那一行(本测试只造一个品项)
    # CSV 段 2 行的最后列是周转率,期望 ≈ 0.67
    # 简单验证:CSV 段 2 行的周转率 = 1000/1500 ≈ 0.67
    # 用 round(0.6667, 2) = 0.67
    assert "0.67" in body
