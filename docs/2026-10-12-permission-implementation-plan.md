# DailyCheck 权限精细化 — Phase 2 实施施工图

> 产出：MVP 开发专家团 · 首席架构师 高见远
> 日期：2026-10-13
> 依据：Spec `docs/2026-10-12-permission-spec.md`（已锁定）+ 架构 `docs/2026-10-12-permission-*.md`（含 §3.7 成本类字段边界清单）
> 定位：**Phase 3 后端工程师的唯一照做依据** —— 精确到"改哪个文件 / 哪个函数 / 加什么"
> 基线：`main @ 75af9c6`（含用户管理 v2）｜本文件为**独立新文件**，不改动三文档
> 生成方法：`app.url_map` 运行时枚举（153 条）+ 全 `blueprints/*.py` AST 静态解析守卫栈（`/tmp/route_ast.py`）双向交叉核对

---

## 0. 使用说明与前提

**版本锚定（照此写，勿凭印象）**：Python 3.13.x｜Flask 3.1.1｜gunicorn 23.0.0｜SQLite（标准库 sqlite3）｜**零新增第三方依赖**。

**本地运行环境提示（施工/测试必读）**：仓库 `.venv` 的 `site-packages` 位于 `python3.12` 目录，而 `.venv/bin/python` 是 3.13，二者不匹配。可用以下方式跑脚本/测试：

```bash
cd /Users/ericmr/Documents/GitHub/DailyCheck
PYTHONPATH=.venv/lib/python3.12/site-packages /opt/homebrew/bin/python3.12 <script.py>
```

**七条硬约束（违反即不合格，来自 Spec §4）**：① 禁新增依赖；② 只走 `init_master_db()` 幂等迁移 + 同步 `tests/test_master_db_migrate.py`；③ 权限解析 fail-closed；④ 权限集每请求解析、禁进程缓存；⑤ `warehouse_users.role` 保持 code 字符串；⑥ `is_admin=1` 超管语义不变；⑦ 架构子系统改动同步 `docs/ARCHITECTURE.md`。

**术语**：`PERM` = 权限点；`PLATFORM` = `@require_platform_admin`（仅平台超管）；`LOGIN` = 仅登录（`@require_login`）；`兼容层` = 本期保留 `require_role(...)` 不改写；`定级待复核` = 行为发生变化、**必须经 Eric 复核**后方可实施。

---

## 1. ROUTE_MATRIX 全量清单

### 1.0 统计概览

| 指标 | 数值 | 说明 |
|---|---|---|
| 全量已注册路由 | **153** | `app.url_map` 实时枚举（含 `static` + `health`）；assessment 记为 156（该数值为早期静态估算，**以本清单为准**） |
| 公开（无守卫） | 6 | `login` / `logout` / `sw.js` / `manifest.webmanifest` / `static` / `health` |
| 仅登录 `LOGIN` 且为**读** | 39 | 页面渲染，保持 |
| 仅登录 `LOGIN` 且为**写/敏感读** | 21 | §1.3 逐条定级 |
| `require_platform_admin` | 26 | 保持（等价） |
| `require_role` 显式守卫 | 47 | §1.2 迁移映射 |
| 死装饰器叠加（platform_admin + require_role） | **5** | §1.2 清除 |
| `require_warehouse_type` 叠加 | 5 | DC 侧路由，保留类型守卫 |

> **列值约定**：意图列取值 `public` / `login` / `PERM:<code>` / `PLATFORM` / `兼容层:role:<x>`；迁移动作列取值 `保持` / `等价迁移` / `新增守卫` / `定级待复核` / `清除死装饰器`。

### 1.1 公开路由（public，6 条，全部保持）

| endpoint | method | 意图 | 迁移动作 | 备注 |
|---|---|---|---|---|
| `auth.login` | GET,POST | public | 保持 | `/login` |
| `auth.logout` | GET | public | 保持 | `/logout` |
| `pwa.service_worker` | GET | public | 保持 | `/sw.js` |
| `pwa.webmanifest` | GET | public | 保持 | `/manifest.webmanifest` |
| `static` | GET | public | 保持 | `/static/<path>` |
| `health` | GET | public | 保持 | `/health`（限速豁免） |

### 1.2 现有守卫路由迁移映射（82 条）

**A. 死装饰器清除（5 处，`清除死装饰器`，行为不变）** —— `@require_platform_admin` 在外层已使内层 `@require_role(...)` 恒不生效（平台超管 bypass，非超管被外层 403）。

| endpoint | method | 现状装饰器 | 意图（清后） | 迁移动作 | 备注 |
|---|---|---|---|---|---|
| `items.items_list` | GET,POST | `require_platform_admin > require_role("manager")` | PLATFORM | 清除死装饰器 | **终裁③**：维持平台级，删 `require_role("manager")`，对外语义零变化 |
| `items.edit_item` | GET,POST | `require_platform_admin > require_role("manager")` | PLATFORM | 清除死装饰器 | **终裁③**：维持平台级，同上 |
| `canonical.canonical_edit`（×2 路由） | GET,POST | `require_platform_admin > require_role("manager")` | PLATFORM | 清除死装饰器 | 删 `require_role("manager")` |
| `forecast.forecast_index` | GET | `require_platform_admin > require_role("manager")` | PLATFORM | 清除死装饰器 | 删 `require_role("manager")` |
| `procurement.procurement_store` | GET | `require_platform_admin > require_role("staff")` | PLATFORM | 清除死装饰器 | 删 `require_role("staff")` |

**B. 平台管理员路由（26 条，`保持`，行为等价；`admin.*` 名义权限，实际端点维持 `@require_platform_admin`）**

| 组 | endpoints | 意图 | 备注 |
|---|---|---|---|
| users.py（12） | `list_users` / `create_user` / `bind_role` / `unbind_role` / `change_role` / `reset_password` / `toggle_admin` / `toggle_active` / `bulk_bind` / `delete_user` / `create_warehouse` / `init_warehouse` | PLATFORM | 名义 `admin.user.manage` / `admin.warehouse.manage`；**保持 `@require_platform_admin` 等价**（Spec §5） |
| agent_tokens.py（3） | `list_tokens` / `create_token` / `revoke_token` | PLATFORM | 内联 `_require_admin()` → 改 `@require_platform_admin` 装饰器（等价迁移）；名义 `admin.token.manage` |
| items.py（4） | `delete_item` / `toggle_item_active` / `recompute_safety_stock` / `items_publish` | PLATFORM | `delete_item` 见 §1.2-C；其余保持 |
| recipe_cost.py（9） | `ic_recipe_new/edit/delete/publish`、`recipe_new/edit/delete/publish`、`update_selling_price` | PLATFORM | 保持（名义 `item.price.edit`） |
| production.py（3） | `product_new` / `product_edit` / `product_delete` | PLATFORM | 保持 |
| notifications.py（1） | `test_emit` | PLATFORM | 保持 |

