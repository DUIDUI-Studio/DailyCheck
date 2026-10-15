# DailyCheck 权限精细化管理 DESIGN.md

> 生成日期：2026-10-13 ｜ 设计师：颜好看 ｜ 基于：Spec v1.0（`docs/2026-10-12-permission-spec.md`）+ 架构 §3.3/§3.4 + UIUX v0.1
> 三轴刻度：**Variance=2 / Motion=2 / Density=7**
> 寄存器：**Product**（后台工具，设计服务产品，"赢得熟悉感"）
> 关联文件：`design-tokens.json` · `design-tokens.css` · `design-system/MASTER.md` · `design-system/pages/permissions.md`

---

## 1. Visual Theme & Atmosphere（视觉主题与氛围）

- **视觉主题关键词**：冷静、精确、克制、可审计、可预测。
- **氛围描述**：浅色中性底 + 1px 细边框承载信息，几乎不用阴影；大量数据靠对齐与留白组织，而非卡片盒子。管理员应感到"这台机器很稳"——每一次勾选都有据可查，没有多余的视觉噪音。类似 Linear 设置页的克制 + Stripe 权限表的密度。
- **对标品牌**：
  1. **Linear — Settings / Members & Roles**（主）：克制中性灰、1px 边框、紧凑行高、键盘优先。
  2. **Stripe Dashboard — Team & Permissions**：权限矩阵信息密度与分组表头标杆。
  3. **Cloudflare — Members / Roles**：内置角色"锁定只读"的表达方式（禁用态 + tooltip 说明）。
- **严格延续现有系统**：不另起炉灶。沿用 `users-admin.css` 已确立的容器规格（白底 + 1px 边框 + 10px 圆角 + `0 1px 2px rgba(0,0,0,.04)`）。

---

## 2. Color Palette & Roles（色彩与角色）

### A1-identity（品牌核心，沿用 `style.css:1-11`，零改动）

| Token | 值 | 角色 |
|-------|-----|------|
| `--bg` | `#eef2f6` | 页面底色 |
| `--panel` / `--surface` | `#ffffff` | 面板/卡片 |
| `--text` / `--fg` | `#1f2d3d` | 主文本 |
| `--muted` | `#6b7280` | 次要文本 |
| `--brand` / `--accent` | **`#0f766e`**（teal） | 品牌主色 |
| `--brand-ink` | `#0b4f4a` | accent 深色 |
| `--border` | `#d9e1ea` | 默认边框 |

> **主色裁定**：沿用 teal `#0f766e`。简报"靛蓝系"与代码不符（`style.css:6`），已校正。靛蓝降级为**角色标签辅助色**，不作主色——顺带规避"Tailwind 默认靛蓝 = 一眼 AI"反模式。

### A2-semantic（语义色）

| Token | 值 | 用途 |
|-------|-----|------|
| `--warn` | `#c2410c` | 警示（沿用） |
| `--danger` | `#dc2626` | 危险/删除/移除标记 |
| `--info` | `#2563eb` | 信息/本次新增（diff） |

### 新增：权限矩阵三态 + 锁定（核心）

| Token | 值 | 视觉 |
|-------|-----|------|
| `--perm-cell-on` | `var(--brand)` | 实心 + 勾，已授予（源头，可点） |
| `--perm-cell-on-bg` | `#ecfdf5` | 已授予底色 |
| `--perm-cell-inherited` | `#6b7280` | **虚线边框**，继承自上级，**不可点** |
| `--perm-cell-off` | `#d1d5db` | 空描边，未授予 |
| `--perm-cell-locked-bg` / `-fg` | `#f3f4f6` / `#9ca3af` | 灰底 + 锁图标，系统锁定 |

> 学术依据：把「继承」折叠进「授予」是权限矩阵可用性第一大坑——用户取消上级勾选却看到它仍亮着，会判定 UI 坏了。故三态必须视觉可辨（实心/虚线/空）。本项目 5 个内置包**非严格继承阶梯**（viewer/finance 为旁支），三态中的"继承"在既有数据下少见；token 与视觉规范**先行定义**，为 Phase 2 自定义角色继承预留，避免届时改版。

### 新增：变更预演（Phase 2 编辑态预留，本期只读不实现）

| Token | 值 | 语义 |
|-------|-----|------|
| `--diff-added` | `#2563eb` | 本次新增（蓝） |
| `--diff-removed` | `#dc2626` | 本次移除（红斜线） |
| `--diff-unchanged` | `var(--brand)` | 已有不变（teal） |

