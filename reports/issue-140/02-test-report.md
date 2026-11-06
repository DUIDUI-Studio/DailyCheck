# 报告 02/4：测试

代码候选 `1f995e2f0c6b962e27551c309e752ce8149ad9df`，Python 3.12.13，pytest 9.2.0，2026-11-03。

| 阶段 | 原生命令 | 终态/退出码 |
|---|---|---|
| 完整基线 | `.venv/bin/python -m pytest -q --junitxml=reports/issue-140/baseline.xml` | exit 1；14 failed, 3614 passed, 39 warnings in 300.51s |
| 完整最终 | `.venv/bin/python -m pytest -q --junitxml=reports/issue-140/final.xml` | exit 1；14 failed, 3642 passed, 39 warnings in 295.30s |
| 零新增门禁 | `.venv/bin/python scripts/check_known_failures.py reports/issue-140/final-pytest.txt` | exit 0；14 条均在清单内 |
| 失败集/完整性独立核对 | 原始 stdout 的 FAILED 集合与 baseline 完全相等；XML tests=3656, failures=14, errors=0 | 核对脚本 exit 0，最终输出无中断标记 |
| 聚焦/权限/架构/导航 | `.venv/bin/python -m pytest -q tests/test_wms_dashboard.py tests/test_wms_web_service.py tests/test_mcp_wms.py tests/test_route_permission_matrix.py tests/test_arch_map.py tests/test_nav_integration.py` | exit 0；199 passed in 18.85s |
| 四项变异 | 仓库范围、单据方向、唯一 ID、行数量 | 四次对应 pytest 均 exit 1 且 AssertionError，源码已恢复 |

`tests/known_failures.txt` 没有修改。完整失败集是同一 14 个既有问题，未把原生 exit 1 描述成 pytest 全绿。

修复过程中一个完整结果曾为 19 failed/3637 passed，零新增门禁正确拒绝五个导航用例的新增失败。根因是可选上游未配置时列表页变成 503；恢复原有 200 安全错误页后，导航用例及本次完整运行通过零新增门禁。另一个主动停止、含 KeyboardInterrupt 的尝试单独保留，未用作最终结果。

所有原始 stdout、XML、退出码、失败比较及变异结果位于外部 writer-evidence 目录，绝对路径见 verification.md；表中为当时实际生成路径。CI 和 Dev 的 WMS 数据均为合成夹具，未连接第三方上游。
