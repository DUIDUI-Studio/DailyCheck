"""Stocktake workflows: start a batch, fill in actuals, submit, rollback,
edit, approve. Approval requires manager role.
"""
from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for

from db import get_warehouse_db
from permissions import require_login, require_role
from ._helpers import now, parse_qty
from .auth import audit


bp = Blueprint("stocktake", __name__)


@bp.route("/stocktake", methods=["GET"])
@require_login
def stocktake_list():
    db = get_warehouse_db()
    batches = db.execute(
        """SELECT b.id, b.created_at, b.note, b.rolled_back, COUNT(s.id) AS item_count
           FROM stocktake_batches b
           LEFT JOIN stocktakes s ON s.batch_id = b.id
           GROUP BY b.id ORDER BY b.id DESC LIMIT 20"""
    ).fetchall()
    return render_template("stocktake.html", batches=batches)


@bp.route("/stocktake/start", methods=["POST"])
@require_login
def stocktake_start():
    return redirect(url_for("stocktake.stocktake_session"))


@bp.route("/stocktake/session", methods=["GET"])
@require_login
def stocktake_session():
    db = get_warehouse_db()
    items_data = db.execute(
        """SELECT i.id, i.name, i.quantity, i.unit, i.safety_stock, c.name AS category_name
           FROM items i JOIN categories c ON c.id = i.category_id
           ORDER BY c.name, i.name"""
    ).fetchall()
    return render_template("stocktake_session.html", items=items_data)


