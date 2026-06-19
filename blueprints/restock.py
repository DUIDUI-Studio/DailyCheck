"""Restock requests: create, submit (auto-applies as inbound), update
status, delete (with quantity rollback if it was already applied)."""
from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for

from db import get_warehouse_db
from permissions import require_login, require_role
from ._helpers import now
from .auth import audit


bp = Blueprint("restock", __name__)


@bp.route("/restock", methods=["GET"])
@require_login
def restock_list():
    db = get_warehouse_db()
    requests_data = db.execute(
        """SELECT r.*, i.name AS item_name, i.unit
           FROM restock_requests r JOIN items i ON i.id = r.item_id
           ORDER BY r.id DESC LIMIT 100"""
    ).fetchall()
    return render_template("restock.html", requests=requests_data)


@bp.route("/restock/start", methods=["POST"])
@require_login
def restock_start():
    return redirect(url_for("restock.restock_session"))


@bp.route("/restock/session", methods=["GET"])
@require_login
def restock_session():
    db = get_warehouse_db()
    items_data = db.execute(
        """SELECT i.id, i.name, i.quantity, i.unit, i.safety_stock, c.name AS category_name
           FROM items i JOIN categories c ON c.id = i.category_id
           ORDER BY c.name, i.name"""
    ).fetchall()
    return render_template("restock_session.html", items=items_data)


@bp.route("/restock/submit", methods=["POST"])
@require_login
def restock_submit():
    db = get_warehouse_db()
    reason = request.form.get("reason", "").strip()
    items_data = db.execute("SELECT id FROM items").fetchall()
    rows = []
    for item in items_data:
        raw = request.form.get(f"restock_{item['id']}", "").strip()
        if raw == "":
            continue
        qty = int(raw)
        if qty > 0:
            rows.append((int(item["id"]), qty))
    if not rows:
        flash("请至少填写一个补货数量")
        return redirect(url_for("restock.restock_session"))
    for item_id, qty in rows:
        cur = db.execute(
            """INSERT INTO restock_requests
               (item_id, requested_quantity, reason, status, created_at)
               VALUES (?, ?, ?, '入库', ?)""",
            (item_id, qty, reason, now()),
        )
        req_id = int(cur.lastrowid)
        db.execute(
            "UPDATE items SET quantity = quantity + ?, updated_at = ? WHERE id = ?",
            (qty, now(), item_id),
        )
        db.execute(
            """INSERT INTO stock_movements (item_id, action, delta, note, created_at)
               VALUES (?, '补货入库', ?, ?, ?)""",
            (item_id, qty, f"补货记录#{req_id}入库", now()),
        )
    db.commit()
    audit("restock.submit", "request", None, {"rows": rows})
    flash("入库已执行")
    return redirect(url_for("restock.restock_list"))


@bp.route("/restock/<int:req_id>/status", methods=["POST"])
@require_login
def update_status(req_id: int):
    status = request.form.get("status", "").strip()
    if status not in {"提交", "入库"}:
        flash("状态非法")
        return redirect(url_for("restock.restock_list"))
    db = get_warehouse_db()
    req = db.execute(
        "SELECT item_id, requested_quantity, status FROM restock_requests WHERE id = ?",
        (req_id,),
    ).fetchone()
    if req is None:
        flash("补货记录不存在")
        return redirect(url_for("restock.restock_list"))
    old_status = req["status"]
    if old_status != "入库" and status == "入库":
        db.execute(
            "UPDATE items SET quantity = quantity + ?, updated_at = ? WHERE id = ?",
            (int(req["requested_quantity"]), now(), int(req["item_id"])),
        )
        db.execute(
            """INSERT INTO stock_movements (item_id, action, delta, note, created_at)
               VALUES (?, '补货入库', ?, ?, ?)""",
            (
                int(req["item_id"]),
                int(req["requested_quantity"]),
                f"补货记录#{req_id}入库",
                now(),
            ),
        )
    db.execute("UPDATE restock_requests SET status = ? WHERE id = ?", (status, req_id))
    db.commit()
    audit("restock.status", "request", req_id, {"from": old_status, "to": status})
    flash("补货记录状态已更新")
    return redirect(url_for("restock.restock_list"))


@bp.route("/restock/<int:req_id>/delete", methods=["POST"])
@require_role("manager")
def delete(req_id: int):
    db = get_warehouse_db()
    req = db.execute(
        "SELECT item_id, requested_quantity, status FROM restock_requests WHERE id = ?",
        (req_id,),
    ).fetchone()
    if req is None:
        flash("补货记录不存在")
        return redirect(url_for("restock.restock_list"))
    if req["status"] == "入库":
        item = db.execute(
            "SELECT quantity FROM items WHERE id = ?",
            (int(req["item_id"]),),
        ).fetchone()
        if item is None:
            flash("库存品不存在，无法删除该记录")
            return redirect(url_for("restock.restock_list"))
        if int(item["quantity"]) < int(req["requested_quantity"]):
            flash("当前库存不足，无法通过删除回滚该入库记录")
            return redirect(url_for("restock.restock_list"))
        db.execute(
            "UPDATE items SET quantity = quantity - ?, updated_at = ? WHERE id = ?",
            (int(req["requested_quantity"]), now(), int(req["item_id"])),
        )
        db.execute(
            """INSERT INTO stock_movements (item_id, action, delta, note, created_at)
               VALUES (?, '补货删除回滚', ?, ?, ?)""",
            (
                int(req["item_id"]),
                -int(req["requested_quantity"]),
                f"删除补货记录#{req_id}回滚",
                now(),
            ),
        )
    db.execute("DELETE FROM restock_requests WHERE id = ?", (req_id,))
    db.commit()
    audit("restock.delete", "request", req_id)
    flash("补货记录已删除")
    return redirect(url_for("restock.restock_list"))
