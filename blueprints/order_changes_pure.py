"""Order-change compatibility domain. No proposal create/accept/reject entrypoints.

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
    conn.execute("""INSERT INTO store_order_change_events
        (proposal_id,actor_id,from_status,to_status,note,created_at) VALUES (?,?,'pending',?,?,?)""",
        (proposal['id'], actor_id, status, note, now()))
    recipients = set(sop._recipients_for_store_users(conn, order)) | set(sop._recipients_for_approvers(conn, sop.order_participants(conn, order['id'])))
    recipients.discard(actor_id)
    if recipients:
        enqueue_event(conn, 'store_order_change_' + status,
            f"订单 {order['order_no']} 变更方案 #{proposal['sequence']} {'已撤回' if status == 'withdrawn' else '已失效'}"[:200],
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
