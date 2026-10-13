# DailyCheck 权限精细化 — Phase 1 架构草稿

> 产出：MVP 开发专家团 · 首席架构师 高见远
> 日期：2026-10-12
> 基线：`main @ 75af9c6`（含用户管理 v2）；输入：`docs/2026-10-12-permission-granularity-assessment.md`
> 方向（Eric 已确认）：**渐进升级 — 先统一纪律止血，后操作级权限点 RBAC**；管理权中心化 + 预留租户/仓级管理员扩展点；MCP token ACL 与 Web 权限词汇表统一
> 状态：**草稿（Phase 1 门禁输入，待 team-lead 汇总为 PRD/架构/UIUX 三文档）**

---

## 0. 范围与不做什么（out-of-scope）

**本期范围（Phase 1 架构定义 → Phase 2/3 实施）**
1. 从"角色=等级"升级为"**角色=权限点集合**"，保留 `require_role` 兼容层
2. 权限词汇表（permission catalog）+ 3 内置角色包（存量零行为变化）
3. `@require_permission` / `@require_any_permission` 装饰器 + 守卫收敛
4. 单一事实来源（manifest）——守卫、UI 导航、测试三者共用
5. MCP `agent_tokens` path ACL 与 Web 权限词汇表统一（渐进，不破坏存量 token）
6. 审批分级 / 数据范围：**本期只落 schema 扩展点，不实现逻辑**
7. "路由×角色" deny/allow 回归基线（防倒挂机制化）

**本次不做（明确 out-of-scope）**
- 不做自定义角色的完整 CRUD UI（Phase 2 可选二期；本期只让数据模型支持）
- 不实现条件权限（金额阈值审批、双人复核、data_scope 运行时求值）——只留 schema
- 不引入外部权限引擎（Casbin/Oso/Flask-Principal）——见 ADR-001
- 不改 `warehouse_users` 绑定主键、不改 `users.is_admin` 平台超管语义
- 不动租户隔离三道闸（`blueprints/auth.py:86-122`）——RBAC 是在其上叠加，不替换
- 不重写 MCP 现有 path ACL 引擎（`mcp_server/agent_mpc_pure.path_matches` 复用）

**版本锚定（钉死，供 Phase 2 实施者按此写）**
- Python `3.13.x`（当前 `.venv` 实测 3.13.12；CI 兼容 3.9+ 见 conftest 注释）
- Flask `3.1.1`、gunicorn `23.0.0`、mcp `>=1.2.0`、SQLite（标准库 `sqlite3`）
- 不新增任何第三方依赖（`requirements.txt` 保持 9 行不变）

---

## 1. 技术选型对比矩阵（结论先行）

### 1.1 候选方案

| 维度（权重） | **A. 自研轻量权限点** | B. Casbin (pycasbin) | C. Flask-Principal | D. Oso (Python) |
|---|---|---|---|---|
| 学习成本（高） | ★★★★☆ 纯 SQL + 1 个装饰器，团队已会 | ★★☆ 需学 PERM 元模型 + model.conf + 策略语法 | ★★★ 概念旧但 API 简单 | ★☆ Polar 语言 + 关系模型 |
| 生态成熟度（高） | ★★★★ 无依赖即无生态风险 | ★★★★★ 1759★、2M 下载/月、Apache 2.0、活跃 | ★☆ **已废弃**（最后发布 2013-06-14） | ★★ **OSS 库已弃用**（2023 年底），转 Oso Cloud 托管 |
| 依赖负担（高） | ★★★★★ **+0 依赖** | ★★ 纯 py 但需 `casbin-sqlalchemy-adapter` → **拖入 SQLAlchemy** | ★★★ 无运行时依赖但装不上现代 Flask | ★★ Rust core + `oso_cloud` |
| 多 worker 部署适配（高） | ★★★★★ **每请求读 DB**，与现有 `g.role` 模式一致，无漂移 | ★★ **enforcer 在内存，gunicorn 多 worker 各持一份策略副本，缺 watcher 时改权限不生效/吊销失效** | N/A | ★★ 托管服务，数据出 VPS |
| SQLite 适配（高） | ★★★★★ 与现有 master.db 同库同事务 | ★★★ 需 adapter；无 Redis watcher（单 VPS 无中间件） | ★★ 无 DB 集成 | ★★★ sqlalchemy-oso（已停更） |
| 与既有代码契合（高） | ★★★★★ 复用 `g.role`/三道闸/`ROLE_RANK` | ★★ 需适配层，词汇表与 MCP ACL 需二次翻译 | ★★ 需重写鉴权流 | ★★ 需重写 |
| 表达力上限（低，MVP 不在意） | ★★★ 够用；条件权限需自建 | ★★★★★ ABAC/deny-override/role 继承 | ★★ 仅 flat permission | ★★★★★ 关系授权 |
| 团队熟悉度（高） | ★★★★★ | ★★ | ★★ | ★ |

### 1.2 结论

**选 A：自研轻量权限点 RBAC。**

三条决定性理由（按权重排序）：
1. **部署适配是杀手级**：DailyCheck 是 gunicorn 多 worker + 单 VPS + SQLite。Casbin 默认 Enforcer 把策略**加载进进程内存**，多 worker 各持一份副本；官方文档明确"缺 watcher 时，通过实例 A 授予的权限对实例 B 不可见，更糟的是通过 A 吊销的权限在 B 上继续生效直到重启"。而 Casbin 的 watcher 需要 Redis/etcd/Kafka/NATS/DB 队列——单 VPS MVP 没有这些中间件。**自研方案每请求从 master.db 读权限集（与现有 `g.role` 每请求查询完全同构），天然无跨 worker 漂移。**
2. **依赖负担**：本项目 `requirements.txt` 只有 9 行，刻意极简。Casbin 走 SQLite 要拉 SQLAlchemy（一整条 ORM 链），与"不选不熟悉的技术/Docker+gunicorn 轻部署"的 MVP 纪律相悖。
3. **词汇表统一**：目标要 MCP path ACL 与 Web 权限共用一套词汇。Casbin 的 `{sub, obj, act}` 与 MCP 的 path glob（复用 `path_matches`）不是同一模型，仍需一层翻译表。自研可以一次性定义 `permissions` + `permission_routes` 两张表，Web 守卫与 MCP 校验读同一份数据。

