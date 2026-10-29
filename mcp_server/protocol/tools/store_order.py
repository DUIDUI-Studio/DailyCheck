"""门店订单 MCP 工具（只读）。

两个工具都只读 master.db 的门店订单五张表，不写、不删、不改 schema。

路径级 ACL（由 token 的 allowed_read_paths 控制）：
  - ``GET /api/v1/store-orders``      —— 列表
  - ``GET /api/v1/store-orders/<id>`` —— 详情

仓库级 ACL 落在服务层（``service/store_order.py``）：订单的
``store_warehouse_code`` 或 ``dc_warehouse_code`` 任一命中 token 的
``allowed_warehouses`` 即可见。
"""
from __future__ import annotations

from mcp_server.infra.errors import ForbiddenError, ValidationError
from mcp_server.service.auth import check_path, resolve_ctx
from mcp_server.service.store_order import (
    DEFAULT_LIMIT,
)
from mcp_server.service.store_order import (
    get_store_order_detail as svc_get_store_order_detail,
)
from mcp_server.service.store_order import (
    list_store_orders as svc_list_store_orders,
)

LIST_PATH = "/api/v1/store-orders"
DETAIL_PATH = "/api/v1/store-orders/<id>"


def _ctx():
    """Return the AuthContext for this call.

    Request-scoped when the token arrived over HTTP (published by
    AuthMiddleware); falls back to the process env token for stdio.
    """
    return resolve_ctx()


def _check_read_path(ctx, path: str) -> bool:
    """读路径 ACL：命中精确路径，或命中该路径的通配子树。

    token 的 ``allowed_read_paths`` 里同一路径有三种自然写法：
    ``/api/v1/store-orders``（精确）、``/api/v1/store-orders/*``（子树通配）、
    ``*``（全放行）。``path_matches`` 的前缀语义下，``/api/v1/store-orders/*``
    并不匹配列表路径本身 ``/api/v1/store-orders``（差一个尾斜杠），
    于是「能读详情却读不了列表」。因此这里再探测一次 ``path + "/"``，
    把通配写法的子树权限如实兑现 —— 放行范围仍限于同一条路径的子树。
    """
    return check_path(ctx, "GET", path) or check_path(ctx, "GET", path + "/")

def store_order_list_impl(args: dict) -> dict:
    """列门店订单。可选 warehouse_code / status / limit / offset 过滤。"""
    ctx = _ctx()
    if not _check_read_path(ctx, LIST_PATH):
        raise ForbiddenError("forbidden_path")
    return svc_list_store_orders(
        ctx,
        warehouse_code=args.get("warehouse_code"),
        status=args.get("status"),
        limit=args.get("limit", DEFAULT_LIMIT),
        offset=args.get("offset", 0),
    )


def store_order_detail_impl(args: dict) -> dict:
    """单个门店订单详情：头 + items + deliveries + receipts + status_history。"""
    ctx = _ctx()
    if not _check_read_path(ctx, DETAIL_PATH):
        raise ForbiddenError("forbidden_path")
    order_id = args.get("order_id")
    if order_id is None:
        raise ValidationError("order_id required")
    return svc_get_store_order_detail(ctx, order_id)
