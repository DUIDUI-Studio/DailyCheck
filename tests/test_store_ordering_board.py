"""Issue #19（2026-10-24）：订单管理与订货审批合并为同一页面。

用户反馈「订单管理和订货审批的功能有重复」，管理员判定「把两个模块的页面合并，
要确保功能无损失」。合并形态：**一份实现、一张模板、两条 URL**——
`/review`（配送中心视角，DC 导航与审批动作回跳都指向它）与
`/admin/orders`（平台视角），差异只在标题文案与是否渲染跨仓筛选。

本文件锁死的验收点：
1. 两条 URL 渲染同一张页面（页签 / 表格 / 筛选结构一致）
2. 导航里只保留一个订单入口（不再并列「订单管理」+「订货审批」）
3. DC 用户只看本配送中心，且 `?dc=` / `?store=` 不生效（防越权枚举）
4. 平台管理员保留全量 + 跨仓筛选（原有能力无损失）
5. 门店用户访问两条 URL 都是 403（权限只收紧不放宽）
6. 状态页签的计数与过滤正确（原审批页「待审批 / 待出库」两份清单的信息不丢）
"""
from __future__ import annotations

import sqlite3

import pytest

BOARD_PATHS = ("/store-ordering/review", "/store-ordering/admin/orders")
SIDEBAR_START = '<aside class="sidebar'


def _sidebar(body: str) -> str:
    start = body.index(SIDEBAR_START)
    return body[start:body.index("</aside>", start)]


