"""配送中心外仓信息：Cookie 会话鉴权，复用 WMS SELECT 服务，无写入入口。"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from flask import Blueprint, make_response, render_template, request

from mcp_server.infra.errors import McpError
from mcp_server.service import wms
from permissions import distribution_center_only, require_login

bp = Blueprint("wms", __name__)

_ERROR_MESSAGES = {
    "wms_not_configured": "外仓连接尚未配置，请联系管理员。",
    "wms_driver_missing": "外仓查询组件尚未安装，请联系管理员。",
    "wms_unavailable": "暂时无法连接外仓，请稍后重试。",
    "wms_query_failed": "外仓查询未成功，请稍后重试。",
}


@bp.after_request
def no_store(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.route("/wms", methods=["GET"])
@require_login
@distribution_center_only
def index():
    view = request.args.get("view", "stock")
    warehouse = request.args.get("warehouse", "").strip()
    goods_code = request.args.get("goods_code", "").strip()
    date_from = request.args.get("date_from", "").strip()
    limit = 100
    error = None
    error_code = None
    status = 200
    warehouses, summary, rows = [], [], []
    checked_at = None

    try:
        limit = int(request.args.get("limit", "100"))
        if view not in ("stock", "in", "out") or not 1 <= limit <= wms.MAX_LIMIT:
            raise ValueError
        if date_from:
            parsed = date.fromisoformat(date_from)
            if parsed.isoformat() != date_from:
                raise ValueError
    except ValueError:
        error = "查询条件无效：请选择库存、入库或出库，条数须为 1–500，日期须为 YYYY-MM-DD。"
        status = 400

    if not error:
        try:
            warehouses = wms.list_warehouses()
            if warehouse and warehouse not in {row["warehouse"] for row in warehouses}:
                error = "所选外仓不在当前可查询范围，请重新选择。"
                status = 400
            elif view == "stock":
                summary = wms.summarize_stock(warehouse=warehouse or None)
                rows = wms.list_stock(warehouse=warehouse or None, goods_code=goods_code or None, limit=limit)
            else:
                rows = wms.list_documents(direction=view, warehouse=warehouse or None, date_from=date_from or None, limit=limit)
            if not error:
                checked_at = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        except McpError as exc:
            error_code = exc.code if exc.code in _ERROR_MESSAGES else "wms_query_failed"
            error = _ERROR_MESSAGES[error_code]
            # 不展示半次查询结果，避免误当成完整库存。
            summary, rows = [], []

    return make_response(render_template(
        "wms.html", title="外仓信息", view=view, warehouse=warehouse,
        goods_code=goods_code, date_from=date_from, limit=limit,
        warehouses=warehouses, summary=summary, rows=rows,
        error=error, error_code=error_code, checked_at=checked_at,
    ), status)
