"""物品路由测试:权限收紧 + gram_per_unit / unit_cost 校验。"""
import sqlite3
from datetime import datetime


def _seed_master_user(master_path, *, is_admin=True, role="manager", wh_db_path=None):
    """在临时 master.db 插入用户 + 仓库绑定。is_admin=True 时跳过 role 检查。

    wh_db_path 为 None 时用一个不会自动创建的占位路径,避免在项目根
    留下 unused.db 之类的测试产物。staff 测试走这个分支,在到达业务
    代码前就被 403 拦截。
    """
    if wh_db_path is None:
        # 用一个绝对路径下不存在的占位符,sqlite3.connect() 会在迁移
        # 路径检查之前不会触碰这个路径。test_staff 只需 403,不走业务
        # 代码,这样不会在 cwd 留下 'unused' 之类的临时文件。
        wh_db_path = "/dev/null/this_path_will_never_exist_for_staff_test.db"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    m = sqlite3.connect(master_path)
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (1, 'admin', 'x', ?, ?)", (1 if is_admin else 0, ts))
    if not is_admin:
        m.execute(
            "INSERT INTO warehouses (id, code, name, db_path, created_at) "
            "VALUES (1, 'wh_t', 'T', ?, ?)", (wh_db_path, ts))
        m.execute(
            "INSERT INTO warehouse_users (user_id, warehouse_id, role) "
            "VALUES (1, 1, ?)", (role,))
    m.commit()
    m.close()


def test_staff_gets_403_on_items(tmp_path, monkeypatch):
    import db as db_module
    from db import init_master_db, init_warehouse_db
    master_path = tmp_path / "master.db"
    wh_path = tmp_path / "wh.db"
    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", tmp_path)
    init_master_db()
    init_warehouse_db(wh_path)
    _seed_master_user(master_path, is_admin=False, role="staff")

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = 1
        s["warehouse_id"] = 1

    resp = client.get("/items")
    assert resp.status_code == 403


def test_manager_can_view_items(tmp_path, monkeypatch):
    import db as db_module
    from db import init_master_db, init_warehouse_db
    master_path = tmp_path / "master.db"
    wh_path = tmp_path / "wh.db"
    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", tmp_path)
    init_master_db()
    init_warehouse_db(wh_path)
    _seed_master_user(master_path, is_admin=False, role="manager", wh_db_path=str(wh_path))

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = 1
        s["warehouse_id"] = 1

    resp = client.get("/items")
    assert resp.status_code == 200


def test_negative_gram_per_unit_rejected(tmp_path, monkeypatch):
    import db as db_module
    from db import init_master_db, init_warehouse_db
    master_path = tmp_path / "master.db"
    wh_path = tmp_path / "wh.db"
    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", tmp_path)
    init_master_db()
    init_warehouse_db(wh_path)
    _seed_master_user(master_path, is_admin=False, role="manager", wh_db_path=str(wh_path))

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = 1
        s["warehouse_id"] = 1

    conn = sqlite3.connect(wh_path)
    cat_id = conn.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()[0]
    conn.close()

    resp = client.post("/items", data={
        "name": "test",
        "category_id": str(cat_id),
        "quantity": "0",
        "safety_stock": "0",
        "unit_cost": "0",
        "unit": "件",
        "gram_per_unit": "-5",
    }, follow_redirects=False)
    assert resp.status_code in (302, 303)

    conn = sqlite3.connect(wh_path)
    cnt = conn.execute("SELECT COUNT(*) FROM items WHERE name='test'").fetchone()[0]
    conn.close()
    assert cnt == 0
