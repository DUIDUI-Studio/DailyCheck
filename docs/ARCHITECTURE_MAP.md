# DailyCheck 架构事实图

> 本文件由 `scripts/arch_map.py` **自动生成，禁止手工编辑**。
> 一致性由 `tests/test_arch_map.py` 在 CI 的 `test` job 强制：与源码不符即失败。
> 要更新请运行 `python3 scripts/arch_map.py --write`。

**怎么用这张表**

1. 想知道「这个 URL 要什么权限」→ 查 §2，按蓝图分组，`鉴权` 列是唯一事实来源。
2. 想知道「系统有哪些模块、谁在服役」→ 查 §1 与 §5（未装配 = 死代码）。
3. 想知道「最近架构变了什么」→ `git log -p -- docs/ARCHITECTURE_MAP.md`。

## 0. 一览

- 蓝图 19 个（已装配 18 / 未装配 1）
- 路由 **165** 条（按鉴权强度：平台超管 48 · 登录 44 · 权限点 31 · 角色 30 · 公开 6 · 配送中心 5 · 任一权限 1）
- 数据表 **59** 张，`WAREHOUSE_SCHEMA_VERSION = 2`
- MCP 工具域 7 个
- ⚠️ 静态解析到但矩阵未登记的路由 5 条（见 §5）

## 1. 蓝图装配

| 蓝图 | 文件 | 路由数 | 装配 |
|---|---|---|---|
| `recipe_cost` | `recipe_cost.py` | 23 | 在役 |
| `store_ordering` | `store_ordering.py` | 21 | 在役 |
| `canonical` | `canonical.py` | 20 | 在役 |
| `stocktake` | `stocktake.py` | 12 | 在役 |
| `users` | `users.py` | 12 | 在役 |
| `items` | `items.py` | 11 | 在役 |
| `production` | `production.py` | 10 | 在役 |
| `feedback` | `feedback.py` | 9 | 在役 |
| `reports` | `reports.py` | 7 | 在役 |
| `auth / pwa` | `auth.py` | 6 | 在役 |
| `core` | `core.py` | 6 | 在役 |
| `outbound` | `outbound.py` | 6 | 在役 |
| `restock` | `restock.py` | 6 | 在役 |
| `adjustment` | `adjustment.py` | 5 | **未装配** |
| `forecast` | `forecast.py` | 4 | 在役 |
| `import_items` | `import_items.py` | 4 | 在役 |
| `procurement` | `procurement.py` | 4 | 在役 |
| `agent_tokens` | `agent_tokens.py` | 3 | 在役 |
| `notifications` | `notifications.py` | 3 | 在役 |

## 2. 路由 × 鉴权（唯一事实来源：`permissions_manifest.ROUTE_MATRIX`）

### `agent_tokens`

| endpoint | 方法 | 规则 | 鉴权 | 明细 |
|---|---|---|---|---|
| `agent_tokens.create_token` | POST | `/admin/agent-tokens/create` | 平台超管 |  |
| `agent_tokens.list_tokens` | GET | `/admin/agent-tokens` | 平台超管 |  |
| `agent_tokens.revoke_token` | POST | `/admin/agent-tokens/<int:token_id>/revoke` | 平台超管 |  |

### `auth`

| endpoint | 方法 | 规则 | 鉴权 | 明细 |
|---|---|---|---|---|
| `auth.login` | GET,POST | `/login` | 公开 |  |
| `auth.logout` | GET | `/logout` | 公开 |  |
| `auth.warehouse_picker` | GET | `/select-warehouse` | 登录 |  |
| `auth.warehouse_select` | POST | `/select-warehouse/<int:warehouse_id>` | 登录 |  |

### `canonical`

