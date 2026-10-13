# Spec — DailyCheck 权限精细化管理 v1.0

> 生成：2026-10-13 · 生成者：MVP 开发专家团 · 项目总监（大湾区靓仔）
> 基于：PRD v0.1 + 架构 v0.1（含 §3.7 成本类字段边界清单）+ UIUX v0.1（均为 `docs/2026-10-12-permission-*.md`）
> 上游确认：Eric 2026-10-13 确认三文档并拍板三项（见 §2.1）
> 状态：**已确认，锁定** —— 团队契约：此后设计细化、开发、测试均以本文件为唯一依据；变更须走 §13 变更流程
> 流程：自动推进设计细化 → 开发 → 测试交付；`git push` 前单独向 Eric 请示（既有铁律）

---

## 1. 产品定义

- **一句话**：把 DailyCheck 的权限体系从「三级单调阶梯」升级为「操作级权限点 RBAC」——角色 = 权限点集合，可精确表达"谁能审批、谁能看成本、谁能导出"，并以单一事实来源 + 全量回归基线保证一致性。
- **目标用户**：平台管理员（Eric）；门店/研发仓的管理者与一线成员；DC 经理；商业化后的财务/审计账号与 Agent 集成方。
- **核心问题**：三级角色做不了加法减法；守卫三套并存无单一来源；两次历史事故（删除倒挂、营业额越权）靠事后补丁；报表/成本零隔离；21 条写操作保护级别不一。

## 2. MVP 范围（阶段 1 — 锁定，不做列表见 §3）

| # | 功能 | 验收要点摘要 | RICE | 状态 |
|---|------|--------------|------|------|
| F1 | 权限矩阵单一事实来源 | `permissions_manifest.py`：PERMISSION_CATALOG / ROLE_PACKAGES / ROUTE_MATRIX 三方共用（守卫/UI/测试/文档） | 15.0 | 锁定 |
| F4 | 报表/成本可见性 + 内置角色包 | `report.view` / `report.export` / `report.cost.view` 生效；成本类字段服务端脱敏；**含只读/审计、财务角色包（Eric 拍板新增）** | 10.0 | 锁定 |
| F2 | 守卫收敛 | 死装饰器 ×5 清除；内联守卫回迁 `permissions.py`；审计日志单一实现；行为等价 | 9.0 | 锁定 |
| F5 | 敏感操作收紧 | 出库/回退/盘点审批/调整/状态跃迁逐点定级落守卫；`stock.outbound` 显式归基础包（Eric 拍板） | 7.2 | 锁定 |
| F3 | 21 条写操作定级 | 定级表（旧/新行为对照）逐条落声明式守卫；**提交 Eric 复核** | 4.8 | 锁定 |
| F9 | 路由×角色回归基线 | Test A 覆盖（未登记即红）/ Test B deny-allow 矩阵 / Test C 嵌套不变式 | 4.5 | 锁定 |

### 2.1 Eric 拍板回填（2026-10-13）

| 决策项 | 结论 | 落地位置 |
|--------|------|----------|
| 只读/审计、财务角色包 | **本期纳入** → 内置包共 5 个（staff/manager/admin/viewer/finance） | 附录 A、§6 播种 |
| 出库执行 `/outbound/submit`（现状无角色） | **显式开放**：`stock.outbound` 归基础作业包（行为不变，仅显式化） | 附录 A |
| 报表查看 `report.view` | **维持现状**：staff 可见非成本报表；成本列/导出单独把闸 | 附录 A、§9 AC-06 |
| 营业额 `daily_revenue.amount` 单列权限点（架构 §3.7-E 待定级） | **不单列**，并入 `report.cost.view` 管控；未来可按需做加法 | 附录 B 记录 |
| 内置角色包"不可改、需克隆" | 采纳（行业惯例） | §6 播种规则 |
| 租户/仓级管理员分权 | 本期不开（`roles.tenant_id` 保持默认 1）；随"中心化 + 预留扩展" | §3、§10 |
| **① `stocktake.approve` 级别** | **收紧到 manager+**：`stock.stocktake.approve` 纳入 manager 包（staff 不再可批） | 附录 A、附录 B.1 |
| **② `items.delete_item`** | **维持平台级**：不进任何角色包 | 附录 A、附录 B.1 |
| **③ `item.view`/`item.edit`（`/items`）** | **维持平台级**：不放开，仅收敛叠加死装饰器 | 附录 A、附录 B.1 |
| **④ `/summary`、`/inventory` 成本金额** | **不纳入脱敏**（口径例外） | §10、附录 B.1 |
| **⑤ `production.submit`** | **维持 LOGIN 现状** + OPEN 登记 | §1.3、附录 B.2 |

