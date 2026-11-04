"""Durable ship requests: real files, independent processes and deterministic gates."""
import multiprocessing
import sqlite3
from itertools import combinations, permutations

import pytest

from blueprints import order_changes_pure as changes
from blueprints import store_ordering_pure as sop
from tests import test_ship_op_protocol as protocol

ship_env = protocol.ship_env
_ship = protocol._ship


def _shipment_effects(e):
    master = {table: [tuple(row) for row in e['master'].execute(f'SELECT * FROM {table} ORDER BY rowid')]
        for table in ('store_order_items', 'store_order_deliveries', 'store_order_delivery_items',
                      'store_order_status_history', 'notifications', 'store_order_ship_claims',
                      'store_order_ship_claim_lines')}
    warehouses = {}
    for code in ('A', 'B'):
        with sqlite3.connect(e['wh_dir'] / f'{code}.db') as warehouse:
            warehouses[code] = {table: warehouse.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall()
                for table in ('items', 'stock_movements')}
    return master, warehouses


@pytest.mark.parametrize('source,actor,quantity', [('A', 2, 3), ('B', 3, 2)])
@pytest.mark.parametrize('initial', ['zeros-first', 'zeros-last', 'positive-only'])
def test_zero_line_transformations_preserve_inferred_shipment(ship_env, source, actor, quantity, initial):
    """Adding/removing/reordering same-order zero lines is a shipment no-op."""
    e = ship_env
    other = 'B' if source == 'A' else 'A'
    order, ids = e['create_order']([(101, 4, source), (101, 3, other), (102, 5, other)])
    positive = (ids[0], quantity)
    zeros = [(oid, 0) for oid in ids[1:]]
    submitted = (zeros + [positive] if initial == 'zeros-first' else
                 [positive] + zeros if initial == 'zeros-last' else [positive])
    result = sop.ship_order(e['master'], order, dict(submitted), shipped_by=actor, op_no='zero-transform')
    delivery = result['delivery_id']
    assert e['stock'](source, 101) == 10 - quantity
    assert e['stock'](other, 101) == e['stock'](other, 102) == 10
    assert [tuple(r) for r in e['master'].execute(
        'SELECT id,warehouse_code FROM store_order_deliveries')] == [(delivery, source)]
    assert [tuple(r) for r in e['master'].execute(
        'SELECT order_item_id,quantity,warehouse_code FROM store_order_delivery_items')] == [(ids[0], quantity, source)]
    assert e['master'].execute('SELECT count(*) FROM notifications').fetchone()[0] == 0
    before = _shipment_effects(e)
    variants = 0
    for size in range(len(zeros) + 1):
        for subset in combinations(ids[1:], size):
            for ordering in permutations([ids[0], *subset]):
                zero = (0, 0.0, '0', '0.00')[variants % 4]
                raw = {oid: quantity if oid == ids[0] else zero for oid in ordering}
                replay = sop.ship_order(e['master'], order, raw, shipped_by=actor, op_no='zero-transform')
                assert replay['delivery_id'] == delivery and replay['idempotent']
                assert _shipment_effects(e) == before
                variants += 1
    print({'source': source, 'initial': initial, 'variants': variants, 'delivery_id': delivery,
        'source_stock': e['stock'](source, 101), 'other_stock': e['stock'](other, 101),
        'deliveries': e['master'].execute('SELECT count(*) FROM store_order_deliveries').fetchone()[0],
        'notifications': e['master'].execute('SELECT count(*) FROM notifications').fetchone()[0]})


@pytest.mark.parametrize('replay', [False, True])
def test_inferred_shipment_rejects_foreign_zero_id(ship_env, replay):
    e = ship_env
    order, ids = e['create_order']([(101, 4, 'A')])
    _, foreign = e['create_order']([(102, 3, 'B')])
    if replay:
        sop.ship_order(e['master'], order, {ids[0]: 3}, shipped_by=2, op_no='foreign-zero')
    before = _shipment_effects(e)
    with pytest.raises(ValueError):
        sop.ship_order(e['master'], order, {ids[0]: 3, foreign[0]: 0}, shipped_by=2, op_no='foreign-zero')
    assert _shipment_effects(e) == before