**C. 敏感操作终裁（已锁定）**

| endpoint | method | 现状 | 终裁意图 | 旧→新行为对照 | 结论 |
|---|---|---|---|---|---|
| `items.delete_item` | POST | `require_platform_admin` | PLATFORM | 仅平台超管 → 仅平台超管 | **终裁②：维持平台级**，不进任何角色包；路由保持 `@require_platform_admin` |
| `stocktake.approve` | POST | `require_role("staff")` | `PERM:stock.stocktake.approve` | 现状=staff/manager/admin 均可审批 → **manager/admin 可批，staff 失去** | **终裁①：收紧至 manager+**（`stock.stocktake.approve` ∈ manager 包=17）；**授权行为变化**，已由 Eric 授权 |

> ⚠ **重要修正（相对 assessment）**：assessment §1.2 称"盘点 admin 审核（stocktake.py:102-204）与 staff 操作分离"——经源码核验，`stocktake.py:102-204` 实为 **CSV 导入（admin）**，而 `approve`（`stocktake.py:476-478`）当前是 `@require_role("staff")`。即"盘点审批"**当前并无 elevated 守卫**；终裁①据此收紧。

**D. 兼容层路由（本期保留 `require_role`，不迁移；无对应权限点，迁移越界）**

| 组 | endpoints | 现状守卫 | 说明 |
|---|---|---|---|
| canonical 管理 | `canonical_claim` / `canonical_unclaim` / `canonical_review_claim_request` / `canonical_resolve_conflict` | `require_role("manager")` | 主数据 claim 流程；25 点未覆盖，**保持兼容层** |
| canonical 管理 | `canonical_bulk_import` / `canonical_fanout`（×2） | `require_role("admin")` | 同上 |
| import_items | `upload_form` / `upload_parse` / `preview` / `commit` | `require_login > require_role("admin")` | 批量导入；保持 |
| stocktake CSV | `stocktake_csv_form` / `_parse` / `_preview_view` / `_commit` | `require_login > require_role("admin")` | 保持 |
| forecast | `forecast_item` / `forecast_product` / `forecast_recompute` | `require_role("manager")` | 保持 |
| procurement | `procurement_hub` | `require_role("manager")` | 保持 |
| procurement | `procurement_accept` / `procurement_accept_download` | `require_role("staff")` | 保持 |
| store_ordering 管理 | `manage_shipping_rule` / `admin_report` / `admin_orders` | `require_login > require_role("admin")` | 保持 |
| store_ordering DC | `review_list` / `shipment_list` | `login > require_warehouse_type("distribution_center") > require_role("staff")` | **保留类型守卫** |
| store_ordering DC | `manage_dc_items` | `login > DC > require_role("manager")` | 保留 |
| reports | `api_upload_revenue` | `require_login > require_role("manager")` | 营业额写；无写点，保持兼容层 |

**E. `require_role` 等价迁移为权限点（`等价迁移`，现有用户零变化）**

| endpoint | method | 现状 | 新意图 | 对照 |
|---|---|---|---|---|
| `items.new_item_redirect` | GET | `require_role("manager")` | `PERM:item.create` | manager 含 item.create，等价 |
| `items.new_item_redirect_submit` | POST | `require_role("manager")` | `PERM:item.create` | 等价 |
| `outbound.rollback` | POST | `require_role("staff")` | `PERM:stock.outbound.rollback` | staff 含该点，等价 |
| `stocktake.rollback` | POST | `require_role("staff")` | `PERM:stock.stocktake` | 等价 |
| `stocktake.edit` | GET | `require_role("staff")` | `PERM:stock.stocktake` | 等价 |
| `stocktake.submit_edit` | POST | `require_role("staff")` | `PERM:stock.stocktake` | 等价 |
| `restock.delete` | POST | `require_role("staff")` | `PERM:stock.restock` | 等价 |
| `adjustment.rollback` | POST | `require_role("staff")` | `PERM:stock.adjust` | 等价 |
| `adjustment.delete` | POST | `require_role("staff")` | `PERM:stock.adjust` | 等价 |
| `core.delete_category` | POST | `require_role("staff")` | 兼容层（保持） | 无对应点 |
| `production.rollback` | POST | `require_role("staff")` | 兼容层（保持） | 无对应点 |
| `production.delete_run` | POST | `require_role("staff")` | 兼容层（保持） | 无对应点 |
| `store_ordering.review_order_route` | POST | `login > DC > require_role("manager")` | `PERM:order.approve` + 保留 DC | 等价（AC 示例） |
| `store_ordering.ship_order_route` | POST | `login > DC > require_role("staff")` | **兼容层（保持 `require_role("staff")`）+ 保留 DC** | ⚠ **施工修正**：原判"等价"有误——`order.ship ∈ manager` 包，迁移会让 **DC staff 失去发货能力**（非白名单回归）。故维持兼容层；见 §6 修正记录 |

> 说明：`E` 组中标记"兼容层（保持）"者（`core.delete_category` / `production.*`）**无对应权限点**，本期不动，避免为凑点而造点。

### 1.3 "仅登录"写操作 / 敏感读 定级表（21 条，含旧/新行为对照）

> 依据 Spec §2.1 拍板：`outbound:70` 显式开放（行为不变）；`report.view` 维持现状；`report.cost.view` 收紧至 manager+；其余等价。