## 3. 明确不做（Out-of-Scope — 锁定）

| 不做的功能 | 原因 | 何时考虑 |
|------------|------|----------|
| 自定义角色完整 CRUD UI（DB 驱动） | Backlog（F10）；本期内置 5 包 + 克隆模式已覆盖 | 阶段 2 之后立项 |
| 租户/仓级管理员分权 | 多租户全链路未租户化；需 data_scope | 商业化阶段（F11） |
| 条件权限运行时（金额阈值审批、双人复核、data_scope 求值） | 只落 schema（`approval_policies` 空表） | 阶段 3（F6） |
| 完整 ABAC / 属性表达式引擎 | 过度工程；行业"金额阈值 + 风险等级"两轴已够 | 不做（保留 Casbin 逃生舱） |
| 前端导航 20+ 字面量全面重写（`can()` 替换） | 避免一次动 20+ 模板引发回归 | 阶段 2（F8 一并处理） |
| 权限矩阵管理 UI（`/admin/permissions` 页面） | 阶段 2（F8）；本阶段仅数据层 + 绑定下拉扩展 | 阶段 2 |
| MCP token 权限点勾选化（Phase C） | 阶段 2（F7）；本阶段仅落 `permission_codes_json` 列与桥接表 | 阶段 2 |
| SSO / SCIM / MFA | 商业化安全项，独立立项 | 商业化 P0 清单 |
| 字段级权限逐字段化 | 只做成本类字段（`report.cost.view`），防角色爆炸 | 按需 |

## 4. 技术架构（锁定）

**选型结论**：自研轻量权限点 RBAC，**零新增第三方依赖**（ADR-001..005 定为契约）。
- 核心理由：gunicorn 多 worker 下 Casbin 默认内存 Enforcer 策略漂移（吊销失效）；`requirements.txt` 9 行极简纪律；需与 MCP path ACL 共用词汇表。
- 否决留痕：Flask-Principal（2013 后废弃）、Oso OSS（2023 弃用转托管）；Casbin 仅作未来逃生舱（schema 已留机械映射路径）。

**版本锚定**：

| 层 | 技术 | 版本 | 说明 |
|----|------|------|------|
| 运行时 | Python | 3.13.x（本地 3.13.12） | 不新增依赖 |
| Web | Flask | 3.1.1 | 现状 |
| WSGI | gunicorn | 23.0.0 | 127.0.0.1:8090 |
| 存储 | SQLite（标准库 sqlite3） | master.db + 各仓 db | 无 ORM |
| 权限引擎 | 自研（`permissions.py` + `permissions_manifest.py`） | — | 每请求解析 |

**七条硬约束**（违反即不合格）：
1. 禁新增第三方依赖；2. 只走 `init_master_db()` 幂等迁移 + 同步 `tests/test_master_db_migrate.py`；3. 权限解析 fail-closed（未知角色→403，绝不放行）；4. 权限集每请求解析，禁进程级缓存；5. `warehouse_users.role` 保持 code 字符串，不改主键不加 FK；6. `is_admin=1` 平台超管语义不变；7. 架构子系统改动同步 `docs/ARCHITECTURE.md`（pre-commit hook）。

## 5. 权限点目录与端点映射（锁定）

- **权限点目录**：25 个点全量见架构文档 §3.3（`order.*` 5 / `stock.*` 6 / `item.*` 6 / `report.*` 3 / `admin.*` 5），以 `PERMISSION_CATALOG` 常量落库。
- **端点映射**：`ROUTE_MATRIX`（manifest）覆盖全部 **147** 条已注册路由（main 基线实测；PR #17 合并后按实际 `url_map` 复核）（阶段 1 无新增 HTTP 端点；现有端点守卫等价收敛 + 逐条定级）。
- 关键端点示例（全量以 manifest 为准）：

