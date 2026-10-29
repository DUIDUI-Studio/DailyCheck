"""逐事件收件矩阵（多仓履约 Task 10 / spec §11）。

收件人按**有效权限**解析，而不是硬编码角色名：
  - 参与仓侧 → 持有 ``order.approve`` 的在职非平台管理员用户；
  - 门店侧 → 下单人 + 持有 ``order.receive`` 的在职用户。

覆盖：提交、批准/拒绝、部分收货（按本批来源仓）、全发齐、取消、调货方向。
"""
from __future__ import annotations

import sqlite3

import pytest

from blueprints import store_ordering_pure as sop

TS = "2026-10-29 12:00:00"

# 用户 id 约定
ADMIN = 1
STORE_STAFF = 2          # 下单人，store_one/staff（order.receive）
STORE_MGR = 3            # store_one/manager（order.approve + order.receive）
ALPHA_MGR = 4            # dc_alpha/manager（order.approve）
BETA_MGR = 5             # dc_beta/manager（order.approve）
ALPHA_VIEWER = 6         # dc_alpha/viewer（无 order.approve）
BETA_CUSTOM = 7          # dc_beta 自定义角色（非内置，持 order.approve）
ALPHA_STAFF = 8          # dc_alpha/staff（无 order.approve）
STORE_CUSTOM = 9         # store_one 自定义角色（持 order.receive）
STORE_VIEWER = 10        # store_one/viewer（无 order.receive）
BOTH_MGR = 11            # dc_alpha + dc_beta manager（去重样本）


def _seed(master_path) -> None:
    conn = sqlite3.connect(str(master_path))
    conn.row_factory = sqlite3.Row
    for uid, name, is_admin in (
        (ADMIN, "admin", 1),
        (STORE_STAFF, "store_staff", 0),
        (STORE_MGR, "store_mgr", 0),
        (ALPHA_MGR, "alpha_mgr", 0),
        (BETA_MGR, "beta_mgr", 0),
        (ALPHA_VIEWER, "alpha_viewer", 0),
        (BETA_CUSTOM, "beta_custom", 0),
        (ALPHA_STAFF, "alpha_staff", 0),
        (STORE_CUSTOM, "store_custom", 0),
        (STORE_VIEWER, "store_viewer", 0),
        (BOTH_MGR, "both_mgr", 0),
    ):
        conn.execute(
            "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
            "VALUES (?, ?, 'x', ?, ?)",
            (uid, name, is_admin, TS),
        )
    for wid, code, kind in (
        (1, "dc_alpha", "distribution_center"),
        (2, "dc_beta", "distribution_center"),
        (3, "store_one", "storefront"),
    ):
        conn.execute(
            "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (wid, code, code, f"/tmp/{code}.db", kind, TS),
        )
    for uid, wid, role in (
        (STORE_STAFF, 3, "staff"),
        (STORE_MGR, 3, "manager"),
        (ALPHA_MGR, 1, "manager"),
        (BETA_MGR, 2, "manager"),
        (ALPHA_VIEWER, 1, "viewer"),
        (ALPHA_STAFF, 1, "staff"),
        (STORE_VIEWER, 3, "viewer"),
        (BOTH_MGR, 1, "manager"),
        (BOTH_MGR, 2, "manager"),
        (BETA_CUSTOM, 2, "beta_approver"),     # 自定义角色，非角色名硬编码
        (STORE_CUSTOM, 3, "store_receiver"),
    ):
        conn.execute(
            "INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (?, ?, ?)",
            (uid, wid, role),
        )
    # 自定义角色：必须显式授予权限才收件（内置角色包在 init_master_db 里已播种）
    for code, name, permission in (
        ("beta_approver", "外部审批人", "order.approve"),
        ("store_receiver", "外部收货人", "order.receive"),
    ):
        cur = conn.execute(
            "INSERT INTO roles (code, tenant_id, name, is_builtin, rank, created_at) "
            "VALUES (?, 1, ?, 0, NULL, ?)",
            (code, name, TS),
        )
        conn.execute(
            "INSERT INTO role_permissions (role_id, permission_code) VALUES (?, ?)",
            (int(cur.lastrowid), permission),
        )
    # 被撤权的内置角色实例：把 viewer 的订单权限清空（原本也没有，显式化）
    conn.commit()
    conn.close()


