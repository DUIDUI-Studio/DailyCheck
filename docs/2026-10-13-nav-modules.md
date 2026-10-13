# 导航模块化：7 模块 IA（2026-10-13）

> 需求（Eric，2026-10-13）：优化页面导航逻辑 —— 主页「采购建议」改「采购模块」、
> 「库存查阅」改「库存管理模块」、「生产录入」改「生产管理模块」，新增「管理员模块」；
> 先评估各功能页面的模块归属，交 owner 确认后实施。
> 落地分支：`feat/nav-modules`（独立 worktree，基于 `origin/main` 75af9c6）。
> 范围约束（owner 拍板）：仅功能与导航结构调整，视觉沿用现有风格。

## 1. 模块契约（7 模块）

| 模块 | 子项（页面） | 可见角色口径 |
|------|--------------|--------------|
| 总览 | `/dashboard` 总览、`/summary` 汇总 | 全体 |
| 库存管理 | `/inventory` 库存查阅、`/stocktake` 库存盘点、`/restock` 入库记录、`/outbound` 出库记录 | 平台管理员 / storefront |
| 生产管理 | `/production/...` 生产录入 + 生产记录（门店侧）；`/recipe-cost` 配方成本（rd 侧） | 平台管理员 / storefront / rd（rd 仅见配方成本） |
| 门店订货 | `/store-ordering/catalog` 订货目录；我的订单；品项管理（DC）；订货报表 / 订单管理 / 运费规则（admin） | 平台管理员 / storefront / distribution_center |
| 采购 | `/procurement/store` 采购建议、`/forecast` 预测看板 | 仅平台管理员 |
| 通知 | `/notifications` 通知中心（含未读角标 `nav-badge`） | 全体 |
| 管理员 | `/users` 账号管理、`/agent-tokens` Agent Token、`/items` 品类与品项、`/canonical` 品项主数据（admin+rd）、`/admin/import-items` 品项批量导入 | 平台管理员；rd 仅见品项主数据；单仓 role=admin 见品项批量导入 |

角色口径实现：一律从 `blueprints/_helpers.py:register_template_context` 注入的
`is_admin / is_rd / is_storefront / is_distribution_center` 判断，**禁止**在 block 内
`{% set %}`（见 ARCHITECTURE.md §13.2）。

## 2. 归属决策（Eric 2026-10-13 拍板）

| 议题 | 决策 |
|------|------|
| `/restock` 入库记录 | 归「库存管理」 |
| 品项类 3 页（`/items`、`/canonical`、`/admin/import-items`） | 归「管理员模块」 |
| `/forecast` 预测看板 | 并入「采购」模块（不再独立入口） |
| `blueprints/adjustment.py` 死代码 | 本次不动，仅登记（ARCHITECTURE.md §14 已有） |
| 首页改造方式 | 仅功能与导航结构调整，视觉沿用现有风格 |
| 开工方式 | 独立 worktree（`feat/nav-modules`） |

## 3. 三个导航载体

| 载体 | 文件 | 规则 |
|------|------|------|
| 桌面侧边栏 | `templates/base.html`（`aside.sidebar`） | 7 个 `nav-group`（模块标题 + 链接）；模块内无可见子项则整块不渲染；管理员模块置底 |
| 移动底栏 | `templates/base.html`（`nav.mobile-nav`） | ≤5 个高频模块入口 + 「更多」→ `/land`；flex 布局（条目数随角色 3~5 动态，不再固定 5 列 grid） |
| 模块选择页 | `templates/land.html`（`/land`，`no_sidebar`） | 每模块一张卡，按角色过滤；卡片描述按角色出文案（不列出该角色不可见的页） |

样式：`static/style.css` 新增 `.nav-group` / `.nav-group-title`；本页改动随 `?v=15`
（`login.html` 同步 bump `?v=11`）。

## 4. 盘点发现的「孤岛」（处置登记）

- `/store-ordering/admin/orders`（订单管理）：有页面、原无任何入口 → **本次补入侧边栏「门店订货」模块**。
- `/procurement/hub`：实为 JSON 接口（`return jsonify`）非页面 → 不建入口（评估初稿误标为「采购中枢页」，已修正）。
- `blueprints/adjustment.py`：代码在、`app.py` 未注册、路由不可达（含 2 个闲置模板）→ 仅登记（§14）。

## 5. 测试与冒烟

- 全量回归（rebase 至 `f00e490` 后，干净 basetemp）：**16 failed / 801 passed / 0 error**。失败集 = 已知预置 16 条
  （`mcp_server×2 / aux_unit×2 / forecast_pure×8 / items_route×2 / procurement_route×1 / recipe_cost_route×1`），**零新增**。
  - `nav_integration×2` 旧断言（假设 staff 应见采购入口）随本次改造修正 → 失败集 18→16；
    `tests/known_failures.txt` 同步删 2 行（清单=失败集 16/16，无残留）。
  - 陷阱：复用被污染的 `--basetemp=/tmp/pytest-dc` 会随机出现 1~3 条
    `sqlite3.OperationalError: unable to open database file` ERROR（沙箱清理不完全，错误目标每轮漂移）；
    **换全新 basetemp 目录即消失**，与代码无关。
- 测试变更：`tests/test_nav_integration.py` 重写（12 用例：侧边栏模块分组 / land 卡片按角色 /
  预测并入采购 / 入口可见性）；`tests/test_store_ordering_e2e.py` 3 条导航断言目标页
  `/` → `/dashboard`（land 页无侧栏，入口链接改在带侧栏页面验证）。
- Playwright 冒烟（admin / store_staff / rd_user × 桌面 1440 + 移动 375）：
  侧边栏 7 模块按角色正确、land 卡片按角色过滤、移动底栏 5 项渲染正常、
  「生产记录」「订单管理」两个新入口 200。

## 6. 后续 / 未决

- 「权限管理」页由权限颗粒度项目（`docs/2026-10-12-permission-uiux.md`）落地后补入「管理员」模块。
- 移动端「更多」页（`/land`）后续可再做信息架构细化（现为模块卡片平铺）。
- 品项类页面归「管理员」属本阶段归属；权限项目落地后按新权限模型复核。