| 端点（示例） | 权限点 | 迁移方式 |
|--------------|--------|----------|
| `/outbound/submit` | `stock.outbound` | 补声明式守卫（显式化，Eric 拍板） |
| `/outbound/<id>/rollback` | `stock.outbound.rollback` | 等价迁移 |
| `/report/outbound`、`/report/inbound` | `report.view` + 成本列按 `report.cost.view` 过滤 | 新挂点 + 服务端脱敏 |
| `/export/consumption`、`/summary/export` | `report.export` | 新挂点 |
| `/store-ordering/orders/<id>/review` | `order.approve` | 等价迁移 |
| `users.py` 全部 | `admin.user.manage`（仅平台超管） | 保持 `@require_platform_admin`（等价） |
| `agent_tokens.py` 两处 | `admin.token.manage`（仅平台超管） | 内联守卫 → 装饰器 |

- **阶段 2 前置**：新增任何管理接口（权限矩阵/角色管理 API）必须先产出 `openapi.yaml`（OpenAPI 3.0）契约，再实现。

## 6. 数据库表清单（锁定）

| 表/列 | 内容 | 迁移通道 |
|-------|------|----------|
| `permissions` | 权限点词汇表（code PK / category / description / risk / created_at） | `MASTER_SCHEMA` + 幂等播种 |
| `roles` | 角色（id / code / tenant_id / name / is_builtin / rank / created_at），内置 5 个 | 同上 |
| `role_permissions` | 角色=权限点集合（role_id + permission_code PK） | 同上 |
| `permission_routes` | 权限点→HTTP 路由桥接（MCP 统一用） | 同上 |
| `approval_policies` | 审批分级扩展点（**空表，未接线**，Phase 3） | 同上 |
| `agent_tokens.permission_codes_json` | 可空列（NULL=legacy path ACL，存量 token 零变化） | PRAGMA 守卫列迁移 |

- DDL 以架构文档 §3.2 为准；播种由 `PERMISSION_CATALOG` + `ROLE_PACKAGES` 常量 `INSERT OR IGNORE` 生成。
- 播种约束：角色包变更必须走 `--reseed-builtin-roles`（先删内置 `role_permissions` 再重播，不动自定义角色）；启动断言"每包 >0 且 staff⊆manager⊆admin"（防空包全站 403 沉默失败）。
- 迁移回归：`tests/test_master_db_migrate.py` 新增"空库→4 表+种子行数正确"与"重复 init 无重复行"用例。

## 7. 页面清单（锁定）

**阶段 1（本次开发）**：

| 页面 | 路由 | 改动 | 对应功能 |
|------|------|------|----------|
| 用户管理 | `/users`（既有） | 绑定/创建抽屉的角色下拉扩展为 5 个内置包；角色筛选器同步；下拉项显示中文名（基础作业/管理审批/仓管理/只读审计/财务） | F4 / 附录 A |
| （无其他页面改动） | — | 模板导航 `can()` 收敛推迟阶段 2；报表页仅服务端脱敏逻辑，无布局改动 | — |

**阶段 2（预告，非本次开发）**：`/admin/permissions` 权限管理页（权限矩阵 Tab + 角色列表 Tab，只读）；users.html 绑定行"查看该角色权限"增强；token 创建页勾选化。

## 8. 设计 Token（锁定）

- **主色**：teal `#0f766e`（`--brand`，沿用现状；设计师已校正"靛蓝"简报误差，靛蓝仅角色标签辅助色）。
- **复用现有 token 全量**（style.css:1-11）；阶段 1 无新增 token（无新页面）。
- **新增语义 token**（权限矩阵三态/锁定/diff/域 chip，UIUX §3.2 清单）→ 锁定为阶段 2 设计细化时纳入 `design-tokens.json` + `DESIGN.md`，本期不实现。
- **图标**：锁定"内联 SVG 描边（Lucide 几何风格）"为全项目唯一图标来源（现状既有做法），零依赖、零 CDN、禁 emoji。
- **字体**：PingFang SC / Microsoft YaHei 显式字体栈 token 化；权限点代码用等宽栈。

## 9. 验收标准（EARS 格式 — 锁定）