**否决理由留痕**：
- **Flask-Principal 直接否决** — 最后发布 2013-06-14（>4800 天未更新），仓库虽迁到 `pallets-eco` 但仅有 typing 类提交、无功能发布。生产不可用。
- **Oso Python 否决** — 原 OSS 库（`osohq/oso`）2023 年底弃用（最后 `oso 0.27.3` / 2024-01-13），公司已转向 Oso Cloud **托管**服务。托管 = 权限决策数据离开自托管 VPS，与项目"数据留在自己 VPS + 多租户数据隔离"的定位冲突；且引入 Rust core。
- **Casbin 保留为逃生舱（不采用，但架构为其留路）** — 若未来出现"大量自定义角色 + 条件 ABAC + deny-override"的真实需求，DB schema 设计为可机械映射到 `casbin_rule`（`role_permissions` → `p` 规则，用户绑定 → `g` 规则），届时可平滑迁移。见 ADR-001 Consequences。

> 成本参考（`references/architecture/mvp-stack.md` 与 `cost-models/development-costs.md`）：本方案属"后端中等改造"，估 2-3 人周（Phase 2 主体），与盘点建议的 2-3 个迭代一致；AI 辅助下可压到 1 人周量级。无新增云成本（复用现有 VPS）。

---

## 2. ADR 草稿（MADR 格式）

> 存放目标：`项目/docs/decisions/ADR-XXX.md`（Phase 2 实施时落库）

### ADR-001：使用自研轻量权限点 RBAC 作为授权机制

```
# ADR-001: 使用自研轻量权限点 RBAC 作为授权机制（而非 Casbin / Flask-Principal / Oso）

## Status
Proposed (2026-10-12) — 待 Eric 确认后 Accepted

## Background
现状是"角色=单调等级"（staff<manager<admin, config.py:63 ROLE_RANK），无法表达
"能审批看不到成本""财务只读报表""实习生只录入"等需求。评估了 4 条路径：
自研权限点、Casbin、Flask-Principal、Oso。约束：gunicorn 多 worker + 单 VPS +
SQLite；requirements.txt 仅 9 行极简；需与 MCP agent_tokens path ACL 共用词汇。

## Decision
自研轻量权限点 RBAC：master.db 内 4 张表（permissions / roles /
role_permissions / permission_routes）+ 每请求读 DB 解析权限集 + 新装饰器
@require_permission。保留 require_role 作为兼容层。不引入任何第三方依赖。

## Consequences
正面：
- 零新增依赖，8GB VPS 单进程部署零额外中间件
- 每请求读 DB，无 Casbin 多 worker 策略漂移；权限变更即时全局生效
- 与 MCP path ACL 共用 permissions/permission_routes，词汇表真正单一
- schema 形状可机械映射到 Casbin casbin_rule（role_permissions→p, 绑定→g），
  未来若需 ABAC 可平滑迁移，逃生舱保留
负面：
- 条件权限（金额阈值、data_scope 运行时求值）需自建，不能白拿 Casbin 能力
- 无现成管理 API / 在线编辑器，管理 UI 需自己写（本就要写权限矩阵视图）
- 表达力低于 Casbin（无 deny-override、无角色继承链），当前需求用不上

## Related ADRs
ADR-002（存储位置）、ADR-003（兼容层）、ADR-004（MCP 统一）
```

### ADR-002：权限数据存 master.db（而非独立 config 文件或 policy.csv）

```
# ADR-002: 权限数据存 master.db，与用户/绑定同库

## Status
Proposed (2026-10-12)

## Background
权限词汇表与角色包需要一个持久化位置。选项：Python 代码常量 / 独立
policy.csv / master.db 表。

## Decision
存 master.db（与 users/warehouses/warehouse_users/tenants 同库）。
代码里保留 PERMISSION_CATALOG 常量作为"编译期唯一事实来源"，启动时
以 INSERT OR IGNORE 幂等播种进 DB——代码常量是种子，DB 是运行时真源。

## Consequences
正面：
- 与绑定关系（warehouse_users）同库同事务，角色解析是一次 JOIN，无跨介质一致性
- 走既有 init_master_db() 幂等迁移通道，不新增迁移机制
- 天然 tenant_id 可扩展（未来租户级自定义角色）
- 每仓独立 SQLite 的模型不受影响（权限是平台级，本就在 master.db）
负面：
- 权限改动需 DB 写入 + 事务，比改代码常量重；但权限改动本就不该高频
- 需处理"代码目录新增权限点但 DB 未播种"——由幂等播种 + 启动校验覆盖

## Related ADRs
ADR-001、ADR-004
```

### ADR-003：保留 require_role 兼容层，渐进迁移（不搞大爆炸）

```
# ADR-003: 保留 require_role 兼容层，@require_permission 渐进替换

## Status
Proposed (2026-10-12)

## Background
现有 82 条路由用 require_role/require_platform_admin/require_warehouse_type。
一次性全部改写为权限点 = 高回归风险（项目有 pre-existing 18 failed 测试基线，
零新增红线）。且 3 个内置角色包与现有 require_role 语义必须严格等价。

## Decision
- require_role 保持现状（仍读 ROLE_RANK），不删除、不改行为
- 新增 @require_permission / @require_any_permission，逐路由替换（一张迁移清单）
- 3 内置角色包的权限集合 == 现有 require_role 阶梯的等价展开（staff⊆manager⊆admin）
- 迁移期间两条路径可共存并交叉验证（同一路由先加权限点做影子断言，再删旧装饰器）

## Consequences
正面：
- 每步可发布、可回滚，符合项目工程纪律；存量 5 用户零行为变化可逐条验证
- 迁移清单本身就是"谁能做 X"的单一事实来源
负面：
- 过渡期两套语义并存，需一道测试断言二者一致（防长期分叉）
- 需在一次迭代末删除兼容层，否则永久技术债

## Related ADRs
ADR-001、ADR-005
```

### ADR-004：MCP token ACL 与 Web 权限词汇表统一（permission_routes 桥接）

