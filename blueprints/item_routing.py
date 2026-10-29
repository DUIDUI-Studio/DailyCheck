"""平台管理员显式管理租户 1 履约路由。"""
import sqlite3

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from blueprints import item_routing_pure as routing
from blueprints.auth import audit
from db import get_master_db
from permissions import require_platform_admin

bp = Blueprint("item_routing", __name__)


def _tenant_one():
    if g.user["tenant_id"] != 1:
        abort(403)


@bp.route("/admin/item-routing")
@require_platform_admin
def admin():
    _tenant_one()
    master = get_master_db()
    try:
        pending = routing.routing_pending(master)
    except (OSError, sqlite3.Error, ValueError):
        return render_template("item_routing/admin.html", error="无法读取配送中心绑定，请修复后重试"), 503
    configured = {row["canonical_id"]: row for row in routing.list_routing(master)}
    items = master.execute(
        "SELECT id, canonical_sku, name, status FROM canonical_items "
        "WHERE tenant_id=1 ORDER BY id").fetchall()
    warehouses = master.execute(
        "SELECT code, name FROM warehouses WHERE tenant_id=1 "
        "AND warehouse_type='distribution_center' ORDER BY code").fetchall()
    return render_template("item_routing/admin.html", items=items, configured=configured,
                           pending=pending, warehouses=warehouses)


@bp.route("/admin/item-routing/<int:canonical_id>", methods=["POST"])
@require_platform_admin
def set_route(canonical_id):
    _tenant_one()
    master = get_master_db()
    dc_code = request.form.get("dc_code", "").strip()
    try:
        routing.set_routing(master, canonical_id, dc_code, actor_id=g.user["id"])
    except ValueError as exc:
        abort(400, description=str(exc))
    audit("item_routing.set", "canonical_item", canonical_id, {"dc_code": dc_code})
    master.commit()
    flash("履约路由已保存")
    return redirect(url_for("item_routing.admin"))


@bp.route("/admin/item-routing/<int:canonical_id>/delete", methods=["POST"])
@require_platform_admin
def delete_route(canonical_id):
    _tenant_one()
    master = get_master_db()
    previous = routing.get_routing(master, canonical_id)
    try:
        routing.delete_routing(master, canonical_id, actor_id=g.user["id"])
    except ValueError as exc:
        abort(400, description=str(exc))
    audit("item_routing.delete", "canonical_item", canonical_id, {"dc_code": previous})
    master.commit()
    flash("履约路由已删除")
    return redirect(url_for("item_routing.admin"))
