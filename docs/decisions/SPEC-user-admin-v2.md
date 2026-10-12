# Spec - 账号管理 v2

> 生成日期：2026-10-12
> 基于：探索报告（账号管理现状分析）+ Eric 拍板方案 A（Notion/Linear 风格）
> 状态：已确认（Eric 拍板 2026-10-12）
> 分支：`feat/user-admin-v2`（基于 `origin/main`）

---

## 1. 产品定义
- **一句话描述**：把 `/users` 从"卡片瀑布 + 行内折叠 details"重写为 Notion/Linear 风格的"顶部工具栏 + 紧凑表格 + 右侧抽屉"管理界面。
- **目标用户**：平台管理员（`users.is_admin=1`）。当前生产仅 1 个 admin。
- **核心问题**：用户增长（>30）后卡片页不可滚；启停账号只能删；改密无回显；无搜索/筛选/分页/批量。

## 2. MVP 范围（锁定——不在此列表一律不做）

| 优先级 | 功能 | 验收标准摘要 | RICE |
|--------|------|-------------|------|
| P0 | 顶部工具栏：搜索框 + 角色筛选 + 租户筛选 + 启停筛选 + "新建"按钮 | 即时过滤（前端 JS debounce 200ms） | 9 |
| P0 | 紧凑表格行：用户名 / 平台管理员 tag / 角色 tags / 最后登录 / 状态 badge | 一行一用户，6 列 | 9 |
| P0 | 行点击展开右侧抽屉：编辑（密码 / 启停 / 平台管理员）+ 仓库角色管理 + 删除 | 抽屉从右侧 360px 滑入 | 9 |
| P0 | "新建"按钮打开抽屉，渲染创建账号表单（含仓库权限矩阵） | 一次提交 | 9 |
| P0 | 启用/停用：`users.is_active` 字段 + `POST /users/<id>/toggle-active` | 停用账号不能登录 | 9 |
| P0 | 批量绑定：`POST /users/bulk-bind` body={user_ids[], warehouse_id, role} | 一次绑定多个用户到同一仓库同一角色 | 8 |
| P0 | 租户过滤：admin 跨租户可见；`?tenant_id=N` 过滤；非 admin scope 到 `g.tenant_id` | 隐藏其他租户用户 | 9 |
| P0 | 单测 `tests/test_users_admin_v2.py` 覆盖全部新行为 | ≥ 10 个测试，全绿 | 10 |
| P1 | 分页（page/page_size，默认 20） | 超过 20 条显示翻页 | 6 |

## 3. 明确不做（Out-of-Scope — 锁定）

| 不做的功能 | 原因 | 何时考虑 |
|------------|------|----------|
| 修改用户名 | schema UNIQUE + 改值影响登录 | v3 |
| 自助改密（非 admin 改自己密码） | 现状无"个人中心"页 | 后续独立 PR |
| 审计日志查询 UI | `admin_audit.log` 是 JSONL，无 db | 先迁 db 再做 UI |
| 租户 CRUD（创建/启停/编辑租户） | 多租户 PoC 仅做了字段 | 商业化阶段 |
| Agent Token 页面重写 | 本次范围只动 /users | 单独 PR |
| `agent_tokens.py` 的 `_require_admin()` 替换 | 跨页面变更 | 单独 PR |
| 删除账号的 `is_admin` 计数保护重构 | 既有守卫 | 不动 |
| 密码强度校验 | v2 不引入复杂度规则 | 后续 |

## 4. 技术架构（锁定 — 含版本锚定）

| 层 | 技术 | 实际版本 | 锁定原因 |
|----|------|----------|----------|
| 后端框架 | Flask | pyproject.toml 锁定 | 既有 |
| ORM | sqlite3 stdlib | Python 3.12 | 既有；不引入 SQLAlchemy |
| 数据库 | SQLite | 3.x | 既有 |
| 前端 JS | 原生 + `static/ajax-form.js` | - | 无新依赖 |
| 抽屉组件 | inline CSS + 60 行 vanilla JS | - | 零依赖 |
| 图标 | inline SVG（24px 行内 / 16px 小图标） | - | 无 emoji（专家团 P0-1） |
| 校验 | HTML5 pattern + `@require_platform_admin` | - | 既有装饰器 |

