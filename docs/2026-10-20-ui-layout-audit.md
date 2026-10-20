# UI 排版审计报告

- **日期**：2026-10-20
- **审计人**：Senior Developer（agent 自动化审计）
- **状态**：**已复核 + 已修复**（见 §9 复核修正、§10 修复记录）
- **范围**：项目内所有继承 `templates/base.html` 的页面（约 60 个 URL）+ 独立模板 login、warehouse_picker
- **审计维度**：桌面 PC（≥1100px）、iPad 横屏（961–1099）、iPad 竖屏（721–960）、大屏手机（481–720）、小屏手机（≤480）

> ⚠️ **读本文前先看 §9**。初版报告有 3 处机理判断错误、2 处严重度夸大、3 项漏报，
> 已在复核阶段用 Chrome 受控用例 + 真实页面渲染逐条纠正。§3–§6 保留初版原文以存证，
> **结论以 §9 为准**。

---

## 1. 摘要

| 类别 | 数量 |
|---|---|
| 全局 CSS 风险（G1–G4） | 4 类 |
| 中屏（721–960）断点空洞（影响 ≥30 个 URL） | 多类 |
| 单页 / 单模板风险（A–J） | 33 项 |
| **受影响模板（命中任意风险）** | **44 / 66（含子目录）** |
| **Top-5 优先关注问题** | 见 §6 |

**整体判断**：CSS 总体走「mobile-first via max-width + desktop via min-width:961」双层架构（18 处 `@media`，断点为 480 / 600 / 640 / 720 / 760 / 960 / 1100 px），但**中屏（iPad 竖屏 721–960px）多个 grid 缺专门覆盖**。其次是**全局 `<table>` 被强制 `display: block`**——复核后确认其真实后果是**表格丢失 `width:100%`（收缩到内容宽度）并废掉 `table-layout:fixed`**，而非初版所称的「表头列错位」。

---

## 2. 方法

1. **静态盘点**：`app.py` 蓝图注册 + `blueprints/*.py` 路由装饰器；列出全部 GET/GET+POST 路由 → 模板映射。
2. **CSS 全扫描**：`ripgrep` 抓 `@media`、`grid-template-columns`、`position: absolute/fixed`、`z-index`、`overflow` 与 `width: Npx`（剔 max/min/%/vw/fr/auto）。
3. **模板反向溯源**：对每一类问题，回归到使用该类的具体模板文件名，定位到所属 URL。
4. **viewport meta 与 base**：检查 `base.html` 与 `login.html` 的 `<meta>`、`<head>`、外层 grid 定义。

审计只读，未改任何文件、未跑测试。

---

## 3. 全局性 CSS 风险

### 3.1 G1 — 全局 `<table>` 被 `display: block` 强制块级化

- **定位**：`static/style.css:389–399`
  ```css
  table {
    width: 100%;
    border-collapse: collapse;
    ...
    overflow: hidden;       /* 行 395 */
    display: block;         /* 行 396 — 改写默认 table layout */
    overflow-x: auto;       /* 行 397 */
    -webkit-overflow-scrolling: touch;
  }
  thead, tbody, tr { width: 100%; }   /* 行 401 */
  th, td {
    padding: 10px;
    ...
    white-space: nowrap;     /* 行 407 — 强制不换行 */
  }
  ```
- **影响模板**：44/66（grep `<table>` 命中），含 `dashboard.html`、`summary.html`、`report_*`、`stocktake*.html`、`users.html`、`items*.html`、`notifications.html`、`procurement_store.html`、`canonical/*.html`、`store_ordering/*.html`、`agent_tokens.html`、`admin/*_import*.html` 等。
- **现象**：
  - **桌面 / Pad**：`display: block` 让浏览器不再以 `table-header-group` 重排表头，纵向滚动后表头与列错位。
  - **手机**：`white-space: nowrap` 强制横滚，长字段只能截断或溢出。
- **缓解**：CSS 同时提供 `.table-scroll { overflow-x: auto }` 包装层（`style.css:126–131`），但仅 4 个模板显式包了（`summary.html`、`report_inbound.html`、`report_outbound.html`、`report_health.html`），**其余 40+ 个表都靠全局 block 化兜底**。