@pytest.fixture
def matrix_env(tmp_path, monkeypatch):
    import config as config_module
    import db as db_module

    master_path = tmp_path / "master.db"
    wh_dir = tmp_path / "warehouses"
    wh_dir.mkdir()
    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_dir)
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", wh_dir)
    db_module.init_master_db()
    _seed(master_path)
    conn = sqlite3.connect(str(master_path))
    conn.row_factory = sqlite3.Row
    yield {"conn": conn, "path": master_path}
    conn.close()


def _order(**overrides) -> dict:
    order = {
        "id": 1,
        "order_no": "SO-20261029-0001",
        "flow_type": sop.FLOW_STORE_ORDER,
        "store_warehouse_code": "store_one",
        "dc_warehouse_code": "dc_alpha",
        "requested_by": STORE_STAFF,
        "status": sop.ORDER_STATUS_PENDING,
        # 跨仓单：alpha 一行、beta 一行
        "order_items": [
            {"id": 11, "source_warehouse_code": "dc_alpha"},
            {"id": 12, "source_warehouse_code": "dc_beta"},
        ],
        "receipts": [],
        "deliveries": [],
        "status_history": [],
    }
    order.update(overrides)
    return order


def _notified(conn, event_type: str) -> set[int]:
    return {
        int(r["user_id"])
        for r in conn.execute(
            "SELECT user_id FROM notifications WHERE event_type=?", (event_type,)
        ).fetchall()
    }


def _notify(env, event_type: str, order: dict, actor=None) -> int:
    return sop.notify_order_event(env["conn"], event_type, order, actor_user_id=actor)


# ─────────────────────────────────────────────────────────────────────
# 提交 → 各参与仓 order.approve 持有人（+ 平台管理员）
# ─────────────────────────────────────────────────────────────────────

def test_submitted_reaches_approvers_of_every_participant(matrix_env):
    count = _notify(matrix_env, sop.EVENT_ORDER_SUBMITTED, _order())
    notified = _notified(matrix_env["conn"], sop.EVENT_ORDER_SUBMITTED)
    # alpha: 4/11；beta: 5/7/11；admin: 1；权限不足的 6/8 不收，门店不参与审批
    assert notified == {ADMIN, ALPHA_MGR, BETA_MGR, BETA_CUSTOM, BOTH_MGR}
    assert count == len(notified)
    # 同时在两个参与仓持权的人只收到一条
    rows = matrix_env["conn"].execute(
        "SELECT COUNT(*) FROM notifications WHERE user_id=?", (BOTH_MGR,)
    ).fetchone()[0]
    assert rows == 1


def test_submitted_excludes_the_actor(matrix_env):
    _notify(matrix_env, sop.EVENT_ORDER_SUBMITTED, _order(), actor=ALPHA_MGR)
    assert ALPHA_MGR not in _notified(matrix_env["conn"], sop.EVENT_ORDER_SUBMITTED)


def test_revoked_or_viewer_roles_are_not_notified(matrix_env):
    _notify(matrix_env, sop.EVENT_ORDER_SUBMITTED, _order())
    notified = _notified(matrix_env["conn"], sop.EVENT_ORDER_SUBMITTED)
    assert ALPHA_VIEWER not in notified
    assert ALPHA_STAFF not in notified


def test_custom_role_holder_is_notified_by_permission_not_role_name(matrix_env):
    _notify(matrix_env, sop.EVENT_ORDER_SUBMITTED, _order())
    assert BETA_CUSTOM in _notified(matrix_env["conn"], sop.EVENT_ORDER_SUBMITTED)


# ─────────────────────────────────────────────────────────────────────
# 批准 / 拒绝 → 门店侧（方向维持现状）
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "event_type", [sop.EVENT_ORDER_APPROVED, sop.EVENT_ORDER_REJECTED]
)
def test_approved_and_rejected_go_to_the_store_side(matrix_env, event_type):
    _notify(matrix_env, event_type, _order())
    notified = _notified(matrix_env["conn"], event_type)
    # 下单人 2 + 门店 order.receive 持有人 3/9；viewer 10、平台管理员、DC 侧都不收
    assert notified == {STORE_STAFF, STORE_MGR, STORE_CUSTOM}


# ─────────────────────────────────────────────────────────────────────
# 部分收货 → 门店 + 该批来源仓
# ─────────────────────────────────────────────────────────────────────

