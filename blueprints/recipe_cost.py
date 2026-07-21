"""Recipe cost: ice-cream recipes + serving recipes with cost/margin."""
from __future__ import annotations

import sqlite3
from typing import Optional

from flask import Blueprint, flash, redirect, render_template, request, url_for

from db import get_warehouse_db
from permissions import require_login, require_platform_admin
from blueprints._helpers import now, parse_qty
from blueprints.auth import audit


bp = Blueprint("recipe_cost", __name__)


@bp.route("/recipe-cost/")
@require_login
def landing():
    """入口：跳到冰激凌配方列表。"""
    return redirect(url_for("recipe_cost.ic_recipes_list"))


# ---------------------------------------------------------------------------
# 冰激凌配方 (ic_recipes)
# ---------------------------------------------------------------------------

def _load_items_for_bom() -> list:
    """Return items for the BOM item picker."""
    db = get_warehouse_db()
    return db.execute(
        """SELECT i.id, i.name, i.unit, i.gram_per_unit, i.aux_rate, i.aux_unit,
                  i.unit_cost, i.selling_price, c.name AS category_name
           FROM items i JOIN categories c ON c.id = i.category_id
           ORDER BY c.name, i.name"""
    ).fetchall()


@bp.route("/recipe-cost/ic-recipes", methods=["GET"])
@require_login
def ic_recipes_list():
    db = get_warehouse_db()
    from blueprints.recipe_cost_pure import ic_recipe_cost
    rows = db.execute(
        "SELECT id, name, output_unit, output_qty, sale_price, created_at "
        "FROM ic_recipes ORDER BY id DESC"
    ).fetchall()
    enriched = []
    for r in rows:
        c = ic_recipe_cost(db, int(r["id"]))
        enriched.append({
            **dict(r),
            "cost_purchase": float(c["cost_purchase"]),
            "cost_selling": float(c["cost_selling"]),
            "margin_purchase": c["margin_purchase"],
            "item_count": len(c["lines"]),
        })
    return render_template(
        "recipe_cost/ic_recipes.html",
        ic_recipes=enriched,
    )


@bp.route("/recipe-cost/ic-recipes/new", methods=["GET", "POST"])
@require_platform_admin
def ic_recipe_new():
    if request.method == "POST":
        return _save_ic_recipe(None)
    return render_template(
        "recipe_cost/ic_recipe_edit.html",
        recipe=None,
        bom_rows=[],
        items=_load_items_for_bom(),
    )


@bp.route("/recipe-cost/ic-recipes/<int:ic_recipe_id>/edit", methods=["GET", "POST"])
@require_platform_admin
def ic_recipe_edit(ic_recipe_id: int):
    if request.method == "POST":
        return _save_ic_recipe(ic_recipe_id)
    db = get_warehouse_db()
    recipe = db.execute(
        "SELECT * FROM ic_recipes WHERE id = ?", (ic_recipe_id,)
    ).fetchone()
    if recipe is None:
        flash("冰激凌配方不存在")
        return redirect(url_for("recipe_cost.ic_recipes_list"))
    bom_rows = db.execute(
        """SELECT ri.*, i.name AS item_name, i.unit AS item_unit,
                  i.gram_per_unit, i.aux_rate, i.aux_unit,
                  i.unit_cost, i.selling_price
           FROM ic_recipe_items ri JOIN items i ON i.id = ri.item_id
           WHERE ri.ic_recipe_id = ? ORDER BY ri.id""",
        (ic_recipe_id,),
    ).fetchall()
    return render_template(
        "recipe_cost/ic_recipe_edit.html",
        recipe=recipe,
        bom_rows=bom_rows,
        items=_load_items_for_bom(),
    )


@bp.route("/recipe-cost/ic-recipes/<int:ic_recipe_id>/delete", methods=["POST"])
@require_platform_admin
def ic_recipe_delete(ic_recipe_id: int):
    db = get_warehouse_db()
    used = db.execute(
        "SELECT COUNT(*) AS c FROM recipe_items "
        "WHERE source_type='ic_recipe' AND ic_recipe_id=?",
        (ic_recipe_id,),
    ).fetchone()["c"]
    if used > 0:
        flash(f"该冰激凌配方被 {used} 个出品配方引用，无法删除")
        return redirect(url_for("recipe_cost.ic_recipes_list"))
    db.execute("DELETE FROM ic_recipes WHERE id = ?", (ic_recipe_id,))
    db.commit()
    audit("recipe_cost.ic_recipe.delete", "ic_recipe", ic_recipe_id)
    flash("冰激凌配方已删除")
    return redirect(url_for("recipe_cost.ic_recipes_list"))


