"""起订倍数（order_multiple）单测：纯函数 + 购物车/提交流程校验。

覆盖：
- normalize_order_multiple / is_multiple_of / next_valid_quantity 边界
- set_dc_item_order_multiple 落库与归一化
- add_cart_item / update_cart_item_quantity 拒绝非法数量
- validate_cart_for_submit 把违规品项汇总进 multiples
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from blueprints import store_ordering_pure as sop
from db import init_master_db, init_warehouse_db


@pytest.fixture
def moq_env(tmp_path, monkeypatch):
    """master + DC + storefront 最小环境，DC 品项 A 起订倍数可配。"""
    import config as config_module
    import db as db_module

    master_path = tmp_path / "master.db"
    wh_dir = tmp_path / "warehouses"
    wh_dir.mkdir()
    dc_path = wh_dir / "dc_test.db"
    store_path = wh_dir / "store_test.db"

    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_dir)
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", wh_dir)

    init_master_db()
    init_warehouse_db(dc_path)
    init_warehouse_db(store_path)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    m = sqlite3.connect(str(master_path))
    m.row_factory = sqlite3.Row
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (1, 'store_staff', 'x', 0, ?)",
        (ts,),
    )
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) "
        "VALUES (1, 'dc_test', '测试配送中心', ?, 'distribution_center', ?)",
        (str(dc_path), ts),
    )
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) "
        "VALUES (2, 'store_test', '测试门店', ?, 'storefront', ?)",
        (str(store_path), ts),
    )
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (1, 2, 'staff')")
    m.execute(
        "INSERT INTO canonical_categories (code, name, description, created_at, updated_at) "
        "VALUES ('PACKAGING', '包材', '', ?, ?)",
        (ts, ts),
    )
    for cid in (101, 102):
        m.execute(
            """INSERT INTO canonical_items
               (id, canonical_sku, name, category_code, unit, gram_per_unit,
                aux_unit, aux_rate, status, created_from, created_at, updated_at)
               VALUES (?, ?, ?, 'PACKAGING', '件', 0, NULL, 0, 'active', 'rd_manual', ?, ?)""",
            (cid, f"IC-000{cid}", f"倍数品项{cid}", ts, ts),
        )
    m.commit()
    m.close()

    dc = sqlite3.connect(str(dc_path))
    dc.row_factory = sqlite3.Row
    cat_id = dc.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()["id"]
    for sku, cid in (("DC-A", 101), ("DC-B", 102)):
        dc.execute(
            "INSERT INTO items (sku, name, category_id, quantity, unit, canonical_id, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sku, f"倍数品项{cid}", cat_id, 100.0, "件", cid, ts),
        )
    dc.commit()
    dc.close()

    store = sqlite3.connect(str(store_path))
    store.row_factory = sqlite3.Row
    cat_id = store.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()["id"]
    for sku, cid in (("ST-A", 101), ("ST-B", 102)):
        store.execute(
            "INSERT INTO items (sku, name, category_id, quantity, unit, canonical_id, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sku, f"倍数品项{cid}", cat_id, 0.0, "件", cid, ts),
        )
    store.commit()
    store.close()

    m = sqlite3.connect(str(master_path))
    m.row_factory = sqlite3.Row
    yield {"master_conn": m, "master_path": master_path, "dc_path": dc_path}
    m.close()


# ---------------------------------------------------------------------------
# 纯函数边界
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 1.0),
        ("", 1.0),
        ("abc", 1.0),
        (0, 1.0),
        (-5, 1.0),
        ("1", 1.0),
        ("5", 5.0),
        (2.5, 2.5),
    ],
)
def test_normalize_order_multiple(raw, expected):
    assert sop.normalize_order_multiple(raw) == expected


@pytest.mark.parametrize(
    ("qty", "multiple", "expected"),
    [
        (10, 5, True),
        (7, 5, False),
        (5, 5, True),
        (3, 1, True),      # 1 = 不限
        (0.3, 0.1, True),  # 浮点容差
        (0.35, 0.1, False),
        (0, 5, False),
    ],
)
def test_is_multiple_of(qty, multiple, expected):
    assert sop.is_multiple_of(qty, multiple) is expected


@pytest.mark.parametrize(
    ("qty", "multiple", "expected"),
    [
        (3, 5, 5.0),
        (5, 5, 5.0),
        (11, 5, 15.0),
        (0, 5, 5.0),
        (7, 1, 7.0),
    ],
)
def test_next_valid_quantity(qty, multiple, expected):
    assert sop.next_valid_quantity(qty, multiple) == expected


def test_fmt_multiple():
    assert sop.fmt_multiple(5.0) == "5"
    assert sop.fmt_multiple(2.5) == "2.5"
    assert sop.fmt_multiple(None) == "1"


# ---------------------------------------------------------------------------
# 配置落库
# ---------------------------------------------------------------------------

def test_set_dc_item_order_multiple_roundtrip(moq_env):
    conn = moq_env["master_conn"]
    assert sop.set_dc_item_order_multiple(conn, "dc_test", 101, "6") == 6.0

    items = {it["canonical_id"]: it for it in sop.list_dc_items_for_management(conn, "dc_test")}
    assert items[101]["order_multiple"] == 6.0

    # 0 / 负值归一化成「不限」
    assert sop.set_dc_item_order_multiple(conn, "dc_test", 101, "0") == 1.0


def test_set_dc_item_order_multiple_unknown_item(moq_env):
    with pytest.raises(ValueError):
        sop.set_dc_item_order_multiple(moq_env["master_conn"], "dc_test", 999, 5)


# ---------------------------------------------------------------------------
# 购物车写入 & 提交校验
# ---------------------------------------------------------------------------

def _cart(moq_env) -> dict:
    return sop.get_or_create_cart(moq_env["master_conn"], 1, "store_test", "dc_test")


def test_add_cart_item_rejects_non_multiple(moq_env):
    conn = moq_env["master_conn"]
    sop.set_dc_item_order_multiple(conn, "dc_test", 101, 5)
    cart = _cart(moq_env)

    with pytest.raises(sop.OrderMultipleError) as ei:
        sop.add_cart_item(conn, cart["id"], 101, 3, "件")
    assert "5" in str(ei.value)
    assert sop.list_cart_items(conn, cart["id"]) == []

    # 合规数量正常写入
    sop.add_cart_item(conn, cart["id"], 101, 5, "件")
    assert [i["quantity"] for i in sop.list_cart_items(conn, cart["id"])] == [5.0]


def test_add_cart_item_validates_merged_quantity(moq_env):
    """已加购 3 件再加 2 件 → 合并成 5 件（合规）；再加 3 件 → 8 件（违规）。"""
    conn = moq_env["master_conn"]
    sop.set_dc_item_order_multiple(conn, "dc_test", 101, 5)
    cart = _cart(moq_env)
    # 倍数设为 1 时先塞 3 件进车
    sop.set_dc_item_order_multiple(conn, "dc_test", 101, 1)
    sop.add_cart_item(conn, cart["id"], 101, 3, "件")
    sop.set_dc_item_order_multiple(conn, "dc_test", 101, 5)

    sop.add_cart_item(conn, cart["id"], 101, 2, "件")  # 3+2=5 合规
    with pytest.raises(sop.OrderMultipleError):
        sop.add_cart_item(conn, cart["id"], 101, 3, "件")  # 5+3=8 违规


def test_update_cart_item_quantity_rejects_non_multiple(moq_env):
    conn = moq_env["master_conn"]
    sop.set_dc_item_order_multiple(conn, "dc_test", 101, 5)
    cart = _cart(moq_env)
    sop.add_cart_item(conn, cart["id"], 101, 5, "件")
    item = sop.list_cart_items(conn, cart["id"])[0]

    with pytest.raises(sop.OrderMultipleError):
        sop.update_cart_item_quantity(conn, item["cart_item_id"], 7)
    assert sop.list_cart_items(conn, cart["id"])[0]["quantity"] == 5.0

    sop.update_cart_item_quantity(conn, item["cart_item_id"], 10)
    assert sop.list_cart_items(conn, cart["id"])[0]["quantity"] == 10.0


def test_validate_cart_for_submit_reports_multiples(moq_env):
    conn = moq_env["master_conn"]
    sop.set_dc_item_order_multiple(conn, "dc_test", 101, 5)
    cart = _cart(moq_env)
    # 绕开 add_cart_item 的校验，直接写一行违规数据（模拟倍数被事后调大）
    ts = sop.now()
    conn.execute(
        """INSERT INTO store_order_cart_items
           (cart_id, canonical_id, quantity, unit, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (cart["id"], 101, 3.0, "件", ts, ts),
    )
    conn.commit()

    result = sop.validate_cart_for_submit(conn, cart)
    assert result["ok"] is False
    assert len(result["multiples"]) == 1
    row = result["multiples"][0]
    assert row["canonical_id"] == 101
    assert row["multiple"] == 5.0
    assert row["quantity"] == 3.0