@pytest.mark.parametrize('kind', ['two-positive', 'all-zero', 'explicit-other-positive'])
def test_execution_projection_retains_rejection_boundaries(ship_env, kind):
    e = ship_env
    order, ids = e['create_order']([(101, 4, 'A'), (102, 3, 'B')])
    raw = {ids[0]: 3, ids[1]: 2} if kind == 'two-positive' else {ids[0]: 0, ids[1]: 0}
    before = _shipment_effects(e)
    with pytest.raises(ValueError):
        if kind == 'explicit-other-positive':
            _ship(e, order, 'A', 'bad-execution', {ids[0]: 0, ids[1]: 2})
        else:
            sop.ship_order(e['master'], order, raw, shipped_by=2, op_no='bad-execution')
    assert _shipment_effects(e) == before


def seed_proposal(master, order_id, ids):
    proposal_id = master.execute("""INSERT INTO store_order_change_proposals
        (order_id,sequence,status,base_order_change_revision,proposed_by,reason,snapshot_json,required_warehouses_json,created_at)
        VALUES (?,1,'pending',0,4,'缺货','{}','["A"]','2026-11-03 12:00:00')""", (order_id,)).lastrowid
    for item_id in ids:
        master.execute("""INSERT INTO store_order_change_lines
            (proposal_id,order_id,order_item_id,operation,base_line_revision,baseline_quantity,baseline_shipped,remove_quantity)
            SELECT ?,order_id,id,'cancel',line_revision,quantity,shipped_quantity,quantity-shipped_quantity
            FROM store_order_items WHERE id=?""", (proposal_id, item_id))
    master.commit()
    return proposal_id


def test_pending_only_blocks_affected_explicit_batch_auto_skips(ship_env):
    e = ship_env
    order, ids = e['create_order']([(101, 4, 'A'), (102, 3, 'A')])
    seed_proposal(e['master'], order, [ids[0]])
    with pytest.raises(ValueError, match='整批拒绝'):
        _ship(e, order, 'A', 'blocked', {ids[0]: 1, ids[1]: 1})
    assert e['stock']('A', 101) == e['stock']('A', 102) == 10
    result = sop.ship_order_full(e['master'], order, 2, operator_dc_code='A', op_no='auto')
    assert result['order_items'][0]['shipped_quantity'] == 0
    assert result['order_items'][1]['shipped_quantity'] == 3
    assert result['status'] == 'approved'


@pytest.mark.parametrize('mismatch', ['quantity','subset','source','order'])
def test_complete_payload_cannot_reuse_key(ship_env, mismatch):
    e = ship_env
    order, ids = e['create_order']([(101, 4, 'A'), (102, 3, 'A')])
    _ship(e, order, 'A', 'bound', {ids[0]: 1, ids[1]: 1})
    other, _ = e['create_order']([(101, 4, 'A')])
    selected = {ids[0]: 2, ids[1]: 1} if mismatch == 'quantity' else {ids[0]: 1, ids[1]: 1}
    if mismatch == 'subset':
        selected.pop(ids[1])
    with pytest.raises(ValueError, match='不同完整发货请求'):
        _ship(e, other if mismatch == 'order' else order, 'B' if mismatch == 'source' else 'A', 'bound', selected)
    assert e['stock']('A', 101) == e['stock']('A', 102) == 9


