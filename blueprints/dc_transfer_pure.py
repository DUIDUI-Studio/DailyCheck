"""Pure logic for 总仓调货（DC → 其他 DC / 门店调拨）.

设计要点（详见 docs/2026-10-19-dc-transfer-design.md）：

- **单据本体复用** ``store_orders``：调货单就是一行 ``flow_type='dc_transfer'``
  的订单，因此明细、批次发货（``store_order_deliveries``）、部分收货
  （``store_order_receipts``）、状态历史、库存流水全部复用订货流程的实现。
- **方向复用现有列**：``dc_warehouse_code`` = 发货仓（总仓），
  ``store_warehouse_code`` = 收货仓（其他 DC / 门店）。不新增方向列。
- **跳过审批**：调货由总仓主动发起，不存在「总仓审批自己」的环节，因此
  建单即 ``approved``（待发货），不经过 ``pending``。
- **发送单 / 接收单** 是同一张单的两个视角：
    * 发送单状态看发货进度（``document_state``）
    * 接收单状态看收货进度（``receipt_state``，由每行实发 - 已收派生）

本模块不做任何 Flask 导入；路由层见 ``blueprints/dc_transfer.py``。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from blueprints import store_ordering_pure as sop
from blueprints._helpers import now, parse_qty

WAREHOUSE_TYPE_STOREFRONT: str = "storefront"
TRANSFER_RECEIVER_TYPES = (sop.WAREHOUSE_TYPE_DC, WAREHOUSE_TYPE_STOREFRONT)

# 发送单状态（总仓视角）
DOC_AWAITING_SHIP: str = "awaiting_ship"
DOC_PARTIAL_SHIPPED: str = "partial_shipped"
DOC_SHIPPED: str = "shipped"
DOC_COMPLETED: str = "completed"
DOC_CANCELLED: str = "cancelled"

# 接收单状态（分仓视角）
RCV_AWAITING_SHIP: str = "awaiting_ship"
RCV_PENDING: str = "pending_receive"
RCV_PARTIAL: str = "partial_received"
RCV_RECEIVED: str = "received"
RCV_CANCELLED: str = "cancelled"

DOC_STATE_LABELS: dict[str, str] = {
    DOC_AWAITING_SHIP: "待发货",
    DOC_PARTIAL_SHIPPED: "部分发出",
    DOC_SHIPPED: "已发出",
    DOC_COMPLETED: "已完成",
    DOC_CANCELLED: "已取消",
}

RCV_STATE_LABELS: dict[str, str] = {
    RCV_AWAITING_SHIP: "待发货",
    RCV_PENDING: "待收货",
    RCV_PARTIAL: "部分收货",
    RCV_RECEIVED: "已收货",
    RCV_CANCELLED: "已取消",
}


# ---------------------------------------------------------------------------
# Numbering
# ---------------------------------------------------------------------------

def generate_transfer_no(master_conn: sqlite3.Connection) -> str:
    """DT-YYYYMMDD-XXXX，与订货单（SO）分号段。"""
    return sop.generate_order_no(master_conn, prefix="DT")


# ---------------------------------------------------------------------------
# Source-warehouse item picker
# ---------------------------------------------------------------------------

def list_transferable_items(
    master_conn: sqlite3.Connection,
    dc_warehouse_code: str,
) -> list[dict[str, Any]]:
    """可调拨品项 = 总仓内已绑定主数据、主数据 active、库存 > 0 的 items。

    库存为 0 的行不列出：调货是「现货发出」，列出来只会让操作者建出一张
    发不出的单（发货时会被库存校验拒绝）。
    """
    items = sop.list_dc_items_for_management(master_conn, dc_warehouse_code)
    return [
        it for it in items
        if it.get("canonical_active") and parse_qty(it.get("quantity") or 0) > 0
    ]


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

def _assert_warehouse(
    master_conn: sqlite3.Connection,
    code: str,
    expected_type: str | tuple[str, ...],
    label: str,
) -> dict[str, Any]:
    master_conn.row_factory = sqlite3.Row
    row = master_conn.execute(
        "SELECT code, name, warehouse_type, tenant_id FROM warehouses WHERE code=?",
        (code,),
    ).fetchone()
    if row is None:
        raise ValueError(f"{label}不存在：{code}")
    types = (expected_type,) if isinstance(expected_type, str) else expected_type
    if row["warehouse_type"] not in types:
        raise ValueError(f"{label} {code} 不是{' / '.join(types)}类型仓库")
    return dict(row)


def create_transfer_order(
    master_conn: sqlite3.Connection,
    *,
    from_warehouse_code: str,
    to_warehouse_code: str,
    items: list[dict[str, Any]],
    actor_id: int,
    note: str | None = None,
    expected_delivery_date: str | None = None,
) -> dict[str, Any]:
    """总仓发起调货：直接创建一张 ``approved`` 的调货单。

    ``items`` 每项：``{"canonical_id": int, "quantity": float}``；同一
    canonical 出现多行时先合并，避免明细重复导致发货/收货对不上。

    校验（任一条不满足 → ValueError，调用方 flash）：
      1. 发货仓必须是配送中心，收货仓为同租户的其他配送中心或门店；
      2. 明细非空、数量 > 0；
      3. 每个 canonical 在发货仓有对应 items 行（否则发不出货）。

    **不在建单时锁库存**：与门店订货的 A7 口径一致——库存校验放在实际
    发货时做（``sop._ship_order_partial_impl`` 硬校验，永不跌负）。建单只是
    意图记录，期间库存可能被其他单据占用。
    """
    if not items:
        raise ValueError("调货明细不能为空")
    if from_warehouse_code == to_warehouse_code:
        raise ValueError("发货仓与收货仓不能相同")
    source = _assert_warehouse(
        master_conn, from_warehouse_code, sop.WAREHOUSE_TYPE_DC, "发货仓"
    )
    receiver = _assert_warehouse(
        master_conn, to_warehouse_code, TRANSFER_RECEIVER_TYPES, "收货仓"
    )
    if receiver["tenant_id"] != source["tenant_id"]:
        raise ValueError("发货仓与收货仓必须属于同一租户")

    # 合并同 canonical 多行（保持首次出现顺序，UI 顺序可预期）。
    merged: dict[int, float] = {}
    order_keys: list[int] = []
    for raw in items:
        try:
            canonical_id = int(raw.get("canonical_id"))
            qty = parse_qty(raw.get("quantity") or 0)
        except (TypeError, ValueError):
            raise ValueError(f"调货明细参数无效：{raw!r}") from None
        if qty <= 0:
            raise ValueError("调货数量必须大于 0")
        if canonical_id not in merged:
            order_keys.append(canonical_id)
            merged[canonical_id] = 0.0
        merged[canonical_id] = parse_qty(merged[canonical_id] + qty)

    source_conn = sop.open_warehouse_db(from_warehouse_code)
    try:
        source_conn.row_factory = sqlite3.Row
        resolved: list[dict[str, Any]] = []
        for canonical_id in order_keys:
            row = source_conn.execute(
                "SELECT id, name, unit FROM items WHERE canonical_id=?",
                (canonical_id,),
            ).fetchone()
            if row is None:
                raise ValueError(
                    f"总仓 {from_warehouse_code} 没有 canonical_id={canonical_id} 的品项"
                )
            resolved.append(
                {
                    "canonical_id": canonical_id,
                    "quantity": merged[canonical_id],
                    "unit": str(row["unit"] or "件"),
                }
            )
    finally:
        source_conn.close()

    ts = now()
    transfer_no = generate_transfer_no(master_conn)
    cur = master_conn.execute(
        """INSERT INTO store_orders
           (order_no, store_warehouse_code, dc_warehouse_code, flow_type, status,
            requested_by, expected_delivery_date, note, created_at, updated_at,
            approved_by, approved_at, approved_note, shipping_fee)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
        (
            transfer_no, to_warehouse_code, from_warehouse_code,
            sop.FLOW_DC_TRANSFER, sop.ORDER_STATUS_APPROVED,
            actor_id, expected_delivery_date, note, ts, ts,
            actor_id, ts, "总仓发起调货",
        ),
    )
    order_id = int(cur.lastrowid)
    for item in resolved:
        master_conn.execute(
            """INSERT INTO store_order_items
               (order_id, canonical_id, source_warehouse_code, quantity, unit,
                fulfilled_quantity, shipped_quantity, status, created_at)
               VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?)""",
            (
                order_id, item["canonical_id"], from_warehouse_code,
                item["quantity"], item["unit"],
                sop.ORDER_ITEM_STATUS_PENDING, ts,
            ),
        )
    sop._insert_status_history(
        master_conn, order_id, None, sop.ORDER_STATUS_APPROVED, actor_id,
        "总仓发起调货（免审批）",
    )
    master_conn.commit()
    return sop.get_order_detail(master_conn, order_id) or {}