```
# ADR-004: MCP agent_tokens 与 Web 共用 permissions/permission_routes 词汇表

## Status
Proposed (2026-10-12)

## Background
MCP 侧 agent_tokens 已有 path 级读写 ACL + 仓库白名单 + 限速（比 Web 更细），
但词汇是"HTTP 路径 glob"，与 Web 的角色/权限词汇互不相通。目标是一个词表
同时描述 Web 守卫与 Agent 令牌。

## Decision
新增 permission_routes 表把权限点映射到 HTTP (method, path_pattern)。
agent_tokens 增加可空列 permission_codes_json：
- 为空 → 走 legacy path ACL（存量 token 零变化）
- 非空 → 由权限点经 permission_routes 展开为允许路径，复用既有 path_matches
分三阶段（Phase A 播种 / B 双读 / C 收敛），见 §6。

## Consequences
正面：
- 一处定义权限点，Web 与 MCP 同语义；新增 MCP 工具只登记权限点即可
- 存量 token 完全不受影响（向后兼容）
负面：
- permission_routes 与真实路由会有漂移风险，需"路由×权限点"覆盖测试兜底
- 两套存储（path JSON + permission codes）并存期需明确优先级

## Related ADRs
ADR-001、ADR-002
```

### ADR-005：审批分级用 approval_policies 数据驱动（本期只落 schema）

```
# ADR-005: 审批分级用 approval_policies 数据驱动 + 预留职责分离(SoD)

## Status
Proposed (2026-10-12) — 本期仅 schema，Phase 3 实现

## Background
现审批一刀切（订货审批=DC manager，store_ordering.py:623-626），无金额/数量/
跨仓条件分级。评估：把阈值硬编码进代码 vs 数据驱动配置表。

## Decision
建 approval_policies 表：以 (tenant_id, scope, warehouse_code, [min_amount,
max_amount]) 命中一条策略，该策略给出 required_permission 与 step 次序；
多级审批 = 多行 step。附 segregation_of_duties 布尔列预留"提交人≠审批人"。
本期只建表 + 播种空集，不接任何 handler。

## Consequences
正面：
- 阈值调整是配置变更不是发版；多级审批/双人复核有清晰落点
- scope 用"动作标识"（如 order.submit / stocktake.submit）与权限点同源
负面：
- 空表 + 未接线的扩展点需在文档中显式标注"未实现"，避免实施者误以为已生效
- 命中算法（区间重叠判定）需在 Phase 3 定义唯一性约束，防止多行冲突

## Related ADRs
ADR-001、ADR-003
```

---

## 3. 权限模型数据设计草案

### 3.1 ER 关系（新增表，虚线为既有表）

```
                 ┌───────────────────────┐
                 │ permissions           │  ← 权限词汇表（单一事实来源 · DB 镜像）
                 │  code PK              │
                 │  category / risk /    │
                 │  description          │
                 └──────────┬────────────┘
                            │ N
                            │
                 ┌──────────▼────────────┐        ┌──────────────────────┐
                 │ role_permissions      │        │ permission_routes    │
                 │  role_id PK ─┐        │        │  permission_code PK ─┼─┐
                 │  permission_code PK   │        │  method / path_pattern│ │
                 └──────────▲────────────┘        └──────────────────────┘ │
                            │ N                                            │
                 ┌──────────┴────────────┐                                 │
                 │ roles                 │         (permission_code FK ────┘)
                 │  id PK / code /       │
                 │  tenant_id / is_builtin│
                 │  rank (兼容层) / name  │
                 └──────────▲────────────┘
                            │ code (string, 非 FK —— 见 3.4)
┌───────────────────────┐   │
│ warehouse_users (既有) │───┘
│  user_id / warehouse_id│        ┌──────────────────────┐
│  role  ← 存角色 code    │        │ approval_policies    │ ← Phase 3 扩展点
└───────────▲───────────┘        │  (tenant_id/scope/… ) │
            │                    └──────────────────────┘
┌───────────┴───────────┐
│ users (既有)           │  ── is_admin=1 平台超管（bypass，见 3.5）
│  id / tenant_id / ...  │
└───────────────────────┘
```

### 3.2 表结构（DDL 草案，写入 `db/__init__.py::MASTER_SCHEMA`）

```sql
-- 权限点词汇表。code 是跨 Web/MCP 的稳定键；risk 供 UI 分级高亮与测试断言。
CREATE TABLE IF NOT EXISTS permissions (
    code        TEXT PRIMARY KEY,            -- 'order.approve' / 'report.cost.view'
    category    TEXT NOT NULL,               -- 'order'|'stock'|'item'|'report'|'admin'|'data'
    description TEXT NOT NULL,               -- 中文可读（UI 矩阵展示）
    risk        TEXT NOT NULL DEFAULT 'normal',  -- 'normal'|'sensitive'|'critical'
    created_at  TEXT NOT NULL
);

-- 角色。内置 3 个 is_builtin=1；未来自定义角色 is_builtin=0 且 tenant 级。
-- rank 仅为 require_role 兼容层服务（映射 ROLE_RANK），自定义角色 rank 为 NULL。
CREATE TABLE IF NOT EXISTS roles (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT NOT NULL,                -- 'staff'|'manager'|'admin'
    tenant_id  INTEGER NOT NULL DEFAULT 1,
    name       TEXT NOT NULL,                -- 中文显示名
    is_builtin INTEGER NOT NULL DEFAULT 0,
    rank       INTEGER,                      -- 1/2/3（仅内置）
    created_at TEXT NOT NULL,
    UNIQUE(tenant_id, code)
);

-- 角色 = 权限点集合。内置包由 PERMISSION_CATALOG 幂等播种。
CREATE TABLE IF NOT EXISTS role_permissions (
    role_id         INTEGER NOT NULL,
    permission_code TEXT NOT NULL,
    PRIMARY KEY (role_id, permission_code),
    FOREIGN KEY (role_id) REFERENCES roles(id),
    FOREIGN KEY (permission_code) REFERENCES permissions(code)
);

-- 权限点 → HTTP 路由映射（Web 守卫与 MCP path ACL 共用）。见 §6。
CREATE TABLE IF NOT EXISTS permission_routes (
    permission_code TEXT NOT NULL,
    method          TEXT NOT NULL,           -- 'GET'|'POST'|'PATCH'|'DELETE'|'*'
    path_pattern    TEXT NOT NULL,           -- '/store-ordering/orders/*'（glob，复用 path_matches）
    PRIMARY KEY (permission_code, method, path_pattern)
);

-- Phase 3 扩展点（本期空表，未接线 —— 见 ADR-005）。
CREATE TABLE IF NOT EXISTS approval_policies (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id            INTEGER NOT NULL DEFAULT 1,
    scope                TEXT NOT NULL,      -- 动作标识，如 'order.submit'
    warehouse_code       TEXT,               -- NULL = 全租户
    min_amount           REAL,               -- 区间下界（含），NULL = 无下界
    max_amount           REAL,               -- 区间上界，NULL = 无上界
    required_permission  TEXT NOT NULL,      -- 命中后所需权限点
    step                 INTEGER NOT NULL DEFAULT 1,  -- 多级审批次序
    segregation_of_duties INTEGER NOT NULL DEFAULT 0, -- 1 = 提交人≠审批人
    created_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_approval_policies_lookup
    ON approval_policies(tenant_id, scope, warehouse_code);
```