| endpoint | 方法 | 规则 | 鉴权 | 明细 |
|---|---|---|---|---|
| `canonical.canonical_backfill_prices` | POST | `/backfill-prices` | 平台超管 |  |
| `canonical.canonical_batch_edit` | POST | `/batch-edit` | 平台超管 |  |
| `canonical.canonical_binding_active` | POST | `/binding-active` | 平台超管 |  |
| `canonical.canonical_bulk_import` | GET,POST | `/bulk-import` | 角色 | admin |
| `canonical.canonical_claim` | POST | `/claim` | 角色 | manager |
| `canonical.canonical_claim_requests` | GET | `/claim-requests` | 登录 |  |
| `canonical.canonical_conflicts` | GET | `/conflicts` | 登录 |  |
| `canonical.canonical_detail` | GET | `/detail/<int:canonical_id>` | 登录 |  |
| `canonical.canonical_diff` | GET | `/diff` | 登录 |  |
| `canonical.canonical_edit` | GET,POST | `/edit/<int:canonical_id>` | 平台超管 |  |
| `canonical.canonical_fanout` | GET,POST | `/fanout/<int:canonical_id>` | 角色 | admin |
| `canonical.canonical_fanout_event` | GET | `/fanout/event/<int:event_id>` | 登录 |  |
| `canonical.canonical_list` | GET | `/list` | 登录 |  |
| `canonical.canonical_resolve_conflict` | GET,POST | `/conflict/<int:conflict_id>` | 角色 | manager |
| `canonical.canonical_review_claim_request` | POST | `/claim-requests/<int:request_id>/review` | 角色 | manager |
| `canonical.canonical_submit_claim_request` | POST | `/claim-requests/new` | 登录 |  |
| `canonical.canonical_unbound` | GET | `/unbound` | 登录 |  |
| `canonical.canonical_unclaim` | POST | `/unclaim` | 角色 | manager |

### `core`

| endpoint | 方法 | 规则 | 鉴权 | 明细 |
|---|---|---|---|---|
| `core.categories` | GET | `/categories` | 登录 |  |
| `core.dashboard` | GET | `/dashboard` | 登录 |  |
| `core.delete_category` | POST | `/categories/<int:category_id>/delete` | 角色 | staff |
| `core.land` | GET | `/land` | 登录 |  |
| `core.summary` | GET | `/summary` | 登录 |  |

### `feedback`

| endpoint | 方法 | 规则 | 鉴权 | 明细 |
|---|---|---|---|---|
| `feedback.admin_feedback_detail` | GET | `/admin/feedback/<int:ticket_id>` | 平台超管 |  |
| `feedback.admin_inbox` | GET | `/admin/feedback` | 平台超管 |  |
| `feedback.dispatch_feedback` | POST | `/admin/feedback/<int:ticket_id>/dispatch` | 平台超管 |  |
| `feedback.ignore_feedback` | POST | `/admin/feedback/<int:ticket_id>/ignore` | 平台超管 |  |
| `feedback.my_feedback` | GET | `/feedback` | 登录 |  |
| `feedback.new_feedback` | GET | `/feedback/new` | 登录 |  |
| `feedback.save_comment` | POST | `/admin/feedback/<int:ticket_id>/comment` | 平台超管 |  |
| `feedback.submit_feedback` | POST | `/feedback` | 登录 |  |
| `feedback.ticket_detail` | GET | `/feedback/<int:ticket_id>` | 登录 |  |

### `forecast`

| endpoint | 方法 | 规则 | 鉴权 | 明细 |
|---|---|---|---|---|
| `forecast.forecast_index` | GET | `/forecast` | 平台超管 |  |
| `forecast.forecast_item` | GET | `/forecast/item/<int:item_id>` | 角色 | manager |
| `forecast.forecast_product` | GET | `/forecast/product/<int:product_id>` | 角色 | manager |
| `forecast.forecast_recompute` | POST | `/forecast/recompute` | 角色 | manager |

### `health`

| endpoint | 方法 | 规则 | 鉴权 | 明细 |
|---|---|---|---|---|
| `health` | ? | `?` | 公开 |  |

### `import_items`

| endpoint | 方法 | 规则 | 鉴权 | 明细 |
|---|---|---|---|---|
| `import_items.commit` | POST | `/commit` | 角色 | admin |
| `import_items.preview` | GET | `/preview` | 角色 | admin |
| `import_items.upload_form` | GET | `` | 角色 | admin |
| `import_items.upload_parse` | POST | `` | 角色 | admin |

### `items`