| 编号 | 功能 | 验收标准 | 优先级 |
|------|------|----------|--------|
| AC-01 | F1 | 系统**应**存在权限矩阵单一事实来源（manifest）；任意已注册路由的守卫意图**应**可从矩阵反查 | P0 |
| AC-02 | F2 | 代码库中"平台管理员+角色守卫"叠加死装饰器**应**为 0 处；平台管理员守卫与审计日志**应**各只有 1 处实现 | P0 |
| AC-03 | F3 | **当**任一条"仅登录"写操作被访问时，系统**应**按定级后的声明式守卫判定放行/拒绝；定级表（含旧/新行为对照）**应**提交 Eric 复核 | P0 |
| AC-04 | F4 | **若**用户不具备 `report.cost.view`，含成本类字段的报表与导出中的该字段**应**被服务端脱敏（§11-15 边界清单；非前端隐藏） | P0 |
| AC-05 | F4 | **若**用户不具备 `report.export`，导出接口**应**拒绝（403），不可仅靠前端隐藏按钮 | P0 |
| AC-06 | F4 | **当** staff 访问报表页时，**应**可见非成本报表（维持现状，Eric 拍板）；**若**访问成本列**应**被脱敏 | P0 |
| AC-07 | F4 | **若**账号绑定"只读/审计"包，**应**可 `report.view`+`report.export`、**且**任何写操作被 403；绑定"财务"包时**应**额外可见成本类字段 | P0 |
| AC-08 | F5 | **当**访问出库/回退/盘点审批/调整/状态跃迁时，系统**应**要求对应权限点；不存在"仅登录"写操作 | P0 |
| AC-09 | F5 | **若**用户不具备 `stock.outbound.rollback`，回退**应**被拒绝；出库执行按 Eric 拍板显式开放给基础包（行为不变） | P0 |
| AC-10 | F9 | **若**新增路由未在 `ROUTE_MATRIX` 登记，回归测试**应**失败（防倒挂机制化） | P0 |
| AC-11 | F9 | deny/allow 矩阵**应**覆盖：`item.delete`（历史倒挂点）、`report.cost.view`、`admin.*` 对 staff 均 403 | P0 |
| AC-12 | 全局 | **若** `warehouse_users.role` 为未知 code，权限解析**应** fail-closed（403 + 日志），绝不放行 | P0 |
| AC-13 | 全局 | **当**角色包空或阶梯破坏（staff⊄manager⊄admin）时，启动**应**告警（防沉默全 403） | P0 |
| AC-14 | 全局 | **当** perm 解析异常时**应**返回空集（fail-closed）；权限集**应**每请求解析（多 worker 无漂移） | P0 |
| AC-15 | 兼容 | 存量 5 用户、8 仓的授权行为**应**零变化（除明确列出为"有意变化"的条目：`report.cost.view` 收紧至 manager+） | P0 |

## 10. 边界与约束

- **行为变化白名单**（除此外一律零变化）：① `report.cost.view` 默认收紧（staff/viewer 不可见成本列，manager+/finance 可见）——F4 的目的本身；② `stock.outbound` 显式化（行为不变）；③ **`stock.stocktake.approve` 收紧至 manager+**（终裁①，staff 失去盘点审批）；④ **`recipe_cost` 配方成本读接口收紧至 `report.cost.view`**（`api_cost` + 4 个 CSV 导出：`ic_recipes_export_csv` / `recipes_export_csv` / `ic_recipe_bom_export_csv` / `recipe_bom_export_csv`；staff/viewer 403，manager/finance/admin 放行）——Eric 终裁 2026-10-13（验证报告 R1），属 ① 的收口延伸。
- **已知口径例外**（登记，非缺口）：`/summary`、`/inventory` 页成本金额**不脱敏**（终裁④，见附录 B.3）。
- **OPEN 项**（附录 B）：`report.export` 是否从基础包收窄到管理岗——本期默认零变化，实施完成后第一轮复核提交 Eric。
- 性能：权限解析每请求一次 JOIN（<1ms 量级），不显著增加请求耗时。
- 安全：一切校验服务端执行；脱敏在数据层过滤后输出；敏感值不落审计日志。
- 并发：权限变更与在途请求并发时，以"请求发起点"快照为准，变更后新请求即时生效。
- 时区：权限相关时间戳沿用 UTC-naive 约定（`rate_limit_pure` 家族），不与进程本地时区混用。
- 测试红线：pre-existing 18 failed 基线**零新增**；`pytest` 带 `--basetemp=/tmp/pytest-dc`；mcp_server 测试容器内跑；ruff 零新增。