**索引说明**（避免过早优化，MVP 只建必要的）：
- `warehouse_users` 的 `(user_id, warehouse_id)` 已是主键，解析走它，无需新索引
- `role_permissions` 主键即覆盖查询
- `approval_policies` 建 `(tenant_id, scope, warehouse_code)` 复合索引（未来按此查）
- 不建 `permissions.category` 索引（全表 < 60 行，全扫更快）

### 3.3 权限词汇表（PERMISSION_CATALOG，草案）

> 由现有 `require_role` 调用点（见 §3.4 映射）反推，**先覆盖已守卫的 82 条 + 1.3 节 21 条写操作**。`risk` 分级：critical=不可撤销/涉钱/涉权；sensitive=影响库存账/成本数据；normal=查询录入。

| category | code | risk | 现有对应守卫 |
|---|---|---|---|
| order | `order.create` | normal | store_ordering 下单（staff） |
| order | `order.approve` | critical | store_ordering:626（manager，DC 审批） |
| order | `order.ship` | sensitive | store_ordering:978/1020/1065（admin，DC 发货） |
| order | `order.receive` | sensitive | store_ordering:669/773（staff/manager） |
| order | `order.cancel.any` | critical | store_ordering.py:715-757 状态矩阵 |
| stock | `stock.outbound` | critical | outbound:70（**当前裸奔，需补**）+ outbound:127 |
| stock | `stock.outbound.rollback` | critical | outbound:158（staff，回退扣账） |
| stock | `stock.stocktake` | normal | stocktake:350/408/431/477（staff 录入） |
| stock | `stock.stocktake.approve` | critical | stocktake:102/118/193/204（admin 审核） |
| stock | `stock.adjust` | sensitive | adjustment:94/124（staff） |
| stock | `stock.restock` | sensitive | restock:146（**当前裸奔，需补**）+ restock:185 |
| item | `item.view` | normal | items 列表（staff） |
| item | `item.edit` | sensitive | items:364/381（manager） |
| item | `item.create` | sensitive | items 新增 |
| item | `item.delete` | critical | **历史事故点**，items 删除（现 platform_admin） |
| item | `item.price.view` | sensitive | 售价/成本可见（**新需求：成本可见性**） |
| item | `item.price.edit` | critical | 售价/采购价编辑（Q6 canonical_managed） |
| report | `report.view` | normal | reports:189（manager 门槛；**新需求：可下放**） |
| report | `report.export` | sensitive | `/export/consumption`、`/summary/export`（**当前仅登录**） |
| report | `report.cost.view` | sensitive | `/report/outbound`、`/report/inbound` 成本列（**当前仅登录**） |
| admin | `admin.user.manage` | critical | users.py（require_platform_admin ×13） |
| admin | `admin.token.manage` | critical | agent_tokens.py（内联 `_require_admin`） |
| admin | `admin.warehouse.manage` | critical | users.py:627/710 |
| admin | `admin.canonical.manage` | critical | canonical:206/278/340/371 |
| admin | `admin.role.manage` | critical | **新增**（权限矩阵编辑/自定义角色，Phase 2 UI） |

> `item.price.view` / `report.cost.view` 是"报表与成本可见性"痛点的正面回答；`admin.*` 支撑"管理权中心化 + 租户/仓级管理员扩展点"。

### 3.4 3 内置角色包（存量零行为变化的映射）

**关键不变式**：内置包必须严格镜像现有 `ROLE_RANK` 阶梯的等价展开 —— `staff ⊆ manager ⊆ admin`，且每个 code 的授权结果与对应 `require_role` 调用点逐一对应。

```
staff   包 = 现有 @require_role("staff") 覆盖面
          {order.create, order.receive, stock.outbound, stock.outbound.rollback,
           stock.stocktake, stock.adjust, stock.restock, item.view, report.view*}

manager 包 = staff ∪ 现有 @require_role("manager") 覆盖面
          = { …staff…, order.approve, order.ship, item.edit, item.create,
              item.price.view, report.export }
          *注：reports.py:189 现为 manager 门槛 → report.view 归 manager 包，
           若 Eric 确认要下放给 staff 属"做加法"，单独决策，不在零变化基线内

admin   包 = manager ∪ 现有 @require_role("admin") 覆盖面
          = { …manager…, stock.stocktake.approve, item.delete, item.price.edit,
              report.cost.view, admin.canonical.manage }
```

- **平台管理员**（`users.is_admin=1`）不是仓库角色，见 §3.5，**不进 role_permissions**。
- 播种 SQL 由 `PERMISSION_CATALOG` + `ROLE_PACKAGES` 两个常量生成，`INSERT OR IGNORE` 幂等；角色包变更走显式 `--reseed-builtin-roles`（见 3.6）。

### 3.5 平台超管与租户/仓级管理员（管理权中心化）

- **保持 `users.is_admin=1` 为平台超管**，语义不变：越过仓库角色校验（等价 Casbin 的 `root`）。`@require_permission` 与 `@require_platform_admin` 均对 `is_admin` 放行/要求，行为与现状一致。
- **中心化**：`admin.*` 权限点默认只授给"平台超管"或"持有 admin 仓库角色的用户"——即管理入口集中在 users.py / agent_tokens.py，路径不变。
- **预留分层扩展点**（不实现）：
  - 租户管理员 = 未来某角色（`roles` 表 `tenant_id != 1` + 持有 `admin.user.manage` 但**数据范围限定本租户**）→ 需 Phase 3 的 data_scope
  - 仓级管理员 = 现"仓库角色 admin"即等价物（`warehouse_users.role='admin'`，只在该仓生效）
  - 结论：**当前"仓库 admin"已是"仓级管理员"**；"租户管理员"待租户商业化时以 `roles.tenant_id` + data_scope 落地。

### 3.6 迁移策略与幂等性

沿用既有 `init_master_db()` 的 PRAGMA 守卫范式（`db/__init__.py:757-880`）：