| endpoint | 方法 | 规则 | 鉴权 | 明细 |
|---|---|---|---|---|
| `items.delete_item` | POST | `/items/<int:item_id>/delete` | 平台超管 |  |
| `items.edit_item` | GET,POST | `/items/<int:item_id>/edit` | 平台超管 |  |
| `items.inventory_view` | GET | `/inventory` | 登录 |  |
| `items.items_list` | GET,POST | `/items` | 平台超管 |  |
| `items.items_publish` | GET,POST | `/items/publish` | 平台超管 |  |
| `items.items_publish_history` | GET | `/items/publish/history/<int:event_id>` | 登录 |  |
| `items.new_item_redirect` | GET | `/items/new-item-redirect` | 权限点 | item.create |
| `items.new_item_redirect_submit` | POST | `/items/new-item-redirect/submit` | 权限点 | item.create |
| `items.recompute_safety_stock` | POST | `/items/recompute-safety-stock` | 平台超管 |  |
| `items.toggle_item_active` | POST | `/items/<int:item_id>/toggle-active` | 平台超管 |  |

### `notifications`

| endpoint | 方法 | 规则 | 鉴权 | 明细 |
|---|---|---|---|---|
| `notifications.mark_notification_read` | POST | `/notifications/<int:event_id>/read` | 登录 |  |
| `notifications.notifications_feed` | GET | `/notifications` | 登录 |  |
| `notifications.test_emit` | POST | `/admin/notifications/test-emit` | 平台超管 |  |

### `outbound`

| endpoint | 方法 | 规则 | 鉴权 | 明细 |
|---|---|---|---|---|
| `outbound.delete` | POST | `/outbound/<int:req_id>/delete` | 角色 | staff |
| `outbound.outbound_list` | GET | `/outbound` | 登录 |  |
| `outbound.outbound_session` | GET | `/outbound/session` | 登录 |  |
| `outbound.outbound_start` | POST | `/outbound/start` | 登录 |  |
| `outbound.outbound_submit` | POST | `/outbound/submit` | 权限点 | stock.outbound |
| `outbound.rollback` | POST | `/outbound/<int:req_id>/rollback` | 权限点 | stock.outbound.rollback |

### `procurement`

| endpoint | 方法 | 规则 | 鉴权 | 明细 |
|---|---|---|---|---|
| `procurement.procurement_accept` | POST | `/procurement/store/accept` | 角色 | staff |
| `procurement.procurement_accept_download` | GET | `/procurement/store/accept/download` | 角色 | staff |
| `procurement.procurement_hub` | GET | `/procurement/hub` | 角色 | manager |
| `procurement.procurement_store` | GET | `/procurement/store` | 平台超管 |  |

### `production`

| endpoint | 方法 | 规则 | 鉴权 | 明细 |
|---|---|---|---|---|
| `production.delete_run` | POST | `/production/runs/<int:run_id>/delete` | 角色 | staff |
| `production.product_delete` | POST | `/production/products/<int:product_id>/delete` | 平台超管 |  |
| `production.product_edit` | GET,POST | `/production/products/<int:product_id>/edit` | 平台超管 |  |
| `production.product_new` | GET,POST | `/production/products/new` | 平台超管 |  |
| `production.products_list` | GET | `/production` | 登录 |  |
| `production.rollback` | POST | `/production/runs/<int:run_id>/rollback` | 角色 | staff |
| `production.runs_csv` | GET | `/production/runs.csv` | 登录 |  |
| `production.runs_list` | GET | `/production/runs` | 登录 |  |
| `production.session` | GET | `/production/session` | 登录 |  |
| `production.submit` | POST | `/production/submit` | 登录 |  |

### `pwa`

| endpoint | 方法 | 规则 | 鉴权 | 明细 |
|---|---|---|---|---|
| `pwa.service_worker` | GET | `/sw.js` | 公开 |  |
| `pwa.webmanifest` | GET | `/manifest.webmanifest` | 公开 |  |

### `recipe_cost`

