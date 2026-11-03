"""Real Flask HTTP adapters, temp SQLite and the existing permission model."""
import hashlib
import re

import pytest

import config
from blueprints import order_changes_pure as changes
from blueprints import store_ordering_pure as sop
from tests import test_ship_op_protocol as protocol
from tests.test_order_changes_claims import seed_proposal

ship_env = protocol.ship_env


def login(e, user=4, warehouse=1):
    with e['client'].session_transaction() as session:
        session['user_id'] = user
        session['warehouse_id'] = warehouse


def proposal(e, status='approved'):
    oid, ids = e['create_order']([(101, 4, 'A'), (102, 3, 'A')], status=status)
    pid = seed_proposal(e['master'], oid, [ids[0]])
    return oid, ids, pid, f'/store-ordering/orders/{oid}/changes/{pid}'


def test_off_flag_read_and_withdraw_survive_csrf_prg_and_retry(ship_env, monkeypatch):
    e = ship_env
    monkeypatch.setattr(config, 'ORDER_CHANGES_ENABLED', False)
    oid, ids, pid, url = proposal(e)
    login(e)
    e['client'].application.config['WTF_CSRF_FORCE_ENABLED'] = True
    page = e['client'].get(url)
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert '撤回整份方案' in body and '未回复不会自动同意' in body
    before = hashlib.sha256(e['master_path'].read_bytes()).hexdigest()
    assert e['client'].get(url).status_code == 200
    assert e['client'].get(f'/store-ordering/orders/{oid}').status_code == 200
    assert hashlib.sha256(e['master_path'].read_bytes()).hexdigest() == before
    assert e['client'].post(url + '/withdraw', data={'request_key': 'withdraw'}).status_code == 400
    csrf = re.search('name="csrf_token" value="([^"]+)"', body).group(1)
    data = {'csrf_token': csrf, 'request_key': 'withdraw', 'note': '恢复原单'}
    result = e['client'].post(url + '/withdraw', data=data)
    assert result.status_code == 302 and result.location.endswith(url)
    assert changes.proposal_detail(e['master'], oid, pid)['status'] == 'withdrawn'
    assert sop.get_order_detail(e['master'], oid)['order_items'][0]['quantity'] == 4
    counts = tuple(e['master'].execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in ('store_order_change_events','store_order_change_commands','notifications'))
    replay = e['client'].post(url + '/withdraw', data=data, headers={'X-Requested-With': 'fetch'})
    assert replay.status_code == 200 and replay.json['ok'] is True
    assert tuple(e['master'].execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in ('store_order_change_events','store_order_change_commands','notifications')) == counts
    bad = e['client'].post(url + '/withdraw', data={**data, 'note': 'different'}, headers={'X-Requested-With': 'fetch'})
    assert bad.status_code == 409 and bad.json['ok'] is False
    assert not changes.blocked_lines(e['master'], oid)


def test_permission_revoked_after_get_is_rechecked_inside_post(ship_env):
    e = ship_env
    oid, _, pid, url = proposal(e)
    e['master'].execute("UPDATE warehouse_users SET role='manager' WHERE user_id=2 AND warehouse_id=1")
    e['master'].commit()
    login(e, user=2)
    assert '撤回整份方案' in e['client'].get(url).get_data(as_text=True)
    e['master'].execute("UPDATE warehouse_users SET role='staff' WHERE user_id=2 AND warehouse_id=1")
    e['master'].commit()
    assert e['client'].post(url + '/withdraw', data={'request_key': 'revoked'}).status_code == 403
    assert changes.proposal_detail(e['master'], oid, pid)['status'] == 'pending'


def test_all_union_warehouses_required_for_non_proposer(ship_env):
    e = ship_env
    oid, ids = e['create_order']([(101, 4, 'A'), (102, 3, 'B')])
    pid = seed_proposal(e['master'], oid, ids)
    e['master'].execute("UPDATE warehouse_users SET role='manager' WHERE user_id=2")
    e['master'].commit()
    login(e, user=2)
    url = f'/store-ordering/orders/{oid}/changes/{pid}'
    assert '撤回整份方案' not in e['client'].get(url).get_data(as_text=True)
    assert e['client'].post(url + '/withdraw', data={'request_key': 'partial-permission'}).status_code == 403
    e['master'].execute("INSERT INTO warehouse_users(user_id,warehouse_id,role) VALUES(2,2,'manager')")
    e['master'].commit()
    assert e['client'].post(url + '/withdraw', data={'request_key': 'union'}).status_code == 302
    assert changes.proposal_detail(e['master'], oid, pid)['status'] == 'withdrawn'