### 3.2 G2 — `.mobile-nav` 与 `.users-drawer` 同 `z-index: 100`

- **定位**：
  - `static/style.css:510` `.mobile-nav { z-index: 100 }`
  - `static/users-admin.css:173` `.users-drawer { z-index: 100 }`
  - 两处 CSS **没有联动**（无 `body.is-drawer-open .mobile-nav { visibility:hidden }` 之类的规则）。
- **影响 URL**：`/users`（打开抽屉时底部 nav 仍浮现，可被点穿）。
- **触发**：≤960px（仅手机 + Pad）。

### 3.3 G3 — 中屏（721–960px）断点空洞

CSS 主要断点为 max-width:480/640/720/760 + min-width:720/960/1100，**721–960 这档没有专门覆盖**的容器类：

| 容器类 | style.css 行号 | 基线列数 | 720 表现 | 721–960 表现 |
|---|---|---|---|---|
| `.kpi-row` | 69 / 76 | `repeat(5,1fr)` | `1fr 1fr` | **仍 2 列**（缺 3 列档） |
| `.recipe-cards` | 1546 / 1551 | `repeat(2, minmax(0,1fr))` | `1fr` | **仍 2 列**（缺单列档） |
| `.session-grid` | 1056 / 1060 | `minmax(220px,280px) 1fr` | `1fr` | **仍 220+1fr 双列**（左列吸 220–280px） |
| `.record-list` | 1272 / 1325 | `grid (gap)` | `gap` 堆叠 | 单列（OK，但 720 列已 OK） |
| `.land-grid` | 91–99 | `1fr 1fr` | `1fr` | 仍 2 列 |
| `.card-list-3` | 82 / 83 / 84 | `repeat(3, minmax(0,1fr))` | `1fr 1fr` | 2 列（OK，但缺 3 列保留） |
| `.info-card-grid` | 1728–1744 | `repeat(3, minmax(0,1fr))` | `1fr 1fr` | 2 列（含 nowrap 截断，见 §3.4） |

- **影响**：几乎所有用到 `.kpi-row` / `.recipe-cards` / `.session-grid` 的页面，在 iPad 竖屏（768）都会出现挤字或挤压。

### 3.4 `.info-card-grid` nowrap 截断

- **定位**：`static/style.css:1736`
  ```css
  .info-card-grid > div {
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  ```
  + `static/style.css:1740–1744` ≤720 `1fr 1fr`、≤480 `1fr`。
- **现象**：手机 481–720 切到 `1fr 1fr` 仍 nowrap，长字符串直接「…」。
- **影响 URL**：`/report/health`、`/land` 等用 `.info-card-grid` 的页。

### 3.5 G4 — `role-tags.css` 与 `users-admin.css` 全无响应式

- **定位**：
  - `static/role-tags.css`（63 行）—— 仅 `forced-colors` 一条媒体查询。
  - `static/users-admin.css:381`（381 行）—— 全无 `@media`。
- **关键固定宽**：`users-admin.css:193` `.users-drawer-panel { width: 360px; max-width: 92vw }`。
- **影响 URL**：`/users`、`/admin/agent-tokens`、`/agent_tokens`。
- **现象**：iPad 竖屏（768）+ 桌面侧栏 240px + drawer 360px = 600，**主区被压到 ~168px**。

---

## 4. 按页面/URL 分组的风险清单

> 结构：`模块 | URL | 模板路径 | 问题 → 命中全局/局部规则`
> 路由 → 模板映射来自 `blueprints/*.py` 的 `render_template(...)` 静态扫描。

### A. 主干总览与报表（core / reports）

| # | URL | 模板 | 问题 |
|---|---|---|---|
| A1 | `/dashboard` | `dashboard.html` | `.kpi-row` 5 列基线；Pad 竖屏（768）直跳 2 列；手机（≤480）2 列。命中 G1、G3。 |
| A2 | `/land` | `land.html` | `.land-grid` 默认 2 列；≤600 才 1 列。Pad 竖屏仍 2 列时字段拥挤。 |
| A3 | `/summary` | `summary.html` | `.card-list-3` + `.table-scroll` + 多 grid；中屏 2 列；表格走 wrapper OK。 |
| A4 | `/report/health` | `report_health.html` | `.info-card-grid` nowrap 截断（§3.4）；表格走 `.table-scroll` OK。 |
| A5 | `/report/outbound` | `report_outbound.html` | `.card-list-3`；表格走 `.table-scroll` OK。 |
| A6 | `/report/inbound` | `report_inbound.html` | grid + 表格；表格走 `.table-scroll` OK。 |

