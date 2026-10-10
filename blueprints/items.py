"""Items CRUD and inventory read view.

2026-10-10 收敛：门店 / 研发中心不再具备品项「创建权」与「跨仓发布权」。
`warehouse.items` 主档的唯一创建入口是平台管理员的「品项主数据」
(`/canonical/*`)；本蓝图只保留只读 + 单仓启停 + 安全库存重算等管理动作。
"""
from __future__ import annotations

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from db import get_warehouse_db
from permissions import require_login, require_platform_admin, require_role

from . import canonical_pure as cp
from ._helpers import now, parse_qty, warehouse_categories_in_clause
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
        # 2026-10-10 收敛：品项创建权统一归「品项主数据」(/canonical/*)。
        # 门店 / 研发中心 / 平台管理员在本页均不再新增品项；
        # 旧书签与脚本一律引导到主数据页，不再走 Q1 三条出路的 session 流程。
        flash(
            "品项已在「品项主数据」统一维护：门店与研发中心不再自行新增。"
            "如需新增品项，请联系平台管理员在主数据创建后扇出下发。"
        )
        return redirect(url_for("items.items_list"))

    placeholders, params = warehouse_categories_in_clause()
    categories_data = db.execute(
        f"SELECT id, name, description FROM categories WHERE name IN ({placeholders}) ORDER BY name",
        params,
    ).fetchall()
    # 2026-10-10：主数据「全公司停用」(canonical_status='disabled') 的门店行
    # 从「品类与品项」列表隐藏（owner 拍板：列表直接隐藏）。
    rows = db.execute(
        f"""SELECT i.*, c.name AS category_name
            FROM items i JOIN categories c ON c.id = i.category_id
            WHERE c.name IN ({placeholders})
              AND {cp.store_visible_clause('i')}
            ORDER BY i.id DESC""",
        params,
    ).fetchall()
    from config import SAFETY_STOCK_FACTOR, SAFETY_STOCK_WINDOW_DAYS

    return render_template(
        "items.html",
        items=rows,
        categories=categories_data,
        safety_stock_window=SAFETY_STOCK_WINDOW_DAYS,
        safety_stock_factor=SAFETY_STOCK_FACTOR,
    )


@bp.route("/items/<int:item_id>/edit", methods=["GET", "POST"])
@require_platform_admin
@require_role("manager")
def edit_item(item_id: int):
    db = get_warehouse_db()
    old = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if old is None:
        from flask import abort
        abort(404)
    # P0-1 + P0-9 主数据收口：已绑定 canonical 的行，名称/品类/单位/辅单位/
    # 克重/售价/进货价 全部由「品项主数据」维护，本页服务端强制忽略表单里的
    # 这些字段（价格自 2026-10-09 收归主数据，见方案 P0-9）。
    bound = old["canonical_id"] is not None

    if request.method == "POST":
        # 已纳管行：主数据字段 + 价格一律不采信表单（本仓仅 is_store_exclusive
        # / is_active 等本仓运营字段可动，且不在本页面提交）。
        if bound:
            flash(
                "该品项已纳入主数据：名称/单位/品类/单位换算/售价/进货价"
                "均由「品项主数据」统一维护，请到主数据修改后扇出。"
            )
            return redirect(url_for("items.edit_item", item_id=item_id))

        name = request.form.get("name", "").strip()
        category_id = request.form.get("category_id", "").strip()
        # safety_stock 自 P0-12 起由系统计算（近 7 天消耗 × 系数），全角色只读，
        # 编辑时一律沿用库中现值，不采信表单。
        safety_stock = float(old["safety_stock"] or 0)
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
    return render_template(
        "edit_item.html", item=old, categories=categories_data, bound=bound,
    )


@bp.route("/items/<int:item_id>/delete", methods=["POST"])
@require_platform_admin
def delete_item(item_id: int):
    db = get_warehouse_db()
    row = db.execute(
        "SELECT canonical_id FROM items WHERE id=?", (item_id,)
    ).fetchone()
    if row is None:
        from flask import abort
        abort(404)
    # P0-2：已纳管主数据的行禁止物理删除（Q3 deactivate_only 的仓内侧）。
    if row["canonical_id"] is not None:
        flash("该品项已纳入主数据，不能删除；如需在本仓停用，请在「品项主数据」详情页操作")
        return redirect(url_for("items.items_list"))
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


@bp.route("/items/<int:item_id>/toggle-active", methods=["POST"])
@require_platform_admin
def toggle_item_active(item_id: int):
    """P0-10 —— 单仓品项启停（仅 platform admin）。

    与「主数据停用」（全公司，走 /canonical 详情页）区分：本路由只改本仓
    items.is_active，仅影响本仓的出库/入库/生产/盘点/调整/订货选择点，
    库存与历史记录全保留。
    """
    from blueprints.items_pure import set_item_active

    db = get_warehouse_db()
    active = request.form.get("active", "0") == "1"
    try:
        result = set_item_active(db, item_id, active)
    except ValueError as e:
        flash(str(e))
        return redirect(url_for("items.items_list"))

    audit("items.set_active", "item", item_id, {
        "active": result["active"], "quantity": result["quantity"],
    })
    if active:
        flash("已在本仓启用该品项")
    elif result["quantity"] > 0:
        flash(
            f"已在本仓停用该品项；注意本仓仍有库存 {result['quantity']}，"
            f"建议先出清或盘点"
        )
    else:
        flash("已在本仓停用该品项")
    return redirect(url_for("items.items_list"))


@bp.route("/items/recompute-safety-stock", methods=["POST"])
@require_platform_admin
def recompute_safety_stock():
    """P0-12 安全库存自动计算：safety_stock = Σ近 N 天消耗量 × 系数。

    消耗口径与 /inventory 页一致；品项消耗历史覆盖不满 N 天窗口则写 0。
    系数与窗口天数见 config.SAFETY_STOCK_*。
    """
    from blueprints.items_pure import recompute_warehouse_safety_stocks
    from config import SAFETY_STOCK_FACTOR, SAFETY_STOCK_WINDOW_DAYS

    db = get_warehouse_db()
    result = recompute_warehouse_safety_stocks(
        db, window_days=SAFETY_STOCK_WINDOW_DAYS, factor=SAFETY_STOCK_FACTOR
    )
    db.commit()
    audit(
        "items.recompute_safety_stock",
        "warehouse",
        g.warehouse["id"] if g.get("warehouse") else None,
        result,
    )
    flash(
        f"安全库存已重算：共 {result['total']} 个品项，"
        f"{result['zeroed']} 个因消耗历史不足 {result['window_days']} 天写 0"
    )
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
             AND {cp.store_visible_clause('i')}
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
    """【已下线，方案 P0-5】旧的 rd → 门店品项直发通道。

    2026-10-09 owner 拍板：品项下发统一走 canonical 扇出（/canonical/fanout），
    本路由仅保留重定向，避免旧书签 / 脚本拿到 404。历史事件仍可在
    /items/publish/history 查询（只读）。
    """
    flash("品项下发已统一到「品项主数据 → 扇出」，请在新页面选择主数据项下发")
    return redirect(url_for("canonical.canonical_fanout"))


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


@bp.route("/items/new-item-redirect")
@require_role("manager")
def new_item_redirect():
    """【已停用，2026-10-10】Q1 三条出路页。

    `/items` POST 已收敛为「只读提示 + 拒绝」，不再写入
    session["pending_new_item"]，因此本页恒走「没有待处理的申请」分支。
    保留路由仅为兼容旧书签 / 脚本（避免 404）。
    """
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
