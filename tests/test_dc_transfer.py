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


def test_storefront_sees_incoming_list(transfer_env, transfer_client):
    master = transfer_env["master"]
    order = _create(transfer_env)
    item_id = order["order_items"][0]["id"]
    sop.ship_order(master, order["id"], {item_id: 4}, shipped_by=3)

    client = transfer_client(4, 2)
    resp = client.get("/dc-transfer/")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    assert "调货接收单" in body
    assert order["order_no"] in body

    # 收完之后默认视图不再列出（scope=all 仍可见）
    sop.receive_order_item(
        master, order_id=order["id"], order_item_id=item_id, qty=4,
        actor_id=4, note=None,
    )
    assert order["order_no"] not in client.get("/dc-transfer/").data.decode("utf-8")
    assert order["order_no"] in client.get("/dc-transfer/?scope=all").data.decode("utf-8")


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


def test_transfer_detail_renders_for_both_sides(transfer_env, transfer_client):
    """详情页按视角渲染发送单 / 接收单，且不因 flow 分支缺变量报 500。"""
    master = transfer_env["master"]
    order = _create(transfer_env)
    item_id = order["order_items"][0]["id"]
    sop.ship_order(master, order["id"], {item_id: 4}, shipped_by=3)

    dc_body = transfer_client(2, 1).get(
        f"/store-ordering/orders/{order['id']}"
    ).data.decode("utf-8")
    assert "发送单" in dc_body
    assert "部分发出" in dc_body
    assert order["order_no"] in dc_body

    store_body = transfer_client(4, 2).get(
        f"/store-ordering/orders/{order['id']}"
    ).data.decode("utf-8")
    assert "接收单" in store_body
    assert "待接收" in store_body
    assert "分仓入库流水" in store_body
    # 内部调拨不展示结算金额
    assert "订单总计" not in store_body
    assert "内部调拨，不产生金额" in store_body
