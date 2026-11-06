"""Real Flask HTTP adapters, temp SQLite and the existing permission model."""
import hashlib
import http.cookiejar
import json
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager

import pytest

import config
from blueprints import order_changes_pure as changes
from blueprints import store_ordering_pure as sop
from tests import test_ship_op_protocol as protocol
from tests.test_order_changes_claims import seed_proposal

ship_env = protocol.ship_env


@contextmanager
def identity_http(e, order_id):
    """Real loopback HTTP, signed DC session and a freshly rendered CSRF token."""
    from werkzeug.serving import make_server

    app = e['client'].application
    app.config['WTF_CSRF_FORCE_ENABLED'] = True
    cookie = app.session_interface.get_signing_serializer(app).dumps(
        {'user_id': 2, 'warehouse_id': 1})
    server = make_server('127.0.0.1', 0, app, threaded=True)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    url = f'http://127.0.0.1:{server.server_port}/store-ordering/orders/{order_id}'
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        with opener.open(urllib.request.Request(url, headers={'Cookie': 'session=' + cookie})) as response:
            csrf = re.search('name="csrf_token" value="([^"]+)"', response.read().decode()).group(1)
        session = '; '.join(c.name + '=' + c.value for c in jar)

        def post(data):
            request = urllib.request.Request(url + '/ship',
                data=urllib.parse.urlencode({**data, 'csrf_token': csrf}).encode(),
                headers={'Cookie': session, 'X-Requested-With': 'fetch'})
            try:
                response = urllib.request.urlopen(request, timeout=20)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                body = response.read().decode()
                return response.status, (json.loads(body) if response.headers.get_content_type() ==
                    'application/json' else {'ok': False, 'html': body})

        yield post
    finally:
        server.shutdown()
        server.server_close()
        worker.join(5)
        assert not worker.is_alive()


def identity_form(mode, item_id, quantity=3, key='identity-request'):
    data = {'op_no': key}
    if mode == 'qty':
        data['qty'] = str(quantity)
    elif mode == 'explicit':
        data[f'shipped_items[{item_id}]'] = str(quantity)
    return data


@pytest.mark.parametrize('mode', ['qty', 'explicit', 'full'])
@pytest.mark.parametrize('phase', ['initial', 'warehouse', 'pre-lock'])
def test_original_identity_survives_concurrent_first_request(ship_env, monkeypatch, mode, phase):
    _identity_window(ship_env, monkeypatch, mode, phase)


@pytest.mark.parametrize('mode,phase', [('full', 'early-full'),
    ('explicit', 'binding'), ('explicit', 'later-batch'), ('explicit', 'prepare-error-active')])
def test_replay_ignores_mutable_preparation(ship_env, monkeypatch, mode, phase):
    _identity_window(ship_env, monkeypatch, mode, phase)


