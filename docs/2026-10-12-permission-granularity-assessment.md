# 权限精细化管理 — 现状盘点与建议方案

> 日期：2026-10-12
> 产出：MVP 开发专家团 · Phase 0（现状盘点 + 方向建议，待 Eric 确认）
> 基线：`main @ 75af9c6`（含用户管理 v2）；本盘点基于 156 条已注册路由的全量静态扫描 + 关键实现人工核验

---

## 1. 现状盘点（证据化）

### 1.1 权限模型五要素

| 要素 | 现状 | 位置 |
|------|------|------|
| 用户 | `users`：`is_admin`（平台级）、`is_active`、`tenant_id` | master.db |
| 绑定 | `warehouse_users(user_id, warehouse_id, role)` — 用户×仓 持**单一**角色 | master.db |
| 角色 | `staff(1) < manager(2) < admin(3)` 单调阶梯 | `config.py:63` `ROLE_RANK` |
| 仓类型 | `storefront / rd / distribution_center`，装饰器按类型放行 | `permissions.py:88-137` |
| 租户 | `g.tenant_id` + 三道租户闸（钩子/picker/select），`is_admin` 豁免 | `blueprints/auth.py:86-122` |

执行机制共 6 个装饰器：`require_login` / `require_role` / `require_platform_admin` / `require_warehouse_type` + 语义包装 `storefront_only` / `distribution_center_only`（`permissions.py`）。

### 1.2 执行层扫描结果（156 条路由）

| 分类 | 数量 | 说明 |
|------|------|------|
| 有角色/仓类型守卫 | 82 | 声明式装饰器 |
| **仅 `require_login`** | **70** | 其中 21 条是 POST 写操作（详见 1.3） |
| 公开（无装饰器） | 4 | login/logout/sw.js/manifest —— 合理 |

**已有设计合理之处**（应保留）：
- 租户隔离三道闸已就位，跨租户绑定视为脏数据拒绝（`auth.py:243-254`）
- MCP `agent_tokens` 具备**路径级读写 ACL + 仓库白名单 + 限速**（`mcp_server/service/auth.py`），事实上比 Web 侧更细
- 订货取消已实现"状态相关权限矩阵"（下单人 / DC manager / 平台管理员三分支，`store_ordering.py:715-757`）
- 盘点已具两段式雏形：staff 操作（`stocktake.py:350-477`）与 admin 审核（`stocktake.py:102-204`）分离

### 1.3 "仅登录"写操作清单（21 条，按风险分组）

**A. 守卫表达不统一（功能有保护，但用内联方式）**
- `agent_tokens.py:108/159` 创建/吊销 Token —— 内联 `_require_admin()`（平台管理员，与 `@require_platform_admin` 重复实现）
- `notifications.py:61` 标记已读 —— 内联归属校验（合理，无需角色）

**B. 保护级别不一致（同类操作，守卫不同）**
- `outbound.py:70` 出库执行（**无角色**）vs `outbound.py:126` 出库回退（`staff`）
- `restock.py:146` 状态跃迁入库（**无角色**）vs `restock.py:184` 删除（`staff`）
- `reports.py` 报表与导出：`/report/outbound`、`/report/inbound`、`/export/consumption`、`/summary/export` 均仅登录（**全量数据对所有成员可见，含导出**）vs `/api/revenue` 写（`manager`）

**C. 依赖内联业务判断（有意为之但不可见）**
- 订货：`cart/*` 7 条、`orders/deliver`、`orders/receive` —— 实际门槛 = "该仓成员"（因 `warehouse_select` 已强校验绑定），具体约束散在 handler 内联逻辑（`_order_viewable` 等）

> 结论：21 条中真正的"裸奔"为零（最严门槛=仓成员），但**保护意图不一致且不可见**——"谁该做什么"没有任何单一事实来源，只能读代码推断。

### 1.4 装饰器使用的三个反模式