def test_validate_cart_for_submit_ok_when_multiple_matches(moq_env):
    conn = moq_env["master_conn"]
    sop.set_dc_item_order_multiple(conn, "dc_test", 101, 5)
    cart = _cart(moq_env)
    sop.add_cart_item(conn, cart["id"], 101, 10, "件")

    result = sop.validate_cart_for_submit(conn, cart)
    assert result["ok"] is True
    assert result["multiples"] == []


def test_submit_order_refuses_invalid_multiple(moq_env):
    """底线下手：即便绕过路由，submit_order 也不允许把违规购物车转成订单。"""
    conn = moq_env["master_conn"]
    cart = _cart(moq_env)
    ts = sop.now()
    conn.execute(
        """INSERT INTO store_order_cart_items
           (cart_id, canonical_id, quantity, unit, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (cart["id"], 101, 3.0, "件", ts, ts),
    )
    conn.commit()
    sop.set_dc_item_order_multiple(conn, "dc_test", 101, 5)

    with pytest.raises(ValueError):
        sop.submit_order(conn, cart["id"], 1, None, None)
    assert conn.execute("SELECT COUNT(*) FROM store_orders").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# 路由层（真实 HTTP 流程）
# ---------------------------------------------------------------------------

@pytest.fixture
def moq_client(moq_env):
    """带 DC manager(2) / store staff(1) 两种身份的应用客户端。"""
    import sqlite3 as _sqlite3
    from datetime import datetime as _dt

    from app import create_app

    m = _sqlite3.connect(str(moq_env["master_path"]))
    ts = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (2, 'dc_mgr', 'x', 0, ?)",
        (ts,),
    )
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (2, 1, 'manager')")
    m.commit()
    m.close()

    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_route_set_multiple_requires_dc_manager(moq_env, moq_client):
    client = moq_client
    # 门店 staff（user 1）打这个路由：不是 DC 仓库 → 被弹回
    with client.session_transaction() as s:
        s["user_id"] = 1
        s["warehouse_id"] = 2
    resp = client.post("/store-ordering/dc/items/multiple", data={"canonical_id": 101, "order_multiple": "5"})
    assert resp.status_code in (301, 302, 403)
    assert sop.get_dc_item_order_multiple(moq_env["master_conn"], "dc_test", 101) == 1.0

    # DC manager 通过
    with client.session_transaction() as s:
        s["user_id"] = 2
        s["warehouse_id"] = 1
    resp = client.post(
        "/store-ordering/dc/items/multiple",
        data={"canonical_id": 101, "order_multiple": "5"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert sop.get_dc_item_order_multiple(moq_env["master_conn"], "dc_test", 101) == 5.0


def test_route_cart_batch_reports_multiple_violation(moq_env, moq_client):
    client = moq_client
    sop.set_dc_item_order_multiple(moq_env["master_conn"], "dc_test", 101, 5)

    with client.session_transaction() as s:
        s["user_id"] = 1
        s["warehouse_id"] = 2
    resp = client.post(
        "/store-ordering/cart/add-batch",
        data={
            "dc": "dc_test",
            "selected[]": ["101", "102"],
            "qty[]": ["3", "7"],
            "unit[]": ["件", "件"],
        },
        follow_redirects=True,
    )
    body = resp.get_data(as_text=True)
    assert "不符合起订倍数" in body
    # 违规项没进车，合规项进车
    quantities = {i["canonical_id"]: i["quantity"] for i in sop.list_cart_items(moq_env["master_conn"], _cart(moq_env)["id"])}
    assert 101 not in quantities
    assert quantities[102] == 7.0


def test_route_submit_page_blocks_and_shows_reason(moq_env, moq_client):
    client = moq_client
    conn = moq_env["master_conn"]
    cart = _cart(moq_env)
    ts = sop.now()
    conn.execute(
        """INSERT INTO store_order_cart_items
           (cart_id, canonical_id, quantity, unit, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (cart["id"], 101, 3.0, "件", ts, ts),
    )
    conn.commit()
    sop.set_dc_item_order_multiple(conn, "dc_test", 101, 5)

    with client.session_transaction() as s:
        s["user_id"] = 1
        s["warehouse_id"] = 2
        s["store_ordering_dc"] = "dc_test"

    resp = client.post(
        "/store-ordering/cart/submit",
        data={"expected_delivery_date": ts[:10], "note": ""},
        follow_redirects=True,
    )
    body = resp.get_data(as_text=True)
    assert "不符合起订倍数" in body
    assert conn.execute("SELECT COUNT(*) FROM store_orders").fetchone()[0] == 0