| # | endpoint | method | 旧守卫 | 新意图 | 行为对照（旧→新） | 结论 |
|---|---|---|---|---|---|---|
| 1 | `agent_tokens.create_token` | POST | login + 内联 `_require_admin` | PLATFORM | 平台超管 → 平台超管 | 等价（内联→装饰器） |
| 2 | `agent_tokens.revoke_token` | POST | login + 内联 `_require_admin` | PLATFORM | 同上 | 等价 |
| 3 | `outbound.outbound_submit` | POST | login | `PERM:stock.outbound` | 已登录 → 基础包（staff+） | **显式化，行为不变**（Eric 拍板） |
| 4 | `restock.restock_submit` | POST | login | `PERM:stock.restock` | 已登录 → staff+ | 现有用户不变；viewer 被拦 |
| 5 | `restock.update_status` | POST | login | `PERM:stock.restock` | 同上 | 同上 |
| 6 | `stocktake.stocktake_submit` | POST | login | `PERM:stock.stocktake` | 已登录 → staff+ | 现有用户不变 |
| 7 | `adjustment.adjustment_submit` | POST | login | `PERM:stock.adjust` | 已登录 → staff+ | 现有用户不变 |
| 8 | `production.submit` | POST | login | （无对应点） | 已登录 → 已登录 | **残留仅登录写**；advisory：需补点或显式接受 |
| 9 | `store_ordering.add_to_cart` | POST | login | `PERM:order.create` | 已登录 → staff+ | 现有用户不变 |
| 10 | `store_ordering.cart_add_batch` | POST | login | `PERM:order.create` | 同上 | 同上 |
| 11 | `store_ordering.update_cart_item` | POST | login | `PERM:order.create` | 同上 | 同上 |
| 12 | `store_ordering.remove_cart_item_route` | POST | login | `PERM:order.create` | 同上 | 同上 |
| 13 | `store_ordering.clear_cart_route` | POST | login | `PERM:order.create` | 同上 | 同上 |
| 14 | `store_ordering.submit_order_route` | GET,POST | login | `PERM:order.create` | 同上 | 同上 |
| 15 | `store_ordering.cancel_order_route` | POST | login（handler 内 owner/manager 矩阵） | `require_any_permission("order.create","order.cancel.any")` + 保留 handler 矩阵 | 已登录 → owner（持 `order.create`）或持 `order.cancel.any` 者 | **定级待复核**（见下方缺口） |
| 16 | `store_ordering.deliver_order_route` | POST | login | `PERM:order.ship` | 已登录 → staff+ | 现有用户不变 |
| 17 | `store_ordering.receive_order_route` | POST | login | `PERM:order.receive` | 已登录 → staff+ | 现有用户不变 |
| 18 | `reports.outbound` | GET（敏感读） | login | `PERM:report.view` | 已登录 → staff+（`report.view` ∈ staff/viewer） | **维持现状**（Eric 拍板） |
| 19 | `reports.inbound` | GET（敏感读） | login | `PERM:report.view` | 同上 | 维持现状 |
| 20 | `reports.export_consumption` | GET（敏感读） | login | `PERM:report.export` + 成本列脱敏 | 已登录 → staff+（`report.export` ∈ staff）；成本列默认脱敏 | 脱敏=**有意变化**（§10 白名单①） |
| 21 | `reports.export_summary` | GET（敏感读） | login | `PERM:report.export` + 成本列脱敏 | 同上 | 同上 |

> **session 启动类（非数据写，保持 LOGIN）**：`outbound.outbound_start`、`restock.restock_start`、`stocktake.stocktake_start`、`production.session`。这些仅初始化会话表单，不写业务数据，**保持 `@require_login`**。
>
> **⚠ 缺口（`order.cancel.any` 无包归属）**：`order.cancel.any` 是 25 点之一，但 Spec 附录 A 的 5 个内置包**均未包含它**（`manager=16` 已锁定此约束，与 `admin.*` 5 点同属"仅平台超管"）。因此 #15 取消路由**不能用 `@require_permission("order.cancel.any")` 单点把闸**——那会把 staff 自撤（owner）与 manager 一起挡在门外，构成回归。**正确处置**：`@require_any_permission("order.create","order.cancel.any")`，使 order owner（持 `order.create` ∈ staff）照旧可撤，`order.cancel.any` 作为"越权撤他人单"的提权点（实际持有者仅平台超管 bypass）。**请 team-lead 确认**：`order.cancel.any` 是否应并入 `manager` 包（若并，manager 契约数 16→17，需同步改 §2.4 与附录 A）；本施工图暂按"不并入"实现。
>
> **内部归属类（保持 LOGIN，handler 内数据范围）**：`notifications.mark_notification_read`（owner 校验）、`canonical.canonical_submit_claim_request`（提交 claim）。
>
> **残留项**：`production.submit`（#8）为唯一"仅登录写且无对应权限点"项，advisory 提请注意；不得为凑点造点。

### 1.4 全量路由清单（按 blueprint 分组）

> 只列 `PERM` / `PLATFORM` / `兼容层` / `新增守卫` / `定级待复核` 的**承重行**；纯 `login` 读路由归入 §1.1/§1.3 已覆盖。完整 153 行原始数据见 `/tmp/route_dump.tsv`（endpoint/method/path）。

**agent_tokens**（3）：`list_tokens` PLATFORM 等价迁移｜`create_token` PLATFORM 等价迁移｜`revoke_token` PLATFORM 等价迁移

**auth / pwa**（6）：全部 public 保持（`login`/`logout`/`warehouse_picker`/`warehouse_select`/`sw.js`/`manifest.webmanifest`）—— note：`warehouse_picker`/`warehouse_select` 是 `require_login` 且属 `WAREHOUSE_EXEMPT`，保持。

**canonical**（25）：`canonical_edit`×2 清除死装饰器(PLATFORM)｜`binding_active`/`batch_edit`/`backfill_prices` PLATFORM 保持｜`claim`/`unclaim`/`review_claim_request`/`resolve_conflict` 兼容层 manager｜`bulk_import`/`fanout`×2 兼容层 admin｜其余读 login 保持

**core**（6）：`summary` LOGIN 保持（终裁④不脱敏）｜`dashboard`/`land`×2/`categories` LOGIN 保持｜`delete_category` 兼容层 staff

**forecast**（4）：`forecast_index` 清除死装饰器(PLATFORM)｜`forecast_item`/`forecast_product`/`forecast_recompute` 兼容层 manager

**import_items**（4）：全部兼容层 `login>admin` 保持

**items**（10）：`items_list`/`edit_item` 清除死装饰器（终裁③维持平台级）｜`delete_item` 维持平台级（终裁②）｜`toggle_item_active`/`recompute_safety_stock`/`items_publish` PLATFORM 保持｜`items_publish_history`×2 LOGIN 保持｜`new_item_redirect`/`_submit` 等价迁移 item.create｜`inventory_view` LOGIN 保持（终裁④不脱敏）

**notifications**（3）：`notifications_feed` LOGIN｜`mark_notification_read` LOGIN(owner)｜`test_emit` PLATFORM

**outbound**（6）：`outbound_list`/`outbound_session` LOGIN｜`outbound_start` LOGIN(session)｜`outbound_submit` 新增 stock.outbound｜`rollback` 等价迁移 stock.outbound.rollback｜`delete` 兼容层 staff

