"""权限矩阵单一事实来源（Single Source of Truth）。

本模块**只放数据**（无 Flask / 无 DB 依赖），供三方共用：
  1. 运行时守卫 `permissions.py`（权限点 code、角色包）
  2. 管理 UI（角色权限矩阵视图；阶段 2）
  3. 测试（`tests/test_route_permission_matrix.py` 的 deny/allow 矩阵与覆盖校验）

依据：docs/2026-10-12-permission-spec.md（附录 A/B，2026-10-13 Eric 终裁锁定）。

约定：
  - `ROUTE_MATRIX` 覆盖全部已注册路由（含 static/health）；新增路由未登记 → Test A 失败。
  - 意图（intent）取值见下方常量；兼容层意图用 `ROLE_PREFIX` 前缀表达（不迁移的旧 require_role）。
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# 权限点目录（25 点：order 5 / stock 6 / item 6 / report 3 / admin 5）
#   category  : 分组（UI 矩阵分域）
#   risk      : 'normal' | 'sensitive' | 'critical'（UI 高亮 + 测试断言）
#   description: 中文可读
# ---------------------------------------------------------------------------
PERMISSION_CATALOG: dict[str, dict[str, str]] = {
    # order.*
    "order.create":            {"category": "order",  "risk": "normal",    "description": "创建订货单（含购物车增删改）"},
    "order.approve":           {"category": "order",  "risk": "critical",  "description": "审批订货单（DC 侧 review）"},
    "order.ship":              {"category": "order",  "risk": "sensitive", "description": "DC 发货 / 门店整单交付确认"},
    "order.receive":           {"category": "order",  "risk": "sensitive", "description": "门店收货确认"},
    "order.cancel.any":        {"category": "order",  "risk": "critical",  "description": "取消任意订货单（越权撤单，非 owner 场景）"},
    # stock.*
    "stock.outbound":          {"category": "stock",  "risk": "sensitive", "description": "出库执行（扣账）"},
    "stock.outbound.rollback": {"category": "stock",  "risk": "critical",  "description": "出库回退（冲销扣账）"},
    "stock.stocktake":         {"category": "stock",  "risk": "normal",    "description": "盘点录入 / 提交 / 修正 / 回滚"},
    "stock.stocktake.approve": {"category": "stock",  "risk": "critical",  "description": "盘点审批（生成盘亏出库）"},
    "stock.adjust":            {"category": "stock",  "risk": "sensitive", "description": "库存调整单提交 / 回滚 / 删除"},
    "stock.restock":           {"category": "stock",  "risk": "sensitive", "description": "补货申请提交 / 状态跃迁 / 删除"},
    # item.*
    "item.view":               {"category": "item",   "risk": "normal",    "description": "查看品项信息"},
    "item.edit":               {"category": "item",   "risk": "sensitive", "description": "编辑品项"},
    "item.create":             {"category": "item",   "risk": "sensitive", "description": "新建品项"},
    "item.delete":             {"category": "item",   "risk": "critical",  "description": "删除品项（历史倒挂点；维持平台级）"},
    "item.price.view":         {"category": "item",   "risk": "sensitive", "description": "查看售价 / 成本字段（单品上下文）"},
    "item.price.edit":         {"category": "item",   "risk": "critical",  "description": "编辑售价 / 采购价（Q6 canonical_managed）"},
    # report.*
    "report.view":             {"category": "report", "risk": "normal",    "description": "查看非成本报表"},
    "report.export":           {"category": "report", "risk": "sensitive", "description": "导出报表（CSV）"},
    "report.cost.view":        {"category": "report", "risk": "sensitive", "description": "查看报表 / 导出中的成本金额列"},
    # admin.*（不进任何仓库角色包，仅平台超管）
    "admin.user.manage":       {"category": "admin",  "risk": "critical",  "description": "用户与绑定管理"},
    "admin.token.manage":      {"category": "admin",  "risk": "critical",  "description": "Agent Token 管理"},
    "admin.warehouse.manage":  {"category": "admin",  "risk": "critical",  "description": "仓库管理"},
    "admin.canonical.manage":  {"category": "admin",  "risk": "critical",  "description": "主数据管理"},
    "admin.role.manage":       {"category": "admin",  "risk": "critical",  "description": "角色 / 权限矩阵管理（阶段 2 UI）"},
}

# ---------------------------------------------------------------------------
# 5 个内置角色包（Spec 附录 A，2026-10-13 终裁）
#   阶梯包 staff ⊆ manager ⊆ admin；viewer / finance 为旁支（rank=None）
# ---------------------------------------------------------------------------
ROLE_PACKAGES: dict[str, frozenset[str]] = {
    "staff": frozenset({
        "order.create", "order.receive", "stock.outbound", "stock.outbound.rollback",
        "stock.stocktake", "stock.adjust", "stock.restock",
        "item.view", "report.view", "report.export",
    }),
    "manager": frozenset({
        "order.create", "order.receive", "stock.outbound", "stock.outbound.rollback",
        "stock.stocktake", "stock.adjust", "stock.restock",
        "item.view", "report.view", "report.export",
        "order.approve", "order.ship", "item.edit", "item.create",
        "item.price.view", "report.cost.view",
        "stock.stocktake.approve",  # 终裁①：由 admin 上移至 manager
    }),
    "admin": frozenset({
        "order.create", "order.receive", "stock.outbound", "stock.outbound.rollback",
        "stock.stocktake", "stock.adjust", "stock.restock",
        "item.view", "report.view", "report.export",
        "order.approve", "order.ship", "item.edit", "item.create",
        "item.price.view", "report.cost.view",
        "stock.stocktake.approve",
        "item.price.edit",  # 终裁②：item.delete 已移出，不含
    }),
    "viewer": frozenset({"item.view", "report.view", "report.export"}),
    "finance": frozenset({"item.view", "report.view", "report.export", "report.cost.view"}),
}

# 内置包 code（5 个）
BUILTIN_ROLE_CODES: tuple[str, ...] = ("staff", "manager", "admin", "viewer", "finance")
# 阶梯三包（只此三者参与包含关系断言）
LADDER_ROLE_CODES: tuple[str, ...] = ("staff", "manager", "admin")

# 权限数契约（播种断言 / 测试核对基准）
BUILTIN_PACKAGE_SIZES: dict[str, int] = {
    "staff": 10, "manager": 17, "admin": 18, "viewer": 3, "finance": 4,
}

# 角色显示名 + rank（rank=None 表示旁支，不参与阶梯）
ROLE_META: dict[str, tuple[str, int | None]] = {
    "staff":   ("基础作业", 1),
    "manager": ("管理审批", 2),
    "admin":   ("仓管理", 3),
    "viewer":  ("只读/审计", None),
    "finance": ("财务", None),
}

# 平台级点（不进任何角色包）：admin.* 5 + item.delete + order.cancel.any
PLATFORM_ONLY_PERMS: frozenset[str] = frozenset({
    "admin.user.manage", "admin.token.manage", "admin.warehouse.manage",
    "admin.canonical.manage", "admin.role.manage",
    "item.delete", "order.cancel.any",
})

# ---------------------------------------------------------------------------
# 路由意图 DSL
# ---------------------------------------------------------------------------
PUBLIC = "public"        # 无需登录
LOGIN = "login"          # 仅登录（+ 选仓）
PLATFORM = "platform"    # 仅平台超管（@require_platform_admin 语义）
PERM_PREFIX = "perm:"     # perm:<code>
ANY_PREFIX = "any:"       # any:<code1>|<code2>
ROLE_PREFIX = "role:"     # role:<code>（兼容层：保留 require_role，不迁移）
DC_PREFIX = "dc>"         # 前缀：需 distribution_center 仓类型


def perm(code: str) -> str:
    return f"{PERM_PREFIX}{code}"


def anyperm(*codes: str) -> str:
    return f"{ANY_PREFIX}{'|'.join(codes)}"


# ---------------------------------------------------------------------------
# ROUTE_MATRIX：endpoint -> intent（覆盖全部已注册路由）
# ---------------------------------------------------------------------------
ROUTE_MATRIX: dict[str, str] = {
    # ---- agent_tokens（平台超管；admin.token.manage 名义）----
    "agent_tokens.list_tokens": PLATFORM,
    "agent_tokens.create_token": PLATFORM,
    "agent_tokens.revoke_token": PLATFORM,
    # ---- auth / pwa ----
    "auth.login": PUBLIC,
    "auth.logout": PUBLIC,
    "auth.warehouse_picker": LOGIN,
    "auth.warehouse_select": LOGIN,
    "pwa.service_worker": PUBLIC,
    "pwa.webmanifest": PUBLIC,
    # ---- canonical ----
    "canonical.canonical_list": LOGIN,
    "canonical.canonical_detail": LOGIN,
    "canonical.canonical_edit": PLATFORM,       # 清除死装饰器（终裁③类）
    "canonical.canonical_binding_active": PLATFORM,
    "canonical.canonical_batch_edit": PLATFORM,
    "canonical.canonical_backfill_prices": PLATFORM,
    "canonical.canonical_diff": LOGIN,
    "canonical.canonical_conflicts": LOGIN,
    "canonical.canonical_resolve_conflict": f"{ROLE_PREFIX}manager",
    "canonical.canonical_unbound": LOGIN,
    "canonical.canonical_claim": f"{ROLE_PREFIX}manager",
    "canonical.canonical_unclaim": f"{ROLE_PREFIX}manager",
    "canonical.canonical_claim_requests": LOGIN,
    "canonical.canonical_submit_claim_request": LOGIN,
    "canonical.canonical_review_claim_request": f"{ROLE_PREFIX}manager",
    "canonical.canonical_bulk_import": f"{ROLE_PREFIX}admin",
    "canonical.canonical_fanout": f"{ROLE_PREFIX}admin",
    "canonical.canonical_fanout_event": LOGIN,
    # ---- core ----
    "core.dashboard": LOGIN,
    "core.land": LOGIN,
    "core.summary": LOGIN,                      # 终裁④：不脱敏
    "core.categories": LOGIN,
    "core.delete_category": f"{ROLE_PREFIX}staff",
    # ---- feedback（用户问题提单 → 管理员判定 → 派发开发闭环）----
    # 用户侧：任意登录用户可提交/看自己的单子（含被忽略的，让他知道被处理过）。
    "feedback.my_feedback": LOGIN,
    "feedback.new_feedback": LOGIN,
    "feedback.submit_feedback": LOGIN,
    "feedback.ticket_detail": LOGIN,            # 函数体内再收一次：非本人且非超管 404
    # 管理员侧：收件箱、判定、评论、派发 —— 平台超管专属。
    # 不开权限点：反馈处理涉及「对外建 Issue / 触发部署」，属于平台级动作，
    # 与 admin.user.manage 同一档，不进任何仓库角色包。
    "feedback.admin_inbox": PLATFORM,
    "feedback.admin_feedback_detail": PLATFORM,
    "feedback.ignore_feedback": PLATFORM,
    "feedback.save_comment": PLATFORM,
    "feedback.dispatch_feedback": PLATFORM,
    # ---- forecast ----
    "forecast.forecast_index": PLATFORM,        # 清除死装饰器
    "forecast.forecast_item": f"{ROLE_PREFIX}manager",
    "forecast.forecast_product": f"{ROLE_PREFIX}manager",
    "forecast.forecast_recompute": f"{ROLE_PREFIX}manager",
    # ---- health ----
    "health": PUBLIC,
    # ---- import_items ----
    "import_items.upload_form": f"{ROLE_PREFIX}admin",
    "import_items.upload_parse": f"{ROLE_PREFIX}admin",
    "import_items.preview": f"{ROLE_PREFIX}admin",
    "import_items.commit": f"{ROLE_PREFIX}admin",
    # ---- items ----
    "items.items_list": PLATFORM,               # 终裁③：维持平台级
    "items.edit_item": PLATFORM,                # 终裁③
    "items.delete_item": PLATFORM,              # 终裁②
    "items.toggle_item_active": PLATFORM,
    "items.recompute_safety_stock": PLATFORM,
    "items.inventory_view": LOGIN,              # 终裁④：不脱敏
    "items.items_publish": PLATFORM,
    "items.items_publish_history": LOGIN,
    "items.new_item_redirect": perm("item.create"),
    "items.new_item_redirect_submit": perm("item.create"),
    # ---- notifications ----
    "notifications.notifications_feed": LOGIN,
    "notifications.mark_notification_read": LOGIN,
    "notifications.test_emit": PLATFORM,
    # ---- outbound ----
    "outbound.outbound_list": LOGIN,
    "outbound.outbound_start": LOGIN,
    "outbound.outbound_session": LOGIN,
    "outbound.outbound_submit": perm("stock.outbound"),          # 终裁：显式化
    "outbound.rollback": perm("stock.outbound.rollback"),
    "outbound.delete": f"{ROLE_PREFIX}staff",
    # ---- procurement ----
    "procurement.procurement_store": PLATFORM,  # 清除死装饰器
    "procurement.procurement_hub": f"{ROLE_PREFIX}manager",
    "procurement.procurement_accept": f"{ROLE_PREFIX}staff",
    "procurement.procurement_accept_download": f"{ROLE_PREFIX}staff",
    # ---- production ----
    "production.products_list": LOGIN,
    "production.product_new": PLATFORM,
    "production.product_edit": PLATFORM,
    "production.product_delete": PLATFORM,
    "production.session": LOGIN,
    "production.submit": LOGIN,                 # 终裁⑤：残留 LOGIN + OPEN
    "production.runs_list": LOGIN,
    "production.rollback": f"{ROLE_PREFIX}staff",
    "production.delete_run": f"{ROLE_PREFIX}staff",
    "production.runs_csv": LOGIN,
    # ---- recipe_cost ----
    "recipe_cost.landing": LOGIN,
    "recipe_cost.ic_recipes_list": LOGIN,
    "recipe_cost.ic_recipe_new": PLATFORM,
    "recipe_cost.ic_recipe_edit": PLATFORM,
    "recipe_cost.ic_recipe_delete": PLATFORM,
    "recipe_cost.recipes_list": LOGIN,
    "recipe_cost.recipe_new": PLATFORM,
    "recipe_cost.recipe_edit": PLATFORM,
    "recipe_cost.recipe_delete": PLATFORM,
    "recipe_cost.update_selling_price": PLATFORM,
    # R1 终裁（2026-10-13，验证报告 R1）：配方成本读接口收紧至 report.cost.view
    # （staff/viewer 403；manager/finance/admin 放行）。
    "recipe_cost.api_cost": perm("report.cost.view"),
    "recipe_cost.ic_recipes_export_csv": perm("report.cost.view"),
    "recipe_cost.recipes_export_csv": perm("report.cost.view"),
    "recipe_cost.ic_recipe_bom_export_csv": perm("report.cost.view"),
    "recipe_cost.recipe_bom_export_csv": perm("report.cost.view"),
    "recipe_cost.ic_recipe_publish": PLATFORM,
    "recipe_cost.recipe_publish": PLATFORM,
    "recipe_cost.recipe_versions_history": LOGIN,
    # F1 全品牌成本总览（2026-10-16）：读挂 report.cost.view（与 CSV 导出同口径）
    "recipe_cost.overview": perm("report.cost.view"),
    "recipe_cost.overview_export_csv": perm("report.cost.view"),
    # F2 门店存量配方导入（2026-10-16）：写操作，与配方编辑同权限
    "recipe_cost.import_index": PLATFORM,
    "recipe_cost.import_preview": PLATFORM,
    "recipe_cost.import_execute": PLATFORM,
    # ---- reports ----
    "reports.outbound": perm("report.view"),
    "reports.inbound": perm("report.view"),
    "reports.export_consumption": perm("report.export"),
    "reports.export_summary": perm("report.export"),
    "reports.api_upload_revenue": f"{ROLE_PREFIX}manager",
    # 库存健康仪表盘（PR #35 并入）：manager+ 单仓只读，兼容层保留
    # require_role 守卫（比 report.view 严 —— staff 无份，不迁移到权限点）。
    "reports.health": f"{ROLE_PREFIX}manager",
    "reports.health_export": f"{ROLE_PREFIX}manager",
    # ---- restock ----
    "restock.restock_list": LOGIN,
    "restock.restock_start": LOGIN,
    "restock.restock_session": LOGIN,
    "restock.restock_submit": perm("stock.restock"),
    "restock.update_status": perm("stock.restock"),
    "restock.delete": perm("stock.restock"),
    # ---- static ----
    "static": PUBLIC,
    # ---- stocktake ----
    "stocktake.stocktake_csv_form": f"{ROLE_PREFIX}admin",
    "stocktake.stocktake_csv_parse": f"{ROLE_PREFIX}admin",
    "stocktake.stocktake_csv_preview_view": f"{ROLE_PREFIX}admin",
    "stocktake.stocktake_csv_commit": f"{ROLE_PREFIX}admin",
    "stocktake.stocktake_list": LOGIN,
    "stocktake.stocktake_start": LOGIN,
    "stocktake.stocktake_session": LOGIN,
    "stocktake.stocktake_submit": perm("stock.stocktake"),
    "stocktake.rollback": perm("stock.stocktake"),
    "stocktake.edit": perm("stock.stocktake"),
    "stocktake.submit_edit": perm("stock.stocktake"),
    "stocktake.approve": perm("stock.stocktake.approve"),  # 终裁①：收紧 manager+
    # ---- store_ordering ----
    "store_ordering.catalog": LOGIN,
    "store_ordering.add_to_cart": perm("order.create"),
    "store_ordering.cart_add_batch": perm("order.create"),
    "store_ordering.cart_view": LOGIN,
    "store_ordering.update_cart_item": perm("order.create"),
    "store_ordering.remove_cart_item_route": perm("order.create"),
    "store_ordering.clear_cart_route": perm("order.create"),
    "store_ordering.submit_order_route": perm("order.create"),
    "store_ordering.store_orders_list": LOGIN,
    "store_ordering.order_detail": LOGIN,
    "store_ordering.review_list": f"{DC_PREFIX}{ROLE_PREFIX}staff",
    "store_ordering.review_order_route": f"{DC_PREFIX}{perm('order.approve')}",
    "store_ordering.shipment_list": f"{DC_PREFIX}{ROLE_PREFIX}staff",
    "store_ordering.manage_dc_items": f"{DC_PREFIX}{ROLE_PREFIX}manager",
    "store_ordering.cancel_order_route": anyperm("order.create", "order.cancel.any"),
    # 兼容层：DC staff 发货是既有行为（order.ship 归 manager 包，迁移会让 DC
    # staff 失去发货能力 → 非白名单行为变化）。施工图 §1.2-E 修正说明。
    "store_ordering.ship_order_route": f"{DC_PREFIX}{ROLE_PREFIX}staff",
    "store_ordering.deliver_order_route": perm("order.ship"),
    # 守卫层记录为 perm:order.receive；但函数体 inline `role not in ("manager","admin")`
    # 使实际放行 manager+（偏严、非漏洞，行为与 main 一致）——留阶段 2 统一。见施工图 §7 R2。
    "store_ordering.receive_order_route": perm("order.receive"),
    "store_ordering.manage_shipping_rule": f"{ROLE_PREFIX}admin",
    "store_ordering.admin_report": f"{ROLE_PREFIX}admin",
    "store_ordering.admin_orders": f"{ROLE_PREFIX}admin",
    # ---- users（全平台超管）----
    "users.list_users": PLATFORM,
    "users.create_user": PLATFORM,
    "users.bind_role": PLATFORM,
    "users.unbind_role": PLATFORM,
    "users.change_role": PLATFORM,
    "users.reset_password": PLATFORM,
    "users.toggle_admin": PLATFORM,
    # v2 并入的路由（PR #17）：停用/启用 + 批量绑定，均 @require_platform_admin。
    "users.toggle_active": PLATFORM,
    "users.bulk_bind": PLATFORM,
    "users.delete_user": PLATFORM,
    "users.create_warehouse": PLATFORM,
    "users.init_warehouse": PLATFORM,
}


def required_permissions(intent: str) -> frozenset[str]:
    """从 intent 解析出所需权限点集合（供测试推导期望放行）。

    - perm:X           -> {X}
    - any:a|b          -> {a, b}（OR，测试侧按"任一命中即允许"处理）
    - dc>perm:X        -> {X}
    - 其余（public/login/platform/role:x）-> 空集（不按权限点判定）
    """
    core = intent.split(">", 1)[1] if intent.startswith(DC_PREFIX) else intent
    if core.startswith(PERM_PREFIX):
        return frozenset({core[len(PERM_PREFIX):]})
    if core.startswith(ANY_PREFIX):
        return frozenset(core[len(ANY_PREFIX):].split("|"))
    return frozenset()


def is_any(intent: str) -> bool:
    core = intent.split(">", 1)[1] if intent.startswith(DC_PREFIX) else intent
    return core.startswith(ANY_PREFIX)


def is_dc_scoped(intent: str) -> bool:
    return intent.startswith(DC_PREFIX)
