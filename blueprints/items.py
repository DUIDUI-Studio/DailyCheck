"""Items CRUD and inventory read view."""
from __future__ import annotations

import sqlite3

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from db import get_warehouse_db
from permissions import require_login, require_platform_admin, require_role

from ._helpers import gen_sku, now, parse_qty, warehouse_categories_in_clause
from .auth import audit

bp = Blueprint("items", __name__)


@bp.before_request
def _require_storefront():
    """Items CRUD are storefront-only;研发中心 can see items but not unit_cost."""
    from flask import abort

    from permissions import WAREHOUSE_EXEMPT
    if request.endpoint in WAREHOUSE_EXEMPT or request.endpoint is None:
        return None
    if g.user is not None and g.user["is_admin"]:
        return None
    wh = g.get("warehouse")
    if wh is None:
        return None
    if wh["warehouse_type"] == "rd":
        return None  # rd 可以看 items，只是模板里不显示 unit_cost
    if wh["warehouse_type"] not in ("storefront", "distribution_center"):
        flash("该仓库类型不支持库存管理功能")
        abort(403)
        return None
    return None


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
        selling_price = float(request.form.get("selling_price", "0") or 0)
        unit = request.form.get("unit", "件").strip() or "件"
        aux_unit = request.form.get("aux_unit", "").strip() or None
        aux_rate = parse_qty(request.form.get("aux_rate", "0"))

        if aux_rate < 0:
            flash("辅单位换算率不能为负数")
            return redirect(url_for("items.items_list"))
        if unit_cost < 0:
            flash("进货单价不能为负数")
            return redirect(url_for("items.items_list"))
        if selling_price < 0:
            flash("销售单价不能为负数")
            return redirect(url_for("items.items_list"))
        if not name or not category_id:
            flash("名称、品类为必填")
            return redirect(url_for("items.items_list"))

        # ─────────────────────────────────────────────────────────────────
        # Q1=deny 三出路拦截 (Spec §1.6.4) + US-2 强信号检测
        # ─────────────────────────────────────────────────────────────────
        from db import get_master_db
        from blueprints import canonical_pure as cp

        m = get_master_db()
        # 拿到本仓品类的 canonical_code
        cat_row = m.execute(
            """SELECT cc.code FROM canonical_categories cc
               JOIN warehouses w ON w.id IS NOT NULL
               WHERE 1=0"""
        ).fetchone()  # placeholder; we'll use a different approach below
        # 直接读本仓 categories 的 canonical_code
        cat_code_row = db.execute(
            "SELECT canonical_code FROM categories WHERE id=?", (int(category_id),)
        ).fetchone()
        category_code = cat_code_row["canonical_code"] if cat_code_row else None

        # US-2 强信号候选(T6 检测:按 name + unit 跨仓找相似)
        similar = _us2_strong_signal_suggestions(m, name, unit)

        # check_new_item_policy — canonical_id=None 表示新建
        policy = cp.check_new_item_policy(
            name=name, canonical_id=None,
            category_code=category_code,
            similar_canonicals=similar,
        )
        if not policy["allowed"]:
            # 拒绝 + 给出三条出路(Spec §1.6.4)
            flash(policy["reason"])
            # 把候选存入 session,渲染选用页时使用
            session_suggestions = [
                {
                    "canonical_id": s.get("canonical_id"),
                    "name": s.get("name"),
                    "score": s.get("score", 0),
                }
                for s in policy["suggestions"]
            ]
            from flask import session as _sess
            _sess["pending_new_item"] = {
                "name": name,
                "category_id": int(category_id),
                "unit": unit,
                "quantity": quantity,
                "safety_stock": safety_stock,
                "unit_cost": unit_cost,
                "selling_price": selling_price,
                "aux_unit": aux_unit,
                "aux_rate": aux_rate,
                "category_code": category_code,
                "suggestions": session_suggestions,
                "next_action": policy["next_action"],
                "reason": policy["reason"],
            }
            return redirect(url_for("items.new_item_redirect"))

        is_store_exclusive = bool(policy["requires_store_exclusive"])
        # gram_per_unit 同步：仅当 aux_unit=='克'
        gram_per_unit = aux_rate if aux_unit == "克" else 0.0

        try:
            db.execute(
                """INSERT INTO items
                   (sku, name, category_id, quantity, safety_stock, unit_cost,
                    selling_price, selling_price_updated_at,
                    unit, gram_per_unit, aux_unit, aux_rate, updated_at,
                    is_store_exclusive)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (gen_sku(), name, int(category_id), quantity, safety_stock,
                 unit_cost, selling_price, now() if selling_price > 0 else None,
                 unit, gram_per_unit, aux_unit, aux_rate, now(),
                 1 if is_store_exclusive else 0),
            )
            new_id = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            db.commit()
            audit("items.create", "item", new_id, {"name": name})
            flash("库存品创建成功" + (" (门店专属)" if is_store_exclusive else ""))
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
        selling_price = float(request.form.get("selling_price", "0") or 0)
        unit = request.form.get("unit", "件").strip() or "件"
        aux_unit = request.form.get("aux_unit", "").strip() or None
        aux_rate = parse_qty(request.form.get("aux_rate", "0"))
        if aux_rate < 0:
            flash("辅单位换算率不能为负数")
            return redirect(url_for("items.edit_item", item_id=item_id))
        if unit_cost < 0:
            flash("进货单价不能为负数")
            return redirect(url_for("items.edit_item", item_id=item_id))
        if selling_price < 0:
            flash("销售单价不能为负数")
            return redirect(url_for("items.edit_item", item_id=item_id))
        if not name or not category_id:
            flash("名称、品类为必填")
            return redirect(url_for("items.edit_item", item_id=item_id))

        # 生产配方锁：被 product_bom 引用且原启用克禁止切走克
        old = db.execute(
            "SELECT aux_unit, aux_rate, gram_per_unit, selling_price FROM items WHERE id=?", (item_id,)
        ).fetchone()
        if (old["aux_unit"] == "克" and old["gram_per_unit"] > 0
                and aux_unit != "克"):
            bom_ref = db.execute(
                "SELECT COUNT(*) AS c FROM product_bom WHERE item_id=?", (item_id,)
            ).fetchone()["c"]
            if bom_ref > 0:
                flash("该品项已被生产配方引用且启用克，不能切到非克辅单位；请先清空相关配方")
                return redirect(url_for("items.edit_item", item_id=item_id))

        sp_updated_at = now() if selling_price != float(old["selling_price"] or 0) else None
        gram_per_unit = aux_rate if aux_unit == "克" else 0.0
        db.execute(
            """UPDATE items SET name=?, category_id=?, safety_stock=?,
               unit_cost=?, selling_price=?, selling_price_updated_at=?,
               unit=?, gram_per_unit=?, aux_unit=?, aux_rate=?,
               updated_at=? WHERE id=?""",
            (name, int(category_id), safety_stock, unit_cost,
             selling_price, sp_updated_at,
             unit, gram_per_unit, aux_unit, aux_rate,
             now(), item_id),
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
@require_platform_admin
def delete_item(item_id: int):
    db = get_warehouse_db()
    usage = db.execute(
        """SELECT
              (SELECT COUNT(*) FROM stock_movements WHERE item_id=?) +
              (SELECT COUNT(*) FROM restock_requests WHERE item_id=?) +
              (SELECT COUNT(*) FROM outbound_requests WHERE item_id=?) +
              (SELECT COUNT(*) FROM stocktakes WHERE item_id=?) +
              (SELECT COUNT(*) FROM ic_recipe_items WHERE item_id=?) +
              (SELECT COUNT(*) FROM recipe_items WHERE item_id=? AND source_type='item') AS c""",
        (item_id, item_id, item_id, item_id, item_id, item_id),
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


# ---------------------------------------------------------------------------
# Item / category cross-warehouse publishing
# ---------------------------------------------------------------------------

@bp.route("/items/publish", methods=["GET", "POST"])
@require_platform_admin
def items_publish():
    """Per-storefront publish of selected items from current (rd) warehouse.

    GET: show picker (all items in current warehouse + storefront checkbox list).
    POST: do the publish via publish_items(), redirect to history.
    """
    if request.method == "POST":
        from contextlib import closing
        from config import MASTER_DB
        import sqlite3 as _sq
        from blueprints.publish_recipe_pure import (
            publish_items, list_item_publish_events,
        )

        item_ids = [int(x) for x in request.form.getlist("item_ids") if x]
        target_codes = request.form.getlist("warehouse_codes")
        default_action = request.form.get("default_action", "overwrite")
        summary = request.form.get("summary", "").strip() or None

        if not item_ids:
            flash("请至少选择一个品项")
            return redirect(url_for("items.items_publish"))
        if not target_codes:
            flash("请至少选择一个目标门店")
            return redirect(url_for("items.items_publish"))

        try:
            wh_db = get_warehouse_db()
        except RuntimeError:
            flash("请先选择一个仓库")
            return redirect(url_for("auth.warehouse_picker"))
        with closing(_sq.connect(MASTER_DB)) as master_conn:
            master_conn.execute("PRAGMA foreign_keys = ON")
            wh_code = g.warehouse["code"] if g.get("warehouse") else "rd_001"
            result = publish_items(
                master_conn, wh_db, wh_code, item_ids, target_codes,
                user_id=g.user["id"] if g.user else None,
                summary=summary, default_action=default_action,
            )
            master_conn.commit()
        audit(
            "items.publish", "items_publish", result["event_id"],
            {"item_count": result["item_count"],
             "warehouses": target_codes,
             "status": result["status"]},
        )
        flash(f"批量同步完成：{result['status']}，共 {result['item_count']} 个品项")
        return redirect(url_for("items.items_publish_history",
                                event_id=result["event_id"]))

    # GET: show picker.
    db = get_warehouse_db()
    items = db.execute(
        "SELECT id, sku, name, unit, gram_per_unit, unit_cost, selling_price "
        "FROM items ORDER BY name"
    ).fetchall()
    return render_template(
        "items_publish.html",
        items=items,
        available_warehouses=_list_storefront_warehouses(),
    )


@bp.route("/items/publish/history", methods=["GET"])
@bp.route("/items/publish/history/<int:event_id>", methods=["GET"])
@require_login
def items_publish_history(event_id: int | None = None):
    """List recent item publish events + drill-down detail."""
    from contextlib import closing
    from config import MASTER_DB
    import sqlite3 as _sq
    from blueprints.publish_recipe_pure import (
        list_item_publish_events, get_item_event_details,
    )
    with closing(_sq.connect(MASTER_DB)) as master_conn:
        master_conn.row_factory = _sq.Row
        events = list_item_publish_events(master_conn)
        detail = get_item_event_details(master_conn, event_id) if event_id else None
    return render_template(
        "items_publish_history.html",
        events=events,
        detail=detail,
        event_id=event_id,
    )


def _list_storefront_warehouses():
    """All storefront warehouses (excludes rd_*) — for publish UI picker.

    Mirrors recipe_cost._list_storefront_warehouses; could be DRY'd later.
    """
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


def _us2_strong_signal_suggestions(master_conn, name: str, unit: str) -> list[dict]:
    """US-2 强信号候选(Spec §3.1)。

    返回与拟新建品项的 (name + unit) 相近的 canonical_items 列表,
    按相似度降序,最多 5 条。
    """
    from blueprints import canonical_pure as cp

    items = cp.list_canonical_items(master_conn, status="active", limit=500)
    norm = cp.normalize_name(name)
    out: list[dict] = []
    for it in items:
        if it["unit"] != unit:
            continue
        sim = cp.name_similarity(norm, cp.normalize_name(it["name"]))
        if sim >= 0.5:
            out.append({
                "canonical_id": it["id"],
                "name": it["name"],
                "score": round(sim, 3),
            })
    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:5]


@bp.route("/items/new-item-redirect")
@require_role("manager")
def new_item_redirect():
    """Q1=deny 拒绝后渲染三条出路页。"""
    from flask import session
    pending = session.pop("pending_new_item", None)
    if not pending:
        flash("没有待处理的申请")
        return redirect(url_for("items.items_list"))
    return render_template("items/new_item_redirect.html", pending=pending)


@bp.route("/items/new-item-redirect/submit", methods=["POST"])
@require_role("manager")
def new_item_redirect_submit():
    """三条出路的提交分支。

    next_action:
      choose       → 已选某个候选:跳到跨仓差异页供管理员操作
      request_new  → 写一条 canonical_claim_requests,跳转申请单列表
    """
    from flask import session
    from db import get_master_db
    import blueprints.canonical_pure as cp
    m = get_master_db()
    pending = session.pop("pending_new_item", None)
    if not pending:
        flash("没有待处理的申请")
        return redirect(url_for("items.items_list"))
    action = request.form.get("action", "")
    if action == "choose":
        cid = request.form.get("canonical_id")
        flash(f"请到「跨仓差异」选用候选主数据 (id={cid})")
        return redirect(url_for("canonical.canonical_diff"))
    elif action == "request_new":
        try:
            rid = cp.submit_claim_request(
                m,
                warehouse_code=g.warehouse["code"],
                local_name=pending["name"],
                proposed_category_code=pending.get("category_code"),
                proposed_unit=pending.get("unit"),
                request_type="new_item",
                reason=request.form.get("reason"),
                submitted_by=g.user["id"],
            )
            flash(f"已提交新增申请 #{rid},等总部审批")
        except ValueError as e:
            flash(f"提交失败: {e}")
        return redirect(url_for("canonical.canonical_claim_requests"))
    flash("未知动作")
    return redirect(url_for("items.items_list"))
