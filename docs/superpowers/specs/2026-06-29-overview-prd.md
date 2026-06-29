# DailyCheck 总览 PRD — 预测 / 采购建议 / MPC / 发布 / 通知 / 汇总自定义

**版本**：v1（2026-06-29）
**状态**：待 review。落地为各子项目 spec/plan 后进入实施。
**目标读者**：接手子项目的 agent / 工程师。PRD 不锁定实现细节，但定义清楚每项能力的产品边界、数据契约、异常行为、自动化要求与验证标准。

---

## 0. 阅读说明

本 PRD 是总览。每个子项目（6 项能力 + 1 项横切自动化）实施时必须走：

```
brainstorm  →  spec  →  plan  →  TDD 实现  →  code review  →  merge
```

每个子项目独立一个 spec 文档（在 `docs/superpowers/specs/`）和一个 plan 文档（在 `docs/superpowers/plans/`）。本 PRD 不能直接进入 plan。

每个子项目 PRD 章节给出的「数据契约」「异常」「测试要点」必须在子 spec 里**直接引用并细化**，不得删除或放宽。

---

## 1. 横切目标与约束（适用于所有子项目）

### 1.1 减少人工介入 — 必须自动化清单

所有子项目实施时，必须保证以下动作为**自动**或**一键**，不接受"管理员每个店手动操作"方案：

| # | 自动化场景 | 触发条件 | 不接受的人工动作 |
|---|---|---|---|
| A1 | 库存消耗预测每日自动跑 | 每日凌晨（或按库存变动阈值） | 手动触发预测 |
| A2 | 采购建议在库存变动后自动重算 | 任意出库/入库/盘点/补货申请完成后 | 手动点"重算" |
| A3 | 补货预警自动推送到总仓待办 | 任意品项库存量 < 安全库存 | staff 手工发现并提报 |
| A4 | 品类/品项/配方发布 = 一次发布多店可见 | admin 选模板 + 选门店 → 一键发布 | 逐个门店手动添加品项 |
| A5 | Agent 通过 MPC token 自动调用 | token 持有者 | 手动复制数据 |
| A6 | 配方发布后全站通知自动出现 | 任意用户登录后顶部红点自动显示 | staff 主动查询 |

### 1.2 数据契约原则

- **门店库**：`db/warehouses/*.db` 仍是每店独立 SQLite。所有门店级数据（库存、流水、预测、采购建议、补货预警、配方版本、通知）写入该库。
- **master 库**：`db/master.db` 承担跨店数据（用户、仓库列表、模板、发布事件、Agent token、通知偏好）。
- **跨库查询**：禁止运行时跨库 JOIN。所有跨库聚合（总仓采购建议、Agent 跨店读取）通过「先按店查询再聚合」的代码层完成。
- **时区**：所有 `created_at` / `updated_at` 用 UTC 存储；前端展示按用户浏览器时区转换（沿用现有约定）。

### 1.3 权限矩阵（新增能力）

| 能力 | admin | manager | staff | Agent |
|---|---|---|---|---|
| 创建/编辑模板 | ✅ | ❌ | ❌ | ❌ |
| 触发品项/配方发布 | ✅ | ✅（限本店 + 授权范围） | ❌ | ❌ |
| 查看预测 | ✅ 全平台 | ✅ 全平台 | ✅ 本店 | ✅ token 授权范围 |
| 查看采购建议 | ✅ | ✅ 全平台 | ✅ 本店 | ✅ token 授权范围 |
| 创建补货申请（写） | ✅ | ✅ | ✅ | ✅ token 白名单内 |
| 查看通知 | ✅ 全部 | ✅ 全部 | ✅ 全部 | ❌ |
| 管理 Agent token | ✅ | ❌ | ❌ | — |
| 查看 MPC 调用日志 | ✅ | ❌ | ❌ | ❌ |

> staff 的「触发发布」需在子 spec 细化（是否允许、需 manager 复核等）。

### 1.4 错误处理与可恢复性

每个子项目 spec 必须明确：

1. **不可逆动作**：哪些动作一旦完成不能回滚（如库存扣减、配方的某次发布版本）。
2. **可回滚动作**：哪些提供 `undo` 入口或补偿接口（如补货申请删除）。
3. **失败兜底**：外部依赖失败（Agent token 失效、定时任务崩溃）时的可见性 — 必须能在 `/admin/...` 看到失败状态。