### B. 库存盘点与流转（stocktake / restock / outbound / adjustment）

| # | URL | 模板 | 问题 |
|---|---|---|---|
| B1 | `/stocktake` | `stocktake.html` | `.entry-grid` + 原生表 → G1。 |
| B1 | `/stocktake/session` | `stocktake_session.html` | `.session-grid` 中屏 721–960 双列挤压；`.entry-grid` 同 G3。 |
| B1 | `/stocktake/batch/\<id\>/edit` | `stocktake_edit.html` | 表格 + `.entry-grid`。 |
| B2 | `/restock` | `restock.html` | `.entry-grid` + 表 + G1。 |
| B2 | `/restock/session` | `restock_session.html` | `.session-grid` / `.session-head`。`.session-head` 默认 `1fr auto`（959），≤720 才 `1fr`——721–960 仍是「标题 + 按钮」横排，按钮宽 140px 挤压标题。 |
| B3 | `/outbound` | `outbound.html` | `.entry-grid` + 表。 |
| B3 | `/outbound/session` | `outbound_session.html` | 同 B2 的 `.session-head` 问题。 |
| B4 | `/adjustment` | `adjustment.html` | 与 B1/B2 同模式。 |
| B4 | `/adjustment/session` | `adjustment_session.html` | 同 B2。 |
| B5 | `/admin/stocktake-import[/preview/commit]` | `admin/stocktake_import*.html` | CSV 上传表单 + `.col-*` + 表 → G1 + G3。 |

### C. 生产管理（production）

| # | URL | 模板 | 问题 |
|---|---|---|---|
| C1 | `/production/session` | `production/session.html` | `.session-grid`（721–960 双列挤压输入区）。 |
| C2 | `/production/runs` | `production/runs.html` | `.runs-table { display: grid; gap: 8px }`（`style.css:1184`）——全无列数与断点，padding 控密度，窄屏长文本无截断。 |
| C3 | `/production/products[/new,/\<id\>/edit]` | `production/{products,product_edit}.html` | `.card-list-3` + 表 + G1。 |

### D. 配方成本（recipe_cost）

| # | URL | 模板 | 问题 |
|---|---|---|---|
| D1 | `/recipe-cost/recipes`, `/recipe-cost/ic-recipes` | `recipe_cost/{ic_,}recipes.html` | `.recipe-cards` 默认 2 列；761–960 仍 2 列；≤760 单列。Pad 竖屏挤字。 |
| D2 | `/recipe-cost/recipes/\<id\>/edit`, `/recipe-cost/ic-recipes/\<id\>/edit` | `recipe_cost/{ic_,}recipe_edit.html` | (1) `.recipe-card__summary { 1fr 1fr }`（`style.css:1633`）**全无 @media** —— 窄屏挤字；(2) BOM 表 `table.bom-table { table-layout: fixed }`（`style.css:1518–1533`）+ G1 的 `display: block` + `nowrap` → 窄屏强行横滚。 |
| D3 | `/recipe-cost/recipes/\<kind\>/\<id\>/versions` | `recipe_cost/recipe_versions.html` | 表格 + `.sp-adj-col { width: 100px }`（`style.css:1659`）—— 固定 100px 无兜底，叠加 nowrap 易撑破行宽。 |

### E. 门店订货（store_ordering；全部 URL 前缀 `/store-ordering`）

| # | URL | 模板 | 问题 |
|---|---|---|---|
| E1 | `/catalog` | `store_ordering/catalog.html` | `.entry-grid` 三档（720/1100）；721–959 单列堆叠。 |
| E2 | `/cart`, `/cart/submit` | `store_ordering/{cart,submit}.html` | `.entry-grid` + 原生表 → G1；`.order-actions` ≤640 重排 OK，**缺 ≤360/≤320 兜底**。 |
| E3 | `/orders`, `/orders/\<id\>` | `store_ordering/{orders,order_detail}.html` | `.record-list` + 表；`<table>` 命中 G1。 |
| E3 | `/review`, `/shipments`, `/dc/items` | `store_ordering/{review,shipments,dc_items}.html` | `.record-list` / `.entry-grid` + 表。 |
| E4 | `/admin/shipping`, `/admin/report` | `store_ordering/{shipping,report}.html` | `.col-*` + 表，无 `table-scroll` 包装。 |

