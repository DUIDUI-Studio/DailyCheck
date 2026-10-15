# pages/permissions.md — 权限管理页 Override

> 页面：`/admin/permissions`（阶段 2）｜ 覆盖 `../MASTER.md` 对应字段，未列出的字段沿用 Master
> 上游：Spec §5（25 点目录）+ §7（页面清单）+ 架构 §3.3/§3.4

---

## 与 Master 的差异（仅列差异字段）

| 字段 | Master 值 | 本页 Override | 原因 |
|------|-----------|---------------|------|
| 布局 | 工具栏→表格→抽屉 | **双 Tab（权限矩阵 / 角色列表）** | 两个强相关视图，Tab 避免导航膨胀 |
| VISUAL_DENSITY | 7 | **8（矩阵 Tab）** | 25 行 × 6 列，需最高密度；矩阵内禁用一切卡片容器 |
| 抽屉宽 | 360px | **480px（`--drawer-w-wide`）** | 角色权限清单按域分组，360 太窄 |
| 表格模型 | 普通表格 | **矩阵表格**（行=权限点，列=角色） | 见下"轴方向决策" |
| 主色使用 | 每屏 ≤2 处 | 矩阵"已授予"格计 **1 处语义强调**（重复实例），主 CTA 第 2 处 | 避免误判超配额 |

## 页面专属结构 token（本页新增）

```css
--matrix-cell-size: 28px;        /* ≥1024 */
--matrix-row-h: 36px;
--matrix-sticky-col-w: 160px;
/* 768-1024：--matrix-cell-size: 24px */
```

## 轴方向决策（关键）

**行 = 权限点，列 = 角色。** 理由：角色仅 5 个（列少），权限点 25 个（行多）。角色作列 → 横向滚动最小化，纵向滚动更自然（ux.stackexchange 共识：切换轴使列 = 少维度）。

## 列定义（6 列）

| 序 | 列 | 内容 | 状态 |
|----|-----|------|------|
| 1 | 权限点（冻结列，160px） | 中文名 + dot-notation 等宽副标 | `position:sticky; left:0` |
| 2 | 基础作业 staff | 三态格 | 只读 |
| 3 | 管理审批 manager | 三态格 | 只读 |
| 4 | 仓管理 admin | 三态格 | 只读 |
| 5 | 只读/审计 viewer | 三态格 | 只读 |
| 6 | 财务 finance | 三态格 | 只读 |
| 7 | 平台管理员 | **全授予 + 锁定**（`--perm-cell-locked-*`，tooltip「系统全权·不可配置」） | 锁定 |

> 平台管理员单列展示但锁定：既回答"超管能做什么"（附录 A：bypass 全部、不进任何包），又表明不可配置。

## 行分组（5 域，默认折叠策略）

| 域 | 图标 | 权限点数 | 默认 | code |
|----|------|----------|------|------|
| 订货 | box | 5 | 展开 | order.create / order.approve / order.ship / order.receive / order.cancel.any |
| 库存 | layers | 6 | 展开 | stock.outbound / stock.outbound.rollback / stock.stocktake / stock.stocktake.approve / stock.adjust / stock.restock |
| 品项 | tag | 6 | 折叠 | item.view / item.edit / item.create / item.delete / item.price.view / item.price.edit |
| 报表 | bar-chart | 3 | 展开 | report.view / report.export / report.cost.view |
| 管理 | shield | 5 | 折叠 | admin.user.manage / admin.token.manage / admin.warehouse.manage / admin.canonical.manage / admin.role.manage |

> 域图标与 Master 的图标方案一致（内联 SVG 描边，禁止 emoji）。域 chip 用中性色（`--domain-chip-*`），区分度交给图标。

## 角色包 → 矩阵预期值（数据契约，供前端与测试核对）

| 角色 | 授予的权限点（预期 ●） | 计数 |
|------|----------------------|------|
| staff 基础作业 | order.create, order.receive, stock.outbound, stock.outbound.rollback, stock.stocktake, stock.adjust, stock.restock, item.view, report.view, report.export | 10 |
| manager 管理审批 | staff ∪ order.approve, order.ship, item.edit, item.create, item.price.view, report.cost.view, stock.stocktake.approve | 17 |
| admin 仓管理 | manager ∪ item.price.edit | 18 |
| viewer 只读/审计 | item.view, report.view, report.export | 3 |
| finance 财务 | item.view, report.view, report.export, report.cost.view | 4 |
| 平台管理员 | 全部 25（bypass，锁定列） | 25 |

> 数据源：`permissions_manifest.ROLE_PACKAGES`（单一事实来源）。UI 不硬编码，从 manifest 渲染。
> `admin.*` 5 点 **+ `item.delete` + `order.cancel.any`** 共 7 点为平台级（`PLATFORM_ONLY_PERMS`），5 个仓库角色均无（仅平台管理员列亮）；这是"管理权中心化"的直观表达。
> `item.view` 归属为 OPEN 项（Spec 附录 B）：本表按 manifest 现值展示，实施时以逐条定级表复核结论为准。

## 页面状态

| 态 | 表现 |
|----|------|
| Loading | 域分组骨架占位框（不闪白） |
| Empty | 「暂无权限点定义」+ 指向 manifest 配置说明 |
| Error | 「权限数据加载失败」+ 重试按钮 |
| Populated | 5 域 + 三态格 + 底部计数 |
| Edge | 权限点 >60 → 默认全闭；域 >8 → 纵向滚动；单域 >15 → 域内二级滚动 |

## 响应式 Override

- <768px：矩阵**退化为逐角色手风琴**（每角色一卡，卡内按域列「权限名 + [已授予]」），**不横向滚动**。