| endpoint | 方法 | 规则 | 鉴权 | 明细 |
|---|---|---|---|---|
| `recipe_cost.api_cost` | GET | `/recipe-cost/api/cost/<kind>/<int:rid>` | 权限点 | report.cost.view |
| `recipe_cost.ic_recipe_bom_export_csv` | GET | `/recipe-cost/ic-recipes/<int:ic_recipe_id>/export.csv` | 权限点 | report.cost.view |
| `recipe_cost.ic_recipe_delete` | POST | `/recipe-cost/ic-recipes/<int:ic_recipe_id>/delete` | 平台超管 |  |
| `recipe_cost.ic_recipe_edit` | GET,POST | `/recipe-cost/ic-recipes/<int:ic_recipe_id>/edit` | 平台超管 |  |
| `recipe_cost.ic_recipe_new` | GET,POST | `/recipe-cost/ic-recipes/new` | 平台超管 |  |
| `recipe_cost.ic_recipe_publish` | POST | `/recipe-cost/ic-recipes/<int:ic_recipe_id>/publish` | 平台超管 |  |
| `recipe_cost.ic_recipes_export_csv` | GET | `/recipe-cost/ic-recipes/export.csv` | 权限点 | report.cost.view |
| `recipe_cost.ic_recipes_list` | GET | `/recipe-cost/ic-recipes` | 登录 |  |
| `recipe_cost.import_execute` | POST | `/recipe-cost/import` | 平台超管 |  |
| `recipe_cost.import_index` | GET | `/recipe-cost/import` | 平台超管 |  |
| `recipe_cost.import_preview` | GET | `/recipe-cost/import/preview` | 平台超管 |  |
| `recipe_cost.landing` | GET | `/recipe-cost/` | 登录 |  |
| `recipe_cost.overview` | GET | `/recipe-cost/overview` | 权限点 | report.cost.view |
| `recipe_cost.overview_export_csv` | GET | `/recipe-cost/overview/export.csv` | 权限点 | report.cost.view |
| `recipe_cost.recipe_bom_export_csv` | GET | `/recipe-cost/recipes/<int:recipe_id>/export.csv` | 权限点 | report.cost.view |
| `recipe_cost.recipe_delete` | POST | `/recipe-cost/recipes/<int:recipe_id>/delete` | 平台超管 |  |
| `recipe_cost.recipe_edit` | GET,POST | `/recipe-cost/recipes/<int:recipe_id>/edit` | 平台超管 |  |
| `recipe_cost.recipe_new` | GET,POST | `/recipe-cost/recipes/new` | 平台超管 |  |
| `recipe_cost.recipe_publish` | POST | `/recipe-cost/recipes/<int:recipe_id>/publish` | 平台超管 |  |
| `recipe_cost.recipe_versions_history` | GET | `/recipe-cost/<recipe_type>/<int:recipe_id>/versions` | 登录 |  |
| `recipe_cost.recipes_export_csv` | GET | `/recipe-cost/recipes/export.csv` | 权限点 | report.cost.view |
| `recipe_cost.recipes_list` | GET | `/recipe-cost/recipes` | 登录 |  |
| `recipe_cost.update_selling_price` | POST | `/recipe-cost/items/<int:item_id>/update-selling-price` | 平台超管 |  |

### `reports`

| endpoint | 方法 | 规则 | 鉴权 | 明细 |
|---|---|---|---|---|
| `reports.api_upload_revenue` | POST | `/api/revenue` | 角色 | manager |
| `reports.export_consumption` | GET | `/export/consumption` | 权限点 | report.export |
| `reports.export_summary` | GET | `/summary/export` | 权限点 | report.export |
| `reports.health` | GET | `/report/health` | 角色 | manager |
| `reports.health_export` | GET | `/report/health/export` | 角色 | manager |
| `reports.inbound` | GET | `/report/inbound` | 权限点 | report.view |
| `reports.outbound` | GET | `/report/outbound` | 权限点 | report.view |

### `restock`

| endpoint | 方法 | 规则 | 鉴权 | 明细 |
|---|---|---|---|---|
| `restock.delete` | POST | `/restock/<int:req_id>/delete` | 权限点 | stock.restock |
| `restock.restock_list` | GET | `/restock` | 登录 |  |
| `restock.restock_session` | GET | `/restock/session` | 登录 |  |
| `restock.restock_start` | POST | `/restock/start` | 登录 |  |
| `restock.restock_submit` | POST | `/restock/submit` | 权限点 | stock.restock |
| `restock.update_status` | POST | `/restock/<int:req_id>/status` | 权限点 | stock.restock |