1. **建表**：4 张新表进 `MASTER_SCHEMA`（`CREATE TABLE IF NOT EXISTS`）——对存量 master.db 是 no-op 追加，**不破坏** tests/test_master_db_migrate.py 的既有断言（新增表不触发旧列迁移分支）。
2. **播种**（幂等）：`INSERT OR IGNORE INTO permissions` / `roles` / `role_permissions`，来源为 `PERMISSION_CATALOG` / `ROLE_PACKAGES` 常量。重复启动无副作用。
3. **列迁移**：`agent_tokens` 增 `permission_codes_json TEXT`（PRAGMA 守卫，仿 `rate_limit` 列，`db/__init__.py:830-838`）。
4. **版本戳**：master.db 目前无 user_version 门（每次启动都跑 PRAGMA 守卫）。**保持现状**，不为 RBAC 单独引入版本戳（避免与 `migrate_warehouse_db_columns` 的版本门混淆）。
5. **回归**：`tests/test_master_db_migrate.py` 必须新增"空库 → init_master_db → 4 表存在 + 种子行数正确"用例 + "已有库重复 init 无重复行"用例。
6. **backfill**：`role_permissions` 播种后，**断言**每个内置角色包的权限数 > 0 且阶梯嵌套（`staff ⊆ manager ⊆ admin`），失败即启动告警——防止空包导致全线 403（沉默失败）。

> **已知坑（内嵌，实施必读）**：
> - `warehouse_users.role` 存的是**角色 code 字符串**（非 FK）。解析权限必须处理"code 在 roles 表查不到"的情况——**默认拒绝（fail-closed）并记日志**，绝不允许 fallback 到放行。历史事故正是"越权放行"。
> - `INSERT OR IGNORE` 不会更新已存在行；若改了 `ROLE_PACKAGES` 常量，**必须**用 `--reseed-builtin-roles`（先 DELETE 内置角色的 role_permissions 再 INSERT）显式重播种，否则新旧权限混杂。此命令**不动**自定义角色（is_builtin=0）。
> - 播种脚本需在 gunicorn 多 worker 并发启动下安全：复用 `init_master_db()` 已有的 `fcntl.flock` 文件锁（`db/__init__.py:776-778`），勿另起加锁机制。

### 3.7 成本类字段边界清单（`report.cost.view` 管控范围）

> 目的：界定"哪些表/字段属于成本口径"，需在报表与导出中受 `report.cost.view` 管控；并厘清它与 `item.price.view` 的分工。基于全库 schema 扫描（`db/__init__.py` + `blueprints/`）逐项判定。

#### 判定规则（决定性，先适用规则再落表）

- **R1 单价 vs 派生**：**存储的单价字段**（`unit_cost` / `selling_price` / `sale_price`）→ 归 `item.price.view`；由"数量 × 单价"派生出的**金额/价值**（无论聚合 SUM 还是逐行）→ 归 `report.cost.view`。
- **R2 上下文优先（不叠加双重门槛）**：同一数据出现在不同页面时，按**所在页面上下文**取权限——单品主数据/详情页以 `item.price.view` 为准；报表/汇总/导出页以 `report.cost.view` 为准。不要求同时满足两者（避免可用性塌陷）。
- **R3 成本 vs 售价 vs 毛利**：`item.price.view` 同时覆盖**成本与售价**两项（承接 §3.3 定义"售价/成本可见"）；但**"售价 − 成本"派生的毛利/报价差**属成本口径，归 `report.cost.view`。

#### A. 存储字段（单价口径，归 `item.price.view`；出现在报表页时按 R2 转 `report.cost.view`）

| 表.字段 | 口径 | 归属权限点 | 判定理由 |
|---|---|---|---|
| `items.unit_cost` | 成本（进货单位成本） | `item.price.view` | 存储单价（`db/__init__.py:572`）。**既有先例**：items 页已对 rd 上下文隐藏该字段（`items.py:23-35` 注释"研发中心 can see items but not unit_cost"），本清单只是把它权限点化 |
| `items.selling_price` | 售价（门店快照） | `item.price.view` | 存储单价（迁移新增，`db/__init__.py:1051`） |
| `items.selling_price_updated_at` | 售价元数据 | 跟随 `item.price.view` | 仅时间戳，无独立敏感度 |
| `items.price_follow_canonical` | 售价归属开关 | 跟随 `item.price.view` | 布尔状态，无金额（`db/__init__.py:1084`） |
| `canonical_items.unit_cost` | 成本（主数据采购价，Q6=canonical_managed） | `item.price.view` | 成本源头（`db/__init__.py:290`） |
| `canonical_items.selling_price` | 售价（主数据统一维护） | `item.price.view` | 售价口径（`db/__init__.py:289`） |
| `ic_recipes.sale_price` | 售价（冰激凌配方） | `item.price.view` | 售价口径（`db/__init__.py:710`） |
| `recipes.sale_price` + `sale_price_updated_at` | 售价（出品配方） | `item.price.view` | 售价口径（`db/__init__.py:731-732`） |

#### B. 金额字段（经营金额口径，归 `report.cost.view`）

| 表.字段 | 口径 | 归属权限点 | 判定理由 |
|---|---|---|---|
| `daily_revenue.amount` | 金额（营业额） | `report.cost.view` | 经营敏感金额（`db/__init__.py:644`）；其**写**已受 `/api/revenue` manager 门槛（`test_permissions_batch1` FIX-4），**读**应受成本权限 |
| `store_orders.shipping_fee` | 金额（运费） | `report.cost.view`（报表/汇总）；单据内随 order 权限可见 | 经营金额（`db/__init__.py:812`）；单据上下文可见性由 `order.*` 承载 |
| `store_order_items.unit_price` | **非存储列**（计算值 `selling_price→unit_cost→0`） | 计算值不落库 | `store_ordering_pure.py:409-410` 现算现用；由它聚合的订单总额见 C 组 |

#### C. 派生金额/成本列（无独立存储，全部归 `report.cost.view`）

