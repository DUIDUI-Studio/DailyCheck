"""Outbound requests: create, submit, rollback, delete."""
from __future__ import annotations

from decimal import Decimal

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from db import get_master_db, get_warehouse_db
from permissions import require_login, require_permission, require_role

from . import canonical_pure as cp
from . import store_ordering_pure as sop
from ._helpers import now, parse_qty
from .auth import audit

bp = Blueprint("outbound", __name__)

_SOURCE_LABELS = {
    "manual": "人工出库",
    "production": "生产领料",
    "stocktake_loss": "盘点盘亏",
}


@bp.before_request
def _require_storefront():
    """Manual outbound is storefront-only; DC reads its order-shipment ledger."""
    from flask import abort

    from permissions import WAREHOUSE_EXEMPT
    if request.endpoint in WAREHOUSE_EXEMPT or request.endpoint is None:
        return None
    if g.user is not None and g.user["is_admin"]:
        return None
    wh = g.get("warehouse")
    if wh is None:
        return None
    if request.endpoint == "outbound.outbound_list" and wh["warehouse_type"] in (
        "storefront",
        "distribution_center",
    ):
        return None
    if wh["warehouse_type"] != "storefront":
        flash("当前仓库无人工出库功能")
        abort(403)
        return None
    return None


def _order_number_from_note(note: str | None) -> str | None:
    for token in (note or "").split():
        if len(token) > 1 and token.startswith("#"):
            return token[1:]
    return None


def _list_order_shipments(db) -> list[dict]:
    """Read order-generated DC stock movements for the outbound history page."""
    rows = db.execute(
        """SELECT sm.id, sm.created_at, sm.action, sm.delta, sm.note,
                  i.name AS item_name, i.unit
           FROM stock_movements sm
           JOIN items i ON i.id = sm.item_id
           WHERE sm.action IN (?, ?)
           ORDER BY sm.id DESC LIMIT 100""",
        (sop.SHIPMENT_ACTION, sop.TRANSFER_SHIPMENT_ACTION),
    ).fetchall()
    if not rows:
        return []

    order_numbers = {
        order_no
        for row in rows
        if (order_no := _order_number_from_note(row["note"])) is not None
    }
    orders_by_no: dict[str, int] = {}
    if order_numbers:
        placeholders = ",".join("?" * len(order_numbers))
        master = get_master_db()
        for order in master.execute(
            f"""SELECT id, order_no FROM store_orders
                WHERE order_no IN ({placeholders})""",
            tuple(order_numbers),
        ).fetchall():
            orders_by_no[order["order_no"]] = int(order["id"])

    shipments = []
    for row in rows:
        order_no = _order_number_from_note(row["note"])
        order_id = orders_by_no.get(order_no or "")
        shipments.append({
            "id": None,
            "created_at": row["created_at"],
            "item_name": row["item_name"],
            "requested_quantity": abs(parse_qty(row["delta"])),
            "unit": row["unit"],
            "reason": row["note"],
            "status": "已出库",
            "rolled_back": 0,
            "is_order_shipment": True,
            "source_label": "订单发货" if row["action"] == sop.SHIPMENT_ACTION else "调货发货",
            "order_no": order_no,
            "order_url": (
                url_for("store_ordering.order_detail", order_id=order_id)
                if order_id is not None else None
            ),
        })
    return shipments


@bp.route("/outbound", methods=["GET"])
@require_login
def outbound_list():
    db = get_warehouse_db()
    warehouse_type = g.warehouse["warehouse_type"] if g.get("warehouse") else None
    manual_rows = db.execute(
        """SELECT o.*, i.name AS item_name, i.unit
           FROM outbound_requests o JOIN items i ON i.id = o.item_id
           ORDER BY o.id DESC LIMIT 100"""
    ).fetchall()
    requests_data = []
    for row in manual_rows:
        record = dict(row)
        record.update({
            "is_order_shipment": False,
            "source_label": _SOURCE_LABELS.get(record.get("source_type"), "其他来源"),
            "order_no": None,
            "order_url": None,
        })
        requests_data.append(record)
    if warehouse_type == "distribution_center":
        requests_data.extend(_list_order_shipments(db))
        requests_data.sort(key=lambda row: (row["created_at"], row["id"] or 0), reverse=True)
        requests_data = requests_data[:100]
    return render_template(
        "outbound.html",
        requests=requests_data,
        can_create=warehouse_type == "storefront" or bool(g.user["is_admin"]),
        show_order_stock_note=warehouse_type == "distribution_center",
    )


@bp.route("/outbound/start", methods=["POST"])
@require_login
def outbound_start():
    return redirect(url_for("outbound.outbound_session"))


@bp.route("/outbound/session", methods=["GET"])
@require_login
def outbound_session():
    db = get_warehouse_db()
    items_data = db.execute(
        f"""SELECT i.id, i.name, i.quantity, i.unit, i.safety_stock,
                  i.aux_unit, i.aux_rate, c.name AS category_name
           FROM items i JOIN categories c ON c.id = i.category_id
           WHERE {cp.store_selectable_clause('i')}
           ORDER BY c.name, i.name"""
    ).fetchall()
    return render_template("outbound_session.html", items=items_data)