### F. 品项管理（items）

| # | URL | 模板 | 问题 |
|---|---|---|---|
| F1 | `/items`, `/items/\<id\>/edit` | `items.html`, `edit_item.html` | `.col-*` + 原生表 → G1 + G3。 |
| F2 | `/items/publish`, `/items/publish/history[/\<id\>]` | `items_publish.html`, `items_publish_history.html` | 表格 + 表单 → G1。 |
| F2 | `/items/new-item-redirect[/submit]` | `items/new_item_redirect.html` | `.entry-grid` + 表单。 |
| F3 | `/inventory` | `inventory.html` | 默认布局 + 表。 |

### G. 主数据（canonical；全部 URL 前缀 `/canonical`）

| # | URL | 模板 | 问题 |
|---|---|---|---|
| G1 路由 | `/list` | `canonical/list.html` | `.col-*` + 表 + G3。 |
| G1 路由 | `/detail/\<id\>` | `canonical/detail.html` | `.entry-grid` + G3。 |
| G1 路由 | `/edit[ /\<id\>]` | `canonical/edit.html` | `.col-*` + 表。 |
| G1 路由 | `/diff` | `canonical/diff.html` | `record-card` 两列对照；≤720 单列；**iPad 横屏（1024）两列对照时无 ellipsis**。 |
| G1 路由 | `/conflicts` | `canonical/conflicts.html` | `record-card` + `.info-card-grid`（nowrap 截断）。 |
| G1 路由 | `/conflict/\<id\>` | `canonical/conflict_detail.html` | `.entry-grid` + 表。 |
| G1 路由 | `/unbound` | `canonical/unbound.html` | `.entry-grid` + 表。 |
| G1 路由 | `/claim-requests` | `canonical/claim_requests.html` | `record-card` + 表。 |
| G1 路由 | `/bulk-import` | `canonical/bulk_import.html` | `.entry-grid`。 |
| G1 路由 | `/fanout[/\<id\>]` | `canonical/fanout.html` | `.entry-grid`。 |
| G1 路由 | `/fanout/event/\<id\>` | `canonical/fanout_event.html` | `record-card` + 表。 |

### H. 用户与权限（users）

| # | URL | 模板 | 问题 |
|---|---|---|---|
| H1 | `/users`, `/users/create`, `/users/\<id\>/bind\|unbind\|change-role\|reset-password\|toggle-admin\|toggle-active\|delete`, `/users/\<id\>/bulk-bind`, `/warehouses/create`, `/warehouses/\<id\>/init` | `users.html` | (1) G2 mobile-nav/drawer 同 z-index；(2) `.role-matrix` ≤720 行 label `min-width:100%`、表 `font-size:12px`—— 字小行密；(3) `.users-drawer-panel` 360px 在 iPad 竖屏挤压主区。 |

### I. 管理员后台（import_items / agent_tokens / categories / notifications / procurement_store）

| # | URL | 模板 | 问题 |
|---|---|---|---|
| I1 | `/admin/agent-tokens`, `/admin/agent-tokens/create`, `/admin/agent-tokens/\<id\>/revoke` | `agent_tokens.html` | 表 + `.role-tags` → G1 + G4。 |
| I2 | `/admin/import-items[/preview/commit]` | `admin/import_items*.html` | `.col-*` + 表。 |
| I3 | `/categories`, `/categories/\<id\>/delete` | `categories.html` | 纯表 → G1。 |
| I4 | `/notifications`, `/notifications/\<id\>/read` | `notifications.html` | feed 列表 + 表 → G1。 |
| I5 | `/procurement/store` | `procurement_store.html` | 表 + grid → G1。 |
| I5 | `/procurement/store/accept[/download]` | 同 view / 内联 | 提交操作按钮流。 |
| I6 | `/admin/notifications/test-emit` | （JSON，无模板） | — |