def _next_wh_id(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM warehouses").fetchone()[0])


def _seed_warehouses(conn: sqlite3.Connection, wh_dir, codes: dict[str, str]) -> None:
    ts = "2026-10-24 10:00:00"
    wh_id = _next_wh_id(conn)
    for code, wh_type in codes.items():
        conn.execute(
            "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (wh_id, code, code, str(wh_dir / f"{code}.db"), wh_type, ts),
        )
        wh_id += 1


def _seed_orders(conn: sqlite3.Connection, *, my_dc: str, other_dc: str, uid: int) -> None:
    ts = "2026-10-24 10:00:00"
    rows = [
        ("SO-MINE-PENDING", "store_a", my_dc, "pending"),
        ("SO-MINE-APPROVED", "store_a", my_dc, "approved"),
        ("SO-OTHER-PENDING", "store_b", other_dc, "pending"),
    ]
    for order_no, store, dc, status in rows:
        conn.execute(
            "INSERT INTO store_orders (order_no, store_warehouse_code, dc_warehouse_code,"
            " flow_type, status, requested_by, created_at, updated_at)"
            " VALUES (?, ?, ?, 'store_order', ?, ?, ?, ?)",
            (order_no, store, dc, status, uid, ts, ts),
        )


@pytest.fixture()
def board(nav_client_factory):
    """DC staff 视角的订单板 + master 里预置的三张订单（含别家 DC 的一张）。"""
    client, wh_path = nav_client_factory("distribution_center", "staff")
    master_path = wh_path.parent.parent / "master.db"
    with client.session_transaction() as sess:
        wh_id = sess["warehouse_id"]
        uid = sess["user_id"]

    conn = sqlite3.connect(master_path)
    my_dc = conn.execute("SELECT code FROM warehouses WHERE id=?", (wh_id,)).fetchone()[0]
    other_dc = "other_dc"
    _seed_warehouses(
        conn,
        wh_path.parent,
        {"store_a": "storefront", "store_b": "storefront", other_dc: "distribution_center"},
    )
    _seed_orders(conn, my_dc=my_dc, other_dc=other_dc, uid=uid)
    conn.commit()
    conn.close()

    return {
        "client": client,
        "master_path": master_path,
        "my_dc": my_dc,
        "other_dc": other_dc,
    }


@pytest.fixture()
def admin_board(nav_client_factory, board):
    """平台管理员客户端，与 board 共用同一套 master 数据。"""
    client, _ = nav_client_factory("storefront", "staff", is_admin=True)
    return {**board, "client": client}


def test_both_urls_render_the_same_page(board):
    """合并的核心：两条 URL 是同一张页面，不是两套 UI。"""
    bodies = []
    for path in BOARD_PATHS:
        resp = board["client"].get(path)
        assert resp.status_code == 200, f"{path} 应可访问（DC staff）"
        bodies.append(resp.data.decode())

    for body in bodies:
        assert "待审批" in body and "待出库" in body, "状态页签缺失"
        assert "SO-MINE-PENDING" in body
    # 页签集合与表格列一致（同一模板）
    assert bodies[0].count('class="ob-tabs"') == bodies[1].count('class="ob-tabs"') == 1


def test_dc_user_scope_is_locked_to_own_dc(board):
    """DC 用户看本配送中心的单 —— 别家 DC 的单不出现。"""
    body = board["client"].get("/store-ordering/review").data.decode()
    assert "SO-MINE-PENDING" in body
    assert "SO-MINE-APPROVED" in body
    assert "SO-OTHER-PENDING" not in body


def test_dc_user_cannot_widen_scope_via_query(board):
    """改 ?dc= 去枚举别家配送中心 —— 合并页必须忽略该参数（越权回归守卫）。"""
    body = board["client"].get(
        f"/store-ordering/admin/orders?dc={board['other_dc']}"
    ).data.decode()
    assert "SO-OTHER-PENDING" not in body, "DC 用户通过 ?dc= 看到了别家配送中心的订单"
    assert "SO-MINE-PENDING" in body


def test_dc_user_query_store_param_is_ignored(board):
    """?store= 同样不生效：DC 的范围只由登录仓库决定。"""
    body = board["client"].get("/store-ordering/review?store=store_b").data.decode()
    assert "SO-OTHER-PENDING" not in body
    assert "SO-MINE-PENDING" in body


def test_status_tab_counts_and_filtering(board):
    """页签计数 = 本 DC 的库存状态分布；?status= 过滤只留该状态。"""
    body = board["client"].get("/store-ordering/review").data.decode()
    assert "待审批<span class=\"n\">1</span>" in body
    assert "待出库<span class=\"n\">1</span>" in body

    only_pending = board["client"].get("/store-ordering/review?status=pending").data.decode()
    assert "SO-MINE-PENDING" in only_pending
    assert "SO-MINE-APPROVED" not in only_pending


def test_nav_has_single_order_entry_for_dc(board):
    """DC 导航里只有一个订单入口（不再并列「订单管理」+「订货审批」）。"""
    sidebar = _sidebar(board["client"].get("/dashboard").data.decode())
    assert 'href="/store-ordering/review"' in sidebar
    assert 'href="/store-ordering/admin/orders"' not in sidebar
    assert "订货审批" in sidebar


def test_nav_has_single_order_entry_for_admin(admin_board):
    """admin 导航同样只有一个入口，且落在平台视角 URL。"""
    sidebar = _sidebar(admin_board["client"].get("/dashboard").data.decode())
    assert 'href="/store-ordering/admin/orders"' in sidebar
    assert 'href="/store-ordering/review"' not in sidebar
    assert "订单管理" in sidebar


def test_admin_keeps_full_scope_and_cross_warehouse_filter(admin_board):
    """平台管理员原有能力无损失：全量可见 + ?dc= 筛选生效。"""
    client = admin_board["client"]
    body = client.get("/store-ordering/admin/orders").data.decode()
    assert "SO-MINE-PENDING" in body
    assert "SO-OTHER-PENDING" in body

    filtered = client.get(f"/store-ordering/admin/orders?dc={admin_board['other_dc']}").data.decode()
    assert "SO-OTHER-PENDING" in filtered
    assert "SO-MINE-PENDING" not in filtered


def test_storefront_user_forbidden_on_both_urls(nav_client_factory):
    """门店用户两条 URL 都 403 —— 合并不能变成权限放宽。"""
    client, _ = nav_client_factory("storefront", "manager")
    for path in BOARD_PATHS:
        assert client.get(path).status_code == 403, f"{path} 不应向门店用户开放"


# ---------------------------------------------------------------------------
# 多仓履约 Task 8：参与仓口径 + ?warehouse= 筛选（D7）
# ---------------------------------------------------------------------------

def test_dc_user_cannot_widen_scope_via_warehouse_param(board):
    """`?warehouse=`（参与仓）对 DC 用户同样不生效，与 ?dc=/?store= 同一守卫。"""
    body = board["client"].get(
        f"/store-ordering/review?warehouse={board['other_dc']}"
    ).data.decode()
    assert "SO-OTHER-PENDING" not in body, "DC 用户通过 ?warehouse= 看到了别家仓的订单"
    assert "SO-MINE-PENDING" in body


def test_legacy_line_less_orders_still_visible_to_head_dc(board):
    """参与仓口径不得把历史无明细行的单据从原头仓的看板上抹掉。"""
    conn = sqlite3.connect(board["master_path"])
    line_count = conn.execute("SELECT COUNT(*) FROM store_order_items").fetchone()[0]
    conn.close()
    assert line_count == 0, "本用例依赖 fixture 里没有明细行的历史订单"
    body = board["client"].get("/store-ordering/review").data.decode()
    assert "SO-MINE-PENDING" in body
    assert "SO-MINE-APPROVED" in body


def test_admin_warehouse_filter_falls_back_to_head(admin_board):
    """admin `?warehouse=` 在无明细行时回落到头仓，与订单板口径一致。"""
    filtered = admin_board["client"].get(
        f"/store-ordering/admin/orders?warehouse={admin_board['other_dc']}"
    ).data.decode()
    assert "SO-OTHER-PENDING" in filtered
    assert "SO-MINE-PENDING" not in filtered


def test_participant_filter_rendered_for_admin_only(board, admin_board):
    """参与仓下拉只给平台管理员：DC 用户不渲染该筛选（也无从生效）。"""
    admin_body = admin_board["client"].get("/store-ordering/admin/orders").data.decode()
    assert 'name="warehouse"' in admin_body
    dc_body = board["client"].get("/store-ordering/review").data.decode()
    assert 'name="warehouse"' not in dc_body