**procurement**（4）：`procurement_store` 清除死装饰器(PLATFORM)｜`procurement_hub` 兼容层 manager｜`procurement_accept`/`_download` 兼容层 staff

**production**（10）：`products_list`/`session`/`runs_list`/`runs_csv` LOGIN｜`product_new`/`product_edit`/`product_delete` PLATFORM｜`submit` 残留 LOGIN(advisory)｜`rollback`/`delete_run` 兼容层 staff

**recipe_cost**（19）：写路由 PLATFORM 保持（`ic_recipe_new/edit/delete/publish`、`recipe_new/edit/delete/publish`、`update_selling_price`）｜读路由：`landing`/`ic_recipes_list`/`recipes_list`/`recipe_versions_history` LOGIN 保持；`api_cost` 与 `*_export_csv`×4 **按 R1 终裁收紧至 report.cost.view（见 §7 M6）**

**reports**（5）：`outbound`/`inbound` 新增 report.view（无成本列）｜`export_consumption`/`export_summary` 新增 report.export + 成本脱敏｜`api_upload_revenue` 兼容层 manager

**restock**（6）：`restock_list`/`restock_session` LOGIN｜`restock_start` LOGIN(session)｜`restock_submit`/`update_status` 新增 stock.restock｜`delete` 等价迁移 stock.restock

**stocktake**（12）：CSV 4 路由 兼容层 login>admin｜`stocktake_list`/`stocktake_session` LOGIN｜`stocktake_start` LOGIN(session)｜`stocktake_submit` 新增 stock.stocktake｜`rollback`/`edit`/`submit_edit` 等价迁移 stock.stocktake｜`approve` **收紧迁移 stock.stocktake.approve（终裁①，manager+）**

**store_ordering**（21）：`catalog`/`cart_view`/`store_orders_list`/`order_detail` LOGIN｜cart 写×7 新增 order.create｜`cancel_order_route` any(order.create|cancel.any)（M4）｜`deliver_order_route` 新增 order.ship（M2：零变化）｜`receive_order_route` 新增 order.receive｜`review_order_route` 等价迁移 order.approve(+DC)｜`ship_order_route` **维持兼容层 role:staff(+DC)（M1）**｜`review_list`/`shipment_list` 兼容层(login>DC>staff)｜`manage_dc_items` 兼容层(login>DC>manager)｜`manage_shipping_rule`/`admin_report`/`admin_orders` 兼容层 login>admin

**users**（12）：全部 PLATFORM 保持

---

## 2. DB 施工细节

### 2.1 `db/__init__.py` 改动点位（行号级）

| # | 位置 | 改动 | 说明 |
|---|---|---|---|
| 1 | `MASTER_SCHEMA`（第 66 行起，字符串止于**第 549 行 `"""`**） | 在**第 548 行 `"""` 之前**追加 4 张表 DDL（`permissions` / `roles` / `role_permissions` / `permission_routes`）+ `approval_policies`（空表） | `CREATE TABLE IF NOT EXISTS`；DDL 原文见架构 §3.2。对存量 master.db 为 no-op 追加 |
| 2 | `init_master_db()` 内 `agent_tokens.rate_limit` 迁移块之后（**第 830-838 行附近**） | 追加 PRAGMA 守卫列迁移：`permission_codes_json TEXT`（可空，NULL=legacy） | **仿 `rate_limit` 列先例**（第 830-838 行）：`at_cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_tokens)")...}; if "permission_codes_json" not in at_cols: conn.execute("ALTER TABLE agent_tokens ADD COLUMN permission_codes_json TEXT")` |
| 3 | `init_master_db()` 内经 `users.is_active` 迁移块之后（**第 875 行之后**，仍在 `fcntl.flock` 块内） | 插入播种调用 `_seed_permissions_and_roles(conn)` | 必须**在 flock 块内**（第 777-778 行锁），防 gunicorn 多 worker 并发播种 |
| 4 | `init_master_db()` 播种后同块内 | 插入启动断言 `_verify_builtin_packages(conn)`，返回告警文本，交 `logging.getLogger(__name__).warning(...)` 输出 | **空包**覆盖全部 5 包；**阶梯**仅 staff⊆manager⊆admin 三包；**权限数契约** 10/16/19/3/4；`admin.*` 5 点不得入任何包 → 告警，不 raise（防启动失败） |
| 5 | `db/__init__.py` 新增模块级函数 | `_seed_permissions_and_roles(conn)` / `reseed_builtin_roles(conn)` / `_verify_builtin_packages(conn)` | 见 §2.2 签名 |
| 6 | 模块顶部新增 import | `from permissions_manifest import PERMISSION_CATALOG, ROLE_PACKAGES` | 注意与 `permissions.py` 无循环导入（manifest 只放数据、无 Flask 依赖） |

> 位置锚点复核：`MASTER_SCHEMA` = 第 66-549 行；`WAREHOUSE_SCHEMA` = 556-754；`init_master_db` = 757-957；`init_warehouse_db` = 958；`WAREHOUSE_SCHEMA_VERSION` = 1001；`migrate_warehouse_db_columns` = 1004。**仓库库（warehouse db）不改动**（权限是平台级，全在 master.db）。

### 2.2 播种 / reseed 接口定义

```python
# db/__init__.py —— 模块级函数（供 init_master_db / CLI 复用）

def _seed_permissions_and_roles(conn: sqlite3.Connection) -> None:
    """幂等播种：权限点目录 + 5 内置角色 + 角色-权限包。
    - permissions:   INSERT OR IGNORE（来源 PERMISSION_CATALOG）
    - roles:         INSERT OR IGNORE（5 内置，is_builtin=1, rank=1/2/3/None/None, tenant_id=1）
    - role_permissions: INSERT OR IGNORE（来源 ROLE_PACKAGES）
    - permission_routes: INSERT OR IGNORE（权限点→HTTP 路由桥接，Phase B 用）
    重复调用无副作用；不删除、不更新既有行。
    """

def reseed_builtin_roles(conn: sqlite3.Connection) -> None:
    """显式重播种内置角色包（`INSERT OR IGNORE` 不会更新已有行）。
    步骤：① 删内置角色（is_builtin=1）的 role_permissions；
          ② 重新 INSERT 全部内置包；③ 不动 is_builtin=0 的自定义角色。
    由 CLI `reseed-builtin-roles` 调用。"""

def _verify_builtin_packages(conn: sqlite3.Connection) -> list[str]:
    """启动断言，返回告警文本列表（空 = 正常）：
      - 空包（覆盖全部 5 包 staff/manager/admin/viewer/finance）：任一包权限数 == 0
      - 阶梯破坏（**仅阶梯三包**）：staff ⊄ manager 或 manager ⊄ admin
      - 旁支校验：viewer/finance 的 rank 必须为 NULL 且不夹在阶梯内
      - 权限数契约：各包权限数必须 == BUILTIN_PACKAGE_SIZES（见 §2.4）
      - 越界点：admin.* 5 点不得出现在任何仓库角色包
      - 孤儿权限点：ROLE_PACKAGES 中的 code 不在 permissions 表
    不 raise；由调用方写日志。"""
```