1. **语义陷阱**：`@require_platform_admin` + `@require_role("manager")` 叠加（`items.py:44-45`、`canonical.py:206-207`、`forecast.py:171-172`、`procurement.py:334-335`）——实际效果=仅平台管理员，第二个装饰器是**死代码**，但读代码者会误判为"平台管理员或 manager"。
2. **双实现**：平台管理员守卫存在两套（`require_platform_admin` vs 各模块内联 `_require_admin()`）；审计日志存在两份重复实现（`users.py:830` 与 `agent_tokens.py:184`）。
3. **UI 散装**：模板导航显隐靠 20+ 处字面量判断（`base.html:23-143`），如 `g.role['role'] in ('manager', 'admin')` 硬编码——新增角色/调整权限需多处改模板。

### 1.5 治理现状

- **历史事故（已修复，有测试留存）**：`test_permissions_batch1.py`——品项删除权限倒挂（staff 可删）、营业额 API 越权（任意登录可写）。当时靠事后补丁 + 4 条测试收尾。
- **权限测试薄**：`test_permissions_batch1`（4 用例）+ `test_tenant_isolation`（8 用例），无"路由×角色"全量 deny/allow 基线。
- **权限文档**：`ARCHITECTURE.md:523-550` 记录装饰器语义；角色能力只有一句 UI 提示（`users.html:265`："仓库级 admin 等价于该仓全权；manager 可审批/删除单据；staff 仅录入"）。

---

## 2. 问题诊断

### P0 — 一致性缺陷（隐患，应尽快收敛）

| # | 问题 | 风险 |
|---|------|------|
| 1 | 守卫三套并存（装饰器/内联/隐含），同类操作级别不一 | 新增路由时"抄错邻居"→ 再次倒挂；审计无法回答"谁能做 X" |
| 2 | 死装饰器与双实现 | 误读权限；维护分叉 |
| 3 | 报表/导出全成员可见（含成本类数据视仓库而定） | 数据敏感度控制为零；无"只读/审计"视角 |
| 4 | 权限回归靠人工，无全量基线 | 上一次同类事故的复发风险 |

### P1 — 粒度不足（"更细致"的核心缺口）

当前模型是**单调阶梯**：升一级 = 获得该级全部能力，**做不了减法、做不了加法**。

- 无法表达"能审批订货但看不到成本价"
- 无法表达"财务只读全仓报表"、"审计只看盘点记录"
- 无法表达"新来的实习生只能录入，不能执行出库扣账"
- 无法表达"同一个 manager 在 A 仓能批、在 B 仓不能批"（多仓绑定有，但审批范围只能整仓开关）
- 审批一刀切：订货审批=DC manager（`store_ordering.py:623-626`），无金额/数量/跨仓的条件分级
- 无"区域视角"：多店管理的用户只能一仓一仓绑，无跨仓汇总角色

### P2 — 管理侧不足（界面与治理）

- 权限管理界面只有"用户×仓×角色"绑定（`users.html` v2），**无法回答"这个角色到底能干什么"**（无权限矩阵视图）
- 无法自定义角色；角色名是代码里的枚举
- 权限变更审计散在 `admin_audit.log`（扁平 JSONL），业务审计在每仓 `audit_log`，无统一查询视图
- 多租户方向下，"租户管理员能否自管本租户权限"未做设计决策

---

## 3. 建议方向（三选一或组合，推荐 A+B 渐进）

### 方向 A — 保守增强（止血，1 个迭代）

不动数据模型，只统一纪律：
1. 清除叠用死装饰器；内联守卫全部收敛回 `permissions.py`
2. 21 条"仅登录"写操作**逐条定级**并补声明式守卫（有据可查的定级，不是拍脑袋）
3. 定义"权限矩阵"单一事实来源（`docs/` + `permissions.py` 常量），UI 与守卫都引用它
4. 补"路由×角色"全量 deny/allow 回归基线（继承既有测试风格）

天花板：角色还是 3 级，需求（只读/财务/审计）依然表达不了。