### J. 认证 / 仓库选择（独立结构）

| # | URL | 模板 | 问题 |
|---|---|---|---|
| J1 | `/login` | `login.html` | viewport + base 居中布局（`main { max-width: 1200px; margin: 0 auto }`），单卡片居中 OK。 |
| J2 | `/select-warehouse` | `warehouse_picker.html` | `.warehouse-list .btn.block { width: 100% }`（`style.css:490–503`）—— 平铺按钮，无响应式补偿，居中卡片 OK。 |
| J3 | `/logout` | （redirect） | — |
| J4 | `/health` | （JSON） | — |

---

## 5. 按影响终端的高发问题矩阵

| 影响终端 | 主要问题 | 触发类 / 文件 |
|---|---|---|
| **手机 ≤480px** | G1 表头错位 + 横滚、G2 用户抽屉与 mobile-nav 同层、`.role-matrix` 字小、`info-card-grid` nowrap 截断 | G1 / G2 / G4 / A4 / H1 |
| **大屏手机 / S 平板竖 481–720** | 全部 grid 退到 2 列；`.session-grid` 仍两列挤压；`.recipe-cards` 仍 2 列 | G3 / B1–B4 / C1 / D1 |
| **iPad 竖 721–960**（最差档） | kpi-row 5→2 直跳；`.session-grid` 220+1fr 强压右侧；users-drawer 360 + sidebar 240 = 600，剩 168；`.recipe-cards` 仍 2 列；`.recipe-card__summary` 无覆盖挤字 | **G3 + H1 + B1 + D1 + D2** |
| **iPad 横 961–1099** | 侧栏 240 + main 切桌面布局；`main { max-width: 1200px }` 不再放大，主区留白；`.inv-grid` 2 列（无 3 列档，要 ≥1100 才 3 列） | `style.css:44` vs 577–600 vs 937–941 |
| **桌面 ≥1100** | `.inv-grid` 3 列、`.cols-N` 全展开；**main 仍卡 1200px**（≥1366 留白多） | `style.css:44` 缺大屏放大规则 |

---

## 6. Top-5 优先关注（按影响广度）

| # | 优先级 | 项 | 修复要点 | 估影响 |
|---|---|---|---|---|
| 1 | **P0** | G1 全局 `<table>` 块级化 | 取消 `static/style.css:389–399` 的 `display: block`；统一以 `.table-scroll` 包装层提供横滚；放宽 `nowrap` → 仅对短字段强制 | 44 模板 / 67% URL |
| 2 | **P0** | G3 中屏断点空洞 | 为 `.kpi-row`（720–1099 补 3 列）、`.recipe-cards`（721–960 补 1 列）、`.session-grid`（721–960 补 `1fr`）、`.land-grid` 增 mid 档 | 30+ 模板 |
| 3 | **P1** | D2 `.recipe-card__summary` 全无 @media | `static/style.css:1633` 加 `≤960 { grid-template-columns: 1fr }`；BOM 表 `table-layout: fixed` 改成 `auto` | 2 配方编辑页 |
| 4 | **P1** | D3 + D2 BOM 表溢出 | `.sp-adj-col` 去掉 `width:100px` 或加 `max-width: 90vw` 兜底；窄屏 `font-size` 收缩 | 3 配方成本页 |
| 5 | **P2** | G2 mobile-nav / drawer 同 z-index | `users-admin.js` 打开 drawer 时给 body 加 `is-drawer-open` 类，CSS `.is-drawer-open .mobile-nav { visibility:hidden }`；或把 `.users-drawer` z-index 调到 110 | 1 URL（/users） |

---

## 7. 关键文件快速对照

```
static/
├── style.css                  # 主样式，1959 行；含全部 @media 与全局 table 规则
├── users-admin.css            # 用户管理抽屉、表格；381 行；全无响应式
└── role-tags.css              # 角色徽标；63 行；仅 forced-colors

templates/
├── base.html                  # 母板：sidebar (desktop-only) + mobile-header + mobile-nav
├── login.html                 # 独立登录
└── {admin,canonical,items,production,recipe_cost,store_ordering}/  # 子路由模板
```

### 7.1 CSS 关键行号速查