def _identity_window(e, monkeypatch, mode, phase):
    order, ids = e['create_order']([(101, 4, 'A')])
    reached, release = threading.Event(), threading.Event()
    wh_reached, wh_release = threading.Event(), threading.Event()
    armed, wh_armed, guard, responses, failures = [True], [True], threading.Lock(), {}, []
    original_get, original_begin = changes.get_claim, sop._begin_immediate
    original_apply, original_binding = sop._apply_warehouse_shipment, sop.resolve_fulfillment_item
    original_result, original_notify = sop._ship_result, sop.notify_order_event
    deliveries, notices = [], []

    def pause():
        with guard:
            first = armed[0]
            armed[0] = False
        if first:
            reached.set()
            assert release.wait(15), 'identity barrier timeout'
        return first

    def get(*args, **kwargs):
        result = original_get(*args, **kwargs)
        caller = sys._getframe(1).f_code.co_name
        # Delay the completed optimistic read, rather than the first SELECT of
        # the old resolver (which performed two reads). Both source versions
        # pause before any authoritative write lock is acquired.
        optimistic = (caller == 'ship_partial' or
            (caller == 'resolve_ship_payload' and len(args) == 3) or
            (phase == 'early-full' and caller == '_ship_order_full_impl'))
        if result is None and optimistic and phase not in ('pre-lock', 'binding', 'prepare-error-active'):
            pause()
        return result

    def begin(*args):
        if phase == 'pre-lock':
            pause()
        return original_begin(*args)

    def binding(*args, **kwargs):
        if phase in ('binding', 'prepare-error-active') and sys._getframe(1).f_code.co_name == 'ship_partial':
            if pause() and phase == 'prepare-error-active':
                # Actual SQLite preparation error, with the valid active batch
                # still executable through its frozen claim on another connection.
                args[1].execute('SELECT * FROM missing_preparation_table')
        return original_binding(*args, **kwargs)

    def apply(*args, **kwargs):
        result = original_apply(*args, **kwargs)
        with guard:
            first = wh_armed[0]
            wh_armed[0] = False
        if first and phase in ('warehouse', 'early-full', 'prepare-error-active'):
            wh_reached.set()
            assert wh_release.wait(15), 'warehouse barrier timeout'
        return result

    def result(*args, **kwargs):
        value = original_result(*args, **kwargs)
        if kwargs['op_no'] == 'identity-request':
            deliveries.append(value['delivery_id'])
        return value

    def notify(*args, **kwargs):
        notices.append(args[1])
        return original_notify(*args, **kwargs)

    monkeypatch.setattr(changes, 'get_claim', get)
    monkeypatch.setattr(sop, '_begin_immediate', begin)
    monkeypatch.setattr(sop, 'resolve_fulfillment_item', binding)
    monkeypatch.setattr(sop, '_apply_warehouse_shipment', apply)
    monkeypatch.setattr(sop, '_ship_result', result)
    monkeypatch.setattr(sop, 'notify_order_event', notify)
    with identity_http(e, order) as post:
        data = identity_form(mode, ids[0])

        def request(name):
            try:
                responses[name] = post(data)
            except Exception as exc:
                failures.append(repr(exc))

        delayed = threading.Thread(target=request, args=('delayed',))
        winner = None
        try:
            delayed.start()
            assert reached.wait(15)
            assert original_get(e['master'], data['op_no']) is None
            if phase in ('warehouse', 'early-full', 'prepare-error-active'):
                winner = threading.Thread(target=request, args=('winner',))
                winner.start()
                assert wh_reached.wait(15)
                assert original_get(e['master'], data['op_no'])['state'] == 'active'
                release.set()
                delayed.join(15)
                wh_release.set()
                winner.join(15)
            else:
                request('winner')
                if phase == 'binding':
                    with sop.open_warehouse_db('A') as wh:
                        wh.execute("UPDATE items SET unit='箱' WHERE canonical_id=101")
                if phase == 'later-batch':
                    responses['later'] = post(identity_form('explicit', ids[0], 1, 'later-request'))
                release.set()
                delayed.join(15)
            assert not delayed.is_alive() and (winner is None or not winner.is_alive())
            claim = original_get(e['master'], data['op_no'])
            with sop.open_warehouse_db('A') as wh:
                movements = wh.execute('SELECT count(*) FROM stock_movements WHERE op_no LIKE ?',
                    (data['op_no'] + ':%',)).fetchone()[0]
            observed = {'mode': mode, 'phase': phase, 'responses': responses,
                'stock': e['stock']('A', 101), 'movements': movements,
                'deliveries': e['master'].execute('SELECT count(*) FROM store_order_deliveries').fetchone()[0],
                'claim': claim, 'result_delivery_ids': deliveries, 'notices': notices,
                'notifications': e['master'].execute('SELECT count(*) FROM notifications').fetchone()[0],
                'failures': failures}
            print(json.dumps(observed, ensure_ascii=False))
            assert not failures
            assert observed['stock'] == (6 if mode == 'full' or phase == 'later-batch' else 7)
            assert observed['deliveries'] == (2 if phase == 'later-batch' else 1)
            assert movements == 1 and claim['state'] == 'committed'
            assert deliveries == [claim['delivery_id'], claim['delivery_id']]
            assert len(notices) == (1 if mode == 'full' or phase == 'later-batch' else 0)
            assert claim['payload_hash'] == changes.payload_hash(json.loads(claim['payload_json']))
            assert all(status == 200 and body['ok'] for status, body in responses.values()), responses
        finally:
            release.set()
            wh_release.set()
            delayed.join(20)
            if winner is not None:
                winner.join(20)


@pytest.mark.parametrize('first,retry', [(a, b) for a in ('qty', 'explicit', 'full')
    for b in ('qty', 'explicit', 'full') if a != b])