---

## 2. 6 项能力详述

### 2.1 库存消耗预测

#### 2.1.1 目标

按品项 × 门店，输出「日均消耗 + 未来 N 天预测总量」两个核心指标，驱动采购建议与 Agent 读取。

#### 2.1.2 数据契约（PRD 锁定，spec 可细化）

```json
GET /forecast?item_id=...&warehouse_code=...&horizon_days=14
→ 200 {
    "item_id": 123,
    "warehouse_code": "wh_001",
    "horizon_days": 14,
    "daily_avg": 2.5,         // 过去 30 天加权日均，0 表示数据不足
    "forecast_total": 35.0,   // 未来 horizon_days 天预测总量
    "confidence": "low|medium|high",
    "computed_at": "2026-06-29T03:00:00Z",
    "data_status": "ok|cold_start"   // cold_start = 不返回 daily_avg/forecast_total
}
```

| 字段 | 说明 |
|---|---|
| `daily_avg` | 过去 30 天加权日均消耗，算法未锁定，PRD 只承诺「加权平均」语义 |
| `forecast_total` | 未来 `horizon_days` 天的预测总量，默认 14，可参数覆盖 |
| `confidence` | 由算法返回，PRD 仅约束取值集合 |
| `data_status` | `cold_start` 时 daily_avg/forecast_total 必须为 0，UI 显示「数据积累中」 |

#### 2.1.3 范围与边界

- **覆盖对象**：`items`（库存品）+ `products`（产品）的成品销售预测。两类对象使用同一预测接口，路径分别 `/forecast/item/<id>` 与 `/forecast/product/<id>`。
- **冷启动**：过去 30 天内消耗记录 < 7 条 → `data_status=cold_start`，**不返回数值**。不允许用「全平台均值」或「人工初值」填充。
- **门店**：每店独立预测；不汇总。
- **重算**：每日凌晨自动跑一次（cron 或线程调度）；用户在 `/forecast` 页点击「手动重算」可立即触发。

#### 2.1.4 异常

| 场景 | 行为 |
|---|---|
| 未知 item_id / warehouse_code | 404 + `{ "error": "not_found" }` |
| horizon_days < 1 或 > 90 | 400 + `{ "error": "invalid_horizon" }` |
| 库存品 / 产品已被禁用 | 410 Gone |
| 数据库锁定（写预测缓存时冲突） | 重试 3 次；仍失败 → 写 `access.log` + `/admin/health` 标记 |

#### 2.1.5 必须自动化（横切 A1）

- 每日凌晨自动跑全平台预测。
- 跑批状态记录在 `forecast_runs` 表（待定 schema，子 spec 锁定）。
- 失败时 admin 在 `/admin/health` 看到「上次成功时间」。

#### 2.1.6 测试要点

- 单元：`compute_daily_avg(movements_list)` / `compute_forecast_total(daily_avg, horizon)` 纯函数，fixture 覆盖多种分布（递增、递减、稀疏、突发）。
- 单元：冷启动边界（7 条以下）→ `data_status=cold_start`，字段为 0。
- 集成：`GET /forecast?item_id=...` 返回结构与字段类型稳定（schema 校验）。
- 集成：手动重算接口幂等（多次点击结果一致）。
- 集成：未知 item_id → 404；非法 horizon → 400。

---

### 2.2 门店采购建议 + 总仓采购建议

#### 2.2.1 目标

门店层：基于预测 + 安全库存 + 在途，给 staff「该补什么、补多少」的清单，支持一键采纳 → CSV。
总仓层：聚合所有门店的需求，给 manager「总仓该向供应商采购多少」的清单。

#### 2.2.2 数据契约

```json
GET /procurement/store?warehouse_code=wh_001
→ 200 {
    "warehouse_code": "wh_001",
    "computed_at": "2026-06-29T03:00:00Z",
    "items": [
        {
            "item_id": 123,
            "item_name": "木樨子油",
            "current_qty": 0.7,
            "in_transit_qty": 0,            // 来自未完成的 restock_requests
            "daily_avg": 0.05,
            "forecast_total_horizon": 0.7,
            "safety_stock": 1.0,            // 固定公式
            "suggested_qty": 1.0            // max(0, safety_stock - current - in_transit)
        }
    ]
}
```

总仓层：