| 行 | 内容 |
|---|---|
| `style.css:14–24` | `body` 基线 |
| `style.css:26–29` | `.app-shell` 基线 |
| `style.css:42–46` | `main { max-width: 1200px; margin: 0 auto }`（大屏不放大） |
| `style.css:69 / 76` | `.kpi-row` 5 列 / ≤720 `1fr 1fr` |
| `style.css:82–84` | `.card-list-3` 三档 |
| `style.css:91–99` | `.land-grid` |
| `style.css:126–131` | `.table-scroll` 包装层 |
| `style.css:389–408` | **全局 `<table>` 块级化 + nowrap**（G1 源头） |
| `style.css:434` | `.ctx-bar` |
| `style.css:490–503` | `.warehouse-list .btn.block { width:100% }` |
| `style.css:505–529` | `.mobile-nav`（含 z-index:100） |
| `style.css:531–545` | `.sidebar` |
| `style.css:577–600` | **桌面端总开关** `min-width:961px` |
| `style.css:602–605` | `.cols-5` ≤960 复位 |
| `style.css:931–941` | `.inv-grid` / `.inv-summary` 720 / 1100 双档 |
| `style.css:1056 / 1060` | `.session-grid` |
| `style.css:1184` | `.runs-table { display: grid }` 无列无断点 |
| `style.css:1264–1269` | `.entry-grid` 720 / 1100 |
| `style.css:1325–1328` | `.record-list` ≥960 2 列 |
| `style.css:1361–1380` | `.role-matrix` ≤720 重排 |
| `style.css:1462–1476` | 顶层模态 `z-index:999` + 关闭按钮 |
| `style.css:1518–1533` | `table.bom-table { table-layout: fixed }` |
| `style.css:1546 / 1551` | `.recipe-cards` |
| `style.css:1633` | **`.recipe-card__summary` 无 @media** |
| `style.css:1659` | `.sp-adj-col { width: 100px }` |
| `style.css:1738–1744` | `.info-card-grid` |
| `style.css:1894–1899` | `.order-actions` ≤640 重排 |
| `style.css:1940` | `.ajax-flash { position: fixed; z-index: 9999 }` |
| `users-admin.css:171–193` | `.users-drawer*` 与 `360px` 宽度 |
| `users-admin.css:372` | 用户列表 `grid-template-columns: repeat(auto-fill, minmax(260px, 1fr))` 无 @media |

---

## 8. 结论与下一步

### 8.1 结论

本项目的 UI 体系「桌面 + 小屏」端可接受，但**中屏与大量表格页面**存在系统性排版问题，主要源于：

- 全局 `<table>` 规则一刀切（`display: block` + `nowrap`）。
- CSS 断点为 720 跳 961，中间**没有任何 721–960 档**。
- 一两个高风险模板（配方编辑、用户管理、store_ordering）的 grid 容器未对齐断点。

### 8.2 建议下一步（不在本审计范围内）

1. P0：跑 §6 Top-5 修复后做一轮可视化回归（建议 Playwright/手动分别截 360 / 768 / 1024 / 1440）。
2. P0：考虑加断点 `721–960px` 统一挡（典型 iPad 竖宽 768）。
3. P1：建立「grid 容器与断点对应表」放到 `docs/ARCHITECTURE.md` 防止后续新增模板漏档。
4. P2：建立「`<table>` 必须用 `.table-scroll` 包装」约定，写入 PR 模板或加 lint。

### 8.3 审计边界声明

- 初版**未修改任何文件**，未跑测试，未部署。
- 路由清单基于「蓝图 + view 函数 + render_template」静态扫描，**未实际访问生产 URL** 验证当前生效行为；如有路由级 `context_processor` 注入 HTML、模板宏（`{% include %}` / `{% import %}`）动态注入 CSS 等情况，本审计无法覆盖。
- 建议在 Playwright headless 中按 5 个断点（360 / 480 / 768 / 1024 / 1440）批量截图复核，作为本审计的视觉验证补充。

---

## 9. 复核修正（2026-10-20，实操验证后）

复核手段：Flask test_client 伪造 admin 会话渲染 9 个真实页面 → 本地 `file://` 加载 → Chrome（Playwright）在 360 / 768 / 1440 三档执行机械探测（页面横向溢出、表格宽度与父容器比、thead/tbody 列左边界差、文字竖排特征）。另加 1 个受控最小用例（含 thead 的四列表格）对 `display` 做 A/B 对比。

