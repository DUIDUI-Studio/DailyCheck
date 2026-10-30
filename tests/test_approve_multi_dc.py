"""Task 8：审批/拒绝/取消守卫 + 订单板视角分离与仓库筛选。

验收点（对齐实施计划 Task 8 Step 1 (a)-(h)、设计 §6/§8/§9/D5/D7/D12）：
1. 只对部分参与仓有审批权的 manager 批准跨仓单 → 403 + 缺失仓提示，订单不动
2. 对全部参与仓有审批权 → 整单一次迁移（一条 approved 历史）
3. 平台管理员绕过参与仓权限
4. 拒绝同守卫；取消走 Task 6 已接入的 can_act_on_order（本文件只做一致性验证）
5. admin `?warehouse=` 参与仓筛选生效；DC 用户该参数被忽略
6. 跨仓单同时出现在 A、B 两仓订单板；详情页只给本仓行发货操作面
7. 页签计数用参与仓 EXISTS 口径，不因多行翻倍
8. 无全权审批人 → 平台管理员恰一条站内告警（重复渲染不重复告警）
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from tests import multi_warehouse_env as env_builder

TS = "2026-10-29 12:00:00"

STORE = (1, 3)
ALPHA_MGR = (2, 1)
BETA_MGR = (3, 2)
PLATFORM_ADMIN = (4, 3)

# 两张 alpha 行 + 一张 beta 行：头仓 = dc_alpha（D8 行数最多）。
# 两行 alpha 也让「页签计数 JOIN 翻倍」这类缺陷无处可藏。
CROSS_LINES = [(201, 1), (206, 1), (202, 5)]
ALPHA_ONLY = [(201, 1)]
BETA_ONLY = [(202, 5)]

ALERT_EVENT = "store_order_no_approver_alert"


@pytest.fixture
def env(tmp_path, monkeypatch):
    env = env_builder.build(tmp_path, monkeypatch)
    env_builder.enable(monkeypatch)
    master = env_builder.connect(env["master_path"])
    master.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (4, 'platform_admin', 'x', 1, ?)",
        (TS,),
    )
    master.commit()
    master.close()
    return env


def _login(env, who: tuple[int, int]) -> None:
    with env["client"].session_transaction() as session:
        session["user_id"] = who[0]
        session["warehouse_id"] = who[1]


def _create_order(env, lines: list[tuple[int, float]]) -> tuple[dict, dict[int, dict]]:
    """建一张订单，返回 (store_orders 行, {canonical_id: store_order_items 行})。"""
    client = env["client"]
    _login(env, STORE)
    for canonical_id, qty in lines:
        client.post(
            "/store-ordering/cart/add",
            data={"canonical_id": canonical_id, "quantity": str(qty), "unit": "件"},
        )
    tomorrow = (datetime.now().date() + timedelta(days=1)).isoformat()
    client.post(
        "/store-ordering/cart/submit",
        data={"expected_delivery_date": tomorrow, "note": ""},
        follow_redirects=True,
    )
    master = env_builder.connect(env["master_path"])
    order = dict(master.execute(
        "SELECT * FROM store_orders ORDER BY id DESC LIMIT 1"
    ).fetchone())
    items = {
        int(row["canonical_id"]): dict(row)
        for row in master.execute(
            "SELECT * FROM store_order_items WHERE order_id=?", (order["id"],)
        ).fetchall()
    }
    master.close()
    return order, items


def _status(env, order_id: int) -> str:
    master = env_builder.connect(env["master_path"])
    status = master.execute(
        "SELECT status FROM store_orders WHERE id=?", (order_id,)
    ).fetchone()["status"]
    master.close()
    return str(status)


def _grant(env, user_id: int, warehouse_id: int, role: str = "manager") -> None:
    master = env_builder.connect(env["master_path"])
    master.execute(
        "INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (?, ?, ?)",
        (user_id, warehouse_id, role),
    )
    master.commit()
    master.close()


def _history(env, order_id: int, to_status: str) -> list[dict]:
    master = env_builder.connect(env["master_path"])
    rows = [
        dict(r) for r in master.execute(
            "SELECT * FROM store_order_status_history WHERE order_id=? AND to_status=?",
            (order_id, to_status),
        ).fetchall()
    ]
    master.close()
    return rows


def _alerts(env, user_id: int) -> list[dict]:
    master = env_builder.connect(env["master_path"])
    rows = [
        dict(r) for r in master.execute(
            "SELECT * FROM notifications WHERE user_id=? AND event_type=?",
            (user_id, ALERT_EVENT),
        ).fetchall()
    ]
    master.close()
    return rows


# ---------------------------------------------------------------------------
# (a)-(c) 审批守卫
# ---------------------------------------------------------------------------

def test_partial_approver_gets_403_with_missing_warehouse(env):
    order, _ = _create_order(env, CROSS_LINES)
    _login(env, ALPHA_MGR)
    resp = env["client"].post(
        f"/store-ordering/orders/{order['id']}/review",
        data={"decision": "approved", "note": ""},
    )
    assert resp.status_code == 403
    assert b"dc_beta" in resp.data, "403 页面必须列出缺失审批权的参与仓"
    assert _status(env, order["id"]) == "pending"


def test_full_approver_moves_order_once(env):
    order, _ = _create_order(env, CROSS_LINES)
    _grant(env, ALPHA_MGR[0], BETA_MGR[1])  # alpha manager also manages beta

    _login(env, ALPHA_MGR)
    resp = env["client"].post(
        f"/store-ordering/orders/{order['id']}/review",
        data={"decision": "approved", "note": "ok"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert _status(env, order["id"]) == "approved"
    assert len(_history(env, order["id"], "approved")) == 1


def test_platform_admin_bypasses_participant_permissions(env):
    order, _ = _create_order(env, CROSS_LINES)
    _login(env, PLATFORM_ADMIN)
    resp = env["client"].post(
        f"/store-ordering/orders/{order['id']}/review",
        data={"decision": "approved", "note": "admin"},
    )
    assert resp.status_code == 302
    assert _status(env, order["id"]) == "approved"


# ---------------------------------------------------------------------------
# (d) 拒绝同守卫 + 取消一致性（Task 6 已接入，不重做）
# ---------------------------------------------------------------------------

def test_partial_approver_cannot_reject(env):
    order, _ = _create_order(env, CROSS_LINES)
    _login(env, ALPHA_MGR)
    resp = env["client"].post(
        f"/store-ordering/orders/{order['id']}/review",
        data={"decision": "rejected", "note": "不行"},
    )
    assert resp.status_code == 403
    assert b"dc_beta" in resp.data
    assert _status(env, order["id"]) == "pending"

    _grant(env, ALPHA_MGR[0], BETA_MGR[1])
    resp = env["client"].post(
        f"/store-ordering/orders/{order['id']}/review",
        data={"decision": "rejected", "note": "不行"},
    )
    assert resp.status_code == 302
    assert _status(env, order["id"]) == "rejected"


def test_cancel_of_approved_cross_order_needs_every_participant(env):
    order, _ = _create_order(env, CROSS_LINES)
    _login(env, PLATFORM_ADMIN)
    env["client"].post(
        f"/store-ordering/orders/{order['id']}/review",
        data={"decision": "approved", "note": "ok"},
    )
    assert _status(env, order["id"]) == "approved"

    # 只有 alpha 权限：取消被 can_act_on_order 拦下（Task 6 已接入的同一守卫）。
    _login(env, ALPHA_MGR)
    body = env["client"].post(
        f"/store-ordering/orders/{order['id']}/cancel",
        data={"reason": "库存调整"},
        follow_redirects=True,
    ).data.decode()
    assert "dc_beta" in body
    assert _status(env, order["id"]) == "approved"

    _grant(env, ALPHA_MGR[0], BETA_MGR[1])
    resp = env["client"].post(
        f"/store-ordering/orders/{order['id']}/cancel",
        data={"reason": "库存调整"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert _status(env, order["id"]) == "cancelled"


# ---------------------------------------------------------------------------
# (e) admin 参与仓筛选；DC 用户参数被忽略（对齐既有 ?dc= 守卫测试模式）
# ---------------------------------------------------------------------------

def test_admin_can_filter_board_by_participating_warehouse(env):
    cross, _ = _create_order(env, CROSS_LINES)
    alpha_only, _ = _create_order(env, ALPHA_ONLY)
    beta_only, _ = _create_order(env, BETA_ONLY)

    _login(env, PLATFORM_ADMIN)
    body = env["client"].get(
        "/store-ordering/admin/orders?warehouse=dc_beta"
    ).data.decode()
    assert beta_only["order_no"] in body
    assert cross["order_no"] in body, "跨仓单在参与仓 B 的筛选下必须出现"
    assert alpha_only["order_no"] not in body


def test_dc_user_warehouse_param_is_ignored(env):
    cross, _ = _create_order(env, CROSS_LINES)
    alpha_only, _ = _create_order(env, ALPHA_ONLY)
    beta_only, _ = _create_order(env, BETA_ONLY)

    _login(env, ALPHA_MGR)
    body = env["client"].get(
        "/store-ordering/review?warehouse=dc_beta"
    ).data.decode()
    assert cross["order_no"] in body
    assert alpha_only["order_no"] in body
    assert beta_only["order_no"] not in body, "DC 用户不能通过 ?warehouse= 枚举别家仓的订单"


# ---------------------------------------------------------------------------
# (f)(g) 双仓订单板 / 本仓发货操作面 / 页签计数不翻倍
# ---------------------------------------------------------------------------

def test_cross_order_appears_on_both_boards_with_own_rows_only(env):
    order, items = _create_order(env, CROSS_LINES)
    # 先由平台管理员整单批准：发货操作面只在 approved 状态出现。
    _login(env, PLATFORM_ADMIN)
    env["client"].post(
        f"/store-ordering/orders/{order['id']}/review",
        data={"decision": "approved", "note": "ok"},
    )
    assert _status(env, order["id"]) == "approved"

    _login(env, ALPHA_MGR)
    body = env["client"].get("/store-ordering/review").data.decode()
    assert order["order_no"] in body

    detail = env["client"].get(
        f"/store-ordering/orders/{order['id']}"
    ).data.decode()
    assert f"shipped_items[{items[201]['id']}]" in detail
    assert f"shipped_items[{items[206]['id']}]" in detail
    assert f"shipped_items[{items[202]['id']}]" not in detail, (
        "A 仓不应渲染 B 仓行的发货输入框"
    )
    # 跨仓进度徽标：本仓 2 行 / 全单 3 行
    assert "本仓 2/2 待发" in detail
    assert "全单 3/3" in detail

    _login(env, BETA_MGR)
    body = env["client"].get("/store-ordering/review").data.decode()
    assert order["order_no"] in body

    detail = env["client"].get(
        f"/store-ordering/orders/{order['id']}"
    ).data.decode()
    assert f"shipped_items[{items[202]['id']}]" in detail
    assert f"shipped_items[{items[201]['id']}]" not in detail
    assert "本仓 1/1 待发" in detail
    assert "全单 3/3" in detail


def test_tab_counts_use_participation_without_doubling(env):
    _create_order(env, CROSS_LINES)  # 2 alpha lines + 1 beta line

    _login(env, ALPHA_MGR)
    body = env["client"].get("/store-ordering/review").data.decode()
    assert '待审批<span class="n">1</span>' in body, "两张 alpha 行不得让页签计数翻倍"

    _login(env, BETA_MGR)
    body = env["client"].get("/store-ordering/review").data.decode()
    assert '待审批<span class="n">1</span>' in body


# ---------------------------------------------------------------------------
# (h) 无全权审批人 → 平台管理员恰一条告警
# ---------------------------------------------------------------------------

def test_no_full_approver_alerts_platform_admin_exactly_once(env):
    order, _ = _create_order(env, CROSS_LINES)

    _login(env, ALPHA_MGR)
    body = env["client"].get("/store-ordering/review").data.decode()
    assert "无全权审批人" in body, "订单板必须给出无人可批的徽标"
    assert len(_alerts(env, PLATFORM_ADMIN[0])) == 1

    # 另一参与仓的用户再打开一次订单板：仍只有一条（幂等，不重复告警）。
    _login(env, BETA_MGR)
    env["client"].get("/store-ordering/review")
    assert len(_alerts(env, PLATFORM_ADMIN[0])) == 1


# ---------------------------------------------------------------------------
# (4) 深链守卫：非参与仓用户直接访问详情/付款凭证 → 403
# ---------------------------------------------------------------------------

def test_foreign_warehouse_deep_link_is_forbidden(env):
    order, _ = _create_order(env, ALPHA_ONLY)
    _login(env, BETA_MGR)
    assert env["client"].get(
        f"/store-ordering/orders/{order['id']}"
    ).status_code == 403
    assert env["client"].get(
        f"/store-ordering/orders/{order['id']}/payment-proofs/1"
    ).status_code == 403
