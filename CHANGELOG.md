# CHANGELOG

所有对外可感知的变更记录在此。格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [Unreleased]

### Added — 问题反馈闭环（feat/feedback-board，2026-10-17）
- **用户提单**：任意登录用户在每页右上「反馈」或导航「提交问题」提交问题单（分类 / 一句话说明 / 详细描述），自动带出当前页面路径。数据落 master.db 新增的 `feedback_tickets` + `feedback_comments` 两表（系统级，不沾仓库库）。
- **双通道提醒**：提交后站内通知扇出全部平台超管（导航「反馈收件箱」带待处理角标）+ 企微群机器人推送（`WECOM_FEEDBACK_WEBHOOK`，空 = 不推企微，站内照发）。两条通道都跑在业务事务外，任一失败不回滚已落库单子。
- **管理员判定**：`/admin/feedback` 收件箱（待处理置顶 + 状态筛选）→ 详情页二选一：「不重要，忽略」（终态）或「写评论 + 发送并派发开发」（终态）。
- **开发闭环派发**：派发按 GitHub REST 建 Issue（标签 `user-feedback`），正文含反馈原文、管理员意见、来源页与闭环要求（开发 → PR → CI → merge → 部署）。一票一单（`issue_number IS NULL` 守卫 + 路由层前置判重）。token 缺失或 API 失败时降级为「只存评论 + 明确提示手动建单」，**绝不伪装成功**。
- **权限**：用户侧 `LOGIN`、管理侧 `PLATFORM`，`ROUTE_MATRIX` 9 条已登记；他人单子一律 404（不泄露 id 存在）。
- 新增 `tests/test_feedback.py`（51 用例）；`docs/ARCHITECTURE.md` 新增 §3.10。

### Added — 商业化 P0 收敛（feat/csrf 分支，2026-10-10）
- **CI 门禁**：`ci.yml`（ruff 增量 + pytest 全量零新增失败断言 + deploy 依赖 test 通过）。
- **CSRF**：全站 75 个 POST 表单注入 token（Flask-WTF）；TESTING 模式存量测试零改动。
- **Rate limit**：全局 IP 600 次/分钟；登录 15 分钟窗内连错 5 次锁 30 分钟；MCP per-token 配额（默认 120/分钟，`agent_tokens.rate_limit` 可调）。
- **迁移 runner**：`flask migrate status|up|down`（user_version 门控 + 迁移前自动热备）。
- **可回滚部署**：`scripts/deploy_vps.sh`（备份→切换→迁移→健康检查→失败自动回滚）；部署 tar 严格排除数据文件，修复原 deploy 脚本可能覆盖生产数据库的问题。
- **合规文档**：LICENSE / SECURITY.md / PRIVACY / TERMS / SLA / 本文件。

### 测试
- 新增 `tests/test_csrf.py`（7）、`tests/test_rate_limit.py`（15）、`tests/test_migration_runner.py`（9）。
- 基线：18 failed（pre-existing 恒定）/ 742+ passed，CI 强制零新增。