## 11. 内嵌已知坑（实施必读，源自 PRD §11 + 架构 §3.6）

| # | 坑 | 修法 |
|---|-----|------|
| 1 | 权限倒挂/越权历史事故（staff 删品项、revenue 越权） | 定级成体系 + F9 全量基线 |
| 2 | 死装饰器叠加误读（×5） | 全部清除并登记为配置错误 |
| 3 | 未知角色 code 曾可能 fallback | fail-closed + 启动断言 |
| 4 | `INSERT OR IGNORE` 不更新已有行 | 角色包变更走 `--reseed-builtin-roles` |
| 5 | 多 worker 启动并发播种 | 复用 `init_master_db()` 的 `fcntl.flock`，勿另起锁 |
| 6 | 模板 `is_rd/is_storefront` 只能在 context_processor 取 | 阶段 2 `can()` 走同一通道 |
| 7 | master.db schema 纪律 | 同步 `init_master_db()` + `test_master_db_migrate.py` |
| 8 | 租户闸三道不得破坏 | RBAC 是叠加层，不替换 |
| 9 | 生产时区坑（CST vs UTC） | 权限时间戳统一 UTC-naive |
| 10 | MCP 侧事实更细 | 统一以其为基准，Web 对齐不反向削弱 |

## 12. 端到端验证步骤（收尾即验收）

```bash
cd /Users/ericmr/Documents/GitHub/DailyCheck
# 1. 建库播种（幂等）
python -c "from db import init_master_db; init_master_db()"
sqlite3 db/master.db "SELECT code,is_builtin,rank FROM roles;"   # 期望 5 行内置
# 2. 路由×角色矩阵 + 防倒挂
pytest --basetemp=/tmp/pytest-dc tests/test_route_permission_matrix.py -v   # 全绿
# 3. 存量权限回归（历史事故不复现）
pytest --basetemp=/tmp/pytest-dc tests/test_permissions_batch1.py tests/test_tenant_isolation.py -v
# 4. master 迁移回归
pytest --basetemp=/tmp/pytest-dc tests/test_master_db_migrate.py -v
# 5. 零新增红线
pytest --basetemp=/tmp/pytest-dc 2>&1 | tail -3     # failed == 18（不新增）
ruff check <改动文件>                                # 零新增
# 6. 错误流：未知角色 code → 403 + 日志；空包 → 启动告警
```

## 13. 变更记录

| 日期 | 变更内容 | 原因 | 影响范围 |
|------|----------|------|----------|
| 2026-10-13 | v1.0 生成 | 三文档确认 + Eric 拍板回填 | 全量（阶段 1 锁定） |
| 2026-10-13 | v1.1 终裁回填 | Eric Phase 3 五项终裁（①-⑤）落库；附录 A 角色包修订（manager 16→17 / admin 19→18）；附录 B 拆分 LOCKED 裁定 + 口径例外登记 | 阶段 1 实施锁定 |
| 2026-10-13 | v1.2 R1 收口 | `recipe_cost` 配方成本读接口（`api_cost` + 4 CSV 导出）收紧至 `report.cost.view`（Eric 终裁，验证报告 R1）；§5 路由数 156→147 校正（R4；PR #17 合并后复核）；§10 白名单追加 ④ | 阶段 1 实施（配方成本读收紧） |

---

## 附录 A：5 个内置角色包（终裁后 — 锁定；调整须走变更流程）

> 2026-10-13 终裁修订：`stock.stocktake.approve` 由 admin 包**上移**至 manager 包（裁定①）；`item.delete` **移出所有角色包**、维持平台级（裁定②）。

| 包（code） | 中文名 | 权限数 | 初始权限集 | 说明 |
|------------|--------|--------|-----------|------|
| staff | 基础作业 | **10** | order.create / order.receive / stock.outbound / stock.outbound.rollback / stock.stocktake / stock.adjust / stock.restock / item.view / report.view / report.export | 显式化现状；**不含** report.cost.view、**不含** stock.stocktake.approve（裁定①） |
| manager | 管理审批 | **17** | staff ∪ order.approve / order.ship / item.edit / item.create / item.price.view / report.cost.view / **stock.stocktake.approve** | 成本可见性 + 盘点审批收紧至 manager+ |
| admin（仓级） | 仓管理 | **18** | manager ∪ item.price.edit | 本仓全权；`item.delete` 已移出（裁定②） |
| viewer | 只读/审计 | **3** | item.view / report.view / report.export | 无任何写点；cost.view 默认不含 |
| finance | 财务 | **4** | item.view / report.view / report.export / report.cost.view | 财务只读 |