```json
GET /procurement/hub?warehouse_codes=wh_001,wh_002
→ 200 {
    "items": [
        {
            "item_id": 123,
            "item_name": "木樨子油",
            "total_suggested_qty": 5.0,    // 所有门店 suggested_qty 求和
            "stores_needing": 3,           // suggested_qty > 0 的门店数
            "stores_detail": [...]         // 可选，CLI/Agent 用
        }
    ]
}
```

#### 2.2.3 安全库存公式（PRD 锁定）

```
safety_stock = max(daily_avg * cover_days, min_absolute)
suggested_qty = max(0, ceil(safety_stock - current_qty - in_transit_qty))
```

- `cover_days` 默认 14，可在 master 配置中覆盖。
- `min_absolute` 默认 0；可在品项级别覆盖（如「至少保留 1 包」）。
- PRD 不做门店间调拨推荐（明确不在范围）。

#### 2.2.4 采纳动作（PRD 锁定：仅 CSV）

- **不接受**自动写入 restock_requests 表。
- staff 点击「采纳」→ 后端生成 `procurement_acceptance_<warehouse_code>_<timestamp>.csv`，包含 `item_id, item_name, suggested_qty, unit, note` 四列。
- CSV 下载完成后，前端 toast「已导出 X 个品项到 CSV」。

#### 2.2.5 重算时机（横切 A2）

- 任意出库 / 入库 / 盘点 / 补货申请完成后 → 触发该品项的采购建议重算。
- 重算不在请求路径同步阻塞；建议缓存表 5 秒内可读到新值即可。

#### 2.2.6 异常

| 场景 | 行为 |
|---|---|
| warehouse_code 不存在 | 404 |
| 当前无任何品项需要补 | 200 + `items: []` |
| CSV 生成时 IO 失败 | 500 + flash「导出失败，请重试」+ 写 access.log |
| 预测数据 cold_start 的品项 | suggested_qty = 0（不基于未稳定的预测给建议） |

#### 2.2.7 测试要点

- 单元：`compute_safety_stock(daily_avg, cover_days, min_absolute)` 纯函数，多场景。
- 单元：`aggregate_hub(store_reports)` 聚合函数。
- 集成：提交一次出库 → 该品项采购建议在 5 秒内更新。
- 集成：采纳 → CSV 格式、内容、空列情况。
- 集成：cold_start 品项不出现在建议列表中。

---

### 2.3 Agent 用的 MPC 接口

#### 2.3.1 目标

为外部 Agent（告警机器人、采购决策助手、报表脚本）提供稳定的 HTTP/JSON 接口，覆盖读 + 有限写。

#### 2.3.2 接口清单（PRD 锁定读侧，写侧最小集）

**读**（所有 Agent 默认可调，需 token）：

| 路径 | 方法 | 说明 |
|---|---|---|
| `/api/v1/items` | GET | 库存品列表（按 warehouse_code） |
| `/api/v1/items/<id>` | GET | 单品项详情 |
| `/api/v1/movements` | GET | 流水（带 warehouse_code, since/until） |
| `/api/v1/forecast/item/<id>` | GET | 见 2.1.2 |
| `/api/v1/procurement/store` | GET | 见 2.2.2 |
| `/api/v1/procurement/hub` | GET | 见 2.2.2 |
| `/api/v1/categories` | GET | 品类 |
| `/api/v1/templates` | GET | 模板列表 |
| `/api/v1/notifications/feed` | GET | 当前未读通知（Agent 暂不消费，仅预留） |

**写**（必须在 token 白名单中显式列出路径前缀）：

| 路径 | 方法 | 说明 |
|---|---|---|
| `/api/v1/restock` | POST | 创建补货申请 |
| `/api/v1/procurement/recompute` | POST | 触发某品项采购建议重算 |
| `/api/v1/forecast/recompute` | POST | 触发某品项预测重算 |

#### 2.3.3 鉴权与跨仓库（PRD 锁定）

- **Token**：`master.db` 表 `agent_tokens`（子 spec 细化 schema）。
  - 字段：token、name、created_by、created_at、revoked_at、allowed_read_paths（JSON array）、allowed_write_paths（JSON array）、allowed_warehouse_codes（JSON array，[] 表示全平台）。
  - Token 通过 `Authorization: Bearer <token>` 头传入。
