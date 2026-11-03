"""配送中心外仓信息：Cookie 鉴权、仓库范围只读查询、稳定分页及单据详情。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from flask import Blueprint, make_response, render_template, request, url_for

from mcp_server.infra.errors import McpError
from mcp_server.service import wms, wms_web
from permissions import distribution_center_only, require_login

bp = Blueprint("wms", __name__)

_ERROR_MESSAGES = {
    "wms_not_configured": "外仓连接尚未配置，请联系管理员。",
    "wms_driver_missing": "外仓查询组件尚未安装，请联系管理员。",
    "wms_unavailable": "暂时无法连接外仓，请稍后重试。",
    "wms_query_failed": "外仓查询未成功，请稍后重试。",
    "wms_scope_unconfirmed": "外仓查询范围尚未确认，请联系管理员。",
    "wms_scope_unavailable": "授权外仓暂不可用，请联系管理员。",
    "validation_error": "所选仓库或货主不在当前可查询范围，请重新选择。",
}


@dataclass(frozen=True)
class Filters:
    view: str
    warehouse_id: int | None
    owner_id: int | None
    goods_code: str
    date_from: str
    limit: int
    page: int

    @classmethod
    def parse(cls, default_view="stock"):
        def positive(name, default=None, maximum=2147483647):
            value = request.args.get(name, "")
            if not value:
                return default
            if not value.isascii() or not value.isdecimal():
                raise ValueError("invalid integer filter")
            number = int(value)
            if not 1 <= number <= maximum:
                raise ValueError("integer filter outside range")
            return number

        view = request.args.get("view", default_view)
        since = request.args.get("date_from", "").strip()
        if view not in ("stock", "in", "out"):
            raise ValueError("invalid view")
        if since and date.fromisoformat(since).isoformat() != since:
            raise ValueError("invalid date")
        return cls(view, positive("warehouse"), positive("owner"),
                   request.args.get("goods_code", "").strip(), since,
                   positive("limit", 100, wms.MAX_LIMIT), positive("page", 1))

    def args(self, **changes):
        args = dict(view=self.view, warehouse=self.warehouse_id or "", owner=self.owner_id or "",
                    goods_code=self.goods_code, date_from=self.date_from, limit=self.limit, page=self.page)
        return args | changes


def _context(filters):
    cfg = wms.load_config()
    warehouses = wms_web.list_warehouses(cfg)
    scope = wms_web.resolve_scope(filters.warehouse_id, warehouses, cfg)
    owners = wms_web.list_owners(scope, cfg)
    if filters.owner_id is not None and filters.owner_id not in {int(row["owner_id"]) for row in owners}:
        raise McpError("validation_error", "owner outside warehouse scope", 400)
    return cfg, warehouses, scope, owners


def _safe_error(exc):
    code = exc.code if exc.code in _ERROR_MESSAGES else "wms_query_failed"
    return code, _ERROR_MESSAGES[code], 400 if code == "validation_error" else 502 if code == "wms_query_failed" else 503


def _scope_label(scope, warehouses):
    names = "、".join(row["warehouse"] or f"仓库 #{row['warehouse_id']}" for row in warehouses
                     if int(row["warehouse_id"]) in scope.warehouse_ids)
    return names + (" · 全部货主可筛选" if scope.customer_id is None else " · 仅配置货主")


@bp.after_request
def no_store(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.route("/wms", methods=["GET"])
@require_login
@distribution_center_only
def index():
    filters = Filters("stock", None, None, "", "", 100, 1)
    error = error_code = checked_at = None
    status = 200
    warehouses, owners = [], []
    page = None
    scope_label = "朴宇冷库、朴宇仓"
    try:
        filters = Filters.parse()
        cfg, warehouses, scope, owners = _context(filters)
        scope_label = _scope_label(scope, warehouses)
        if filters.view == "stock":
            page = wms_web.inventory_page(scope, cfg, owner_id=filters.owner_id,
                                          goods_code=filters.goods_code, number=filters.page, size=filters.limit)
        else:
            page = wms_web.documents_page(scope, cfg, wms_web.Direction(filters.view),
                                          owner_id=filters.owner_id, date_from=filters.date_from,
                                          number=filters.page, size=filters.limit)
        checked_at = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        error = "查询条件无效：请选择有效视图、正整数仓库/货主/页码、1–500 条每页和 YYYY-MM-DD 日期。"
        status = 400
    except McpError as exc:
        error_code, error, status = _safe_error(exc)
        # The dashboard remains a reachable navigation page during upstream outages.
        if status >= 500:
            status = 200

    return make_response(render_template(
        "wms.html", title="外仓信息", filters=filters, warehouses=warehouses, owners=owners,
        page=page, scope_label=scope_label, error=error, error_code=error_code, checked_at=checked_at,
    ), status)


@bp.route("/wms/documents/<direction>/<document_id>", methods=["GET"])
@require_login
@distribution_center_only
def detail(direction, document_id):
    filters = Filters(direction, None, None, "", "", 100, 1)
    detail = None
    error = error_code = None
    status = 200
    scope_label = "朴宇冷库、朴宇仓"
    try:
        key = wms_web.DocumentKey(wms_web.Direction(direction), int(document_id))
        filters = Filters.parse(default_view=direction)
        cfg, warehouses, scope, _ = _context(filters)
        scope_label = _scope_label(scope, warehouses)
        detail = wms_web.document_detail(scope, cfg, key)
        if detail is None:
            error, status = "单据不存在或不在当前可查询范围。", 404
    except ValueError:
        error, status = "单据标识或查询条件无效。", 400
    except McpError as exc:
        error_code, error, status = _safe_error(exc)
    return make_response(render_template(
        "wms_detail.html", title="外仓单据详情", detail=detail, direction=direction,
        scope_label=scope_label, error=error, error_code=error_code,
        back_url=url_for("wms.index", **filters.args()),
    ), status)