## 5. API 端点清单（锁定）

| Method | Path | 功能 | 认证 | 请求体 | 响应体 |
|--------|------|------|------|--------|--------|
| GET | `/users?q=&role=&tenant_id=&is_active=&sort=&page=&page_size=&format=html\|json` | 列表 + 筛选 + 分页 | `@require_platform_admin` | — | HTML 页 / JSON 片段（Accept: application/json） |
| POST | `/users/create` | 创建账号（既有） | `@require_platform_admin` | form: username/password/is_admin/wh_<id>=role | 302 → /users |
| POST | `/users/<id>/bind` | 绑定角色（既有） | `@require_platform_admin` | form: warehouse_id/role | 302 |
| POST | `/users/<id>/unbind` | 解绑（既有） | `@require_platform_admin` | form: warehouse_id | 302 |
| POST | `/users/<id>/change-role` | 改角色（既有） | `@require_platform_admin` | form: warehouse_id/role | 302 |
| POST | `/users/<id>/reset-password` | 重置密码（既有） | `@require_platform_admin` | form: password | 302 |
| POST | `/users/<id>/toggle-admin` | 切换平台管理员（既有） | `@require_platform_admin` | — | 302 |
| **POST** | **`/users/<id>/toggle-active`** | **新增**：切换 is_active | `@require_platform_admin` | — | 302 |
| **POST** | **`/users/bulk-bind`** | **新增**：批量绑定 | `@require_platform_admin` | form: user_ids[] (csv)/warehouse_id/role | 302 |
| POST | `/users/<id>/delete` | 删除（既有） | `@require_platform_admin` | — | 302 |
| POST | `/warehouses/create` | 创建仓库（既有） | `@require_platform_admin` | form: code/name/warehouse_type/clone_from_code | 302 |
| POST | `/warehouses/<id>/init` | 初始化仓库（既有） | `@require_platform_admin` | form: confirm_token | 302 |

## 6. 数据库表变更（锁定）

```sql
-- users 新增列
ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1;
-- 列表筛选索引
CREATE INDEX idx_users_tenant_active ON users(tenant_id, is_active);
```

迁移位置：`db/__init__.py:init_master_db()` 的 ALTER 块（PRAGMA 守卫做幂等）。

## 7. 页面清单（锁定）

| 页面 | 路由 | 核心组件 |
|------|------|----------|
| 账号管理（重写） | `/users` | 顶部工具栏 + 紧凑表格 + 右侧抽屉（创建/编辑）+ 仓库卡片网格 |
| 仓库卡片网格 | `/users` 内嵌区块 | 创建仓库折叠卡片 + 仓库管理卡片网格（既有） |

## 8. 设计 Token（沿用既有 + 新增）
- 主色：`#0f766e`（teal，style.css 既有）
- 表格行 hover：`#f9fafb`（灰）
- 状态 badge：active=`#10b981`（绿）/ inactive=`#9ca3af`（灰）
- 抽屉：`position: fixed; right: 0; top: 0; width: 360px; height: 100vh; background: #fff; box-shadow: -4px 0 16px rgba(0,0,0,0.08); z-index: 100`
- 字体：style.css 既有
- 图标：inline SVG（24px / 16px），无 emoji
- 主题：浅色（既有）

## 9. 验收标准（锁定——QA 以此为唯一依据）