- **路由**：每个 Agent 调用必须显式指定 `warehouse_code`，未指定 → 400。
- **路径校验**：调用路径不在 `allowed_*_paths` 中 → 403 + access.log。
- **门店校验**：warehouse_code 不在 `allowed_warehouse_codes` 中 → 403。

#### 2.3.4 可观测性（横切 A5 + 管理页面）

- **access.log**：每条 MPC 调用写一条 JSON 行（`timestamp, agent_token_id, path, status, duration_ms`）。
- **`/admin/mpc-usage`**：admin 页面，列出：
  - 每个 token 的累计调用次数、错误率、最近一次调用时间。
  - 路径 Top N。
  - 异常事件（连续 401/403、token 突然高频调用）的红色标记。
- 不在 PRD 范围：限流、邮件告警、自动封禁（后续 spec 可加）。

#### 2.3.5 异常

| 场景 | 行为 |
|---|---|
| 缺 Authorization 头 | 401 |
| Token 不存在或 revoked_at 非空 | 401 + access.log |
| Token 路径 / 门店越权 | 403 + access.log |
| warehouse_code 缺失 | 400 + `{ "error": "warehouse_code_required" }` |
| 写动作参数不合法 | 400 + `{ "error": "...", "field": "..." }` |
| 数据库错误 | 500 + access.log + 唯一 request_id 返回给调用方 |

#### 2.3.6 测试要点

- 单元：token 校验函数（解析、查表、判定撤销）。
- 单元：路径白名单匹配（精确、前缀、通配符 `*`）。
- 集成：每个读路径覆盖正常 + 异常（缺 warehouse_code、无权限、未知资源）。
- 集成：每个写路径覆盖幂等性（如 POST /restock 重复 token + 同 payload 应当返回同一申请 ID）。
- 集成：admin 撤销 token 后，已发出的下一次请求 401。
- 安全测试：篡改 Authorization 头、SQL 注入（参数化查询必须）、CSRF（API 不依赖 session cookie）。

---

### 2.4 品类与品项发布（多模板 + 多门店可见性）

#### 2.4.1 目标

admin 创建"品项集合模板"，选门店集合一次性发布；门店端自动看到新发布的品项，已有同名品项时强制人工选保留/覆盖/取消。

#### 2.4.2 模板身份（PRD 锁定：品项集合，不是门店型）

- 模板是「一组品项定义」的快照，包含 `name, category, unit, unit_cost, gram_per_unit, safety_stock`。
- 模板**不是**门店型（不绑定门店类型），但可在「关联推荐」字段建议常用门店集合（仅 UI 提示，不强制）。
- 模板版本化：每次保存创建新版本号（递增整数），老版本冻结可查。

#### 2.4.3 发布动作流程

1. admin 在 `/admin/publish/items` 选择模板 + 目标门店集合。
2. 系统对每个目标门店做差异分析：
   - 该品项在门店不存在 → 「新增」
   - 该品项已存在且字段全等 → 「跳过」
   - 该品项已存在但字段不同 → 「冲突」
3. 仅当没有「冲突」时，可一键发布。
4. 出现冲突 → 弹出冲突列表，admin 对每条冲突选：
   - 「保留门店现有」— 该品项不发布到此门店
   - 「用模板覆盖」— 名称、单位、克重、单位成本、安全库存全部用模板值
   - 「合并」— 模板有但门店无的字段补齐，门店已有字段保留
5. admin 选完后点「确认发布」→ 系统写入 `publish_events` + 目标门店的 items 表（按选择处理）。

#### 2.4.4 数据契约

```json
POST /admin/publish/items/preview
{
    "template_id": 7,
    "template_version": 3,
    "warehouse_codes": ["wh_001", "wh_002"]
}
→ 200 {
    "template_id": 7,
    "template_version": 3,
    "warehouses": [
        {
            "warehouse_code": "wh_001",
            "items": [
                {"template_item_idx": 0, "item_name": "木樨子油",
                 "status": "add|skip|conflict",
                 "existing_item_id": 123,    // 仅 conflict
                 "diff_fields": ["unit_cost", "gram_per_unit"]   // 仅 conflict
                }
            ]
        }
    ]
}

POST /admin/publish/items/confirm
{
    "template_id": 7,
    "template_version": 3,
    "warehouse_codes": ["wh_001"],
    "resolutions": [
        {"template_item_idx": 0, "warehouse_code": "wh_001",
         "action": "keep_store|overwrite|merge"}
    ]
}
→ 200 { "publish_event_id": 42 }
```

