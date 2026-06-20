"""Outbound requests: create, submit, rollback, delete."""
from __future__ import annotations

from decimal import Decimal

from flask import Blueprint, flash, redirect, render_template, request, url_for

from db import get_warehouse_db
from permissions import require_login, require_role
from ._helpers import now, parse_qty
from .auth import audit


bp = Blueprint("outbound", __name__)


@bp.route("/outbound", methods=["GET"])
@require_login
def outbound_list():
    db = get_warehouse_db()
    requests_data = db.execute(
        """SELECT o.*, i.name AS item_name, i.unit
           FROM outbound_requests o JOIN items i ON i.id = o.item_id
           ORDER BY o.id DESC LIMIT 100"""
    ).fetchall()
    return render_template("outbound.html", requests=requests_data)


@bp.route("/outbound/start", methods=["POST"])
@require_login
def outbound_start():
    return redirect(url_for("outbound.outbound_session"))


@bp.route("/outbound/session", methods=["GET"])
@require_login
def outbound_session():
    db = get_warehouse_db()
    items_data = db.execute(
        """SELECT i.id, i.name, i.quantity, i.unit, i.safety_stock, c.name AS category_name
           FROM items i JOIN categories c ON c.id = i.category_id
           ORDER BY c.name, i.name"""
    ).fetchall()
    return render_template("outbound_session.html", items=items_data)


@bp.route("/outbound/submit", methods=["POST"])
@require_login
def outbound_submit():
    db = get_warehouse_db()
    reason = request.form.get("reason", "").strip()
    items_data = db.execute("SELECT id, quantity FROM items").fetchall()
    rows = []
    for item in items_data:
        raw = request.form.get(f"outbound_{item['id']}", "").strip()
        if raw == "":
            continue
        qty = parse_qty(raw)
        if qty <= 0:
            continue
        if qty > Decimal(str(item["quantity"])):
            flash("存在出库数量大于当前库存的品项，请检查后重试")
            return redirect(url_for("outbound.outbound_session"))
        rows.append((int(item["id"]), qty))
    if not rows:
        flash("请至少填写一个出库数量")
        return redirect(url_for("outbound.outbound_session"))
    for item_id, qty in rows:
        cur = db.execute(
            """INSERT INTO outbound_requests
               (item_id, requested_quantity, reason, status, rolled_back, created_at)
               VALUES (?, ?, ?, '出库', 0, ?)""",
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
    db.commit()
    audit("outbound.submit", "request", None, {"rows": rows})
    flash("出库已执行")
    return redirect(url_for("outbound.outbound_list"))


@bp.route("/outbound/<int:req_id>/rollback", methods=["POST"])
@require_role("manager")
def rollback(req_id: int):
    db = get_warehouse_db()
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
    audit("outbound.rollback", "request", req_id)
    flash("出库记录已回退")
    return redirect(url_for("outbound.outbound_list"))


@bp.route("/outbound/<int:req_id>/delete", methods=["POST"])
@require_role("manager")
def delete(req_id: int):
    db = get_warehouse_db()
    db.execute("DELETE FROM outbound_requests WHERE id = ?", (req_id,))
    db.commit()
    audit("outbound.delete", "request", req_id)
    flash("出库记录已删除")
    return redirect(url_for("outbound.outbound_list"))
