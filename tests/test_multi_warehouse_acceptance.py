"""多仓履约验收矩阵汇总：spec v2 §13 七领域端到端（Task 11）。

本文件是**验收汇总**，不是第二次逐条实现。每个领域的细粒度用例仍由各 Task
的专门测试负责，这里只做两类补充：

1. **跨 Task 串起来的端到端链** —— 建跨仓单 → 双仓并行发货 → 交错收货 →
   delivered 一条测试打通（``test_cross_warehouse_end_to_end_*``）。
2. **各领域必须可观察的那一条** —— 薄聚合断言，保证矩阵每一行在同一个夹具
   （``tests/multi_warehouse_env.py``）下可复现。

spec §13 领域 → 本文件用例：

============  ==========================================================
数量与事务    retry 只扣一次 / 取消与发货竞争不产生已取消在途单
并发状态      A 发收时 B 未发：单停 approved 且 A 可收；收齐 delivered 一次
路由          缺路由/专属/重复绑定/单位冲突失败关闭；改路由不改历史单；镜像价不泄漏
可见与动作    B-only 批准缺权 403；空表单/越仓行不越权
迁移与发布    跨仓单建成后重跑迁移保留来源；开关关=存量恒等（含未路由品）
报表与补货    多仓筛选数量/订单数/运费不跨仓求和；非主仓健康指标不漏
前端与通知    草稿键按 (user, store) 隔离且回退不重复加购；收件方向矩阵；无全权审批人可诊断
============  ==========================================================

逐项细节参考：``test_ship_op_protocol`` / ``test_receipt_op_protocol`` /
``test_submit_routing`` / ``test_catalog_multi_dc`` / ``test_approve_multi_dc`` /
``test_store_order_mcp`` / ``test_report_multi_dc`` / ``test_replenishment_routing`` /
``test_migration_multi_warehouse`` / ``test_notification_matrix``。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

import reports_health_pure as hp
from blueprints import fulfillment_pure
from blueprints import store_ordering_pure as sop
from tests import multi_warehouse_env as env_builder

TS = "2026-10-29 12:00:00"
REPORT_DAY = "2026-10-29"


@pytest.fixture
def env(tmp_path, monkeypatch):
    env = env_builder.build(tmp_path, monkeypatch)
    env_builder.enable(monkeypatch)
    return env


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _tomorrow() -> str:
    return (datetime.now().date() + timedelta(days=1)).isoformat()


def _login(env, user_id: int, warehouse_id: int) -> None:
    with env["client"].session_transaction() as session:
        session["user_id"] = user_id
        session["warehouse_id"] = warehouse_id


def _add(env, canonical_id: int, quantity: float) -> None:
    env["client"].post(
        "/store-ordering/cart/add",
        data={"canonical_id": canonical_id, "quantity": str(quantity), "unit": "件"},
    )


def _submit(env, **extra):
    data = {"expected_delivery_date": _tomorrow(), "note": ""}
    data.update(extra)
    return env["client"].post(
        "/store-ordering/cart/submit", data=data, follow_redirects=True,
    )


def _master(env) -> sqlite3.Connection:
    return env_builder.connect(env["master_path"])


def _orders(env) -> list[sqlite3.Row]:
    conn = _master(env)
    rows = conn.execute("SELECT * FROM store_orders ORDER BY id").fetchall()
    conn.close()
    return rows


def _lines(env, order_id: int) -> dict[int, sqlite3.Row]:
    conn = _master(env)
    rows = conn.execute(
        "SELECT * FROM store_order_items WHERE order_id=? ORDER BY id", (order_id,),
    ).fetchall()
    conn.close()
    return {int(row["canonical_id"]): row for row in rows}


def _detail(env, order_id: int) -> dict:
    conn = _master(env)
    try:
        detail = sop.get_order_detail(conn, order_id)
    finally:
        conn.close()
    assert detail is not None
    return detail


def _add_user(env, user_id: int, username: str, *, is_admin: int = 0) -> None:
    conn = _master(env)
    conn.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (?, ?, 'x', ?, ?)",
        (user_id, username, is_admin, TS),
    )
    conn.commit()
    conn.close()


def _grant(env, user_id: int, warehouse_id: int, role: str) -> None:
    conn = _master(env)
    conn.execute(
        "INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (?, ?, ?)",
        (user_id, warehouse_id, role),
    )
    conn.commit()
    conn.close()


def _stock(env, warehouse_code: str, canonical_id: int) -> float:
    conn = env_builder.connect(env["paths"][warehouse_code])
    row = conn.execute(
        "SELECT quantity FROM items WHERE canonical_id=?", (canonical_id,),
    ).fetchone()
    conn.close()
    return float(row["quantity"])


def _notifications(env, event_type: str) -> list[sqlite3.Row]:
    conn = _master(env)
    rows = conn.execute(
        "SELECT user_id, target_url FROM notifications WHERE event_type=?",
        (event_type,),
    ).fetchall()
    conn.close()
    return rows


def _ship(env, order_id: int, **form):
    return env["client"].post(
        f"/store-ordering/orders/{order_id}/ship", data=form, follow_redirects=True,
    )


def _receive(env, order_id: int, order_item_id: int, quantity: float, op_no: str):
    return env["client"].post(
        f"/store-ordering/orders/{order_id}/receive",
        data={"order_item_id": order_item_id, "quantity": str(quantity), "op_no": op_no},
        follow_redirects=True,
    )


def _grant_full_approver(env) -> None:
    """alpha 经理同时持有 beta 仓 manager，凑出「对全部参与仓有审批权」。"""
    _grant(env, 2, 2, "manager")


def _build_cross_order(env) -> int:
    """门店提交一张 alpha+beta 的跨仓单，返回 order_id（pending）。"""
    _login(env, 1, 3)
    _add(env, 201, 2)   # dc_alpha
    _add(env, 206, 1)   # dc_alpha
    _add(env, 202, 10)  # dc_beta
    _submit(env)
    orders = _orders(env)
    assert len(orders) == 1, orders
    return int(orders[0]["id"])


def _insert_cross_order(env, *, status: str = "pending", fee: float = 10.0) -> int:
    """直接落一张三行跨仓单（报表/通知用，不经过路由提交）。"""
    conn = _master(env)
    cur = conn.execute(
        """INSERT INTO store_orders
           (order_no, store_warehouse_code, dc_warehouse_code, status, requested_by,
            created_at, updated_at, shipping_fee)
           VALUES ('SO-ACC-1', 'store_one', 'dc_alpha', ?, 1, ?, ?, ?)""",
        (status, TS, TS, fee),
    )
    order_id = int(cur.lastrowid)
    for canonical_id, source, quantity, shipped, fulfilled in (
        (201, "dc_alpha", 2.0, 2.0, 2.0),
        (206, "dc_alpha", 1.0, 0.0, 0.0),
        (202, "dc_beta", 10.0, 10.0, 10.0),
    ):
        conn.execute(
            """INSERT INTO store_order_items
               (order_id, canonical_id, source_warehouse_code, quantity,
                shipped_quantity, fulfilled_quantity, unit, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, '件', 'pending', ?)""",
            (order_id, canonical_id, source, quantity, shipped, fulfilled, TS),
        )
    conn.commit()
    conn.close()
    return order_id


# ---------------------------------------------------------------------------
# 端到端主链：建单 → 双仓并行发货 → 交错收货 → delivered 恰一次
# ---------------------------------------------------------------------------

def test_cross_warehouse_end_to_end_parallel_ship_interleaved_receive(env):
    """一张单跨 alpha/beta：并行发货、A 先收、收齐 delivered 恰一次。

    这是 Task 1-10 串起来的验收主链，同时覆盖：
    - 并发状态：A 发/收时 B 未发 → 单停 approved 且 A 可收；
    - 并发状态：最后一行的发货才触发 shipped，恰一次；
    - 收齐 delivered 恰一次；
    - 数量与事务：双仓库存各按来源行扣减，互不串仓。
    """
    _add_user(env, 4, "store_mgr")
    _grant(env, 4, 3, "manager")
    _grant_full_approver(env)

    order_id = _build_cross_order(env)
    lines = _lines(env, order_id)
    assert {row["source_warehouse_code"] for row in lines.values()} == {
        "dc_alpha", "dc_beta",
    }
    assert _orders(env)[0]["status"] == "pending"

    # 审批：只有同时覆盖两个参与仓的用户才能整单批准（D5）。
    _login(env, 2, 1)
    env["client"].post(
        f"/store-ordering/orders/{order_id}/review",
        data={"decision": "approved", "note": ""}, follow_redirects=True,
    )
    assert _detail(env, order_id)["status"] == "approved"

    # alpha 并行发齐本仓两行。
    _ship(
        env, order_id, op_no="e2e-alpha-1",
        **{
            f"shipped_items[{lines[201]['id']}]": "2",
            f"shipped_items[{lines[206]['id']}]": "1",
        },
    )
    # beta 只发一半 → 整单仍 approved。
    _login(env, 3, 2)
    _ship(
        env, order_id, op_no="e2e-beta-1",
        **{f"shipped_items[{lines[202]['id']}]": "5"},
    )
    assert _detail(env, order_id)["status"] == "approved"

    # A 已发已收、B 尚未发齐：A 的收货合法，整单仍 approved。
    _login(env, 4, 3)
    _receive(env, order_id, int(lines[201]["id"]), 2, "e2e-recv-1")
    _receive(env, order_id, int(lines[206]["id"]), 1, "e2e-recv-2")
    _receive(env, order_id, int(lines[202]["id"]), 5, "e2e-recv-3")
    detail = _detail(env, order_id)
    assert detail["status"] == "approved"
    fulfilled = {int(i["canonical_id"]): i["fulfilled_quantity"] for i in detail["order_items"]}
    assert fulfilled == {201: 2.0, 206: 1.0, 202: 5.0}

    # beta 补齐剩余 5 件 → 全行发齐，approved → shipped 恰一次。
    _login(env, 3, 2)
    _ship(
        env, order_id, op_no="e2e-beta-2",
        **{f"shipped_items[{lines[202]['id']}]": "5"},
    )
    detail = _detail(env, order_id)
    assert detail["status"] == "shipped"

    # 门店收齐 beta 剩余 → delivered 恰一次。
    _login(env, 4, 3)
    _receive(env, order_id, int(lines[202]["id"]), 5, "e2e-recv-4")
    detail = _detail(env, order_id)
    assert detail["status"] == "delivered"

    conn = _master(env)
    transitions = conn.execute(
        "SELECT to_status, COUNT(*) AS n FROM store_order_status_history "
        "WHERE order_id=? GROUP BY to_status",
        (order_id,),
    ).fetchall()
    conn.close()
    by_status = {row["to_status"]: row["n"] for row in transitions}
    assert by_status["shipped"] == 1
    assert by_status["delivered"] == 1

    # 出库通知/送达通知按收件人各恰一条（不因多行或重试重复）。
    for event_type in (sop.EVENT_ORDER_SHIPPED, sop.EVENT_ORDER_DELIVERED):
        counts: dict[int, int] = {}
        for row in _notifications(env, event_type):
            counts[int(row["user_id"])] = counts.get(int(row["user_id"]), 0) + 1
        assert counts, event_type
        assert set(counts.values()) == {1}, (event_type, counts)

    # 库存按来源行扣减，互不串仓。
    assert _stock(env, "dc_alpha", 201) == 98.0
    assert _stock(env, "dc_alpha", 206) == 99.0
    assert _stock(env, "dc_beta", 202) == 90.0


# ---------------------------------------------------------------------------
# 数量与事务
# ---------------------------------------------------------------------------

def test_same_op_no_retry_deducts_once_through_route(env):
    """复用 op_no 重试只扣一次：路由层与纯逻辑层口径一致（spec §13 数量与事务）。"""
    _grant_full_approver(env)
    order_id = _build_cross_order(env)
    lines = _lines(env, order_id)
    _login(env, 2, 1)
    env["client"].post(
        f"/store-ordering/orders/{order_id}/review",
        data={"decision": "approved", "note": ""}, follow_redirects=True,
    )

    form = {"op_no": "retry-once", f"shipped_items[{lines[201]['id']}]": "2"}
    _ship(env, order_id, **form)
    assert _stock(env, "dc_alpha", 201) == 98.0
    _ship(env, order_id, **form)
    assert _stock(env, "dc_alpha", 201) == 98.0
    conn = _master(env)
    delivered = conn.execute("SELECT COUNT(*) FROM store_order_deliveries").fetchone()[0]
    conn.close()
    assert delivered == 1


def test_cancel_after_ship_commit_is_rejected(env):
    """取消与发货竞争：货已扣减/在途，取消必须失败，不产生「已取消在途单」。"""
    _grant_full_approver(env)
    order_id = _build_cross_order(env)
    lines = _lines(env, order_id)
    _login(env, 2, 1)
    env["client"].post(
        f"/store-ordering/orders/{order_id}/review",
        data={"decision": "approved", "note": ""}, follow_redirects=True,
    )
    _ship(env, order_id, op_no="cancel-race", **{f"shipped_items[{lines[201]['id']}]": "2"})

    resp = env["client"].post(
        f"/store-ordering/orders/{order_id}/cancel",
        data={"reason": "测试取消"}, follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "无法取消" in resp.data.decode()
    assert _detail(env, order_id)["status"] == "approved"


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

def test_routing_missing_and_exclusive_reject_whole_order(env):
    """缺路由 / 门店专属都是整单退回，并逐行列明品项（D6/D10）。"""
    _login(env, 1, 3)
    _add(env, 201, 1)
    _add(env, 205, 1)  # 无路由
    body = _submit(env).data.decode()
    assert "未配置履约仓" in body and "品项E" in body
    assert _orders(env) == []

    env["client"].post("/store-ordering/cart/clear", data={})
    _login(env, 1, 3)
    _add(env, 204, 1)  # is_store_exclusive
    body = _submit(env).data.decode()
    assert "门店专属" in body
    assert _orders(env) == []


def test_routing_duplicate_binding_fails_closed(env):
    """重复绑定（同一仓两行 active 绑定）不得猜首行，整单失败关闭。"""
    conn = env_builder.connect(env["paths"]["dc_alpha"])
    category_id = conn.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()[0]
    conn.execute(
        "INSERT INTO items (sku, name, category_id, quantity, unit, canonical_id, "
        "updated_at) VALUES ('DCA-A2', '品项A重复', ?, 5.0, '件', 201, ?)",
        (category_id, TS),
    )
    conn.commit()
    conn.close()

    _login(env, 1, 3)
    _add(env, 201, 1)
    body = _submit(env).data.decode()
    assert "无法解析履约行" in body
    assert "2 active fulfillment rows" in body
    assert _orders(env) == []


def test_routing_unit_conflict_fails_closed(env):
    """单位冲突（本地单位 != 主数据单位）不得静默换算，整单失败关闭。"""
    conn = env_builder.connect(env["paths"]["dc_alpha"])
    conn.execute("UPDATE items SET unit='kg' WHERE canonical_id=203")
    conn.commit()
    conn.close()

    _login(env, 1, 3)
    _add(env, 203, 1)
    body = _submit(env).data.decode()
    assert "无法解析履约行" in body
    assert "incomparable units" in body
    assert _orders(env) == []


def test_routing_change_does_not_rewrite_existing_order(env):
    """提交后改 item_routing 不影响已建单的冻结来源（D6）。"""
    order_id = _build_cross_order(env)
    before = {cid: row["source_warehouse_code"] for cid, row in _lines(env, order_id).items()}

    conn = _master(env)
    conn.execute(
        "UPDATE item_routing SET fulfillment_warehouse_code='dc_beta' WHERE canonical_id=201",
    )
    conn.commit()
    conn.close()

    after = {cid: row["source_warehouse_code"] for cid, row in _lines(env, order_id).items()}
    assert after == before == {201: "dc_alpha", 206: "dc_alpha", 202: "dc_beta"}


def test_routing_mirror_price_never_leaks(env):
    """研发镜像（beta 同 canonical 99.0）不得覆盖路由来源仓的价格（D3/§13 路由）。"""
    body = env["client"].get("/store-ordering/catalog").data.decode()
    assert 'data-canonical-id="201"' in body
    # 路由 = dc_alpha，价格取 9.9；镜像价 99.0 不出现在目录里。
    assert 'data-unit-price="9.9"' in body
    assert "99.0" not in body


def test_fulfillment_resolver_matrix(env):
    """解析器失败关闭矩阵（薄聚合，细节见 test_fulfillment_pure）。"""
    master = _master(env)
    try:
        for code, canonical_id, error in (
            ("dc_alpha", 999, fulfillment_pure.FulfillmentMissingError),
            ("dc_beta", 205, fulfillment_pure.FulfillmentMissingError),
        ):
            conn = env_builder.connect(env["paths"][code])
            try:
                with pytest.raises(error):
                    fulfillment_pure.resolve_fulfillment_item(
                        master, conn, canonical_id=canonical_id, warehouse_code=code,
                    )
            finally:
                conn.close()
    finally:
        master.close()


# ---------------------------------------------------------------------------
# 可见与动作
# ---------------------------------------------------------------------------

def test_b_only_approver_cannot_approve_cross_order_alone(env):
    """B-only 用户看得到参与单，但缺 A 仓审批权 → 403 且订单不动（D5）。"""
    order_id = _build_cross_order(env)
    _login(env, 3, 2)  # 仅 dc_beta manager
    board = env["client"].get("/store-ordering/review").data.decode()
    assert f"/store-ordering/orders/{order_id}" in board

    resp = env["client"].post(
        f"/store-ordering/orders/{order_id}/review",
        data={"decision": "approved", "note": ""},
    )
    assert resp.status_code == 403
    assert _detail(env, order_id)["status"] == "pending"


def test_ship_rejects_empty_form_and_foreign_lines_without_stock_change(env):
    """空表单/越仓行不得越权发货（spec §13 可见与动作，场景 g/c）。"""
    _grant_full_approver(env)
    order_id = _build_cross_order(env)
    lines = _lines(env, order_id)
    _login(env, 2, 1)
    env["client"].post(
        f"/store-ordering/orders/{order_id}/review",
        data={"decision": "approved", "note": ""}, follow_redirects=True,
    )

    # 空表单：shipped_items[] 存在但全空 → 拒绝，不全发。
    resp = _ship(env, order_id, op_no="empty", **{f"shipped_items[{lines[201]['id']}]": ""})
    assert "请至少填写一项发货数量" in resp.data.decode()
    assert _stock(env, "dc_alpha", 201) == 100.0
    assert _stock(env, "dc_beta", 202) == 100.0

    # 越仓行：alpha 会话提交 beta 来源行 → 拒绝且两仓库存不动。
    resp = _ship(
        env, order_id, op_no="foreign",
        **{f"shipped_items[{lines[201]['id']}]": "1",
           f"shipped_items[{lines[202]['id']}]": "1"},
    )
    assert "不属于" in resp.data.decode()
    assert _stock(env, "dc_alpha", 201) == 100.0
    assert _stock(env, "dc_beta", 202) == 100.0


# ---------------------------------------------------------------------------
# 迁移与发布
# ---------------------------------------------------------------------------

def test_migration_rerun_preserves_cross_warehouse_sources(env):
    """跨仓单建成后重跑迁移：A/B 来源原样保留（只回填历史空来源）。"""
    order_id = _build_cross_order(env)
    before = {cid: row["source_warehouse_code"] for cid, row in _lines(env, order_id).items()}
    assert set(before.values()) == {"dc_alpha", "dc_beta"}

    from migrations import migrate_multi_warehouse_fulfillment

    master = _master(env)
    warehouses = {code: env_builder.connect(path) for code, path in env["paths"].items()}
    try:
        result = migrate_multi_warehouse_fulfillment(master, warehouses)
        master.commit()
        for conn in warehouses.values():
            conn.commit()
    finally:
        for conn in warehouses.values():
            conn.close()
        master.close()

    assert result["backfilled_items"] == 0
    after = {cid: row["source_warehouse_code"] for cid, row in _lines(env, order_id).items()}
    assert after == before


def test_flag_off_keeps_legacy_identity(tmp_path, monkeypatch):
    """开关关闭 = 存量恒等：单仓选择器路径，未路由品仍可下单（不咨询路由）。"""
    env = env_builder.build(tmp_path, monkeypatch)  # 故意不 enable
    client = env["client"]

    client.post(
        "/store-ordering/cart/add",
        data={"dc": "dc_alpha", "canonical_id": 205, "quantity": "2", "unit": "件"},
    )
    resp = client.post(
        "/store-ordering/cart/submit",
        data={"expected_delivery_date": _tomorrow(), "note": ""},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    orders = _orders(env)
    assert len(orders) == 1
    assert orders[0]["dc_warehouse_code"] == "dc_alpha"
    line = _lines(env, int(orders[0]["id"]))[205]
    assert line["source_warehouse_code"] == "dc_alpha"


# ---------------------------------------------------------------------------
# 报表与补货
# ---------------------------------------------------------------------------

def test_report_grain_and_shipping_fee_do_not_sum_across_warehouses(env):
    """多仓筛选数量正确、订单不重复、运费不跨仓求和（D11/§10）。"""
    _insert_cross_order(env)

    conn = _master(env)
    try:
        report = sop.compute_store_order_report(
            conn, start_date=REPORT_DAY, end_date=REPORT_DAY,
        )
        beta = sop.compute_store_order_report(
            conn, start_date=REPORT_DAY, end_date=REPORT_DAY, dc_warehouse_code="dc_beta",
        )
        health_beta = hp.compute_fulfillment(
            conn, "dc_beta", datetime(2026, 10, 29, 12, 0, 0),
        )
    finally:
        conn.close()

    assert report["summary"]["order_count"] == 1
    assert report["summary"]["ordered_qty"] == 13.0
    # 运费是头级事实：全局只计一次。
    assert report["summary"]["shipping_fee"] == 10.0
    by_dc = {row["dc_code"]: row for row in report["by_dc"]}
    assert set(by_dc) == {"dc_alpha", "dc_beta"}
    assert by_dc["dc_alpha"]["ordered_qty"] == 3.0
    assert by_dc["dc_beta"]["ordered_qty"] == 10.0
    # 仓维度展示全单值，各行相加 ≠ 全局（这正是禁止 SUM 跨仓的原因）。
    assert by_dc["dc_alpha"]["shipping_fee"] == 10.0
    assert by_dc["dc_beta"]["shipping_fee"] == 10.0

    # 单仓筛选：只取该仓明细，订单数仍为 1（不因多行翻倍）。
    assert beta["summary"]["order_count"] == 1
    assert beta["summary"]["ordered_qty"] == 10.0

    # 非主仓健康指标不漏：beta 是「他仓」，仍看到这张跨仓单。
    assert health_beta["backlog"] == 1


def test_replenishment_pulls_only_the_routed_warehouse(env):
    """采购/补货只拉动路由来源仓（薄聚合，细节见 test_replenishment_routing）。"""
    from blueprints import replenishment_data as rd

    conn = _master(env)
    try:
        routing = rd.load_routing_map()
    finally:
        conn.close()
    assert routing[201] == "dc_alpha"
    assert routing[202] == "dc_beta"
    # 未路由的 205 不在拉动集合里。
    assert 205 not in routing


# ---------------------------------------------------------------------------
# 前端与通知
# ---------------------------------------------------------------------------

def _draft_scope(body: str) -> str:
    marker = "var DRAFT_SCOPE = "
    start = body.index(marker) + len(marker)
    end = body.index(";", start)
    return body[start:end].strip().strip('"')


def test_draft_key_isolated_by_user_and_store(env):
    """双门店/双用户草稿隔离：键 = (user_id, store_code)，v1 DC 键不再复用。"""
    body = env["client"].get("/store-ordering/catalog").data.decode()
    assert "dc.catalogDraft.v2:" in body
    assert _draft_scope(body) == "1:store_one"

    _add_user(env, 5, "store_two_staff")
    _grant(env, 5, 3, "staff")
    _login(env, 5, 3)
    other_user = env["client"].get("/store-ordering/catalog").data.decode()
    assert _draft_scope(other_user) == "5:store_one"
    assert _draft_scope(other_user) != _draft_scope(body)


def test_draft_restore_is_idempotent_and_never_adds_to_cart(env):
    """回退不重复加购：恢复草稿只重绘输入，不触发 /cart/add。"""
    body = env["client"].get("/store-ordering/catalog").data.decode()
    start = body.index("function restoreDraft()")
    end = body.index("function setActiveCmPill", start)
    block = body[start:end]

    # 幂等：先清空所有可见卡片，再按草稿重绘（不会叠加上一次的选择）。
    assert block.index("clearCardSelection") < block.index("applyCardSelection")
    assert "fetch(" not in block
    assert "/cart/add" not in block


def test_notification_direction_matrix(env):
    """收件方向矩阵逐事件：提交→参与仓审批人；出库/送达→门店侧。"""
    order_id = _build_cross_order(env)
    detail = _detail(env, order_id)
    conn = _master(env)
    try:
        sop.notify_order_event(conn, sop.EVENT_ORDER_SUBMITTED, detail, None)
        conn.commit()
    finally:
        conn.close()
    submitted = {int(r["user_id"]) for r in _notifications(env, sop.EVENT_ORDER_SUBMITTED)}
    assert submitted == {2, 3}  # alpha/beta 各自审批人（按权限解析，非角色名）

    conn = _master(env)
    try:
        sop.notify_order_event(conn, sop.EVENT_ORDER_SHIPPED, detail, None)
        conn.commit()
    finally:
        conn.close()
    shipped = {int(r["user_id"]) for r in _notifications(env, sop.EVENT_ORDER_SHIPPED)}
    assert shipped == {1}  # 门店侧 order.receive 持有人


def test_no_full_approver_is_diagnosable_and_deduped(env):
    """无全权审批人 → 平台管理员恰一条可诊断告警，重复渲染不重复告警。"""
    order_id = _build_cross_order(env)
    _add_user(env, 6, "platform_admin", is_admin=1)
    detail = _detail(env, order_id)
    participants = [p["code"] for p in detail["participant_warehouses"]]
    assert sorted(participants) == ["dc_alpha", "dc_beta"]

    conn = _master(env)
    try:
        assert sop.has_full_approver(conn, participants) is False
        first = sop.alert_no_full_approver(conn, detail)
        conn.commit()
        second = sop.alert_no_full_approver(conn, detail)
        conn.commit()
    finally:
        conn.close()
    assert first == 1
    assert second == 0
    alerts = _notifications(env, sop.NO_APPROVER_ALERT_EVENT)
    assert {int(r["user_id"]) for r in alerts} == {6}
