# 导航模块化：7 模块 IA（2026-10-13）

> 需求（Eric，2026-10-13）：优化页面导航逻辑 —— 主页「采购建议」改「采购模块」、
> 「库存查阅」改「库存管理模块」、「生产录入」改「生产管理模块」，新增「管理员模块」；
> 先评估各功能页面的模块归属，交 owner 确认后实施。
> 落地分支：`feat/nav-modules`（独立 worktree，起点 `origin/main` 75af9c6，
> 交付前已 rebase 至 `f00e490`）；审核整改在 `fix/pr-18-nav-modules-defects`（见 §7）。
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
| 模块索引页 | `templates/land.html`（`/land`，`no_sidebar`） | 每模块一张卡（`section.land-card`），按角色过滤；**卡内必须列全该角色在该模块可见的每一个页面**（`ul.land-links`），不是只链模块首屏 |

⚠️ **三载体的角色口径与模块归属必须逐条一致**（同一条 `is_admin / is_rd /
is_storefront / is_distribution_center` 表达式）。三条链路只保证「一致」还不够：
**`/land` 必须列全二级页** —— 视口 <961px 时侧边栏 `display:none`，「更多」→ `/land`
是移动端唯一全模块入口，模块首屏（如 `/inventory`）页内又没有出链，
只链首屏会让 汇总/盘点/入库/出库 这类二级页在移动端整页断链（v0.1 即如此，见 §7）。
`tests/test_nav_integration.py` 用「侧边栏模块集 == `/land` 模块卡集」+「移动端
可达性（底栏 + `/land` 链接集合须覆盖库存管理二级页）」两组断言把这条不变量固化。

样式：`static/style.css` 新增 `.nav-group` / `.nav-group-title` / `.land-links`；
`style.css` 每改必 bump 模板里的 `?v=`（`/static/` 走 SW cache-first 不回源，
暖缓存客户端会永久拿旧文件）—— 本次 `base.html` `?v=16`、`login.html` `?v=12`。

## 4. 盘点发现的「孤岛」（处置登记）

- `/store-ordering/admin/orders`（订单管理）：有页面、原无任何入口 → **本次补入侧边栏「门店订货」模块**。
- `/procurement/hub`：实为 JSON 接口（`return jsonify`）非页面 → 不建入口（评估初稿误标为「采购中枢页」，已修正）。
- `blueprints/adjustment.py`：代码在、`app.py` 未注册、路由不可达（含 2 个闲置模板）→ 仅登记（§14）。

## 5. 测试与冒烟

- 全量回归（rebase 至 `f00e490` 后，干净 basetemp）：**16 failed / 801 passed / 0 error**。
  ⚠️ 数字口径：这是**本地全量（含 `tests/mcp_server`）**——本地 python3.12 能跑 mcp 测试
  （实测 2 failed / 78 passed）。**CI 口径**是 `--ignore=tests/mcp_server`，同一份代码为
  **14 failed / 723 passed**（整改分支为 768 passed，本地全量 846 passed，新增导航用例后）。
  两套数字都必须对得上 `tests/known_failures.txt`（清单 16 条 = 本地失败集，含 mcp×2），
  引用时务必写明口径，否则「零新增对比法」的锚点会失效。
- 失败集 = 已知预置 16 条
  （`mcp_server×2 / aux_unit×2 / forecast_pure×8 / items_route×2 / procurement_route×1 / recipe_cost_route×1`），**零新增**。
  - `nav_integration×2` 旧断言（假设 staff 应见采购入口）随本次改造修正 → 失败集 18→16；
    `tests/known_failures.txt` 同步删 2 行（清单=失败集 16/16，无残留）。
  - 陷阱：复用被污染的 `--basetemp=/tmp/pytest-dc` 会随机出现 1~3 条
    `sqlite3.OperationalError: unable to open database file` ERROR（沙箱清理不完全，错误目标每轮漂移）；
    **换全新 basetemp 目录即消失**，与代码无关。
- 测试变更：`tests/test_nav_integration.py` 重写（v0.1 为 12 用例；整改后 57 用例 =
  模块集行列式 + 三载体链接集相等 + 全链接可达性 + 移动底栏 + 移动端可达性守卫）；
  `tests/test_store_ordering_e2e.py` 3 条导航断言目标页 `/` → `/dashboard`
  （land 页无侧栏，入口链接改在带侧栏页面验证），并补回 A8「DC staff 403」真实断言。
