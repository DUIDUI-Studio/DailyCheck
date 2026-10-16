"""品项主数据 MCP Tools。

11 个工具，每个与 Flask canonical 蓝图路由 1:1 对齐（path 白名单校验）。

权限模型：service 层不做角色检查；token 的 allowed_read_paths /
allowed_write_paths 是唯一授权边界。给运营签发 token 时按需勾选
`/api/v1/canonical*` 即可。
"""
from __future__ import annotations

import logging

from mcp_server.infra.errors import ForbiddenError
from mcp_server.service.auth import check_path, resolve_ctx
from mcp_server.service.canonical import (
    claim_item as svc_claim_item,
)
from mcp_server.service.canonical import (
    create_canonical as svc_create,
)
from mcp_server.service.canonical import (
    fanout_canonical as svc_fanout,
)
from mcp_server.service.canonical import (
    get_canonical_detail as svc_detail,
)
from mcp_server.service.canonical import (
    get_fanout_event as svc_event,
)
from mcp_server.service.canonical import (
    list_canonical as svc_list,
)
from mcp_server.service.canonical import (
    list_canonical_categories as svc_categories,
)
from mcp_server.service.canonical import (
    list_fanout_targets as svc_targets,
)
from mcp_server.service.canonical import (
    list_open_conflicts as svc_conflicts,
)
from mcp_server.service.canonical import (
    resolve_conflict as svc_resolve,
)
from mcp_server.service.canonical import (
    set_binding_active as svc_binding_active,
)
from mcp_server.service.canonical import (
    unclaim_item as svc_unclaim_item,
)
from mcp_server.service.canonical import (
    update_canonical as svc_update,
)

logger = logging.getLogger(__name__)


def _ctx():
    return resolve_ctx()


# ─────────────────────────────────────────────────────────────────────
# 读
# ─────────────────────────────────────────────────────────────────────

def canonical_list_impl(args: dict) -> dict:
    """列主数据。可选 status / category_code / 关键词 q + limit / offset。"""
    ctx = _ctx()
    if not check_path(ctx, "GET", "/api/v1/canonical"):
        raise ForbiddenError("forbidden_path")
    return svc_list(
        status=args.get("status"),
        category_code=args.get("category_code"),
        q=args.get("q"),
        limit=int(args.get("limit") or 200),
        offset=int(args.get("offset") or 0),
    )


def canonical_detail_impl(args: dict) -> dict:
    """单条主数据详情（canonical + aliases + 跨仓 bindings）。"""
    canonical_id = int(args["canonical_id"])
    ctx = _ctx()
    if not check_path(ctx, "GET", "/api/v1/canonical/<id>"):
        raise ForbiddenError("forbidden_path")
    return svc_detail(canonical_id)


def canonical_categories_impl(args: dict) -> list[dict]:
    """列主数据品类（供分类筛选下拉）。"""
    _ = args
    ctx = _ctx()
    if not check_path(ctx, "GET", "/api/v1/canonical/categories"):
        raise ForbiddenError("forbidden_path")
    return svc_categories()


def canonical_fanout_targets_impl(args: dict) -> list[dict]:
    """可作为扇出目标的仓（storefront + rd + distribution_center）。"""
    _ = args
    ctx = _ctx()
    if not check_path(ctx, "GET", "/api/v1/canonical/fanout-targets"):
        raise ForbiddenError("forbidden_path")
    return svc_targets()


def canonical_conflicts_impl(args: dict) -> list[dict]:
    """列 open 冲突；可选 canonical_id 过滤。"""
    ctx = _ctx()
    if not check_path(ctx, "GET", "/api/v1/canonical/conflicts"):
        raise ForbiddenError("forbidden_path")
    return svc_conflicts(
        canonical_id=int(args["canonical_id"]) if args.get("canonical_id") else None,
    )


def canonical_fanout_event_impl(args: dict) -> dict:
    """单次扇出事件详情（含 per-仓 / per-item 应用结果）。"""
    event_id = int(args["event_id"])
    ctx = _ctx()
    if not check_path(ctx, "GET", "/api/v1/canonical/fanout/event/<id>"):
        raise ForbiddenError("forbidden_path")
    return svc_event(event_id)


# ─────────────────────────────────────────────────────────────────────
# 写
# ─────────────────────────────────────────────────────────────────────