### 新增：域分组 chip（中性化）

`--domain-chip-bg #f3f4f6` / `-fg #374151` / `-on-bg #ecfdf5` / `-on-fg #0b4f4a`。
区分度**由域图标承担，不用 5 种彩色**——避免彩虹矩阵（红噪声）。

### 新增：角色标签语义系统（B-slot，定稿）

**核心语义规则：实心 = 可写；描边 = 只读。** 色相仅 3 族 + 琥珀保留给平台管理员，不靠颜色单独表意。

| 角色（code） | 中文名 | tag 样式 | bg / fg |
|--------------|--------|----------|---------|
| staff | 基础作业 | 实心·中性 | `#f3f4f6` / `#374151` |
| manager | 管理审批 | 实心·靛蓝 | `#eef2ff` / `#3730a3` |
| admin（仓级） | 仓管理 | 实心·teal | `#ecfdf5` / `#0b4f4a` |
| viewer | 只读/审计 | **描边·虚线·中性** | transparent / `#6b7280`，边框 `#d1d5db` |
| finance | 财务 | **描边·teal** | transparent / `#0b4f4a`，边框 `#0f766e` |
| （平台管理员 `is_admin=1`） | 平台管理员 | 实心·琥珀（系统保留） | `#fef3c7` / `#92400e` |

- 与既有代码的连续性：`manager` 靛蓝 = `users-admin.css:113` `.tag` 默认色；平台管理员琥珀 = `users-admin.css:117` `.tag.admin`，**均为既有色，不新增视觉债务**。
- `viewer` 用虚线描边 = 强语义"无写权限"；`finance` 用 teal 描边 = 只读但可见成本（与 admin 同族、靠实/描区分）。

### 每屏强调色用量纪律

每屏可见 `--brand` 强调 **≤2 处**。矩阵里"已授予"格虽多，属**同一功能的重复实例**（计 1 处语义强调）；主 CTA 为第 2 处。域 chip、角色 tag 均属中性/语义类，不计入 accent 配额。

### 深色模式

**本期不做**（现有系统全浅色，`users-admin.css:6` 明确"全部用浅色主题"）。token 命名已预留，不实现。

---

## 3. Typography（排版）

### 字体栈（显式声明，非"系统字体直出"）

```css
--font-ui:   "PingFang SC", "Microsoft YaHei", "Noto Sans SC", system-ui, sans-serif;
--font-mono: "JetBrains Mono", "Fira Code", ui-monospace, monospace;
```

> 中文后台**不引入 Web Font**（加载阻塞 + 中文字集巨大），以显式字体栈 token 化满足"明确品牌字体组合"要求。

### 字号阶梯

| Token | px | 用途 | 行高 |
|-------|-----|------|------|
| `--text-xs` | 12 | 标签/徽章/辅助 | 1.5 |
| `--text-sm` | 13 | 紧凑说明/抽屉 label | 1.5 |
| `--text-base` | 14 | 正文/表格 | 1.5 |
| `--text-md` | 16 | 抽屉标题/小标题 | 1.25 |
| `--text-lg` | 18 | 区块标题 | 1.25 |
| `--text-xl` | 20 | 页面标题 | 1.25 |

### 字重三级

400 正文（Read） / 510 表头与按钮（Emphasize） / 590 标题（Announce）。

### 字距

- 正文 `0`；ALL CAPS 表头（如 `PERMISSION`）`0.06em`；中文表头**不加字距**；大标题 `-0.02em`。
- 权限点 dot-notation 代码（`order.approve`）：`--font-mono` 12px，`letter-spacing:0`。

---

## 4. Components（组件规范）

### 按钮（沿用既有 `.btn`）

| 变体 | 默认 | hover | active | disabled |
|------|------|-------|--------|----------|
| primary | `--brand` / 白字 | `--brand-ink` | 更深（color-mix 12% 黑） | opacity .5, cursor not-allowed |
| secondary | 透明 + 1px `--brand` 边 + `--brand` 字 | `--accent-soft` 底 | — | opacity .5 |
| ghost | 透明 / `--fg-2` 字 | `--border-soft` 底 | — | opacity .5 |
| destructive | `--danger` / 白字 | 深红 | — | opacity .5 |

### 输入框 / 下拉（沿用 `.users-toolbar` 规格）