### 方向 B — 操作级权限点 RBAC（推荐主目标，2-3 个迭代）★

把"角色 = 等级"升级为"**角色 = 权限点集合**"：

```text
权限点示例（草案，待 Phase 1 细化）
order.create / order.approve / order.ship / order.cancel.any
stock.outbound / stock.outbound.rollback / stock.stocktake.approve / stock.adjust
item.view / item.edit / item.price.view / item.price.edit
report.view / report.export / report.cost.view
admin.user.manage / admin.token.manage / admin.warehouse.manage
```

- 现有 3 角色 → 映射为 3 个**内置角色包**（默认权限集合），绑定关系不变 → **存量零迁移成本**
- 新增 `@require_permission("...")`，逐步替换 `require_role`（保留兼容层，不搞大爆炸）
- 权限点支持**做加法**（给 staff 加"看报表"）与**做减法**（manager 去掉"审批"）
- 管理 UI 增加"角色权限矩阵"视图；自定义角色（DB 驱动）为可选二期
- MCP `agent_tokens` 的 path ACL 未来引用同一份权限词汇表 → Web 与 Agent 权限体系统一

### 方向 C — 条件权限（ABAC 轻量，后续按需）

- 数据范围（self / warehouse / multi-warehouse / tenant）
- 审批阈值分级（金额/数量 → 升级审批人）
- 双人复核（如盘点：提交人 ≠ 审批人强制）
- **建议**：本期只预留 schema/接口扩展点，不实现

### 推荐组合

**A 立即做**（本身就是 B 的地基：权限词汇表 + 矩阵 + 回归基线），**B 作为主目标推进**，C 留扩展点。理由：A 单独做只是止痛；B 才能回答"更细致"；A+B 的渐进路径每一步都可发布、可回滚，符合既有工程纪律（pre-existing 失败集零新增红线可保持）。

---

## 4. 初步实施计划（方向确认后由专家团细化）

### 阶段 1：纪律统一 + 显式矩阵（1 个迭代）

| 交付物 | 验收 |
|--------|------|
| ① 权限矩阵文档 + `permissions.py` 常量（单一事实来源） | 每条路由的守卫意图可从矩阵反查 |
| ② 清理：死装饰器 ×4、内联守卫收敛、审计日志去重 | ruff 零新增；相关测试全绿 |
| ③ 21 条写操作逐条定级并落守卫 | 定级表评审通过；每条的旧/新行为对照 |
| ④ 路由×角色回归基线（deny/allow 全量） | 新增路由未登记 → 测试拒绝（防倒挂机制化） |

### 阶段 2：操作级 RBAC（2-3 个迭代）

| 交付物 | 验收 |
|--------|------|
| ① 权限点词汇表 + 种子数据（3 内置角色映射） | 现有 5 用户授权行为**零变化**（回归验证） |
| ② `@require_permission` 装饰器 + 迁移（含兼容层） | 迁移清单逐条对照 |
| ③ 管理 UI：权限矩阵视图（角色 × 权限点） | 平台管理员可查看任意角色能力 |
| ④ （可选）自定义角色 | 新建角色 → 绑定用户 → 生效闭环 |

### 阶段 3：条件权限扩展点（按需）

数据范围字段设计入 schema；审批阈值/双人复核单独立项评估。

---

## 5. 待决策问题（Phase 0 → Phase 1 门禁）

1. **模型方向**：A（保守统一）/ B（操作级权限点，推荐）/ A+B 渐进 / C
2. **优先痛点**：报表与成本可见性、敏感操作收紧、审批分级、新职业角色、MCP 权限——先解决哪些？
3. **管理权分布**：维持平台管理员中心化，还是引入"租户/仓级管理员"分层管理（对多租户商业化有影响）？

> Eric 确认方向后，专家团将进入 Phase 1（并行调研：权限模型对标 + 迁移方案 + 管理 UI 方向），产出 PRD/架构/UIUX 三文档供确认。
