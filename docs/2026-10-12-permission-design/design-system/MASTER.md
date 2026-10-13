# MASTER.md — DailyCheck 全局设计源

> 设计师：颜好看 ｜ 生成：2026-10-13 ｜ 版本：v1.0
> 检索规则：设计任意页面前先读本文件；再检查 `pages/<page>.md`，存在则以该文件字段覆盖本文件对应字段，其余用本文件。
> 源规范：`../DESIGN.md`（9 节完整文档）｜ `../design-tokens.json` / `../design-tokens.css`

---

## 全局基线（所有页面共享）

| 项 | 值 |
|----|-----|
| 寄存器 | Product（后台工具，"赢得熟悉感"） |
| 三轴 | Variance=2 / Motion=2 / Density=7 |
| 主色 | `--brand #0f766e`（teal，`style.css:6`，非靛蓝） |
| 字体 | `--font-ui` PingFang SC / Microsoft YaHei；`--font-mono` JetBrains Mono |
| 容器规格 | `--panel` + 1px `--border` + `--radius-lg(10px)` + `--shadow-xs` |
| 图标 | 内联 SVG 描边（Lucide 几何），16/20/24，禁 emoji |
| 间距 | 4px 网格：4/8/12/16/20/24/32/40/48 |
| 圆角 | sm 4 / md 6 / lg 10 / xl 16 / pill |
| 动效 | instant 80 / fast 150（收敛）/ base 220；ease-standard |
| 强调色 | 每屏 `--brand` 可见 ≤2 处 |
| 深色模式 | 不做（系统全浅色） |

## 全局组件约定

- 工具栏 → 表格 → 右侧抽屉（沿用 `users-admin.css` 模式）。
- 焦点环：`outline:2px solid var(--brand); outline-offset:1px`。
- 抽屉 360px；宽内容用 `--drawer-w-wide` 480px。
- 所有组件零裸 hex；零 CDN；零图标字体。

## 角色标签系统（全局，跨页面统一）

实心=可写 / 描边=只读：

| 角色 | 类名 | 样式 |
|------|------|------|
| 基础作业 staff | `.role-tag--staff` | 实心中性 `#f3f4f6`/`#374151` |
| 管理审批 manager | `.role-tag--manager` | 实心靛蓝 `#eef2ff`/`#3730a3` |
| 仓管理 admin | `.role-tag--admin` | 实心 teal `#ecfdf5`/`#0b4f4a` |
| 只读/审计 viewer | `.role-tag--viewer` | 描边虚线中性 `#6b7280` |
| 财务 finance | `.role-tag--finance` | 描边 teal `#0b4f4a`/边框 `#0f766e` |
| 平台管理员 | `.role-tag--super` | 实心琥珀 `#fef3c7`/`#92400e` |

## 页面清单

| 页面 | Override 文件 | 状态 |
|------|---------------|------|
| 用户管理 `/users` | `pages/users.md` | 阶段 1 改动（下拉 5 包 + tag） |
| 权限管理 `/admin/permissions` | `pages/permissions.md` | 阶段 2（矩阵 + 角色列表） |
| 账号管理抽屉 | 见 `pages/users.md` | 阶段 1 绑定增强（阶段 2 加"查看角色权限"） |

## 变更记录

| 日期 | 变更 | 原因 | 影响 |
|------|------|------|------|
| 2026-10-13 | v1.0 建立 | Phase 2 设计细化；Spec v1.0 锁定 5 包 25 点 | 全局 |
| 2026-10-13 | 主色裁定 teal（非靛蓝） | 简报与代码不符，以 `style.css:6` 为准 | 全局 token |
| 2026-10-13 | 角色标签扩为 6 类（+viewer/finance） | Eric 拍板只读/审计、财务包纳入本期 | 用户列表 / 矩阵 |