default：1px `#d1d5db` 边 + `--radius-md`；focus：`outline:2px solid var(--brand); outline-offset:1px`；error：1px `--danger` + `--danger` 辅文；disabled：`#f3f4f6` 底 + `--meta` 字。

### 角色标签（`.role-tag`）

见 §2 定稿表。6 个修饰类：`.role-tag--staff/--manager/--admin/--viewer/--finance/--super`。`border-radius: var(--radius-pill)`，`padding: 2px 8px`，`font-size: var(--text-xs)`。

### 权限矩阵单元格

真实 `<button>` 或 `<input type="checkbox">`（**非 styled div**），可焦点化；锁定/只读用 `disabled` 属性（让屏幕阅读器报"不可用"而非"看起来不可用"），配 `aria-label`。

### 抽屉

复用既有 `users-drawer` 结构（360px）；权限清单用 `--drawer-w-wide`（480px）变体。过渡 `--motion-base` + `--ease-drawer`。

### 卡片

`--panel` 底 + 1px `--border` + `--radius-lg` + `--shadow-xs`（**默认无阴影，不加 blur**）。

### 图标

**锁定：内联 SVG 描边（Lucide 几何风格）为全项目唯一图标来源。** 零依赖、零 CDN、零图标字体。尺寸 16（行内/文字按钮）/ 20（工具栏/抽屉按钮）/ 24（导航/独立）。`stroke-width:2`、`stroke:currentColor`、`fill:none`、`aria-hidden="true"`。**禁止 emoji 作功能图标。**

---

## 5. Layout & Spacing（布局与间距）

- **间距基准**：4px 网格，仅允许 `4/8/12/16/20/24/32/40/48`（禁 5/7/13/15/22/30）。
- **圆角**：`sm 4 / md 6 / lg 10 / xl 16 / pill`。
- **容器最大宽**：`--container-max 1200px`。
- **栅格**：桌面 12 列（gap 24）/ 平板 8 列 / 手机 4 列。
- **节区节奏**：桌面 24-32 / 平板 20 / 手机 16。
- **矩阵尺寸**：单元格 28px、行高 36px、冻结左列 160px（桌面）；768-1024 压至 24px；<768 退化手风琴。

---

## 6. Depth & Elevation（深度与阴影）

| 层级 | Token | 值 | 用途 |
|------|-------|-----|------|
| flat | `--shadow-flat` | none | 默认（矩阵单元格、行） |
| xs | `--shadow-xs` | `0 1px 2px rgba(0,0,0,.04)` | 工具栏/卡片 |
| raised | `--shadow-raised` | `0 10px 26px rgba(15,23,42,.08)` | 既有卡片强调 |
| drawer | `--shadow-drawer` | `-4px 0 16px rgba(0,0,0,.08)` | 右侧抽屉 |

- z-index：base 0 / dropdown 100 / sticky 110 / modal 200 / toast 300 / drawer 100（沿用既有）。
- 毛玻璃：**未使用**（无功能目的不作装饰）。

---

## 7. Do's & Don'ts（设计守则）

### Do（应该做）
1. 信息靠**对齐 + 1px 细线 + 留白**组织（Density=7 → 用 `border-t`/`divide-y`/负空间，不用卡片盒子）。
2. 三态单元格**实心/虚线/空**三种形态，色盲可辨。
3. 内置角色包复选框 `disabled` + tooltip「内置角色包，权限由系统定义」。
4. 真实数据（成员数来自 `warehouse_users` 聚合），**禁止虚构指标**。
5. 沿用既有容器规格与 `--brand`，不引入新视觉语言。
6. 管理员可见性：导航"权限管理"仅 `is_admin` 渲染。
7. 底部"已授予 N / 共 M"实时计数，编辑前后一致。

### Don't（不应该做）
1. 禁 emoji 作功能图标（用锁定 SVG 图标库）。
2. 禁紫→粉渐变主视觉（Indigo→Pink 三位一体套路）。
3. 禁组件内裸写 hex（只引用 token）。
4. 禁"彩虹矩阵"（域分组用中性 chip，区分交给图标）。
5. 禁 `border-left/right >1px` 彩色侧条纹作强调。
6. 禁 `background-clip:text` 渐变文字。
7. 禁默认毛玻璃/装饰性模糊。
8. 禁卡片圆角 ≥24px（上限 16px）。
9. 禁虚构数字、禁 "Welcome to"/"Lorem ipsum" 空洞文案。
10. 禁奶油/米色背景默认化（保持中性冷灰底）。

---