### `static`

| endpoint | 方法 | 规则 | 鉴权 | 明细 |
|---|---|---|---|---|
| `static` | ? | `?` | 公开 |  |

### `stocktake`

| endpoint | 方法 | 规则 | 鉴权 | 明细 |
|---|---|---|---|---|
| `stocktake.approve` | POST | `/stocktake/batch/<int:batch_id>/approve` | 权限点 | stock.stocktake.approve |
| `stocktake.edit` | GET | `/stocktake/batch/<int:batch_id>/edit` | 权限点 | stock.stocktake |
| `stocktake.rollback` | POST | `/stocktake/batch/<int:batch_id>/rollback` | 权限点 | stock.stocktake |
| `stocktake.stocktake_csv_commit` | POST | `/admin/stocktake-import/commit` | 角色 | admin |
| `stocktake.stocktake_csv_form` | GET | `/admin/stocktake-import` | 角色 | admin |
| `stocktake.stocktake_csv_parse` | POST | `/admin/stocktake-import` | 角色 | admin |
| `stocktake.stocktake_csv_preview_view` | GET | `/admin/stocktake-import/preview` | 角色 | admin |
| `stocktake.stocktake_list` | GET | `/stocktake` | 登录 |  |
| `stocktake.stocktake_session` | GET | `/stocktake/session` | 登录 |  |
| `stocktake.stocktake_start` | POST | `/stocktake/start` | 登录 |  |
| `stocktake.stocktake_submit` | POST | `/stocktake/submit` | 权限点 | stock.stocktake |
| `stocktake.submit_edit` | POST | `/stocktake/batch/<int:batch_id>/edit` | 权限点 | stock.stocktake |

### `store_ordering`

| endpoint | 方法 | 规则 | 鉴权 | 明细 |
|---|---|---|---|---|
| `store_ordering.add_to_cart` | POST | `/cart/add` | 权限点 | order.create |
| `store_ordering.admin_orders` | GET | `/admin/orders` | 角色 | admin |
| `store_ordering.admin_report` | GET | `/admin/report` | 角色 | admin |
| `store_ordering.cancel_order_route` | POST | `/orders/<int:order_id>/cancel` | 任一权限 | order.create|order.cancel.any |
| `store_ordering.cart_add_batch` | POST | `/cart/add-batch` | 权限点 | order.create |
| `store_ordering.cart_view` | GET | `/cart` | 登录 |  |
| `store_ordering.catalog` | GET | `/catalog` | 登录 |  |
| `store_ordering.clear_cart_route` | POST | `/cart/clear` | 权限点 | order.create |
| `store_ordering.deliver_order_route` | POST | `/orders/<int:order_id>/deliver` | 权限点 | order.ship |
| `store_ordering.manage_dc_items` | GET,POST | `/dc/items` | 配送中心 | role:manager |
| `store_ordering.manage_shipping_rule` | GET,POST | `/admin/shipping` | 角色 | admin |
| `store_ordering.order_detail` | GET | `/orders/<int:order_id>` | 登录 |  |
| `store_ordering.receive_order_route` | POST | `/orders/<int:order_id>/receive` | 权限点 | order.receive |
| `store_ordering.remove_cart_item_route` | POST | `/cart/remove/<int:cart_item_id>` | 权限点 | order.create |
| `store_ordering.review_list` | GET | `/review` | 配送中心 | role:staff |
| `store_ordering.review_order_route` | POST | `/orders/<int:order_id>/review` | 配送中心 | perm:order.approve |
| `store_ordering.ship_order_route` | POST | `/orders/<int:order_id>/ship` | 配送中心 | role:staff |
| `store_ordering.shipment_list` | GET | `/shipments` | 配送中心 | role:staff |
| `store_ordering.store_orders_list` | GET | `/orders` | 登录 |  |
| `store_ordering.submit_order_route` | GET,POST | `/cart/submit` | 权限点 | order.create |
| `store_ordering.update_cart_item` | POST | `/cart/update/<int:cart_item_id>` | 权限点 | order.create |

