"""Route tests for distribution-center store-ordering flows."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from io import BytesIO
from urllib.parse import unquote

import pytest
from openpyxl import load_workbook

from db import init_master_db, init_warehouse_db


@pytest.fixture
def dc_env(tmp_path, monkeypatch):
    """DC manager (user 2) + store staff (user 1) environment with one submitted order."""
    import config as config_module
    import db as db_module

    master_path = tmp_path / "master.db"
    wh_dir = tmp_path / "warehouses"
    wh_dir.mkdir()
    dc_path = wh_dir / "dc_test.db"
    dc2_path = wh_dir / "dc2_test.db"
    store_path = wh_dir / "store_test.db"

    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_dir)
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", wh_dir)

    init_master_db()
    init_warehouse_db(dc_path)
    init_warehouse_db(dc2_path)
    init_warehouse_db(store_path)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    m = sqlite3.connect(str(master_path))
    m.row_factory = sqlite3.Row
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (1, 'store_staff', 'x', 0, ?)",
        (ts,),
    )
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (2, 'dc_mgr', 'x', 0, ?)",
        (ts,),
    )
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (3, 'dc_staff', 'x', 0, ?)",
        (ts,),
    )
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (4, 'dc2_mgr', 'x', 0, ?)",
        (ts,),
    )
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (5, 'admin', 'x', 1, ?)",
        (ts,),
    )
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) VALUES (1, 'dc_test', '测试配送中心', ?, 'distribution_center', ?)",
        (str(dc_path), ts),
    )
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) VALUES (4, 'dc2_test', '测试配送中心2', ?, 'distribution_center', ?)",
        (str(dc2_path), ts),
    )
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) VALUES (2, 'store_test', '测试门店', ?, 'storefront', ?)",
        (str(store_path), ts),
    )
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (1, 2, 'staff')")
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (2, 1, 'manager')")
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (3, 1, 'staff')")
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (4, 4, 'manager')")
    m.execute(
        "INSERT INTO canonical_categories (code, name, description, created_at, updated_at) VALUES ('PACKAGING', '包材', '', ?, ?)",
        (ts, ts),
    )
    m.execute(
        """INSERT INTO canonical_items
           (id, canonical_sku, name, category_code, unit, gram_per_unit, aux_unit, aux_rate,
            status, created_from, created_at, updated_at)
           VALUES (101, 'IC-000101', '测试包材A', 'PACKAGING', '件', 0, NULL, 0, 'active', 'rd_manual', ?, ?)""",
        (ts, ts),
    )
    m.commit()
    m.close()

    dc = sqlite3.connect(str(dc_path))
    dc.row_factory = sqlite3.Row
    cat_id = dc.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()["id"]
    dc.execute(
        "INSERT INTO items (sku, name, category_id, quantity, unit, canonical_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("DC-A", "测试包材A", cat_id, 100.0, "件", 101, ts),
    )
    dc.commit()
    dc.close()

    dc2 = sqlite3.connect(str(dc2_path))
    dc2.row_factory = sqlite3.Row
    cat_id = dc2.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()["id"]
    dc2.execute(
        "INSERT INTO items (sku, name, category_id, quantity, unit, canonical_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("DC2-A", "测试包材A", cat_id, 100.0, "件", 101, ts),
    )
    dc2.commit()
    dc2.close()

    store = sqlite3.connect(str(store_path))
    store.row_factory = sqlite3.Row
    cat_id = store.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()["id"]
    store.execute(
        "INSERT INTO items (sku, name, category_id, quantity, unit, canonical_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("ST-A", "测试包材A", cat_id, 0.0, "件", 101, ts),
    )
    store.commit()
    store.close()

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    # Create an order as store user.
    with client.session_transaction() as s:
        s["user_id"] = 1
        s["warehouse_id"] = 2
    client.post(
        "/store-ordering/cart/add",
        data={"dc": "dc_test", "canonical_id": 101, "quantity": "5", "unit": "件"},
    )
    client.post(
        "/store-ordering/cart/submit",
        data={"expected_delivery_date": datetime.now().strftime("%Y-%m-%d"), "note": ""},
    )
    # Grab order id.
    master = sqlite3.connect(str(master_path))
    master.row_factory = sqlite3.Row
    order_id = master.execute("SELECT id FROM store_orders ORDER BY id DESC LIMIT 1").fetchone()["id"]
    master.close()

    yield {
        "app": app,
        "client": client,
        "master_path": master_path,
        "dc_path": dc_path,
        "dc2_path": dc2_path,
        "store_path": store_path,
        "order_id": order_id,
    }


def _login_as(client, user_id, warehouse_id):
    with client.session_transaction() as s:
        s["user_id"] = user_id
        s["warehouse_id"] = warehouse_id


def test_review_list(dc_env):
    client = dc_env["client"]
    _login_as(client, 2, 1)
    resp = client.get("/store-ordering/review")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "SO-" in body


def test_review_approve(dc_env):
    client = dc_env["client"]
    _login_as(client, 2, 1)
    resp = client.post(
        f"/store-ordering/orders/{dc_env['order_id']}/review",
        data={"decision": "approved", "note": "ok"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    master = sqlite3.connect(str(dc_env["master_path"]))
    master.row_factory = sqlite3.Row
    status = master.execute("SELECT status FROM store_orders WHERE id=?", (dc_env["order_id"],)).fetchone()["status"]
    master.close()
    assert status == "approved"


def test_shipment_workbook_uses_each_committed_batch(dc_env, tmp_path, monkeypatch):
    import config

    client = dc_env['client']
    order_id = dc_env['order_id']
    detail = f'/store-ordering/orders/{order_id}'
    label = '下载订货单 Excel'.encode()
    profile = tmp_path / 'shipment-profile.json'
    profile.write_text(json.dumps({'dc_test': {
        'stores': {'store_test': {'code': 'STORE-001', 'brand': '测试品牌',
                                  'recipient': '示例收货人', 'phone': '01234567890',
                                  'address': '示例地址'}},
        'items': {'IC-000101': {'code': 'ITEM-001', 'spec': '2 件/箱', 'unit': '箱',
                                'base_unit': '件', 'base_units_per_order_unit': '2'}},
    }}, ensure_ascii=False), encoding='utf-8')
    monkeypatch.setattr(config, 'SHIPMENT_EXPORT_PROFILE_PATH', str(profile))
    master = sqlite3.connect(dc_env['master_path'])
    try:
        item_id = master.execute('SELECT id FROM store_order_items WHERE order_id=?',
                                 (order_id,)).fetchone()[0]
        master.execute("UPDATE store_orders SET note='=1+1' WHERE id=?", (order_id,))
        master.commit()

        _login_as(client, 2, 1)
        assert label not in client.get(detail).data
        assert client.get(f'{detail}/deliveries/1/export.xlsx').status_code == 404
        assert client.post(f'{detail}/review', data={'decision': 'approved'}).status_code == 302
        assert label not in client.get(detail).data
        assert client.get(f'{detail}/deliveries/1/export.xlsx').status_code == 404

        _login_as(client, 3, 1)
        first = {'op_no': 'first-export-batch', f'shipped_items[{item_id}]': '1.5'}
        assert client.post(f'{detail}/ship', data=first, follow_redirects=True).data.count(label) == 1
        assert client.post(f'{detail}/ship', data=first, follow_redirects=True).data.count(label) == 1
        second = {'op_no': 'second-export-batch', f'shipped_items[{item_id}]': '3.5'}
        assert client.post(f'{detail}/ship', data=second, follow_redirects=True).data.count(label) == 2

        order_no = master.execute('SELECT order_no FROM store_orders WHERE id=?',
                                  (order_id,)).fetchone()[0]
        batches = master.execute('SELECT id, delivery_no FROM store_order_deliveries ORDER BY id').fetchall()
        assert len(batches) == 2
        for (batch_id, delivery_no), quantity in zip(batches, (0.75, 1.75), strict=True):
            response = client.get(f'{detail}/deliveries/{batch_id}/export.xlsx')
            assert response.status_code == 200
            assert response.mimetype == 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            assert 'attachment' in response.headers['Content-Disposition']
            assert delivery_no in response.headers['Content-Disposition']
            assert '资料待补' not in unquote(response.headers['Content-Disposition'])
            assert response.headers['Cache-Control'] == 'private, no-store'
            workbook = load_workbook(BytesIO(response.data))
            assert workbook.sheetnames == ['门店订货单模板']
            sheet = workbook.active
            assert (sheet.max_row, sheet.max_column) == (2, 18)
            assert [cell.value for cell in sheet[1]] == [
                '序号', '来源单号', '订货单号', '序号', '门店编码', '品牌', '门店名称', '收货人',
                '收件人电话', '收货地址', '品项名称', '品项编码', '规格', '订货单位', '订货数量',
                '期望到货日期', '是否加急', '备注',
            ]
            assert [cell.value for cell in sheet[2][:15]] == [
                1, delivery_no, order_no, 1, 'STORE-001', '测试品牌', '测试门店', '示例收货人',
                '01234567890', '示例地址', '测试包材A', 'ITEM-001', '2 件/箱', '箱', quantity,
            ]
            assert sheet['P2'].is_date
            assert sheet['Q2'].value is None
            assert sheet['R2'].value == '=1+1'
            assert sheet['R2'].data_type == 's'
            assert sheet['F1'].fill.fgColor.rgb.endswith('FFFF00')
            assert sheet.column_dimensions['J'].width == 52.5

        url = f'{detail}/deliveries/{batches[0][0]}/export.xlsx'
        for user, warehouse, status in ((1, 2, 403), (4, 4, 404), (5, 2, 403), (5, 4, 404)):
            _login_as(client, user, warehouse)
            assert client.get(url).status_code == status
            assert label not in client.get(detail).data
        _login_as(client, 5, 1)
        assert client.get(url).status_code == 200
        assert client.get(f'/store-ordering/orders/{order_id + 1}/deliveries/{batches[0][0]}/export.xlsx').status_code == 404
        _login_as(client, 3, 1)
        master.execute("UPDATE users SET is_active=0 WHERE id=3")
        master.commit()
        assert client.get(url).status_code == 403
        assert label not in client.get(detail).data
        master.execute("UPDATE users SET is_active=1 WHERE id=3")
        master.execute("INSERT INTO tenants (id, name, created_at) VALUES (2, '测试租户', datetime('now'))")
        master.execute("UPDATE warehouses SET tenant_id=2 WHERE code='store_test'")
        master.commit()
        assert client.get(url).status_code == 403
        assert label not in client.get(detail).data
        assert master.execute('SELECT COUNT(*) FROM store_order_deliveries').fetchone()[0] == 2
        with sqlite3.connect(dc_env['dc_path']) as dc:
            assert dc.execute('SELECT quantity FROM items WHERE canonical_id=101').fetchone()[0] == 95
    finally:
        master.close()


@pytest.mark.parametrize('ineligible', ['history_without_lines', 'dc_transfer', 'not_approved'])
def test_shipment_workbook_requires_approved_store_order_and_batch_lines(dc_env, ineligible):
    client = dc_env['client']
    order_id = dc_env['order_id']
    detail = f'/store-ordering/orders/{order_id}'
    _login_as(client, 2, 1)
    assert client.post(f'{detail}/review', data={'decision': 'approved'}).status_code == 302
    assert client.post(f'{detail}/ship').status_code == 302
    with sqlite3.connect(dc_env['master_path']) as master:
        batch_id = master.execute('SELECT id FROM store_order_deliveries').fetchone()[0]
        if ineligible == 'dc_transfer':
            master.execute("UPDATE store_orders SET flow_type='dc_transfer' WHERE id=?", (order_id,))
        elif ineligible == 'not_approved':
            master.execute('UPDATE store_orders SET approved_at=NULL WHERE id=?', (order_id,))
        else:
            master.execute('DELETE FROM store_order_delivery_items WHERE delivery_id=?', (batch_id,))
    assert '下载订货单 Excel'.encode() not in client.get(detail).data
    assert client.get(f'{detail}/deliveries/{batch_id}/export.xlsx').status_code == 404


def test_shipment_workbook_flags_missing_data_and_invalid_unit_conversion(dc_env, tmp_path, monkeypatch):
    import config

    client = dc_env['client']
    detail = f'/store-ordering/orders/{dc_env["order_id"]}'
    _login_as(client, 2, 1)
    assert client.post(f'{detail}/review', data={'decision': 'approved'}).status_code == 302
    assert client.post(f'{detail}/ship').status_code == 302
    with sqlite3.connect(dc_env['master_path']) as master:
        batch_id = master.execute('SELECT id FROM store_order_deliveries').fetchone()[0]
    url = f'{detail}/deliveries/{batch_id}/export.xlsx'
    monkeypatch.setattr(config, 'SHIPMENT_EXPORT_PROFILE_PATH', None)
    response = client.get(url)
    assert response.status_code == 200
    assert '资料待补' in unquote(response.headers['Content-Disposition'])
    sheet = load_workbook(BytesIO(response.data)).active
    assert (sheet['E2'].value, sheet['H2'].value, sheet['L2'].value) == ('store_test', None, 'IC-000101')
    assert (sheet['N2'].value, sheet['O2'].value) == ('件', 5)

    path = tmp_path / 'invalid-shipment-profile.json'
    monkeypatch.setattr(config, 'SHIPMENT_EXPORT_PROFILE_PATH', str(path))
    for factor in ('1e+500', '1e-500', '1e-1000000'):
        path.write_text(json.dumps({'dc_test': {'items': {'IC-000101': {
            'unit': '箱', 'base_unit': '件', 'base_units_per_order_unit': factor,
        }}}}), encoding='utf-8')
        assert client.get(url).status_code == 503


def test_admin_without_warehouse_binding_sees_ship_form(dc_env):
    """平台管理员进「没有 warehouse_users 绑定」的 DC 也要看到发货入口。

    2026-11-02 生产现象：蓝天大厦（wh_005）一条绑定都没有，g.role 为 None；
    详情页 can_ship 之前是显式角色判断（不认 is_admin），发货区整块不渲染，
    而 POST /orders/<id>/ship 的 @require_role("staff") 对管理员放行 ——
    路由允许写、界面不给按钮。这里锁住「界面与路由同一判定」。
    """
    client = dc_env["client"]
    _login_as(client, 5, 1)  # admin，warehouse 1 无绑定
    client.post(
        f"/store-ordering/orders/{dc_env['order_id']}/review",
        data={"decision": "approved", "note": "ok"},
    )

    master = sqlite3.connect(str(dc_env["master_path"]))
    bound = master.execute(
        "SELECT 1 FROM warehouse_users WHERE user_id=5 AND warehouse_id=1"
    ).fetchone()
    master.close()
    assert bound is None  # 前提：确实没有绑定

    resp = client.get(f"/store-ordering/orders/{dc_env['order_id']}")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "确认发货" in body
    assert "shipped_items[" in body


def test_review_reject_requires_note(dc_env):
    client = dc_env["client"]
    _login_as(client, 2, 1)
    resp = client.post(
        f"/store-ordering/orders/{dc_env['order_id']}/review",
        data={"decision": "rejected"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "必须填写原因" in body


def test_shipment_list_and_ship(dc_env):
    client = dc_env["client"]
    # Approve as manager
    _login_as(client, 2, 1)
    client.post(
        f"/store-ordering/orders/{dc_env['order_id']}/review",
        data={"decision": "approved", "note": "ok"},
    )
    # Ship as staff — DC operations (review + ship) live on the unified
    # /review page; /shipments is now a legacy alias that redirects.
    _login_as(client, 3, 1)
    resp = client.get("/store-ordering/shipments")
    assert resp.status_code == 302
    assert "/store-ordering/review" in resp.headers.get("Location", "")

    # Issue #19：合并后的订单页用状态页签承载「待审批 / 待出库 / …」，
    # 不再是「已审批 · 待出库」这样的分节标题。
    resp = client.get("/store-ordering/review")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "待出库" in body  # 页签存在
    assert "SO-" in body

    # 页签过滤到「待出库」后，刚审批的那张单仍在该页可见。
    resp = client.get("/store-ordering/review?status=approved")
    assert resp.status_code == 200
    assert "SO-" in resp.data.decode()

    resp = client.post(
        f"/store-ordering/orders/{dc_env['order_id']}/ship",
        data={"tracking_note": "快递"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    master = sqlite3.connect(str(dc_env["master_path"]))
    master.row_factory = sqlite3.Row
    status = master.execute("SELECT status FROM store_orders WHERE id=?", (dc_env["order_id"],)).fetchone()["status"]
    master.close()
    assert status == "shipped"


def test_dc_outbound_page_explains_approval_does_not_lock_stock(dc_env):
    client = dc_env["client"]
    _login_as(client, 2, 1)
    client.post(
        f"/store-ordering/orders/{dc_env['order_id']}/review",
        data={"decision": "approved", "note": "ok"},
    )

    _login_as(client, 3, 1)
    resp = client.get("/outbound")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "审批不锁定库存" in body
    assert "发货时" in body
    assert "扣减" in body

    dc = sqlite3.connect(str(dc_env["dc_path"]))
    quantity = dc.execute(
        "SELECT quantity FROM items WHERE canonical_id=101"
    ).fetchone()[0]
    dc.close()
    assert quantity == 100.0


def test_dc_outbound_page_links_store_order_shipment(dc_env):
    client = dc_env["client"]
    _login_as(client, 2, 1)
    client.post(
        f"/store-ordering/orders/{dc_env['order_id']}/review",
        data={"decision": "approved", "note": "ok"},
    )
    _login_as(client, 3, 1)
    client.post(
        f"/store-ordering/orders/{dc_env['order_id']}/ship",
        data={},
    )

    master = sqlite3.connect(str(dc_env["master_path"]))
    order_no = master.execute(
        "SELECT order_no FROM store_orders WHERE id=?", (dc_env["order_id"],)
    ).fetchone()[0]
    master.close()

    resp = client.get("/outbound")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert order_no in body
    assert "订单发货" in body
    assert f"/store-ordering/orders/{dc_env['order_id']}" in body


def test_storefront_outbound_page_keeps_existing_columns(dc_env):
    store = sqlite3.connect(str(dc_env["store_path"]))
    item_id = store.execute("SELECT id FROM items WHERE canonical_id=101").fetchone()[0]
    store.execute(
        """INSERT INTO outbound_requests
           (item_id, requested_quantity, reason, status, rolled_back, created_at, source_type)
           VALUES (?, 1, '生产补录', '出库', 0, '2026-10-27 10:00:00', 'production')""",
        (item_id,),
    )
    store.execute(
        """INSERT INTO outbound_requests
           (item_id, requested_quantity, reason, status, rolled_back, created_at, source_type)
           VALUES (?, 2, '盘点补录', '出库', 0, '2026-10-27 10:01:00', 'stocktake_loss')""",
        (item_id,),
    )
    store.commit()
    store.close()

    client = dc_env["client"]
    _login_as(client, 1, 2)
    resp = client.get("/outbound")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "生产补录" in body
    assert "盘点补录" in body
    assert "<th>来源</th>" not in body
    assert "人工出库" not in body
    assert "生产领料" not in body
    assert "盘点盘亏" not in body


def test_dc_outbound_page_derives_manual_source_labels(dc_env):
    dc = sqlite3.connect(str(dc_env["dc_path"]))
    item_id = dc.execute("SELECT id FROM items WHERE canonical_id=101").fetchone()[0]
    dc.execute(
        """INSERT INTO outbound_requests
           (item_id, requested_quantity, reason, status, rolled_back, created_at, source_type)
           VALUES (?, 1, '生产补录', '出库', 0, '2026-10-27 10:00:00', 'production')""",
        (item_id,),
    )
    dc.execute(
        """INSERT INTO outbound_requests
           (item_id, requested_quantity, reason, status, rolled_back, created_at, source_type)
           VALUES (?, 2, '盘点补录', '出库', 0, '2026-10-27 10:01:00', 'stocktake_loss')""",
        (item_id,),
    )
    dc.commit()
    dc.close()

    client = dc_env["client"]
    _login_as(client, 3, 1)
    resp = client.get("/outbound")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "<th>来源</th>" in body
    assert "生产领料" in body
    assert "盘点盘亏" in body
    assert "人工出库" not in body


def test_ship_insufficient_stock_rejected(dc_env):
    """2026-10-10 决策反转：DC 发货不得超过现有库存（原 A7 欠货出库已废止）。

    路由层 /ship 应 flash「出库失败：库存不足…」；订单停在 approved；
    DC 库存不被扣减、不跌负。
    """
    client = dc_env["client"]
    # Approve as manager
    _login_as(client, 2, 1)
    client.post(
        f"/store-ordering/orders/{dc_env['order_id']}/review",
        data={"decision": "approved", "note": "ok"},
    )
    # Deplete DC stock to 1 while the order needs more (canonical 101).
    dc = sqlite3.connect(str(dc_env["dc_path"]))
    dc.execute("UPDATE items SET quantity=? WHERE canonical_id=?", (1.0, 101))
    dc.commit()
    dc.close()
    # Try ship as staff — 必须被拒。
    _login_as(client, 3, 1)
    resp = client.post(
        f"/store-ordering/orders/{dc_env['order_id']}/ship",
        data={"tracking_note": ""},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "库存不足" in body
    assert "出库失败" in body
    # 不应出现「出库成功」。
    assert "出库成功" not in body

    # 订单仍在 approved。
    master = sqlite3.connect(str(dc_env["master_path"]))
    master.row_factory = sqlite3.Row
    status = master.execute("SELECT status FROM store_orders WHERE id=?", (dc_env["order_id"],)).fetchone()["status"]
    master.close()
    assert status == "approved"

    # DC 库存未被扣减（仍为 1，不为负）。
    dc = sqlite3.connect(str(dc_env["dc_path"]))
    dc.row_factory = sqlite3.Row
    qty = dc.execute("SELECT quantity FROM items WHERE canonical_id=101").fetchone()["quantity"]
    assert qty == 1.0
    dc.close()


def test_dc_staff_can_view_review_but_cannot_approve(dc_env):
    """Combined review page is read-only for DC staff — they see the lists but
    cannot act. The inline approve/reject forms live on order_detail and
    are guarded by can_review (manager+ only).
    """
    client = dc_env["client"]
    _login_as(client, 3, 1)
    # Page is reachable.
    resp = client.get("/store-ordering/review")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "订货审批" in body
    # But POST /review must fail (only managers can decide).
    post_resp = client.post(
        f"/store-ordering/orders/{dc_env['order_id']}/review",
        data={"decision": "approved"},
    )
    assert post_resp.status_code == 403


def test_review_and_ship_other_dc_order_forbidden(dc_env):
    """DC manager of dc_test must not review/ship an order bound to dc2_test."""
    client = dc_env["client"]
    # Create an order bound to dc2_test as store user.
    _login_as(client, 1, 2)
    client.post(
        "/store-ordering/cart/add",
        data={"dc": "dc2_test", "canonical_id": 101, "quantity": "3", "unit": "件"},
    )
    client.post(
        "/store-ordering/cart/submit",
        data={"expected_delivery_date": datetime.now().strftime("%Y-%m-%d"), "note": ""},
    )
    master = sqlite3.connect(str(dc_env["master_path"]))
    master.row_factory = sqlite3.Row
    order_id = master.execute("SELECT id FROM store_orders WHERE dc_warehouse_code='dc2_test'").fetchone()["id"]
    master.close()

    # dc_test manager attempts review.
    _login_as(client, 2, 1)
    resp = client.post(
        f"/store-ordering/orders/{order_id}/review",
        data={"decision": "approved", "note": "ok"},
    )
    assert resp.status_code == 403

    # dc_test staff attempts ship.
    _login_as(client, 3, 1)
    resp = client.post(
        f"/store-ordering/orders/{order_id}/ship",
        data={"tracking_note": ""},
    )
    assert resp.status_code == 403


def test_admin_can_review_and_ship_any_dc_order(dc_env):
    """Platform admin logged into a storefront can still review DC orders.

    Shipping is different: the operation warehouse is always the session
    warehouse (multi-warehouse §7), so the ship segment switches the admin's
    session to dc_test. Manually adapting only the setup, per coordinator
    ruling; both assertions (302 + shipped) are unchanged.
    """
    client = dc_env["client"]
    order_id = dc_env["order_id"]

    # Admin logged into storefront reviews an order bound to dc_test.
    _login_as(client, 5, 2)
    resp = client.post(
        f"/store-ordering/orders/{order_id}/review",
        data={"decision": "approved", "note": "admin ok"},
    )
    assert resp.status_code == 302

    master = sqlite3.connect(str(dc_env["master_path"]))
    master.row_factory = sqlite3.Row
    status = master.execute("SELECT status FROM store_orders WHERE id=?", (order_id,)).fetchone()["status"]
    master.close()
    assert status == "approved"

    # Admin ships the order as well — in an explicit DC session.
    _login_as(client, 5, 1)
    resp = client.post(
        f"/store-ordering/orders/{order_id}/ship",
        data={"tracking_note": "admin ship"},
    )
    assert resp.status_code == 302

    master = sqlite3.connect(str(dc_env["master_path"]))
    master.row_factory = sqlite3.Row
    status = master.execute("SELECT status FROM store_orders WHERE id=?", (order_id,)).fetchone()["status"]
    master.close()
    assert status == "shipped"