## 8. Responsive & Accessibility（响应式与无障碍）

### 响应式

| 断点 | 矩阵行为 |
|------|----------|
| ≥1024px | 完整矩阵：冻结列 + 5 角色列（+平台管理员锁定列），格 28px、行高 36px |
| 768-1024px | 压缩：格 24px，域默认全闭 |
| <768px | **退化为逐角色手风琴**（每角色一卡，卡内按域列「权限名 [已授予]」），**不做横向滚动** |

- 移动端手风琴行 ≥44px（WCAG 2.5.5）；桌面矩阵格 28px 属鼠标优化（键盘可达已保障）。
- 导航：<768px 并入既有 `mobile-nav` 的「账号」组，不新增底部 Tab（底部导航 ≤5）。
- 冻结左列用 `position:sticky; left:0`（**非 absolute**），避免横向滚动错位。

### 无障碍

- **对比度**：正文 ≥4.5:1（`--muted #6b7280` on `#fff` ≈ 4.8:1）；`--perm-cell-on-fg #0b4f4a` on `#ecfdf5` ≈ 7:1。
- **键盘**：Tab 进出矩阵，方向键在单元格间移动（roving tabindex）；焦点环 `outline:2px solid var(--brand); offset 1px`。
- **不靠颜色单独表意**：三态/角色类别均有形态或文字冗余（勾/虚线/锁图标；实心/描边；中文名）。
- **`prefers-reduced-motion`**：抽屉与勾选动画降级为 0ms。
- **`forced-colors`**：角色标签用 `CanvasText` 轮廓兜底。

### 5 态覆盖

Loading（骨架屏）/ Empty（无权限点定义 → 说明）/ Error（加载失败 + 重试）/ Populated（域分组 + 三态格）/ Edge（>60 点强制全闭；域 >8 纵向滚动）。

---

## 9. Agent Implementation Guide（实现指南）

### 关键实现提示

1. **零依赖**：纯 static CSS/JS，内联 SVG，无框架、无 CDN、无图标字体。
2. **token 引用**：`import tokens from './design-tokens.json'`；CSS 侧 `import './design-tokens.css'`，组件内**零裸 hex**。
3. **矩阵表格**：`border-collapse:separate; border-spacing:0`（**非 collapse**，冻结列更可控）；冻结列 `position:sticky`。
4. **单元格**：真实 `<button>`/`<input type=checkbox>` + `aria-pressed`/`checked` + `disabled`；不自造 div 开关。
5. **DOM 连续性**：阶段 1 仅改 `users.html` 下拉与 tag，不破坏 `ajax-form.js` 的 `reload-stay`；阶段 2 新增 `/admin/permissions`。
6. **数据源**：权限点来自 `permissions_manifest.PERMISSION_CATALOG`（25 点）；角色包来自 `ROLE_PACKAGES`（5 包）。**UI 与守卫共用同一 manifest**（单一事实来源）。
7. **已知坑**：模板 `is_rd/is_storefront` 只能在 context_processor 取（架构 §11-6）；角色包变更走 `--reseed-builtin-roles`。

### Tailwind（如项目未来采用；当前为纯 CSS）

```js
// tailwind.config.js（节选）
module.exports = {
  theme: {
    extend: {
      colors: {
        bg: 'var(--bg)', surface: 'var(--panel)', fg: 'var(--text)',
        muted: 'var(--muted)', accent: 'var(--brand)', 'accent-ink': 'var(--brand-ink)',
        border: 'var(--border)', danger: 'var(--danger)',
        'cell-on': 'var(--perm-cell-on)', 'cell-on-bg': 'var(--perm-cell-on-bg)',
        'cell-inherited': 'var(--perm-cell-inherited)', 'cell-off': 'var(--perm-cell-off)'
      },
      fontFamily: { ui: 'var(--font-ui)', mono: 'var(--font-mono)' },
      borderRadius: { sm: '4px', md: '6px', lg: '10px', xl: '16px' },
      spacing: { '1': '4px', '2': '8px', '3': '12px', '4': '16px', '5': '20px', '6': '24px', '8': '32px' }
    }
  }
}
```

### CSS 变量定义

完整 `:root` 见 `design-tokens.css`（A1/A1-structure/A2/B-slot/C-extension 分层）。

---

> 本文件所有颜色/间距/圆角均为 token 引用或 token 定义；零 emoji；无渐变主视觉；零新依赖。变更须在 `design-system/MASTER.md` 末尾变更记录追加一行。