def test_partial_receipt_notifies_store_and_the_batch_source_only(matrix_env):
    order = _order(
        receipts=[
            {"order_item_id": 12, "created_at": TS, "quantity": 1.0},
        ],
        status=sop.ORDER_STATUS_SHIPPED,
    )
    _notify(matrix_env, sop.EVENT_ORDER_RECEIVED, order)
    notified = _notified(matrix_env["conn"], sop.EVENT_ORDER_RECEIVED)
    # 门店侧 2/3/9 + 本批来源 dc_beta 的审批人 5/7/11 + 平台管理员 1
    assert notified == {
        ADMIN, STORE_STAFF, STORE_MGR, STORE_CUSTOM,
        BETA_MGR, BETA_CUSTOM, BOTH_MGR,
    }
    # 非本批来源仓（dc_alpha 独有审批人）不收
    assert ALPHA_MGR not in notified


def test_partial_receipt_without_receipt_rows_falls_back_to_all_participants(
    matrix_env,
):
    order = _order(status=sop.ORDER_STATUS_SHIPPED)
    _notify(matrix_env, sop.EVENT_ORDER_RECEIVED, order)
    notified = _notified(matrix_env["conn"], sop.EVENT_ORDER_RECEIVED)
    assert ALPHA_MGR in notified
    assert BETA_MGR in notified
    assert STORE_MGR in notified


# ─────────────────────────────────────────────────────────────────────
# 全发齐 / 送达 → 门店
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "event_type", [sop.EVENT_ORDER_SHIPPED, sop.EVENT_ORDER_DELIVERED]
)
def test_shipped_and_delivered_go_to_the_store_only(matrix_env, event_type):
    _notify(matrix_env, event_type, _order())
    assert _notified(matrix_env["conn"], event_type) == {
        STORE_STAFF, STORE_MGR, STORE_CUSTOM,
    }


def test_shipped_notification_written_once_per_user(matrix_env):
    count = _notify(matrix_env, sop.EVENT_ORDER_SHIPPED, _order())
    assert count == 3
    duplicates = matrix_env["conn"].execute(
        "SELECT user_id, COUNT(*) AS n FROM notifications "
        "WHERE event_type=? GROUP BY user_id HAVING n > 1",
        (sop.EVENT_ORDER_SHIPPED,),
    ).fetchall()
    assert duplicates == []


# ─────────────────────────────────────────────────────────────────────
# 取消 → 参与仓审批人 + 门店
# ─────────────────────────────────────────────────────────────────────

def test_cancelled_notifies_participant_approvers_and_store(matrix_env):
    _notify(matrix_env, sop.EVENT_ORDER_CANCELLED, _order(), actor=STORE_STAFF)
    notified = _notified(matrix_env["conn"], sop.EVENT_ORDER_CANCELLED)
    assert notified == {
        ADMIN, ALPHA_MGR, BETA_MGR, BETA_CUSTOM, BOTH_MGR,
        STORE_MGR, STORE_CUSTOM,
    }
    assert STORE_STAFF not in notified  # 取消人自己不收


# ─────────────────────────────────────────────────────────────────────
# 调货事件方向维持现状
# ─────────────────────────────────────────────────────────────────────

def _transfer_order(**overrides) -> dict:
    order = _order(
        flow_type=sop.FLOW_DC_TRANSFER,
        dc_warehouse_code="dc_alpha",
        store_warehouse_code="store_one",
        order_items=[{"id": 21, "source_warehouse_code": "dc_alpha"}],
    )
    order.update(overrides)
    return order


def test_transfer_created_notifies_the_destination_store(matrix_env):
    _notify(matrix_env, sop.EVENT_TRANSFER_CREATED, _transfer_order())
    assert _notified(matrix_env["conn"], sop.EVENT_TRANSFER_CREATED) == {
        STORE_STAFF, STORE_MGR, STORE_CUSTOM,
    }


def test_transfer_partial_receipt_stays_on_the_sending_warehouse(matrix_env):
    """调货收货方向维持现状：只通知发货仓侧，不扩大到门店。"""
    order = _transfer_order(
        status=sop.ORDER_STATUS_SHIPPED,
        receipts=[{"order_item_id": 21, "created_at": TS, "quantity": 1.0}],
    )
    _notify(matrix_env, sop.EVENT_ORDER_RECEIVED, order, actor=STORE_STAFF)
    assert _notified(matrix_env["conn"], sop.EVENT_ORDER_RECEIVED) == {
        ADMIN, ALPHA_MGR, BOTH_MGR,
    }