#### 2.4.5 事件追溯（PRD 锁定：发布记录 + 事件表）

- `publish_events`（master.db）：发布动作元数据（发布人、时间、模板、版本、目标门店集合、resolutions 摘要）。
- `publish_event_items`（master.db）：每个品项在每个门店的处理结果（add / skip / overwrite / merge）。
- **不**写入门店库；门店库通过 `items.created_by_publish_event_id` 反向引用（子 spec 决定具体 schema）。

#### 2.4.6 异常

| 场景 | 行为 |
|---|---|
| 模板无该版本 | 404 |
| 门店不存在 | 400 |
| confirm 时 resolutions 缺失或 idx 不匹配 | 400 |
| confirm 时仍有未处理的 conflict | 400 |
| 某门店写 items 失败 | 部分成功语义：返回成功列表 + 失败列表，admin 可重试 |

#### 2.4.7 必须自动化（横切 A4）

- 一次发布 = 多个门店同时收到。不接受"逐个门店手动添加"作为产品路径。

#### 2.4.8 测试要点

- 单元：`detect_conflicts(template_items, store_items)` 纯函数，多场景。
- 集成：preview 接口覆盖 add / skip / conflict 三种状态。
- 集成：confirm 接口覆盖 keep_store / overwrite / merge 三种 action。
- 集成：发布失败部分成功语义。
- E2E：admin 发布 → 门店 staff 登录 → 在 `/items` 看到新品项。

---

### 2.5 配方发布 + 全站通知

#### 2.5.1 目标

manager 或 admin 触发发布新配方 → 自动版本化 → 全站通知（Web 红点 + 详情页）。

#### 2.5.2 配方发布语义（PRD 锁定：版本化）

- 每次发布在 `product_bom_versions` 表（子 spec 细化）写入一条新版本。
- `products.current_version_id` 指向「当前生效版本」。
- 老版本冻结可查；不允许删除老版本。
- 生产录入按 current_version 取 BOM。

#### 2.5.3 发布范围（PRD 锁定：按门店发布）

- 发布时选目标门店集合（与 2.4 同 UX）；每门店可独立接受不同版本。
- 门店级生效版本：`product_bom_store_versions` 表（子 spec 细化）。
- 不发布到的门店保持当前生效版本不变。

#### 2.5.4 全站通知（横切 A6）

- 通知系统**通用事件总线**，Web 首期：
  - 事件源：`event_types` 枚举（recipe_published / item_published / system_alert），首期只启用 recipe_published。
  - 渠道：`channels` 表（web / email / im）；首期只实现 web。
  - 用户偏好：`notification_prefs` 表，按用户+事件类型+渠道存「订阅 / 静音」。
- Web 体验：
  - 顶部 nav 出现红点（未读数量），点击跳到 `/notifications` 中心。
  - 通知中心显示 `event_type, summary, created_at, target_url`；点「标记已读」或「查看」后跳详情页。
  - 详情页（如发布详情）内显式提供「标记已读」按钮。
- 不在范围：toast 弹窗、邮件、IM、推送。

#### 2.5.5 数据契约

```json
GET /notifications?unread=true
→ 200 {
    "unread_count": 3,
    "events": [
        {
            "event_id": 88,
            "event_type": "recipe_published",
            "summary": "经典柠檬茶 v3 已发布",
            "created_at": "2026-06-29T...",
            "target_url": "/products/12/versions/3",
            "read": false
        }
    ]
}

POST /notifications/<event_id>/read
→ 200 { "ok": true }
```

发布侧：

```json
POST /admin/publish/recipe
{
    "product_id": 12,
    "bom_items": [...],
    "warehouse_codes": ["wh_001", "wh_002"],
    "summary": "经典柠檬茶 v3 已发布"
}
→ 200 { "version_id": 3, "publish_event_id": 88 }
```

#### 2.5.6 异常

| 场景 | 行为 |
|---|---|
| product 不存在 | 404 |
| bom_items 为空 | 400 |
| 仓库不存在 | 400 |
| 系统故障导致部分门店未生效 | 返回成功列表 + 失败列表；通知仍发（含受影响门店数） |
| 用户无登录态访问 /notifications | 302 → /login |

#### 2.5.7 测试要点

