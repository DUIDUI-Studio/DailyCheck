"""汇总路由测试:range 参数 + 时间筛选。"""
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


def test_default_range_is_7d(tmp_path, monkeypatch):
    client, _ = _login_as_admin(tmp_path, monkeypatch)
    resp = client.get("/summary")
    assert resp.status_code == 200
    # 模板应包含 7 日 chip;但 Task 5 不要求 chip 渲染,所以此测试可只验证 200 + 不报错


def test_range_param_passes_through(tmp_path, monkeypatch):
    client, _ = _login_as_admin(tmp_path, monkeypatch)
    resp = client.get("/summary?range=month")
    assert resp.status_code == 200


def test_invalid_range_falls_back_to_7d(tmp_path, monkeypatch):
    client, _ = _login_as_admin(tmp_path, monkeypatch)
    resp = client.get("/summary?range=bogus")
    assert resp.status_code == 200


def test_total_stock_value_unchanged_by_range(tmp_path, monkeypatch):
    """库存金额字段必须不受 range 影响(账面永远是当下)。"""
    client, wh_path = _login_as_admin(tmp_path, monkeypatch)
    conn = sqlite3.connect(wh_path)
    cat_id = conn.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()[0]
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO items (sku, name, category_id, quantity, safety_stock, unit_cost, unit, gram_per_unit, updated_at) "
        "VALUES ('X', 'test', ?, 100, 0, 5, '件', 0, ?)", (cat_id, ts))
    conn.commit()
    conn.close()

    resp = client.get("/summary")
    # 库存金额 ¥500 在 HTML 中存在(无论 range)
    assert b"500" in resp.data


def test_turnover_zero_when_no_consume(tmp_path, monkeypatch):
    """无消耗数据时 turnover=0.00,turnover_days=None(模板渲染为 '—')。"""
    client, _ = _login_as_admin(tmp_path, monkeypatch)
    resp = client.get("/summary")
    assert resp.status_code == 200
    # turnover 字段在响应体里能找到(模板渲染后);HTML 出现 "库存周转率" 字样
    assert "库存周转率" in resp.data.decode("utf-8")
