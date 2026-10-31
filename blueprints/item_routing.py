"""平台管理员显式管理租户 1 履约路由。"""
import sqlite3
from uuid import uuid4

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from blueprints import item_routing_pure as routing
from blueprints.auth import audit
from db import get_master_db
from permissions import require_platform_admin

bp = Blueprint("item_routing", __name__)


def _tenant_one():
    if g.user["tenant_id"] != 1:
        abort(403)


def _no_store(response):
    response.headers["Cache-Control"] = "no-store"
    return response


def _audit_committed(action, target_id, detail):
    """路由已经提交；仓内审计另行提交，失败不能冒充主库回滚。"""
    try:
        if not g.get("warehouse_db_path"):
            raise RuntimeError("未选择审计仓库")
        audit(action, "canonical_item" if target_id else "item_routing_batch", target_id, detail)
        return True
    except Exception:
        current_app.logger.exception(
            "item_routing committed but warehouse audit failed actor=%s detail=%s",
            g.user["id"], detail)
        return False


@bp.route("/admin/item-routing")
@require_platform_admin
def admin():
    _tenant_one()
    master = get_master_db()
    try:
        diagnostics = routing.routing_diagnostics(master)
    except (OSError, sqlite3.Error, ValueError):
        return render_template("item_routing/admin.html", error="无法读取配送中心绑定，请修复后重试"), 503
    configured = {row["canonical_id"]: row for row in routing.list_routing(master)}
    rows = master.execute(
        "SELECT c.id, c.canonical_sku, c.name, c.status, c.category_code, "
        "cc.name AS category_name FROM canonical_items c "
        "LEFT JOIN canonical_categories cc ON cc.code=c.category_code "
        "WHERE c.tenant_id=1 ORDER BY c.id").fetchall()
    warehouses = [dict(row) for row in master.execute(
        "SELECT code, name FROM warehouses WHERE tenant_id=1 "
        "AND warehouse_type='distribution_center' ORDER BY code")]
    categories = [dict(row) for row in master.execute(
        "SELECT code, name FROM canonical_categories ORDER BY name, code")]
    items = []
    for row in rows:
        item = dict(row)
        route = configured.get(item["id"])
        item["saved"] = {"dc_code": route["fulfillment_warehouse_code"],
                         "updated_at": route["updated_at"]} if route else {
                             "dc_code": None, "updated_at": None}
        item["diagnostic"] = diagnostics.get(item["id"])
        items.append(item)
    response = current_app.make_response(render_template(
        "item_routing/admin.html", dataset={"items": items, "warehouses": warehouses,
                                           "categories": categories}))
    return _no_store(response)


@bp.route("/admin/item-routing/batch", methods=["POST"])
@require_platform_admin
def batch():
    _tenant_one()
    batch_id = uuid4().hex
    try:
        changes = routing.parse_batch(request.get_json(silent=True))
    except routing.BatchRoutingError as exc:
        return _no_store(jsonify(ok=False, message=str(exc), errors=exc.errors)), exc.status
    master = get_master_db()
    try:
        master.execute("BEGIN IMMEDIATE")
        items, diffs = routing.apply_batch(master, changes, actor_id=g.user["id"])
        master.commit()
    except routing.BatchRoutingError as exc:
        master.rollback()
        return _no_store(jsonify(ok=False, message=str(exc), errors=exc.errors)), exc.status
    except sqlite3.Error:
        master.rollback()
        current_app.logger.exception("item_routing batch rolled back batch_id=%s actor=%s",
                                     batch_id, g.user["id"])
        return _no_store(jsonify(ok=False, message="数据库暂不可用，整批已回滚；草稿保留，请重试",
                                 errors={}, batch_id=batch_id)), 503
    audit_recorded = _audit_committed("item_routing.batch", None, {
        "batch_id": batch_id, "actor_id": g.user["id"], "changes": diffs})
    return _no_store(jsonify(ok=True, batch_id=batch_id, items=items,
                             updated_count=sum(d["changed"] for d in diffs),
                             audit_recorded=audit_recorded))


@bp.route("/admin/item-routing/<int:canonical_id>", methods=["POST"])
@require_platform_admin
def set_route(canonical_id):
    _tenant_one()
    master = get_master_db()
    dc_code = request.form.get("dc_code", "").strip()
    previous = routing.routing_snapshot(master, canonical_id)
    try:
        routing.set_routing(master, canonical_id, dc_code, actor_id=g.user["id"])
        master.commit()
    except ValueError as exc:
        master.rollback()
        abort(400, description=str(exc))
    _audit_committed("item_routing.set", canonical_id, {
        "batch_id": uuid4().hex, "actor_id": g.user["id"], "dc_code": dc_code,
        "old": previous, "new": routing.routing_snapshot(master, canonical_id)})
    flash("履约路由已保存")
    return redirect(url_for("item_routing.admin"))


@bp.route("/admin/item-routing/<int:canonical_id>/delete", methods=["POST"])
@require_platform_admin
def delete_route(canonical_id):
    _tenant_one()
    master = get_master_db()
    previous = routing.routing_snapshot(master, canonical_id)
    try:
        routing.delete_routing(master, canonical_id, actor_id=g.user["id"])
        master.commit()
    except ValueError as exc:
        master.rollback()
        abort(400, description=str(exc))
    _audit_committed("item_routing.delete", canonical_id, {
        "batch_id": uuid4().hex, "actor_id": g.user["id"], "dc_code": previous["dc_code"],
        "old": previous, "new": {"dc_code": None, "updated_at": None}})
    flash("履约路由已删除")
    return redirect(url_for("item_routing.admin"))
