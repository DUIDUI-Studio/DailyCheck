"""Issue #23（2026-10-24）：订单板带日期筛选整页 500（`ambiguous column name: created_at`）。

生产实测（Prod Agent，PR #21 部署后烟测）：

    GET /store-ordering/admin/orders?start_date=2020-01-01&end_date=2030-01-01 → HTTP 500
    GET /store-ordering/review?start_date=...&end_date=...                     → HTTP 500

根因：`blueprints/store_ordering_pure.py::list_orders()` 的 WHERE 子句用**未限定**列名
拼日期条件（`created_at >= ?`），而同一查询 `LEFT JOIN users` 与 `LEFT JOIN warehouses`
—— 两张表都有 `created_at` 列 → SQLite 抛 ambiguous。

本文件锁死的验收点：
1. `list_orders()` 带起止日期时不再抛异常（纯层，最小复现）
2. 日期区间真的生效：区间外的订单不出现，区间内的出现
3. **边界语义**：`start_date` 含当日 00:00:00、`end_date` 含当日 23:59:59
   （种子数据刻意放在区间首日 00:00:00 与末日 23:59:59 两个端点上，
   任何把区间改成半开区间 / 截断末秒的改动都会让本文件打红）
4. 不带日期参数时行为零变化
5. 路由层：**三条**带日期参数的 URL 都返回 200（不再 500）
   —— `/store-ordering/admin/orders`（平台）、`/store-ordering/review`（DC）、
   `/store-ordering/orders`（门店「我的订单」）；三者共用 `list_orders()`，
   但各带不同的范围裁剪，必须逐条验
6. `count_orders_by_status()` 的计数口径不因本次修复漂移

⚠️ 页签计数**不吃日期条件**（`count_orders_by_status()` 签名没有 start/end，
`_render_order_board()` 也没传）—— 带日期筛选时列表是区间内订单、页签仍是全时段
计数。这是**既有行为，本次未改**（改它属产品口径变更），本文件只锁「不改坏」。
"""
from __future__ import annotations

import sqlite3

import pytest

from blueprints import store_ordering_pure as sop

# 平台 / DC 两条订单板 URL
BOARD_PATHS = ("/store-ordering/review", "/store-ordering/admin/orders")
# 门店侧「我的订单」URL（同一个 `list_orders()`，范围裁剪不同）
STOREFRONT_PATH = "/store-ordering/orders"

# 种子订单的 created_at 分布 —— 刻意把两张放在区间端点上：
#   SO-AT-START : 区间首日 00:00:00  → 锁住 `>=`（改成 `>` 就打红）
#   SO-AT-END   : 区间末日 23:59:59  → 锁住 `<=` 与 `23:59:59`（改成 `<` / `23:59:58` 就打红）
#   SO-IN-RANGE : 区间中段
#   SO-BEFORE   : 区间之前 → 区间外
#   SO-AFTER    : 区间之后（末日次日 00:00:00）→ 区间外
RANGE_START = "2026-10-01"
RANGE_END = "2026-10-31"
AT_START_TS = "2026-10-01 00:00:00"
AT_END_TS = "2026-10-31 23:59:59"
IN_RANGE_TS = "2026-10-15 10:00:00"
BEFORE_TS = "2026-09-01 10:00:00"
AFTER_TS = "2026-11-01 00:00:00"

ALL_SEEDED_NOS = {
    "SO-AT-START",
    "SO-AT-END",
    "SO-IN-RANGE",
    "SO-BEFORE",
    "SO-AFTER",
}
IN_RANGE_NOS = {"SO-AT-START", "SO-AT-END", "SO-IN-RANGE"}


def _seed_orders(
    conn: sqlite3.Connection,
    *,
    dc_code: str,
    store_code: str,
    uid: int,
) -> None:
    rows = [
        ("SO-AT-START", AT_START_TS),
        ("SO-AT-END", AT_END_TS),
        ("SO-IN-RANGE", IN_RANGE_TS),
        ("SO-BEFORE", BEFORE_TS),
        ("SO-AFTER", AFTER_TS),
    ]
    for order_no, ts in rows:
        conn.execute(
            "INSERT INTO store_orders (order_no, store_warehouse_code, dc_warehouse_code,"
            " flow_type, status, requested_by, created_at, updated_at)"
            " VALUES (?, ?, ?, 'store_order', 'pending', ?, ?, ?)",
            (order_no, store_code, dc_code, uid, ts, ts),
        )