@bp.route("/outbound/submit", methods=["POST"])
@require_permission("stock.outbound")
def outbound_submit():
    db = get_warehouse_db()
    reason = request.form.get("reason", "").strip()
    items_data = db.execute("SELECT id, quantity, aux_unit, aux_rate FROM items").fetchall()
    from ._helpers import aux_to_base
    rows = []
    for item in items_data:
        raw = request.form.get(f"outbound_{item['id']}", "").strip()
        if raw == "":
            continue
        qty_raw = parse_qty(raw)
        if qty_raw <= 0:
            continue
        unit_choice = request.form.get(f"outbound_{item['id']}_unit", "base")
        if unit_choice == "aux":
            aux_rate = float(item["aux_rate"] or 0)
            if aux_rate <= 0:
                flash("存在品项未启用辅单位，请用基础单位录入")
                return redirect(url_for("outbound.outbound_session"))
            qty = aux_to_base(qty_raw, aux_rate)
        else:
            qty = qty_raw
        if qty > Decimal(str(item["quantity"])):
            flash("存在出库数量大于当前库存的品项，请检查后重试")
            return redirect(url_for("outbound.outbound_session"))
        rows.append((int(item["id"]), qty))
    if not rows:
        flash("请至少填写一个出库数量")
        return redirect(url_for("outbound.outbound_session"))
    from blueprints.procurement import mark_procurement_invalid
    for item_id, qty in rows:
        cur = db.execute(
            """INSERT INTO outbound_requests
               (item_id, requested_quantity, reason, status, rolled_back,
                created_at, source_type)
               VALUES (?, ?, ?, '出库', 0, ?, 'manual')""",
            (item_id, qty, reason, now()),
        )
        req_id = int(cur.lastrowid)
        db.execute(
            "UPDATE items SET quantity = quantity - ?, updated_at = ? WHERE id = ?",
            (qty, now(), item_id),
        )
        db.execute(
            """INSERT INTO stock_movements (item_id, action, delta, note, created_at)
               VALUES (?, '出库', ?, ?, ?)""",
            (item_id, -qty, f"出库记录#{req_id}出库", now()),
        )
        mark_procurement_invalid(item_id)
    db.commit()
    audit("outbound.submit", "request", None, {"rows": rows})
    flash("出库已执行")
    return redirect(url_for("outbound.outbound_list"))


@bp.route("/outbound/<int:req_id>/rollback", methods=["POST"])
@require_permission("stock.outbound.rollback")
def rollback(req_id: int):
    db = get_warehouse_db()
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("BEGIN IMMEDIATE")
    try:
        req = db.execute(
            """SELECT item_id, requested_quantity, rolled_back
               FROM outbound_requests WHERE id = ? AND status = '出库'""",
            (req_id,),
        ).fetchone()
        if req is None:
            flash("出库记录不存在")
            return redirect(url_for("outbound.outbound_list"))
        if int(req["rolled_back"]) == 1:
            flash("该记录已回退，无需重复操作")
            return redirect(url_for("outbound.outbound_list"))
        db.execute(
            "UPDATE items SET quantity = quantity + ?, updated_at = ? WHERE id = ?",
            (parse_qty(req["requested_quantity"]), now(), int(req["item_id"])),
        )
        db.execute(
            """INSERT INTO stock_movements (item_id, action, delta, note, created_at)
               VALUES (?, '出库回退', ?, ?, ?)""",
            (int(req["item_id"]), parse_qty(req["requested_quantity"]), f"回退出库记录#{req_id}", now()),
        )
        db.execute("UPDATE outbound_requests SET rolled_back = 1 WHERE id = ?", (req_id,))
        db.commit()
    finally:
        # Also releases the write lock on validation/early return and exceptions.
        if db.in_transaction:
            db.rollback()

    audit("outbound.rollback", "request", req_id)
    flash("出库记录已回退")
    return redirect(url_for("outbound.outbound_list"))


@bp.route("/outbound/<int:req_id>/delete", methods=["POST"])
@require_role("staff")
def delete(req_id: int):
    """Remove an outbound record and return its quantity to stock.

    Aligned with the restock.delete semantic: deleting a record is
    "undo this transaction". If the operator only wants to clean up
    audit noise without touching stock, they should NOT use delete.

    No quantity check: returning stock to items is always safe
    (cannot push quantity negative, only increase it). Already-
    rolled-back rows are silently removed without another reversal.
    """
    db = get_warehouse_db()
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("BEGIN IMMEDIATE")
    try:
        req = db.execute(
            """SELECT item_id, requested_quantity, rolled_back
               FROM outbound_requests WHERE id = ?""",
            (req_id,),
        ).fetchone()
        if req is None:
            flash("出库记录不存在")
            return redirect(url_for("outbound.outbound_list"))

        if int(req["rolled_back"]) == 0:
            qty = parse_qty(req["requested_quantity"])
            db.execute(
                "UPDATE items SET quantity = quantity + ?, updated_at = ? WHERE id = ?",
                (qty, now(), int(req["item_id"])),
            )
            db.execute(
                """INSERT INTO stock_movements (item_id, action, delta, note, created_at)
                   VALUES (?, '出库回退', ?, ?, ?)""",
                (int(req["item_id"]), qty, f"删除出库记录#{req_id}回滚", now()),
            )

        db.execute("DELETE FROM outbound_requests WHERE id = ?", (req_id,))
        db.commit()
    finally:
        # Also releases the write lock on validation/early return and exceptions.
        if db.in_transaction:
            db.rollback()

    audit("outbound.delete", "request", req_id, {
        "rolled_back": int(req["rolled_back"]),
        "qty": parse_qty(req["requested_quantity"]),
    })
    flash("出库记录已删除，库存已归还")
    return redirect(url_for("outbound.outbound_list"))