def canonical_create_impl(args: dict) -> dict:
    """新建主数据。字段：name / unit 必填，其它可选（Q6=canonical_managed 时
    selling_price / unit_cost 也进白名单）。"""
    ctx = _ctx()
    if not check_path(ctx, "POST", "/api/v1/canonical"):
        raise ForbiddenError("forbidden_path")
    return svc_create(
        name=args["name"],
        unit=args["unit"],
        category_code=args.get("category_code"),
        gram_per_unit=float(args.get("gram_per_unit") or 0),
        aux_unit=args.get("aux_unit"),
        aux_rate=float(args.get("aux_rate") or 0),
        barcode=args.get("barcode"),
        status=args.get("status") or "active",
        selling_price=(
            float(args["selling_price"])
            if args.get("selling_price") not in (None, "",)
            else None
        ),
        unit_cost=(
            float(args["unit_cost"])
            if args.get("unit_cost") not in (None, "",)
            else None
        ),
        actor_id=ctx.token_id,
    )


def canonical_update_impl(args: dict) -> dict:
    """更新主数据。只能改白名单字段；status='deleted' 会被 Q3 拒绝。"""
    canonical_id = int(args["canonical_id"])
    ctx = _ctx()
    if not check_path(ctx, "POST", "/api/v1/canonical/<id>/update"):
        raise ForbiddenError("forbidden_path")
    return svc_update(
        canonical_id,
        **{k: v for k, v in args.items() if k != "canonical_id"},
        actor_id=ctx.token_id,
    )


def canonical_claim_impl(args: dict) -> dict:
    """把指定仓的 items 行认领到 canonical_id。"""
    ctx = _ctx()
    if not check_path(ctx, "POST", "/api/v1/canonical/claim"):
        raise ForbiddenError("forbidden_path")
    return svc_claim_item(
        warehouse_code=args["warehouse_code"],
        local_item_id=int(args["local_item_id"]),
        canonical_id=int(args["canonical_id"]),
        ctx=ctx,
        is_alias=bool(args.get("is_alias") or False),
        local_keep_name=args.get("local_keep_name"),
        reason=args.get("reason"),
        actor_id=ctx.token_id,
    )


def canonical_unclaim_impl(args: dict) -> dict:
    """解绑 —— 把 warehouse_code + local_item_id 的 canonical_id 清空。"""
    ctx = _ctx()
    if not check_path(ctx, "POST", "/api/v1/canonical/unclaim"):
        raise ForbiddenError("forbidden_path")
    return svc_unclaim_item(
        warehouse_code=args["warehouse_code"],
        local_item_id=int(args["local_item_id"]),
        ctx=ctx,
        reason=args.get("reason"),
        actor_id=ctx.token_id,
    )


def canonical_fanout_impl(args: dict) -> dict:
    """M2 主路径：扇出——把 canonical_ids 的字段写到 warehouse_codes。

    - canonical_ids: int[] 必填
    - warehouse_codes: string[] 必填（**必须含至少一个 distribution_center**）
    - action: 'overwrite'（默认）| 'skip'
    - force: bool，是否强制覆盖 Q4=freeze 字段
    - summary: string，事件摘要
    """
    ctx = _ctx()
    if not check_path(ctx, "POST", "/api/v1/canonical/fanout"):
        raise ForbiddenError("forbidden_path")
    return svc_fanout(
        canonical_ids=list(args["canonical_ids"]),
        warehouse_codes=list(args["warehouse_codes"]),
        action=args.get("action") or "overwrite",
        force=bool(args.get("force") or False),
        summary=args.get("summary"),
        actor_id=ctx.token_id,
    )


def canonical_resolve_conflict_impl(args: dict) -> dict:
    """裁决 conflict。

    decision:
      - 'keep_local'       → status='resolved_keep_local'
      - 'accept_canonical' → status='resolved_accept_canonical'
      - 'waive'            → status='waived'
    """
    ctx = _ctx()
    if not check_path(ctx, "POST", "/api/v1/canonical/conflicts/<id>/resolve"):
        raise ForbiddenError("forbidden_path")
    return svc_resolve(
        conflict_id=int(args["conflict_id"]),
        decision=args["decision"],
        note=args.get("note"),
        actor_id=ctx.token_id,
    )


def canonical_set_binding_active_impl(args: dict) -> dict:
    """P0-10 —— 按仓启停 items.is_active（不动库存 / 主数据字段）。

    active=false 把目标行停用，门店侧立即从列表隐藏。
    """
    ctx = _ctx()
    if not check_path(ctx, "POST", "/api/v1/canonical/binding-active"):
        raise ForbiddenError("forbidden_path")
    return svc_binding_active(
        warehouse_code=args["warehouse_code"],
        canonical_id=int(args["canonical_id"]),
        active=bool(args.get("active")),
        ctx=ctx,
    )