def _order_nos(orders: list[dict]) -> set[str]:
    return {str(o["order_no"]) for o in orders}


@pytest.fixture()
def seeded_master(tmp_path, monkeypatch):
    """master 里预置五张不同日期的订单 + users / warehouses（JOIN 存在是本 bug 的前提）。"""
    import config as config_module
    import db as db_module
    from db import init_master_db

    master_path = tmp_path / "master.db"
    wh_dir = tmp_path / "warehouses"
    wh_dir.mkdir()

    # 与 tests/conftest.py 的夹具同口径：db 与 config 两侧都 patch。
    # 纯层用例其实只经 db 侧，但一侧漏 patch 时，将来往这里挂路由用例就会写真实库。
    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_dir)
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", wh_dir)

    init_master_db()

    ts = "2026-10-15 10:00:00"
    conn = sqlite3.connect(str(master_path))
    conn.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at)"
        " VALUES (1, 'dc_staff', 'x', 0, ?)",
        (ts,),
    )
    conn.execute(
        "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at)"
        " VALUES (1, 'dc_test', '测试配送中心', ?, 'distribution_center', ?)",
        (str(wh_dir / "dc_test.db"), ts),
    )
    conn.execute(
        "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at)"
        " VALUES (2, 'store_test', '测试门店', ?, 'storefront', ?)",
        (str(wh_dir / "store_test.db"), ts),
    )
    _seed_orders(conn, dc_code="dc_test", store_code="store_test", uid=1)
    conn.commit()
    conn.close()

    conn = sqlite3.connect(str(master_path))
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# 纯层：最小复现 + 语义
# ---------------------------------------------------------------------------

def test_list_orders_with_date_range_does_not_raise(seeded_master):
    """回归核心：带日期区间不再抛 `ambiguous column name: created_at`。"""
    orders = sop.list_orders(
        seeded_master,
        dc_warehouse_code="dc_test",
        start_date=RANGE_START,
        end_date=RANGE_END,
    )
    assert _order_nos(orders) == IN_RANGE_NOS


def test_list_orders_date_range_filters_outside_orders(seeded_master):
    """区间外的订单不出现 —— 不只是「不报错」，还得真的过滤。"""
    orders = sop.list_orders(
        seeded_master,
        dc_warehouse_code="dc_test",
        start_date=RANGE_START,
        end_date=RANGE_END,
    )
    nos = _order_nos(orders)
    assert "SO-BEFORE" not in nos
    assert "SO-AFTER" not in nos


def test_list_orders_without_date_filter_is_unchanged(seeded_master):
    """不带日期参数 → 五张单全出（行为零变化）。"""
    orders = sop.list_orders(seeded_master, dc_warehouse_code="dc_test")
    assert _order_nos(orders) == ALL_SEEDED_NOS


def test_list_orders_start_date_boundary_is_inclusive(seeded_master):
    """`start_date` 含当日 00:00:00 —— 端点订单必须命中（改成 `>` 就打红）。"""
    orders = sop.list_orders(
        seeded_master,
        dc_warehouse_code="dc_test",
        start_date=RANGE_START,
        end_date=RANGE_END,
    )
    assert "SO-AT-START" in _order_nos(orders)


def test_list_orders_end_date_boundary_is_inclusive(seeded_master):
    """`end_date` 含当日 23:59:59 —— 端点订单必须命中（改成 `<` / `23:59:58` 就打红）。"""
    orders = sop.list_orders(
        seeded_master,
        dc_warehouse_code="dc_test",
        start_date=RANGE_START,
        end_date=RANGE_END,
    )
    assert "SO-AT-END" in _order_nos(orders)


def test_list_orders_single_day_window(seeded_master):
    """同日区间（start == end）只命中当天 10:00:00 的单，端点外的都不进。"""
    orders = sop.list_orders(
        seeded_master,
        dc_warehouse_code="dc_test",
        start_date="2026-10-15",
        end_date="2026-10-15",
    )
    assert _order_nos(orders) == {"SO-IN-RANGE"}