def _paused_ship(master_path, wh_dir, order, item, phase, reached, release, result):
    import config
    config.WAREHOUSE_DB_DIR = wh_dir
    master = sqlite3.connect(master_path, timeout=15)
    master.row_factory = sqlite3.Row
    original = sop._apply_warehouse_shipment

    class PausedWarehouse(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            cursor = super().execute(sql, parameters)
            if phase == 'warehouse_lock' and 'UPDATE items SET quantity = quantity -' in sql:
                reached.set()
                if not release.wait(15):
                    raise RuntimeError('barrier timeout')
            return cursor

    def factory(code):
        return sqlite3.connect(wh_dir / f'{code}.db', timeout=15, factory=PausedWarehouse)

    def paused(*args, **kwargs):
        if phase == 'before_warehouse':
            reached.set()
            if not release.wait(15):
                raise RuntimeError('barrier timeout')
        value = original(*args, **kwargs)
        if phase == 'after_warehouse':
            reached.set()
            if not release.wait(15):
                raise RuntimeError('barrier timeout')
        return value
    sop._apply_warehouse_shipment = paused
    try:
        sop.ship_partial(master, factory, order_id=order, operator_dc_code='A',
                         op_no='process-request', line_qty={item: 2}, shipped_by=2)
        result.put({'outcome': 'committed'})
    except Exception as exc:
        result.put({'outcome': 'error', 'type': type(exc).__name__, 'message': str(exc)})
    finally:
        master.close()


def start_ship(e, order, item, phase):
    ctx = multiprocessing.get_context('spawn')
    reached, release, result = ctx.Event(), ctx.Event(), ctx.Queue()
    worker = ctx.Process(target=_paused_ship, args=(e['master_path'], e['wh_dir'], order, item, phase, reached, release, result))
    worker.start()
    assert reached.wait(15), 'writer did not reach barrier'
    return worker, release, result


@pytest.mark.parametrize('phase', ['before_warehouse','after_warehouse','warehouse_lock'])
def test_recovery_fence_blocks_late_executor_and_cancel(ship_env, phase):
    e = ship_env
    order, ids = e['create_order']([(101, 4, 'A')])
    worker, release, result = start_ship(e, order, ids[0], phase)
    try:
        with pytest.raises(ValueError, match='发货处理中'):
            sop.cancel_order(e['master'], order, 4, 'cannot cross claim')
        with pytest.raises(sqlite3.IntegrityError, match='occupied'):
            seed_proposal(e['master'], order, ids)
        e['master'].rollback()
        assert e['stock']('A', 101) == (8 if phase == 'after_warehouse' else 10)

        def recovery_factory(code):
            # This call is after the committed master fence. Let a warehouse-lock
            # holder finish, then acquire the real warehouse lock and reverse it.
            if phase == 'warehouse_lock':
                release.set()
            return sqlite3.connect(e['wh_dir'] / f'{code}.db', timeout=15)

        reversed_rows = changes.recover_shipment(e['master'], recovery_factory, 'process-request')
        assert len(reversed_rows) == (0 if phase == 'before_warehouse' else 1)
        release.set()
        worker.join(15)
        execution = result.get(timeout=2)
        observed = {'phase': phase, 'stock': e['stock']('A', 101),
                    'claim': changes.get_claim(e['master'], 'process-request')['state'],
                    'deliveries': e['master'].execute('SELECT count(*) FROM store_order_deliveries').fetchone()[0],
                    'executor': execution, 'process_exit': worker.exitcode}
        print(observed)
        assert observed['stock'] == 10
        assert observed['deliveries'] == 0
        assert observed['claim'] == 'aborted'
        assert observed['process_exit'] == 0
        assert execution['outcome'] == 'error' and execution['type'] == 'ValueError'
        assert changes.recover_shipment(e['master'], recovery_factory, 'process-request') == []
        with pytest.raises(ValueError, match='不可复用'):
            _ship(e, order, 'A', 'process-request', {ids[0]: 2})
        assert sop.cancel_order(e['master'], order, 4, 'safe now')['status'] == 'cancelled'
    finally:
        release.set()
        worker.join(15)
        if worker.is_alive():
            worker.terminate()
            worker.join()


def test_finalize_wins_recovery_cannot_reverse(ship_env):
    e = ship_env
    order, ids = e['create_order']([(101, 4, 'A')])
    _ship(e, order, 'A', 'committed', {ids[0]: 2})
    assert changes.recover_shipment(e['master'], sop.open_warehouse_db, 'committed') == []
    assert changes.get_claim(e['master'], 'committed')['state'] == 'committed'
    assert e['stock']('A', 101) == 8


def test_crash_after_reversal_before_master_settlement_can_resume(ship_env, monkeypatch):
    e = ship_env
    order, ids = e['create_order']([(101, 4, 'A')])
    original = sop._generate_delivery_no
    monkeypatch.setattr(sop, '_generate_delivery_no', lambda _: (_ for _ in ()).throw(RuntimeError('crash')))
    with pytest.raises(RuntimeError, match='crash'):
        _ship(e, order, 'A', 'gap', {ids[0]: 2})
    monkeypatch.setattr(sop, '_generate_delivery_no', original)

    class CrashBeforeSettlement(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            if "SET state='aborted'" in sql:
                raise RuntimeError('master settlement crash')
            return super().execute(sql, parameters)
    broken = sqlite3.connect(e['master_path'], factory=CrashBeforeSettlement)
    broken.row_factory = sqlite3.Row
    with pytest.raises(RuntimeError, match='settlement crash'):
        changes.recover_shipment(broken, sop.open_warehouse_db, 'gap')
    broken.close()
    assert e['stock']('A', 101) == 10
    assert changes.get_claim(e['master'], 'gap')['state'] == 'recovering'
    assert changes.recover_shipment(e['master'], sop.open_warehouse_db, 'gap') == []
    assert changes.get_claim(e['master'], 'gap')['state'] == 'aborted'
    assert e['stock']('A', 101) == 10


def test_concurrent_same_payload_retry_settles_once(ship_env):
    e = ship_env
    order, ids = e['create_order']([(101, 4, 'A')])
    worker, release, result = start_ship(e, order, ids[0], 'after_warehouse')
    try:
        settled = _ship(e, order, 'A', 'process-request', {ids[0]: 2})
        release.set()
        worker.join(15)
        assert worker.exitcode == 0 and result.get(timeout=2)['outcome'] == 'committed'
        assert e['stock']('A', 101) == 8
        assert e['master'].execute('SELECT count(*) FROM store_order_deliveries').fetchone()[0] == 1
        assert e['master'].execute('SELECT shipped_quantity FROM store_order_items WHERE id=?', (ids[0],)).fetchone()[0] == 2
        assert changes.get_claim(e['master'], 'process-request')['delivery_id'] == settled['delivery_id']
    finally:
        release.set()
        worker.join(15)
        if worker.is_alive():
            worker.terminate()
            worker.join()


def test_schema_pending_unique_foreign_key_and_terminal_guards(ship_env):
    e = ship_env
    master = e['master']
    master.execute('PRAGMA foreign_keys=ON')
    order, ids = e['create_order']([(101, 4, 'A')])
    pid = seed_proposal(master, order, ids)
    with pytest.raises(sqlite3.IntegrityError, match='UNIQUE'):
        master.execute("INSERT INTO store_order_change_proposals(order_id,sequence,status,base_order_change_revision,proposed_by,reason,snapshot_json,required_warehouses_json,created_at) VALUES(?,2,'pending',0,4,'x','{}','[]','now')", (order,))
    master.rollback()
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        master.execute("UPDATE store_order_change_proposals SET snapshot_json='[]' WHERE id=?", (pid,))
    master.rollback()
    with pytest.raises(sqlite3.IntegrityError, match='CHECK'):
        master.execute("INSERT INTO store_order_change_lines(proposal_id,order_id,order_item_id,operation,base_line_revision,baseline_quantity,baseline_shipped,remove_quantity) VALUES(?,?,?,'cancel',0,4,0,5)", (pid, order, ids[0]))
    master.rollback()
    with pytest.raises(sqlite3.IntegrityError, match='FOREIGN KEY'):
        master.execute("INSERT INTO store_order_change_events(proposal_id,actor_id,to_status,note,created_at) VALUES(999,4,'stale','bad','now')")
    master.rollback()
    master.execute("UPDATE store_order_change_proposals SET status='withdrawn' WHERE id=?", (pid,))
    master.commit()
    with pytest.raises(sqlite3.IntegrityError, match='cannot reopen'):
        master.execute("UPDATE store_order_change_proposals SET status='pending' WHERE id=?", (pid,))
    master.rollback()


def test_automatic_full_retry_uses_original_request_not_new_remaining(ship_env):
    e = ship_env
    order, _ = e['create_order']([(101, 4, 'A')])
    first = sop.ship_order_full(e['master'], order, 2, operator_dc_code='A', op_no='auto-original')
    second = sop.ship_order_full(e['master'], order, 2, operator_dc_code='A', op_no='auto-original')
    assert first['delivery_id'] == second['delivery_id'] and second['idempotent']
    assert e['stock']('A', 101) == 6


def test_unclaimed_legacy_movement_and_reversal_cannot_be_adopted(ship_env):
    e = ship_env
    order, ids = e['create_order']([(101, 4, 'A')])
    key = f'old-request:{ids[0]}'
    with sop.open_warehouse_db('A') as wh:
        item_id = wh.execute('SELECT id FROM items WHERE canonical_id=101').fetchone()[0]
        for op, delta in ((key, -2), ('REV:' + key, 2)):
            wh.execute("INSERT INTO stock_movements(item_id,action,delta,note,created_at,op_no) VALUES(?,'门店订货出库',?,'legacy','now',?)", (item_id, delta, op))
    with pytest.raises(ValueError, match='旧仓库请求或REV'):
        _ship(e, order, 'A', 'old-request', {ids[0]: 3})
    assert changes.get_claim(e['master'], 'old-request') is None
    assert e['stock']('A', 101) == 10


def test_effective_quantity_update_cannot_cross_active_claim(ship_env):
    e = ship_env
    order, ids = e['create_order']([(101, 4, 'A')])
    worker, release, result = start_ship(e, order, ids[0], 'before_warehouse')
    try:
        with pytest.raises(sqlite3.IntegrityError, match='occupied'):
            e['master'].execute('UPDATE store_order_items SET quantity=2,line_revision=line_revision+1 WHERE id=?', (ids[0],))
        e['master'].rollback()
        release.set()
        worker.join(15)
        assert worker.exitcode == 0 and result.get(timeout=2)['outcome'] == 'committed'
        assert e['master'].execute('SELECT quantity FROM store_order_items WHERE id=?', (ids[0],)).fetchone()[0] == 4
        assert e['stock']('A', 101) == 8
    finally:
        e['master'].rollback()
        release.set()
        worker.join(15)
        if worker.is_alive():
            worker.terminate()
            worker.join()


def test_rev_tombstone_checked_before_warehouse_deduction(ship_env):
    e = ship_env
    order, ids = e['create_order']([(101, 4, 'A')])
    worker, release, result = start_ship(e, order, ids[0], 'before_warehouse')
    try:
        with sop.open_warehouse_db('A') as wh:
            item_id = wh.execute('SELECT id FROM items WHERE canonical_id=101').fetchone()[0]
            wh.execute("INSERT INTO stock_movements(item_id,action,delta,note,created_at,op_no) VALUES(?,'门店订货出库',2,'permanent reversal','now',?)", (item_id, f'REV:process-request:{ids[0]}'))
        release.set()
        worker.join(15)
        assert worker.exitcode == 0
        execution = result.get(timeout=2)
        assert e['stock']('A', 101) == 10
        assert changes.get_claim(e['master'], 'process-request')['state'] == 'aborted'
        assert execution['outcome'] == 'error' and execution['type'] == 'ValueError'
    finally:
        release.set()
        worker.join(15)
        if worker.is_alive():
            worker.terminate()
            worker.join()


def test_ship_permission_reloaded_after_binding_resolution(ship_env):
    e = ship_env
    order, ids = e['create_order']([(101, 4, 'A')])
    factory_calls = 0

    def factory(code):
        nonlocal factory_calls
        factory_calls += 1
        if factory_calls == 1:
            with sqlite3.connect(e['master_path']) as other:
                other.execute("UPDATE warehouse_users SET role='viewer' WHERE user_id=2")
        return sop.open_warehouse_db(code)

    before_changes = e['master'].total_changes
    failure = None
    try:
        sop.ship_partial(e['master'], factory, order_id=order, operator_dc_code='A',
                         op_no='revoked-ship', line_qty={ids[0]: 2}, shipped_by=2)
    except Exception as exc:
        failure = exc
    observed = {'stock': e['stock']('A', 101),
                'claim': changes.get_claim(e['master'], 'revoked-ship'),
                'deliveries': e['master'].execute('SELECT count(*) FROM store_order_deliveries').fetchone()[0],
                'master_writes': e['master'].total_changes - before_changes,
                'exception_type': type(failure).__name__ if failure is not None else None}
    print(observed)
    assert observed['stock'] == 10
    assert observed['deliveries'] == 0 and observed['claim'] is None
    assert observed['master_writes'] == 0
    assert isinstance(failure, ValueError)


@pytest.mark.parametrize('record', ['delivery', 'negative', 'rev', 'negative-rev'])
@pytest.mark.parametrize('already', [1, 4])
def test_genuine_unclaimed_keys_stay_manual_even_when_full_selection_empty(ship_env, record, already):
    e = ship_env
    order, ids = e['create_order']([(101, 4, 'A')])
    key = 'genuine-old'
    with sop.open_warehouse_db('A') as wh:
        local = wh.execute('SELECT id FROM items WHERE canonical_id=101').fetchone()[0]
        for op, delta in ((key + ':' + str(ids[0]), -already),
                          ('REV:' + key + ':' + str(ids[0]), already)):
            if (delta < 0 and record in ('negative', 'negative-rev') or
                    delta > 0 and record in ('rev', 'negative-rev')):
                wh.execute("""INSERT INTO stock_movements
                    (item_id,action,delta,note,created_at,op_no)
                    VALUES (?,'门店订货出库',?,'legacy','now',?)""", (local, delta, op))
    if record == 'delivery':
        item = sop.get_order_detail(e['master'], order)['order_items'][0]
        sop._insert_master_batch(e['master'], order, 'A', [(ids[0], item, already)],
            {ids[0]: key + ':' + str(ids[0])}, {ids[0]: local}, key, 2, None, 'now')
    else:
        e['master'].execute('UPDATE store_order_items SET shipped_quantity=? WHERE id=?', (already, ids[0]))
    e['master'].commit()
    before = [tuple(r) for r in e['master'].execute('SELECT * FROM store_order_deliveries')]
    with pytest.raises(ValueError):
        sop.ship_order_full(e['master'], order, 2, operator_dc_code='A', op_no=key)
    assert e['stock']('A', 101) == 10
    assert changes.get_claim(e['master'], key) is None
    assert [tuple(r) for r in e['master'].execute('SELECT * FROM store_order_deliveries')] == before


@pytest.mark.parametrize('change', ['missing', 'unit'])
def test_new_batch_binding_preparation_failure_never_registers(ship_env, change):
    e = ship_env
    order, ids = e['create_order']([(101, 4, 'A')])
    with sop.open_warehouse_db('A') as wh:
        if change == 'missing':
            wh.execute('UPDATE items SET is_active=0 WHERE canonical_id=101')
        else:
            wh.execute("UPDATE items SET unit='箱' WHERE canonical_id=101")
    with pytest.raises(ValueError):
        _ship(e, order, 'A', 'bad-binding', {ids[0]: 3})
    assert e['stock']('A', 101) == 10
    assert changes.get_claim(e['master'], 'bad-binding') is None
    assert e['master'].execute('SELECT count(*) FROM store_order_deliveries').fetchone()[0] == 0


def test_preparation_factory_does_not_hold_master_write_lock(ship_env):
    e = ship_env
    order, ids = e['create_order']([(101, 4, 'A')])
    calls = []

    def factory(code):
        if not calls:
            assert not e['master'].in_transaction
            with sqlite3.connect(e['master_path'], timeout=0) as other:
                other.execute('BEGIN IMMEDIATE')
                other.rollback()
        calls.append(code)
        return sop.open_warehouse_db(code)

    sop.ship_partial(e['master'], factory, order_id=order, operator_dc_code='A',
        op_no='outside-master', line_qty={ids[0]: 3}, shipped_by=2)
    assert calls and e['stock']('A', 101) == 7


def test_new_full_plan_changed_after_preparation_requires_clean_retry(ship_env):
    e = ship_env
    order, ids = e['create_order']([(101, 4, 'A'), (102, 3, 'A')])
    pid = seed_proposal(e['master'], order, [ids[1]])
    calls = []

    def factory(code):
        if not calls:
            with sqlite3.connect(e['master_path']) as other:
                other.execute("UPDATE store_order_change_proposals SET status='withdrawn' WHERE id=?", (pid,))
        calls.append(code)
        return sop.open_warehouse_db(code)

    with pytest.raises(ValueError):
        sop.ship_partial(e['master'], factory, order_id=order, operator_dc_code='A',
            op_no='changed-plan', line_qty={}, shipped_by=2, request_mode='full')
    assert e['stock']('A', 101) == e['stock']('A', 102) == 10
    assert changes.get_claim(e['master'], 'changed-plan') is None
    assert e['master'].execute('SELECT count(*) FROM store_order_deliveries').fetchone()[0] == 0
    sop.ship_order_full(e['master'], order, 2, operator_dc_code='A', op_no='changed-plan')
    assert e['stock']('A', 101) == 6 and e['stock']('A', 102) == 7


def test_caller_owned_transaction_is_rejected_before_warehouse_access(ship_env):
    e = ship_env
    order, ids = e['create_order']([(101, 4, 'A')])
    e['master'].execute('BEGIN')
    e['master'].execute('SELECT * FROM store_orders').fetchall()
    factories = []
    with pytest.raises(ValueError):
        sop.ship_partial(e['master'], lambda code: factories.append(code), order_id=order,
            operator_dc_code='A', op_no='deferred-caller', line_qty={ids[0]: 3}, shipped_by=2)
    assert e['master'].in_transaction and not factories
    e['master'].rollback()
    assert e['stock']('A', 101) == 10
    assert changes.get_claim(e['master'], 'deferred-caller') is None


@pytest.mark.parametrize('replay', [False, True])
def test_raw_explicit_foreign_zero_id_never_bypasses_membership(ship_env, replay):
    e = ship_env
    order, ids = e['create_order']([(101, 4, 'A')])
    if replay:
        _ship(e, order, 'A', 'raw-membership', {ids[0]: 3})
    before = changes.get_claim(e['master'], 'raw-membership')
    stock = e['stock']('A', 101)
    deliveries = [tuple(r) for r in e['master'].execute('SELECT * FROM store_order_deliveries')]
    with pytest.raises(ValueError):
        _ship(e, order, 'A', 'raw-membership', {999999: 0, ids[0]: 3})
    assert changes.get_claim(e['master'], 'raw-membership') == before
    assert e['stock']('A', 101) == stock
    assert [tuple(r) for r in e['master'].execute('SELECT * FROM store_order_deliveries')] == deliveries


@pytest.mark.parametrize('change', ['unit', 'binding'])
def test_active_executor_keeps_frozen_binding_after_preparation(ship_env, change):
    e = ship_env
    order, ids = e['create_order']([(101, 4, 'A')])
    worker, release, result = start_ship(e, order, ids[0], 'before_warehouse')
    try:
        with sop.open_warehouse_db('A') as wh:
            if change == 'unit':
                wh.execute("UPDATE items SET unit='箱' WHERE canonical_id=101")
            else:
                wh.execute('UPDATE items SET is_active=0 WHERE canonical_id=101')
        release.set()
        worker.join(15)
        execution = result.get(timeout=2)
        assert worker.exitcode == 0
        assert e['stock']('A', 101) == 10
        assert e['master'].execute('SELECT count(*) FROM store_order_deliveries').fetchone()[0] == 0
        assert changes.get_claim(e['master'], 'process-request')['state'] == 'aborted'
        assert execution['outcome'] == 'error'
        assert execution['type'] == ('FulfillmentConflictError' if change == 'unit' else 'FulfillmentMissingError')
    finally:
        release.set()
        worker.join(15)
        if worker.is_alive():
            worker.terminate()
            worker.join()


@pytest.mark.parametrize('mode', ['full', 'legacy_qty'])
@pytest.mark.parametrize('state', ['recovering', 'aborted'])
def test_raw_wrappers_cannot_revive_fenced_key(ship_env, monkeypatch, mode, state):
    e = ship_env
    order, _ = e['create_order']([(101, 4, 'A')])

    def ship():
        if mode == 'full':
            return sop.ship_order_full(e['master'], order, 2, operator_dc_code='A', op_no='fenced-raw')
        return sop.ship_legacy_quantity(e['master'], order_id=order, operator_dc_code='A',
            op_no='fenced-raw', quantity=3, shipped_by=2)

    def crash(*args, **kwargs):
        raise RuntimeError('simulated crash before warehouse')

    with monkeypatch.context() as patch:
        patch.setattr(sop, '_apply_warehouse_shipment', crash)
        with pytest.raises(RuntimeError):
            ship()
    with pytest.raises(RuntimeError):
        changes.recover_shipment(e['master'], crash, 'fenced-raw')
    if state == 'aborted':
        changes.recover_shipment(e['master'], sop.open_warehouse_db, 'fenced-raw')
    before = changes.get_claim(e['master'], 'fenced-raw')
    with pytest.raises(ValueError):
        ship()
    assert before['state'] == state and changes.get_claim(e['master'], 'fenced-raw') == before
    assert e['stock']('A', 101) == 10
    assert e['master'].execute('SELECT count(*) FROM store_order_deliveries').fetchone()[0] == 0
