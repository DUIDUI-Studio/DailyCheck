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


def _list_stores(master_conn: sqlite3.Connection) -> list[dict[str, Any]]:
    master_conn.row_factory = sqlite3.Row
    rows = master_conn.execute(
        "SELECT code, name FROM warehouses WHERE warehouse_type=? ORDER BY code",
        ("storefront",),
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
    # DC users and admins in a DC warehouse go straight to the review queue.
    if _current_warehouse_type() == sop.WAREHOUSE_TYPE_DC:
        return redirect(url_for("store_ordering.review_list"))
    if _current_warehouse_type() != "storefront":
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
    items = sop.list_available_dc_items(
        master, dc_code, category_code, keyword,
        store_warehouse_code=store_code or None,
    )
    # P1-5: 订货量建议（仅当前门店有真实仓库时才计算，admin 无门店跳过）
    suggestions: dict[int, int] = {}
    if store_code and not _is_admin():
        try:
            from db import get_warehouse_db
            store_conn = get_warehouse_db()
            suggestions = sop.compute_suggested_order_qty(
                store_conn, master, store_code,
            )
        except Exception:
            suggestions = {}
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
        suggestions=suggestions,
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
    for cid_raw, qty_raw, unit_raw in zip(selected_list, qty_list, unit_list, strict=False):
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
    cart_total = sum(parse_qty(it.get("line_subtotal") or 0) for it in items)
    if not items:
        flash("购物车为空")
        return redirect(url_for("store_ordering.cart_view"))

    # v3.1: 校验购物车里品项在 DC 端仍可订（防止「先加购后被下架」漏提交）
    cart_canonicals = [int(it["canonical_id"]) for it in items if it.get("canonical_id")]
    unorderable = sop.list_unorderable_cart_canonicals(master, dc_code, cart_canonicals)

    expected_date = _tomorrow()
    note = ""
    errors: dict[str, Any] = {"shortages": [], "unbound": [], "unorderable": unorderable}

    if request.method == "POST":
        expected_date = _parse_date_input(request.form.get("expected_delivery_date")) or _tomorrow()
        note = (request.form.get("note") or "").strip()
        if expected_date < _today():
            flash("期望到货日期不能早于今天")
        elif unorderable:
            flash("以下品项已被下架，请移除后再提交：" + "、".join(
                u["canonical_name"] for u in unorderable
            ))
        else:
            validation = sop.validate_cart_for_submit(master, cart)
            if not validation["ok"]:
                errors = validation
                flash("提交失败，请检查门店绑定")
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
        cart_total=cart_total,
        shipping_fee=sop.compute_shipping_fee(
            cart_total, sop.get_active_shipping_rule(master),
        ),
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
    """Order detail view shared by store, DC and admin.

    v3 A4: 计算订单总金额 Σ(quantity × unit_price)。
    v3 A6: 收集 DC 仓库当前库存（含允许欠货出库后的负值）。
    """
    master = get_master_db()
    order = sop.get_order_detail(master, order_id)
    if order is None:
        abort(404)
    if not _order_viewable(order):
        abort(403)
    role = g.role["role"] if g.role else None
    wh_type = _current_warehouse_type()
    is_admin = _is_admin()

    # v3 A6: 收集 DC 仓当前库存（每个明细：name / dc_available / unit_price /
    # category_name）。同时把 unit_price / dc_available 写回 item，模板直接
    # 用 item.unit_price / item.dc_available 取值，避免模板侧再二次查询。
    dc_items_info = sop.get_dc_items_for_order_detail(master, order_id)
    for item in order["order_items"]:
        info = dc_items_info.get(int(item["id"]), {})
        item["unit_price"] = info.get("unit_price", 0.0)
        item["dc_available"] = info.get("dc_available", 0.0)

    # v3 A4: 订单总金额 = Σ(item.quantity × item.unit_price)，2dp 量化。
    total_amount = sum(
        parse_qty(item["quantity"]) * parse_qty(item.get("unit_price") or 0)
        for item in order["order_items"]
    )
    # v3.1: 订单含 shipping_fee（submit 时锁定）。order dict 已含 shipping_fee 字段。
    if "shipping_fee" not in order:
        order["shipping_fee"] = 0.0

    is_dc_view = wh_type == sop.WAREHOUSE_TYPE_DC

    # Store-side inflow log: stock_movements with action='门店订货入库' for the
    # order's store warehouse, joined to canonical_items for the name.
    # Surfaced only on the storefront view so store users can see the inbound
    # inventory moves triggered by their receiving.
    store_inflows: list[dict[str, Any]] = []
    if not is_dc_view:
        try:
            wh_db = sop.open_warehouse_db(order["store_warehouse_code"])
            wh_db.row_factory = sqlite3.Row
            for r in wh_db.execute(
                """SELECT sm.created_at, sm.action, sm.delta, sm.note,
                          i.unit, i.name AS local_name, i.canonical_id
                   FROM stock_movements sm
                   JOIN items i ON i.id = sm.item_id
                   WHERE sm.action = ?
                     AND sm.note LIKE ?
                   ORDER BY sm.id DESC LIMIT 50""",
                (sop.RECEIPT_ACTION, f"%{order['order_no']}%"),
            ).fetchall():
                d = dict(r)
                d["delta"] = parse_qty(d["delta"])
                # Hydrate canonical_name from master.db; fall back to local
                # name if the canonical link is missing or canonical item is
                # not found.
                cid = d.pop("canonical_id")
                local_name = d.pop("local_name")
                if cid is not None:
                    cn = master.execute(
                        "SELECT name FROM canonical_items WHERE id=?",
                        (cid,),
                    ).fetchone()
                    if cn:
                        d["canonical_name"] = cn["name"]
                        store_inflows.append(d)
                        continue
                d["canonical_name"] = local_name
                store_inflows.append(d)
            wh_db.close()
        except Exception:
            store_inflows = []

    return render_template(
        "store_ordering/order_detail.html",
        order=order,
        total_amount=total_amount,
        dc_items_info=dc_items_info,
        is_dc_view=is_dc_view,
        store_inflows=store_inflows,
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
@require_role("staff")
def review_list() -> str:
    """Combined DC dashboard: pending (待审批) + approved (待出库) orders.

    Both sections render in a single page so the operator only needs to look
    in one place. Outbound staff can reach `/orders/<id>/ship` from the
    待出库 table; managers use the inline approve/reject forms on the
    待审批 table.
    """
    master = get_master_db()
    dc_code = _current_warehouse_code()
    pending = sop.list_orders(
        master, dc_warehouse_code=dc_code, status=sop.ORDER_STATUS_PENDING
    )
    approved = sop.list_orders(
        master, dc_warehouse_code=dc_code, status=sop.ORDER_STATUS_APPROVED
    )
    return render_template(
        "store_ordering/review.html",
        pending_orders=pending,
        approved_orders=approved,
    )


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
    """Legacy alias. The combined /review page is the canonical entry; this
    just redirects there for any bookmarked deep links."""
    return redirect(url_for("store_ordering.review_list"))


@bp.route("/dc/items", methods=["GET", "POST"])
@require_login
@require_warehouse_type("distribution_center")
@require_role("manager")
def manage_dc_items() -> str:
    """v3.1: DC 品项可订开关管理（DC manager + admin）。

    GET  : 列当前 DC 所有品项（按品类分组），每行带可订 toggle。
    POST : 切换单个品项的 is_orderable。
    """
    dc_code = _current_warehouse_code()
    if dc_code is None and _is_admin():
        # admin 没绑仓库时不绑，这里只允许具体仓库视角
        flash("请先选择配送中心仓库")
        return redirect(url_for("core.dashboard"))

    master = get_master_db()
    if request.method == "POST":
        canonical_id_raw = request.form.get("canonical_id", "")
        is_orderable_raw = request.form.get("is_orderable", "1")
        try:
            cid = int(canonical_id_raw)
            is_orderable = is_orderable_raw == "1"
        except ValueError:
            flash("请求参数无效")
            return redirect(url_for("store_ordering.manage_dc_items"))
        try:
            sop.set_dc_item_orderable(master, dc_code, cid, is_orderable)
        except ValueError as e:
            flash(str(e))
        else:
            flash("已切换为" + ("可订" if is_orderable else "不可订"))
        return redirect(url_for("store_ordering.manage_dc_items"))

    items = sop.list_dc_items_for_management(master, dc_code)
    # 按 category_code 分组
    groups: dict[str, list[dict]] = {}
    for it in items:
        groups.setdefault(it.get("category_code") or "未分类", []).append(it)
    return render_template(
        "store_ordering/dc_items.html",
        dc_code=dc_code,
        groups=groups,
    )


@bp.route("/orders/<int:order_id>/cancel", methods=["POST"])
@require_login
def cancel_order_route(order_id: int) -> str:
    """Cancel a pending or approved order (P1-4).

    Permission matrix (PRD §5 + §P1-4):
      - pending: 下单人本人（order.requested_by == g.user.id），或平台管理员
      - approved: 当前 DC 的 manager/admin，或平台管理员
      - 其他状态（shipped/delivered/cancelled/rejected）：不可取消
    """
    master = get_master_db()
    order = sop.get_order_detail(master, order_id)
    if order is None:
        abort(404)
    if not _order_viewable(order):
        abort(403)

    reason = (request.form.get("reason") or "").strip()
    if not reason:
        flash("取消订单时必须填写原因")
        return redirect(url_for("store_ordering.order_detail", order_id=order_id))

    status = order["status"]
    actor_id = int(g.user["id"])
    is_requestor = int(order["requested_by"]) == actor_id
    wh_type = _current_warehouse_type()
    role = g.role["role"] if g.role else None

    if status == sop.ORDER_STATUS_PENDING:
        if not (is_requestor or _is_admin()):
            flash("只有下单人或管理员可以取消待审批订单")
            return redirect(url_for("store_ordering.order_detail", order_id=order_id))
    elif status == sop.ORDER_STATUS_APPROVED:
        # DC 已审批：必须当前 DC 的 manager/admin 或平台管理员
        if not _is_admin():
            if wh_type != sop.WAREHOUSE_TYPE_DC or role not in ("manager", "admin"):
                flash("审批后只能由配送中心经理以上或管理员取消")
                return redirect(url_for("store_ordering.order_detail", order_id=order_id))
            if order["dc_warehouse_code"] != _current_warehouse_code():
                abort(403)
    else:
        flash(f"订单当前状态 {status} 不可取消")
        return redirect(url_for("store_ordering.order_detail", order_id=order_id))

    try:
        order = sop.cancel_order(master, order_id, actor_id, reason)
    except ValueError as e:
        flash(str(e))
        return redirect(url_for("store_ordering.order_detail", order_id=order_id))

    sop.notify_order_event(master, sop.EVENT_ORDER_CANCELLED, order, actor_id)
    flash("订单已取消")
    return redirect(url_for("store_ordering.order_detail", order_id=order_id))


@bp.route("/orders/<int:order_id>/ship", methods=["POST"])
@require_login
@require_warehouse_type("distribution_center")
@require_role("staff")
def ship_order_route(order_id: int) -> str:
    """DC 部分/全部发货（v3）。

    表单字段约定：
      - ``shipped_items[N]=qty``：多值，每行一个明细的本批发货数。N 是
        ``store_order_items.id``，与 order_detail.html 中的 input name 配对。
        新 UI 的每个数量 input 都带该 name（即使留空也会随表单提交），因此
        「字段存在」是「新 UI 提交」的可靠信号。
      - ``qty``：兼容 v1/v2 的「全量一次发货」字段；填了就给所有明细
        各发 qty 件（仍受累计上限校验）。
      - 上述两者都缺省（且表单不含任何 ``shipped_items[`` 字段）：走
        ``ship_order_full``（legacy full-ship），把每个明细的剩余可发数量
        一次性发齐。仅用于无 shipped_items 字段的旧客户端 / 老测试。

    ⚠️ 安全护栏（2026-10-10）：一旦表单携带 ``shipped_items[`` 字段但全部
    为空/0，视为「用户未填写」→ 直接 flash 拒绝，**绝不**退化为整单全发。
    这是「DC 只发 2、却被记发 100」缺陷的直接堵口。

    通知：仅在订单全部发齐后写一条 ``store_order_shipped``；partial 不发
    通知避免刷屏（v3 设计 §10.4）。
    """
    tracking_note = (request.form.get("tracking_note") or "").strip() or None
    master = get_master_db()
    _assert_order_belongs_to_current_dc(master, order_id)

    # 1. 解析 shipped_items[N] = qty 形式（v3 多值表单）。
    shipped_items_map: dict[int, float] = {}
    has_multival_fields = False
    for key, value in request.form.items():
        if key.startswith("shipped_items[") and key.endswith("]"):
            has_multival_fields = True
            try:
                oid = int(key[len("shipped_items["):-1])
                qty = parse_qty(value)
                if qty > 0:
                    shipped_items_map[oid] = qty
            except (ValueError, TypeError):
                continue

    # 1b. 新 UI 提交了 shipped_items[] 字段但全部为空 → 拒绝，不猜、不全发。
    if not shipped_items_map and has_multival_fields:
        flash("请至少填写一项发货数量")
        return redirect(url_for("store_ordering.order_detail", order_id=order_id))

    # 2. 兼容 v2 全量字段 'qty'：所有未发齐明细都发 qty。
    if not shipped_items_map:
        raw_qty = request.form.get("qty")
        if raw_qty is not None and raw_qty != "":
            qty_value = parse_qty(raw_qty)
            if qty_value > 0:
                detail = sop.get_order_detail(master, order_id)
                if detail is not None:
                    for it in detail["order_items"]:
                        already = parse_qty(it.get("shipped_quantity") or 0)
                        remaining = parse_qty(it["quantity"]) - already
                        if remaining > 0:
                            shipped_items_map[int(it["id"])] = min(
                                qty_value, remaining
                            )

    # 3. 都没填（且不含 shipped_items 字段）→ 走 legacy full-ship（旧客户端兼容）。
    used_full_ship_fallback = False
    if not shipped_items_map:
        try:
            order = sop.ship_order_full(
                master, order_id, g.user["id"], tracking_note
            )
            used_full_ship_fallback = True
        except ValueError as e:
            flash(f"出库失败：{e}")
            return redirect(url_for("store_ordering.shipment_list"))
    else:
        try:
            order = sop.ship_order(
                master, order_id, shipped_items_map,
                shipped_by=g.user["id"], tracking_note=tracking_note,
            )
        except ValueError as e:
            flash(f"出库失败：{e}")
            return redirect(url_for("store_ordering.order_detail", order_id=order_id))

    # 4. 通知：仅全部发齐才触发 store_order_shipped 通知（v3 §10.4）。
    is_fully_shipped = bool(order.get("is_fully_shipped")) or (
        order.get("status") == sop.ORDER_STATUS_SHIPPED and used_full_ship_fallback
    )
    if is_fully_shipped:
        sop.notify_order_event(
            master, sop.EVENT_ORDER_SHIPPED, order, g.user["id"]
        )

    # 5. flash 文案：全部发齐 → "出库成功"；部分 → "本批发货 X 件"。
    if is_fully_shipped:
        flash("出库成功")
    else:
        shipped_total = sum(
            parse_qty(it.get("shipped_quantity") or 0)
            for it in order.get("order_items", [])
        )
        flash(f"本批发货 {shipped_total:g} 件（订单仍可继续部分发货）")

    return redirect(url_for("store_ordering.order_detail", order_id=order_id))


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
        # 部分收货：通知 DC 审批人 + 平台管理员，避免门店店长漏感知到货进度
        refreshed = sop.get_order_detail(master, order_id)
        if refreshed is not None:
            sop.notify_order_event(
                master, sop.EVENT_ORDER_RECEIVED, refreshed, g.user["id"]
            )
        flash(f"已收货 {quantity:g} 件")
    return redirect(url_for("store_ordering.order_detail", order_id=order_id))


# ---------------------------------------------------------------------------
# Admin board
# ---------------------------------------------------------------------------

@bp.route("/admin/shipping", methods=["GET", "POST"])
@require_login
@require_role("admin")
def manage_shipping_rule() -> str:
    """v3.1: admin 配置运费规则。

    GET  : 显示当前 active 规则的表单（含示例计算）
    POST : 调 upsert_shipping_rule
    """
    master = get_master_db()
    rule = sop.get_active_shipping_rule(master)

    if request.method == "POST":
        try:
            base_fee = float(request.form.get("base_fee", "0") or 0)
            pct_fee = float(request.form.get("pct_fee", "0") or 0) / 100.0
            active = request.form.get("active", "1") == "1"
        except ValueError:
            flash("运费规则参数无效")
            return redirect(url_for("store_ordering.manage_shipping_rule"))

        sop.upsert_shipping_rule(
            master,
            base_fee=base_fee,
            pct_fee=pct_fee,
            active=active,
        )
        flash("运费规则已保存")
        return redirect(url_for("store_ordering.manage_shipping_rule"))

    # 表单展示：用 100 作为示例订单金额
    sample_subtotal = 100.0
    sample_fee = sop.compute_shipping_fee(sample_subtotal, rule)

    return render_template(
        "store_ordering/shipping.html",
        rule=rule or {},
        sample_fee=sample_fee,
        sample_subtotal=sample_subtotal,
    )


@bp.route("/admin/report")
@require_login
@require_role("admin")
def admin_report() -> str:
    """Platform admin 报表 (P2-4)。

    维度：门店 / DC / 品类 / 品项；指标：订货量 / 出货量 / 收货量 / 欠收量 / 欠收率。
    筛选：日期范围、门店、DC、品类。
    """
    master = get_master_db()
    start_date = request.args.get("start_date", "").strip() or None
    end_date = request.args.get("end_date", "").strip() or None
    store = request.args.get("store", "").strip() or None
    dc = request.args.get("dc", "").strip() or None
    category_code = request.args.get("cat", "").strip() or None

    report = sop.compute_store_order_report(
        master,
        start_date=start_date,
        end_date=end_date,
        store_warehouse_code=store,
        dc_warehouse_code=dc,
        category_code=category_code,
    )

    # 拉品类下拉选项
    cat_rows = master.execute(
        "SELECT code, name FROM canonical_categories ORDER BY code"
    ).fetchall()
    categories = [(r["code"], r["name"]) for r in cat_rows]

    return render_template(
        "store_ordering/report.html",
        report=report,
        start_date=start_date or "",
        end_date=end_date or "",
        store=store or "",
        dc=dc or "",
        category_code=category_code or "",
        categories=categories,
        stores=_list_stores(master),
        dcs=_list_dcs(master),
    )


@bp.route("/admin/orders")
@require_login
@require_role("admin")
def admin_orders() -> str:
    """Platform admin order board."""
    master = get_master_db()
    status = request.args.get("status", "").strip() or None
    store = request.args.get("store", "").strip() or None
    dc = request.args.get("dc", "").strip() or None
    start_date = request.args.get("start_date", "").strip() or None
    end_date = request.args.get("end_date", "").strip() or None
    orders = sop.list_orders(
        master,
        store_warehouse_code=store,
        dc_warehouse_code=dc,
        status=status,
        start_date=start_date,
        end_date=end_date,
    )
    return render_template(
        "store_ordering/orders.html",
        orders=orders,
        status=status or "",
        store=store or "",
        dc=dc or "",
        start_date=start_date or "",
        end_date=end_date or "",
        stores=_list_stores(master),
        dcs=_list_dcs(master),
        is_admin_view=True,
    )