| 编号 | 功能 | EARS 格式 |
|------|------|-----------|
| AC-01 | 搜索 | While admin 在搜索框输入文本 ≥ 2 字符，系统必须过滤 `username LIKE '%q%'` 用户并刷新表格 |
| AC-02 | 角色筛选 | If admin 选择角色"manager"，系统必须仅显示在任一仓库有 manager 角色的用户 |
| AC-03 | 启停筛选 | If admin 切换启停筛选到"已停用"，系统必须仅显示 `is_active=0` 的用户 |
| AC-04 | 新建抽屉 | When admin 点击"新建账号"，系统必须打开抽屉并显示创建账号表单 |
| AC-05 | 编辑抽屉 | When admin 点击表格行，系统必须打开抽屉并显示该用户的详情与编辑表单 |
| AC-06 | 切换启停 | When admin 提交 toggle-active，系统必须切换 is_active 并刷新该行 |
| AC-07 | 批量绑定 | When admin 提交 bulk-bind，系统必须把多个用户绑定到指定仓库同一角色 |
| AC-08 | 守卫 | If 非 admin 访问 /users，系统必须返回 403 |
| AC-09 | 租户筛选 | While admin 跨租户，系统必须按 `?tenant_id=N` 过滤用户列表 |
| AC-10 | 停用登录拦截 | When `is_active=0` 用户尝试登录，系统必须拒绝并显示"账号已停用" |
| AC-11 | 分页 | While 用户总数 > page_size，系统必须显示翻页控件 |
| AC-12 | 守卫替换 | When `users.py` 任一视图被调用，系统必须经过 `@require_platform_admin` 装饰器 |

## 10. 边界与约束
- 无新 npm / pip 依赖
- 保留既有 CSRF 防护（每个 form 带 `csrf_token()` 隐藏字段）
- 保留既有 `ajax-form.js` 行内表单模式（重置密码 / 切换平台管理员 / 解绑仍走行内 form + reload-stay；抽屉内表单走整页刷新）
- 危险操作（删除 / 初始化仓库）保留 confirm 弹窗
- `is_active=0` 用户的密码哈希不会被清空（保留供将来恢复）

## 11. 内嵌已知坑

| 坑 | 技术栈指纹 | 根因 | 修法 |
|----|------------|------|------|
| `_require_admin()` 重复 | `blueprints/users.py:31-34` | 历史内联 | 本次替换为 `@require_platform_admin`（仅 users.py） |
| `tenants` 表已建但无 CRUD UI | `master.db` | P0-1 PoC 范围 | UI 仅 SELECT 用于筛选，不做 CRUD |
| dev master.db 是 stub 库（缺 code/db_path） | `master.db` | 测试 fixture 跑 init_master_db() 后完整 | 不影响测试 |
| `users.html` 251 行单页 | `templates/users.html` | 既有 | 重写为工具栏+表格+抽屉；仓库管理区块保留 |

## 12. 端到端验证步骤

```bash
# 1. ruff（仅改动文件零新增）
/Users/ericmr/.workbuddy/binaries/python/versions/3.13.12/bin/ruff check \
  blueprints/users.py db/__init__.py tests/test_users_admin_v2.py

# 2. 新测试
~/.local/bin/python3.12 -m pytest tests/test_users_admin_v2.py \
  --basetemp=/tmp/pytest-dc -v

# 3. 全量 pytest（基线对比）
~/.local/bin/python3.12 -m pytest --basetemp=/tmp/pytest-dc -q 2>&1 | tail -3
# 期望：18 failed 恒定（pre-existing 基线），passed +N（N ≥ 10 新测）

# 4. 关键页面
# 启 dev：cd /Users/ericmr/Documents/GitHub/DailyCheck && flask run --port 8080 &
# curl -b "session=admin-cookie" -I http://127.0.0.1:8080/users  # 200
```

## 13. 变更记录

| 日期 | 变更内容 | 原因 | 影响范围 |
|------|----------|------|----------|
| 2026-10-12 | 新建（方案 A 重写 + 租户过滤 + 单测） | Eric 拍板 | /users、users.py、master.db schema、tests/、docs/ |