**CLI（`cli.py`）**：

```python
@click.command("reseed-builtin-roles")
def reseed_builtin_roles_cmd() -> None:
    """重建 5 个内置角色包的权限映射（不动自定义角色）。"""
    from db import reseed_builtin_roles
    with closing(sqlite3.connect(MASTER_DB)) as conn:
        reseed_builtin_roles(conn); conn.commit()
    click.echo("内置角色包已重播种")

# 在 register_cli(app)（cli.py:45-64）追加：
#   app.cli.add_command(reseed_builtin_roles_cmd)
```

> 命令名对应 Spec §6 的 `--reseed-builtin-roles`；flask CLI 形态为 `flask reseed-builtin-roles`。

### 2.3 启动断言位置

`init_master_db()`（第 777-778 行 `fcntl.flock` 块内）播种后调用 `_verify_builtin_packages(conn)`：

```python
warnings = _verify_builtin_packages(conn)
for w in warnings:
    logging.getLogger("db").warning("[permissions] %s", w)
```

**为何不 raise**：空包会导致全站 403（沉默失败），但 raise 会让 gunicorn 拒绝启动、放大故障面；采用"告警 + 继续"（Spec §6"启动断言…防空包全站 403 沉默失败"），并由 `tests/test_route_permission_matrix.py` 的 Test C 在 CI 兜底。

### 2.4 内置角色包契约（播种断言 + 测试核对基准）

**权限数契约**（设计/前端/测试统一按此核对；定义于 manifest 常量 `BUILTIN_PACKAGE_SIZES`）：

| 包 code | 中文名 | 权限数 | 阶梯归属 | rank | 初始权限集（Spec 附录 A） |
|---|---|---|---|---|---|
| `staff` | 基础作业 | **10** | 阶梯① | 1 | order.create / order.receive / stock.outbound / stock.outbound.rollback / stock.stocktake / stock.adjust / stock.restock / item.view / report.view / report.export |
| `manager` | 管理审批 | **17** | 阶梯② | 2 | staff ∪ order.approve / order.ship / item.edit / item.create / item.price.view / report.cost.view / **stock.stocktake.approve**（终裁①） |
| `admin` | 仓管理 | **18** | 阶梯③ | 3 | manager ∪ item.price.edit（`item.delete` 已移出，终裁②） |
| `viewer` | 只读/审计 | **3** | **旁支** | **NULL** | item.view / report.view / report.export |
| `finance` | 财务 | **4** | **旁支** | **NULL** | item.view / report.view / report.export / report.cost.view |

**关键约定（写入播种断言与 Test C）**：
1. **阶梯检查只针对 `staff ⊆ manager ⊆ admin` 三包**（10 ⊆ 17 ⊆ 18，均成立）；`viewer` / `finance` 是**旁支**，`rank=NULL`，**不参与**阶梯包含关系（viewer⊆staff、finance⊆manager 是巧合而非契约，不得据此断言）。
2. **空包告警覆盖全部 5 包**（任一为 0 即告警）。
3. **平台级点（不进任何角色包）**：`admin.*` 5 点 + `item.delete`（终裁②）+ `order.cancel.any`（无包归属）；断言 `platform_only ∩ (任一包) == ∅`。
4. 权限数不符契约（≠ 10/17/18/3/4）→ 启动告警（防"改包漏改"的沉默漂移）。
5. 25 点中 `admin.*` 5 点 + `item.delete` + `order.cancel.any` 共 7 点不在任何包——见 §1.3 #15 取消路由处置（`order.cancel.any` 用 `require_any_permission` 兜 owner 自撤）。

---

## 3. `permissions.py` 新增函数行为规格

现有 6 装饰器（`require_login` / `require_role` / `require_platform_admin` / `require_warehouse_type` + `storefront_only` / `distribution_center_only`）**签名与行为 100% 不变**。新增如下：

### 3.1 `current_permissions() -> frozenset[str]`

```python
def current_permissions() -> frozenset[str]:
    """返回当前请求在某仓的权限点集合。

    取值顺序：
      1. g.user is None            → frozenset()（空集）
      2. g.user["is_admin"] == 1   → 特殊哨兵（见下），调用方一律放行
      3. 未选仓（g.get("warehouse_db_path") is None 或 g.get("warehouse") is None）
                                   → frozenset()
      4. 否则：一次 JOIN 查 master.db（每请求一次，结果缓存到 g.perm_set）

    实现（请求内缓存，绝不进程级缓存）：
      if "perm_set" in g: return g.perm_set
      ... 查询 ...
      g.perm_set = frozenset(...)
      return g.perm_set
    """
```

- **SQL**（参数化，防注入；tenant 前向兼容）：
  ```sql
  SELECT DISTINCT rp.permission_code
    FROM warehouse_users wu
    JOIN roles r            ON r.code = wu.role
                           AND (r.is_builtin = 1 OR r.tenant_id = ?)   -- ? = g.tenant_id
    JOIN role_permissions rp ON rp.role_id = r.id
   WHERE wu.user_id = ? AND wu.warehouse_id = ?
  ```
- **fail-closed**：任何异常（DB 错误 / JSON 解析 / 未知 code）→ 捕获 → 返回 `frozenset()` + `current_app.logger.warning("[permissions] resolve failed: %s", exc)`（**不写敏感值**）。
- **未知角色 code**（`warehouse_users.role` 在 `roles` 查不到）→ JOIN 自然得空集 → 空集 → 403 + 告警（AC-12）。

### 3.2 `require_permission(*codes) -> Callable`

```python
def require_permission(*codes: str) -> Callable:
    """全部 codes 命中才放行（AND）。语义与 require_role 对齐：

    1. g.user is None            → redirect(url_for("auth.login", next=request.path))
    2. is_admin == 1             → 放行（平台超管 bypass，行为与 require_role 一致）
    3. 未选仓 且 view 不在 WAREHOUSE_EXEMPT → redirect(url_for("auth.warehouse_picker"))
    4. codes 为空                → 视为 require_login（防误用；并 logger.warning）
    5. perms ⊇ set(codes)        → 放行
    6. 否则                      → flash(f"需要权限：{'/'.join(codes)}"); abort(403)
    """
```