@pytest.mark.parametrize('action', ['cancel','reject'])
def test_order_terminal_decision_invalidates_proposal_atomically(ship_env, action, monkeypatch):
    import blueprints.notifications_pure as notifications
    e = ship_env
    oid, _, pid, _ = proposal(e, status='pending')
    login(e)
    with pytest.raises(ValueError, match='待确认变更'):
        sop.review_order(e['master'], oid, 'approved', 4)
    original = notifications.enqueue_event
    monkeypatch.setattr(notifications, 'enqueue_event', lambda *a: (_ for _ in ()).throw(RuntimeError('notification failure')))
    command = (lambda: sop.cancel_order(e['master'], oid, 4, '取消')) if action == 'cancel' else (lambda: sop.review_order(e['master'], oid, 'rejected', 4, '拒绝'))
    with pytest.raises(RuntimeError, match='notification failure'):
        command()
    assert sop.get_order_detail(e['master'], oid)['status'] == 'pending'
    assert changes.proposal_detail(e['master'], oid, pid)['status'] == 'pending'
    assert e['master'].execute('SELECT count(*) FROM store_order_change_events').fetchone()[0] == 0
    monkeypatch.setattr(notifications, 'enqueue_event', original)
    command()
    assert changes.proposal_detail(e['master'], oid, pid)['status'] == 'stale'
    assert sop.get_order_detail(e['master'], oid)['change_revision'] == 1
    assert e['master'].execute('SELECT count(*) FROM store_order_change_events').fetchone()[0] == 1


def test_proposal_pause_preserves_old_receipt_and_staff_shipping(ship_env):
    e = ship_env
    oid, ids = e['create_order']([(101, 4, 'A'), (102, 3, 'A')])
    protocol._ship(e, oid, 'A', 'before-proposal', {ids[0]: 1})
    seed_proposal(e['master'], oid, [ids[0]])
    login(e, user=2)
    response = e['client'].post(f'/store-ordering/orders/{oid}/ship', data={f'shipped_items[{ids[0]}]': 1, f'shipped_items[{ids[1]}]': 1, 'op_no': 'blocked-http'}, headers={'X-Requested-With': 'fetch'})
    assert response.status_code == 409 and response.json['ok'] is False
    assert e['stock']('A', 101) == 9 and e['stock']('A', 102) == 10
    protocol._ship(e, oid, 'A', 'unaffected', {ids[1]: 1})
    sop.receive_partial(e['master'], sop.open_warehouse_db, order_id=oid, op_no='receive-pending', item_qty={ids[0]: 1}, received_by=1)
    received = sop.get_order_detail(e['master'], oid)
    assert received['order_items'][0]['fulfilled_quantity'] == 1
    assert received['status'] == 'approved'


def test_notification_and_audit_rollback_after_enqueue(ship_env, monkeypatch):
    e = ship_env
    oid, _, pid, _ = proposal(e)
    original = changes.canonical_json

    def fail_after_enqueue(value):
        if isinstance(value, dict) and value == {'proposal_id': pid, 'status': 'withdrawn'}:
            raise RuntimeError('failure after notification enqueue')
        return original(value)
    monkeypatch.setattr(changes, 'canonical_json', fail_after_enqueue)
    with pytest.raises(RuntimeError, match='after notification enqueue'):
        changes.withdraw_proposal(e['master'], order_id=oid, proposal_id=pid, actor_id=4,
            current_warehouse_code='A', request_key='rollback-after-enqueue')
    assert changes.proposal_detail(e['master'], oid, pid)['status'] == 'pending'
    assert e['master'].execute('SELECT count(*) FROM store_order_change_events').fetchone()[0] == 0
    assert e['master'].execute('SELECT count(*) FROM notifications').fetchone()[0] == 0
