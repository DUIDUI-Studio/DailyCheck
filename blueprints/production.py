"""Production: products, BOM, runs, rollback, delete, CSV export."""
from __future__ import annotations

import sqlite3

from flask import Blueprint, flash, redirect, request, url_for

from db import get_warehouse_db
from permissions import require_login, require_role
from ._helpers import now, parse_qty, render
from .auth import audit


bp = Blueprint("production", __name__)


@bp.route("/production", methods=["GET"])
@require_login
def products_list():
    db = get_warehouse_db()
    products_data = db.execute(
        """SELECT p.*, COUNT(b.id) AS bom_count
           FROM products p
           LEFT JOIN product_bom b ON b.product_id = p.id
           GROUP BY p.id ORDER BY p.id DESC"""
    ).fetchall()
    return render("production/products.html", products=products_data)


def _load_items_for_bom():
    """Return items for the BOM item picker: id, name, unit, category_name."""
    db = get_warehouse_db()
    return db.execute(
        """SELECT i.id, i.name, i.unit, c.name AS category_name
           FROM items i JOIN categories c ON c.id = i.category_id
           ORDER BY c.name, i.name"""
    ).fetchall()


@bp.route("/production/products/new", methods=["GET", "POST"])
@require_role("manager")
def product_new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        unit = request.form.get("unit", "件").strip() or "件"
        note = request.form.get("note", "").strip()
        if not name:
            flash("产品名称为必填")
            return redirect(url_for("production.product_new"))
        db = get_warehouse_db()
        try:
            db.execute(
                "INSERT INTO products (name, unit, note, created_at) VALUES (?, ?, ?, ?)",
                (name, unit, note, now()),
            )
            new_id = int(db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
            db.commit()
            audit("production.product.create", "product", new_id, {"name": name})
            flash("产品已创建，请补充配方")
            return redirect(url_for("production.product_edit", product_id=new_id))
        except sqlite3.IntegrityError:
            flash("产品名称已存在")
            return redirect(url_for("production.product_new"))
    return render("production/product_edit.html", product=None, bom_rows=[], items=_load_items_for_bom())


@bp.route("/production/products/<int:product_id>/edit", methods=["GET", "POST"])
@require_role("manager")
def product_edit(product_id: int):
    db = get_warehouse_db()
    product = db.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if product is None:
        flash("产品不存在")
        return redirect(url_for("production.products_list"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        unit = request.form.get("unit", "件").strip() or "件"
        note = request.form.get("note", "").strip()
        if not name:
            flash("产品名称为必填")
            return redirect(url_for("production.product_edit", product_id=product_id))
        db.execute(
            "UPDATE products SET name=?, unit=?, note=? WHERE id=?",
            (name, unit, note, product_id),
        )
        # BOM rows: parallel arrays. bom_row_id[] identifies existing rows
        # (empty string = new); bom_delete[]='1' removes that row. We do
        # NOT need a separate product_bom check on delete — schema has
        # ON DELETE CASCADE from products (see db/__init__.py).
        bom_ids = request.form.getlist("bom_row_id")
        item_ids = request.form.getlist("bom_item_id")
        qtys = request.form.getlist("bom_qty")
        deletes = request.form.getlist("bom_delete")
        added, removed, updated = 0, 0, 0
        for i in range(len(bom_ids)):
            row_id = bom_ids[i].strip()
            if i < len(deletes) and deletes[i] == "1":
                if row_id:
                    db.execute("DELETE FROM product_bom WHERE id = ?", (int(row_id),))
                    removed += 1
                continue
            item_id = item_ids[i].strip() if i < len(item_ids) else ""
            qty = parse_qty(qtys[i]) if i < len(qtys) else 0.0
            if not item_id or qty <= 0:
                continue
            if row_id:
                db.execute(
                    "UPDATE product_bom SET item_id=?, qty_per_unit=? WHERE id=?",
                    (int(item_id), qty, int(row_id)),
                )
                updated += 1
            else:
                try:
                    db.execute(
                        "INSERT INTO product_bom (product_id, item_id, qty_per_unit) VALUES (?, ?, ?)",
                        (product_id, int(item_id), qty),
                    )
                    added += 1
                except sqlite3.IntegrityError:
                    flash(f"第 {i+1} 行原料重复或无效")
        db.commit()
        audit("production.product.update", "product", product_id, {
            "added": added, "removed": removed, "updated": updated,
        })
        flash("产品已保存")
        return redirect(url_for("production.product_edit", product_id=product_id))

    bom_rows = db.execute(
        """SELECT b.*, i.name AS item_name, i.unit AS item_unit
           FROM product_bom b JOIN items i ON i.id = b.item_id
           WHERE b.product_id = ? ORDER BY b.id""",
        (product_id,),
    ).fetchall()
    items = _load_items_for_bom()
    return render("production/product_edit.html", product=product, bom_rows=bom_rows, items=items)


@bp.route("/production/products/<int:product_id>/delete", methods=["POST"])
@require_role("manager")
def product_delete(product_id: int):
    db = get_warehouse_db()
    used = db.execute(
        "SELECT COUNT(*) AS c FROM production_runs WHERE product_id = ?",
        (product_id,),
    ).fetchone()["c"]
    if used > 0:
        flash("该产品存在生产记录，无法删除")
        return redirect(url_for("production.products_list"))
    db.execute("DELETE FROM products WHERE id = ?", (product_id,))
    db.commit()
    audit("production.product.delete", "product", product_id)
    flash("产品已删除")
    return redirect(url_for("production.products_list"))


@bp.route("/production/session", methods=["GET"])
@require_login
def session():
    db = get_warehouse_db()
    products_data = db.execute(
        "SELECT id, name, unit FROM products ORDER BY name"
    ).fetchall()
    product_id = request.args.get("product_id", type=int)
    bom = []
    chosen = None
    if product_id:
        chosen = db.execute(
            "SELECT * FROM products WHERE id = ?", (product_id,)
        ).fetchone()
        if chosen is not None:
            bom = db.execute(
                """SELECT b.id AS bom_id, b.qty_per_unit, b.item_id,
                          i.name AS item_name, i.unit, i.quantity AS stock
                   FROM product_bom b JOIN items i ON i.id = b.item_id
                   WHERE b.product_id = ? ORDER BY b.id""",
                (product_id,),
            ).fetchall()
    return render(
        "production/session.html",
        products=products_data,
        chosen=chosen,
        bom=bom,
    )


@bp.route("/production/submit", methods=["POST"])
@require_login
def submit():
    from flask import g
    db = get_warehouse_db()
    product_id = request.form.get("product_id", type=int)
    output_qty = parse_qty(request.form.get("output_qty", "0"))
    note = request.form.get("note", "").strip()

    if not product_id or output_qty <= 0:
        flash("请选择产品并填写大于 0 的产出量")
        return redirect(url_for("production.session"))

    product = db.execute(
        "SELECT id, name, unit FROM products WHERE id = ?", (product_id,)
    ).fetchone()
    if product is None:
        flash("产品不存在")
        return redirect(url_for("production.session"))

    bom_rows = db.execute(
        """SELECT b.id AS bom_id, b.item_id, b.qty_per_unit,
                  i.name AS item_name, i.quantity AS stock
           FROM product_bom b JOIN items i ON i.id = b.item_id
           WHERE b.product_id = ? ORDER BY b.id""",
        (product_id,),
    ).fetchall()
    if not bom_rows:
        flash("该产品尚未配置配方")
        return redirect(url_for("production.session", product_id=product_id))

    # Build planned & actual
    plan = []
    for b in bom_rows:
        planned = round(b["qty_per_unit"] * output_qty, 2)
        raw = request.form.get(f"actual_{b['item_id']}", "").strip()
        actual = parse_qty(raw) if raw != "" else planned
        if actual < 0:
            flash(f"原料 {b['item_name']} 实际消耗不能为负")
            return redirect(url_for("production.session", product_id=product_id))
        plan.append((int(b["item_id"]), b["item_name"], float(b["stock"]), planned, actual))

    # 硬性拦截: 库存不足
    for item_id, name, stock, planned, actual in plan:
        if actual > stock:
            flash(f"原料 {name} 库存不足（需 {actual}，现有 {stock}）")
            return redirect(url_for("production.session", product_id=product_id))

    created_by = g.user["username"] if g.get("user") else None
    cur = db.execute(
        """INSERT INTO production_runs
           (product_id, output_qty, note, rolled_back, created_by, created_at)
           VALUES (?, ?, ?, 0, ?, ?)""",
        (product_id, output_qty, note, created_by, now()),
    )
    run_id = int(cur.lastrowid)

    for item_id, name, stock, planned, actual in plan:
        db.execute(
            """INSERT INTO production_run_items
               (run_id, item_id, planned_qty, actual_qty) VALUES (?, ?, ?, ?)""",
            (run_id, item_id, planned, actual),
        )
        db.execute(
            "UPDATE items SET quantity = quantity - ?, updated_at = ? WHERE id = ?",
            (actual, now(), item_id),
        )
        db.execute(
            """INSERT INTO stock_movements (item_id, action, delta, note, created_at)
               VALUES (?, '生产消耗', ?, ?, ?)""",
            (item_id, -actual, f"生产记录#{run_id}领料", now()),
        )

    db.commit()
    audit("production.run.submit", "run", run_id, {
        "product_id": product_id, "output_qty": output_qty, "rows": len(plan),
    })
    flash("生产已记录")
    return redirect(url_for("production.runs_list"))