### 3.3 `require_any_permission(*codes) -> Callable`

```python
def require_any_permission(*codes: str) -> Callable:
    """任一 code 命中即放行（OR）。其余语义同 require_permission。
    用于「审批员或平台管理员」「owner 或 manager」类场景。
    """
```

### 3.4 建议补充的内部工具

```python
def has_permission(*codes: str) -> bool:
    """非装饰器查询：is_admin → True；否则 codes ⊆ current_permissions()。
    供 handler 内条件分支与模板 `can()` 复用。"""
```

**UI 契约（阶段 2 F8，非本期）**：`blueprints/_helpers.py::register_template_context(app)` 追加
`app.jinja_env.globals["can"] = lambda code: bool(g.user and (g.user["is_admin"] or code in current_permissions()))`。

### 3.5 守卫收敛（`permissions.py` / `agent_tokens.py` / `users.py`）

| 动作 | 位置 | 内容 |
|---|---|---|
| 删除内联双实现 | `agent_tokens.py:51-53` `_require_admin()` | 删函数；3 处路由改 `@require_platform_admin` |
| 审计日志单一实现 | 新增 `audit_pure.py::append_admin_audit(actor, action, target, detail)` | `agent_tokens.py:185-188` 与 `users.py:835` 改为调用它 |

---

## 4. 测试用例大纲

### 4.1 `tests/test_route_permission_matrix.py`（新增）

**新夹具需求**（加在 `tests/conftest.py`，沿用 `logged_client` / `staff_client` 的 temp-db + 直设 session 模式）：
- `manager_client` —— 用户 role=`manager`
- `viewer_client` —— 用户 role=`viewer`
- `finance_client` —— 用户 role=`finance`
- 复用 `logged_client`（admin）

**Test A — 路由覆盖（AC-10）**
- `test_every_route_is_classified`：`{(r.endpoint, m) for r in app.url_map.iter_rules() for m in r.methods} - ROUTE_MATRIX.keys() == set()`，否则列出未登记路由 → **fail**。
- `test_matrix_has_no_stale_entries`：反向，manifest 无已删路由。

**Test B — deny/allow 矩阵（AC-11）**
- 参数化 `@pytest.mark.parametrize("endpoint, role, expected", matrix_cases())`，`expected` 由 `ROLE_PACKAGES[role]` × `ROUTE_MATRIX` 推导。
- 关键断言（硬编码）：
  - `item.delete` 对 `staff` → **403**（历史倒挂点）
  - `report.cost.view` 对 `staff`/`viewer` → 成本列被脱敏
  - `admin.*` 对 `staff` → **403**
  - `stock.outbound` 对 `staff` → 放行（Eric 拍板显式开放）
  - `stock.stocktake.approve` 对 `staff` → 403（定级收紧后；若 Eric 判 manager 保留则调整）
- `test_viewer_all_writes_403`：viewer 对全部写端点 → 403（AC-07）
- `test_finance_sees_cost`：finance 访问 `/export/consumption` → 成本列非空（AC-07）
- `test_cancel_allows_order_owner`：order owner（staff，持 `order.create`）撤自己单 → 非 403（验证 `require_any_permission("order.create","order.cancel.any")` 未误伤 owner）

**Test C — 反倒挂不变式（AC-13）**
- `test_ladder_packages_nested`：**仅阶梯三包** `staff ⊆ manager ⊆ admin`（**不含** viewer/finance）
- `test_viewer_finance_are_side_branches`：`viewer` / `finance` 的 `roles.rank IS NULL`，且二者**不**夹在阶梯包含链内（旁支）
- `test_builtin_package_sizes`：**精确** `{"staff":10,"manager":17,"admin":18,"viewer":3,"finance":4}`（契约核对，见 §2.4）
- `test_no_empty_builtin_package`：**全部 5 包**权限数 > 0
- `test_admin_perms_not_in_any_package`：`admin.*`（5 点） ∩（任一仓库角色包）== ∅；且 `order.cancel.any` 亦 ∉ 任何包
- `test_sensitive_perms_not_in_staff`：`{"item.delete","item.price.edit","admin.user.manage","admin.token.manage","report.cost.view","stock.stocktake.approve"} ∩ staff == ∅`
- `test_viewer_finance_have_no_write_perms`：viewer/finance 包不含任何写点

**过渡期一致性（AC-01/AC-15）**
- `test_require_role_matches_permission_package`：对迁移前用 `require_role("manager")` 的端点，断言 `require_permission(*manager_pkg)` 判定一致。

### 4.2 `tests/test_master_db_migrate.py`（新增用例，不改既有）

- `test_rbac_tables_created_on_empty_db`：空库 `init_master_db()` 后 4 表 + `approval_policies` 存在。
- `test_rbac_seed_row_counts`：`permissions` == 25；`roles` 内置 == 5；`role_permissions` 按包 **精确等于 {staff:10, manager:17, admin:18, viewer:3, finance:4}**（契约核对，见 §2.4）。
- `test_rbac_seed_idempotent`：连续两次 `init_master_db()` → 行数不变（无重复）。
- `test_agent_tokens_permission_codes_json_column_added`：旧库无该列 → init 后 PRAGMA 含 `permission_codes_json`。
- `test_verify_builtin_packages_detects_empty`：删空**任一**（含 viewer/finance）包 → `_verify_builtin_packages` 返回"空包"告警。
- `test_verify_builtin_packages_detects_broken_ladder`：破坏 `staff⊆manager`（如删 manager 的某 staff 点）→ 返回"阶梯破坏"告警。
- `test_verify_builtin_packages_detects_size_drift`：改某包权限数（≠契约）→ 返回告警。

### 4.3 存量回归（保持全绿，零新增）

`tests/test_permissions_batch1.py`、`tests/test_tenant_isolation.py` 必须全绿；总 `failed == 18`（不新增）。运行：`pytest --basetemp=/tmp/pytest-dc`。

---

## 5. 报表脱敏施工点（结合架构 §3.7 A/B/C/D/E 组）

**统一设计**：新增纯函数模块 `cost_redaction_pure.py`：