# ---------------------------------------------------------------------------
# Progress / states
# ---------------------------------------------------------------------------

def transfer_progress(order: dict[str, Any]) -> dict[str, float]:
    """发货 / 接收进度（基础单位）。

    ``pending_receive_qty`` 逐行调用 ``sop.order_item_receivable_qty``，与
    收货路由的门禁**同口径**——列表上显示「可收 N」必须等于点进去能收的
    上限，否则用户会看到「待接收 5」却收不了。
    """
    ordered = shipped = received = pending_receive = 0.0
    for item in order.get("order_items", []):
        ordered += parse_qty(item.get("quantity") or 0)
        shipped += parse_qty(item.get("shipped_quantity") or 0)
        received += parse_qty(item.get("fulfilled_quantity") or 0)
        pending_receive += sop.order_item_receivable_qty(
            str(order.get("status") or ""), item
        )
    return {
        "ordered_qty": parse_qty(ordered),
        "shipped_qty": parse_qty(shipped),
        "received_qty": parse_qty(received),
        "pending_ship_qty": parse_qty(max(0.0, ordered - shipped)),
        "pending_receive_qty": parse_qty(pending_receive),
    }


def document_state(order: dict[str, Any]) -> str:
    """发送单状态（总仓视角）。"""
    status = str(order.get("status") or "")
    if status == sop.ORDER_STATUS_CANCELLED:
        return DOC_CANCELLED
    if status == sop.ORDER_STATUS_DELIVERED:
        return DOC_COMPLETED
    if status == sop.ORDER_STATUS_SHIPPED:
        return DOC_SHIPPED
    progress = transfer_progress(order)
    if progress["shipped_qty"] > 0:
        return DOC_PARTIAL_SHIPPED
    return DOC_AWAITING_SHIP


