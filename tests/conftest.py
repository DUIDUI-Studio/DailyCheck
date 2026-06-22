"""集成测试夹具：临时仓库 db + 直设 session 登录。

绕过密码校验（直接写 session），规避 Python 3.9 无 hashlib.scrypt
导致 werkzeug 默认 hash 不可用的问题。
"""
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest


@pytest.fixture
def logged_client(tmp_path, monkeypatch):
    import db as db_module
    from db import init_master_db, init_warehouse_db

    master_path = tmp_path / "master.db"
    wh_dir = tmp_path / "warehouses"
    wh_dir.mkdir()
    wh_path = wh_dir / "wh_test.db"

    # 让 db 模块用临时路径
    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_dir)

    init_master_db()
    init_warehouse_db(wh_path)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    m = sqlite3.connect(master_path)
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (1, 'admin', 'x', 1, ?)", (ts,))
    # db_path 存绝对路径：auth.py 用 BASE_DIR / db_path，绝对路径右值会覆盖
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, created_at) "
        "VALUES (1, 'wh_test', '测试仓', ?, ?)", (str(wh_path), ts))
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


def _wh(wh_path):
    conn = sqlite3.connect(wh_path)
    conn.row_factory = sqlite3.Row
    return conn
