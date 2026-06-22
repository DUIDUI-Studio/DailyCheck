"""Production: products, BOM, runs, rollback, delete, CSV export."""
from __future__ import annotations

import csv
import io
import sqlite3
from datetime import datetime
from decimal import Decimal

from flask import Blueprint, flash, g, redirect, request, url_for

from db import get_warehouse_db
from permissions import require_login, require_platform_admin, require_role
from ._helpers import now, parse_qty, render, grams_to_stock
from .auth import audit


bp = Blueprint("production", __name__)


@bp.before_request
def _set_no_sidebar():
    """Production module has its own sidebar-free layout — users navigate
    between 产品/录入/历史 via the top tabs and return to /land for the
    库存管理/生产录入 choice. The view must still pass no_sidebar=True
    in its render() call (Flask's g is not visible to Jinja by default).
    This hook sets g for any future context_processor that wants it."""
    from flask import g
    g.no_sidebar = True


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
    return render("production/products.html", products=products_data, no_sidebar=True)


def _load_items_for_bom():
    """Return items for the BOM item picker: id, name, unit, gram_per_unit, category_name."""
    db = get_warehouse_db()
    return db.execute(
        """SELECT i.id, i.name, i.unit, i.gram_per_unit, c.name AS category_name
           FROM items i JOIN categories c ON c.id = i.category_id
           ORDER BY c.name, i.name"""
    ).fetchall()


@bp.route("/production/products/new", methods=["GET", "POST"])
@require_platform_admin
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
    return render("production/product_edit.html", product=None, bom_rows=[], items=_load_items_for_bom(), no_sidebar=True)


@bp.route("/production/products/<int:product_id>/edit", methods=["GET", "POST"])
@require_platform_admin
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
    return render("production/product_edit.html", product=product, bom_rows=bom_rows, items=items, no_sidebar=True)


