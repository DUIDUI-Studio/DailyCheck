"""Recipe cost: ice-cream recipes + serving recipes with cost/margin."""
from __future__ import annotations

import csv
import io
import sqlite3
from datetime import datetime
from pathlib import Path

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for

from blueprints._helpers import now, parse_qty
from blueprints.auth import audit
from db import get_warehouse_db
from permissions import require_login, require_platform_admin

bp = Blueprint("recipe_cost", __name__)


# ---------------------------------------------------------------------------
# CSV export helpers
# ---------------------------------------------------------------------------

def _csv_response(rows: list[dict], filename: str) -> Response:
    """Build a CSV response with a UTF-8 BOM so Excel opens Chinese correctly.

    `rows` is an iterable of dicts; column order = first row's key order.
    Use `csv.DictWriter` so missing keys yield empty cells (vs KeyError).
    """
    if not rows:
        # Header-only file when there's no data — still gives a valid download.
        body = ""
        keys: list[str] = []
    else:
        keys = list(rows[0].keys())
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        body = buf.getvalue()

    # BOM + content. Filename wrapped in quotes for clients that don't
    # RFC 5987 encode (Excel on Windows reads the plain filename).
    return Response(
        "\ufeff" + body,
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"; '
                                   f"filename*=UTF-8''{filename}",
            "Cache-Control": "no-store",
        },
    )


def _stamp() -> str:
    """Filename-safe timestamp, e.g. '20260903-001530'."""
    return datetime.now().strftime("%Y%m%d-%H%M%S")


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
    _upsert_recipe_draft("ic_recipe", target_id, user_id=g.user["id"] if g.user else None)
    flash("冰激凌配方已保存")
    return redirect(url_for("recipe_cost.ic_recipe_edit", ic_recipe_id=target_id))


def _upsert_recipe_draft(recipe_type: str, recipe_id: int, user_id: int | None) -> int:
    """Snapshot the recipe in its current warehouse db and upsert a draft
    version row in master.db. Returns version_id. No-op on failure (so
    save flow isn't blocked by cross-db issues).
    """
    from contextlib import closing
    from config import MASTER_DB
    import sqlite3 as _sq
    import json as _json
    try:
        from blueprints.publish_recipe_pure import (
            snapshot_recipe, upsert_draft_version,
        )
        wh_conn = get_warehouse_db()
        snap = snapshot_recipe(wh_conn, recipe_type, recipe_id)
        if snap is None:
            return None
        wh_code = g.warehouse["code"] if g.get("warehouse") else "rd_001"
        with closing(_sq.connect(MASTER_DB)) as master_conn:
            master_conn.execute("PRAGMA foreign_keys = ON")
            version_id = upsert_draft_version(
                master_conn, recipe_type, recipe_id, wh_code, snap,
                user_id=user_id,
            )
            master_conn.commit()
        return version_id
    except Exception as exc:  # noqa: BLE001
        audit("recipe_cost.draft.upsert_failed", recipe_type, recipe_id,
              {"error": str(exc)})
        return None


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
    _upsert_recipe_draft("recipe", target_id, user_id=g.user["id"] if g.user else None)
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


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

@bp.route("/recipe-cost/ic-recipes/export.csv", methods=["GET"])
@require_login
def ic_recipes_export_csv():
    """Flat list of every ic_recipe with computed cost / margin columns."""
    db = get_warehouse_db()
    from blueprints.recipe_cost_pure import ic_recipe_cost
    rows = db.execute(
        "SELECT id, name, output_unit, output_qty, sale_price, "
        "       created_at, updated_at "
        "FROM ic_recipes ORDER BY id"
    ).fetchall()
    out = []
    for r in rows:
        c = ic_recipe_cost(db, int(r["id"]))
        cp = float(c["cost_purchase"])
        cs = float(c["cost_selling"])
        sp = float(r["sale_price"] or 0)
        out.append({
            "id": r["id"],
            "name": r["name"],
            "output_qty": float(r["output_qty"] or 0),
            "output_unit": r["output_unit"],
            "sale_price": sp,
            "cost_purchase": cp,
            "cost_selling": cs,
            "margin_purchase_pct": (
                round((sp - cp) / sp * 100, 2) if sp > 0 else None
            ),
            "margin_selling_pct": (
                round((cs - cp) / cs * 100, 2) if cs > 0 else None
            ),
            "profit": round(cs - cp, 2),
            "item_count": len(c["lines"]),
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        })
    return _csv_response(out, f"ic_recipes_{_stamp()}.csv")


