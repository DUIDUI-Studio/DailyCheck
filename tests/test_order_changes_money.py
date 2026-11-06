"""Authoritative commercial snapshots and effective-quantity behavior."""
import json
import sqlite3
from contextlib import closing

import pytest

from blueprints import order_changes_pure as changes
from blueprints import store_ordering_pure as sop
from tests import multi_warehouse_env as builder
from tests import test_ship_op_protocol as protocol

ship_env = protocol.ship_env


@pytest.fixture
def money_env(tmp_path, monkeypatch):
    env = builder.build(tmp_path, monkeypatch)
    builder.enable(monkeypatch)
    return env


def new_order(env, cid=201, qty=3):
    with closing(builder.connect(env['master_path'])) as master:
        cart = sop.get_or_create_cart(master, 1, 'store_one', None)
        sop.add_cart_item(master, cart['id'], cid, qty, '件')
        return sop.submit_order(master, cart['id'], 1, None, None)


def test_decimal_half_cent_server_price_rule_and_live_stock(money_env, monkeypatch):
    env = money_env
    with closing(builder.connect(env['paths']['dc_alpha'])) as wh:
        wh.execute('UPDATE items SET selling_price=0.335,unit_cost=99 WHERE canonical_id=201')
        wh.commit()
    with closing(builder.connect(env['master_path'])) as master:
        sop.upsert_shipping_rule(master, base_fee=0.005, pct_fee=0)
    order = new_order(env)
    line = order['order_items'][0]
    assert line['unit_price_snapshot'] == '0.335'
    assert line['line_amount'] == order['goods_amount'] == '1.01'
    assert order['shipping_fee'] == 0.01
    assert order['total_amount'] == '1.02'
    rule = json.loads(order['shipping_rule_snapshot'])
    assert rule['rounding'] == 'ROUND_HALF_UP' and rule['base_fee'] == '0.005'
    with closing(builder.connect(env['paths']['dc_alpha'])) as wh:
        wh.execute('UPDATE items SET selling_price=20,unit_cost=30,quantity=7 WHERE canonical_id=201')
        wh.commit()
    with closing(builder.connect(env['master_path'])) as master:
        sop.upsert_shipping_rule(master, base_fee=100, pct_fee=0.5)
        master.execute("UPDATE canonical_items SET name='后来改名' WHERE id=201")
        master.commit()
        refreshed = sop.get_order_detail(master, order['id'])
        assert refreshed['goods_amount'] == '1.01' and refreshed['shipping_fee'] == 0.01
        assert refreshed['order_items'][0]['canonical_name'] == '品项A'
        assert sop.get_dc_items_for_order_detail(master, order['id'])[line['id']]['dc_available'] == 7
        sop.review_order(master, order['id'], 'approved', 2)
        sop.ship_partial(master, sop.open_warehouse_db, order_id=order['id'], operator_dc_code='dc_alpha', op_no='money-ship', line_qty={line['id']: 3}, shipped_by=2)
        sop.receive_partial(master, sop.open_warehouse_db, order_id=order['id'], op_no='money-receive', item_qty={line['id']: 3}, received_by=1)
        assert sop.get_order_detail(master, order['id'])['total_amount'] == '1.02'
    from mcp_server.data import unit_of_work
    from mcp_server.service.auth import AuthContext
    from mcp_server.service.store_order import get_store_order_detail
    monkeypatch.setattr(unit_of_work, 'MASTER_DB', env['master_path'])
    ctx = AuthContext(token_id=1, allowed_read_paths=['/api/v1/store-orders'], allowed_write_paths=[], allowed_warehouses=['dc_alpha'], read_only=True)
    result = get_store_order_detail(ctx, order['id'])
    assert result['order']['total_amount'] == '1.02'
    assert result['items'][0]['unit_price_snapshot'] == '0.335'
    assert 'shipping_rule_snapshot' not in result['order']
    assert 'product_snapshot' not in result['items'][0]
    assert env['client'].get(f"/store-ordering/orders/{order['id']}").status_code == 200
    body = env['client'].get(f"/store-ordering/orders/{order['id']}").get_data(as_text=True)
    assert '1.02' in body and '后来改名' not in body