@bp.route("/production/products/<int:product_id>/delete", methods=["POST"])
@require_platform_admin
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
                          i.name AS item_name, i.unit, i.quantity AS stock,
                          i.gram_per_unit
                   FROM product_bom b JOIN items i ON i.id = b.item_id
                   WHERE b.product_id = ? ORDER BY b.id""",
                (product_id,),
            ).fetchall()
    return render(
        "production/session.html",
        products=products_data,
        chosen=chosen,
        bom=bom,
        # 禁用产品下拉: 卡片点入 = 锁定产品;session.html 收到 chosen 后只展示该产品的配方。
        lock_product=bool(chosen),
        no_sidebar=True,
    )


@bp.route("/production/submit", methods=["POST"])
@require_login
def submit():
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
                  i.name AS item_name, i.quantity AS stock, i.gram_per_unit
           FROM product_bom b JOIN items i ON i.id = b.item_id
           WHERE b.product_id = ? ORDER BY b.id""",
        (product_id,),
    ).fetchall()
    if not bom_rows:
        flash("该产品尚未配置配方")
        return redirect(url_for("production.session", product_id=product_id))

    # Build planned & actual. Use Decimal for qty_per_unit × output_qty
    # to avoid the 0.1 + 0.2 float trap the parse_qty helper exists to
    # prevent — see _helpers.py:50.
    plan = []
    for b in bom_rows:
        gpu = float(b["gram_per_unit"] or 0)
        # qty_per_unit: 启用克时是克/件，否则是库存单位/件。
        # 先乘产出量得本批配方量，再一次性折算成库存单位（避免逐件累积误差）。
        batch_recipe_qty = float(
            (Decimal(str(b["qty_per_unit"])) * Decimal(str(output_qty)))
            .quantize(Decimal("0.01"))
        )
        planned = grams_to_stock(batch_recipe_qty, gpu)  # 库存单位
        raw = request.form.get(f"actual_{b['item_id']}", "").strip()
        actual = parse_qty(raw) if raw != "" else planned  # 表单已传库存单位
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
        # 双写 outbound_requests 让 /outbound 出库记录页面也能看到生产领料
        # reason 标识生产 run id 以便回退时定位
        db.execute(
            """INSERT INTO outbound_requests
               (item_id, requested_quantity, reason, status, rolled_back, created_at)
               VALUES (?, ?, ?, '出库', 0, ?)""",
            (item_id, actual, f"生产领料(run=#{run_id})", now()),
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
    return redirect(url_for("production.products_list"))


@bp.route("/production/runs", methods=["GET"])
@require_login
def runs_list():
    db = get_warehouse_db()
    # 时间筛选:
    #   scope=today  (default) - 当天 created_at LIKE 'YYYY-MM-DD%'
    #   scope=7d               - 最近 7 天
    #   scope=day&date=YYYY-MM-DD - 自定义单日
    scope = request.args.get("scope", "today")
    custom_day = request.args.get("date", "").strip()
    where = ""
    params: list = []
    if scope == "7d":
        where = "WHERE r.created_at >= datetime('now', '-7 days')"
    elif scope == "day" and custom_day:
        where = "WHERE r.created_at LIKE ? || '%'"
        params = [custom_day]
    else:  # default 'today'
        where = "WHERE r.created_at LIKE ? || '%'"
        params = [datetime.now().strftime("%Y-%m-%d")]
    flat = db.execute(
        f"""SELECT r.id AS run_id, r.output_qty, r.note AS run_note,
                  r.rolled_back, r.created_at, r.created_by,
                  p.name AS product_name, p.unit AS product_unit,
                  pri.id AS pri_id, pri.planned_qty, pri.actual_qty,
                  i.name AS item_name, i.unit AS item_unit
           FROM production_runs r
           JOIN products p ON p.id = r.product_id
           LEFT JOIN production_run_items pri ON pri.run_id = r.id
           LEFT JOIN items i ON i.id = pri.item_id
           {where}
           ORDER BY r.id DESC, pri.id LIMIT 200""",
        params,
    ).fetchall()
    # Group items under their parent run
    runs_by_id: dict = {}
    for row in flat:
        rid = row["run_id"]
        if rid not in runs_by_id:
            runs_by_id[rid] = {
                "id": rid,
                "output_qty": row["output_qty"],
                "note": row["run_note"],
                "rolled_back": row["rolled_back"],
                "created_at": row["created_at"],
                "created_by": row["created_by"],
                "product_name": row["product_name"],
                "product_unit": row["product_unit"],
                "items": [],
            }
        if row["pri_id"] is not None:
            runs_by_id[rid]["items"].append({
                "id": row["pri_id"],
                "planned_qty": row["planned_qty"],
                "actual_qty": row["actual_qty"],
                "item_name": row["item_name"],
                "unit": row["item_unit"],
            })
    runs = list(runs_by_id.values())
    return render(
        "production/runs.html",
        runs=runs,
        scope=scope,
        custom_day=custom_day,
        no_sidebar=True,
    )


@bp.route("/production/runs/<int:run_id>/rollback", methods=["POST"])
@require_role("staff")
def rollback(run_id: int):
    db = get_warehouse_db()
    run = db.execute(
        "SELECT id, rolled_back FROM production_runs WHERE id = ?", (run_id,),
    ).fetchone()
    if run is None:
        flash("生产记录不存在")
        return redirect(url_for("production.runs_list"))
    if int(run["rolled_back"]) == 1:
        flash("该记录已回退")
        return redirect(url_for("production.runs_list"))

    items = db.execute(
        "SELECT item_id, actual_qty FROM production_run_items WHERE run_id = ?",
        (run_id,),
    ).fetchall()
    qty_total = 0.0
    for it in items:
        qty = parse_qty(it["actual_qty"])
        qty_total += qty
        # 标记该 run 对应的 outbound_requests 为已回退 — 库存和汇总口径同步
        db.execute(
            "UPDATE outbound_requests SET rolled_back = 1 WHERE reason = ? AND rolled_back = 0",
            (f"生产领料(run=#{run_id})",),
        )
        db.execute(
            "UPDATE items SET quantity = quantity + ?, updated_at = ? WHERE id = ?",
            (qty, now(), int(it["item_id"])),
        )
        db.execute(
            """INSERT INTO stock_movements (item_id, action, delta, note, created_at)
               VALUES (?, '生产消耗回退', ?, ?, ?)""",
            (int(it["item_id"]), qty, f"回退生产记录#{run_id}", now()),
        )
    db.execute("UPDATE production_runs SET rolled_back = 1 WHERE id = ?", (run_id,))
    db.commit()
    audit("production.run.rollback", "run", run_id, {"qty_total": qty_total})
    flash("生产记录已回退")
    return redirect(url_for("production.runs_list"))


@bp.route("/production/runs/<int:run_id>/delete", methods=["POST"])
@require_role("staff")
def delete_run(run_id: int):
    db = get_warehouse_db()
    run = db.execute(
        "SELECT id, rolled_back FROM production_runs WHERE id = ?", (run_id,),
    ).fetchone()
    if run is None:
        flash("生产记录不存在")
        return redirect(url_for("production.runs_list"))

    qty_total = 0.0
    if int(run["rolled_back"]) == 0:
        items = db.execute(
            "SELECT item_id, actual_qty FROM production_run_items WHERE run_id = ?",
            (run_id,),
        ).fetchall()
        for it in items:
            qty = parse_qty(it["actual_qty"])
            qty_total += qty
            db.execute(
                "UPDATE items SET quantity = quantity + ?, updated_at = ? WHERE id = ?",
                (qty, now(), int(it["item_id"])),
            )
            db.execute(
                """INSERT INTO stock_movements (item_id, action, delta, note, created_at)
                   VALUES (?, '生产消耗回退', ?, ?, ?)""",
                (int(it["item_id"]), qty, f"删除生产记录#{run_id}回滚", now()),
            )
        # 直接删除 run 对应的 outbound_requests（语义：删除生产记录 ≡ 删除出库记录）
        db.execute(
            "DELETE FROM outbound_requests WHERE reason = ?",
            (f"生产领料(run=#{run_id})",),
        )
    else:
        # 已回退的 outbound_requests 已 rolled_back=1，删除 run 时把它们也清掉
        db.execute(
            "DELETE FROM outbound_requests WHERE reason = ?",
            (f"生产领料(run=#{run_id})",),
        )

    db.execute("DELETE FROM production_runs WHERE id = ?", (run_id,))
    db.commit()
    audit("production.run.delete", "run", run_id, {
        "rolled_back": int(run["rolled_back"]),
        "qty_total": qty_total,
    })
    flash("生产记录已删除" + ("，库存已归还" if int(run["rolled_back"]) == 0 else ""))
    return redirect(url_for("production.runs_list"))


@bp.route("/production/runs.csv", methods=["GET"])
@require_login
def runs_csv():
    """CSV download: one row per production_run_item.

    口径:包含 rolled_back=1 的行(审计需要),文件加 UTF-8 BOM 以兼容 Excel CN。
    粒度比 /export/consumption 更细:这里每行是一条生产批次的某一种原料消耗,
    在 Excel 里按 run_id 透视即可还原完整批次。
    """
    from flask import current_app  # match reports.py local-import style
    db = get_warehouse_db()
    rows = db.execute(
        """SELECT pr.id AS run_id, pr.created_at, pr.created_by,
                  pr.output_qty, pr.note AS run_note, pr.rolled_back,
                  p.name AS product_name, p.unit AS product_unit,
                  pri.planned_qty, pri.actual_qty,
                  i.sku AS item_sku, i.name AS item_name, i.unit AS item_unit
           FROM production_runs pr
           JOIN products p ON p.id = pr.product_id
           JOIN production_run_items pri ON pri.run_id = pr.id
           JOIN items i ON i.id = pri.item_id
           ORDER BY pr.id DESC, pri.id"""
    ).fetchall()
    audit("production.runs_export", "report", None, {"rows": len(rows)})

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "run_id", "created_at", "created_by", "product_name", "output_qty",
        "output_unit", "item_sku", "item_name", "planned_qty", "actual_qty",
        "item_unit", "rolled_back", "run_note",
    ])
    for r in rows:
        writer.writerow([
            r["run_id"], r["created_at"], r["created_by"] or "",
            r["product_name"], r["output_qty"], r["product_unit"],
            r["item_sku"], r["item_name"], r["planned_qty"], r["actual_qty"],
            r["item_unit"], r["rolled_back"], r["run_note"] or "",
        ])
    today = datetime.now().strftime("%Y%m%d")
    response = current_app.response_class(
        output.getvalue().encode("utf-8-sig"),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename=production_runs_{today}.csv"},
    )
    return response