@bp.route("/recipe-cost/recipes/export.csv", methods=["GET"])
@require_login
def recipes_export_csv():
    """Flat list of every serving recipe (出品配方) with computed columns."""
    db = get_warehouse_db()
    from blueprints.recipe_cost_pure import recipe_cost
    rows = db.execute(
        "SELECT id, name, output_unit, output_qty, sale_price, "
        "       created_at, updated_at "
        "FROM recipes ORDER BY id"
    ).fetchall()
    out = []
    for r in rows:
        c = recipe_cost(db, int(r["id"]))
        cp = float(c["cost_purchase"])
        cs = float(c["cost_selling"])
        sp = float(r["sale_price"] or 0)
        out.append({
            "id": r["id"],
            "name": r["name"],
            "output_qty": float(r["output_qty"] or 0),
            "output_unit": r["output_unit"],
            "sale_price": sp,
            "cost_purchase": cp,
            "cost_selling": cs,
            "margin_purchase_pct": (
                round((sp - cp) / sp * 100, 2) if sp > 0 else None
            ),
            "margin_selling_pct": (
                round((cs - cp) / cs * 100, 2) if cs > 0 else None
            ),
            "profit": round(cs - cp, 2),
            "item_count": len(c["lines"]),
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        })
    return _csv_response(out, f"recipes_{_stamp()}.csv")


@bp.route("/recipe-cost/ic-recipes/<int:ic_recipe_id>/export.csv", methods=["GET"])
@require_login
def ic_recipe_bom_export_csv(ic_recipe_id: int):
    """BOM rows (one per ingredient line) for a single ic_recipe."""
    db = get_warehouse_db()
    recipe = db.execute(
        "SELECT id, name, output_unit, output_qty, sale_price "
        "FROM ic_recipes WHERE id=?", (ic_recipe_id,),
    ).fetchone()
    if recipe is None:
        flash("冰激凌配方不存在")
        return redirect(url_for("recipe_cost.ic_recipes_list"))
    rows = db.execute(
        """SELECT ri.item_id, ri.qty_per_unit,
                  i.name AS item_name, c.name AS category_name,
                  i.unit, i.gram_per_unit, i.unit_cost, i.selling_price
           FROM ic_recipe_items ri
           JOIN items i ON i.id = ri.item_id
           JOIN categories c ON c.id = i.category_id
           WHERE ri.ic_recipe_id = ?
           ORDER BY ri.id""",
        (ic_recipe_id,),
    ).fetchall()
    out = []
    for r in rows:
        gpu = float(r["gram_per_unit"] or 0)
        unit_label = "克" if gpu > 0 else (r["unit"] or "")
        qty = float(r["qty_per_unit"])
        if gpu > 0:
            qty_stock = round(qty / gpu, 4)
        else:
            qty_stock = qty
        unit_cost = float(r["unit_cost"] or 0)
        selling = float(r["selling_price"] or 0)
        out.append({
            "recipe_id": recipe["id"],
            "recipe_name": recipe["name"],
            "line_no": len(out) + 1,
            "category": r["category_name"],
            "item_id": r["item_id"],
            "item_name": r["item_name"],
            "qty_per_unit": qty,
            "unit": unit_label,
            "qty_stock_units": qty_stock,
            "unit_cost": unit_cost,
            "selling_price": selling,
            "cost_purchase": round(qty_stock * unit_cost, 4),
            "cost_selling": round(qty_stock * selling, 4),
        })
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in recipe["name"])
    return _csv_response(out, f"ic_recipe_{recipe['id']}_{safe_name}_{_stamp()}.csv")