def test_real_zero_is_distinct_from_unknown_history(ship_env):
    e = ship_env
    oid, ids = e['create_order']([(101, 5, 'A')])
    before = e['master'].total_changes
    old = sop.get_order_detail(e['master'], oid)
    assert e['master'].total_changes == before
    assert old['goods_amount'] is None and old['amount_state'] == 'unknown'
    e['master'].execute("UPDATE store_order_items SET unit_price_snapshot='0',price_snapshot_origin='verified' WHERE id=?", (ids[0],))
    e['master'].commit()
    zero = sop.get_order_detail(e['master'], oid)
    assert zero['goods_amount'] == '0.00' and zero['amount_state'] == 'locked'
    assert zero['order_items'][0]['price_state'] == 'locked'


def test_snapshot_identity_and_original_are_immutable(money_env):
    env = money_env
    order = new_order(env)
    with closing(builder.connect(env['master_path'])) as master:
        for sql in ("original_quantity=99", "unit_price_snapshot='0'", "canonical_id=203", "source_warehouse_code='dc_beta'", "dc_item_id=999", "unit='箱'"):
            with pytest.raises(sqlite3.IntegrityError, match='immutable'):
                master.execute('UPDATE store_order_items SET ' + sql + ' WHERE order_id=?', (order['id'],))
            master.rollback()


def test_cancel_last_unshipped_quantity_can_complete_without_fake_records(ship_env):
    e = ship_env
    order, ids = e['create_order']([(101, 5, 'A'), (102, 3, 'A')])
    protocol._ship(e, order, 'A', 'partial', {ids[0]: 2})
    sop.receive_partial(e['master'], sop.open_warehouse_db, order_id=order, op_no='received', item_qty={ids[0]: 2}, received_by=1)
    receipts_before = [tuple(r) for r in e['master'].execute('SELECT * FROM store_order_receipts')]
    delivery_before = [tuple(r) for r in e['master'].execute('SELECT * FROM store_order_delivery_items')]
    e['master'].execute('BEGIN IMMEDIATE')
    e['master'].execute('UPDATE store_order_items SET quantity=shipped_quantity,line_revision=line_revision+1 WHERE order_id=?', (order,))
    assert sop.refresh_effective_order_state(e['master'], order, 4) == 'delivered'
    e['master'].commit()
    assert [tuple(r) for r in e['master'].execute('SELECT * FROM store_order_receipts')] == receipts_before
    assert [tuple(r) for r in e['master'].execute('SELECT * FROM store_order_delivery_items')] == delivery_before
    assert [r[0] for r in e['master'].execute('SELECT original_quantity FROM store_order_items ORDER BY id')] == [5, 3]
    assert sop.compute_store_order_report(e['master'])['summary']['ordered_qty'] == 2
    from blueprints.replenishment_data import _store_open_orders
    assert _store_open_orders().get(('store_test', 101), 0) == 0


def test_unapplied_receipt_cannot_complete_even_after_effective_reduction(ship_env):
    e = ship_env
    order, ids = e['create_order']([(101, 2, 'A')])
    protocol._ship(e, order, 'A', 'all', {ids[0]: 2})
    e['master'].execute('UPDATE store_order_items SET fulfilled_quantity=2 WHERE id=?', (ids[0],))
    e['master'].execute("INSERT INTO store_order_receipts(order_id,order_item_id,quantity,received_by,created_at,op_no,applied) VALUES(?,?,2,1,'2026-11-03','unapplied',0)", (order, ids[0]))
    assert changes.effective_progress(e['master'], order)['all_received'] is False
    assert sop.refresh_effective_order_state(e['master'], order, 4) == 'shipped'
    assert sop._finalise_receipt_state(e['master'], order, 1, None, '2026-11-03') is False
    e['master'].rollback()


@pytest.mark.parametrize('multi', [True, False])
def test_submit_cannot_freeze_price_under_a_forged_unit(money_env, monkeypatch, multi):
    import config
    env = money_env
    monkeypatch.setattr(config, 'MULTI_WAREHOUSE_FULFILLMENT', multi)
    with closing(builder.connect(env['master_path'])) as master:
        cart = sop.get_or_create_cart(master, 1, 'store_one', None if multi else 'dc_alpha')
        sop.add_cart_item(master, cart['id'], 201, 3, '箱')
        with pytest.raises(ValueError, match='基础单位不一致'):
            sop.submit_order(master, cart['id'], 1, None, None)
        assert master.execute('SELECT count(*) FROM store_orders').fetchone()[0] == 0