def test_original_modes_cannot_exchange_identical_frozen_batch(ship_env, first, retry):
    e = ship_env
    order, ids = e['create_order']([(101, 4, 'A')])
    with identity_http(e, order) as post:
        assert post(identity_form(first, ids[0], 4))[0] == 200
        before = changes.get_claim(e['master'], 'identity-request')
        assert json.loads(before['payload_json'])['request_mode'] == {'qty': 'legacy_qty'}.get(first, first)
        notifications = e['master'].execute('SELECT count(*) FROM notifications').fetchone()[0]
        status, body = post(identity_form(retry, ids[0], 4))
        assert e['stock']('A', 101) == 6
        assert e['master'].execute('SELECT count(*) FROM store_order_deliveries').fetchone()[0] == 1
        assert changes.get_claim(e['master'], 'identity-request') == before
        assert e['master'].execute('SELECT count(*) FROM notifications').fetchone()[0] == notifications
        assert status == 409 and not body['ok']


@pytest.mark.parametrize('legacy_scalar,tamper', [(True, False), (True, True), (False, False)])
def test_unmarked_claim_requires_provable_scalar_and_original_hash(ship_env, legacy_scalar, tamper):
    e = ship_env
    order, ids = e['create_order']([(101, 4, 'A')])
    detail = sop.get_order_detail(e['master'], order)
    # Seed the pre-mode shape without rewriting any immutable persisted hash.
    payload = {'order_id': order, 'source': 'A', 'lines': [[ids[0], '3.00']]}
    if legacy_scalar:
        payload['legacy_quantity'] = '3.00'
    digest = changes.payload_hash(payload) if not tamper else '0' * 64
    e['master'].execute("""INSERT INTO store_order_ship_claims
        (op_no,order_id,source_warehouse_code,payload_hash,payload_json,state,actor_id,created_at)
        VALUES ('unmarked',?,'A',?,?,'active',2,'now')""",
        (order, digest, changes.canonical_json(payload)))
    with sop.open_warehouse_db('A') as wh:
        item_id = wh.execute('SELECT id FROM items WHERE canonical_id=101').fetchone()[0]
    item = detail['order_items'][0]
    e['master'].execute("""INSERT INTO store_order_ship_claim_lines
        (op_no,order_id,order_item_id,quantity,base_line_revision,baseline_quantity,
         baseline_shipped,canonical_id,dc_item_id,unit,state)
        VALUES ('unmarked',?,?,3,?,4,0,101,?,'件','active')""",
        (order, ids[0], item['line_revision'], item_id))
    e['master'].commit()
    with identity_http(e, order) as post:
        status, _ = post(identity_form('qty' if legacy_scalar else 'explicit', ids[0], key='unmarked'))
        after = changes.get_claim(e['master'], 'unmarked')
        assert after['payload_json'] == changes.canonical_json(payload)
        assert after['payload_hash'] == digest
        if legacy_scalar and not tamper:
            assert status == 200 and after['state'] == 'committed'
            assert post(identity_form('qty', ids[0], key='unmarked'))[0] == 200
            assert post(identity_form('qty', ids[0], 2, 'unmarked'))[0] == 409
            assert e['stock']('A', 101) == 7
        else:
            assert status == 409 and after['state'] == 'active'
            assert e['stock']('A', 101) == 10
            assert e['master'].execute('SELECT count(*) FROM store_order_deliveries').fetchone()[0] == 0


@pytest.mark.parametrize('change', ['unit', 'binding', 'permission'])
def test_committed_identity_ignores_binding_but_rechecks_permission(ship_env, change):
    e = ship_env
    order, ids = e['create_order']([(101, 4, 'A')])
    with identity_http(e, order) as post:
        form = identity_form('explicit', ids[0])
        assert post(form)[0] == 200
        before = changes.get_claim(e['master'], 'identity-request')
        if change == 'permission':
            e['master'].execute("UPDATE warehouse_users SET role='viewer' WHERE user_id=2")
            e['master'].commit()
            with pytest.raises(ValueError):
                protocol._ship(e, order, 'A', 'identity-request', {ids[0]: 3})
        else:
            with sop.open_warehouse_db('A') as wh:
                if change == 'unit':
                    wh.execute("UPDATE items SET unit='箱' WHERE canonical_id=101")
                else:
                    wh.execute('UPDATE items SET is_active=0 WHERE canonical_id=101')
        status, _ = post(form)
        assert e['stock']('A', 101) == 7
        assert changes.get_claim(e['master'], 'identity-request') == before
        assert e['master'].execute('SELECT count(*) FROM store_order_deliveries').fetchone()[0] == 1
        assert status == (403 if change == 'permission' else 200)