@bp.route("/stocktake/submit", methods=["POST"])
@require_login
def stocktake_submit():
    db = get_warehouse_db()
    note = request.form.get("note", "").strip()
    items_data = db.execute("SELECT id, quantity FROM items").fetchall()

    changed_rows = []
    for item in items_data:
        raw = request.form.get(f"actual_{item['id']}", "").strip()
        if raw == "":
            continue
        actual_quantity = parse_qty(raw)
        previous_quantity = parse_qty(item["quantity"])
        diff = actual_quantity - previous_quantity
        changed_rows.append((int(item["id"]), previous_quantity, actual_quantity, diff))

    if not changed_rows:
        flash("请至少填写一个盘点数量")
        return redirect(url_for("stocktake.stocktake_session"))

    cur = db.execute(
        "INSERT INTO stocktake_batches (created_at, note, status, rolled_back) VALUES (?, ?, 'pending', 0)",
        (now(), note),
    )
    batch_id = cur.lastrowid
    for item_id, previous_quantity, actual_quantity, diff in changed_rows:
        db.execute(
            """INSERT INTO stocktakes
               (item_id, previous_quantity, actual_quantity, diff, batch_id, created_at, note)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (item_id, previous_quantity, actual_quantity, diff, batch_id, now(), note),
        )
    db.commit()
    audit("stocktake.submit", "batch", batch_id, {"count": len(changed_rows)})
    flash(f"盘点批次 #{batch_id} 已提交，等待审核")
    return redirect(url_for("stocktake.stocktake_list"))


@bp.route("/stocktake/batch/<int:batch_id>/rollback", methods=["POST"])
@require_role("staff")
def rollback(batch_id: int):
    db = get_warehouse_db()
    batch = db.execute(
        "SELECT id, status, rolled_back FROM stocktake_batches WHERE id = ?", (batch_id,)
    ).fetchone()
    if batch is None:
        flash("盘点批次不存在")
        return redirect(url_for("stocktake.stocktake_list"))
    if int(batch["rolled_back"]) == 1:
        flash("该批次已回滚，无需重复操作")
        return redirect(url_for("stocktake.stocktake_list"))

    records = db.execute(
        "SELECT item_id, diff FROM stocktakes WHERE batch_id = ?", (batch_id,)
    ).fetchall()
    for record in records:
        diff = parse_qty(record["diff"])
        if diff == 0:
            continue
        item_id = int(record["item_id"])
        db.execute(
            "UPDATE items SET quantity = quantity - ?, updated_at = ? WHERE id = ?",
            (diff, now(), item_id),
        )
        db.execute(
            """INSERT INTO stock_movements (item_id, action, delta, note, created_at)
               VALUES (?, '盘点回滚', ?, ?, ?)""",
            (item_id, -diff, f"回滚盘点批次#{batch_id}", now()),
        )

    # Reverse any synthetic outbound_requests written at approve time
    # so /summary no longer counts the loss as consumption. We hard-
    # delete the row rather than flipping rolled_back, because the
    # user's intent at this point is "undo the approval entirely".
    batch_row = db.execute(
        "SELECT loss_req_ids FROM stocktake_batches WHERE id=?", (batch_id,)
    ).fetchone()
    if batch_row and batch_row["loss_req_ids"]:
        for req_id in (
            int(x) for x in batch_row["loss_req_ids"].split(",") if x.strip()
        ):
            db.execute(
                "DELETE FROM outbound_requests WHERE id=? AND reason=?",
                (req_id, f"盘点审核#{batch_id}盘亏"),
            )

    db.execute(
        "UPDATE stocktake_batches SET rolled_back = 1, status = 'rolled_back' WHERE id = ?",
        (batch_id,),
    )
    db.commit()
    audit("stocktake.rollback", "batch", batch_id)
    flash(f"盘点批次 #{batch_id} 已回滚")
    return redirect(url_for("stocktake.stocktake_list"))


@bp.route("/stocktake/batch/<int:batch_id>/edit", methods=["GET"])
@require_role("staff")
def edit(batch_id: int):
    db = get_warehouse_db()
    batch = db.execute(
        "SELECT id, note, rolled_back FROM stocktake_batches WHERE id = ?", (batch_id,)
    ).fetchone()
    if batch is None:
        flash("盘点批次不存在")
        return redirect(url_for("stocktake.stocktake_list"))
    records = db.execute(
        """SELECT s.id, s.item_id, s.previous_quantity, s.actual_quantity, s.diff,
                  i.name AS item_name, i.quantity AS current_quantity, i.unit,
                  c.name AS category_name
           FROM stocktakes s
           JOIN items i ON i.id = s.item_id
           JOIN categories c ON c.id = i.category_id
           WHERE s.batch_id = ? ORDER BY c.name, i.name""",
        (batch_id,),
    ).fetchall()
    return render_template("stocktake_edit.html", batch=batch, records=records)


@bp.route("/stocktake/batch/<int:batch_id>/edit", methods=["POST"])
@require_role("staff")
def submit_edit(batch_id: int):
    db = get_warehouse_db()
    batch = db.execute(
        "SELECT id, rolled_back FROM stocktake_batches WHERE id = ?", (batch_id,)
    ).fetchone()
    if batch is None:
        flash("盘点批次不存在")
        return redirect(url_for("stocktake.stocktake_list"))
    if int(batch["rolled_back"]) == 1:
        flash("已回滚的批次不能修改")
        return redirect(url_for("stocktake.stocktake_list"))

    records = db.execute(
        "SELECT id, item_id FROM stocktakes WHERE batch_id = ?", (batch_id,)
    ).fetchall()
    changed = 0
    for rec in records:
        raw = request.form.get(f"actual_{rec['item_id']}", "").strip()
        if raw == "":
            continue
        new_actual = parse_qty(raw)
        current_qty = parse_qty(
            db.execute(
                "SELECT quantity FROM items WHERE id = ?", (int(rec["item_id"]),)
            ).fetchone()["quantity"]
        )
        new_diff = new_actual - current_qty
        if new_diff == 0:
            continue
        db.execute(
            "UPDATE items SET quantity = ?, updated_at = ? WHERE id = ?",
            (new_actual, now(), int(rec["item_id"])),
        )
        db.execute(
            "UPDATE stocktakes SET actual_quantity = ?, diff = ? WHERE id = ?",
            (new_actual, new_diff, int(rec["id"])),
        )
        db.execute(
            """INSERT INTO stock_movements (item_id, action, delta, note, created_at)
               VALUES (?, '盘点修正', ?, ?, ?)""",
            (int(rec["item_id"]), new_diff, f"修正盘点批次#{batch_id}", now()),
        )
        changed += 1
    if changed == 0:
        flash("未检测到变更")
    else:
        db.commit()
        audit("stocktake.edit", "batch", batch_id, {"changed": changed})
        flash(f"盘点批次 #{batch_id} 已更新 {changed} 项")
    return redirect(url_for("stocktake.stocktake_list"))


@bp.route("/stocktake/batch/<int:batch_id>/approve", methods=["POST"])
@require_role("staff")
def approve(batch_id: int):
    """Approval semantics (口径 B):
    - 盘亏 (diff < 0): real consumption. We write a synthetic
      outbound_requests row (rolled_back=0) so /summary counts it
      in total_consumed_value, AND the matching stock_movements
      action='出库' row for the flow / audit trail.
    - 盘盈 (diff > 0): only inventory adjustment (账面增, NOT counted
      as consumption). Written to stock_movements.action='库存调整'.
      Operators can use the adjustment-order page to write off a
      盘盈 that was actually a previous 出库-mis-entry.

    Rollback reverses both: deletes the synthetic outbound_requests,
    or flips rolled_back=1 if a later rollback is needed.
    """
    db = get_warehouse_db()
    batch = db.execute(
        "SELECT id, status, rolled_back FROM stocktake_batches WHERE id = ?", (batch_id,)
    ).fetchone()
    if batch is None:
        flash("盘点批次不存在")
        return redirect(url_for("stocktake.stocktake_list"))
    if batch["status"] == "approved":
        flash("该批次已审核通过，无需重复操作")
        return redirect(url_for("stocktake.stocktake_list"))
    if int(batch["rolled_back"]) == 1:
        flash("该批次已回滚，无法审核")
        return redirect(url_for("stocktake.stocktake_list"))

    records = db.execute(
        "SELECT item_id, diff FROM stocktakes WHERE batch_id = ?", (batch_id,)
    ).fetchall()
    loss_items, gain_items = [], []
    for record in records:
        diff = parse_qty(record["diff"])
        if diff == 0:
            continue
        item_id = int(record["item_id"])
        (loss_items if diff < 0 else gain_items).append((item_id, diff))

    # Apply stock changes first.
    for item_id, diff in loss_items + gain_items:
        db.execute(
            "UPDATE items SET quantity = quantity + ?, updated_at = ? WHERE id = ?",
            (diff, now(), item_id),
        )

    # 盘亏 → write a synthetic outbound request so summary counts it.
    # Store the new req id in the audit so rollback can find it.
    loss_req_ids = []
    for item_id, diff in loss_items:
        loss_qty = abs(diff)
        cur = db.execute(
            """INSERT INTO outbound_requests
               (item_id, requested_quantity, reason, status, rolled_back, created_at)
               VALUES (?, ?, ?, '出库', 0, ?)""",
            (
                item_id, loss_qty,
                f"盘点审核#{batch_id}盘亏",
                now(),
            ),
        )
        loss_req_ids.append(int(cur.lastrowid))
        db.execute(
            """INSERT INTO stock_movements (item_id, action, delta, note, created_at)
               VALUES (?, '出库', ?, ?, ?)""",
            (item_id, diff, f"盘点审核#{batch_id}盘亏 (req#{loss_req_ids[-1]})", now()),
        )

    # 盘盈 → inventory adjustment only (NOT a consumption).
    for item_id, diff in gain_items:
        db.execute(
            """INSERT INTO stock_movements (item_id, action, delta, note, created_at)
               VALUES (?, '库存调整', ?, ?, ?)""",
            (item_id, diff, f"盘点审核#{batch_id}盘盈", now()),
        )

    db.execute(
        "UPDATE stocktake_batches SET status = 'approved', loss_req_ids = ? WHERE id = ?",
        (",".join(str(i) for i in loss_req_ids), batch_id),
    )
    db.commit()
    audit("stocktake.approve", "batch", batch_id, {
        "losses": len(loss_items),
        "gains": len(gain_items),
        "loss_req_ids": loss_req_ids,
    })
    msg = f"盘点批次 #{batch_id} 已审核"
    if loss_items:
        msg += f"，{len(loss_items)}项盘亏已计入消耗"
    if gain_items:
        msg += f"，{len(gain_items)}项盘盈已调整为库存"
    flash(msg)
    return redirect(url_for("stocktake.stocktake_list"))
