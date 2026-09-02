"""Recipe cost: ice-cream recipes + serving recipes with cost/margin."""
from __future__ import annotations

import sqlite3

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from blueprints._helpers import now, parse_qty
from blueprints.auth import audit
from db import get_warehouse_db
from permissions import require_login, require_platform_admin

bp = Blueprint("recipe_cost", __name__)


@bp.before_request
def _require_rd():
    """Recipe-cost module is RD-only; storefronts don't need it (their inventory
    lives in their own DB, not in recipes). Platform admins bypass for testing."""
    from flask import abort

    from permissions import WAREHOUSE_EXEMPT
    if request.endpoint in WAREHOUSE_EXEMPT or request.endpoint is None:
        return None
    if g.user is not None and g.user["is_admin"]:
        return None
    wh = g.get("warehouse")
    if wh is None:
        return None  # require_login redirects to picker
    if wh["warehouse_type"] != "rd":
        flash("配方功能仅在研发中心可用")
        abort(403)
        return None
    return None


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


def _build_ic_card_lines(db, lines):
    """把 ic_recipe_cost 返回的纯成本行补全为卡片展示行（带品项名 + 单位）。

    pure 函数的 lines 只有 item_id / 用量 / 成本，没有可读名称；这里一次性
    JOIN items+categories 取名称与单位标签（克 / 库存单位），避免逐行查询。
    """
    meta = {}
    for r in db.execute(
        "SELECT i.id, i.name AS item_name, i.unit AS item_unit, "
        "i.gram_per_unit, c.name AS category_name "
        "FROM items i JOIN categories c ON c.id = i.category_id"
    ).fetchall():
        gpu = float(r["gram_per_unit"] or 0)
        meta[r["id"]] = {
            "name": f"{r['category_name']} / {r['item_name']}",
            "unit": "克" if gpu > 0 else (r["item_unit"] or ""),
        }
    out = []
    for ln in lines:
        m = meta.get(ln["item_id"], {"name": f"原料#{ln['item_id']}", "unit": ""})
        out.append({
            "name": m["name"],
            "qty": float(ln["qty_per_unit"]),
            "unit": m["unit"],
            "cost_purchase": float(ln["cost_purchase"]),
            "cost_selling": float(ln["cost_selling"]),
        })
    return out


