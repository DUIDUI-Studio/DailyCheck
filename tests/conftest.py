"""集成测试夹具：临时仓库 db + 直设 session 登录。

绕过密码校验（直接写 session），规避 Python 3.9 无 hashlib.scrypt
导致 werkzeug 默认 hash 不可用的问题。
"""
import sqlite3
from datetime import datetime

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
    # Also patch config directly — several blueprints (forecast,
    # procurement, notifications) import MASTER_DB from config at call
    # time to bypass flask 'g', so without this patch the test would
    # write to the real on-disk master.db and pollute it.
    import config as config_module
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", wh_dir)

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


@pytest.fixture
def staff_client(tmp_path, monkeypatch):
    """Same temp-db layout as logged_client but the bound user has role=staff.

    Adds a second user (id=2) bound to the same warehouse with role='staff'
    and sets the session to that user. Used by forecast role-gate tests
    (TASK 9).
    """
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
        "VALUES (1, 'admin', 'x', 1, ?)", (ts,))
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (2, 'staff1', 'x', 0, ?)", (ts,))
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, created_at) "
        "VALUES (1, 'wh_test', '测试仓', ?, ?)", (str(wh_path), ts))
    m.execute(
        "INSERT INTO warehouse_users (user_id, warehouse_id, role) "
        "VALUES (1, 1, 'admin')")
    m.execute(
        "INSERT INTO warehouse_users (user_id, warehouse_id, role) "
        "VALUES (2, 1, 'staff')")
    m.commit()
    m.close()

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = 2
        s["warehouse_id"] = 1

    return client, wh_path


@pytest.fixture
def nav_client_factory(tmp_path, monkeypatch):
    """按「仓库类型 × 角色 × 是否平台管理员」批量构造登录客户端。

    导航三载体（侧边栏 / 移动底栏 / `/land` 模块卡）的角色矩阵断言需要
    storefront / distribution_center / rd 三类仓库 × staff / manager / admin
    三类角色 × 平台管理员开关的组合，而 `logged_client` / `staff_client` 只覆盖
    「单仓 + 平台管理员」「单仓 + staff」。这里提供工厂：同一夹具实例内每个
    (warehouse_type, role, is_admin) 组合建独立用户 + 独立仓库 db，重复调用
    同一组合返回同一客户端。

    用法：
        client, _ = nav_client_factory("storefront", "admin")
        client, _ = nav_client_factory(warehouse_type="rd")
    """
    import config as config_module
    import db as db_module
    from db import init_master_db, init_warehouse_db

    master_path = tmp_path / "master.db"
    wh_dir = tmp_path / "warehouses"
    wh_dir.mkdir(exist_ok=True)

    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_dir)
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", wh_dir)

    init_master_db()

    # 惰性建 app：不调用 _build 的用例（以及只用 fixture 做别的事的用例）不必付
    # create_app() 的代价。
    _app_cache: list = []

    def _app():
        if not _app_cache:
            from app import create_app
            application = create_app()
            application.config["TESTING"] = True
            _app_cache.append(application)
        return _app_cache[0]

    built: dict[str, tuple] = {}

    def _build(warehouse_type="storefront", role="staff", is_admin=False):
        key = f"{warehouse_type}-{role}-{'admin' if is_admin else 'normal'}"
        if key in built:
            return built[key]

        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 文件名必须等于 f"{code}.db"：store_ordering_pure.open_warehouse_db(code)
        # 是按 code 拼路径的，与 master.db_path 必须是同一个文件。
        wh_path = wh_dir / f"{key}.db"
        init_warehouse_db(wh_path)

        m = sqlite3.connect(master_path)

        # id 取 max+1 而非 len(built)+1：本夹具与 logged_client / staff_client 若在同一
        # 用例里共用 tmp_path，后者的 user 1 / warehouse 1 已存在，硬编码从 1 递增会撞
        # UNIQUE 约束。
        uid = m.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM users").fetchone()[0]
        wh_id = m.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM warehouses").fetchone()[0]

        # 首次构建时补一个配送中心：订货目录 / 订货报表 / 订单管理等页面会按
        # master 里的 DC 列表解析仓库（无 DC 时会落到空 code 的库上 → 500）。
        # 与生产环境一致（门店订货必然有 DC），故属于夹具最小必要数据。
        if not built:
            dc_path = wh_dir / "dc_test.db"
            init_warehouse_db(dc_path)
            m.execute(
                "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) "
                "VALUES (?, 'dc_test', '测试配送中心', ?, 'distribution_center', ?)",
                (wh_id, str(dc_path), ts))
            wh_id += 1

        m.execute(
            "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
            "VALUES (?, ?, 'x', ?, ?)", (uid, key, 1 if is_admin else 0, ts))
        m.execute(
            "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (wh_id, key, key, str(wh_path), warehouse_type, ts))
        m.execute(
            "INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (?, ?, ?)",
            (uid, wh_id, role))
        m.commit()
        m.close()

        client = _app().test_client()
        with client.session_transaction() as s:
            s["user_id"] = uid
            s["warehouse_id"] = wh_id
        built[key] = (client, wh_path)
        return client, wh_path

    return _build


def _wh(wh_path):
    conn = sqlite3.connect(wh_path)
    conn.row_factory = sqlite3.Row
    return conn


def _seed_item(wh_path, name, qty, unit_cost, gram_per_unit=0):
    """插入一个测试品项,返回 (item_id, category_id)。"""
    conn = _wh(wh_path)
    cat_id = conn.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()["id"]
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "INSERT INTO items (sku, name, category_id, quantity, safety_stock, unit_cost, unit, gram_per_unit, updated_at) "
        "VALUES (?, ?, ?, ?, 0, ?, '件', ?, ?)",
        (f"T-{name}", name, cat_id, qty, unit_cost, gram_per_unit, ts))
    item_id = cur.lastrowid
    conn.commit()
    conn.close()
    return item_id, cat_id