```python
COST_KEYS = {
    "unit_cost", "selling_price", "sale_price",          # §3.7-A 单价
    "consumed_value", "total_inbound_value", "total_consumed_value",
    "total_stock_value", "inbound_value", "stock_value", "avg_stock_value",
    "amount", "shipping_fee", "consumed_amount",          # §3.7-B/C 金额
}

def mask_cost(mapping: dict, keys, allow: bool) -> dict:
    """allow=False 时把 keys 命中的键值置 None（服务端脱敏，非前端隐藏）。"""

def mask_cost_csv_row(values: list, indices: set[int], allow: bool) -> list:
    """allow=False 时把指定列索引的值替换为 ''。"""
```

**判定入口**：`allow = g.user["is_admin"] or ("report.cost.view" in current_permissions())`。

### 5.1 逐端点施工点

| 端点 | 文件:函数 | 现状成本字段 | 施工 |
|---|---|---|---|
| `/report/outbound` | `reports.py:29 outbound()` | **无**（模板 `report_outbound.html` 仅品项/数量/次数，§3.7-D 数量口径） | 仅挂 `@require_permission("report.view")`；**无需脱敏**（预留 hook 注释） |
| `/report/inbound` | `reports.py:84 inbound()` | **无**（`report_inbound.html` 仅数量） | 仅挂 `@require_permission("report.view")` |
| `/export/consumption` | `reports.py:143 export_consumption()` | SQL 第 155-156 行 `unit_cost`、`consumed_value`；CSV 表头第 167 行「单价/消耗金额」 | 挂 `@require_permission("report.export")`；`allow` 为假时 `mask_cost_csv_row` 把第 5、6 列（单价、消耗金额）写 `""`（**列保留、值留空**，避免破坏列结构） |
| `/summary/export` | `reports.py:231 export_summary()` | 段落① `total_inbound/consumed/stock_value`（284-285）；段落② `inbound/consumed/stock/avg_stock_value`（293-300）；段落③ `consumed_value`（304-309）；`turnover` 为成本派生比 | 挂 `@require_permission("report.export")`；`allow` 假时把全部 ¥ 列与 `turnover_str` 写 `""`（金额+派生比率一并脱敏） |
| ~~`/summary`（页面）~~ | `core.py summary()` + `summary.html` | 含成本金额 | **终裁④：不脱敏**（保持现状，登记为口径例外，Spec 附录 B.3） |
| ~~`/inventory`（页面）~~ | `items.py inventory_view()` + `inventory.html` | 含成本金额 | **终裁④：不脱敏**（同上） |

> **§3.7 组别对应**：A 组（`unit_cost`/`selling_price`）→ consumption 报表列；B 组（`amount`/`shipping_fee`）→ 营业额/运费（不在本期脱敏端点）；C 组（派生金额列）→ summary/export；D 组（数量口径）→ outbound/inbound 报表**不脱敏**。
> **④ 例外说明**：`/summary`、`/inventory` 页成本金额对 staff 可见，**不脱敏**（Eric 终裁，登记 Spec 附录 B.3）——与 `report.cost.view`（报表/导出）口径有意不一致。

### 5.2 需 PM/Eric 裁定的关联发现（advisory，非本期强制）

1. **`/summary` 页与 `/inventory` 页**：终裁④ = **不纳入脱敏**（登记为口径例外，Spec 附录 B.3）；本期不改。
2. **`/recipe-cost/*` 读路由与 `/canonical/list`、`/canonical/detail`**：登录即可见 `unit_cost`/`selling_price`（§3.7-A），本期**保持现状**（Spec 未列入），建议后续复核。
3. **`production.runs_csv`**：CSV 导出，若含成本列需 `report.export` 口径；当前未确认，标注待查。
4. **`daily_revenue.amount` / `store_orders.shipping_fee`**（§3.7-B）：并入 `report.cost.view`（Eric 已裁定不单列）；其读入口随 #1（不脱敏）。

---

## 6. 交付校验清单（Phase 3 完成定义）

- [ ] §1.2-A 死装饰器 5 处清零；内联 `_require_admin` 归零；审计日志单一实现（AC-02）
- [ ] §1.3 定级表逐条落 `@require_permission`；`production.submit` 残留项已上报（AC-03/AC-08）
- [ ] `permissions_manifest.py` 覆盖 153 条路由；Test A 未登记即红（AC-01/AC-10）
- [ ] DB：4 表 + `agent_tokens.permission_codes_json`；播种幂等；**包权限数契约 {staff:10,manager:17,admin:18,viewer:3,finance:4}**；阶梯仅三包、空包覆盖 5 包；`test_master_db_migrate.py` 新用例全绿（AC-13）
- [ ] `permissions.py` fail-closed；`g.perm_set` 请求内缓存；未知角色 403+日志（AC-12/AC-14）
- [ ] 脱敏：`export_consumption`/`export_summary` 服务端留空；`/summary`、`/inventory` **不脱敏**（终裁④，口径例外）（AC-04/AC-05）
- [ ] 存量 5 用户 8 仓零行为变化（除白名单：①report.cost.view 收紧、③stocktake.approve 收紧至 manager+）（AC-15）
- [ ] `failed == 18` 零新增；ruff 零新增；`docs/ARCHITECTURE.md` 同步

**终裁结论（已锁定，非阻塞）**：① `stocktake.approve`→manager+；② `items.delete_item`→维持平台级；③ `/items`→维持平台级；④ `/summary`、`/inventory`→不脱敏（口径例外）；⑤ `production.submit`→维持 LOGIN + OPEN；⑥ `order.cancel.any`→暂不并入包，用 `require_any_permission` 兜 owner 自撤。

---

## 7. 施工修正记录（Phase 3 实施期间）

> 实施中发现施工图与权限包契约不一致处，按"零非白名单行为变化"红线修正，并回写本文件。