### 9.1 判断错误的 3 条

| 初版结论 | 复核事实 | 证据 |
|---|---|---|
| G1：`display:block` 让 thead/tbody 各自成匿名表格，**列宽不对齐、表头错位** | ❌ **错**。Chrome 将 thead/tbody 放进**同一个匿名表**，列左边界完全对齐（delta = 0/0/0/0）。真实后果是 **`width:100%` 失效**：受控用例中表格宽 464px、容器 739px；同时 `table-layout:fixed` 与 `border-radius` 裁切一并失效 | 受控用例 `theadW=tbodyW=464`，`delta=[0,0,0,0]`；改回 `display:table` 后 `theadW=tbodyW=739` |
| D2-2：`table.bom-table { table-layout: fixed }` 是窄屏问题源 | ❌ **反了**。它是**正确做法**（`style.css:1516` 注释写明 "so columns don't squish on mobile"），被 G1 的 `display:block` 废掉 | 同上：`table-layout` 在 `display:block` 下不参与布局 |
| D3：`.sp-adj-col { width:100px }` 无兜底、会撑破窄屏 | ❌ **误判**。它依赖 `table-layout:fixed` 才成立；G1 修好后即为正确行为 | 同上 |

### 9.2 严重度夸大的 2 条

| 初版结论 | 复核事实 |
|---|---|
| `.recipe-cards` 761–960 仍 2 列是问题 | 768px 下每列约 350px，卡片内容（配方名 + BOM 摘要）可正常排布，**不是错位**。仅属可优化项 |
| `.land-grid` 600–768 仍 2 列是问题 | 同上。入口卡片两列在 768px 属合理密度 |
| `.session-grid` 721–960 双列「强压右侧」 | 偏重。768px 下左栏 220px + gap 16，右侧仍有约 500px，录入表单可用。真正的问题是下面的 `.recipe-pane` 固定高度（初版漏报） |

### 9.3 漏报的 3 项（复核新增）

| # | 问题 | 定位 | 影响 | 实测 |
|---|---|---|---|---|
| N1 | `.recipe-pane { position:sticky; height:500px }` 手机单列后仍是 500px 固定高 + sticky | `static/style.css:1061` | `/production/session`、`/stocktake/session`、`/restock/session`、`/outbound/session`、`/adjustment/session` | 360px 屏上固定占 500px ≈ 64% 屏高，且 sticky 在单列下无意义 |
| N2 | `.ajax-flash` 无 `max-width`，长提示撑出视口 | `static/style.css:1939` | 全站（ajax-form.js 提交成功后提示） | `position:fixed` + `translateX(-50%)` 下内容超宽会双向溢出 |
| N3 | **`.users-drawer-close` 未覆盖全局 `button{width:100%}`，把抽屉标题挤成竖排** | `static/users-admin.css:228` + `static/style.css:156` | `/users` | 实测 h3 宽度仅 **29px**、高度 44px（两字竖排）；关闭按钮吃掉整行 |

> N3 是本次复核中唯一「肉眼可见且严重」的新发现，性质为**布局挤压**，比初版报告的多数条目更贴近 Eric 原话的「重叠 / 挤」。
> 根因是全局 `input, select, button { width: 100% }` 与 flex 头栏叠加，属系统性问题（见 §10.3 遗留）。

---

## 10. 修复记录（2026-10-20）

### 10.1 已修复（10 项）

> 行号以 `origin/main` @ `6c837f0` 为基准。

