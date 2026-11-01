"""总仓调货（DC → 门店调拨）测试。

覆盖三层：
  1. 纯逻辑：建单跳过审批、跨仓校验、明细合并、部分发货/部分收货状态派生；
  2. 库存与报表：调货流水文案、报表排除调货单、发货后禁止取消；
  3. 路由与权限：DC manager 可建单、DC staff / 分仓拒绝、分仓可看待接收列表。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from blueprints import dc_transfer_pure as dtp
from blueprints import store_ordering_pure as sop
from db import init_master_db, init_warehouse_db


@pytest.fixture
def transfer_env(tmp_path, monkeypatch):
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
    m = sqlite3.connect(master_path)
    m.row_factory = sqlite3.Row
    # 1 平台管理员 / 2 总仓经理 / 3 总仓 staff / 4 分仓经理
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (1, 'admin', 'x', 1, ?)", (ts,))
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (2, 'dc_mgr', 'x', 0, ?)", (ts,))
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (3, 'dc_staff', 'x', 0, ?)", (ts,))
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (4, 'store_mgr', 'x', 0, ?)", (ts,))
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) "
        "VALUES (1, 'dc_test', '测试总仓', ?, 'distribution_center', ?)",
        (str(dc_path), ts))
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) "
        "VALUES (2, 'store_test', '测试分仓', ?, 'storefront', ?)",
        (str(store_path), ts))
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (2, 1, 'manager')")
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (3, 1, 'staff')")
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (4, 2, 'manager')")
    m.execute(
        "INSERT INTO canonical_categories (code, name, description, created_at, updated_at) "
        "VALUES ('PACKAGING', '包材', '', ?, ?)", (ts, ts))
    for cid, sku, name in ((101, 'IC-000101', '调货品A'), (102, 'IC-000102', '调货品B')):
        m.execute(
            """INSERT INTO canonical_items
               (id, canonical_sku, name, category_code, unit, gram_per_unit, aux_unit,
                aux_rate, status, created_from, created_at, updated_at)
               VALUES (?, ?, ?, 'PACKAGING', '件', 0, NULL, 0, 'active', 'rd_manual', ?, ?)""",
            (cid, sku, name, ts, ts))
    m.commit()
    m.close()

    dc = sqlite3.connect(dc_path)
    dc.row_factory = sqlite3.Row
    cat_id = dc.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()["id"]
    for sku, name, qty, cid in (
        ("DC-A", "调货品A", 100.0, 101),
        ("DC-B", "调货品B", 5.0, 102),
    ):
        dc.execute(
            "INSERT INTO items (sku, name, category_id, quantity, unit, canonical_id, updated_at) "
            "VALUES (?, ?, ?, ?, '件', ?, ?)",
            (sku, name, cat_id, qty, cid, ts))
    dc.commit()
    dc.close()

    store = sqlite3.connect(store_path)
    store.row_factory = sqlite3.Row
    cat_id = store.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()["id"]
    store.execute(
        "INSERT INTO items (sku, name, category_id, quantity, unit, canonical_id, updated_at) "
        "VALUES ('ST-A', '调货品A', ?, 0, '件', 101, ?)", (cat_id, ts))
    store.commit()
    store.close()

    master = sqlite3.connect(master_path)
    master.row_factory = sqlite3.Row
    yield {
        "master": master,
        "master_path": master_path,
        "dc_path": dc_path,
        "store_path": store_path,
    }
    master.close()


def _create(env, items=None):
    return dtp.create_transfer_order(
        env["master"],
        from_warehouse_code="dc_test",
        to_warehouse_code="store_test",
        items=items or [{"canonical_id": 101, "quantity": 10}],
        actor_id=2,
        note="测试调货",
    )


# ---------------------------------------------------------------------------
# 建单
# ---------------------------------------------------------------------------

def test_create_transfer_skips_pending_approval(transfer_env):
    order = _create(transfer_env)
    assert order["flow_type"] == sop.FLOW_DC_TRANSFER
    assert order["status"] == sop.ORDER_STATUS_APPROVED, "调货单不得停在 pending"
    assert order["order_no"].startswith("DT-")
    assert order["shipping_fee"] == 0
    assert order["dc_warehouse_code"] == "dc_test"
    assert order["store_warehouse_code"] == "store_test"
    assert order["approved_by"] == 2
    # 状态历史第一条：None -> approved，备注标注免审批
    assert order["status_history"][-1]["from_status"] is None
    assert order["status_history"][-1]["to_status"] == sop.ORDER_STATUS_APPROVED


def test_create_transfer_freezes_line_source_to_the_head_warehouse(transfer_env):
    """多仓履约 Task 10：调货建单的明细行来源仓 = 头仓（发货仓）。"""
    order = _create(transfer_env)
    assert len(order["order_items"]) == 1
    assert order["order_items"][0]["source_warehouse_code"] == "dc_test"


def test_create_transfer_merges_same_canonical(transfer_env):
    order = _create(
        transfer_env,
        items=[
            {"canonical_id": 101, "quantity": 3},
            {"canonical_id": 101, "quantity": 2},
        ],
    )
    assert len(order["order_items"]) == 1
    assert order["order_items"][0]["quantity"] == 5


def test_create_transfer_rejects_same_warehouse(transfer_env):
    with pytest.raises(ValueError, match="不能相同"):
        dtp.create_transfer_order(
            transfer_env["master"],
            from_warehouse_code="dc_test",
            to_warehouse_code="dc_test",
            items=[{"canonical_id": 101, "quantity": 1}],
            actor_id=2,
        )


def test_create_transfer_rejects_reversed_direction(transfer_env):
    """分仓不能作为发货仓：调货只能由总仓发起。"""
    with pytest.raises(ValueError, match="不是distribution_center"):
        dtp.create_transfer_order(
            transfer_env["master"],
            from_warehouse_code="store_test",
            to_warehouse_code="dc_test",
            items=[{"canonical_id": 101, "quantity": 1}],
            actor_id=4,
        )


def test_create_transfer_rejects_unknown_item(transfer_env):
    with pytest.raises(ValueError, match="没有 canonical_id=999"):
        _create(transfer_env, items=[{"canonical_id": 999, "quantity": 1}])


def test_create_transfer_rejects_empty_items(transfer_env):
    with pytest.raises(ValueError, match="不能为空"):
        dtp.create_transfer_order(
            transfer_env["master"],
            from_warehouse_code="dc_test",
            to_warehouse_code="store_test",
            items=[],
            actor_id=2,
        )


def test_transfer_no_sequencing(transfer_env):
    first = _create(transfer_env)
    second = _create(transfer_env)
    assert first["order_no"] != second["order_no"]
    assert second["order_no"].endswith("0002")


# ---------------------------------------------------------------------------
# 状态流转：部分发货 / 部分收货
# ---------------------------------------------------------------------------

def test_document_and_receipt_states_across_partial_flow(transfer_env):
    master = transfer_env["master"]
    order = _create(transfer_env)
    item_id = order["order_items"][0]["id"]

    # 初始：待发货，接收单无货可收
    assert dtp.document_state(order) == dtp.DOC_AWAITING_SHIP
    assert dtp.receipt_state(order) == dtp.RCV_AWAITING_SHIP
    assert dtp.transfer_progress(order)["pending_receive_qty"] == 0

    # 部分发货 6/10：主状态仍 approved，但发送单已是「部分发出」、接收单「待接收」
    sop.ship_order(master, order["id"], {item_id: 6}, shipped_by=3)
    order = sop.get_order_detail(master, order["id"])
    assert order["status"] == sop.ORDER_STATUS_APPROVED
    assert dtp.document_state(order) == dtp.DOC_PARTIAL_SHIPPED
    assert dtp.receipt_state(order) == dtp.RCV_PENDING
    progress = dtp.transfer_progress(order)
    assert progress["shipped_qty"] == 6
    assert progress["pending_receive_qty"] == 6
    assert progress["pending_ship_qty"] == 4

    # 部分收货 4：接收单「部分接收」
    sop.receive_order_item(
        master, order_id=order["id"], order_item_id=item_id, qty=4,
        actor_id=4, note=None,
    )
    order = sop.get_order_detail(master, order["id"])
    assert dtp.receipt_state(order) == dtp.RCV_PARTIAL
    assert dtp.document_state(order) == dtp.DOC_PARTIAL_SHIPPED

    # 剩余 4 全发 → shipped；接收单仍显示剩余 2 待收
    sop.ship_order(master, order["id"], {item_id: 4}, shipped_by=3)
    order = sop.get_order_detail(master, order["id"])
    assert order["status"] == sop.ORDER_STATUS_SHIPPED
    assert dtp.document_state(order) == dtp.DOC_SHIPPED
    assert dtp.receipt_state(order) == dtp.RCV_PARTIAL

    # 收齐 → delivered / received
    sop.receive_order_item(
        master, order_id=order["id"], order_item_id=item_id, qty=6,
        actor_id=4, note=None,
    )
    order = sop.get_order_detail(master, order["id"])
    assert order["status"] == sop.ORDER_STATUS_DELIVERED
    assert dtp.document_state(order) == dtp.DOC_COMPLETED
    assert dtp.receipt_state(order) == dtp.RCV_RECEIVED


def test_transfer_uses_dedicated_stock_movement_actions(transfer_env):
    master = transfer_env["master"]
    order = _create(transfer_env)
    item_id = order["order_items"][0]["id"]
    sop.ship_order(master, order["id"], {item_id: 10}, shipped_by=3)
    sop.receive_order_item(
        master, order_id=order["id"], order_item_id=item_id, qty=10,
        actor_id=4, note=None,
    )

    dc = sqlite3.connect(transfer_env["dc_path"])
    out = dc.execute(
        "SELECT delta FROM stock_movements WHERE action=?",
        (sop.TRANSFER_SHIPMENT_ACTION,),
    ).fetchall()
    dc.close()
    assert [(r[0]) for r in out] == [-10.0]

    store = sqlite3.connect(transfer_env["store_path"])
    inbound = store.execute(
        "SELECT delta FROM stock_movements WHERE action=?",
        (sop.TRANSFER_RECEIPT_ACTION,),
    ).fetchall()
    qty = store.execute(
        "SELECT quantity FROM items WHERE canonical_id=101"
    ).fetchone()[0]
    store.close()
    assert [r[0] for r in inbound] == [10.0]
    assert qty == 10.0

    # 订货单的旧文案不得被调货单占用（避免报表口径混淆）
    assert sop.TRANSFER_SHIPMENT_ACTION != sop.SHIPMENT_ACTION
    assert sop.TRANSFER_RECEIPT_ACTION != sop.RECEIPT_ACTION


# ---------------------------------------------------------------------------
# 取消守卫 / 报表口径
# ---------------------------------------------------------------------------

def test_cancel_blocked_after_partial_shipment(transfer_env):
    master = transfer_env["master"]
    order = _create(transfer_env)
    item_id = order["order_items"][0]["id"]
    sop.ship_order(master, order["id"], {item_id: 2}, shipped_by=3)
    with pytest.raises(ValueError, match="已有发货记录"):
        sop.cancel_order(master, order["id"], cancelled_by=2, reason="测试")


def test_cancel_allowed_before_shipment(transfer_env):
    master = transfer_env["master"]
    order = _create(transfer_env)
    cancelled = sop.cancel_order(master, order["id"], cancelled_by=2, reason="改计划")
    assert cancelled["status"] == sop.ORDER_STATUS_CANCELLED
    reloaded = sop.get_order_detail(master, order["id"])
    assert dtp.document_state(reloaded) == dtp.DOC_CANCELLED
    assert dtp.receipt_state(reloaded) == dtp.RCV_CANCELLED


def test_store_order_report_excludes_transfers(transfer_env):
    master = transfer_env["master"]
    _create(transfer_env)
    # 一张普通订货单（pending），用于对照
    ts = sop.now()
    cur = master.execute(
        """INSERT INTO store_orders
           (order_no, store_warehouse_code, dc_warehouse_code, flow_type, status,
            requested_by, created_at, updated_at)
           VALUES ('SO-TEST-0001', 'store_test', 'dc_test', ?, 'pending', 4, ?, ?)""",
        (sop.FLOW_STORE_ORDER, ts, ts),
    )
    order_id = cur.lastrowid
    master.execute(
        """INSERT INTO store_order_items
           (order_id, canonical_id, quantity, unit, fulfilled_quantity,
            shipped_quantity, status, created_at)
           VALUES (?, 101, 7, '件', 0, 0, 'pending', ?)""",
        (order_id, ts),
    )
    master.commit()

    report = sop.compute_store_order_report(master)
    assert report["summary"]["ordered_qty"] == 7, "调货单不得计入订货报表"
    assert report["summary"]["order_count"] == 1


# ---------------------------------------------------------------------------
# 路由 / 权限
# ---------------------------------------------------------------------------

@pytest.fixture
def transfer_client(transfer_env, monkeypatch):
    from app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    def login_as(user_id: int, warehouse_id: int):
        with client.session_transaction() as session:
            session["user_id"] = user_id
            session["warehouse_id"] = warehouse_id
        return client

    return login_as


def test_dc_manager_can_create_transfer_via_route(transfer_env, transfer_client):
    client = transfer_client(2, 1)
    resp = client.post(
        "/dc-transfer/new",
        data={
            "to_warehouse_code": "store_test",
            "note": "路由建单",
            "items[101]": "3",
            "items[102]": "0",
        },
    )
    assert resp.status_code == 302
    row = transfer_env["master"].execute(
        "SELECT id, flow_type, status FROM store_orders WHERE note='路由建单'"
    ).fetchone()
    assert row is not None
    assert row["flow_type"] == sop.FLOW_DC_TRANSFER
    assert row["status"] == sop.ORDER_STATUS_APPROVED


def test_dc_staff_cannot_create_transfer_via_route(transfer_env, transfer_client):
    client = transfer_client(3, 1)
    resp = client.post(
        "/dc-transfer/new",
        data={"to_warehouse_code": "store_test", "items[101]": "1"},
    )
    assert resp.status_code == 403

    rows = transfer_env["master"].execute(
        "SELECT COUNT(*) FROM store_orders WHERE flow_type=?",
        (sop.FLOW_DC_TRANSFER,),
    ).fetchone()
    assert rows[0] == 0


def test_storefront_cannot_open_transfer_new(transfer_env, transfer_client):
    client = transfer_client(4, 2)
    assert client.get("/dc-transfer/new").status_code == 403


def test_storefront_transfer_list_redirects_to_orders(transfer_env, transfer_client):
    """门店不设独立调货入口：访问 /dc-transfer/ 回到「本店订单」。"""
    order = _create(transfer_env)
    item_id = order["order_items"][0]["id"]
    sop.ship_order(transfer_env["master"], order["id"], {item_id: 4}, shipped_by=3)

    client = transfer_client(4, 2)
    resp = client.get("/dc-transfer/")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/store-ordering/orders")


def test_dc_list_shows_outgoing_and_create_entry(transfer_env, transfer_client):
    order = _create(transfer_env)
    client = transfer_client(2, 1)
    body = client.get("/dc-transfer/").data.decode("utf-8")
    assert "总仓调货 · 发送单" in body
    assert order["order_no"] in body
    assert "新建调货单" in body


def test_list_transfers_only_receivable(transfer_env):
    master = transfer_env["master"]
    order = _create(transfer_env)
    assert dtp.list_transfers(
        master, to_warehouse_code="store_test", only_receivable=True
    ) == []

    item_id = order["order_items"][0]["id"]
    sop.ship_order(master, order["id"], {item_id: 5}, shipped_by=3)
    listed = dtp.list_transfers(
        master, to_warehouse_code="store_test", only_receivable=True
    )
    assert len(listed) == 1
    assert listed[0]["rcv_state"] == dtp.RCV_PENDING
    assert listed[0]["pending_receive_qty"] == 5


def test_my_orders_includes_transfer_as_receipt_order(transfer_env, transfer_client):
    """分仓「本店订单」必须包含调货单，并按收货进度显示（不暴露发送单概念）。"""
    master = transfer_env["master"]
    order = _create(transfer_env)
    item_id = order["order_items"][0]["id"]
    sop.ship_order(master, order["id"], {item_id: 5}, shipped_by=3)

    body = transfer_client(4, 2).get("/store-ordering/orders").data.decode("utf-8")
    assert order["order_no"] in body
    assert "总仓调货" in body
    assert "待收货" in body

    # 部分收货 → 列表显示「部分收货」
    sop.receive_order_item(
        master, order_id=order["id"], order_item_id=item_id, qty=2,
        actor_id=4, note=None,
    )
    body = transfer_client(4, 2).get("/store-ordering/orders").data.decode("utf-8")
    assert "部分收货" in body


def test_admin_order_board_marks_transfer_type(transfer_env, transfer_client):
    order = _create(transfer_env)
    body = transfer_client(1, 1).get("/store-ordering/admin/orders").data.decode("utf-8")
    assert order["order_no"] in body
    assert "总仓调货" in body


def test_transfer_detail_renders_for_both_sides(transfer_env, transfer_client):
    """详情页按视角渲染：总仓是调货单 / 发送视角，门店是订单 / 收货视角。"""
    master = transfer_env["master"]
    order = _create(transfer_env)
    item_id = order["order_items"][0]["id"]
    sop.ship_order(master, order["id"], {item_id: 4}, shipped_by=3)

    dc_body = transfer_client(2, 1).get(
        f"/store-ordering/orders/{order['id']}"
    ).data.decode("utf-8")
    assert "部分发出" in dc_body
    assert order["order_no"] in dc_body

    store_body = transfer_client(4, 2).get(
        f"/store-ordering/orders/{order['id']}"
    ).data.decode("utf-8")
    # 门店侧不出现「接收单」这类新概念，就是一张订单 + 待收货
    assert "接收单" not in store_body
    assert "待收货" in store_body
    assert "门店入库流水" in store_body
    # 内部调拨不展示结算金额
    assert "订单总计" not in store_body
    assert "内部调拨，不产生金额" in store_body

# Issue #107: receiving warehouse type must not determine document direction.
@pytest.fixture
def dc_receiver_env(transfer_env):
    env = transfer_env
    master = env["master"]
    ts = sop.now()
    master.execute(
        "INSERT INTO tenants (id, name, created_at) VALUES (2, 'other tenant', ?)", (ts,)
    )
    for wid, code, kind, tenant in (
        (3, "dc_receiver", "distribution_center", 1),
        (4, "dc_foreign", "distribution_center", 1),
        (5, "rd_receiver", "rd", 1),
        (6, "dc_other_tenant", "distribution_center", 2),
    ):
        path = env["dc_path"].parent / f"{code}.db"
        init_warehouse_db(path)
        master.execute(
            "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, tenant_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (wid, code, code, str(path), kind, tenant, ts),
        )
    for uid, name, wid, role in (
        (5, "receiver_manager", 3, "manager"),
        (6, "receiver_staff", 3, "staff"),
        (7, "foreign_manager", 4, "manager"),
    ):
        master.execute(
            "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (?, ?, 'x', 0, ?)",
            (uid, name, ts),
        )
        master.execute(
            "INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (?, ?, ?)",
            (uid, wid, role),
        )
    master.commit()
    env["receiver_path"] = env["dc_path"].parent / "dc_receiver.db"
    return env


def _to_dc(env, quantity=10):
    return dtp.create_transfer_order(
        env["master"],
        from_warehouse_code="dc_test",
        to_warehouse_code="dc_receiver",
        items=[{"canonical_id": 101, "quantity": quantity}],
        actor_id=2,
    )


def _stock(path):
    with sqlite3.connect(path) as conn:
        return conn.execute(
            "SELECT COALESCE(SUM(quantity), 0) FROM items WHERE canonical_id=101"
        ).fetchone()[0]


def _receive(client, order, item_id, qty, key):
    return client.post(
        f"/store-ordering/orders/{order['id']}/receive",
        data={
            "order_item_id": item_id,
            "quantity": str(qty),
            "op_no": key,
        },
    )


def _ship(client, order, item_id, qty, key):
    return client.post(
        f"/store-ordering/orders/{order['id']}/ship",
        data={
            f"shipped_items[{item_id}]": str(qty),
            "op_no": key,
        },
    )


def test_dc_receiver_candidates_and_creation(dc_receiver_env, transfer_client):
    env = dc_receiver_env
    client = transfer_client(2, 1)
    body = client.get("/dc-transfer/new").get_data(as_text=True)
    assert 'value="dc_receiver"' in body
    assert 'value="store_test"' in body
    for code in ("dc_test", "rd_receiver", "dc_other_tenant"):
        assert f'value="{code}"' not in body
    response = client.post(
        "/dc-transfer/new",
        data={
            "to_warehouse_code": "dc_receiver",
            "items[101]": "10",
        },
    )
    assert response.status_code == 302
    order = sop.list_orders(env["master"], store_warehouse_code="dc_receiver")[0]
    detail = sop.get_order_detail(env["master"], order["id"])
    assert detail["status"] == "approved"
    assert detail["flow_type"] == sop.FLOW_DC_TRANSFER
    assert detail["order_items"][0]["source_warehouse_code"] == "dc_test"
    notifications = (
        env["master"]
        .execute(
            "SELECT user_id, target_url FROM notifications WHERE event_type=?",
            (sop.EVENT_TRANSFER_CREATED,),
        )
        .fetchall()
    )
    # Fan-out keeps the existing effective order.receive recipient policy.
    # Staff may receive notifications but the manager role gate still blocks receipt.
    assert {r["user_id"] for r in notifications} == {5, 6}
    assert {r["target_url"] for r in notifications} == {f"/store-ordering/orders/{order['id']}"}
    assert _stock(env["dc_path"]) == 100 and _stock(env["receiver_path"]) == 0


@pytest.mark.parametrize("target", ["dc_test", "rd_receiver", "dc_other_tenant", "missing"])
def test_dc_transfer_invalid_receiver_is_atomic(dc_receiver_env, transfer_client, target):
    env = dc_receiver_env
    response = transfer_client(2, 1).post(
        "/dc-transfer/new",
        data={
            "to_warehouse_code": target,
            "items[101]": "10",
        },
    )
    assert response.status_code == 302
    assert env["master"].execute("SELECT COUNT(*) FROM store_orders").fetchone()[0] == 0
    assert _stock(env["dc_path"]) == 100


def test_dc_transfer_staff_cannot_create_for_dc(dc_receiver_env, transfer_client):
    client = transfer_client(3, 1)
    assert client.get("/dc-transfer/new").status_code == 403
    assert (
        client.post(
            "/dc-transfer/new",
            data={
                "to_warehouse_code": "dc_receiver",
                "items[101]": "1",
            },
        ).status_code
        == 403
    )
    assert dc_receiver_env["master"].execute("SELECT COUNT(*) FROM store_orders").fetchone()[0] == 0


def test_dc_receiver_incoming_list_and_detail_are_directional(dc_receiver_env, transfer_client):
    env = dc_receiver_env
    incoming = _to_dc(env)
    outgoing = _create(env)
    # Another sender's transfer must not be discoverable by receiving DC.
    env["master"].execute(
        "UPDATE store_orders SET dc_warehouse_code='dc_foreign' WHERE id=?", (outgoing["id"],)
    )
    env["master"].execute(
        "UPDATE store_order_items SET source_warehouse_code='dc_foreign' WHERE order_id=?",
        (outgoing["id"],),
    )
    env["master"].commit()
    item = incoming["order_items"][0]["id"]
    _ship(transfer_client(2, 1), incoming, item, 4, "list-ship")
    receiver = transfer_client(5, 3)
    outgoing_body = receiver.get("/dc-transfer/").get_data(as_text=True)
    assert incoming["order_no"] not in outgoing_body
    incoming_body = receiver.get(
        "/dc-transfer/?scope=incoming&from_warehouse_code=dc_foreign&to_warehouse_code=store_test"
    ).get_data(as_text=True)
    assert incoming["order_no"] in incoming_body and "待收货" in incoming_body
    assert outgoing["order_no"] not in incoming_body
    detail = receiver.get(f"/store-ordering/orders/{incoming['id']}").get_data(as_text=True)
    assert "待收货" in detail and f"/orders/{incoming['id']}/receive" in detail
    assert f"/orders/{incoming['id']}/deliver" in detail
    assert "shipped_items[" not in detail
    assert f"/orders/{incoming['id']}/cancel" not in detail
    assert "/dc-transfer/?scope=incoming" in detail
    sender_body = (
        transfer_client(2, 1).get(f"/store-ordering/orders/{incoming['id']}").get_data(as_text=True)
    )
    assert "部分发出" in sender_body and "shipped_items[" in sender_body
    assert f"/orders/{incoming['id']}/receive" not in sender_body
    # A DC incoming list must not open unrelated storefront catalogue/order routes.
    assert receiver.get("/store-ordering/orders").status_code == 302
    assert receiver.get("/store-ordering/cart").status_code == 302


@pytest.mark.parametrize(
    "target,user_id,warehouse_id,path_key",
    [
        ("dc_receiver", 5, 3, "receiver_path"),
        ("store_test", 4, 2, "store_path"),
    ],
)
def test_transfer_route_partial_final_receipt_and_retries(
    dc_receiver_env, transfer_client, target, user_id, warehouse_id, path_key
):
    env = dc_receiver_env
    order = dtp.create_transfer_order(
        env["master"],
        from_warehouse_code="dc_test",
        to_warehouse_code=target,
        items=[{"canonical_id": 101, "quantity": 10}],
        actor_id=2,
    )
    item = order["order_items"][0]["id"]
    sender = transfer_client(2, 1)
    assert _ship(sender, order, item, 6, "s1").status_code == 302
    assert _ship(sender, order, item, 6, "s1").status_code == 302
    assert _stock(env["dc_path"]) == 94
    receiver = transfer_client(user_id, warehouse_id)
    assert _receive(receiver, order, item, 4, "r1").status_code == 302
    assert _receive(receiver, order, item, 4, "r1").status_code == 302
    detail = sop.get_order_detail(env["master"], order["id"])
    assert detail["status"] == "approved"
    assert detail["order_items"][0]["fulfilled_quantity"] == 4
    assert _stock(env[path_key]) == 4
    # Overreceipt and reused key with altered quantity must leave all data unchanged.
    assert _receive(receiver, order, item, 3, "over").status_code == 302
    assert _receive(receiver, order, item, 1, "r1").status_code == 302
    assert _stock(env[path_key]) == 4
    assert len(sop.get_order_detail(env["master"], order["id"])["receipts"]) == 1
    sender = transfer_client(2, 1)
    assert (
        sender.post(
            f"/store-ordering/orders/{order['id']}/cancel", data={"reason": "too late"}
        ).status_code
        == 302
    )
    assert sop.get_order_detail(env["master"], order["id"])["status"] == "approved"
    assert _ship(sender, order, item, 4, "s2").status_code == 302
    assert _stock(env["dc_path"]) == 90
    receiver = transfer_client(user_id, warehouse_id)
    assert _receive(receiver, order, item, 6, "r2").status_code == 302
    final = sop.get_order_detail(env["master"], order["id"])
    assert final["status"] == "delivered"
    assert final["order_items"][0]["fulfilled_quantity"] == 10
    assert final["order_items"][0]["shipped_quantity"] == 10
    before = env["master"].execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
    assert _receive(receiver, order, item, 6, "r2").status_code == 302
    assert env["master"].execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == before
    assert _stock(env[path_key]) == 10
    assert len(sop.get_order_detail(env["master"], order["id"])["receipts"]) == 2
    with sqlite3.connect(env[path_key]) as conn:
        assert conn.execute(
            "SELECT delta FROM stock_movements WHERE action=? ORDER BY id",
            (sop.TRANSFER_RECEIPT_ACTION,),
        ).fetchall() == [(4,), (6,)]
    with sqlite3.connect(env["dc_path"]) as conn:
        assert conn.execute(
            "SELECT delta FROM stock_movements WHERE action=? ORDER BY id",
            (sop.TRANSFER_SHIPMENT_ACTION,),
        ).fetchall() == [(-6,), (-4,)]
    assert [h["to_status"] for h in final["status_history"]].count("delivered") == 1
    assert (
        env["master"]
        .execute(
            "SELECT COUNT(*) FROM notifications WHERE event_type=?", (sop.EVENT_ORDER_SHIPPED,)
        )
        .fetchone()[0]
        > 0
    )
    partial = (
        env["master"]
        .execute(
            "SELECT user_id FROM notifications WHERE event_type=?", (sop.EVENT_ORDER_RECEIVED,)
        )
        .fetchall()
    )
    assert {r[0] for r in partial} == {1, 2}


@pytest.mark.parametrize("route", ["receive", "deliver"])
@pytest.mark.parametrize("uid,wid", [(6, 3), (2, 1), (7, 4), (4, 2), (1, 1)])
def test_transfer_receipt_rejects_unauthorized_or_wrong_warehouse(
    dc_receiver_env, transfer_client, route, uid, wid
):
    env = dc_receiver_env
    order = _to_dc(env)
    item = order["order_items"][0]["id"]
    _ship(transfer_client(2, 1), order, item, 4, "auth-ship")
    client = transfer_client(uid, wid)
    assert (
        client.post(
            f"/store-ordering/orders/{order['id']}/{route}",
            data={
                "order_item_id": item,
                "quantity": "4",
                "op_no": "unauthorized",
            },
        ).status_code
        == 403
    )
    assert _stock(env["receiver_path"]) == 0
    assert sop.get_order_detail(env["master"], order["id"])["receipts"] == []


def test_foreign_dc_cannot_discover_view_or_ship_transfer(dc_receiver_env, transfer_client):
    env = dc_receiver_env
    order = _to_dc(env)
    client = transfer_client(7, 4)
    for url in ("/dc-transfer/", "/dc-transfer/?scope=incoming", "/store-ordering/review"):
        assert order["order_no"] not in client.get(url).get_data(as_text=True)
    assert client.get(f"/store-ordering/orders/{order['id']}").status_code == 403
    assert _ship(client, order, order["order_items"][0]["id"], 1, "foreign").status_code == 403
    assert _stock(env["dc_path"]) == 100


@pytest.mark.parametrize("route", ["receive", "deliver"])
def test_dc_receiver_cannot_receive_normal_order(dc_receiver_env, transfer_client, route):
    env = dc_receiver_env
    order = _to_dc(env)
    item = order["order_items"][0]["id"]
    _ship(transfer_client(2, 1), order, item, 4, "normal-ship")
    env["master"].execute(
        "UPDATE store_orders SET flow_type=? WHERE id=?", (sop.FLOW_STORE_ORDER, order["id"])
    )
    env["master"].commit()
    assert (
        transfer_client(5, 3)
        .post(
            f"/store-ordering/orders/{order['id']}/{route}",
            data={
                "order_item_id": item,
                "quantity": "4",
                "op_no": "normal-receipt",
            },
        )
        .status_code
        == 302
    )
    assert _stock(env["receiver_path"]) == 0
    assert sop.get_order_detail(env["master"], order["id"])["receipts"] == []


def test_receiver_cannot_cancel_even_with_sender_role(dc_receiver_env, transfer_client):
    env = dc_receiver_env
    order = _to_dc(env)
    # Having sender privileges in another warehouse must not make this an outgoing order.
    env["master"].execute(
        "INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (5, 1, 'manager')"
    )
    env["master"].commit()
    assert (
        transfer_client(5, 3)
        .post(f"/store-ordering/orders/{order['id']}/cancel", data={"reason": "receiver"})
        .status_code
        == 403
    )
    assert sop.get_order_detail(env["master"], order["id"])["status"] == "approved"
    assert (
        transfer_client(2, 1)
        .post(f"/store-ordering/orders/{order['id']}/cancel", data={"reason": "sender"})
        .status_code
        == 302
    )
    assert sop.get_order_detail(env["master"], order["id"])["status"] == "cancelled"
    assert _stock(env["dc_path"]) == 100 and _stock(env["receiver_path"]) == 0


def test_dc_receiving_shortcut_handles_partial_full_and_repeat(dc_receiver_env, transfer_client):
    env = dc_receiver_env
    order = _to_dc(env)
    item = order["order_items"][0]["id"]
    _ship(transfer_client(2, 1), order, item, 4, "quick-s1")
    receiver = transfer_client(5, 3)
    url = f"/store-ordering/orders/{order['id']}/deliver"
    assert receiver.post(url).status_code == 302
    assert _stock(env["receiver_path"]) == 4
    assert sop.get_order_detail(env["master"], order["id"])["status"] == "approved"
    assert (
        env["master"]
        .execute(
            "SELECT COUNT(*) FROM notifications WHERE event_type=?", (sop.EVENT_ORDER_DELIVERED,)
        )
        .fetchone()[0]
        == 0
    )
    before = env["master"].execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
    assert receiver.post(url).status_code == 302
    assert env["master"].execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == before
    _ship(transfer_client(2, 1), order, item, 6, "quick-s2")
    receiver = transfer_client(5, 3)
    assert receiver.post(url).status_code == 302
    assert sop.get_order_detail(env["master"], order["id"])["status"] == "delivered"
    before = env["master"].execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
    assert receiver.post(url).status_code == 302
    assert env["master"].execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == before
    assert _stock(env["dc_path"]) == 90 and _stock(env["receiver_path"]) == 10


def test_dc_receipt_storage_failure_retries_without_duplicate_stock(
    dc_receiver_env, transfer_client, monkeypatch
):
    env = dc_receiver_env
    order = _to_dc(env)
    item = order["order_items"][0]["id"]
    _ship(transfer_client(2, 1), order, item, 4, "failure-ship")
    client = transfer_client(5, 3)
    apply = sop._apply_store_receipts

    def fail_storage(*args, **kwargs):
        raise sqlite3.OperationalError("injected receiving warehouse unavailable")

    monkeypatch.setattr(sop, "_apply_store_receipts", fail_storage)
    with pytest.raises(sqlite3.OperationalError, match="injected"):
        _receive(client, order, item, 4, "repair")
    pending = sop.get_order_detail(env["master"], order["id"])
    assert pending["receipts"][0]["applied"] == 0
    assert pending["order_items"][0]["fulfilled_quantity"] == 4
    assert _stock(env["receiver_path"]) == 0
    monkeypatch.setattr(sop, "_apply_store_receipts", apply)
    assert _receive(client, order, item, 4, "repair").status_code == 302
    assert _receive(client, order, item, 4, "repair").status_code == 302
    repaired = sop.get_order_detail(env["master"], order["id"])
    assert len(repaired["receipts"]) == 1 and repaired["receipts"][0]["applied"] == 1
    assert repaired["order_items"][0]["fulfilled_quantity"] == 4
    assert _stock(env["receiver_path"]) == 4 and _stock(env["dc_path"]) == 96


def test_dc_receipt_foreign_line_and_cancelled_order_are_rejected(dc_receiver_env, transfer_client):
    env = dc_receiver_env
    order = _to_dc(env)
    other = _to_dc(env)
    item = order["order_items"][0]["id"]
    foreign_item = other["order_items"][0]["id"]
    _ship(transfer_client(2, 1), order, item, 4, "line-ship")
    receiver = transfer_client(5, 3)
    assert _receive(receiver, order, foreign_item, 1, "foreign-line").status_code == 302
    assert sop.get_order_detail(env["master"], order["id"])["receipts"] == []
    sop.cancel_order(env["master"], other["id"], 2, "cancel")
    assert _receive(receiver, other, foreign_item, 1, "cancelled").status_code == 302
    assert sop.get_order_detail(env["master"], other["id"])["receipts"] == []
    assert _stock(env["receiver_path"]) == 0


def test_dc_receiver_manager_still_requires_receive_permission(dc_receiver_env, transfer_client):
    env = dc_receiver_env
    order = _to_dc(env)
    item = order["order_items"][0]["id"]
    _ship(transfer_client(2, 1), order, item, 4, "permission-ship")
    env["master"].execute(
        "DELETE FROM role_permissions WHERE permission_code='order.receive' AND role_id IN (SELECT id FROM roles WHERE code='manager')"
    )
    env["master"].commit()
    client = transfer_client(5, 3)
    assert _receive(client, order, item, 4, "no-permission").status_code == 403
    body = client.get(f"/store-ordering/orders/{order['id']}").get_data(as_text=True)
    assert f"/orders/{order['id']}/receive" not in body
    assert _stock(env["receiver_path"]) == 0


def test_dc_transfer_small_batch_replay_is_idempotent(dc_receiver_env, transfer_client):
    env = dc_receiver_env
    order = _to_dc(env)
    item = order["order_items"][0]["id"]
    sender = transfer_client(2, 1)
    assert _ship(sender, order, item, 6, "small-ship").status_code == 302
    receiver = transfer_client(5, 3)
    assert _receive(receiver, order, item, 2, "small-receipt").status_code == 302
    before = env["master"].execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
    assert _receive(receiver, order, item, 2, "small-receipt").status_code == 302
    assert _stock(env["receiver_path"]) == 2
    assert env["master"].execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == before
    assert len(sop.get_order_detail(env["master"], order["id"])["receipts"]) == 1


def test_dc_transfer_shortcut_replay_does_not_receive_later_arrivals(
    dc_receiver_env, transfer_client
):
    env = dc_receiver_env
    order = dtp.create_transfer_order(
        env["master"],
        from_warehouse_code="dc_test",
        to_warehouse_code="dc_receiver",
        items=[{"canonical_id": 101, "quantity": 10}, {"canonical_id": 102, "quantity": 3}],
        actor_id=2,
    )
    first, second = [it["id"] for it in order["order_items"]]
    _ship(transfer_client(2, 1), order, first, 4, "arrivals-1")
    receiver = transfer_client(5, 3)
    url = f"/store-ordering/orders/{order['id']}/deliver"
    assert receiver.post(url, data={"op_no": "arrival-receipt"}).status_code == 302
    _ship(transfer_client(2, 1), order, second, 3, "arrivals-2")
    receiver = transfer_client(5, 3)
    before = env["master"].execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
    assert receiver.post(url, data={"op_no": "arrival-receipt"}).status_code == 302
    detail = sop.get_order_detail(env["master"], order["id"])
    assert len(detail["receipts"]) == 1
    assert [it["fulfilled_quantity"] for it in detail["order_items"]] == [4, 0]
    assert env["master"].execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == before
    assert receiver.post(url, data={"op_no": "new-arrival-receipt"}).status_code == 302
    assert [
        it["fulfilled_quantity"]
        for it in sop.get_order_detail(env["master"], order["id"])["order_items"]
    ] == [4, 3]


def test_dc_transfer_shortcut_recovers_interrupted_receipt(
    dc_receiver_env, transfer_client, monkeypatch
):
    env = dc_receiver_env
    order = _to_dc(env)
    item = order["order_items"][0]["id"]
    _ship(transfer_client(2, 1), order, item, 4, "shortcut-failure")
    receiver = transfer_client(5, 3)
    url = f"/store-ordering/orders/{order['id']}/deliver"
    apply = sop._apply_store_receipts

    def fail_storage(*args, **kwargs):
        raise sqlite3.OperationalError("injected shortcut warehouse unavailable")

    monkeypatch.setattr(sop, "_apply_store_receipts", fail_storage)
    with pytest.raises(sqlite3.OperationalError, match="injected shortcut"):
        receiver.post(url, data={"op_no": "shortcut-repair"})
    assert _stock(env["receiver_path"]) == 0
    monkeypatch.setattr(sop, "_apply_store_receipts", apply)
    assert receiver.post(url, data={"op_no": "shortcut-repair"}).status_code == 302
    assert receiver.post(url, data={"op_no": "shortcut-repair"}).status_code == 302
    detail = sop.get_order_detail(env["master"], order["id"])
    assert len(detail["receipts"]) == 1 and detail["receipts"][0]["applied"] == 1
    assert _stock(env["receiver_path"]) == 4 and _stock(env["dc_path"]) == 96


def test_dc_transfer_receipt_and_shipping_status_constraints(dc_receiver_env, transfer_client):
    env = dc_receiver_env
    order = _to_dc(env)
    item = order["order_items"][0]["id"]
    receiver = transfer_client(5, 3)
    assert _receive(receiver, order, item, 1, "not-shipped").status_code == 302
    assert sop.get_order_detail(env["master"], order["id"])["receipts"] == []
    assert _stock(env["receiver_path"]) == 0
    cancelled = _to_dc(env)
    cancelled_item = cancelled["order_items"][0]["id"]
    sender = transfer_client(2, 1)
    sender.post(f"/store-ordering/orders/{cancelled['id']}/cancel", data={"reason": "before ship"})
    assert _ship(sender, cancelled, cancelled_item, 1, "cancelled-ship").status_code == 302
    assert _stock(env["dc_path"]) == 100
    assert sop.get_order_detail(env["master"], cancelled["id"])["status"] == "cancelled"
    assert _ship(sender, order, item, 10, "full-ship").status_code == 302
    sender.post(f"/store-ordering/orders/{order['id']}/cancel", data={"reason": "after ship"})
    assert sop.get_order_detail(env["master"], order["id"])["status"] == "shipped"
    assert _receive(transfer_client(5, 3), order, item, 10, "full-receipt").status_code == 302
    sender = transfer_client(2, 1)
    sender.post(f"/store-ordering/orders/{order['id']}/cancel", data={"reason": "after receipt"})
    assert _ship(sender, order, item, 1, "after-receipt").status_code == 302
    assert sop.get_order_detail(env["master"], order["id"])["status"] == "delivered"
    assert _stock(env["dc_path"]) == 90 and _stock(env["receiver_path"]) == 10


def test_receiving_dc_keeps_own_outgoing_transfers_separate(dc_receiver_env, transfer_client):
    env = dc_receiver_env
    incoming = _to_dc(env)
    item = incoming["order_items"][0]["id"]
    _ship(transfer_client(2, 1), incoming, item, 4, "dual-direction")
    receiver = transfer_client(5, 3)
    _receive(receiver, incoming, item, 4, "dual-direction-receipt")
    response = receiver.post(
        "/dc-transfer/new",
        data={
            "to_warehouse_code": "store_test",
            "items[101]": "2",
        },
    )
    assert response.status_code == 302
    outgoing = sop.list_orders(env["master"], dc_warehouse_code="dc_receiver")[0]
    out_body = receiver.get("/dc-transfer/").get_data(as_text=True)
    in_body = receiver.get("/dc-transfer/?scope=incoming").get_data(as_text=True)
    assert outgoing["order_no"] in out_body and incoming["order_no"] not in out_body
    assert incoming["order_no"] in in_body and outgoing["order_no"] not in in_body
    detail = sop.get_order_detail(env["master"], outgoing["id"])
    assert detail["order_items"][0]["source_warehouse_code"] == "dc_receiver"
    assert _stock(env["receiver_path"]) == 4


@pytest.mark.parametrize("route,expected", [("receive", 2), ("deliver", 10)])
def test_normal_order_admin_cross_storefront_receipt_compatibility(
    transfer_env, transfer_client, route, expected
):
    env = transfer_env
    master = env["master"]
    other = env["dc_path"].parent / "other_store.db"
    init_warehouse_db(other)
    master.execute(
        "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) "
        "VALUES (3, 'other_store', 'Other storefront', ?, 'storefront', ?)",
        (str(other), sop.now()),
    )
    master.commit()
    order = _create(env)
    master.execute(
        "UPDATE store_orders SET flow_type=? WHERE id=?", (sop.FLOW_STORE_ORDER, order["id"])
    )
    master.commit()
    item = order["order_items"][0]["id"]
    sop.ship_order(master, order["id"], {item: 10}, shipped_by=3)
    client = transfer_client(1, 3)
    response = client.post(
        f"/store-ordering/orders/{order['id']}/{route}",
        data={
            "order_item_id": item,
            "quantity": "2",
            "op_no": "normal-admin-compat",
        },
    )
    assert response.status_code == 302
    assert _stock(env["store_path"]) == expected
    assert _stock(other) == 0
    detail = sop.get_order_detail(master, order["id"])
    assert detail["order_items"][0]["fulfilled_quantity"] == expected
    assert detail["status"] == ("delivered" if route == "deliver" else "shipped")


@pytest.mark.parametrize("route", ["receive", "deliver"])
@pytest.mark.parametrize("uid", [1, 2])
def test_normal_order_dc_receipt_keeps_legacy_redirects(transfer_env, transfer_client, route, uid):
    env = transfer_env
    master = env["master"]
    order = _create(env)
    master.execute(
        "UPDATE store_orders SET flow_type=? WHERE id=?", (sop.FLOW_STORE_ORDER, order["id"])
    )
    master.commit()
    item = order["order_items"][0]["id"]
    sop.ship_order(master, order["id"], {item: 10}, shipped_by=3)
    response = transfer_client(uid, 1).post(
        f"/store-ordering/orders/{order['id']}/{route}",
        data={
            "order_item_id": item,
            "quantity": "2",
            "op_no": "normal-dc-reject",
        },
    )
    assert response.status_code == 302
    expected = (
        "/store-ordering/review"
        if route == "receive" and uid == 2
        else f"/store-ordering/orders/{order['id']}"
    )
    assert response.location.endswith(expected)
    assert sop.get_order_detail(master, order["id"])["receipts"] == []
    assert _stock(env["store_path"]) == 0 and _stock(env["dc_path"]) == 90