### `users`

| endpoint | 方法 | 规则 | 鉴权 | 明细 |
|---|---|---|---|---|
| `users.bind_role` | POST | `/users/<int:user_id>/bind` | 平台超管 |  |
| `users.bulk_bind` | POST | `/users/bulk-bind` | 平台超管 |  |
| `users.change_role` | POST | `/users/<int:user_id>/change-role` | 平台超管 |  |
| `users.create_user` | POST | `/users/create` | 平台超管 |  |
| `users.create_warehouse` | POST | `/warehouses/create` | 平台超管 |  |
| `users.delete_user` | POST | `/users/<int:user_id>/delete` | 平台超管 |  |
| `users.init_warehouse` | POST | `/warehouses/<int:warehouse_id>/init` | 平台超管 |  |
| `users.list_users` | GET | `/users` | 平台超管 |  |
| `users.reset_password` | POST | `/users/<int:user_id>/reset-password` | 平台超管 |  |
| `users.toggle_active` | POST | `/users/<int:user_id>/toggle-active` | 平台超管 |  |
| `users.toggle_admin` | POST | `/users/<int:user_id>/toggle-admin` | 平台超管 |  |
| `users.unbind_role` | POST | `/users/<int:user_id>/unbind` | 平台超管 |  |

## 3. 数据层

- `WAREHOUSE_SCHEMA_VERSION = 2`（改 ALTER 块须 bump，见 ARCHITECTURE.md §4）
- 表 59 张：

- `adjustment_requests` · `agent_tokens` · `approval_policies` · `audit_log`
- `canonical_categories` · `canonical_claim_requests` · `canonical_conflicts` · `canonical_item_aliases`
- `canonical_items` · `canonical_publish_event_items` · `canonical_publish_event_warehouses` · `canonical_publish_events`
- `categories` · `daily_revenue` · `feedback_comments` · `feedback_tickets`
- `forecast_runs` · `ic_recipe_items` · `ic_recipes` · `item_publish_event_items`
- `item_publish_event_warehouses` · `item_publish_events` · `items` · `login_throttle`
- `notification_prefs` · `notifications` · `outbound_requests` · `permission_routes`
- `permissions` · `procurement_cache` · `procurement_config` · `product_bom`
- `production_run_items` · `production_runs` · `products` · `recipe_items`
- `recipe_publish_event_warehouses` · `recipe_publish_events` · `recipe_versions` · `recipe_versions_new`
- `recipes` · `restock_requests` · `role_permissions` · `roles`
- `shipping_rules` · `stock_movements` · `stocktake_batches` · `stocktakes`
- `store_order_cart_items` · `store_order_carts` · `store_order_deliveries` · `store_order_items`
- `store_order_receipts` · `store_order_status_history` · `store_orders` · `tenants`
- `users` · `warehouse_users` · `warehouses`

## 4. MCP 工具域

| 域 | 导出函数数 |
|---|---|
| `canonical` | 13 |
| `consumption` | 2 |
| `forecast` | 1 |
| `inbound` | 2 |
| `inventory` | 4 |
| `outbound` | 3 |
| `procurement` | 2 |

## 5. 未装配 / 未登记（死代码与漏网）

**未装配蓝图的路由**（所属蓝图未注册到 app，访问即 404，属死代码；当前：`adjustment`）

- `adjustment.adjustment_list` ← `/adjustment` (GET)
- `adjustment.adjustment_session` ← `/adjustment/session` (GET)
- `adjustment.adjustment_submit` ← `/adjustment/submit` (POST)
- `adjustment.delete` ← `/adjustment/<int:req_id>/delete` (POST)
- `adjustment.rollback` ← `/adjustment/<int:req_id>/rollback` (POST)

**矩阵已登记、代码中未解析到 URL 规则**（应用级路由或动态注册，非缺陷）

- `health`
- `static`

---

生成方式：`scripts/arch_map.py`（纯 stdlib、不连库、不 import app）。
数据来源与权威性说明见脚本头部 docstring。