| # | 文件:行 | 改动 | 验证 |
|---|---|---|---|
| 1 | `static/style.css:399` | `table` 由 `display:block + overflow-x` 改为 `display:table`；横滚移交 `.table-scroll` | 受控用例 theadW/tbodyW 464→739，`width:100%` 恢复 |
| 2 | `templates/base.html:194-214` | 新增 DOMContentLoaded 脚本：给 `main` 内未包装的表补 `.table-scroll`，父级已有 `overflow-x:auto/scroll` 的跳过 | 9 页 3 档探测：`pageOvf` 全 0；宽表（items 876px）在 740px 容器内横滚 |
| 3 | `static/users-admin.css:170` | `.users-drawer` z-index `100 → 120`（高于 `.mobile-nav` 的 100） | 计算值 drawer=120 / nav=100；drawer 在 `<main>` 内 DOM 早于 nav，原层级会被底栏压住且可点 |
| 4 | `static/users-admin.css:228` | `.users-drawer-close` 补 `width:auto; flex:0 0 auto; line-height:1` | h3 由 29×44（竖排）→ 32×22（单行） |
| 5 | `static/users-admin.css:205-218` | `@media (max-width:480px)`：drawer 面板全宽 + `100dvh` | 390px 视口下 panel 宽 390 |
| 6 | `static/style.css:1118` | `@media (max-width:720px)`：`.recipe-pane` 改 `position:static; height:auto; max-height:260px` | 单列下不再固定 500px |
| 7 | `static/style.css:74-80` | 新增 `721–1099px` 档：`.kpi-row` 补 `repeat(3,1fr)` | `/report/health` @768 实测 `grid-template-columns: 238.7px × 3`（5 卡走 3+2） |
| 8 | `static/style.css:2067` | `.ajax-flash` 补 `max-width: calc(100vw - 32px); text-align:center` | — |
| 9 | `static/style.css:1729` | `.recipe-card__summary` 新增 `≤560px` 单列 | — |
| 10 | `static/style.css:1847` | `≤960px` 时 `.info-card-grid > div` 由 `nowrap` 改 `normal`，避免截断成「…」 | — |

### 10.2 明确不修（判定为合理设计）

| 项 | 理由 |
|---|---|
| `.cols-5` ≤960 降 2 列、第 5 卡占满整行（`style.css:602-605`） | 5 个卡片 2+2+1 是刻意排布，非缺陷 |
| `.cols-4` ≤960 保持 2 列 | 4 卡 2×2 合理 |
| `.recipe-cards` 761–960 两列 | 见 §9.2 |
| `.land-grid` 600–768 两列 | 见 §9.2 |
| `.session-grid` `minmax(220px,280px)` | 右侧仍有约 500px，未构成挤压 |
| `main { max-width:1200px }` 大屏留白 | 是阅读宽度约束，非错位 |
| `th, td { white-space: nowrap }` | 窄屏溢出已由 `.table-scroll` 承接；改 `normal` 会改动全部表观感，收益不抵风险 |

### 10.3 遗留 / 待评估

- **全局 `input, select, button { width:100% }`（`style.css:156`）**：N3 只是其中一个受害者。凡是 flex/grid 头栏里含 button 而未显式 `width:auto` 的容器都有同类风险。本次只修了已实测的 `/users` 抽屉，其余未逐一排查。
- **`.table-scroll` 在移动端无可见滚动条**（macOS / iOS 默认隐藏），用户不易察觉表格可横滚。若要提示，需加渐隐遮罩或 `scrollbar` 样式，属体验优化。
- `style.css:135` 的 `.table-scroll table { display:table; min-width:100% }` specificity 高于裸 `table` 规则——**这是初版误判的直接原因**，后续改表格样式务必先想到它。

### 10.4 回归验证

| 项 | 结果 |
|---|---|
| 渲染冒烟 | 9 页（dashboard / summary / report/health / stocktake / items / recipe-cost/recipes / users / store-ordering/orders / store-ordering/catalog）× 360·768·1440，全部 HTTP 200 |
| 页面横向溢出 | 9 页 3 档共 27 组，`scrollWidth - clientWidth` **全部为 0** |
| 表格横滚 | 未包装表已自动补 `.table-scroll`；`users` 页因自带 `.users-table-wrap` 正确跳过 |
| 文字竖排扫描 | 27 组全量扫描（h1–h4/span/strong/label/td/th/a/button），**0 命中** |
| pytest（导航/母板） | `test_nav_integration.py` + `test_base_nav_isadmin.py` → **59 passed** |
| pytest（全量） | 见下节结论 |

> 验证脚本为一次性临时脚本（`/tmp/dc_render.py`、`/tmp/dc_shots.py`），未入库。
> DB 用 `/tmp/dc-ui-audit-db` 副本，未触碰工作树 `db/`。