def _save_ic_recipe(ic_recipe_id: Optional[int]):
    """Create or update an ic_recipe + its BOM rows.

    Form fields: name / note / output_unit / output_qty / sale_price /
                 bom_row_id[] / bom_item_id[] / bom_qty[] / bom_delete[]
    """
    name = request.form.get("name", "").strip()
    note = request.form.get("note", "").strip()
    output_unit = request.form.get("output_unit", "g").strip() or "g"
    output_qty = parse_qty(request.form.get("output_qty", "100"))
    sale_price = float(request.form.get("sale_price", "0") or 0)

    if not name:
        flash("配方名称为必填")
        if ic_recipe_id:
            return redirect(url_for("recipe_cost.ic_recipe_edit", ic_recipe_id=ic_recipe_id))
        return redirect(url_for("recipe_cost.ic_recipe_new"))
    if sale_price < 0:
        flash("售价不能为负")

    db = get_warehouse_db()
    if ic_recipe_id is None:
        try:
            db.execute(
                """INSERT INTO ic_recipes
                   (name, note, output_unit, output_qty, sale_price,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (name, note, output_unit, output_qty, sale_price, now(), now()),
            )
            new_id = int(db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
            db.commit()
            audit("recipe_cost.ic_recipe.create", "ic_recipe", new_id, {"name": name})
            return redirect(url_for("recipe_cost.ic_recipe_edit", ic_recipe_id=new_id))
        except sqlite3.IntegrityError:
            flash("配方名称已存在")
            return redirect(url_for("recipe_cost.ic_recipe_new"))
    else:
        db.execute(
            """UPDATE ic_recipes SET name=?, note=?, output_unit=?,
               output_qty=?, sale_price=?, updated_at=? WHERE id=?""",
            (name, note, output_unit, output_qty, sale_price, now(), ic_recipe_id),
        )

    bom_ids = request.form.getlist("bom_row_id")
    item_ids = request.form.getlist("bom_item_id")
    qtys = request.form.getlist("bom_qty")
    deletes = request.form.getlist("bom_delete")
    added = removed = updated = 0
    for i in range(len(bom_ids)):
        row_id = bom_ids[i].strip()
        if i < len(deletes) and deletes[i] == "1":
            if row_id:
                db.execute("DELETE FROM ic_recipe_items WHERE id = ?", (int(row_id),))
                removed += 1
            continue
        item_id = item_ids[i].strip() if i < len(item_ids) else ""
        qty = parse_qty(qtys[i]) if i < len(qtys) else 0.0
        if not item_id or qty <= 0:
            continue
        if row_id:
            db.execute(
                "UPDATE ic_recipe_items SET item_id=?, qty_per_unit=? WHERE id=?",
                (int(item_id), qty, int(row_id)),
            )
            updated += 1
        else:
            try:
                db.execute(
                    """INSERT INTO ic_recipe_items
                       (ic_recipe_id, item_id, qty_per_unit) VALUES (?, ?, ?)""",
                    (ic_recipe_id, int(item_id), qty),
                )
                added += 1
            except sqlite3.IntegrityError:
                flash(f"第 {i+1} 行原料重复")

    db.commit()
    audit("recipe_cost.ic_recipe.update", "ic_recipe", ic_recipe_id, {
        "added": added, "removed": removed, "updated": updated,
    })
    flash("冰激凌配方已保存")
    return redirect(url_for("recipe_cost.ic_recipe_edit", ic_recipe_id=ic_recipe_id))


# ---------------------------------------------------------------------------
# 出品配方 (recipes) — 占位, Task 7 实现
# ---------------------------------------------------------------------------

@bp.route("/recipe-cost/recipes", methods=["GET"])
@require_login
def recipes_list():
    """占位：出品配方列表。"""
    return render_template("recipe_cost/recipes.html", recipes=[])