def test_full_replays_only_original_source_batch_after_unpause(ship_env):
    e = ship_env
    order, ids = e['create_order']([(101, 4, 'A'), (102, 3, 'A'), (101, 2, 'B')])
    pid = seed_proposal(e['master'], order, [ids[1]])
    first = sop.ship_order_full(e['master'], order, 2, operator_dc_code='A', op_no='full-paused')
    e['master'].execute("UPDATE store_order_change_proposals SET status='withdrawn' WHERE id=?", (pid,))
    e['master'].commit()
    second = sop.ship_order_full(e['master'], order, 2, operator_dc_code='A', op_no='full-paused')
    assert first['delivery_id'] == second['delivery_id']
    assert second['status'] == 'approved'
    assert e['stock']('A', 101) == 6 and e['stock']('A', 102) == e['stock']('B', 101) == 10
    payload = json.loads(changes.get_claim(e['master'], 'full-paused')['payload_json'])
    assert payload == {'order_id': order, 'source': 'A', 'request_mode': 'full', 'lines': [[ids[0], '4.00']]}


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


@pytest.mark.parametrize('requested', [3, 4, 10])
def test_legacy_qty_retry_keeps_original_delivery_and_notification_count(ship_env, requested):
    e = ship_env
    oid, ids = e['create_order']([(101, 4, 'A')])
    login(e, user=2)
    url = f'/store-ordering/orders/{oid}/ship'
    data = {'op_no': 'legacy-qty-retry', 'qty': str(requested)}
    first = e['client'].post(url, data=data, headers={'X-Requested-With': 'fetch'})
    assert first.status_code == 200 and first.json['ok']
    deliveries = [tuple(r) for r in e['master'].execute('SELECT * FROM store_order_deliveries')]
    delivery_lines = [tuple(r) for r in e['master'].execute('SELECT * FROM store_order_delivery_items')]
    notifications = e['master'].execute('SELECT count(*) FROM notifications').fetchone()[0]
    repeat = e['client'].post(url, data=data, headers={'X-Requested-With': 'fetch'})
    assert repeat.status_code == 200 and repeat.json['ok'], repeat.get_data(as_text=True)
    assert e['stock']('A', 101) == 10 - min(requested, 4)
    assert [tuple(r) for r in e['master'].execute('SELECT * FROM store_order_deliveries')] == deliveries
    assert [tuple(r) for r in e['master'].execute('SELECT * FROM store_order_delivery_items')] == delivery_lines
    assert e['master'].execute('SELECT count(*) FROM notifications').fetchone()[0] == notifications
    assert e['master'].execute('SELECT shipped_quantity FROM store_order_items WHERE id=?', (ids[0],)).fetchone()[0] == min(requested, 4)


@pytest.mark.parametrize('mismatch', ['quantity', 'clipped_quantity', 'order', 'source', 'explicit_lines'])
def test_legacy_qty_key_still_rejects_changed_request(ship_env, mismatch):
    e = ship_env
    oid, ids = e['create_order']([(101, 4, 'A'), (102, 4, 'B')])
    login(e, user=2)
    requested = '10' if mismatch == 'clipped_quantity' else '3'
    data = {'op_no': 'legacy-qty-bound', 'qty': requested}
    url = f'/store-ordering/orders/{oid}/ship'
    assert e['client'].post(url, data=data, headers={'X-Requested-With': 'fetch'}).status_code == 200
    initial_a = e['stock']('A', 101)
    if mismatch in ('quantity', 'clipped_quantity'):
        data['qty'] = '11' if mismatch == 'clipped_quantity' else '2'
    elif mismatch == 'order':
        other, _ = e['create_order']([(101, 4, 'A')])
        url = f'/store-ordering/orders/{other}/ship'
    elif mismatch == 'source':
        login(e, user=3, warehouse=2)
    else:
        data = {'op_no': data['op_no'], f'shipped_items[{ids[0]}]': requested}
    rejected = e['client'].post(url, data=data, headers={'X-Requested-With': 'fetch'})
    assert rejected.status_code == 409 and rejected.json['ok'] is False
    assert e['stock']('A', 101) == initial_a and e['stock']('B', 102) == 10
    assert e['master'].execute('SELECT count(*) FROM store_order_deliveries').fetchone()[0] == 1