- 单元：版本号分配（连续递增、不复用）。
- 单元：事件 → 通知 fanout（按订阅偏好过滤）。
- 集成：发布 → 通知生成 → 用户拉 feed → 标记已读 → 未读数减 1。
- 集成：老版本可查、不可删。
- 集成：生产录入按 current_version 取 BOM，跨版本不影响进行中批次。
- E2E：manager 发布 → 任意角色登录 → 看到红点 → 进入详情 → 标记已读。

---

### 2.6 汇总表自定义时间

#### 2.6.1 目标

汇总页 `/summary` 提供任意起止日期 + 预设快捷，替换现有 `?range=` 参数。

#### 2.6.2 URL 契约

- **新参数**：`?start=YYYY-MM-DD&end=YYYY-MM-DD`。
- **预设快捷**（按钮）：本周 / 上周 / 本月 / 上月 / 本季 / 本年，点击后跳到对应 URL。
- **默认值**：`start` 缺省 = 过去 7 天（与当前 `7d` 等价）；`end` 缺省 = 今天。
- **替换语义**：`start` / `end` 优先于 `range`。`range` 参数保留向后兼容一个版本，但忽略。

#### 2.6.3 校验规则

| 场景 | 行为 |
|---|---|
| start > end | 400 / flash「开始日期不能晚于结束日期」 |
| 跨度 > 365 天 | 400 / flash「时间范围不能超过 1 年」 |
| 未来日期 | 允许，但 end 不超过今天 + 1 天 |
| 格式错误 | 400 / flash「日期格式应为 YYYY-MM-DD」 |

#### 2.6.4 聚合口径（沿用现有，PRD 不重定义）

- 进货金额、消耗金额、当前库存金额、周转率、可售天数 → 见 [2026-06-27-foundation-updates-design.md](2026-06-27-foundation-updates-design.md) 第 6 节。
- `start` / `end` 直接替换原 SQL 中的 `>= datetime('now','-7 days')` 边界。

#### 2.6.5 导出兼容

- `/summary/export?start=...&end=...` 输出 CSV，内容遵循当前可见项（PRD 锁定：导出遵行当前可见项）。
- 保留 `range=` 参数向后兼容（忽略，但 URL 拼接不报错）。

#### 2.6.6 测试要点

- 单元：URL 参数解析（start/end 各种组合 → SQL 时间边界）。
- 集成：边界值（跨度 365 天、未来日期、跨年）。
- 集成：导出 CSV 与页面数据一致。

---

## 3. 开发模式与测试要求

### 3.1 开发模式（横切约束）

1. **每项能力 = 一个独立子项目**：每个子项目独立 spec → plan → 实现 → review → merge。
2. **每个子项目独立 PR**，独立 commit 历史，不混入其他子项目变更。
3. **实现顺序建议**（按依赖关系）：
   - 子项目 1：库存消耗预测（其他能力的上游）
   - 子项目 2：门店 + 总仓采购建议（依赖 1）
   - 子项目 3：通知事件总线（横向，被 5、6 使用）
   - 子项目 4：品类与品项发布（独立）
   - 子项目 5：配方发布 + 通知（依赖 3）
   - 子项目 6：Agent MPC 接口（依赖 1、2；写动作建议先于读动作细化）
   - 子项目 7：汇总表自定义时间（独立）

### 3.2 TDD 要求（强制）

- **所有新能力**严格 RED → GREEN → commit。每个子任务 = 一个失败测试 + 最小实现 + commit。
- **bug 修复**也写回归测试（先 RED 再修复）。
- **不接受**「先实现再补测试」「批量写测试」。

### 3.3 测试分层（强制）

每个子项目必须包含以下三层（缺一项视为不完整）：

| 层 | 工具 | 覆盖 |
|---|---|---|
| **单元** | pytest，纯函数 / 算法 / 业务规则 | 预测算法、安全库存计算、冲突检测、版本号分配、token 校验、事件 fanout 等 |
| **集成** | pytest + Flask test_client + 现有 `logged_client` fixture | 每个新路由正常 + 异常路径；写动作的幂等性、并发基础正确性 |
| **E2E** | Playwright（项目已有 `.playwright-mcp/`） | 关键用户路径：发布 → 门店可见、预测 → 采购 → 采纳 → CSV、配方发布 → 通知 → 标记已读 |

### 3.4 覆盖率指标

