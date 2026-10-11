# CHANGELOG

所有对外可感知的变更记录在此。格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [Unreleased]

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
