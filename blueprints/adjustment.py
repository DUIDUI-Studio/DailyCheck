"""Adjustment orders — pure stock quantity changes that should NOT count
as consumption. Use cases: damaged goods written off, found inventory,
manual corrections. Each request writes a 调整出库 stock_movement for
audit purposes but is excluded from consumption reports by the
WHERE action = '出库' clause in reports.
"""
from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for

from db import get_warehouse_db
from permissions import require_login, require_role
from ._helpers import now
from .auth import audit


bp = Blueprint("adjustment", __name__)


@bp.route("/adjustment", methods=["GET"])
@require_login
def adjustment_list():
    db = get_warehouse_db()
    requests_data = db.execute(
        """SELECT a.*, i.name AS item_name, i.unit
           FROM adjustment_requests a JOIN items i ON i.id = a.item_id
           ORDER BY a.id DESC LIMIT 100"""
    ).fetchall()
    return render_template("adjustment.html", requests=requests_data)


@bp.route("/adjustment/session", methods=["GET"])
@require_login
def adjustment_session():
    db = get_warehouse_db()
    items_data = db.execute(
        """SELECT i.id, i.name, i.quantity, i.unit, i.safety_stock, c.name AS category_name
           FROM items i JOIN categories c ON c.id = i.category_id
           ORDER BY c.name, i.name"""
    ).fetchall()
    return render_template("adjustment_session.html", items=items_data)


@bp.route("/adjustment/submit", methods=["POST"])
@require_login
def adjustment_submit():
    db = get_warehouse_db()
    reason = request.form.get("reason", "").strip()
    items_data = db.execute("SELECT id, quantity FROM items").fetchall()
    rows = []
    for item in items_data:
        raw = request.form.get(f"adjustment_{item['id']}", "").strip()
        if raw == "":
            continue
        qty = int(raw)
        if qty < 0:
            continue
        # No upper bound on adjustment quantity — consistent with
        # restock.delete which now allows negative stock. Operators
        # can use adjustments to write off known losses, even when
        # current stock is already zero or negative.
        rows.append((int(item["id"]), qty))
    if not rows:
        flash("请至少填写一个调整数量（0 表示无变化但仍会留记录）")
        return redirect(url_for("adjustment.adjustment_session"))
    for item_id, qty in rows:
        cur = db.execute(
            """INSERT INTO adjustment_requests
               (item_id, adjusted_quantity, reason, rolled_back, created_at)
               VALUES (?, ?, ?, 0, ?)""",
            (item_id, qty, reason, now()),
        )
        req_id = int(cur.lastrowid)
        db.execute(
            "UPDATE items SET quantity = quantity - ?, updated_at = ? WHERE id = ?",
            (qty, now(), item_id),
        )
        db.execute(
            """INSERT INTO stock_movements (item_id, action, delta, note, created_at)
               VALUES (?, '调整出库', ?, ?, ?)""",
            (item_id, -qty, f"调整单#{req_id}", now()),
        )
    db.commit()
    audit("adjustment.submit", "request", None, {"rows": rows})
    flash("调整单已执行")
    return redirect(url_for("adjustment.adjustment_list"))


@bp.route("/adjustment/<int:req_id>/rollback", methods=["POST"])
@require_role("manager")
def rollback(req_id: int):
    db = get_warehouse_db()
    req = db.execute(
        "SELECT item_id, adjusted_quantity, rolled_back FROM adjustment_requests WHERE id = ?",
        (req_id,),
    ).fetchone()
    if req is None:
        flash("调整记录不存在")
        return redirect(url_for("adjustment.adjustment_list"))
    if int(req["rolled_back"]) == 1:
        flash("该记录已回退，无需重复操作")
        return redirect(url_for("adjustment.adjustment_list"))
    db.execute(
        "UPDATE items SET quantity = quantity + ?, updated_at = ? WHERE id = ?",
        (int(req["adjusted_quantity"]), now(), int(req["item_id"])),
    )
    db.execute(
        """INSERT INTO stock_movements (item_id, action, delta, note, created_at)
           VALUES (?, '调整出库回滚', ?, ?, ?)""",
        (int(req["item_id"]), int(req["adjusted_quantity"]), f"回滚调整单#{req_id}", now()),
    )
    db.execute("UPDATE adjustment_requests SET rolled_back = 1 WHERE id = ?", (req_id,))
    db.commit()
    audit("adjustment.rollback", "request", req_id)
    flash("调整记录已回退")
    return redirect(url_for("adjustment.adjustment_list"))


@bp.route("/adjustment/<int:req_id>/delete", methods=["POST"])
@require_role("manager")
def delete(req_id: int):
    db = get_warehouse_db()
    db.execute("DELETE FROM adjustment_requests WHERE id = ?", (req_id,))
    db.commit()
    audit("adjustment.delete", "request", req_id)
    flash("调整记录已删除")
    return redirect(url_for("adjustment.adjustment_list"))
