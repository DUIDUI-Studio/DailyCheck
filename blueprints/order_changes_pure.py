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


def ship_payload(order_id: int, source: str, line_qty: dict[int, float]) -> dict:
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
    return {'order_id': int(order_id), 'source': source, 'lines': sorted(normalized.items())}


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
    payload = ship_payload(order_id, source, {oid: qty for oid, _, qty in selected})
    claim = get_claim(conn, op_no, payload)
    if claim is None or claim['state'] != 'active':
        raise ValueError('发货占用已恢复或终结，迟到请求不可执行')
    rows = claim_lines(conn, op_no)
    items = {oid: item for oid, item, _ in selected}
    for line in rows:
        row = conn.execute('SELECT * FROM store_order_items WHERE id=?', (line['order_item_id'],)).fetchone()
        if row is None:
            raise ValueError('发货冻结明细已变更')
        item = dict(row)
        if (item['line_revision'] != line['base_line_revision'] or
            item['canonical_id'] != line['canonical_id'] or item['unit'] != line['unit'] or
            (item['source_warehouse_code'] or source) != source or
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