| 派生值 | 位置 | 归属 | 说明 |
|---|---|---|---|
| 库存总值 `SUM(quantity * unit_cost)` | `core.py:90`、`core.py:163` | `report.cost.view` | 全仓库存金额 |
| 消耗/期初/生产价值 `qty * unit_cost` | `core.py:65/72/80/109-121/161-163/206/245` | `report.cost.view` | 各类金额聚合 |
| 品项消耗金额 `SUM(qty * unit_cost)` | `items.py:290` | `report.cost.view` | 品项页的成本聚合列 |
| 配方成本 `line_cost().cost_purchase` | `recipe_cost_pure.py:33-51` | `report.cost.view` | 由 `unit_cost` 派生 |
| 配方报价差/毛利（含 `cost_selling` / margin） | `recipe_cost_pure.py`（`test_recipe_cost_pure.py:325` margin 用例） | `report.cost.view` | R3：毛利属成本口径 |
| 订单行小计/总额 `Σ(qty * unit_price)` | `store_ordering_pure.py:409-410`、`store_ordering.py:514-516` | `report.cost.view`（汇总/报表）；单据详情随 order 权限 | 经营金额 |
| 出库/入库报表成本列 | `reports.py`（`/report/outbound`、`/report/inbound`） | `report.cost.view` | 痛点"报表全量可见"的正面拦截点 |
| 导出金额列 | `/export/consumption`、`/summary/export` | `report.cost.view` | 痛点"导出全量可见"的正面拦截点 |

#### D. 明确不纳入成本口径（防过度管控）

| 表.字段 | 理由 |
|---|---|
| `items.gram_per_unit` / `aux_unit` / `aux_rate` | 单位换算，非金额 |
| `items.safety_stock` / `items.quantity` | 数量口径 |
| `procurement_cache.*`（`daily_avg`/`current_qty`/`in_transit_qty`/`safety_stock`/`suggested_qty`） | 全为数量口径，无金额列（`db/__init__.py:121-132`）；若未来折算"建议采购金额"→ 归 `report.cost.view` |
| `stocktakes.diff` / `adjustments.adjusted_quantity` | 数量差异，非金额 |

#### E. 与 §3.3 词汇表的衔接

- §3.3 中 `item.price.view`（sensitive）= **单品成本/售价字段**的读门槛；`report.cost.view`（sensitive）= **报表/导出金额与成本列**的读门槛。本节 R1-R3 即二者的判定细则。
- 写侧不变：`item.price.edit`（critical）继续管售价/采购价编辑（Q6 canonical_managed）；`item.price.view` 只管读。
- **待 Eric 定级（列入 §8.3）**：营业额 `daily_revenue.amount` 是否单列 `report.revenue.view`（与成本解耦，供"财务只读报表但不看成本"角色）；当前草案默认并入 `report.cost.view`。

---

## 4. 装饰器设计与守卫收敛

### 4.1 新增装饰器（`permissions.py`）

```python
def require_permission(*codes: str) -> Callable:
    """全部 codes 都在当前仓权限集内才放行；平台超管(is_admin)放行。

    codes 为空 → 视为 require_login（防误用）。
    未选仓 → 转 picker（与 require_login 一致）。
    """

def require_any_permission(*codes: str) -> Callable:
    """任一 code 命中即放行（OR 语义，用于'审批员或平台管理员'类）。"""
```

- **权限集解析**：新增 `current_permissions() -> frozenset[str]`，从 `g`（若已解析）或每请求按 (user_id, warehouse_id, tenant_id) 做一次 JOIN 取回，缓存到 `g.perm_set`（**仅请求内缓存**，不跨请求、不进进程内存——这正是避开 Casbin 多 worker 漂移的关键）。
  ```sql
  SELECT rp.permission_code
    FROM warehouse_users wu
    JOIN roles r           ON r.code = wu.role AND (r.tenant_id = ? OR r.is_builtin = 1)
    JOIN role_permissions rp ON rp.role_id = r.id
   WHERE wu.user_id = ? AND wu.warehouse_id = ?
  ```
- **fail-closed**：解析异常 / 角色 code 未知 / 结果为空 → 空集（403），并 `app.logger.warning` 记录（不含敏感值）。
- `is_admin=1` → 直接放行（与 `require_role` 现状一致，保持平台超管语义）。

### 4.2 兼容层（require_role 不动）

- `require_role` / `require_platform_admin` / `require_login` / `require_warehouse_type` / `storefront_only` / `distribution_center_only` **保持签名与行为 100% 不变**。
- `ROLE_RANK` 保留；`roles.rank` 只是它的 DB 镜像（供 UI/测试读取），**运行时不改变 require_role 的判定**。
- 过渡期一致性测试：对同一路由断言 `require_role("manager")` 与 `require_permission(*manager_pkg)` 结果一致（防两套语义长期分叉）。

### 4.3 守卫收敛计划（P0 一致性修复）

| 反模式 | 位置 | 处置 |
|---|---|---|
| **死装饰器叠用 ×5** | items.py:44-45、items.py:85-86、canonical.py:206-207、forecast.py:171-172、procurement.py:334-335 | 删掉无效的 `@require_role(...)`，保留 `@require_platform_admin`（语义：仅平台超管）。改后行为不变，纯消除误读 |
| **平台管理员双实现** | `permissions.require_platform_admin` vs `agent_tokens._require_admin`（agent_tokens.py:51） | 删除 `_require_admin`，改用 `@require_platform_admin`；agent_tokens 两处路由（108/159）补声明式守卫 |
| **审计日志双实现** | users.py:835 vs agent_tokens.py:185 | 抽 `audit_pure.py::append_admin_audit(actor, action, target, detail)` 单一实现，两处调用 |
| **UI 散装字面量** | base.html:23-143（20+ 处 `g.role['role'] in (...)`） | 注入 `can(code)` 模板全局函数（context processor），模板改 `{% if can('admin.user.manage') %}`，见 §5 |
| **21 条写操作"仅登录"** | 盘点 1.3 节清单 | 逐条在 manifest 定级（Phase 1）+ 落 `@require_permission`（Phase 2）；**A 组**（功能有保护）只做实现收敛；**B 组**（保护级别不一致）按 manifest 统一；**C 组**（内联业务判断=仓成员）标 `data_scope=warehouse` 留 Phase 3 |

> **行为不变承诺**：以上除"B 组定级"外均为等价重构。B 组的定级需 Eric 评审（如 `outbound:70` 出库执行补 `stock.outbound` —— 与 127 回退对齐），属**有意收紧**，单独列出旧/新行为对照。

---

## 5. 单一事实来源（single source of truth）与 UI 收敛

**问题**：现状"谁能做 X"无单一来源——守卫在装饰器、意图散在模板字面量、文档只有一句话。

**方案**：一个可导入的 Python manifest，三方共用：

