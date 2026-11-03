# 报告 03/4：代码质量

2026-11-03，本地代码候选 `1f995e2f0c6b962e27551c309e752ce8149ad9df`。

| 检查 | 命令 | 原生退出码/结果 |
|---|---|---|
| 改动 Python lint | `.venv/bin/ruff check blueprints/wms.py mcp_server/service/wms_web.py permissions_manifest.py tests/test_wms_dashboard.py tests/test_wms_web_service.py` | 0，All checks passed |
| 安全扫描 | `.venv/bin/bandit -r blueprints/wms.py mcp_server/service/wms_web.py -lll` | 0，High 0；6 条低置信度 SQL 模板中危记录，外部值均绑定参数 |
| 契约 | `.venv/bin/python -m scripts.mcp_contracts --check` | 0，32 runtime / 19 external subset / 22 Bot approved |
| 架构生成图 | `.venv/bin/python scripts/arch_map.py --check` | 0，与源码一致 |
| 权限/图/受影响测试 | `.venv/bin/python -m pytest -q tests/test_wms_dashboard.py tests/test_wms_web_service.py tests/test_mcp_wms.py tests/test_route_permission_matrix.py tests/test_arch_map.py tests/test_nav_integration.py` | 0，199 passed in 18.85s |
| 开工与红线 | `check_pr_hygiene.py --branch feat/issue-140-wms-owner-details --base origin/main --body-file reports/issue-140/pr-body.md`；`check_redlines.py --base origin/main --body-file reports/issue-140/pr-body.md` | 各 0，无红线 |
| 变异 | 临时移除仓库范围、单据唯一 ID、方向映射或重复行数量，再运行对应服务断言 | 四项原生 pytest 各 exit 1，出现 AssertionError，随后恢复源码 |

Ruff 0.17.3 和 `uvx --from ruff==0.16.10 ruff check`（同一文件列表）均原生 exit 0，匹配 CI 固定版本。无 shell 变更。MCP 服务、工具 schema、Bot ACL 与批准池、known_failures、工作流和分支保护均未修改。

原始输出及变异脚本保存在外部 `writer-evidence-task_c1ad7027772a` 目录，绝对路径见 verification.md。独立只读 review 归 Coordinator，本文不构成独立评审。