def receipt_state(order: dict[str, Any]) -> str:
    """接收单状态（分仓视角），由实发 - 已收派生。

    不使用主状态做唯一判据：调货单分批发货期间主状态停在 ``approved``，
    只看主状态会把「已发 5、待收 5」显示成「待发货」。
    """
    status = str(order.get("status") or "")
    if status == sop.ORDER_STATUS_CANCELLED:
        return RCV_CANCELLED
    if status == sop.ORDER_STATUS_DELIVERED:
        return RCV_RECEIVED
    progress = transfer_progress(order)
    if progress["pending_receive_qty"] <= 0:
        return RCV_AWAITING_SHIP
    if progress["received_qty"] > 0:
        return RCV_PARTIAL
    return RCV_PENDING


def enrich_transfers(
    master_conn: sqlite3.Connection,
    orders: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """为列表行补充进度与两种状态（批量查询，避免 N+1）。"""
    if not orders:
        return []
    master_conn.row_factory = sqlite3.Row
    ids = [int(o["id"]) for o in orders]
    placeholders = ",".join("?" for _ in ids)
    rows = master_conn.execute(
        f"""SELECT order_id, quantity, shipped_quantity, fulfilled_quantity
            FROM store_order_items WHERE order_id IN ({placeholders})""",
        ids,
    ).fetchall()
    items_by_order: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        items_by_order.setdefault(int(r["order_id"]), []).append(dict(r))

    out: list[dict[str, Any]] = []
    for order in orders:
        enriched = dict(order)
        enriched["order_items"] = items_by_order.get(int(order["id"]), [])
        progress = transfer_progress(enriched)
        enriched.update(progress)
        enriched["doc_state"] = document_state(enriched)
        enriched["doc_state_label"] = DOC_STATE_LABELS[enriched["doc_state"]]
        enriched["rcv_state"] = receipt_state(enriched)
        enriched["rcv_state_label"] = RCV_STATE_LABELS[enriched["rcv_state"]]
        out.append(enriched)
    return out


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def list_transfers(
    master_conn: sqlite3.Connection,
    *,
    from_warehouse_code: str | None = None,
    to_warehouse_code: str | None = None,
    status: str | None = None,
    only_receivable: bool = False,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """列出调货单（已附加进度 / 发送单状态 / 接收单状态）。

    ``only_receivable=True`` 只返回「有已发未收」的单据，用于分仓的
    待接收列表——它必须包含停在 ``approved`` 的部分发货单。
    """
    orders = sop.list_orders(
        master_conn,
        dc_warehouse_code=from_warehouse_code,
        store_warehouse_code=to_warehouse_code,
        status=status,
        flow_type=sop.FLOW_DC_TRANSFER,
        limit=limit,
    )
    enriched = enrich_transfers(master_conn, orders)
    if only_receivable:
        enriched = [o for o in enriched if o["pending_receive_qty"] > 0]
    return enriched