```
permissions_manifest.py            # 新增（架构唯一事实来源）
├── PERMISSION_CATALOG  (code → {category, description, risk})
├── ROLE_PACKAGES       (role_code → frozenset[code])   # 内置包，播种来源
├── ROUTE_MATRIX        (endpoint → required: code | ("any", codes) | "public" | "login")
└── SCOPE_NOTES         (endpoint → 数据范围备注，Phase 3 用)
```

消费方：
1. **运行时守卫**：`@require_permission` 的 codes 从 `PERMISSION_CATALOG` 取（字符串常量集中，防拼错）
2. **UI**：`app.jinja_env.globals["can"] = lambda code: code in current_permissions()`（context processor）；模板/base.html 的 20+ 字面量判断替换为 `can(...)`
3. **测试**：`ROUTE_MATRIX` 直接驱动 deny/allow 矩阵（§7）
4. **文档**：`docs/ARCHITECTURE.md` 的权限段改为引用 manifest（pre-commit hook 要求同步）

**防漂移**：`ROUTE_MATRIX` 与 `app.url_map` 的**覆盖测试**（§7 Test A）保证"新路由未登记 → 测试红"，即盘点要求的"防倒挂机制化"。

---

## 6. MCP token 与 Web 权限词汇表统一

### 6.1 现状（保留不推翻）
`agent_tokens`：`allowed_read_paths_json` / `allowed_write_paths_json`（glob）+ `allowed_warehouse_codes_json`（白名单）+ `rate_limit`。校验在 `mcp_server/service/auth.py::check_path`，glob 匹配复用 `agent_mpc_pure.path_matches`。**比 Web 更细，是资产不是负债。**

### 6.2 统一方案（三阶段，向后兼容）

**Phase A — 播种桥接表（本次架构定义）**
- 新增 `permission_routes(permission_code, method, path_pattern)`，把每个权限点映射到它保护的 HTTP 路由（glob 形式，与 MCP 现有 pattern 同语法）。
- `agent_tokens` 增可空列 `permission_codes_json TEXT DEFAULT NULL`。

**Phase B — 双读（Phase 2 实施）**
- MCP `check_path`：若 `permission_codes_json` 非空 → 由权限点经 `permission_routes` **展开成 path 集合**再走原 `path_matches`；为空 → 原 legacy path ACL 路径。
- Web `@require_permission`：直接查 `role_permissions`。
- 至此 Web 与 MCP **引用同一 `permissions` 表**；新增 MCP 工具只需登记一条 `permission_routes`。

**Phase C — 收敛（Phase 3+）**
- 管理 UI 的 token 创建页从"填路径 glob"升级为"勾选权限点"（勾选后生成 `permission_codes_json`）。
- `permission_routes` 由 §7 覆盖测试保证与真实路由一致。

### 6.3 `agent_tokens` ACL 演进路径小结

```
legacy (path glob)  ──Phase B 双读──▶  permission-code 模式（统一词汇）
   ▲                                          │
   └── 存量 token 永久兼容（permission_codes_json 为 NULL 即旧行为）
```

> 约束：`permission_routes` 的 pattern 必须与 `agent_mpc_pure.path_matches` 语义一致（同一匹配器），否则 Web 与 MCP 会出现"同名不同判"。**新增测试**：同一 (method, path) 在 Web 与 MCP 两侧判定一致。

---

## 7. 「路由×角色」自动化测试策略（防倒挂）

### 7.1 三层测试

**Test A — 路由覆盖（coverall，防漏登记）**
```python
def test_every_route_is_classified(app):
    registered = {(r.endpoint, m) for r in app.url_map.iter_rules() for m in r.methods}
    manifest   = ROUTE_MATRIX.keys()
    assert registered - manifest == set(), f"未登记路由: {registered - manifest}"
```
→ 新增路由未进 `ROUTE_MATRIX` ⇒ 测试**红**。这就是"防倒挂机制化"。

**Test B — deny/allow 矩阵（行为等价 + 越权拦截）**
- 参数化：`@pytest.mark.parametrize("endpoint, role, expected", matrix_cases())`
- `expected` 由 manifest × 角色包**推导**：`code ∈ ROLE_PACKAGES[role]` → 非 403；否则 403（平台超管单独一列恒放行，`public`/`login` 单独处理）。
- 夹具：复用现有 `logged_client`（admin）/ `staff_client`（staff），**新增** `manager_client`（role=manager）与 `manager` 包的工位。测试用 GET 只读路由为主（避免副作用），写操作用最小 body。
- 关键断言：`item.delete`（历史倒挂点）、`report.cost.view`、`admin.*` 对 staff **必须 403**；`@require_permission` 与 `require_role` 对同一路由结果一致（过渡期一致性）。

**Test C — 反倒挂不变式（静态）**
```python
def test_role_packages_are_nested():
    assert ROLE_PACKAGES["staff"] <= ROLE_PACKAGES["manager"] <= ROLE_PACKAGES["admin"]

def test_sensitive_perms_not_granted_to_staff():
    for code in ["item.delete","item.price.edit","admin.user.manage",
                 "admin.token.manage","report.cost.view","stock.stocktake.approve"]:
        assert code not in ROLE_PACKAGES["staff"]
```
→ 任何未来"给 staff 加个删除权限"的改动会在 CI 立刻变红。

### 7.2 继承既有风格与红线
- 新测试文件 `tests/test_route_permission_matrix.py`（沿用 `test_permissions_batch1.py` 的夹具风格）。
- 运行命令：`pytest --basetemp=/tmp/pytest-dc tests/test_route_permission_matrix.py`
- **pre-existing 18 failed 基线零新增**：新测试必须全绿，且不得改动 touch 既有失败用例的路径。
- 提交前 `ruff` 零新增。

---

## 8. 技术约束与不可行警告

### 8.1 硬约束（实施必须遵守）
| # | 约束 | 依据 |
|---|---|---|
| 1 | **禁新增第三方依赖** | `requirements.txt` 9 行极简 + MVP 纪律；Casbin/Oso 否决主因之一 |
| 2 | 只走 `init_master_db()` 幂等迁移通道；master.db 变更必须同步 `tests/test_master_db_migrate.py` | 团队级工程约束 |
| 3 | 权限解析 **fail-closed**（未知角色/异常 → 403），绝不 fallback 放行 | 历史越权事故（test_permissions_batch1） |
| 4 | 权限集**每请求解析**，不做进程级长期缓存 | gunicorn 多 worker 一致性（ADR-001 核心理由） |
| 5 | `warehouse_users.role` 是 code 字符串，**不改主键/不加 FK** | 存量绑定零迁移 |
| 6 | `is_admin=1` 超管语义不变 | 保持平台管理员中心化 + 兼容 |
| 7 | 架构子系统改动必须同步 `docs/ARCHITECTURE.md`（pre-commit 检查） | 项目 hook |