- **不设统一覆盖率指标**（按 common/testing.md 但针对本项目放宽）。
- **必须覆盖**关键路径：所有 PRD 锁定公式、必须自动化场景、错误处理兜底。
- **允许** UI 装饰层、Jinja 模板小修改覆盖率较低。

### 3.5 验证门（本地，无 CI）

每个子项目合并前必须满足：

1. `pytest -q` 全绿。
2. `ruff check .`（如未配置则项目需新增 ruff 配置 — 子 spec 决定）。
3. 手动启动应用，按子 spec「手工验证清单」逐条执行。
4. PR 描述必须包含：
   - 改动文件清单（一行一个）
   - `pytest -q` 输出末尾
   - 关键 E2E 截图（发布后门店看到的页面、配方通知红点）
   - 引用的 PRD 章节编号
5. 合并需 owner 确认（admin / 当前 reviewer）。

### 3.6 必须自动化的可观测性

每个子项目交付时，必须在 `/admin/health` 或专属页面显示其自动化任务的最近成功时间：

- 预测每日自动跑批
- 采购建议事件触发重算
- 补货预警推送
- 通知 fanout 状态

---

## 4. 优先级与里程碑（建议，非强制）

| 阶段 | 子项目 | 估时（参考） |
|---|---|---|
| Phase 1（核心闭环） | 2.1 预测、2.2 采购建议、2.3 MPC 接口（仅读）、横切 A1-A3 | 见子 spec |
| Phase 2（发布与通知） | 2.4 品项发布、2.5 配方发布 + 通知、横切 A4 A6 | 见子 spec |
| Phase 3（优化体验） | 2.6 汇总自定义时间 | 见子 spec |

Phase 1 完成前不接受 Phase 2 / 3 启动。Phase 2 内部 2.4 / 2.5 可并行（通知事件总线抽到独立子项目 3）。

---

## 5. 成功度量（产品级）

| 能力 | 度量 | 目标 |
|---|---|---|
| 2.1 预测 | 至少 7 天数据的品项中，预测误差 ±20% 内的比例 | ≥ 80% |
| 2.2 采购建议 | 总仓经理每周省下的手工盘点/合并时间 | ≥ 2 小时（访谈基线） |
| 2.3 MPC 接口 | 至少 1 个外部 Agent 稳定运行 | 持续 30 天 |
| 2.4 发布 | 门店接收新品项的端到端延迟（admin 发布 → staff 可见） | ≤ 5 分钟 |
| 2.5 通知 | 配方发布后 24h 内目标门店 staff 进入详情页的比例 | ≥ 80% |
| 2.6 汇总 | 至少 50% 登录用户使用过任意起止日期 | 30 天统计 |

---

## 6. 范围之外（明确不做）

- 多租户 / 订阅计费
- 移动端原生 App（仍走 PWA）
- 复杂 ML 模型（季节性分解、LSTM 等）— PRD 仅承诺基础加权平均语义
- 通知多渠道（邮件、IM、推送）— 仅 Web
- 配方成本自动核算、自动报价
- 跨平台审计日志聚合
- Agent 接口限流、自动封禁（留口子，后续 spec）

---

## 7. 实施下一步

每个子项目按以下顺序产出文档：

1. `docs/superpowers/specs/YYYY-MM-DD-<topic>-design.md`（spec）
2. `docs/superpowers/plans/YYYY-MM-DD-<topic>-plan.md`（plan）
3. 实现代码 + 测试
4. PR

每个 spec 文档必须**引用本 PRD 对应章节**（如 `> 引用 PRD 2.1.2 数据契约`），不得删除或放宽 PRD 锁定项。

---

## 8. PRD 锁定项速查

为便于子 spec 校对，下面是**不可放宽**的清单：

- 2.1：冷启动不返回数值；每日自动跑；范围含产品
- 2.2：安全库存公式；采纳仅生成 CSV；总仓简单加总（不做调拨推荐）
- 2.3：读 + 有限写；按 token + 路径白名单；access.log + 管理页面
- 2.4：模板不是门店型；冲突必须人工选；按门店发布；事件追溯表
- 2.5：配方版本化；按门店发布；通用事件总线 Web 首期
- 2.6：替换 `range=` 参数；导出遵行可见项
- 横切 A1-A6 必须自动化
- TDD 强制；测试三层缺一不可
- 每项必须有 `/admin/health` 可观测性

任何子 spec 放宽上述项必须先回到本 PRD 修订版本号并 re-review。