- Playwright 冒烟（admin / store_staff / rd_user × 桌面 1440 + 移动 375）：
  侧边栏 7 模块按角色正确、land 卡片按角色过滤、移动底栏 5 项渲染正常、
  「生产记录」「订单管理」两个新入口 200。
  ⚠️ 只验证了「底栏渲染正常」，**没验证「二级页在移动端是否还有入口」** ——
  这正是 v0.1 漏掉 §7 那批断链的原因，冒烟清单已补该检查项。

## 6. 后续 / 未决

- 「权限管理」页由权限颗粒度项目（`docs/2026-10-12-permission-uiux.md`）落地后补入「管理员」模块。
- 品项类页面归「管理员」属本阶段归属；权限项目落地后按新权限模型复核。

## 7. 审核整改（2026-10-14，`fix/pr-18-nav-modules-defects`）

代码审核（code-review 专家，第 1 轮 FAIL）判定 v0.1 存在一处**移动端可达性回归**：
底栏从 15 项收敛为 ≤5 项时删掉了 汇总 / 盘点 / 入库 / 出库，而 `/land` 只链模块首屏、
`/inventory` 页内无出链、<961px 又无侧边栏 → 这 4 个页面前端彻底无路可达。
连带发现三载体口径不一致（单仓 `role=admin` 的「品项批量导入」、rd 的配方成本）。

| # | 问题 | 处置 |
|---|------|------|
| 1 | **移动端断链（HIGH）**：汇总/盘点/入库/出库 无入口 | `/land` 卡片改为「模块卡 + 卡内子页链接」（`ul.land-links`），列全每模块二级页 |
| 2 | 单仓 `role=admin`：侧栏有「品项批量导入」、`/land` 无管理员卡 → 移动端够不到 | `/land` 管理员卡 gate 与侧栏对齐（`is_admin or is_rd or role=='admin'`），卡内链接逐条对应 |
| 3 | rd 底栏只剩 总览/更多 2 项 | 底栏「生产管理」项 gate 加 `is_rd`，rd 直指 `/recipe-cost/` |
| 4 | 测试只断言侧边栏与 `/land`，无移动底栏/rd/单仓 admin 用例（1~3 全漏检） | 新增 `nav_client_factory` 夹具 + 8 组角色矩阵用例 + 全链接可达性 + 移动端可达性守卫（35 用例） |
| 5 | `test_dc_staff_blocked_from_dc_items` 是 `pass` 占位（恒绿） | 用例内就地补 `dc_staff` 用户，恢复 `assert 403` |
| 6 | 文档基线数字未标口径 | §5 已分别标注「本地全量含 mcp」与「CI `--ignore=tests/mcp_server`」两套数字 |
| 7 | `/land` 生产管理卡 href 用 `is_rd`、文案用 `is_rd and not is_admin` | 两处统一为 `is_rd and not is_admin` |

已知限制 / 未修（登记，非本次引入）：
- `/canonical/list` 仅 `@require_login`，任何登录角色可直接访问，导航只对 admin/rd 展示
  （隐藏式而非强制式权限）；对称地 DC 用户实际可访问 `/inventory` 但导航不展示。
  应在权限颗粒度项目按统一模型收敛。
- 侧边栏 `<h1>库存管理</h1>` 与模块标题「库存管理」同名（文案问题，视觉/文案由 owner 定）。
- `/production*` 模板把 `sidebar` block 覆盖为空 → 桌面端在这些页无侧栏（存量，本次
  「生产记录」入口沿用同布局）。

**第 2/3 轮审核抓到的两个「测试资产」缺陷（比代码缺陷更容易复发，必须记住）**：

1. **可达性扫描不能扫整页**：`/land` 整页 href 含 ctx-bar 的「注销」(`/logout`)，
   `auth.logout` 会 `session.clear()` —— 一旦 GET 到它，排序在其后的 10 个链接全部以
   匿名身份拿到 302，`status_code < 400` 恒真（假绿）。取链必须收窄到载体内部
   （`aside.sidebar` / `nav.mobile-nav` / `div.land-grid`），并断言目标集非空。
2. **不变量必须双向**：`sidebar ⊆ /land ∪ 底栏` 单向时，「侧栏自己丢链接」恒过
   （实测：删侧栏「采购建议」/「汇总」后全量 CI 仍全绿）。改为 `sidebar == /land` 双向
   相等后两个方向都能挡住。**断言写完要做变异验证**（删链接 / 改可达性 / 挪位置看是否
   真的 FAIL），静态审查看不出恒真式。
