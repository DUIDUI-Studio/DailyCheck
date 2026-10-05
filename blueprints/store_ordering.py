"""Store ordering blueprint: storefront catalog/cart/orders + DC review/ship.

Spec: docs/2026-10-04-store-ordering-design.md §3.3 / §4 / §6.
"""
from __future__ import annotations

import functools
import sqlite3
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

from flask import (
    Blueprint,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from blueprints import store_ordering_pure as sop
from blueprints._helpers import parse_qty
from db import get_master_db
from permissions import require_login, require_role, require_warehouse_type

bp = Blueprint("store_ordering", __name__, url_prefix="/store-ordering")


# ---------------------------------------------------------------------------
# Helpers / decorators
# ---------------------------------------------------------------------------

def _current_warehouse_code() -> str | None:
    wh = g.get("warehouse")
    return wh["code"] if wh else None


def _current_warehouse_type() -> str | None:
    wh = g.get("warehouse")
    return wh["warehouse_type"] if wh else None


def _is_admin() -> bool:
    return bool(g.user and g.user["is_admin"])


def _list_dcs(master_conn: sqlite3.Connection) -> list[dict[str, Any]]:
    master_conn.row_factory = sqlite3.Row
    rows = master_conn.execute(
        "SELECT code, name FROM warehouses WHERE warehouse_type=? ORDER BY code",
        (sop.WAREHOUSE_TYPE_DC,),
    ).fetchall()
    return [dict(r) for r in rows]


def _today() -> date:
    return date.today()


def _tomorrow() -> date:
    return date.today() + timedelta(days=1)


def _date_input(d: date | None) -> str:
    return d.strftime("%Y-%m-%d") if d else ""


def _parse_date_input(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _storefront_or_admin(view: Callable) -> Callable:
    """Decorator: storefront routes. DC users are redirected to DC review page."""
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if _is_admin():
            return view(*args, **kwargs)
        wh_type = _current_warehouse_type()
        if wh_type == sop.WAREHOUSE_TYPE_DC:
            return redirect(url_for("store_ordering.review_list"))
        if wh_type != "storefront":
            flash("门店订货仅对门店或管理员开放")
            return redirect(url_for("core.dashboard"))
        return view(*args, **kwargs)
    return wrapped


def _order_viewable(order: dict[str, Any]) -> bool:
    """Order can be viewed if it belongs to current store, current DC, or user is admin."""
    if _is_admin():
        return True
    wh = g.get("warehouse")
    if wh is None:
        return False
    code = wh["code"]
    return order["store_warehouse_code"] == code or order["dc_warehouse_code"] == code


def _assert_order_belongs_to_current_dc(master: sqlite3.Connection, order_id: int) -> None:
    if _is_admin():
        return
    order_dc = sop.get_order_dc_code(master, order_id)
    if order_dc is None:
        abort(404)
    if order_dc != _current_warehouse_code():
        abort(403)


# ---------------------------------------------------------------------------
# Storefront catalog / cart
# ---------------------------------------------------------------------------

@bp.route("/catalog")
@require_login
def catalog() -> str:
    """Show DC selector or item catalog for the selected DC."""
    if _is_admin():
        pass
    elif _current_warehouse_type() == sop.WAREHOUSE_TYPE_DC:
        return redirect(url_for("store_ordering.review_list"))
    elif _current_warehouse_type() != "storefront":
        flash("门店订货仅对门店或管理员开放")
        return redirect(url_for("core.dashboard"))

    master = get_master_db()
    dc_code = request.args.get("dc") or session.get("store_ordering_dc")
    dcs = _list_dcs(master)

    if dc_code is None:
        return render_template(
            "store_ordering/catalog.html",
            dcs=dcs,
            items=None,
            selected_dc=None,
            cart_summary=None,
        )

    # Validate DC exists.
    if not any(d["code"] == dc_code for d in dcs):
        flash("请选择有效的配送中心")
        session.pop("store_ordering_dc", None)
        return redirect(url_for("store_ordering.catalog"))

    session["store_ordering_dc"] = dc_code
    store_code = _current_warehouse_code()
    # Admin without a storefront warehouse can still browse as admin, but
    # cannot create a cart. Treat admin in non-storefront as read-only.
    cart_summary = None
    if store_code:
        cart = sop.get_or_create_cart(master, g.user["id"], store_code, dc_code)
        cart_items = sop.list_cart_items(master, cart["id"])
        cart_summary = {
            "count": len(cart_items),
            "total_qty": sum(i["quantity"] for i in cart_items),
        }

    category_code = request.args.get("cat", "").strip() or None
    keyword = request.args.get("q", "").strip() or None
    items = sop.list_available_dc_items(master, dc_code, category_code, keyword)
    # Build (code, display_name) tuples for chips, deduped by code while keeping
    # the Chinese display name (display = canonical_categories.name).
    code_name_pairs: list[tuple[str, str]] = []
    seen_codes: set[str] = set()
    for it in items:
        code = it.get("category_code") or ""
        if not code or code in seen_codes:
            continue
        seen_codes.add(code)
        name = it.get("category_name") or code
        code_name_pairs.append((code, str(name)))
    category_names = sorted(code_name_pairs, key=lambda x: x[1])

    return render_template(
        "store_ordering/catalog.html",
        dcs=dcs,
        selected_dc=dc_code,
        items=items,
        categories=[c for c, _ in category_names],
        category_names=category_names,
        current_cat=category_code or "",
        keyword=keyword or "",
        cart_summary=cart_summary,
    )


@bp.route("/cart/add", methods=["POST"])
@require_login
@_storefront_or_admin
def add_to_cart() -> str:
    """DEPRECATED single-item form submit; catalog.html uses /cart/add-batch now.

    Kept for backward compatibility (e.g. legacy links, future "quick +1"
    affordances). The catalog page renders an inline modal-driven batch form
    pointing at /cart/add-batch.
    """
    dc_code = request.form.get("dc") or session.get("store_ordering_dc")
    if not dc_code:
        flash("请先选择配送中心")
        return redirect(url_for("store_ordering.catalog"))
    session["store_ordering_dc"] = dc_code

    canonical_id = request.form.get("canonical_id", type=int)
    quantity = parse_qty(request.form.get("quantity", "0"))
    unit = (request.form.get("unit") or "件").strip() or "件"
    if canonical_id is None or quantity <= 0:
        flash("请选择品项并填写有效数量")
        return redirect(url_for("store_ordering.catalog", dc=dc_code))

    master = get_master_db()
    store_code = _current_warehouse_code()
    if not store_code:
        flash("当前未绑定门店仓库")
        return redirect(url_for("store_ordering.catalog", dc=dc_code))
    cart = sop.get_or_create_cart(master, g.user["id"], store_code, dc_code)
    sop.add_cart_item(master, cart["id"], canonical_id, quantity, unit)
    flash("已加入购物车")
    return redirect(url_for("store_ordering.catalog", dc=dc_code))


@bp.route("/cart/add-batch", methods=["POST"])
@require_login
@_storefront_or_admin
def cart_add_batch() -> str:
    """Batch add items from the catalog modal.

    Form contract (catalog.html):
        selected[]: canonical_id (one per card)
        qty[]:      number (may be empty = skipped)
        unit[]:     'base' | 'aux' (default 'base')

    Items with empty qty are skipped. After processing, redirect to /cart.
    """
    dc_code = request.form.get("dc") or session.get("store_ordering_dc")
    if not dc_code:
        flash("请先选择配送中心")
        return redirect(url_for("store_ordering.catalog"))
    session["store_ordering_dc"] = dc_code

    selected_list = request.form.getlist("selected[]")
    qty_list = request.form.getlist("qty[]")
    unit_list = request.form.getlist("unit[]")
    if len(selected_list) != len(qty_list) or len(selected_list) != len(unit_list):
        flash("表单字段不一致，请重试")
        return redirect(url_for("store_ordering.catalog", dc=dc_code))

    store_code = _current_warehouse_code()
    if not store_code:
        flash("当前未绑定门店仓库")
        return redirect(url_for("store_ordering.catalog", dc=dc_code))

    master = get_master_db()
    cart = sop.get_or_create_cart(master, g.user["id"], store_code, dc_code)

    added = 0
    skipped = 0
    for cid_raw, qty_raw, unit_raw in zip(selected_list, qty_list, unit_list):
        qty = parse_qty(qty_raw)
        if qty <= 0:
            skipped += 1
            continue
        try:
            cid = int(cid_raw)
        except (TypeError, ValueError):
            skipped += 1
            continue
        unit = (unit_raw or "base").strip() or "base"
        sop.add_cart_item(master, cart["id"], cid, qty, unit)
        added += 1
    if added:
        flash(f"已加入购物车 {added} 项")
    else:
        flash("没有可加入购物车的品项（请先在卡片弹窗中填写数量）")
    return redirect(url_for("store_ordering.cart_view"))


@bp.route("/cart")
@require_login
@_storefront_or_admin
def cart_view() -> str:
    """Show cart."""
    dc_code = session.get("store_ordering_dc")
    store_code = _current_warehouse_code()
    if not dc_code or not store_code:
        flash("购物车为空")
        return redirect(url_for("store_ordering.catalog"))
    master = get_master_db()
    cart = sop.get_or_create_cart(master, g.user["id"], store_code, dc_code)
    items = sop.list_cart_items(master, cart["id"])
    cart_total = sum(parse_qty(i.get("line_subtotal") or 0) for i in items)
    return render_template(
        "store_ordering/cart.html",
        cart=cart,
        items=items,
        dc_code=dc_code,
        cart_total=cart_total,
    )


def _assert_cart_item_owner(master: sqlite3.Connection, cart_item_id: int) -> None:
    owner_id = sop.get_cart_item_user_id(master, cart_item_id)
    if owner_id is None:
        abort(404)
    if owner_id != g.user["id"]:
        abort(403)


@bp.route("/cart/update/<int:cart_item_id>", methods=["POST"])
@require_login
@_storefront_or_admin
def update_cart_item(cart_item_id: int) -> str:
    """Update cart item quantity."""
    quantity = parse_qty(request.form.get("quantity", "0"))
    master = get_master_db()
    _assert_cart_item_owner(master, cart_item_id)
    try:
        sop.update_cart_item_quantity(master, cart_item_id, quantity)
        flash("数量已更新")
    except ValueError as e:
        flash(str(e))
    return redirect(url_for("store_ordering.cart_view"))


@bp.route("/cart/remove/<int:cart_item_id>", methods=["POST"])
@require_login
@_storefront_or_admin
def remove_cart_item_route(cart_item_id: int) -> str:
    """Remove item from cart."""
    master = get_master_db()
    _assert_cart_item_owner(master, cart_item_id)
    sop.remove_cart_item(master, cart_item_id)
    flash("已删除")
    return redirect(url_for("store_ordering.cart_view"))


@bp.route("/cart/clear", methods=["POST"])
@require_login
@_storefront_or_admin
def clear_cart_route() -> str:
    """Clear cart."""
    dc_code = session.get("store_ordering_dc")
    store_code = _current_warehouse_code()
    if dc_code and store_code:
        master = get_master_db()
        cart = sop.get_or_create_cart(master, g.user["id"], store_code, dc_code)
        sop.clear_cart(master, cart["id"])
    flash("购物车已清空")
    return redirect(url_for("store_ordering.catalog"))


@bp.route("/cart/submit", methods=["GET", "POST"])
@require_login
@_storefront_or_admin
def submit_order_route() -> str:
    """Submit cart as order."""
    dc_code = session.get("store_ordering_dc")
    store_code = _current_warehouse_code()
    if not dc_code or not store_code:
        flash("请先选择配送中心并添加商品")
        return redirect(url_for("store_ordering.catalog"))

    master = get_master_db()
    cart = sop.get_or_create_cart(master, g.user["id"], store_code, dc_code)
    items = sop.list_cart_items(master, cart["id"])
    if not items:
        flash("购物车为空")
        return redirect(url_for("store_ordering.cart_view"))

    expected_date = _tomorrow()
    note = ""
    errors: dict[str, Any] = {"shortages": [], "unbound": []}

    if request.method == "POST":
        expected_date = _parse_date_input(request.form.get("expected_delivery_date")) or _tomorrow()
        note = (request.form.get("note") or "").strip()
        if expected_date < _today():
            flash("期望到货日期不能早于今天")
        else:
            validation = sop.validate_cart_for_submit(master, cart)
            if not validation["ok"]:
                errors = validation
                flash("提交失败，请检查库存与门店绑定")
            else:
                order = sop.submit_order(
                    master, cart["id"], g.user["id"],
                    _date_input(expected_date), note or None,
                )
                sop.notify_order_event(master, sop.EVENT_ORDER_SUBMITTED, order, g.user["id"])
                session.pop("store_ordering_dc", None)
                flash(f"订单 {order['order_no']} 提交成功")
                return redirect(url_for("store_ordering.order_detail", order_id=order["id"]))

    validation = sop.validate_cart_for_submit(master, cart)
    if not validation["ok"]:
        errors = validation

    return render_template(
        "store_ordering/submit.html",
        cart=cart,
        items=items,
        expected_date=_date_input(expected_date),
        note=note,
        errors=errors,
        today=_date_input(_today()),
    )


# ---------------------------------------------------------------------------
# Storefront orders
# ---------------------------------------------------------------------------

@bp.route("/orders")
@require_login
@_storefront_or_admin
def store_orders_list() -> str:
    """List orders for current store."""
    store_code = _current_warehouse_code()
    if not store_code:
        flash("当前未绑定门店仓库")
        return redirect(url_for("core.dashboard"))
    master = get_master_db()
    status = request.args.get("status", "").strip() or None
    start_date = request.args.get("start_date", "").strip() or None
    end_date = request.args.get("end_date", "").strip() or None
    orders = sop.list_orders(
        master,
        store_warehouse_code=store_code,
        status=status,
        start_date=start_date,
        end_date=end_date,
    )
    return render_template(
        "store_ordering/orders.html",
        orders=orders,
        status=status or "",
        start_date=start_date or "",
        end_date=end_date or "",
        is_admin_view=False,
    )


@bp.route("/orders/<int:order_id>")
@require_login
def order_detail(order_id: int) -> str:
    """Order detail view shared by store, DC and admin."""
    master = get_master_db()
    order = sop.get_order_detail(master, order_id)
    if order is None:
        abort(404)
    if not _order_viewable(order):
        abort(403)
    role = g.role["role"] if g.role else None
    wh_type = _current_warehouse_type()
    is_admin = _is_admin()
    return render_template(
        "store_ordering/order_detail.html",
        order=order,
        can_review=(
            wh_type == sop.WAREHOUSE_TYPE_DC
            and role in ("manager", "admin")
        ),
        can_ship=(
            wh_type == sop.WAREHOUSE_TYPE_DC
            and role in ("staff", "manager", "admin")
        ),
        can_deliver=(
            wh_type == "storefront"
            and role in ("manager", "admin")
        ),
        can_receive=(
            (wh_type == "storefront" and role in ("manager", "admin"))
            or is_admin
        ),
    )


# ---------------------------------------------------------------------------
# DC review / ship
# ---------------------------------------------------------------------------

@bp.route("/review")
@require_login
@require_warehouse_type("distribution_center")
@require_role("manager")
def review_list() -> str:
    """List pending orders for current DC."""
    master = get_master_db()
    dc_code = _current_warehouse_code()
    orders = sop.list_orders(master, dc_warehouse_code=dc_code, status=sop.ORDER_STATUS_PENDING)
    return render_template("store_ordering/review.html", orders=orders)


@bp.route("/orders/<int:order_id>/review", methods=["POST"])
@require_login
@require_warehouse_type("distribution_center")
@require_role("manager")
def review_order_route(order_id: int) -> str:
    """Approve or reject an order."""
    decision = request.form.get("decision", "").strip()
    note = (request.form.get("note") or "").strip()
    if decision not in (sop.ORDER_STATUS_APPROVED, sop.ORDER_STATUS_REJECTED):
        flash("请选择审批结论")
        return redirect(url_for("store_ordering.review_list"))
    if decision == sop.ORDER_STATUS_REJECTED and not note:
        flash("拒绝审批时必须填写原因")
        return redirect(url_for("store_ordering.review_list"))

    master = get_master_db()
    _assert_order_belongs_to_current_dc(master, order_id)
    try:
        order = sop.review_order(master, order_id, decision, g.user["id"], note or None)
    except ValueError as e:
        flash(str(e))
        return redirect(url_for("store_ordering.review_list"))

    event = (
        sop.EVENT_ORDER_APPROVED
        if decision == sop.ORDER_STATUS_APPROVED
        else sop.EVENT_ORDER_REJECTED
    )
    sop.notify_order_event(master, event, order, g.user["id"])
    flash("审批已处理")
    return redirect(url_for("store_ordering.review_list"))


@bp.route("/shipments")
@require_login
@require_warehouse_type("distribution_center")
@require_role("staff")
def shipment_list() -> str:
    """List approved orders ready to ship."""
    master = get_master_db()
    dc_code = _current_warehouse_code()
    orders = sop.list_orders(master, dc_warehouse_code=dc_code, status=sop.ORDER_STATUS_APPROVED)
    return render_template("store_ordering/shipments.html", orders=orders)


@bp.route("/orders/<int:order_id>/ship", methods=["POST"])
@require_login
@require_warehouse_type("distribution_center")
@require_role("staff")
def ship_order_route(order_id: int) -> str:
    """Ship an approved order."""
    tracking_note = (request.form.get("tracking_note") or "").strip() or None
    master = get_master_db()
    _assert_order_belongs_to_current_dc(master, order_id)
    try:
        order = sop.ship_order(master, order_id, g.user["id"], tracking_note)
    except ValueError as e:
        flash(f"出库失败：{e}")
        return redirect(url_for("store_ordering.shipment_list"))
    sop.notify_order_event(master, sop.EVENT_ORDER_SHIPPED, order, g.user["id"])
    flash("出库成功")
    return redirect(url_for("store_ordering.shipment_list"))


@bp.route("/orders/<int:order_id>/deliver", methods=["POST"])
@require_login
def deliver_order_route(order_id: int) -> str:
    """Mark order as delivered (store manager/admin)."""
    master = get_master_db()
    order = sop.get_order_detail(master, order_id)
    if order is None:
        abort(404)
    if not _order_viewable(order):
        abort(403)
    if _current_warehouse_type() != "storefront":
        flash("只有门店可确认收货")
        return redirect(url_for("store_ordering.order_detail", order_id=order_id))
    role = g.role["role"] if g.role else None
    if role not in ("manager", "admin") and not _is_admin():
        flash("需要经理及以上权限")
        return redirect(url_for("store_ordering.order_detail", order_id=order_id))
    try:
        order = sop.mark_order_delivered(master, order_id, g.user["id"])
    except ValueError as e:
        flash(str(e))
        return redirect(url_for("store_ordering.order_detail", order_id=order_id))
    sop.notify_order_event(master, sop.EVENT_ORDER_DELIVERED, order, g.user["id"])
    flash("已确认收货")
    return redirect(url_for("store_ordering.order_detail", order_id=order_id))


@bp.route("/orders/<int:order_id>/receive", methods=["POST"])
@require_login
@_storefront_or_admin
def receive_order_route(order_id: int) -> str:
    """Receive (partial or full) a single order_item into the storefront warehouse.

    Form fields:
      order_item_id: the store_order_items.id being received
      quantity:      base-unit quantity being received this batch
      note:          optional free-text note

    Permission: storefront manager/admin (or platform admin). DC users are
    redirected to review list by `_storefront_or_admin`.
    """
    master = get_master_db()
    order = sop.get_order_detail(master, order_id)
    if order is None:
        abort(404)
    if not _order_viewable(order):
        abort(403)
    if _current_warehouse_type() != "storefront":
        flash("只有门店可以收货")
        return redirect(url_for("store_ordering.order_detail", order_id=order_id))
    role = g.role["role"] if g.role else None
    if role not in ("manager", "admin") and not _is_admin():
        flash("需要经理及以上权限")
        return redirect(url_for("store_ordering.order_detail", order_id=order_id))

    order_item_id = request.form.get("order_item_id", type=int)
    quantity = parse_qty(request.form.get("quantity", "0"))
    note = (request.form.get("note") or "").strip() or None

    if order_item_id is None or quantity <= 0:
        flash("请填写有效的收货数量")
        return redirect(url_for("store_ordering.order_detail", order_id=order_id))

    try:
        result = sop.receive_order_item(
            master,
            order_id=order_id,
            order_item_id=order_item_id,
            qty=quantity,
            actor_id=g.user["id"],
            note=note,
        )
    except ValueError as e:
        flash(str(e))
        return redirect(url_for("store_ordering.order_detail", order_id=order_id))

    if result["is_fully_received"]:
        # Notify only when the whole order is fulfilled (matches v2 §9.3).
        refreshed = sop.get_order_detail(master, order_id)
        if refreshed is not None:
            sop.notify_order_event(
                master, sop.EVENT_ORDER_DELIVERED, refreshed, g.user["id"]
            )
        flash("已收齐该订单")
    else:
        flash(f"已收货 {quantity:g} 件")
    return redirect(url_for("store_ordering.order_detail", order_id=order_id))


# ---------------------------------------------------------------------------
# Admin board
# ---------------------------------------------------------------------------

@bp.route("/admin/orders")
@require_login
@require_role("admin")
def admin_orders() -> str:
    """Platform admin order board."""
    master = get_master_db()
    status = request.args.get("status", "").strip() or None
    start_date = request.args.get("start_date", "").strip() or None
    end_date = request.args.get("end_date", "").strip() or None
    orders = sop.list_orders(
        master,
        status=status,
        start_date=start_date,
        end_date=end_date,
    )
    return render_template(
        "store_ordering/orders.html",
        orders=orders,
        status=status or "",
        start_date=start_date or "",
        end_date=end_date or "",
        is_admin_view=True,
    )
