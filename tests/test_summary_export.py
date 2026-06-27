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
    return client


def test_export_has_utf8_bom(tmp_path, monkeypatch):
    client = _login_as_admin(tmp_path, monkeypatch)
    resp = client.get("/summary/export?range=7d")
    assert resp.status_code == 200
    assert resp.data.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM


def test_export_filename(tmp_path, monkeypatch):
    client = _login_as_admin(tmp_path, monkeypatch)
    resp = client.get("/summary/export?range=7d")
    cd = resp.headers.get("Content-Disposition", "")
    assert "summary-" in cd
    assert "-7d.csv" in cd


def test_export_three_sections(tmp_path, monkeypatch):
    client = _login_as_admin(tmp_path, monkeypatch)
    resp = client.get("/summary/export?range=7d")
    body = resp.data.decode("utf-8-sig")
    assert "范围" in body
    assert "进货金额" in body
    assert "消耗金额" in body