- 平台超管（`users.is_admin=1`）：bypass 全部检查，**不进任何包**；`admin.*` 5 点 + `item.delete` + `order.cancel.any` 默认仅平台超管使用（对应端点维持 `@require_platform_admin` 等价行为）。
- **阶梯不变式仅约束** staff ⊆ manager ⊆ admin（10 ⊆ 17 ⊆ 18）；`viewer` / `finance` 为**旁支**（`rank=NULL`），不参与阶梯包含链。
- **空包告警覆盖全部 5 包**；权限数契约 = 10 / 17 / 18 / 3 / 4（`BUILTIN_PACKAGE_SIZES`）。
- `item.view` / `item.edit` 权限点保留在包内（词汇表成员），但 `/items` 列表与编辑**维持平台级**（裁定③：不放开；仅将叠加死装饰器收敛为单一平台级 guard，对外语义零变化）。

## 附录 B：开放决策登记（OPEN-DECISIONS）

### B.1 终裁锁定（Eric，2026-10-13；Phase 3 实施依据，LOCKED）

| # | 裁定项 | 结论 | 落地位置 |
|---|--------|------|----------|
| ① | `stocktake.approve` 级别 | **收紧到 manager+**：`stock.stocktake.approve` 纳入 manager 包；staff 不再可批 | 附录 A（manager=17）；`stocktake.approve` 路由挂点 |
| ② | `items.delete_item` | **维持平台级**：不进任何角色包，不向仓内 admin 开放 | 附录 A（admin 不含 item.delete）；路由维持 `@require_platform_admin` |
| ③ | `item.view`/`item.edit`（`/items` 列表与编辑） | **维持平台级**：不放开；仅收敛叠加死装饰器为单一平台级 guard，对外语义零变化 | 路由维持 `@require_platform_admin` |
| ④ | `/summary`、`/inventory` 成本金额脱敏 | **不纳入脱敏**（保持现状），登记为**已知口径例外** | §10；B.3 例外登记 |
| ⑤ | `production.submit` | **维持 LOGIN 现状** + OPEN 登记 | §1.3 残留项；B.2 |

> ① 是**有意的行为变化**（staff 失去盘点审批，manager/admin 保留），系 Eric 拍板授权，超出 §10 白名单①的加法范围，特此登记。

### B.2 OPEN-DECISIONS

| Date | Source | Open Item | Current Leaning | Blocked By | Resolves When | Status |
|------|--------|-----------|-----------------|------------|---------------|--------|
| 2026-10-13 | 架构 §3.7-E | `daily_revenue.amount` 是否单列权限点 | 不单列（并入 report.cost.view） | 无 | Eric 复核 | RESOLVED（不单列） |
| 2026-10-13 | 拍板回填 | `report.export` 是否从基础包收窄到管理岗 | 本期零变化（staff 含） | 定级表交付 | 阶段 1 完成后第一轮复核 | OPEN |
| 2026-10-13 | 架构 §8.3 | `/items` 列表放宽范围（item.view 归属） | **维持平台级** | — | 已终裁（裁定③） | RESOLVED |
| 2026-10-13 | PRD §12.5 | 审批阈值"=按≤处理"口径（阶段 2 用） | 采纳 ≤ 口径 | F6 立项 | 阶段 2 实施前 | OPEN |
| 2026-10-13 | 终裁⑤ | `production.submit` 仅登录写 | 维持 LOGIN | 无 | 后续补点或显式接受 | OPEN |
| 2026-10-13 | 架构 §1.3 #15 | `order.cancel.any` 无包归属 | 暂不并入（用 `require_any_permission("order.create","order.cancel.any")` 处置） | 待确认 | 复核 | OPEN |

### B.3 已知口径例外登记

| 例外项 | 口径 | 理由 | 影响 |
|--------|------|------|------|
| `/summary`、`/inventory` 页成本金额对 staff 可见 | 不脱敏 | 终裁④：保持现状，避免本期扩大改动面；两页为经营看板而非导出 | staff 可见库存/消耗金额；与 `report.cost.view`（报表/导出）口径**有意不一致** |
