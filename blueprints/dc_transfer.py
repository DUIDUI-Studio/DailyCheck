"""总仓调货 blueprint：总仓发起 → 分仓接收（复用订货流程的单据与收发实现）.

路由刻意保持精简：
  - 列表（总仓看发送单 / 分仓看接收单）
  - 建单（总仓 manager 及以上）
  - 发货、收货、取消、详情全部复用 ``store_ordering`` 的既有路由 ——
    调货单就是 ``flow_type='dc_transfer'`` 的订单，不需要第二套收发实现。

Spec: docs/2026-10-19-dc-transfer-design.md
"""
from __future__ import annotations

import sqlite3
from typing import Any

from flask import (
    Blueprint,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

from blueprints import dc_transfer_pure as dtp
from blueprints import store_ordering_pure as sop
from blueprints._helpers import parse_qty
from db import get_master_db
from permissions import (
    has_permission,
    require_login,
    require_permission,
    require_warehouse_type,
)

bp = Blueprint("dc_transfer", __name__, url_prefix="/dc-transfer")

DC = sop.WAREHOUSE_TYPE_DC
STOREFRONT = dtp.WAREHOUSE_TYPE_STOREFRONT


def _current_warehouse() -> Any:
    return g.get("warehouse")


def _list_stores(master: sqlite3.Connection) -> list[dict[str, Any]]:
    master.row_factory = sqlite3.Row
    rows = master.execute(
        "SELECT code, name FROM warehouses WHERE warehouse_type=? ORDER BY code",
        (STOREFRONT,),
    ).fetchall()
    return [dict(r) for r in rows]


@bp.route("/")
@require_login
@require_warehouse_type(DC, STOREFRONT)
def transfer_list() -> str:
    """总仓：发送单列表。

    门店不设独立入口：分仓收到的调货单对门店就是一张待收货的订单，
    统一在「本店订单」里查看与接收（2026-10-19 owner 明确），因此门店
    访问本地址直接回到订单列表。
    """
    wh = _current_warehouse()
    if wh is None:
        flash("请先选择仓库")
        return redirect(url_for("auth.warehouse_picker"))

    if wh["warehouse_type"] != DC:
        return redirect(url_for("store_ordering.store_orders_list"))

    master = get_master_db()
    transfers = dtp.list_transfers(master, from_warehouse_code=wh["code"])

    return render_template(
        "store_ordering/transfers.html",
        transfers=transfers,
        # 建单入口只给持 transfer.create 的角色（DC manager/admin）；
        # staff 能看列表但看不到按钮，避免点进去吃 403。
        can_create=has_permission("transfer.create"),
    )


@bp.route("/new", methods=["GET", "POST"])
@require_login
@require_warehouse_type(DC)
@require_permission("transfer.create")
def transfer_new() -> str:
    """总仓建调货单：选分仓 + 勾品项数量，提交即 approved（免审批）。"""
    wh = _current_warehouse()
    if wh is None:
        flash("请先选择配送中心仓库")
        return redirect(url_for("auth.warehouse_picker"))
    dc_code = wh["code"]

    master = get_master_db()

    if request.method == "POST":
        to_code = (request.form.get("to_warehouse_code") or "").strip()
        note = (request.form.get("note") or "").strip() or None

        items: list[dict[str, Any]] = []
        for key, value in request.form.items():
            if not (key.startswith("items[") and key.endswith("]")):
                continue
            try:
                canonical_id = int(key[len("items["):-1])
                qty = parse_qty(value)
            except (TypeError, ValueError):
                continue
            if qty > 0:
                items.append({"canonical_id": canonical_id, "quantity": qty})

        if not to_code:
            flash("请选择收货分仓")
            return redirect(url_for("dc_transfer.transfer_new"))
        if not items:
            flash("请至少填写一项调货数量")
            return redirect(url_for("dc_transfer.transfer_new"))

        try:
            order = dtp.create_transfer_order(
                master,
                from_warehouse_code=dc_code,
                to_warehouse_code=to_code,
                items=items,
                actor_id=int(g.user["id"]),
                note=note,
            )
        except ValueError as e:
            flash(str(e))
            return redirect(url_for("dc_transfer.transfer_new"))

        # 通知分仓经理/管理员准备接收。走与订货同一事件通道，文案区分。
        sop.notify_order_event(
            master, sop.EVENT_TRANSFER_CREATED, order, g.user["id"]
        )
        flash(f"调货单 {order['order_no']} 已创建，请安排发货")
        return redirect(
            url_for("store_ordering.order_detail", order_id=order["id"])
        )

    items = dtp.list_transferable_items(master, dc_code)
    groups: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        groups.setdefault(it.get("category_code") or "未分类", []).append(it)

    return render_template(
        "store_ordering/transfer_new.html",
        dc_code=dc_code,
        stores=_list_stores(master),
        groups=groups,
        item_count=len(items),
    )
