# 报告 04/4：部署预演报告（本地预演，未上生产）

> 生成方式：本地启动 + 只读线上核对
> 说明：本 PR 只改 Python 与模板文案，无 schema 变更、无新外部依赖。
> 生产部署由 Prod Agent 在 merge 后按蓝绿流程执行。

## 1. 元信息

| 字段 | 值 |
|------|-----|
| PR | #18 |
| 分支 | chore/feedback-gitea-copy |
| 基线 | origin/main @ 0e75255 |
| 部署主机 | 未部署（本地 127.0.0.1:5099 预演） |
| 生成时间 | 2026-10-24T16:58:00+08:00 |

## 2. 环境对齐检查

| 项 | 本机（dev） | wdg_vps（prod） | 一致性 |
|----|----------------|----------------|--------|
| Python | 3.13.12 | 3.12.3 | 同一 minor 线，未用 3.13 语法特性 |
| 依赖 | 无新增 | 无新增 | 一致（纯标准库 `urllib`） |
| 数据库 | 未改动 | `db/master.db` 未触碰 | 无 migration |
| 环境变量 | 无需新增 | 无需新增（新键有默认值） | 一致 |

## 3. 启动日志摘要

```
app loaded, routes with feedback: ['/admin/feedback', '/admin/feedback/<int:ticket_id>',
  '/admin/feedback/<int:ticket_id>/comment', '/admin/feedback/<int:ticket_id>/dispatch',
  '/admin/feedback/<int:ticket_id>/ignore', '/feedback', '/feedback/<int:ticket_id>',
  '/feedback/api']
 * Serving Flask app 'app'
 * Debug mode: off
 * Running on http://127.0.0.1:5099
127.0.0.1 - - [24/Oct/2026 16:54:32] "GET / HTTP/1.1" 302 -
127.0.0.1 - - [24/Oct/2026 16:54:32] "GET /feedback/new HTTP/1.1" 302 -
127.0.0.1 - - [24/Oct/2026 16:54:32] "GET /login HTTP/1.1" 200 -
```

无 traceback、无 `UndefinedError`（模板新增引用只有既有 `dispatch_enabled` 变量）。

## 4. 关键路径冒烟

| 路径 | 方式 | 状态 | 说明 |
|------|------|------|------|
| 应用加载 | 本地起进程 | 通过 | 8 条 feedback 路由全部注册 |
| 未登录跳转 | `curl /feedback/new` | 通过 | 302 → 登录页（鉴权未受影响） |
| 链接拼接 | 进程内直调 | 通过 | `issue_url(17)` → `http://100.70.210.104:3000/eric/dailycheck/issues/17` |
| 回填不吃内网地址 | 单测 | 通过 | `test_create_issue_url_ignores_internal_html_url` |
| WEB_BASE 空回退 | 单测 | 通过 | `test_issue_url_falls_back_to_html_url_when_web_base_empty` |
| 真实建单 | 生产侧受控演练 | 通过 | 建出 issue #17（标签 `user-feedback` 已挂），随后关闭 |

## 5. 阻塞与已知问题

**（1）办公端不可达的根因不在本 PR**

Gitea 实例 `ROOT_URL` 仍是内网 `172.28.129.216:3000`。本 PR 只在应用侧绕开，不改 Gitea 全局配置 —— 因此 `ops_approvals` 页面里**历史**记录中的 `pr_url`（Prod Agent 登记时写入）仍可能指向内网地址。若要根治，需改 Gitea `ROOT_URL`，影响全域链接，属独立决策。

**（2）派发后自动开发仍未接通**

Gitea repo webhook events 不含 `issues`，兜底 cron `02ee9e01cb13` 为 `enabled=false`。建单成功不会自动起开发任务，与本 PR 无关，需单独立项。

## 6. 回滚方案

纯代码回滚，无状态残留：

```bash
git revert 431e2cd    # 或直接在 Gitea 上 revert PR #18
```

`FEEDBACK_GITEA_WEB_BASE` 未配置时行为与改动前完全一致（退回 `html_url`），因此回滚不需要动 env。

## 7. 报告签名

Dev Agent（WorkBuddy）· 基线 0e75255 · 工作分支 chore/feedback-gitea-copy
报告生成时间: 2026-10-24T16:58:00+08:00
验证范围: 本地启动 / 路由注册 / 鉴权跳转 / 链接拼接 / 单测 / 生产侧建单演练
未覆盖: 生产部署（等 merge）、管理员端页面人工点击核对（需登录态）
