# 阶段 1 最小规格（唯一 UI 改动）

> 页面：`/users`（账号管理）｜ 阶段 1（本次开发）｜ 设计师：颜好看
> 依据：Spec §7「阶段 1（本次开发）」——**唯一 UI 改动**：绑定/创建抽屉下拉扩为 5 包 + 列表 tag 显示
> 其余（矩阵/角色列表/绑定增强）全部为阶段 2，不在本次范围

---

## 改动范围（1 页，最小侵入）

### 改 1：角色下拉从 3 项 → 5 项（中文名）

涉及三处 `users.html` 的角色选择控件，选项统一替换：

| 位置 | 现状 | 改为 |
|------|------|------|
| 工具栏角色筛选器（`users.html:39-44`） | staff / manager / admin | 5 项（含 viewer / finance） |
| 创建抽屉角色下拉（JS 渲染） | 3 项 | 5 项 |
| 绑定行角色下拉（JS 渲染） | 3 项 | 5 项 |

**选项文案（中文名，Spec 附录 A 为准）：**

```html
<option value="">所有角色</option>              <!-- 仅筛选器有"所有" -->
<option value="staff">基础作业</option>
<option value="manager">管理审批</option>
<option value="admin">仓管理</option>
<option value="viewer">只读/审计</option>
<option value="finance">财务</option>
```

> 值仍为 code 字符串（`warehouse_users.role` 保持 code 语义，架构 §4 硬约束 5）。中文名仅作显示。

### 改 2：列表行角色 tag 显示 5 类（含平台管理员）

`users.html:104-116` 的 `.users-roles` tag 渲染，按角色 code 输出对应 `.role-tag--<code>`：

```
{% for wid, info in bindings.items() %}
  <span class="role-tag role-tag--{{ info['role'] }}" title="{{ info['warehouse_code'] }}">
    {{ info['warehouse_code'] }}·{{ role_display[info['role']] }}
  </span>
{% endfor %}
```

- `role_display` 映射：staff→基础作业 / manager→管理审批 / admin→仓管理 / viewer→只读审计 / finance→财务。
- 平台管理员 tag（`is_admin`）改类名为 `.role-tag--super`（沿用既有琥珀色，仅类名 token 化）。
- 未知 code → 中性降级 tag（防未来自定义角色裸奔）：默认 `.role-tag--staff` 样式 + 显示原 code。

---

## 角色标签样式（定稿，阶段 1 即用）

`design-tokens.css` 已含完整定义，阶段 1 需引入该文件（或将该段 CSS 并入 `users-admin.css`）：

| 角色 | 类名 | 视觉 |
|------|------|------|
| 基础作业 | `.role-tag--staff` | 实心·中性 `#f3f4f6`/`#374151` |
| 管理审批 | `.role-tag--manager` | 实心·靛蓝 `#eef2ff`/`#3730a3` |
| 仓管理 | `.role-tag--admin` | 实心·teal `#ecfdf5`/`#0b4f4a` |
| 只读/审计 | `.role-tag--viewer` | **描边·虚线**·中性 `#6b7280`，边框 `#d1d5db` |
| 财务 | `.role-tag--finance` | **描边·teal** `#0b4f4a`，边框 `#0f766e` |
| 平台管理员 | `.role-tag--super` | 实心·琥珀 `#fef3c7`/`#92400e` |

**语义规则**：实心 = 可写；描边 = 只读。**不靠颜色单独表意**（描边/虚线与实心构成形态冗余）。

---

## 验收（对应 Spec AC）

| AC | 断言 |
|----|------|
| AC-15 | 存量 5 用户 8 仓授权行为零变化（下拉新增 viewer/finance 不影响既有绑定） |
| AC-07 | 绑定 viewer 包 → 可 report.view+export、写操作 403；绑定 finance → 额外可见成本 |

## 不做（阶段 1 明确排除）

- 权限矩阵页 `/admin/permissions`（阶段 2）
- 角色列表页（阶段 2）
- 绑定行「查看该角色权限」入口（阶段 2）
- 模板导航 `can()` 收敛（阶段 2）
- diff 预演编辑态（阶段 2+）

## P0 自检

- 无 emoji（图标为内联 SVG 或无图标）— 通过
- 无紫→粉渐变（角色 tag 全为中性/teal/靛蓝/琥珀，无渐变）— 通过
- 无裸 hex（全部经 `design-tokens.css` 的 `--role-tag-*` token 引用）— 通过
- 零新依赖（纯 CSS，沿用 `users-admin.css`）— 通过