def test_list_orders_excludes_day_after_end_boundary(seeded_master):
    """末日次日 00:00:00 的单不进区间（`<= 23:59:59` 的对面）。"""
    orders = sop.list_orders(
        seeded_master,
        dc_warehouse_code="dc_test",
        start_date=RANGE_START,
        end_date=RANGE_END,
    )
    assert "SO-AFTER" not in _order_nos(orders)


def test_list_orders_start_date_only(seeded_master):
    """只传 `start_date`（不传 `end_date`）—— 单边条件的 SQL 分支同样不得歧义。"""
    nos = _order_nos(
        sop.list_orders(seeded_master, dc_warehouse_code="dc_test", start_date=RANGE_START)
    )
    assert nos == {"SO-AT-START", "SO-AT-END", "SO-IN-RANGE", "SO-AFTER"}


def test_list_orders_end_date_only(seeded_master):
    """只传 `end_date`（不传 `start_date`）—— 另一半单边分支。"""
    nos = _order_nos(
        sop.list_orders(seeded_master, dc_warehouse_code="dc_test", end_date=RANGE_END)
    )
    assert nos == {"SO-AT-START", "SO-AT-END", "SO-IN-RANGE", "SO-BEFORE"}


def test_list_orders_date_range_combined_with_other_filters(seeded_master):
    """日期区间与其他过滤条件叠加仍正常（不只单条件可用）。"""
    orders = sop.list_orders(
        seeded_master,
        dc_warehouse_code="dc_test",
        status="pending",
        flow_type="store_order",
        start_date=RANGE_START,
        end_date=RANGE_END,
    )
    assert _order_nos(orders) == IN_RANGE_NOS

    # 叠加一个不匹配的状态 → 空集，而不是异常
    empty = sop.list_orders(
        seeded_master,
        dc_warehouse_code="dc_test",
        status="shipped",
        start_date=RANGE_START,
        end_date=RANGE_END,
    )
    assert empty == []


def test_count_orders_by_status_matches_list_orders_scope(seeded_master):
    """页签计数口径与 list_orders 一致 —— 本次修复不得让计数漂移。"""
    counts = sop.count_orders_by_status(seeded_master, dc_warehouse_code="dc_test")
    assert counts == {"pending": 5}

    orders = sop.list_orders(seeded_master, dc_warehouse_code="dc_test")
    assert len(orders) == sum(counts.values())


# ---------------------------------------------------------------------------
# 路由层：三条 URL 带日期参数都不得 500
# ---------------------------------------------------------------------------

def _ensure_warehouse(
    conn: sqlite3.Connection,
    wh_dir,
    code: str,
    name: str,
    wh_type: str,
    ts: str,
) -> None:
    """建仓（已存在则跳过）—— 让 JOIN warehouses 能取到 name，与生产形态一致。

    `db_path` 必须落在本用例的 `tmp_path/warehouses/` 下（不能写 `/tmp/<code>.db`）：
    master 里存的是绝对路径，`blueprints/auth.py` 等处直接用它连库 —— 写共享
    `/tmp` 会让后续用例静默连上跨运行残留的库，污染的是别人的断言。
    """
    exists = conn.execute("SELECT 1 FROM warehouses WHERE code=?", (code,)).fetchone()
    if exists:
        return
    from db import init_warehouse_db

    db_file = wh_dir / f"{code}.db"
    init_warehouse_db(db_file)
    conn.execute(
        "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at)"
        " VALUES ((SELECT COALESCE(MAX(id),0)+1 FROM warehouses), ?, ?, ?, ?, ?)",
        (code, name, str(db_file), wh_type, ts),
    )