| # | 位置 | 原施工图 | 施工修正 | 依据 |
|---|---|---|---|---|
| M1 | §1.2-E `store_ordering.ship_order_route` | 迁移为 `PERM:order.ship`（判"等价"） | **维持兼容层 `login > DC > require_role("staff")`** | `order.ship ∈ manager` 包；迁移会使 **DC staff 失去发货**（`store_ordering_*` 回归 20 用例）。发货是 DC 一线作业，非白名单行为变化 → 维持。**team-lead 终裁（2026-10-13）：接受现状，代码保持不动**——`role:*` 兼容层本就有约 18 条未点化路由（`core.delete_category`、`outbound.delete`、`production.*`、`canonical_*` 等），ship 归入同一档而非孤例；若把 `order.ship` 移入 staff 包，`deliver` 的矩阵记录会失真 |
| M2 | §1.3 #16 `store_ordering.deliver_order_route` | `PERM:order.ship` | 保持 `PERM:order.ship`（不变） | 原守卫为 `@require_login`；门店"整单送达"快捷入口为 manager 动作（docstring: store manager/admin），现由 `order.ship`（manager+）把闸；现有测试以 store_mgr 调用，零回归。**team-lead 终裁（2026-10-13）：核验后为零行为变化 → 关闭**——deliver 路由体自始就有 `role not in ("manager","admin") and not _is_admin()` 检查（与 main 一致、本次未改），旧守卫仅 `require_login`，实际可达人群本就是 manager/admin，与 `order.ship ∈ manager` 一致；唯一差异是 staff 被拒方式由 flash+302 变为 403，与其他路由拒绝模式统一，接受 |
| M3 | 路由总数 | 施工图 §1.0 记 153 | 本 worktree（`main @ 75af9c6`）实际 **147**（`blueprints/users.py` 无 `bulk_bind`/`toggle_active`，属 `user-admin-v2` 分支） | manifest 以实际 `app.url_map` 为准，147/147 覆盖 |
| M4 | §1.3 #16 `store_ordering.cancel_order_route` | `require_any_permission("order.create","order.cancel.any")` | 保持（不变） | **team-lead 终裁（2026-10-13）：接受现方案 → 关闭**——`order.cancel.any` 不进任何角色包（测试已断言，属 `PLATFORM_ONLY_PERMS`）；`require_any_permission` 保留 owner（持 `order.create`）自撤能力，`order.cancel.any` 作平台级预留 |
| M5 | 缺口 A `blueprints/users.py` `VALID_ROLES` | 沿用 `set(config.ROLE_RANK.keys())`（仅阶梯三包） | 改 `set(BUILTIN_ROLE_CODES)`（manifest 单一来源，5 包） | 绑定/展示**必须接纳 viewer/finance**，否则后端校验会挡前端绑定（Spec §7）。同时 `list_users` 注入 `role_options`（模板不硬编码角色集）；`_parse_binding` 提示文案改用 manifest 顺序+显示名 |
| M6 | §1.3 `recipe_cost` 读接口（`api_cost` + 4 CSV 导出） | 5 条 `LOGIN`（`ic_recipes_export_csv`/`recipes_export_csv`/`ic_recipe_bom_export_csv`/`recipe_bom_export_csv`/`api_cost`） | 收紧为 `@require_permission("report.cost.view")`；`ROUTE_MATRIX` 5 条 intent 同步 `perm("report.cost.view")` | **R1 终裁（Eric，验证报告 R1，2026-10-13）**：配方成本属成本口径，与 `report.cost.view` 同源；staff/viewer 403、manager/finance/admin 放行。调用方仅模板导航链接（点击→403，符合收紧本意，**未额外美化**；无页面自动请求场景）。回归新增 9 用例。Spec §10 白名单 ④ |

### 7.1 Phase 3 收官裁定（team-lead 2026-10-13）

三项复核均**关闭**：① M1 ship 兼容层维持现状（接受）；② M2 deliver 零行为变化（关闭）；⑥ `order.cancel.any` 不进包、保留 `require_any_permission`（关闭）。另派两处必改缺口（A 绑定校验接纳 5 包 / B `role_options` 数据合约）与一处描述微调（C `order.ship`），均已在本次补齐（见 M5）。

### 7.2 合并对账清单（Phase 3 → 阶段 2 / 合并回 main 时核对）

1. **PR #17（`user-admin-v2`）合并 main 后**：`ROUTE_MATRIX` 需补其新增路由（`users.bulk_bind`、`users.toggle_active` 等）——否则 Test A 覆盖校验会失败（本 worktree 基于 main，仅 147 路由）。
   → ✅ **2026-10-15 已执行（并流 main `9bb9d99`）**：补登 4 条——`users.bulk_bind` / `users.toggle_active`（PR #17）+ `reports.health` / `reports.health_export`（库存健康报表，同为并流新增）；实测 `url_map` = 151 endpoint（含 `static`），Test A 双向断言通过（无遗漏 / 无残留）。
2. **`users.html` v2 与本期 v1 前端改动对账**：本期后端已注入 `role_options=[{code,label}×5]`（manifest 顺序）；v2 模板的角色下拉/toolbar 筛选/tag 渲染须以该变量为来源，不得硬编码角色集（设计师并行改 `templates/users.html`，架构侧不碰该文件）。
   → ✅ **2026-10-15 已执行（并流 main）**：`templates/users.html` 取 v2 结构并把 `role_options` 合约整体移植（`#role-options-data` JSON + 角色单元格 `data-warehouse-code`/`data-role` 属性 + 筛选器/下拉渲染 `role_options`）；`static/users-admin.js` 4 处硬编码三包列表清零（新增 `roleOptions()` 读取 JSON 源），绑定表单改读 `data-*` 属性（不再解析展示文本）；资源版本 `users-admin.js?v=4`。
3. **`order.ship` 语义**与"发货是否收紧 manager+"决议，留**阶段 2**连同自定义角色一起评审（当前 ship 走 DC 兼容层；`order.ship` 描述已核准为"DC 发货 / 门店整单交付确认"）。

### 7.3 R2 备注（receive 守卫层 vs 函数体）

`store_ordering.receive_order_route` 在 `ROUTE_MATRIX` 记录为守卫层 `perm:order.receive`，但函数体内联 `role not in ("manager","admin")` 使其**实际放行 manager+**（偏严、**非漏洞**，行为与 main 一致）。已在 `permissions_manifest.py` 的 receive 条目旁加同义注释；留**阶段 2**统一（改为纯 `order.receive` 语义，或明确收紧至 manager+）。

## 8. 规范化交付校验清单（本 worktree 实测）

- [x] `permissions_manifest.py` 覆盖 worktree 全部 endpoint；Test A 未登记即红
- [x] `permissions.py`：fail-closed + `g.perm_set` 请求内缓存 + `is_admin` sentinel + 未知 code 空集 403
- [x] `agent_tokens` 内联 `_require_admin` 归零；`users.py` 内联 `_require_admin` 归零（改 `@require_login + @require_platform_admin`）
- [x] 审计日志单一实现（`blueprints/audit_pure.py::append_admin_audit`）
- [x] 死装饰器 5 处清零（`items_list`/`edit_item`/`canonical_edit`/`forecast_index`/`procurement_store`）
- [x] DB：4 表 + `approval_policies` + `agent_tokens.permission_codes_json`；播种幂等；包权限数 {staff:10,manager:17,admin:18,viewer:3,finance:4}
- [x] 存量测试 `failed == 18` 零新增（与 `main @ 75af9c6` 基线**同一失败集**）

