# 页面设计提示词：用户绑定角色交互增强

> 页面：`/users`（账号管理）抽屉内 ｜ 阶段 2 ｜ 设计师：颜好看
> 依赖：`users-admin.css`（既有）· `design-tokens.css` · 阶段 1 先落地下拉/tag（见 `00-phase1-minimal-spec.md`）

## 目标

闭合现状痛点：管理员绑定角色时**不知道角色能干什么**（现状仅 `users.html:265` 一句概述）。在既有绑定行加一个"查看权限"入口，零结构侵入。

## 现有结构（不改）

`users.html` 抽屉 → `.users-bindings`（`users-admin.css:348-365`）：
```
.role-row: [.role-row-label 仓名] [角色 select] [...解绑...]
```

## 改动（仅新增一处）

每个 `.role-row` 的角色下拉后，追加一个文字按钮：

```
[wh_001 新天地店]  [ 基础作业 ▾ ]  [查看该角色权限]
[wh_002 中心仓  ]  [ 管理审批 ▾ ]  [查看该角色权限]
```

- 类名：`.role-row-perms`（新增）。
- 样式：文字按钮（ghost），14px，`color:var(--brand)`；hover 下划线 + `--accent-soft` 底；
  `focus-visible` 用 `outline:2px solid var(--brand); outline-offset:1px`。
- 触摸目标 ≥44px（移动端；桌面可 32px）。

## 交互

| 操作 | 反馈 |
|------|------|
| 点击「查看该角色权限」 | 打开**只读权限清单浮层**（复用角色列表抽屉内容与视觉，480px 桌面 / 底部 ActionSheet 移动） |
| 浮层内 | 标题=角色中文名 + `.role-tag--<code>`；按域分组列已授予权限；底部小计；无勾选框 |
| 关闭 | Esc / 点遮罩 / 关闭按钮，焦点回触发按钮 |
| 切换角色下拉 | 若浮层开着且角色变更 → 浮层内容同步刷新（或关闭后重开，二选一由前端定） |

## 约束

1. **不改变既有 DOM 结构与 `ajax-form.js` 的 `reload-stay` 逻辑**——只在 `.role-row` 内追加一个按钮 + 一个浮层容器。
2. 浮层数据源 = manifest（`ROLE_PACKAGES[role]`），**前端不硬编码**。
3. 角色显示用中文名 + tag（见 MASTER 角色标签系统），下拉值仍为 code（`staff/manager/admin/viewer/finance`）。
4. 只读：浮层无任何可编辑控件。

## 响应式

- ≥768px：右侧 480px 抽屉浮层（与角色列表一致）。
- <768px：底部 ActionSheet。

## 5 态

| 态 | 表现 |
|----|------|
| Loading | 浮层骨架 |
| Empty | 该角色无权限（理论不空） |
| Error | 「权限清单加载失败」+ 重试 |
| Populated | 域分组已授予清单 |
| Edge | 仓名超长 → `.role-row-label` ellipsis + title；权限清单超长 → 抽屉内独立滚动 |
