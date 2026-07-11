"""Items CRUD and inventory read view."""
from __future__ import annotations

import sqlite3

from flask import Blueprint, flash, redirect, render_template, request, url_for

from db import get_warehouse_db
from permissions import require_login, require_platform_admin, require_role
from ._helpers import warehouse_categories_in_clause, fmt_qty, gen_sku, now, parse_qty
from .auth import audit


bp = Blueprint("items", __name__)


@bp.route("/items", methods=["GET", "POST"])
@require_platform_admin
@require_role("manager")
def items_list():
    db = get_warehouse_db()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        category_id = request.form.get("category_id", "").strip()
        quantity = parse_qty(request.form.get("quantity", "0"))
        safety_stock = parse_qty(request.form.get("safety_stock", "0"))
        unit_cost = float(request.form.get("unit_cost", "0") or 0)
        unit = request.form.get("unit", "件").strip() or "件"
        gram_per_unit = parse_qty(request.form.get("gram_per_unit", "0"))

        if gram_per_unit < 0:
            flash("每单位克重不能为负数")
            return redirect(url_for("items.items_list"))
        if unit_cost < 0:
            flash("进货单价不能为负数")
            return redirect(url_for("items.items_list"))

        if not name or not category_id:
            flash("名称、品类为必填")
            return redirect(url_for("items.items_list"))

        try:
            db.execute(
                """INSERT INTO items
                   (sku, name, category_id, quantity, safety_stock, unit_cost, unit, gram_per_unit, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (gen_sku(), name, int(category_id), quantity, safety_stock, unit_cost, unit, gram_per_unit, now()),
            )
            new_id = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            db.commit()
            audit("items.create", "item", new_id, {"name": name})
            flash("库存品创建成功")
        except sqlite3.IntegrityError:
            flash("库存品创建失败，请重试")
        return redirect(url_for("items.items_list"))

    placeholders, params = warehouse_categories_in_clause()
    categories_data = db.execute(
        f"SELECT id, name, description FROM categories WHERE name IN ({placeholders}) ORDER BY name",
        params,
    ).fetchall()
    rows = db.execute(
        f"""SELECT i.*, c.name AS category_name
            FROM items i JOIN categories c ON c.id = i.category_id
            WHERE c.name IN ({placeholders})
            ORDER BY i.id DESC""",
        params,
    ).fetchall()
    return render_template(
        "items.html",
        items=rows,
        categories=categories_data,
    )


@bp.route("/items/<int:item_id>/edit", methods=["GET", "POST"])
@require_platform_admin
@require_role("manager")
def edit_item(item_id: int):
    db = get_warehouse_db()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        category_id = request.form.get("category_id", "").strip()
        safety_stock = parse_qty(request.form.get("safety_stock", "0"))
        unit_cost = float(request.form.get("unit_cost", "0") or 0)
        unit = request.form.get("unit", "件").strip() or "件"
        gram_per_unit = parse_qty(request.form.get("gram_per_unit", "0"))
        if gram_per_unit < 0:
            flash("每单位克重不能为负数")
            return redirect(url_for("items.edit_item", item_id=item_id))
        if unit_cost < 0:
            flash("进货单价不能为负数")
            return redirect(url_for("items.edit_item", item_id=item_id))
        if not name or not category_id:
            flash("名称、品类为必填")
            return redirect(url_for("items.edit_item", item_id=item_id))
        db.execute(
            """UPDATE items SET name=?, category_id=?, safety_stock=?,
               unit_cost=?, unit=?, gram_per_unit=?, updated_at=? WHERE id=?""",
            (name, int(category_id), safety_stock, unit_cost, unit, gram_per_unit, now(), item_id),
        )
        db.commit()
        audit("items.update", "item", item_id, {"name": name})
        flash("已更新")
        return redirect(url_for("items.items_list"))

    placeholders, params = warehouse_categories_in_clause()
    categories_data = db.execute(
        f"SELECT id, name FROM categories WHERE name IN ({placeholders}) ORDER BY name",
        params,
    ).fetchall()
    item = db.execute(
        "SELECT * FROM items WHERE id=?", (item_id,)
    ).fetchone()
    return render_template("edit_item.html", item=item, categories=categories_data)


@bp.route("/items/<int:item_id>/delete", methods=["POST"])
@require_role("staff")
def delete_item(item_id: int):
    db = get_warehouse_db()
    usage = db.execute(
        """SELECT
              (SELECT COUNT(*) FROM stock_movements WHERE item_id=?) +
              (SELECT COUNT(*) FROM restock_requests WHERE item_id=?) +
              (SELECT COUNT(*) FROM outbound_requests WHERE item_id=?) +
              (SELECT COUNT(*) FROM stocktakes WHERE item_id=?) AS c""",
        (item_id, item_id, item_id, item_id),
    ).fetchone()["c"]
    if usage > 0:
        flash("该品项存在关联业务记录，无法删除")
        return redirect(url_for("items.items_list"))
    db.execute("DELETE FROM items WHERE id=?", (item_id,))
    db.commit()
    audit("items.delete", "item", item_id)
    flash("已删除")
    return redirect(url_for("items.items_list"))


@bp.route("/inventory")
@require_login
def inventory_view():
    db = get_warehouse_db()
    placeholders, params = warehouse_categories_in_clause()
    q = request.args.get("q", "").strip()
    cat = request.args.get("cat", "").strip()
    # 7-day consumption per item.
    # 口径:业务表 + 7 日窗口,rolled_back=0 表示未被回退/删除。
    # 数据源:
    #   - outbound_requests (rolled_back=0, 排除生产领料 reason)
    #   - production_run_items JOIN production_runs (rolled_back=0)
    # 与 /summary 同源。被删除的出库记录 (outbound.delete 会 DELETE 该行)
    # 和被回退的批次 (production.run.rollback 标记 rolled_back=1)
    # 都自动从统计中消失。
    rows = db.execute(
        f"""SELECT i.*, c.name AS category_name,
                  COALESCE(c7.qty, 0) AS consume_7d_qty,
                  COALESCE(c7.value, 0) AS consume_7d_value,
                  COALESCE(c7.days, 0) AS consume_7d_days
           FROM items i
           JOIN categories c ON c.id = i.category_id
           LEFT JOIN (
               SELECT item_id,
                      SUM(qty) AS qty,
                      ROUND(SUM(qty * unit_cost), 2) AS value,
                      COUNT(DISTINCT substr(created_at, 1, 10)) AS days
               FROM (
                   -- 出库 (业务表,删除即消失)
                   SELECT o.item_id, o.requested_quantity AS qty,
                          i2.unit_cost, o.created_at
                   FROM outbound_requests o
                   JOIN items i2 ON i2.id = o.item_id
                   WHERE o.rolled_back = 0
                     AND (o.reason IS NULL OR o.reason NOT LIKE '生产领料(run=#%')
                     AND o.created_at >= datetime('now', '-7 days')
                   UNION ALL
                   -- 生产消耗 (业务表,run.rolled_back=0 排除回退)
                   SELECT pri.item_id, pri.actual_qty AS qty,
                          i2.unit_cost, pr.created_at
                   FROM production_run_items pri
                   JOIN production_runs pr ON pr.id = pri.run_id
                   JOIN items i2 ON i2.id = pri.item_id
                   WHERE pr.rolled_back = 0
                     AND pr.created_at >= datetime('now', '-7 days')
               )
               GROUP BY item_id
           ) c7 ON c7.item_id = i.id
           WHERE c.name IN ({placeholders})
             AND (? = '' OR i.name LIKE '%' || ? || '%' OR i.sku LIKE '%' || ? || '%')
             AND (? = '' OR c.name = ?)
           ORDER BY (i.quantity <= i.safety_stock) DESC, i.name""",
        params + [q, q, q, cat, cat],
    ).fetchall()
    return render_template("inventory.html", items=rows, q=q, cat=cat)