@bp.route("/recipe-cost/recipes/<int:recipe_id>/export.csv", methods=["GET"])
@require_login
def recipe_bom_export_csv(recipe_id: int):
    """BOM rows for a single recipe (出品配方), polymorphic (item or ic_recipe)."""
    db = get_warehouse_db()
    recipe = db.execute(
        "SELECT id, name, output_unit, output_qty, sale_price "
        "FROM recipes WHERE id=?", (recipe_id,),
    ).fetchone()
    if recipe is None:
        flash("出品配方不存在")
        return redirect(url_for("recipe_cost.recipes_list"))
    rows = db.execute(
        """SELECT ri.id AS line_id, ri.source_type, ri.qty_per_unit,
                  ri.item_id, ri.ic_recipe_id,
                  i.name AS item_name, i.unit AS item_unit,
                  i.gram_per_unit, i.unit_cost, i.selling_price,
                  c.name AS category_name,
                  ic.name AS ic_recipe_name, ic.output_qty AS ic_output_qty
           FROM recipe_items ri
           LEFT JOIN items i ON i.id = ri.item_id
           LEFT JOIN categories c ON c.id = i.category_id
           LEFT JOIN ic_recipes ic ON ic.id = ri.ic_recipe_id
           WHERE ri.recipe_id = ?
           ORDER BY ri.id""",
        (recipe_id,),
    ).fetchall()
    out = []
    for r in rows:
        qty = float(r["qty_per_unit"])
        if r["source_type"] == "item":
            gpu = float(r["gram_per_unit"] or 0)
            unit_label = "克" if gpu > 0 else (r["item_unit"] or "")
            qty_stock = round(qty / gpu, 4) if gpu > 0 else qty
            unit_cost = float(r["unit_cost"] or 0)
            selling = float(r["selling_price"] or 0)
            out.append({
                "recipe_id": recipe["id"],
                "recipe_name": recipe["name"],
                "line_no": len(out) + 1,
                "source_type": "item",
                "ingredient": f"{r['category_name']} / {r['item_name']}",
                "qty_per_unit": qty,
                "unit": unit_label,
                "qty_stock_units": qty_stock,
                "unit_cost": unit_cost,
                "selling_price": selling,
                "cost_purchase": round(qty_stock * unit_cost, 4),
                "cost_selling": round(qty_stock * selling, 4),
            })
        elif r["source_type"] == "ic_recipe":
            ic_grams = float(r["ic_output_qty"] or 0)
            qty_stock = round(qty * ic_grams, 4) if ic_grams > 0 else 0.0
            # Cost basis: pull ic_recipe totals and divide by ic.output_qty.
            from blueprints.recipe_cost_pure import ic_recipe_cost
            ic_cost = ic_recipe_cost(db, int(r["ic_recipe_id"]))
            if ic_cost and ic_grams > 0:
                per_unit_cp = float(ic_cost["cost_purchase"]) / ic_grams
                per_unit_cs = float(ic_cost["cost_selling"]) / ic_grams
            else:
                per_unit_cp = per_unit_cs = 0.0
            out.append({
                "recipe_id": recipe["id"],
                "recipe_name": recipe["name"],
                "line_no": len(out) + 1,
                "source_type": "ic_recipe",
                "ingredient": r["ic_recipe_name"],
                "qty_per_unit": qty,
                "unit": "份",
                "qty_stock_units": qty_stock,
                "unit_cost": round(per_unit_cp, 4),
                "selling_price": round(per_unit_cs, 4),
                "cost_purchase": round(per_unit_cp * qty, 4),
                "cost_selling": round(per_unit_cs * qty, 4),
            })
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in recipe["name"])
    return _csv_response(out, f"recipe_{recipe['id']}_{safe_name}_{_stamp()}.csv")


