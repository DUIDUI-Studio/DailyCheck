"""Order changes: atomic whole-proposal commands and fulfillment compatibility.

Master transactions own commercial revisions, proposals and ship occupancy. Warehouse
transactions own stock; a recovering claim fences executors without holding both locks.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from blueprints._helpers import now


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def payload_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def quantity(value: Any) -> str:
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            raise ValueError('发货数量必须为非负有限数')
        return str(result.quantize(Decimal('.01'), rounding=ROUND_HALF_UP))
    except (InvalidOperation, TypeError):
        raise ValueError('无效发货数量') from None


def has_tables(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='store_order_change_proposals'").fetchone() is not None


def change_history(conn: sqlite3.Connection, order_id: int) -> list[dict]:
    if not has_tables(conn):
        return []
    return [dict(r) for r in conn.execute(
        """SELECT id,order_id,sequence,status,proposed_by,reason,created_at,
                  decided_by,decided_at,decision_note,stale_reason
           FROM store_order_change_proposals WHERE order_id=? ORDER BY sequence DESC""", (order_id,))]


def blocked_lines(conn: sqlite3.Connection, order_id: int) -> dict[int, str]:
    if not has_tables(conn):
        return {}
    blocked = {int(r[0]): '变更方案待确认，暂停此行发货' for r in conn.execute(
        """SELECT l.order_item_id FROM store_order_change_lines l
           JOIN store_order_change_proposals p ON p.id=l.proposal_id
           WHERE p.order_id=? AND p.status='pending'""", (order_id,))}
    for r in conn.execute(
        "SELECT order_item_id,state FROM store_order_ship_claim_lines WHERE order_id=? AND state IN ('active','recovering')", (order_id,)):
        blocked[int(r[0])] = '发货处理中，需恢复/对账' if r[1] == 'recovering' else '发货处理中'
    return blocked


def assert_unoccupied(conn: sqlite3.Connection, order_id: int, item_ids: list[int] | None = None) -> None:
    query = "SELECT 1 FROM store_order_ship_claim_lines WHERE order_id=? AND state IN ('active','recovering')"
    params: list = [order_id]
    if item_ids is not None:
        if not item_ids:
            return
        query += ' AND order_item_id IN (' + ','.join('?' for _ in item_ids) + ')'
        params.extend(item_ids)
    if conn.execute(query, params).fetchone():
        raise ValueError('订单行发货处理中，须恢复/对账后再修改')


def assert_ship_permission(conn: sqlite3.Connection, order: dict, actor_id: int, source: str) -> None:
    """Same DC staff ladder as HTTP ship, freshly read inside the claim lock."""
    row = conn.execute(
        """SELECT u.is_admin,u.is_active,u.tenant_id,w.tenant_id,w.warehouse_type,wu.role
           FROM users u JOIN warehouses w ON w.code=?
           LEFT JOIN warehouse_users wu ON wu.user_id=u.id AND wu.warehouse_id=w.id
           WHERE u.id=?""", (source, actor_id)).fetchone()
    if row is None or not row[1] or row[4] != 'distribution_center':
        raise ValueError('无本仓发货权限')
    if row[0]:
        return
    if row[2] != row[3] or row[5] not in ('staff', 'manager', 'admin'):
        raise ValueError('无本仓发货权限')
    target = conn.execute('SELECT tenant_id FROM warehouses WHERE code=?', (order['store_warehouse_code'],)).fetchone()
    if target is None or target[0] != row[2]:
        raise ValueError('订单目标仓不在当前租户')


def ship_payload(order_id: int, source: str, line_qty: dict[int, float], *,
                 legacy_quantity: float | str | None = None,
                 request_mode: str | None = 'explicit') -> dict:
    normalized: dict[int, str] = {}
    for key, raw in line_qty.items():
        try:
            item_id = int(key)
        except (ValueError, TypeError):
            raise ValueError('无效发货明细') from None
        value = quantity(raw)
        if Decimal(value) > 0:
            if item_id in normalized:
                raise ValueError('重复发货明细')
            normalized[item_id] = value
    if not normalized:
        raise ValueError('本批发货数量全部为空，请至少填写一项')
    payload = {'order_id': int(order_id), 'source': source, 'lines': sorted(normalized.items())}
    if request_mode is not None:
        payload['request_mode'] = request_mode
    if legacy_quantity is not None:
        payload['legacy_quantity'] = quantity(legacy_quantity)
    return payload


def resolve_ship_payload(conn: sqlite3.Connection, *, op_no: str, order_id: int,
                         source: str, line_qty: dict[int, float],
                         legacy_quantity: float | None = None,
                         request_mode: str = 'explicit') -> tuple[dict, dict | None]:
    """Adjudicate raw intent against the current claim under the master write lock.

    Full/scalar replay the original batch; explicit input must supply its entire
    original map. Unmarked non-scalar claims cannot prove their wire mode.
    """
    claim = get_claim(conn, op_no)
    stored_mode = request_mode
    if claim is not None:
        saved = json.loads(claim['payload_json'])
        if claim['payload_hash'] != payload_hash(saved):
            raise ValueError('持久发货请求与原hash不一致，须人工核对')
        stored_mode = saved.get('request_mode')
        if stored_mode is None:
            if 'legacy_quantity' not in saved:
                raise ValueError('旧发货请求缺少原始模式，须人工核对，不可猜测重放')
            matches_mode = request_mode == 'legacy_qty'
        else:
            matches_mode = stored_mode == request_mode
        if (not matches_mode or saved['order_id'] != order_id or saved['source'] != source or
                (request_mode == 'legacy_qty' and
                 saved.get('legacy_quantity') != quantity(legacy_quantity))):
            raise ValueError('op_no 已用于不同完整发货请求或原始模式，拒绝复用')
        if request_mode in ('full', 'legacy_qty'):
            line_qty = {int(oid): qty for oid, qty in saved['lines']}
    payload = ship_payload(order_id, source, line_qty, legacy_quantity=legacy_quantity,
                           request_mode=stored_mode)
    return payload, get_claim(conn, op_no, payload)


def get_claim(conn: sqlite3.Connection, op_no: str, payload: dict | None = None) -> dict | None:
    row = conn.execute('SELECT * FROM store_order_ship_claims WHERE op_no=?', (op_no,)).fetchone()
    if row is None:
        return None
    claim = dict(row)
    if payload is not None and claim['payload_hash'] != payload_hash(payload):
        raise ValueError('op_no 已用于不同完整发货请求，拒绝复用')
    return claim


def claim_lines(conn: sqlite3.Connection, op_no: str) -> list[dict]:
    return [dict(r) for r in conn.execute('SELECT * FROM store_order_ship_claim_lines WHERE op_no=? ORDER BY order_item_id', (op_no,))]


def register_claim(conn: sqlite3.Connection, *, op_no: str, order: dict, payload: dict,
                   selected: list, frozen: dict[int, int], actor_id: int) -> dict:
    """Caller holds BEGIN IMMEDIATE; validates the entire batch before any INSERT."""
    assert_ship_permission(conn, order, actor_id, payload['source'])
    if order['status'] != 'approved':
        raise ValueError(f"order must be approved to ship, got {order['status']!r}")
    blocked = blocked_lines(conn, order['id'])
    if any(oid in blocked for oid, _, _ in selected):
        raise ValueError('本批含暂停或发货处理中明细，整批拒绝')
    conn.execute(
        """INSERT INTO store_order_ship_claims
           (op_no,order_id,source_warehouse_code,payload_hash,payload_json,state,actor_id,created_at)
           VALUES (?,?,?,?,?,'active',?,?)""",
        (op_no, order['id'], payload['source'], payload_hash(payload), canonical_json(payload), actor_id, now()))
    for oid, item, qty in selected:
        conn.execute(
            """INSERT INTO store_order_ship_claim_lines
               (op_no,order_id,order_item_id,quantity,base_line_revision,baseline_quantity,
                baseline_shipped,canonical_id,dc_item_id,unit,state)
               VALUES (?,?,?,?,?,?,?,?,?,?,'active')""",
            (op_no, order['id'], oid, qty, item['line_revision'], item['quantity'],
             item['shipped_quantity'], item['canonical_id'], frozen[oid], item['unit']))
    return get_claim(conn, op_no, payload)


def assert_active_claim(conn: sqlite3.Connection, op_no: str, *, order_id: int,
                        source: str, selected: list) -> list[dict]:
    claim = get_claim(conn, op_no)
    if claim is None or claim['state'] != 'active':
        raise ValueError('发货占用已恢复或终结，迟到请求不可执行')
    saved = json.loads(claim['payload_json'])
    payload = ship_payload(order_id, source, {oid: qty for oid, _, qty in selected},
                           legacy_quantity=saved.get('legacy_quantity'),
                           request_mode=saved.get('request_mode'))
    get_claim(conn, op_no, payload)
    rows = claim_lines(conn, op_no)
    items = {oid: item for oid, item, _ in selected}
    for line in rows:
        row = conn.execute('SELECT * FROM store_order_items WHERE id=?', (line['order_item_id'],)).fetchone()
        if row is None:
            raise ValueError('发货冻结明细已变更')
        item = dict(row)
        header_source = conn.execute('SELECT dc_warehouse_code FROM store_orders WHERE id=?', (order_id,)).fetchone()[0]
        if (item['line_revision'] != line['base_line_revision'] or
            item['canonical_id'] != line['canonical_id'] or item['unit'] != line['unit'] or
            (item['source_warehouse_code'] or header_source) != source or
            (item['dc_item_id'] is not None and item['dc_item_id'] != line['dc_item_id']) or
            item['quantity'] != line['baseline_quantity'] or
            item['shipped_quantity'] != line['baseline_shipped'] or
            line['order_item_id'] not in items):
            raise ValueError('发货冻结明细或版本已变更')
    # Proposal creation must use assert_unoccupied; this also protects bad seed/config changes.
    if conn.execute("""SELECT 1 FROM store_order_change_lines l JOIN store_order_change_proposals p
        ON p.id=l.proposal_id WHERE p.status='pending' AND l.order_item_id IN
        (SELECT order_item_id FROM store_order_ship_claim_lines WHERE op_no=?)""", (op_no,)).fetchone():
        raise ValueError('发货行有待确认方案')
    return rows


def recover_shipment(conn: sqlite3.Connection, wh_conn_factory, op_no: str) -> list[dict]:
    """Fence master first, then reverse under warehouse lock, then settle master.

    No lease, timeout or exception releases occupancy. Re-entry after either commit
    is safe; committed delivery wins permanently over recovery.
    """
    if conn.in_transaction:
        raise ValueError('发货恢复必须在独立master事务中执行')
    conn.execute('BEGIN IMMEDIATE')
    try:
        claim = get_claim(conn, op_no)
        if claim is None:
            raise ValueError('无持久发货占用；旧请求须按人工孤儿核对流程处理')
        if claim['state'] in ('committed', 'aborted'):
            conn.commit()
            return []
        prefix = op_no + ':'
        if conn.execute('SELECT 1 FROM store_order_delivery_items WHERE substr(op_no,1,?)=? LIMIT 1', (len(prefix), prefix)).fetchone():
            raise ValueError('配送记录与claim状态不一致，保留占用待人工核对')
        conn.execute("UPDATE store_order_ship_claims SET state='recovering' WHERE op_no=? AND state='active'", (op_no,))
        lines = claim_lines(conn, op_no)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    reversed_rows = []
    warehouse = wh_conn_factory(claim['source_warehouse_code'])
    try:
        warehouse.row_factory = sqlite3.Row
        warehouse.execute('BEGIN IMMEDIATE')
        for line in lines:
            key = f"{op_no}:{line['order_item_id']}"
            if warehouse.execute('SELECT 1 FROM stock_movements WHERE op_no=?', ('REV:' + key,)).fetchone():
                continue
            movements = warehouse.execute('SELECT * FROM stock_movements WHERE op_no=?', (key,)).fetchall()
            if not movements:
                continue
            if (len(movements) != 1 or movements[0]['item_id'] != line['dc_item_id'] or
                abs(float(movements[0]['delta']) + line['quantity']) > 1e-9):
                raise ValueError('仓库流水与冻结发货请求不一致，保持recovering待人工核对')
            movement = movements[0]
            if warehouse.execute('UPDATE items SET quantity=quantity+? WHERE id=?',
                                 (line['quantity'], line['dc_item_id'])).rowcount != 1:
                raise ValueError('冻结库存行丢失，保持recovering')
            warehouse.execute(
                """INSERT INTO stock_movements(item_id,action,delta,note,created_at,op_no)
                   VALUES (?,?,?,?,?,?)""", (line['dc_item_id'], movement['action'], line['quantity'],
                   f'红字冲销 孤儿发货 op_no={key}', now(), 'REV:' + key))
            reversed_rows.append({'warehouse_code': claim['source_warehouse_code'],
                                  'movement_id': movement['id'], 'item_id': line['dc_item_id'],
                                  'quantity': line['quantity'], 'op_no': key})
        warehouse.commit()
    except Exception:
        warehouse.rollback()
        raise
    finally:
        warehouse.close()
    conn.execute('BEGIN IMMEDIATE')
    try:
        conn.execute("UPDATE store_order_ship_claims SET state='aborted',completed_at=? WHERE op_no=? AND state='recovering'", (now(), op_no))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return reversed_rows


def snapshot_product(conn: sqlite3.Connection, warehouse: sqlite3.Connection,
                     canonical_id: int, item_id: int) -> dict:
    """Server values only. Preserve unit price precision; round extended amounts."""
    canonical = dict(conn.execute('SELECT * FROM canonical_items WHERE id=?', (canonical_id,)).fetchone())
    local = dict(warehouse.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone())
    sell = Decimal(str(local.get('selling_price') or 0))
    origin = 'selling_price' if sell > 0 else 'unit_cost'
    price = sell if sell > 0 else Decimal(str(local.get('unit_cost') or 0))
    if not price.is_finite() or price < 0:
        raise ValueError('来源仓单价无效，无法锁定订单价格')
    product = {key: canonical.get(key) for key in ('name', 'canonical_sku', 'unit', 'aux_unit', 'aux_rate')}
    return {'unit_price_snapshot': str(price), 'price_snapshot_origin': origin,
            'product_snapshot': canonical_json(product), 'unit_price': float(price)}


def project_order(order: dict) -> None:
    """Pure read projection: no lazy migration, price guessing or state writes."""
    transfer = order.get('flow_type') == 'dc_transfer'
    subtotal = Decimal(0)
    known = True
    for item in order['order_items']:
        raw = item.get('unit_price_snapshot')
        item['price_state'] = 'not_applicable' if transfer else ('locked' if raw is not None else 'unknown')
        item['unit_price'] = float(raw) if raw is not None and not transfer else None
        item['line_amount'] = str(money(Decimal(str(item['quantity'])) * Decimal(raw))) if raw is not None and not transfer else None
        if item.get('product_snapshot'):
            product = json.loads(item['product_snapshot'])
            item['canonical_name'] = product['name']
        item['original_quantity'] = item.get('original_quantity') if item.get('original_quantity') is not None else item['quantity']
        if not transfer and float(item['quantity']) > 0:
            if item['line_amount'] is None:
                known = False
            else:
                subtotal += Decimal(item['line_amount'])
    order['amount_state'] = 'not_applicable' if transfer else ('locked' if known else 'unknown')
    order['goods_amount'] = str(money(subtotal)) if known and not transfer else None
    order['total_amount'] = str(money(subtotal + Decimal(str(order.get('shipping_fee') or 0)))) if known and not transfer else None
    order['shipping_rule_state'] = 'locked' if order.get('shipping_rule_snapshot') is not None else 'unknown'


def effective_progress(conn: sqlite3.Connection, order_id: int) -> dict[str, bool]:
    """One effective-quantity denominator for ship, receive and future acceptance."""
    rows = conn.execute("""SELECT CASE WHEN status='cancelled' THEN 0 ELSE quantity END,
        shipped_quantity,fulfilled_quantity FROM store_order_items WHERE order_id=?""", (order_id,)).fetchall()
    unapplied = conn.execute('SELECT 1 FROM store_order_receipts WHERE order_id=? AND applied=0 LIMIT 1', (order_id,)).fetchone()
    return {'all_shipped': bool(rows) and all(float(r[1]) >= float(r[0]) - 1e-9 for r in rows),
            'all_received': bool(rows) and not unapplied and all(float(r[2]) >= float(r[0]) - 1e-9 for r in rows)}


def proposal_detail(conn: sqlite3.Connection, order_id: int, proposal_id: int) -> dict | None:
    row = conn.execute('SELECT * FROM store_order_change_proposals WHERE order_id=? AND id=?', (order_id, proposal_id)).fetchone()
    if row is None:
        return None
    proposal = dict(row)
    proposal['snapshot'] = json.loads(proposal.pop('snapshot_json'))
    proposal['lines'] = [dict(r) for r in conn.execute('SELECT * FROM store_order_change_lines WHERE proposal_id=? ORDER BY id', (proposal_id,))]
    proposal['events'] = [dict(r) for r in conn.execute('SELECT * FROM store_order_change_events WHERE proposal_id=? ORDER BY id', (proposal_id,))]
    return proposal


def can_withdraw(conn: sqlite3.Connection, order: dict, proposal: dict,
                 actor_id: int, current_warehouse_code: str | None) -> bool:
    """No Flask permission cache: POST calls again after BEGIN IMMEDIATE."""
    from blueprints import store_ordering_pure as sop
    user = conn.execute('SELECT is_active,is_admin,tenant_id FROM users WHERE id=?', (actor_id,)).fetchone()
    target = conn.execute('SELECT tenant_id FROM warehouses WHERE code=?', (order['store_warehouse_code'],)).fetchone()
    if user is None or target is None or not user[0] or user[2] != target[0] or proposal['status'] != 'pending':
        return False
    if actor_id == proposal['proposed_by']:
        return True
    required = set(json.loads(proposal['required_warehouses_json'])) | set(sop.order_participants(conn, order['id']))
    if current_warehouse_code not in required or not required:
        return False
    if user[1]:
        return True
    return all(actor_id in sop._users_with_permission(conn, 'order.approve', code, tenant_id=user[2]) for code in required)


def _proposal_event(conn: sqlite3.Connection, order: dict, proposal: dict,
                    actor_id: int, status: str, note: str) -> None:
    from blueprints import store_ordering_pure as sop
    from blueprints.notifications_pure import enqueue_event
    labels = {'pending': '待订货方确认', 'accepted': '已同意', 'rejected': '已拒绝',
              'withdrawn': '已撤回', 'stale': '已失效'}
    conn.execute("""INSERT INTO store_order_change_events
        (proposal_id,actor_id,from_status,to_status,note,created_at) VALUES (?,?,?,?,?,?)""",
        (proposal['id'], actor_id, None if status == 'pending' else 'pending', status, note, now()))
    tenant = _tenant(conn, order)
    users = [int(row[0]) for row in conn.execute(
        'SELECT id FROM users WHERE is_active=1 AND tenant_id=?', (tenant,))]
    store_users = {uid for uid in users if can_decide(conn, order, proposal, uid, order['store_warehouse_code'])}
    recipients = store_users
    if status != 'pending':
        # Only actual participants can open the order link; a proposed new source is
        # not a participant until acceptance has appended its frozen line.
        participants = sop.order_participants(conn, order['id'])
        recipients |= {uid for uid in users if any(
            _has_permission(conn, uid, code, 'order.approve', tenant) for code in participants)}
        proposer = proposal['proposed_by']
        if proposer in users and (
                _fresh_user(conn, order, proposer)['is_admin'] or conn.execute(
                    '''SELECT 1 FROM warehouse_users wu JOIN warehouses w ON w.id=wu.warehouse_id
                       WHERE wu.user_id=? AND w.tenant_id=? AND w.code IN
                       (SELECT COALESCE(i.source_warehouse_code,o.dc_warehouse_code) FROM store_order_items i
                        JOIN store_orders o ON o.id=i.order_id WHERE o.id=?)''',
                    (proposer, tenant, order['id'])).fetchone()):
            recipients.add(proposer)
    recipients.discard(actor_id)
    if recipients:
        enqueue_event(conn, 'store_order_change_' + ('proposed' if status == 'pending' else status),
            f"订单 {order['order_no']} 变更方案 #{proposal['sequence']} {labels[status]}"[:200],
            f"/store-ordering/orders/{order['id']}/changes/{proposal['id']}", sorted(recipients))


def invalidate_pending(conn: sqlite3.Connection, order: dict, actor_id: int, reason: str) -> None:
    """Caller owns order cancellation/review rejection transaction."""
    rows = conn.execute("SELECT * FROM store_order_change_proposals WHERE order_id=? AND status='pending'", (order['id'],)).fetchall()
    for row in rows:
        proposal = dict(row)
        if conn.execute("UPDATE store_order_change_proposals SET status='stale',stale_reason=?,decided_by=?,decided_at=? WHERE id=? AND status='pending'",
                        (reason, actor_id, now(), proposal['id'])).rowcount:
            _proposal_event(conn, order, proposal, actor_id, 'stale', reason)


def withdraw_proposal(conn: sqlite3.Connection, *, order_id: int, proposal_id: int,
                      actor_id: int, current_warehouse_code: str | None,
                      request_key: str, note: str = '') -> dict:
    from blueprints import store_ordering_pure as sop
    if not request_key:
        raise ValueError('撤回请求键不能为空')
    if conn.in_transaction:
        raise ValueError('撤回需要独立master事务')
    payload = {'order_id': order_id, 'proposal_id': proposal_id, 'actor_id': actor_id,
               'action': 'withdraw', 'note': note}
    digest = payload_hash(payload)
    conn.execute('BEGIN IMMEDIATE')
    try:
        order = sop.get_order_detail(conn, order_id)
        proposal = proposal_detail(conn, order_id, proposal_id)
        if order is None or proposal is None:
            raise ValueError('变更方案不存在')
        prior = conn.execute('SELECT * FROM store_order_change_commands WHERE request_key=?', (request_key,)).fetchone()
        if prior is not None:
            if prior['payload_hash'] != digest:
                raise ValueError('请求键已用于其他撤回命令')
            if not can_withdraw(conn, order, {**proposal, 'status': 'pending'}, actor_id, current_warehouse_code):
                raise PermissionError('当前无撤回权限')
            # Replayed result is not a new decision and emits no new notification.
            conn.commit()
            return json.loads(prior['result_json'])
        if not can_withdraw(conn, order, proposal, actor_id, current_warehouse_code):
            raise PermissionError('仅原提议人或具备全部参与仓审批权者可撤回待确认方案')
        if conn.execute("UPDATE store_order_change_proposals SET status='withdrawn',decided_by=?,decided_at=?,decision_note=? WHERE id=? AND status='pending'",
                        (actor_id, now(), note, proposal_id)).rowcount != 1:
            raise ValueError('方案已处理，请刷新')
        _proposal_event(conn, order, proposal, actor_id, 'withdrawn', note or '撤回方案')
        result = {'proposal_id': proposal_id, 'status': 'withdrawn'}
        conn.execute("""INSERT INTO store_order_change_commands
            (request_key,order_id,proposal_id,actor_id,action,payload_hash,result_json,created_at)
            VALUES (?,?,?,?,'withdraw',?,?,?)""",
            (request_key, order_id, proposal_id, actor_id, digest, canonical_json(result), now()))
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise

# Whole-order commercial proposals. Snapshot v1 is private persistence, not an MCP contract.
SNAPSHOT_VERSION = 1


def _number(value, label: str) -> Decimal:
    try:
        if isinstance(value, bool):
            raise ValueError(label + '不能为布尔值')
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            raise ValueError(label + '必须为非负有限数')
        return result
    except (InvalidOperation, TypeError):
        raise ValueError(label + '无效') from None


def _id(value) -> int:
    if isinstance(value, bool) or not str(value).isdigit() or int(value) <= 0:
        raise ValueError('无效明细ID')
    return int(value)


def normalize_intent(intent: dict) -> dict:
    if not isinstance(intent, dict):
        raise ValueError('方案必须为整份对象')
    revision = intent.get('base_change_revision')
    if isinstance(revision, bool) or not str(revision).isdigit():
        raise ValueError('缺少所见商业版本')
    reason = str(intent.get('reason') or '').strip()
    if not reason:
        raise ValueError('请填写变更原因')
    lines, seen = [], set()
    raw_lines = intent.get('lines')
    if not isinstance(raw_lines, list) or not raw_lines:
        raise ValueError('方案不能为空')
    for raw in raw_lines:
        if not isinstance(raw, dict):
            raise ValueError('无效方案明细')
        oid = _id(raw.get('order_item_id'))
        operation = raw.get('operation')
        if oid in seen or operation not in ('cancel', 'reduce', 'replace'):
            raise ValueError('重复明细或无效操作')
        seen.add(oid)
        line = {'order_item_id': oid, 'operation': operation}
        if operation == 'reduce':
            line['remove_quantity'] = quantity(_number(raw.get('remove_quantity'), '移除量'))
        if operation == 'replace':
            line['replacement_canonical_id'] = _id(raw.get('replacement_canonical_id'))
            line['replacement_quantity'] = quantity(_number(raw.get('replacement_quantity'), '替换数量'))
        lines.append(line)
    prices = {}
    raw_prices = intent.get('historical_prices') or {}
    if not isinstance(raw_prices, dict):
        raise ValueError('历史核对价格必须按明细填写')
    for key, raw in raw_prices.items():
        if not isinstance(raw, dict):
            raise ValueError('历史核对须包含价格、引用和说明')
        prices[str(_id(key))] = {
            'price': str(_number(raw.get('price'), '历史单价')),
            'reference': str(raw.get('reference') or '').strip(),
            'note': str(raw.get('note') or '').strip(),
        }
    return {'base_change_revision': int(revision), 'reason': reason,
            'lines': sorted(lines, key=lambda line: line['order_item_id']),
            'historical_prices': prices,
            'after_fee': str(_number(intent['after_fee'], '运费')) if intent.get('after_fee') not in (None, '') else None,
            'fee_reference': str(intent.get('fee_reference') or '').strip(),
            'fee_note': str(intent.get('fee_note') or '').strip()}


def _tenant(conn, order: dict) -> int:
    row = conn.execute('SELECT tenant_id FROM warehouses WHERE code=?', (order['store_warehouse_code'],)).fetchone()
    if row is None:
        raise ValueError('目标门店不存在')
    return int(row[0])


def _fresh_user(conn, order: dict, actor_id: int):
    row = conn.execute('SELECT * FROM users WHERE id=? AND is_active=1 AND tenant_id=?', (actor_id, _tenant(conn, order))).fetchone()
    return dict(row) if row else None


def _has_permission(conn, actor_id: int, code: str, permission: str, tenant: int) -> bool:
    return conn.execute('''SELECT 1 FROM users u JOIN warehouses w ON w.code=?
        JOIN warehouse_users wu ON wu.user_id=u.id AND wu.warehouse_id=w.id
        JOIN roles r ON r.code=wu.role AND (r.is_builtin=1 OR r.tenant_id=u.tenant_id)
        JOIN role_permissions rp ON rp.role_id=r.id AND rp.permission_code=?
        WHERE u.id=? AND u.is_active=1 AND u.tenant_id=? AND w.tenant_id=u.tenant_id''',
        (code, permission, actor_id, tenant)).fetchone() is not None


def can_propose(conn, order: dict, actor_id: int, current_warehouse_code: str | None,
                required: list[str] | None = None) -> bool:
    from blueprints import store_ordering_pure as sop
    user = _fresh_user(conn, order, actor_id)
    originals = sop.order_participants(conn, order['id'])
    required = sorted(set(required or originals) | set(originals))
    if (not user or order.get('flow_type') not in (None, 'store_order') or
            current_warehouse_code not in originals or not required):
        return False
    for code in required:
        warehouse = conn.execute('SELECT tenant_id,warehouse_type FROM warehouses WHERE code=?', (code,)).fetchone()
        if warehouse is None or warehouse[0] != user['tenant_id'] or warehouse[1] != 'distribution_center':
            return False
        if not user['is_admin'] and not _has_permission(conn, actor_id, code, 'order.approve', user['tenant_id']):
            return False
    return True


def can_decide(conn, order: dict, proposal: dict, actor_id: int,
               current_warehouse_code: str | None) -> bool:
    user = _fresh_user(conn, order, actor_id)
    if (not user or current_warehouse_code != order['store_warehouse_code'] or
            actor_id == proposal['proposed_by']):
        return False
    if user['is_admin']:
        return True
    tenant = user['tenant_id']
    if actor_id == order['requested_by'] and _has_permission(conn, actor_id, current_warehouse_code, 'order.create', tenant):
        return True
    role = conn.execute('''SELECT wu.role FROM warehouse_users wu JOIN warehouses w ON w.id=wu.warehouse_id
        WHERE wu.user_id=? AND w.code=? AND w.tenant_id=?''', (actor_id, current_warehouse_code, tenant)).fetchone()
    return bool(role and role[0] in ('manager', 'admin') and _has_permission(conn, actor_id, current_warehouse_code, 'order.receive', tenant))


def _readonly_warehouse(conn, code: str):
    from pathlib import Path

    import config
    row = conn.execute('SELECT db_path FROM warehouses WHERE code=?', (code,)).fetchone()
    if row is None:
        raise ValueError('仓库不存在')
    path = (Path(config.BASE_DIR) / row[0]).resolve()
    wh = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    wh.row_factory = sqlite3.Row
    wh.execute('BEGIN')
    return wh


def _candidate_master(conn, order: dict, canonical_id: int) -> dict:
    tenant = _tenant(conn, order)
    row = conn.execute('''SELECT ci.*,r.fulfillment_warehouse_code AS source,w.db_path AS source_path,
        w.tenant_id AS source_tenant,w.warehouse_type AS source_type
        FROM canonical_items ci JOIN item_routing r ON r.canonical_id=ci.id AND r.tenant_id=?
        JOIN warehouses w ON w.code=r.fulfillment_warehouse_code WHERE ci.id=? AND ci.tenant_id=?''', (tenant, canonical_id, tenant)).fetchone()
    if row is None or row['status'] != 'active' or row['source_tenant'] != tenant or row['source_type'] != 'distribution_center':
        raise ValueError('替换品项当前路由、租户或可订资格无效')
    return dict(row)


def _candidate(conn, order: dict, canonical_id: int, qty: str) -> dict:
    from blueprints.fulfillment_pure import resolve_fulfillment_item
    from blueprints.store_ordering_pure import is_multiple_of
    master = _candidate_master(conn, order, canonical_id)
    source = master['source']
    store = _readonly_warehouse(conn, order['store_warehouse_code'])
    try:
        flags = [dict(row) for row in store.execute('SELECT is_active,is_store_exclusive FROM items WHERE canonical_id=?', (canonical_id,))]
    finally:
        store.close()
    if any(not row['is_active'] or row['is_store_exclusive'] for row in flags):
        raise ValueError('目标门店已停用或专属品项不可替换')
    wh = _readonly_warehouse(conn, source)
    try:
        resolved = resolve_fulfillment_item(conn, wh, canonical_id=canonical_id, warehouse_code=source)
        local = dict(wh.execute('SELECT * FROM items WHERE id=?', (resolved['item_id'],)).fetchone())
        if not local['is_orderable']:
            raise ValueError('替换品项当前不可订')
        amount = _number(qty, '替换数量')
        multiple = resolved['order_multiple']
        if amount <= 0 or not is_multiple_of(float(amount), multiple):
            raise ValueError('替换数量须为正数且符合基础单位起订倍数')
        price = snapshot_product(conn, wh, canonical_id, resolved['item_id'])
        quote = {'canonical_id': canonical_id, 'dc_item_id': resolved['item_id'],
                 'source_warehouse_code': source, 'unit': resolved['unit'], 'quantity': qty,
                 'order_multiple': str(multiple), **price,
                 'product': json.loads(price['product_snapshot']),
                 'master_fingerprint': payload_hash(master)}
        quote.pop('unit_price')
        quote['quote_fingerprint'] = payload_hash(quote)
        return quote
    finally:
        wh.close()


def list_replacement_candidates(conn, order: dict) -> list[dict]:
    rows = conn.execute('SELECT canonical_id FROM item_routing WHERE tenant_id=? ORDER BY canonical_id', (_tenant(conn, order),)).fetchall()
    result = []
    for row in rows:
        try:
            # Quantity is just a display probe; the submitted quantity is independently checked.
            master = _candidate_master(conn, order, row[0])
            wh = _readonly_warehouse(conn, master['source'])
            try:
                from blueprints.fulfillment_pure import resolve_fulfillment_item
                item = resolve_fulfillment_item(conn, wh, canonical_id=row[0], warehouse_code=master['source'])
            finally:
                wh.close()
            result.append(_candidate(conn, order, row[0], quantity(item['order_multiple'])))
        except (ValueError, sqlite3.Error, OSError):
            continue
    return result


def _assert_order_changeable(order):
    if order.get('flow_type') not in (None, 'store_order') or order['status'] not in ('pending', 'approved'):
        raise ValueError('仅待审批或已审批的普通订货单可提变更')
    for row in order['order_items']:
        received, shipped, effective, original = (_number(row[key], '订单数量') for key in ('fulfilled_quantity', 'shipped_quantity', 'quantity', 'original_quantity'))
        if not 0 <= received <= shipped <= effective <= original:
            raise ValueError('历史数量异常，须人工核对后再变更')


def _reference(conn, order_id, reference: str, note: str):
    if not reference or not note:
        raise ValueError('历史核对须填写本单凭证ID或线下编号及证据说明')
    if reference.startswith('proof:'):
        proof_id = _id(reference[6:])
        if not conn.execute('SELECT 1 FROM store_order_payment_proofs WHERE order_id=? AND id=?', (order_id, proof_id)).fetchone():
            raise ValueError('凭证不属于本订单')


def _build_snapshot(conn, order, intent, actor_id, quotes):
    _assert_order_changeable(order)
    if order['change_revision'] != intent['base_change_revision']:
        raise ValueError('商业版本已变化，请刷新重提')
    before, after, operations, verification = [], [], [], {}
    by_id = {row['id']: row for row in order['order_items']}
    affected = {line['order_item_id']: line for line in intent['lines']}
    if not set(affected) <= set(by_id):
        raise ValueError('明细不属于本订单')
    if not set(intent['historical_prices']) <= {str(key) for key in by_id}:
        raise ValueError('历史价格明细不属于本订单')
    for row in order['order_items']:
        raw_price = row['unit_price_snapshot']
        if raw_price is None and _number(row['quantity'], '有效量') > 0:
            evidence = intent['historical_prices'].get(str(row['id']))
            if not evidence:
                raise ValueError('全单正量缺价行均须经凭据核对，不能以当前价或零替代')
            _reference(conn, order['id'], evidence['reference'], evidence['note'])
            raw_price = evidence['price']
            verification[str(row['id'])] = {**evidence, 'checked_by': actor_id}
        elif str(row['id']) in intent['historical_prices']:
            raise ValueError('已有非空历史单价不可重新定价')
        price = _number(raw_price, '历史单价') if raw_price is not None else None
        product = json.loads(row['product_snapshot']) if row['product_snapshot'] else {
            'name': row['canonical_name'], 'canonical_sku': None, 'unit': row['unit'],
            'aux_unit': None, 'aux_rate': None}
        display = {'order_item_id': row['id'], 'canonical_id': row['canonical_id'],
            'source_warehouse_code': row['effective_source_warehouse_code'], 'unit': row['unit'],
            'original_quantity': quantity(row['original_quantity']), 'quantity': quantity(row['quantity']),
            'shipped_quantity': quantity(row['shipped_quantity']), 'fulfilled_quantity': quantity(row['fulfilled_quantity']),
            'product': product, 'product_basis': 'historical_snapshot' if row['product_snapshot'] else 'current_identification_only',
            'unit_price_snapshot': str(price) if price is not None else None,
            'price_snapshot_origin': row['price_snapshot_origin'] if row['unit_price_snapshot'] is not None else ('verified' if price is not None else None),
            'line_amount': str(money(price * Decimal(str(row['quantity'])))) if price is not None else '0.00',
            'verification_pending': str(row['id']) in verification}
        before.append(display)
        changed = dict(display)
        if row['id'] in affected:
            line = affected[row['id']]
            remaining = Decimal(str(row['quantity'])) - Decimal(str(row['shipped_quantity']))
            remove = _number(line['remove_quantity'], '移除量') if line['operation'] == 'reduce' else remaining
            if remaining <= 0 or remove <= 0 or remove > remaining or (line['operation'] == 'reduce' and remove >= remaining):
                raise ValueError('减量须严格小于未发量；取消或替换移除全部未发量')
            new_qty = Decimal(str(row['quantity'])) - remove
            if not new_qty.is_finite() or new_qty < Decimal(str(row['shipped_quantity'])):
                raise ValueError('有效量不得低于已发量')
            changed['quantity'] = quantity(new_qty)
            changed['line_amount'] = str(money(price * new_qty))
            replacement = quotes.get(row['id'])
            operations.append({'order_item_id': row['id'], 'operation': line['operation'],
                'base_line_revision': row['line_revision'], 'baseline_quantity': quantity(row['quantity']),
                'baseline_shipped': quantity(row['shipped_quantity']), 'remove_quantity': quantity(remove),
                'replacement': replacement})
        after.append(changed)
        if row['id'] in quotes:
            replacement = quotes[row['id']]
            after.append({'order_item_id': None, 'replaces_order_item_id': row['id'], **replacement,
                'original_quantity': replacement['quantity'], 'shipped_quantity': '0.00', 'fulfilled_quantity': '0.00',
                'product_basis': 'replacement_quote', 'verification_pending': False,
                'line_amount': str(money(Decimal(replacement['unit_price_snapshot']) * Decimal(replacement['quantity'])))})
    before_goods = sum((Decimal(line['line_amount']) for line in before), Decimal(0))
    after_goods = sum((Decimal(line['line_amount']) for line in after), Decimal(0))
    before_fee = money(_number(order['shipping_fee'], '已记录运费'))
    rule = json.loads(order['shipping_rule_snapshot']) if order['shipping_rule_snapshot'] else None
    all_zero = all(Decimal(line['quantity']) == 0 for line in after)
    if all_zero:
        after_fee, fee_basis = Decimal(0), 'all_cancelled'
    elif rule is not None:
        after_fee = money(_number(rule['base_fee'], '原规则基础费') + _number(rule['pct_fee'], '原规则比例') * after_goods)
        fee_basis = 'original_rule_snapshot'
        if intent['after_fee'] is not None and money(intent['after_fee']) != after_fee:
            raise ValueError('新单运费须按原规则快照计算')
    elif intent['after_fee'] is None:
        after_fee, fee_basis = before_fee, 'recorded_fee_carried_forward_rule_unknown'
    else:
        _reference(conn, order['id'], intent['fee_reference'], intent['fee_note'])
        after_fee, fee_basis = money(intent['after_fee']), 'verified_adjustment_rule_unknown'
    from blueprints.store_ordering_pure import order_participants
    required = sorted(set(order_participants(conn, order['id'])) | {quote['source_warehouse_code'] for quote in quotes.values()})
    def totals(goods, fee):
        return {'goods_amount': str(money(goods)), 'shipping_fee': str(money(fee)), 'total_amount': str(money(goods + fee))}
    return {'version': SNAPSHOT_VERSION, 'base_change_revision': order['change_revision'],
        'before': {'lines': before, **totals(before_goods, before_fee)},
        'after': {'lines': after, **totals(after_goods, after_fee)},
        'delta': str(money(after_goods + after_fee - before_goods - before_fee)),
        'operations': operations, 'required_warehouses': required, 'historical_verification': verification,
        'fee_basis': fee_basis, 'fee_reference': intent['fee_reference'], 'fee_note': intent['fee_note'],
        'reason': intent['reason'], 'prepared_by': actor_id,
        'catalog_boundary': '仓库只读报价为观测快照，不预留库存；跨库配置不具有原子性'}


def _prepare(conn, order, intent):
    try:
        return {line['order_item_id']: _candidate(conn, order, line['replacement_canonical_id'], line['replacement_quantity'])
                for line in intent['lines'] if line['operation'] == 'replace'}
    except (sqlite3.Error, OSError):
        raise ValueError('来源或目标仓报价不可读取，请核对后重试') from None


def preview_proposal(conn, *, order_id: int, actor_id: int, current_warehouse_code: str, intent: dict) -> dict:
    from blueprints.store_ordering_pure import get_order_detail
    order = get_order_detail(conn, order_id)
    if order is None:
        raise LookupError('订单不存在')
    if not can_propose(conn, order, actor_id, current_warehouse_code):
        raise PermissionError('须在原参与仓且具备全部联合仓审批权')
    intent = normalize_intent(intent)
    snapshot = _build_snapshot(conn, order, intent, actor_id, _prepare(conn, order, intent))
    if not can_propose(conn, order, actor_id, current_warehouse_code, snapshot['required_warehouses']):
        raise PermissionError('缺少新增来源仓的联合审批权')
    return {'snapshot': snapshot, 'digest': payload_hash(snapshot), 'intent': intent}


def _enabled():
    import config
    if not config.ORDER_CHANGES_ENABLED:
        raise ValueError('订单变更入口尚未启用')


def _independent(conn, request_key):
    if conn.in_transaction:
        raise ValueError('变更命令需要独立master事务')
    if not isinstance(request_key, str) or not request_key.strip():
        raise ValueError('请求键不能为空')


def _prior(conn, request_key, digest):
    prior = conn.execute('SELECT * FROM store_order_change_commands WHERE request_key=?', (request_key,)).fetchone()
    if prior is not None and prior['payload_hash'] != digest:
        raise ValueError('请求键已用于不同身份或完整变更命令')
    return dict(prior) if prior else None


def _save_command(conn, request_key, order_id, proposal_id, actor_id, action, digest, result):
    conn.execute('''INSERT INTO store_order_change_commands
        (request_key,order_id,proposal_id,actor_id,action,payload_hash,result_json,created_at)
        VALUES (?,?,?,?,?,?,?,?)''', (request_key, order_id, proposal_id, actor_id, action, digest, canonical_json(result), now()))


def _master_quotes_unchanged(conn, order, quotes):
    for quote in quotes.values():
        if payload_hash(_candidate_master(conn, order, quote['canonical_id'])) != quote['master_fingerprint']:
            raise ValueError('替换路由或主数据在准备期间变化，请刷新重试')


def create_proposal(conn, *, order_id: int, actor_id: int, current_warehouse_code: str,
                    request_key: str, intent: dict, seen_digest: str) -> dict:
    from blueprints.store_ordering_pure import get_order_detail
    _independent(conn, request_key)
    _enabled()
    intent = normalize_intent(intent)
    digest = payload_hash({'action': 'create', 'order_id': order_id, 'actor_id': actor_id,
                           'intent': intent, 'seen_digest': seen_digest})
    order = get_order_detail(conn, order_id)
    if order is None:
        raise LookupError('订单不存在')
    if not can_propose(conn, order, actor_id, current_warehouse_code):
        raise PermissionError('无整份提议权限')
    # A replay never depends on rereading the warehouse catalogue.
    prior = _prior(conn, request_key, digest)
    quotes = {} if prior else _prepare(conn, order, intent)
    conn.execute('BEGIN IMMEDIATE')
    try:
        order = get_order_detail(conn, order_id)
        prior = _prior(conn, request_key, digest)
        if prior:
            proposal = proposal_detail(conn, order_id, prior['proposal_id'])
            if not can_propose(conn, order, actor_id, current_warehouse_code, proposal['snapshot']['required_warehouses']):
                raise PermissionError('当前无联合仓审批权')
            conn.commit()
            return json.loads(prior['result_json'])
        snapshot = _build_snapshot(conn, order, intent, actor_id, quotes)
        if not can_propose(conn, order, actor_id, current_warehouse_code, snapshot['required_warehouses']):
            raise PermissionError('当前无全部联合仓审批权')
        _master_quotes_unchanged(conn, order, quotes)
        if not seen_digest or seen_digest != payload_hash(snapshot):
            raise ValueError('所见整份方案或报价已变化，请预览后重新发布')
        if conn.execute("SELECT 1 FROM store_order_change_proposals WHERE order_id=? AND status='pending'", (order_id,)).fetchone():
            raise ValueError('已有待确认整份方案，请先处理或撤回')
        assert_unoccupied(conn, order_id, [line['order_item_id'] for line in snapshot['operations']])
        timestamp = now()
        snapshot['observed_at'] = timestamp
        for evidence in snapshot['historical_verification'].values():
            evidence['checked_at'] = timestamp
        sequence = conn.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM store_order_change_proposals WHERE order_id=?', (order_id,)).fetchone()[0]
        pid = conn.execute('''INSERT INTO store_order_change_proposals
            (order_id,sequence,status,base_order_change_revision,proposed_by,reason,snapshot_json,required_warehouses_json,created_at)
            VALUES (?,?,'pending',?,?,?,?,?,?)''', (order_id, sequence, order['change_revision'], actor_id,
            intent['reason'], canonical_json(snapshot), canonical_json(snapshot['required_warehouses']), timestamp)).lastrowid
        for line in snapshot['operations']:
            conn.execute('''INSERT INTO store_order_change_lines
                (proposal_id,order_id,order_item_id,operation,base_line_revision,baseline_quantity,baseline_shipped,remove_quantity,replacement_snapshot)
                VALUES (?,?,?,?,?,?,?,?,?)''', (pid, order_id, line['order_item_id'], line['operation'], line['base_line_revision'],
                float(line['baseline_quantity']), float(line['baseline_shipped']),
                min(float(line['remove_quantity']), float(line['baseline_quantity']) - float(line['baseline_shipped'])),
                canonical_json(line['replacement']) if line['replacement'] else None))
        proposal = proposal_detail(conn, order_id, pid)
        _proposal_event(conn, order, proposal, actor_id, 'pending', intent['reason'])
        result = {'proposal_id': pid, 'status': 'pending', 'snapshot_digest': payload_hash(snapshot)}
        _save_command(conn, request_key, order_id, pid, actor_id, 'create', digest, result)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


def _accepted_state(conn, order, actor_id):
    from blueprints import store_ordering_pure as sop
    rows = conn.execute('SELECT quantity,shipped_quantity,fulfilled_quantity FROM store_order_items WHERE order_id=?', (order['id'],)).fetchall()
    occupied = conn.execute("SELECT 1 FROM store_order_ship_claim_lines WHERE order_id=? AND state IN ('active','recovering')", (order['id'],)).fetchone()
    pending = conn.execute("SELECT 1 FROM store_order_change_proposals WHERE order_id=? AND status='pending'", (order['id'],)).fetchone()
    unapplied = conn.execute('SELECT 1 FROM store_order_receipts WHERE order_id=? AND applied=0', (order['id'],)).fetchone()
    if occupied or pending:
        return
    if rows and all(row[0] == row[1] == row[2] == 0 for row in rows) and not unapplied:
        if conn.execute('SELECT 1 FROM store_order_deliveries WHERE order_id=?', (order['id'],)).fetchone() or conn.execute('SELECT 1 FROM store_order_receipts WHERE order_id=?', (order['id'],)).fetchone():
            raise ValueError('零有效量但存在履约记录，须人工核对')
        conn.execute("UPDATE store_orders SET status='cancelled',shipping_fee=0,cancelled_by=?,cancelled_at=?,updated_at=? WHERE id=?", (actor_id, now(), now(), order['id']))
        sop._insert_status_history(conn, order['id'], order['status'], 'cancelled', actor_id, '整份变更接受：全部未发量取消')
    elif order['status'] != 'pending':
        sop.refresh_effective_order_state(conn, order['id'], actor_id, '整份变更后按有效量履约完成')


def _assert_pending_valid(conn, order, proposal, quotes, preparation_error):
    from blueprints.store_ordering_pure import order_participants
    snapshot = proposal['snapshot']
    _assert_order_changeable(order)
    if snapshot.get('version') != SNAPSHOT_VERSION or not snapshot.get('operations'):
        raise ValueError('方案快照缺少业务版本，须重新提议')
    if order['change_revision'] != proposal['base_order_change_revision'] or order['status'] not in ('pending', 'approved'):
        raise ValueError('订单商业版本或状态已变化')
    if not can_propose(conn, order, proposal['proposed_by'],
            next(iter(order_participants(conn, order['id'])), None), snapshot['required_warehouses']):
        raise ValueError('原提议人的全部联合仓背书已失效')
    if preparation_error:
        raise ValueError('替换资格或报价已变化，须重新提议')
    _master_quotes_unchanged(conn, order, quotes)
    by_id = {row['id']: row for row in order['order_items']}
    for saved in snapshot['before']['lines']:
        current = by_id.get(saved['order_item_id'])
        if current is None or (
                saved['verification_pending'] and current['unit_price_snapshot'] is not None) or (
                not saved['verification_pending'] and current['unit_price_snapshot'] != saved['unit_price_snapshot']):
            raise ValueError('历史单价基线已变化，须重新核对')
    for line in snapshot['operations']:
        row = by_id.get(line['order_item_id'])
        if (row is None or row['line_revision'] != line['base_line_revision'] or
                quantity(row['quantity']) != line['baseline_quantity'] or quantity(row['shipped_quantity']) != line['baseline_shipped']):
            raise ValueError('受影响行基线已变化')
        if line['replacement'] != quotes.get(line['order_item_id']):
            raise ValueError('替换品项、路由、单位、倍数或报价已变化')
    assert_unoccupied(conn, order['id'], [line['order_item_id'] for line in snapshot['operations']])


def _decision(conn, *, action, order_id, proposal_id, actor_id, current_warehouse_code,
              request_key, seen_digest, note, agree=False):
    from blueprints.store_ordering_pure import get_order_detail
    _independent(conn, request_key)
    _enabled()
    note = str(note or '').strip()
    if action == 'accept' and agree is not True:
        raise ValueError('请明确同意整份方案')
    if action == 'reject' and not note:
        raise ValueError('不同意须填写留言')
    digest = payload_hash({'action': action, 'order_id': order_id, 'proposal_id': proposal_id,
        'actor_id': actor_id, 'seen_digest': seen_digest, 'note': note, 'agree': agree})
    order = get_order_detail(conn, order_id)
    proposal = proposal_detail(conn, order_id, proposal_id)
    if order is None or proposal is None:
        raise LookupError('订单或方案不存在')
    if not can_decide(conn, order, proposal, actor_id, current_warehouse_code):
        raise PermissionError('仅目标门店合法订货方可决定，提议人不能自确认')
    prior = _prior(conn, request_key, digest)
    quotes, preparation_error = {}, None
    if action == 'accept' and not prior and proposal['status'] == 'pending':
        try:
            quotes = {line['order_item_id']: _candidate(conn, order, line['replacement']['canonical_id'], line['replacement']['quantity'])
                      for line in proposal['snapshot'].get('operations', []) if line['replacement']}
        except (ValueError, sqlite3.Error, OSError) as exc:
            preparation_error = exc
    conn.execute('BEGIN IMMEDIATE')
    try:
        order = get_order_detail(conn, order_id)
        proposal = proposal_detail(conn, order_id, proposal_id)
        if not can_decide(conn, order, proposal, actor_id, current_warehouse_code):
            raise PermissionError('当前无目标门店整份确认权限')
        prior = _prior(conn, request_key, digest)
        if prior:
            conn.commit()
            return json.loads(prior['result_json'])
        if not seen_digest or seen_digest != payload_hash(proposal['snapshot']):
            raise ValueError('所见方案不一致，请刷新后明确决定')
        target = 'accepted' if action == 'accept' else 'rejected'
        decision_note = note
        if _fresh_user(conn, order, actor_id)['is_admin']:
            decision_note = f"平台管理员代确认（目标门店 {order['store_warehouse_code']}）：{note}"
        if proposal['status'] != 'pending':
            if proposal['status'] != target:
                raise ValueError('方案已被其他方向处理，不能覆盖')
            result = {'proposal_id': proposal_id, 'status': target, 'already_processed': True,
                      'decided_by': proposal['decided_by'], 'decision_note': proposal['decision_note']}
        else:
            if action == 'accept':
                try:
                    _assert_pending_valid(conn, order, proposal, quotes, preparation_error)
                except ValueError as exc:
                    reason = str(exc)
                    conn.execute("UPDATE store_order_change_proposals SET status='stale',stale_reason=?,decided_by=?,decided_at=? WHERE id=? AND status='pending'", (reason, actor_id, now(), proposal_id))
                    _proposal_event(conn, order, proposal, actor_id, 'stale', reason)
                    result = {'proposal_id': proposal_id, 'status': 'stale', 'conflict': reason}
                    _save_command(conn, request_key, order_id, proposal_id, actor_id, action, digest, result)
                    conn.commit()
                    return result
            if conn.execute("UPDATE store_order_change_proposals SET status=?,decided_by=?,decided_at=?,decision_note=? WHERE id=? AND status='pending'", (target, actor_id, now(), decision_note, proposal_id)).rowcount != 1:
                raise ValueError('方案已处理，请刷新')
            if action == 'accept':
                snapshot = proposal['snapshot']
                for key, evidence in snapshot['historical_verification'].items():
                    if conn.execute("UPDATE store_order_items SET unit_price_snapshot=?,price_snapshot_origin='verified' WHERE id=? AND order_id=? AND unit_price_snapshot IS NULL", (evidence['price'], int(key), order_id)).rowcount != 1:
                        raise ValueError('历史核对价格基线已变化')
                line_ids = {line['order_item_id']: line['id'] for line in proposal['lines']}
                for line in snapshot['operations']:
                    new_quantity = float(Decimal(line['baseline_quantity']) - Decimal(line['remove_quantity']))
                    if conn.execute('''UPDATE store_order_items SET quantity=?,line_revision=line_revision+1
                        WHERE id=? AND order_id=? AND line_revision=? AND quantity=? AND shipped_quantity=?
                        AND ? >= shipped_quantity''', (new_quantity, line['order_item_id'], order_id, line['base_line_revision'],
                        float(line['baseline_quantity']), float(line['baseline_shipped']), new_quantity)).rowcount != 1:
                        raise ValueError('受影响行已变更，整份接受回滚')
                    replacement = line['replacement']
                    if replacement:
                        conn.execute('''INSERT INTO store_order_items
                            (order_id,canonical_id,dc_item_id,source_warehouse_code,quantity,unit,created_at,
                             unit_price_snapshot,price_snapshot_origin,product_snapshot,replaces_order_item_id,introduced_by_proposal_line_id)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''', (order_id, replacement['canonical_id'], replacement['dc_item_id'],
                            replacement['source_warehouse_code'], float(replacement['quantity']), replacement['unit'], now(),
                            replacement['unit_price_snapshot'], replacement['price_snapshot_origin'], replacement['product_snapshot'],
                            line['order_item_id'], line_ids[line['order_item_id']]))
                conn.execute('UPDATE store_orders SET change_revision=change_revision+1,shipping_fee=?,updated_at=? WHERE id=?', (float(snapshot['after']['shipping_fee']), now(), order_id))
                _accepted_state(conn, order, actor_id)
            _proposal_event(conn, get_order_detail(conn, order_id), proposal, actor_id, target, decision_note or '明确同意整份方案')
            result = {'proposal_id': proposal_id, 'status': target, 'decided_by': actor_id, 'decision_note': decision_note}
        _save_command(conn, request_key, order_id, proposal_id, actor_id, action, digest, result)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


def accept_proposal(conn, *, order_id: int, proposal_id: int, actor_id: int,
                    current_warehouse_code: str, request_key: str, seen_digest: str,
                    agree: bool, note: str = '') -> dict:
    return _decision(conn, action='accept', order_id=order_id, proposal_id=proposal_id,
        actor_id=actor_id, current_warehouse_code=current_warehouse_code, request_key=request_key,
        seen_digest=seen_digest, agree=agree, note=note)


def reject_proposal(conn, *, order_id: int, proposal_id: int, actor_id: int,
                    current_warehouse_code: str, request_key: str, seen_digest: str, note: str) -> dict:
    return _decision(conn, action='reject', order_id=order_id, proposal_id=proposal_id,
        actor_id=actor_id, current_warehouse_code=current_warehouse_code, request_key=request_key,
        seen_digest=seen_digest, note=note)