### 8.2 不可行 / 反模式警告（出现即不合格）
1. **引入 Flask-Principal** —— 库已废弃（2013 后无发布），现代 Flask 3.1 兼容性无保证。禁用。
2. **引入 Oso OSS（Python）** —— 已弃用并转向托管云；决策数据外流，与自托管 VPS 定位冲突。禁用。
3. **引入 Casbin 且使用默认内存 Enforcer** —— 多 worker 策略漂移（改权限不生效/吊销失效）。若未来必须上 Casbin，**强制**用 `SyncedEnforcer` + watcher（Redis/DB）+ 每次变更后 reload，且需先补齐中间件——**本期明确不引入**。
4. **把权限集缓存进进程全局** —— 与约束 #4 冲突，多 worker 下吊销失效。禁用。
5. **大爆炸式"一次全改 require_role→require_permission"** —— 破坏零新增红线、无法逐条回滚。禁用（ADR-003）。
6. **权限未知时默认放行** —— 最致命的沉默越权。禁用（约束 #3）。
7. **`permission_routes` 与真实路由脱节** —— 会让 MCP 拿到错误的 path 白名单。由 §7 Test A 覆盖 + §6.3 一致性测试兜底。
8. **空权限包** —— `role_permissions` 播种失败导致全站 403 的沉默失败。由 §3.6 步骤 6 断言兜底。

### 8.3 存疑/待 Eric 决策（不阻塞架构，但影响定级）
- `report.view` 现为 manager 门槛，是否下放 staff？（属"做加法"，不在零变化基线内）
- `outbound:70` 出库执行当前"仅登录"，补 `stock.outbound` 属**收紧**，需确认业务上 staff 是否本就该能出库（若该路由设计为 staff 可执行，则是补 `stock.outbound` 到 staff 包，而非收紧）。
- 租户管理员是否本期就要开设计决策（影响 `roles.tenant_id` 是否现在启用）？架构已预留，不阻塞。

---

## 9. 目录/文件组织（本期涉及，符合 Code Organization 规范）

```
DailyCheck/
├── permissions.py               # 改：+require_permission/+require_any_permission/+current_permissions
├── permissions_manifest.py      # 新：PERMISSION_CATALOG / ROLE_PACKAGES / ROUTE_MATRIX（<300 行）
├── audit_pure.py                # 新：append_admin_audit（去重 users.py/agent_tokens.py）
├── config.py                    # 改：保留 ROLE_RANK（兼容层），新增引用 manifest
├── db/__init__.py               # 改：MASTER_SCHEMA + init_master_db() 播种 + agent_tokens 列
├── blueprints/*.py              # 改：守卫收敛（等价重构 + 逐条迁移）
├── templates/base.html          # 改：字面量 → can(code)
├── mcp_server/service/auth.py   # 改：Phase B 双读
├── tests/test_route_permission_matrix.py  # 新：Test A/B/C
├── tests/test_master_db_migrate.py        # 改：+RBAC 表/种子断言
└── docs/
    ├── decisions/ADR-001..005.md          # 新：本文件 §2 落库
    ├── ARCHITECTURE.md                    # 改：权限段引用 manifest（hook 要求）
    └── permission-matrix.md               # 新：人类可读权限矩阵（从 manifest 生成）
```

单一职责：manifest 只放数据（无逻辑）；`permissions.py` 只放守卫（无数据）；播种在 `db/__init__.py`（数据落库）。

---

## 10. 端到端验证步骤（收尾即验收）

**核心成功流（存量零行为变化）**
```bash
cd /Users/ericmr/Documents/GitHub/DailyCheck
# 1. 建库播种
python -c "from db import init_master_db; init_master_db()"
sqlite3 db/master.db "SELECT code,is_builtin,rank FROM roles;"          # 期望 3 行内置
sqlite3 db/master.db "SELECT r.code, COUNT(*) FROM roles r
  JOIN role_permissions rp ON rp.role_id=r.id WHERE r.is_builtin=1 GROUP BY r.code;"
# 期望 staff<=manager<=admin 且均 >0

# 2. 路由×角色矩阵（含防倒挂）
pytest --basetemp=/tmp/pytest-dc tests/test_route_permission_matrix.py -v   # 全绿

# 3. 存量权限回归（历史事故不复现）
pytest --basetemp=/tmp/pytest-dc tests/test_permissions_batch1.py tests/test_tenant_isolation.py -v  # 全绿

# 4. master 迁移回归
pytest --basetemp=/tmp/pytest-dc tests/test_master_db_migrate.py -v         # 全绿

# 5. 零新增红线
pytest --basetemp=/tmp/pytest-dc 2>&1 | tail -3   # failed 数 == 18（不新增）
ruff check .                                       # 零新增
```

**关键错误流**
```bash
# 未知角色 code → fail-closed（403，非 200/302 放行）
# 构造 warehouse_users.role='ghost'，命中任意受保护路由，断言 403 + 日志告警
# 权限包为空 → 启动告警（不静默全 403）
```

**验收标准（完成定义）**：上述 5 步全绿 + 2 条错误流断言通过 + 新路由未登记会致 Test A 红（人为注入一条空 manifest 路由验证）。

---

## 11. 给 team-lead / PM / Designer 的交接要点

- **PM（PRD）**：§3.3 权限词汇表是需求语言；§8.3 三个存疑项需 Eric 定级后写入 PRD 验收。
- **Designer（UIUX）**：管理 UI 新增"角色权限矩阵"视图（角色 × 权限点网格，`risk` 列高亮 critical）；token 创建页 Phase C 改为权限点勾选。`can(code)` 模板函数是导航显隐的 UI 契约。
- **实施者**：§2 ADR + §4.3 收敛清单 + §3.6 已知坑是施工图；务必遵守 §8.1 七条硬约束。

> **无 `openapi.yaml` 不放行 Phase 2** —— 本改动若新增"权限矩阵 / 角色管理"管理接口，Phase 2 需先出 OpenAPI 3.0 契约（本草稿未含，属 Phase 2 交付）。