def _seed_board_orders(client, wh_path):
    """在 client 所属 master 里按「当前登录仓库」种五张单，返回 (my_code, my_type)。

    订单的仓码必须与登录用户当前仓库**一致**，否则会被订单板的范围裁剪
    （DC 看本 DC / 门店看本店）过滤掉 —— 那样断言会「假绿」：
    页面 200 但一张单都没渲染，测不出任何东西。
    """
    import db as db_module

    # 走夹具 patch 过的 db.MASTER_DB，不靠 `wh_path.parent.parent` 猜目录布局 ——
    # 布局一变，猜错会静默建出一个空库，报错点落到后面的 SELECT 上，排查成本高。
    master_path = db_module.MASTER_DB
    wh_dir = wh_path.parent
    with client.session_transaction() as sess:
        uid = sess["user_id"]
        wh_id = sess["warehouse_id"]

    conn = sqlite3.connect(master_path)
    row = conn.execute(
        "SELECT code, warehouse_type FROM warehouses WHERE id=?", (wh_id,)
    ).fetchone()
    my_code, my_type = row[0], row[1]

    ts = "2026-10-15 10:00:00"
    if my_type == "storefront":
        # 门店视角：订单挂在**自己的门店**名下，DC 用仓侧固定。
        store_code = my_code
        dc_code = "dc_test"
        _ensure_warehouse(conn, wh_dir, "dc_test", "测试配送中心", "distribution_center", ts)
    else:
        # DC 视角：订单挂在本 DC 名下，门店侧固定（并真实建仓，
        # 让 LEFT JOIN warehouses 能取到 name —— 与生产形态一致）。
        dc_code = my_code
        store_code = "store_test"
        _ensure_warehouse(conn, wh_dir, "store_test", "测试门店", "storefront", ts)

    _seed_orders(conn, dc_code=dc_code, store_code=store_code, uid=uid)
    conn.commit()
    conn.close()
    return my_code, my_type


@pytest.fixture()
def board_client(nav_client_factory):
    """DC staff 视角客户端 + 五张不同日期的订单。"""
    client, wh_path = nav_client_factory("distribution_center", "staff")
    _seed_board_orders(client, wh_path)
    return client


@pytest.fixture()
def storefront_client(nav_client_factory):
    """门店 staff 视角客户端 + 五张不同日期的订单（门店侧范围 = 自己的仓）。"""
    client, wh_path = nav_client_factory("storefront", "staff")
    _seed_board_orders(client, wh_path)
    return client


@pytest.mark.parametrize("path", BOARD_PATHS)
def test_board_paths_with_date_range_return_200(board_client, path):
    """生产 500 的直接回归：订单板带日期区间不再 500。"""
    resp = board_client.get(f"{path}?start_date=2020-01-01&end_date=2030-01-01")
    assert resp.status_code == 200, f"{path} 带日期参数应返回 200"


@pytest.mark.parametrize("path", BOARD_PATHS)
def test_board_paths_with_date_range_filter_content(board_client, path):
    """区间内的订单出现、区间外的不出现。"""
    body = board_client.get(
        f"{path}?start_date={RANGE_START}&end_date={RANGE_END}"
    ).data.decode()
    assert "SO-IN-RANGE" in body
    assert "SO-BEFORE" not in body
    assert "SO-AFTER" not in body


@pytest.mark.parametrize("path", BOARD_PATHS)
def test_board_paths_without_date_range_still_200(board_client, path):
    """不带日期参数行为零变化。"""
    resp = board_client.get(path)
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "SO-IN-RANGE" in body and "SO-BEFORE" in body and "SO-AFTER" in body


def test_storefront_orders_path_with_date_range_returns_200(storefront_client):
    """门店侧「我的订单」同样共用 `list_orders()`，带日期参数也不得 500。

    该 URL 与订单板两条 URL 的范围裁剪不同（门店只看自己的仓），
    所以必须单独验，不能靠订单板用例顺带覆盖。
    """
    resp = storefront_client.get(
        f"{STOREFRONT_PATH}?start_date=2020-01-01&end_date=2030-01-01"
    )
    assert resp.status_code == 200, f"{STOREFRONT_PATH} 带日期参数应返回 200"


def test_storefront_orders_path_with_date_range_filter_content(storefront_client):
    """门店侧日期筛选真的生效：区间内的出、区间外的不出。"""
    body = storefront_client.get(
        f"{STOREFRONT_PATH}?start_date={RANGE_START}&end_date={RANGE_END}"
    ).data.decode()
    assert "SO-IN-RANGE" in body
    assert "SO-BEFORE" not in body
    assert "SO-AFTER" not in body