def _build_recipe_card_lines(db, lines):
    """把 recipe_cost 返回的 lines（含 item / ic_recipe 两种类型）补全为卡片展示行。"""
    # 品项元数据
    item_meta = {}
    for r in db.execute(
        "SELECT i.id, i.name AS item_name, i.unit AS item_unit, "
        "i.gram_per_unit, c.name AS category_name "
        "FROM items i JOIN categories c ON c.id = i.category_id"
    ).fetchall():
        gpu = float(r["gram_per_unit"] or 0)
        item_meta[r["id"]] = {
            "name": f"{r['category_name']} / {r['item_name']}",
            "unit": "克" if gpu > 0 else (r["item_unit"] or ""),
        }
    # 冰激凌配方元数据
    ic_meta = {}
    for r in db.execute("SELECT id, name FROM ic_recipes").fetchall():
        ic_meta[r["id"]] = r["name"]
    out = []
    for ln in lines:
        if ln.get("line_type") == "ic_recipe":
            name = ic_meta.get(ln.get("ic_recipe_id"), f"冰激凌配方#{ln.get('ic_recipe_id')}")
            out.append({
                "name": name,
                "qty": float(ln.get("qty_per_unit", 0)),
                "unit": "份",
                "cost_purchase": float(ln["cost_purchase"]),
                "cost_selling": float(ln["cost_selling"]),
            })
        else:
            m = item_meta.get(ln.get("item_id"), {"name": f"原料#{ln.get('item_id')}", "unit": ""})
            out.append({
                "name": m["name"],
                "qty": float(ln.get("qty_per_unit", 0)),
                "unit": m["unit"],
                "cost_purchase": float(ln["cost_purchase"]),
                "cost_selling": float(ln["cost_selling"]),
            })
    return out


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
        sale_price = float(r["sale_price"] or 0)
        cp = float(c["cost_purchase"])
        cs = float(c["cost_selling"])
        # 三套口径（与编辑页 footer 对齐）：
        #   采购毛利 = 售价 − 采购成本；销售毛利 = 售价 − 销售小计；利润 = 销售小计 − 采购成本
        margin_purchase_amt = round(sale_price - cp, 2)
        margin_selling_amt = round(sale_price - cs, 2)
        profit = round(cs - cp, 2)
        enriched.append({
            **dict(r),
            "cost_purchase": cp,
            "cost_selling": cs,
            "margin_purchase": c["margin_purchase"],
            "margin_selling": c.get("margin_selling"),
            "margin_purchase_amt": margin_purchase_amt,
            "margin_selling_amt": margin_selling_amt,
            "margin_selling_pct": (
                round((sale_price - cs) / sale_price, 4) if sale_price > 0 else None
            ),
            "profit": profit,
            "item_count": len(c["lines"]),
            "lines": _build_ic_card_lines(db, c["lines"]),
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


def _save_ic_recipe(ic_recipe_id: int | None):
    """Create or update an ic_recipe + its BOM rows.

    Form fields: name / note / output_unit / output_qty / sale_price /
                 bom_row_id[] / bom_item_id[] / bom_qty[] / bom_delete[]

    `output_qty` is auto-computed as Σ(bom.qty_per_unit) of surviving rows
    (grams in RD world). The user-supplied value is overwritten.
    """
    name = request.form.get("name", "").strip()
    note = request.form.get("note", "").strip()
    output_unit = "g"  # hard-coded: 冰激凌配方 always grams
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
            # Insert with placeholder output_qty=0; we'll update after BOM.
            db.execute(
                """INSERT INTO ic_recipes
                   (name, note, output_unit, output_qty, sale_price,
                    created_at, updated_at)
                   VALUES (?, ?, ?, 0, ?, ?, ?)""",
                (name, note, output_unit, sale_price, now(), now()),
            )
            new_id = int(db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
            db.commit()
            audit("recipe_cost.ic_recipe.create", "ic_recipe", new_id, {"name": name})
        except sqlite3.IntegrityError:
            flash("配方名称已存在")
            return redirect(url_for("recipe_cost.ic_recipe_new"))
    else:
        # output_qty updated after BOM processing
        pass

    # Process BOM rows (works for both new + existing)
    bom_ids = request.form.getlist("bom_row_id")
    item_ids = request.form.getlist("bom_item_id")
    qtys = request.form.getlist("bom_qty")
    deletes = request.form.getlist("bom_delete")
    sp_adjs = request.form.getlist("bom_sp_adj")
    has_sp_adj = "bom_sp_adj" in request.form  # 仅当字段存在时才更新
    added = removed = updated = 0
    total_grams = 0.0
    target_id = ic_recipe_id if ic_recipe_id is not None else new_id
    item_price_updates = {}  # {int(item_id): float(sp_adj)} → 写回 items 表

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
        # 收集需要更新到品项表的销售单价
        if has_sp_adj:
            sp_adj_raw = sp_adjs[i].strip() if i < len(sp_adjs) else ""
            sp_adj = parse_qty(sp_adj_raw) if sp_adj_raw else None
            if sp_adj is not None and sp_adj > 0:
                item_price_updates[int(item_id)] = sp_adj
        if row_id:
            db.execute(
                "UPDATE ic_recipe_items SET item_id=?, qty_per_unit=? WHERE id=?",
                (int(item_id), qty, int(row_id)),
            )
            updated += 1
        else:
            try:
                db.execute(
                    "INSERT INTO ic_recipe_items (ic_recipe_id, item_id, qty_per_unit)"
                    " VALUES (?, ?, ?)",
                    (target_id, int(item_id), qty),
                )
                added += 1
            except sqlite3.IntegrityError:
                flash(f"第 {i+1} 行原料重复")
        total_grams += qty

    # 将调整后的原料销售单价持久化到品项表（items.selling_price）
    if item_price_updates:
        _ts = now()
        for iid, sp in item_price_updates.items():
            db.execute(
                "UPDATE items SET selling_price=?, selling_price_updated_at=?"
                " WHERE id=?",
                (sp, _ts, iid),
            )

    # output_qty is ALWAYS derived from the surviving BOM rows in DB
    # (grams in the RD world). This makes it non-editable-by-nature and also
    # prevents the metadata-only form submit from zeroing it out.
    _sum = db.execute(
        "SELECT COALESCE(SUM(qty_per_unit), 0) AS g "
        "FROM ic_recipe_items WHERE ic_recipe_id=?",
        (target_id,),
    ).fetchone()
    output_qty_final = round(float(_sum["g"]), 2)

    # Now update output_qty + sale_price + name/note for the recipe
    db.execute(
        """UPDATE ic_recipes SET name=?, note=?, output_unit=?,
           output_qty=?, sale_price=?, updated_at=? WHERE id=?""",
        (name, note, output_unit, output_qty_final, sale_price, now(), target_id),
    )

    db.commit()
    audit("recipe_cost.ic_recipe.update", "ic_recipe", target_id, {
        "added": added, "removed": removed, "updated": updated,
        "output_qty": output_qty_final,
    })
    flash("冰激凌配方已保存")
    return redirect(url_for("recipe_cost.ic_recipe_edit", ic_recipe_id=target_id))


# ---------------------------------------------------------------------------
# 出品配方 (recipes)
# ---------------------------------------------------------------------------

@bp.route("/recipe-cost/recipes", methods=["GET"])
@require_login
def recipes_list():
    db = get_warehouse_db()
    from blueprints.recipe_cost_pure import recipe_cost
    rows = db.execute(
        "SELECT id, name, output_unit, output_qty, sale_price, created_at "
        "FROM recipes ORDER BY id DESC"
    ).fetchall()
    enriched = []
    for r in rows:
        c = recipe_cost(db, int(r["id"]))
        sale_price = float(r["sale_price"] or 0)
        cp = float(c["cost_purchase"])
        cs = float(c["cost_selling"])
        margin_purchase_amt = round(sale_price - cp, 2)
        margin_selling_amt = round(sale_price - cs, 2)
        profit = round(cs - cp, 2)
        enriched.append({
            **dict(r),
            "cost_purchase": cp,
            "cost_selling": cs,
            "margin_purchase": c["margin_purchase"],
            "margin_selling": c.get("margin_selling"),
            "margin_purchase_amt": margin_purchase_amt,
            "margin_selling_amt": margin_selling_amt,
            "margin_selling_pct": (
                round((sale_price - cs) / sale_price, 4) if sale_price > 0 else None
            ),
            "profit": profit,
            "item_count": len(c["lines"]),
            "lines": _build_recipe_card_lines(db, c["lines"]),
        })
    return render_template("recipe_cost/recipes.html", recipes=enriched)


def _load_ic_recipes_for_picker() -> list:
    db = get_warehouse_db()
    from blueprints.recipe_cost_pure import ic_recipe_cost
    rows = db.execute(
        "SELECT id, name, sale_price, output_unit, output_qty FROM ic_recipes ORDER BY name"
    ).fetchall()
    enriched = []
    for r in rows:
        c = ic_recipe_cost(db, int(r["id"]))
        out_qty = float(r["output_qty"] or 1)
        enriched.append({
            **dict(r),
            "cost_purchase_per_unit": float(c["cost_purchase"]) / out_qty if out_qty > 0 else 0.0,
            "cost_selling_per_unit": float(c["cost_selling"]) / out_qty if out_qty > 0 else 0.0,
        })
    return enriched


@bp.route("/recipe-cost/recipes/new", methods=["GET", "POST"])
@require_platform_admin
def recipe_new():
    if request.method == "POST":
        return _save_recipe(None)
    return render_template(
        "recipe_cost/recipe_edit.html",
        recipe=None,
        bom_rows=[],
        items=_load_items_for_bom(),
        ic_recipes=_load_ic_recipes_for_picker(),
    )


@bp.route("/recipe-cost/recipes/<int:recipe_id>/edit", methods=["GET", "POST"])
@require_platform_admin
def recipe_edit(recipe_id: int):
    if request.method == "POST":
        return _save_recipe(recipe_id)
    db = get_warehouse_db()
    recipe = db.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    if recipe is None:
        flash("出品配方不存在")
        return redirect(url_for("recipe_cost.recipes_list"))
    bom_rows = db.execute(
        """SELECT ri.*, i.name AS item_name, i.unit AS item_unit,
                  i.gram_per_unit, i.aux_rate, i.aux_unit,
                  i.unit_cost, i.selling_price,
                  ic.name AS ic_recipe_name
           FROM recipe_items ri
           LEFT JOIN items i ON i.id = ri.item_id
           LEFT JOIN ic_recipes ic ON ic.id = ri.ic_recipe_id
           WHERE ri.recipe_id = ? ORDER BY ri.id""",
        (recipe_id,),
    ).fetchall()
    return render_template(
        "recipe_cost/recipe_edit.html",
        recipe=recipe,
        bom_rows=bom_rows,
        items=_load_items_for_bom(),
        ic_recipes=_load_ic_recipes_for_picker(),
    )


@bp.route("/recipe-cost/recipes/<int:recipe_id>/delete", methods=["POST"])
@require_platform_admin
def recipe_delete(recipe_id: int):
    db = get_warehouse_db()
    db.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
    db.commit()
    audit("recipe_cost.recipe.delete", "recipe", recipe_id)
    flash("出品配方已删除")
    return redirect(url_for("recipe_cost.recipes_list"))


def _save_recipe(recipe_id):
    name = request.form.get("name", "").strip()
    note = request.form.get("note", "").strip()
    output_unit = "g"  # RD world: output always grams
    sale_price = float(request.form.get("sale_price", "0") or 0)

    if not name:
        flash("配方名称为必填")
        if recipe_id:
            return redirect(url_for("recipe_cost.recipe_edit", recipe_id=recipe_id))
        return redirect(url_for("recipe_cost.recipe_new"))
    if sale_price < 0:
        flash("售价不能为负")

    db = get_warehouse_db()
    if recipe_id is None:
        try:
            db.execute(
                """INSERT INTO recipes
                   (name, note, output_unit, output_qty, sale_price,
                    sale_price_updated_at, created_at, updated_at)
                   VALUES (?, ?, ?, 0, ?, ?, ?, ?)""",
                (name, note, output_unit, sale_price, now(), now(), now()),
            )
            new_id = int(db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
            db.commit()
            audit("recipe_cost.recipe.create", "recipe", new_id, {"name": name})
        except sqlite3.IntegrityError:
            flash("配方名称已存在")
            return redirect(url_for("recipe_cost.recipe_new"))
    else:
        old = db.execute(
            "SELECT sale_price FROM recipes WHERE id=?", (recipe_id,)
        ).fetchone()
        old_sp = float(old["sale_price"] or 0) if old else 0
        sp_updated_at = now() if sale_price != old_sp else None
        db.execute(
            """UPDATE recipes SET name=?, note=?, output_unit=?,
               sale_price=?, sale_price_updated_at=?,
               updated_at=? WHERE id=?""",
            (name, note, output_unit, sale_price, sp_updated_at,
             now(), recipe_id),
        )

    # BOM rows (多态)
    bom_ids = request.form.getlist("bom_row_id")
    source_types = request.form.getlist("bom_source_type")
    item_ids = request.form.getlist("bom_item_id")
    ic_ids = request.form.getlist("bom_ic_recipe_id")
    qtys = request.form.getlist("bom_qty")
    deletes = request.form.getlist("bom_delete")
    added = removed = updated = 0
    total_grams = 0.0
    target_id = recipe_id if recipe_id is not None else new_id

    for i in range(len(bom_ids)):
        row_id = bom_ids[i].strip()
        if i < len(deletes) and deletes[i] == "1":
            if row_id:
                db.execute("DELETE FROM recipe_items WHERE id = ?", (int(row_id),))
                removed += 1
            continue
        st = source_types[i].strip() if i < len(source_types) else "item"
        qty = parse_qty(qtys[i]) if i < len(qtys) else 0.0
        if qty <= 0:
            continue
        item_id = item_ids[i].strip() if i < len(item_ids) else ""
        ic_id = ic_ids[i].strip() if i < len(ic_ids) else ""
        if st == "item":
            if not item_id:
                continue
            if row_id:
                db.execute(
                    """UPDATE recipe_items SET source_type='item',
                       item_id=?, ic_recipe_id=NULL, qty_per_unit=? WHERE id=?""",
                    (int(item_id), qty, int(row_id)),
                )
                updated += 1
            else:
                db.execute(
                    """INSERT INTO recipe_items
                       (recipe_id, source_type, item_id, ic_recipe_id, qty_per_unit)
                       VALUES (?, 'item', ?, NULL, ?)""",
                    (target_id, int(item_id), qty),
                )
                added += 1
            # For items: each qty is in grams (input form is grams per serving).
            total_grams += qty
        elif st == "ic_recipe":
            if not ic_id:
                continue
            if row_id:
                db.execute(
                    """UPDATE recipe_items SET source_type='ic_recipe',
                       item_id=NULL, ic_recipe_id=?, qty_per_unit=? WHERE id=?""",
                    (int(ic_id), qty, int(row_id)),
                )
                updated += 1
            else:
                db.execute(
                    """INSERT INTO recipe_items
                       (recipe_id, source_type, item_id, ic_recipe_id, qty_per_unit)
                       VALUES (?, 'ic_recipe', NULL, ?, ?)""",
                    (target_id, int(ic_id), qty),
                )
                added += 1
            # For ic_recipe rows: qty is "servings" of that ic recipe,
            # and each ic recipe is output_qty grams. So total_grams = qty * ic.output_qty.
            ic_row = db.execute(
                "SELECT output_qty FROM ic_recipes WHERE id=?", (int(ic_id),)
            ).fetchone()
            if ic_row:
                ic_grams = float(ic_row["output_qty"] or 0)
                total_grams += qty * ic_grams

    # Update output_qty (auto = total grams)
    db.execute(
        """UPDATE recipes SET output_qty=?, updated_at=? WHERE id=?""",
        (round(total_grams, 2), now(), target_id),
    )

    db.commit()
    audit("recipe_cost.recipe.update", "recipe", target_id, {
        "added": added, "removed": removed, "updated": updated,
        "output_qty": round(total_grams, 2),
    })
    flash("出品配方已保存")
    return redirect(url_for("recipe_cost.recipe_edit", recipe_id=target_id))


@bp.route("/recipe-cost/items/<int:item_id>/update-selling-price", methods=["POST"])
@require_platform_admin
def update_selling_price(item_id: int):
    """滑块"保存为新价"按钮：POST 写回 items.selling_price。"""
    db = get_warehouse_db()
    new_sp_raw = request.form.get("selling_price", "0") or "0"
    try:
        new_sp = float(new_sp_raw)
    except ValueError:
        flash("销售单价格式错误")
        return redirect(request.referrer or url_for("core.land"))
    if new_sp < 0:
        flash("销售单价不能为负")
        return redirect(request.referrer or url_for("core.land"))

    old_row = db.execute(
        "SELECT selling_price FROM items WHERE id=?", (item_id,)
    ).fetchone()
    if old_row is None:
        flash("品项不存在")
        return redirect(request.referrer or url_for("core.land"))
    old_sp = float(old_row["selling_price"] or 0)

    db.execute(
        "UPDATE items SET selling_price=?, selling_price_updated_at=?, "
        "updated_at=? WHERE id=?",
        (new_sp, now(), now(), item_id),
    )
    db.commit()
    audit("recipe_cost.items.update_selling_price", "item", item_id, {
        "old": old_sp, "new": new_sp,
    })
    flash(f"已保存新售价 ¥{new_sp:.2f}")
    return redirect(request.referrer or url_for("core.land"))


@bp.route("/recipe-cost/api/cost/<kind>/<int:rid>", methods=["GET"])
@require_login
def api_cost(kind: str, rid: int):
    """只读 JSON：当前 ic_recipe / recipe 的成本与毛利率（前端 sanity check 用）。"""
    db = get_warehouse_db()
    from blueprints.recipe_cost_pure import ic_recipe_cost, recipe_cost
    if kind == "ic_recipe":
        c = ic_recipe_cost(db, rid)
    elif kind == "recipe":
        c = recipe_cost(db, rid)
    else:
        return {"error": "unknown_kind"}, 400
    if c is None:
        return {"error": "not_found"}, 404
    return {
        "cost_purchase": float(c["cost_purchase"]),
        "cost_selling": float(c["cost_selling"]),
        "sale_price": float(c["sale_price"]),
        "margin_purchase": c["margin_purchase"],
        "margin_selling": c.get("margin_selling"),
        "profit": float(c.get("profit", 0.0)),
    }
