# 页面设计提示词：权限矩阵视图

> 页面：`/admin/permissions`（Tab 1 = 权限矩阵）｜ 阶段 2 ｜ 设计师：颜好看
> 依赖：`design-tokens.css` · `design-system/pages/permissions.md` · manifest（25 点 / 5 包）

## 路由与可见性

- 路由：`/admin/permissions`（`?tab=matrix` 默认）。
- 可见性：仅 `g.user['is_admin']` 渲染导航入口与页面；非管理员访问 → 403 页（复刻"Access Denied"友好页）。
- 阶段 1 **不实现**（`/admin/permissions` 为 Phase 2；阶段 1 仅改 users.html，见 `00-phase1-minimal-spec.md`）。

## 布局

```
[Tab 条]  权限矩阵 | 角色列表
[工具栏]  搜索权限点 · 域筛选 · 视图切换(矩阵|分组) · 导出 CSV
[图例]    已授予 / 继承 / 未授予 / 系统锁定 · 命名格式说明
[矩阵]    冻结列(权限点) + 5 角色列 + 平台管理员锁定列，按 5 域折叠
[底栏]    已授予 N / 共 25 个权限点 · 5 个域
```

- 容器：`--panel` + 1px `--border` + `--radius-lg` + `--shadow-xs`（工具栏、矩阵容器各一）。
- 矩阵表格：`border-collapse:separate; border-spacing:0`；**保留 1px 水平分隔线**（`--border-soft`），不使用行卡片。
- 冻结列：`position:sticky; left:0; z-index:110; background:var(--panel)`，右侧 `border-right:1px solid var(--border)`。

## 核心组件

| 组件 | 规格 | 状态 |
|------|------|------|
| Tab 条 | 文案 14px / 510；选中下划线 `--brand` 2px；未选 `--muted` | default / hover(`--fg`) / focus-visible / selected |
| 搜索框 | 复刻 `.users-search`（16px search 图标 + 输入框） | default/focus/disabled |
| 域筛选下拉 | 复刻 `.users-toolbar select` | — |
| 图例 | 4 个符号 + 文案，12px `--muted` | — |
| 域分组头 | 图标(16px) + 中文域名 + 「N 项」计数 + chevron；`--domain-chip-bg` 行底 | default / hover / collapsed |
| 权限点行（冻结列） | 中文名 14px/400 + dot-notation 等宽 12px `--muted` | — |
| 矩阵单元格 | 真实 `<button>`/`<input type=checkbox>`，28×28 内容居中 | 见下"三态" |

### 单元格三态（视觉 + 无障碍）

| 态 | 视觉 | 元素属性 |
|----|------|----------|
| 已授予 | 实心 `--perm-cell-on` + 勾图标；底色 `--perm-cell-on-bg` | `aria-pressed="true"`（只读则 `disabled`） |
| 继承 | **虚线** 1px `--perm-cell-inherited`，无填充，不可点；tooltip「继承自 <角色>」 | `disabled` + `aria-label` |
| 未授予 | 空描边 1px `--perm-cell-off` | `aria-pressed="false"` + `disabled` |
| 系统锁定 | `--perm-cell-locked-bg` 底 + 锁图标 `--perm-cell-locked-fg` | `disabled` + `aria-label="系统全权，不可配置"` |

> 本期**全部只读**：所有单元格 `disabled`，`cursor:default`；悬停仅 tooltip 释义，不出现可点暗示。

## 交互

| 用户操作 | 视觉反馈 |
|----------|----------|
| 悬停单元格 | 显示 tooltip：权限中文名 + dot-notation + 状态说明；单元格 `outline` 极淡高亮（不改变三态语义） |
| 点击域分组头 | 组内行展开/收起，chevron 旋转 150ms；折叠态保留组计数（"库存 · 6 项，已授予 4"） |
| 搜索权限点 | 实时（≥1 字）过滤行，命中域自动展开，无命中显示"未找到「xxx」" |
| 域筛选 | 仅显示所选域，其余隐藏 |
| 视图切换 矩阵/分组 | "分组"= 按域纵向堆叠的清单视图（窄屏友好降级） |
| 导出 CSV | `report.export` 校验由服务端承担；前端仅触发 |
| 键盘 | Tab 进矩阵 → 方向键移动单元格（roving tabindex）→ Esc 退出 |

## 响应式

| 断点 | 行为 |
|------|------|
| ≥1024px | 完整矩阵（格 28px / 行高 36px / 冻结列 160px） |
| 768-1024px | 格 24px，域默认全闭 |
| <768px | **逐角色手风琴**：每角色一张卡，卡内按域列「权限名 [已授予]」，行 ≥44px，无横向滚动 |

## 实现要点（前端必读）

1. 数据从 `permissions_manifest`（`PERMISSION_CATALOG` + `ROLE_PACKAGES`）渲染，**UI 不硬编码权限点**。
2. 单元格用真实表单元素，非 `div`；`disabled` 而非"看起来禁用"。
3. 冻结列 `sticky` 而非 `absolute`（防横滚错位坑）。
4. 图例常驻（降低首次认知负荷）。
5. 底部计数与矩阵同源计算，避免不一致。