def _seed_outbound(wh_path, item_id, qty, reason=None):
    """插入一条出库请求。"""
    conn = _wh(wh_path)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO outbound_requests (item_id, requested_quantity, reason, rolled_back, created_at) "
        "VALUES (?, ?, ?, 0, ?)",
        (item_id, qty, reason, ts))
    conn.commit()
    conn.close()


def _seed_store_ordering_env(
    tmp_path,
    monkeypatch,
    *,
    warehouse_type="storefront",
    dc_code="dc_test",
    store_code="store_test",
):
    """Create a temporary master + two warehouse dbs (store + DC) for store-ordering tests.

    Returns (app, client, master_path, dc_path, store_path).
    """
    import db as db_module
    from db import init_master_db, init_warehouse_db

    master_path = tmp_path / "master.db"
    wh_dir = tmp_path / "warehouses"
    wh_dir.mkdir()
    dc_path = wh_dir / f"{dc_code}.db"
    store_path = wh_dir / f"{store_code}.db"

    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_dir)
    import config as config_module
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", wh_dir)

    init_master_db()
    init_warehouse_db(dc_path)
    init_warehouse_db(store_path)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    m = sqlite3.connect(master_path)
    m.row_factory = sqlite3.Row
    # Users: admin(1), store staff(2), store manager(3), DC staff(4), DC manager(5)
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (1, 'admin', 'x', 1, ?)",
        (ts,),
    )
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (2, 'store_staff', 'x', 0, ?)",
        (ts,),
    )
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (3, 'store_mgr', 'x', 0, ?)",
        (ts,),
    )
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (4, 'dc_staff', 'x', 0, ?)",
        (ts,),
    )
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (5, 'dc_mgr', 'x', 0, ?)",
        (ts,),
    )
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) VALUES (1, ?, '测试配送中心', ?, 'distribution_center', ?)",
        (dc_code, str(dc_path), ts),
    )
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) VALUES (2, ?, '测试门店', ?, 'storefront', ?)",
        (store_code, str(store_path), ts),
    )
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (2, 2, 'staff')")
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (3, 2, 'manager')")
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (4, 1, 'staff')")
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (5, 1, 'manager')")
    m.commit()
    m.close()

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    return app, client, master_path, dc_path, store_path


def _seed_production_consumption(wh_path, item_id, qty):
    """插入一条生产消耗(自动建 product + production_run)。"""
    conn = _wh(wh_path)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "INSERT INTO products (name, unit, note, created_at) VALUES ('test-product', '件', '', ?)",
        (ts,))
    product_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO production_runs (product_id, output_qty, note, rolled_back, created_at) "
        "VALUES (?, 1, 'test', 0, ?)", (product_id, ts))
    run_id = cur.lastrowid
    conn.execute(
        "INSERT INTO production_run_items (run_id, item_id, planned_qty, actual_qty) VALUES (?, ?, ?, ?)",
        (run_id, item_id, qty, qty))
    conn.commit()
    conn.close()