# ---------------------------------------------------------------------------
# Publish recipes to storefronts (cross-warehouse)
# ---------------------------------------------------------------------------

def _apply_recipe_snapshot_to_warehouse(master_conn, target_code: str, snapshot: dict) -> None:
    """Open target warehouse db and replay the snapshot in.

    Steps:
      1. Ensure source category exists in target (idempotent INSERT).
      2. Upsert each item in the BOM by sku (overwrite semantics).
      3. Insert a NEW ic_recipe / recipe row + BOM lines.

    Each insert is in its own transaction; the whole apply is wrapped
    in `with sqlite3.connect(...)` so it's atomic per warehouse.
    """
    from contextlib import closing
    import sqlite3 as _sq
    from blueprints.publish_recipe_pure import apply_item_to_warehouse

    master_conn.row_factory = _sq.Row
    row = master_conn.execute(
        "SELECT db_path, warehouse_type FROM warehouses WHERE code = ?",
        (target_code,),
    ).fetchone()
    if row is None:
        raise ValueError(f"warehouse {target_code} not registered in master.db")
    # Don't try to publish into an R&D warehouse (would create circular).
    if row["warehouse_type"] == "rd":
        raise ValueError(
            f"{target_code} is an R&D warehouse; cannot publish into it"
        )

    target_path = Path(row["db_path"])
    if not target_path.is_absolute():
        from config import BASE_DIR as _BASE_DIR
        target_path = Path(_BASE_DIR) / target_path
    if not target_path.exists():
        raise FileNotFoundError(f"warehouse db not found: {target_path}")

    recipe_type = snapshot["recipe_type"]
    head = snapshot["head"]
    lines = snapshot["lines"]

    table = "ic_recipes" if recipe_type == "ic_recipe" else "recipes"
    bom_table = "ic_recipe_items" if recipe_type == "ic_recipe" else "recipe_items"
    bom_id_col = "ic_recipe_id" if recipe_type == "ic_recipe" else "recipe_id"

    with closing(_sq.connect(target_path)) as tc:
        tc.row_factory = _sq.Row
        tc.execute("PRAGMA foreign_keys = ON")
        ts = now()

        # Upsert items first so the BOM can reference them.
        sku_to_new_id: dict[str, int] = {}
        for ln in lines:
            snap_item = {
                "sku": ln["sku"],
                "name": ln["item_name"],
                "category_name": ln["category_name"],
                "unit": ln["item_unit"],
                "gram_per_unit": ln["gram_per_unit"],
                "unit_cost": ln["unit_cost"],
                "selling_price": ln["selling_price"],
                "aux_unit": None,
                "aux_rate": 0,
                "safety_stock": 0,
            }
            apply_item_to_warehouse(tc, snap_item, action="overwrite")
            sku_to_new_id[ln["sku"]] = int(tc.execute(
                "SELECT id FROM items WHERE sku = ?", (ln["sku"],)
            ).fetchone()["id"])

        # Insert the recipe. output_unit is hard-coded 'g' in the
        # source (CostReview convention). We DO overwrite on publish —
        # a storefront never has an "existing" recipe from R&D.
        tc.execute(
            f"""INSERT INTO {table}
               (name, note, output_unit, output_qty, sale_price,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (head["name"], head["note"] or "",
             head["output_unit"] or "g",
             float(head["output_qty"] or 0),
             float(head["sale_price"] or 0), ts, ts),
        )
        new_recipe_id = int(tc.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

        # BOM lines.
        for ln in lines:
            if recipe_type == "ic_recipe":
                tc.execute(
                    f"""INSERT INTO {bom_table} ({bom_id_col}, item_id, qty_per_unit)
                       VALUES (?, ?, ?)""",
                    (new_recipe_id, sku_to_new_id[ln["sku"]],
                     float(ln["qty_per_unit"])),
                )
            else:
                # polymorphic recipe — respect the source's source_type
                st = ln.get("source_type") or "item"
                if st == "ic_recipe":
                    # Need to also publish the referenced ic_recipe
                    # recursively. Defer to a follow-up TODO; for now
                    # skip polymorphic ic_recipe lines.
                    continue
                tc.execute(
                    f"""INSERT INTO {bom_table} ({bom_id_col}, source_type,
                       item_id, ic_recipe_id, qty_per_unit)
                       VALUES (?, 'item', ?, NULL, ?)""",
                    (new_recipe_id, sku_to_new_id[ln["sku"]],
                     float(ln["qty_per_unit"])),
                )
        tc.commit()


def _handle_recipe_publish(recipe_type: str, recipe_id: int):
    """POST handler shared by ic_recipe and recipe publish endpoints."""
    from contextlib import closing
    from config import MASTER_DB
    import sqlite3 as _sq
    from blueprints.publish_recipe_pure import (
        publish_version, list_versions, get_published_version_for_recipe,
    )

    # 1. Confirm source recipe still exists in current warehouse.
    wh_db = get_warehouse_db()
    head = wh_db.execute(
        "SELECT id, name FROM {} WHERE id = ?".format(
            "ic_recipes" if recipe_type == "ic_recipe" else "recipes"
        ),
        (recipe_id,),
    ).fetchone()
    if head is None:
        flash("配方不存在")
        return redirect(url_for(
            "recipe_cost.ic_recipes_list" if recipe_type == "ic_recipe"
            else "recipe_cost.recipes_list"
        ))

    # 2. Parse + validate target warehouse_codes.
    target_codes = request.form.getlist("warehouse_codes")
    summary = request.form.get("summary", "").strip() or None
    if not target_codes:
        flash("请至少选择一个目标门店")
        return redirect(request.referrer or url_for(
            "recipe_cost.ic_recipes_list" if recipe_type == "ic_recipe"
            else "recipe_cost.recipes_list"
        ))

    # 3. Find the current draft version (or warn).
    with closing(_sq.connect(MASTER_DB)) as master_conn:
        master_conn.execute("PRAGMA foreign_keys = ON")
        draft = master_conn.execute(
            """SELECT * FROM recipe_versions
               WHERE recipe_type = ? AND recipe_id = ? AND status = 'draft'
               ORDER BY version DESC LIMIT 1""",
            (recipe_type, recipe_id),
        ).fetchone()

    if draft is None:
        # No draft yet — make one from current state.
        version_id = _upsert_recipe_draft(
            recipe_type, recipe_id,
            user_id=g.user["id"] if g.user else None,
        )
        if version_id is None:
            flash("无法创建草稿版本（数据库异常）")
            return redirect(request.referrer)
    else:
        version_id = int(draft["id"])

    # 4. Publish.
    with closing(_sq.connect(MASTER_DB)) as master_conn:
        master_conn.execute("PRAGMA foreign_keys = ON")
        result = publish_version(
            master_conn, version_id, target_codes,
            user_id=g.user["id"] if g.user else None,
            summary=summary,
            apply_func=_apply_recipe_snapshot_to_warehouse,
        )
        master_conn.commit()

    audit(
        "recipe_cost.recipe_publish",
        recipe_type, recipe_id,
        {"event_id": result["event_id"], "status": result["status"],
         "targets": target_codes},
    )

    # 5. Fan-out: emit_event 'recipe_published' for each user (PRD §2.5.4).
    try:
        from blueprints.notifications_pure import emit_event
        target_url = (
            url_for("recipe_cost.ic_recipe_edit", ic_recipe_id=recipe_id)
            if recipe_type == "ic_recipe"
            else url_for("recipe_cost.recipe_edit", recipe_id=recipe_id)
        )
        with closing(_sq.connect(MASTER_DB)) as master_conn:
            master_conn.row_factory = _sq.Row  # dict-style access on rows
            user_ids = [r["id"] for r in master_conn.execute(
                "SELECT id FROM users WHERE is_admin = 1"
            ).fetchall()]
            summary_text = summary or f"{head['name']} 已发布到 {len(target_codes)} 个门店"
            emit_event(master_conn, "recipe_published", summary_text,
                       target_url=target_url, user_ids=user_ids)
            master_conn.commit()
    except Exception as exc:  # noqa: BLE001 — don't fail publish on notification error
        audit("recipe_cost.recipe_publish.notify_failed", recipe_type, recipe_id,
              {"error": str(exc)})

    flash(f"发布完成（{result['status']}）："
          f"{sum(1 for w in result['per_warehouse'] if w['status'] == 'success')}/"
          f"{len(result['per_warehouse'])} 个门店成功")
    return redirect(url_for(
        "recipe_cost.recipe_versions_history",
        recipe_type=recipe_type, recipe_id=recipe_id,
    ))


@bp.route("/recipe-cost/ic-recipes/<int:ic_recipe_id>/publish", methods=["POST"])
@require_platform_admin
def ic_recipe_publish(ic_recipe_id: int):
    return _handle_recipe_publish("ic_recipe", ic_recipe_id)


@bp.route("/recipe-cost/recipes/<int:recipe_id>/publish", methods=["POST"])
@require_platform_admin
def recipe_publish(recipe_id: int):
    return _handle_recipe_publish("recipe", recipe_id)


@bp.route("/recipe-cost/<recipe_type>/<int:recipe_id>/versions", methods=["GET"])
@require_login
def recipe_versions_history(recipe_type: str, recipe_id: int):
    """Show version history + publish events for a recipe."""
    from contextlib import closing
    from config import MASTER_DB
    import sqlite3 as _sq
    from blueprints.publish_recipe_pure import (
        list_versions, list_publish_events_for_recipe, get_event_warehouses,
    )

    if recipe_type not in ("ic_recipe", "recipe"):
        abort(404)

    # Resolve human-readable source name from current warehouse db.
    wh_db = get_warehouse_db()
    table = "ic_recipes" if recipe_type == "ic_recipe" else "recipes"
    head = wh_db.execute(
        f"SELECT id, name FROM {table} WHERE id = ?", (recipe_id,)
    ).fetchone()
    if head is None:
        flash("配方不存在")
        return redirect(url_for(
            "recipe_cost.ic_recipes_list" if recipe_type == "ic_recipe"
            else "recipe_cost.recipes_list"
        ))

    with closing(_sq.connect(MASTER_DB)) as master_conn:
        master_conn.row_factory = _sq.Row
        versions = list_versions(master_conn, recipe_type, recipe_id)
        events = list_publish_events_for_recipe(
            master_conn, recipe_type, recipe_id
        )
        # Attach per-event warehouse rows.
        for ev in events:
            ev["warehouses"] = get_event_warehouses(master_conn, int(ev["id"]))

    target_codes = request.args.getlist("warehouse_codes")
    return render_template(
        "recipe_cost/recipe_versions.html",
        recipe_type=recipe_type,
        recipe=head,
        versions=versions,
        events=events,
        available_warehouses=_list_storefront_warehouses(),
    )


def _list_storefront_warehouses() -> list[dict]:
    """All storefront warehouses (excludes rd_*) — for the publish UI picker."""
    from contextlib import closing
    from config import MASTER_DB
    import sqlite3 as _sq
    with closing(_sq.connect(MASTER_DB)) as master_conn:
        master_conn.row_factory = _sq.Row
        rows = master_conn.execute(
            "SELECT code, name FROM warehouses "
            "WHERE warehouse_type = 'storefront' ORDER BY code"
        ).fetchall()
    return [dict(r) for r in rows]
