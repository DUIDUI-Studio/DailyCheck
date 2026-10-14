<!--
DailyCheck PR 模板 — 强制四报告齐全才可 review/merge
详见 docs/agent-framework/v0.1.md §三
-->

## 改动摘要

<!-- 一句话讲清楚本次改动解决什么问题。例：
修复 /items 价格修改后未触发审批流的回归，详见 issue #42
-->

## 关联 issue / PRD

- issue / PRD 链接：

## 改动文件清单

<!-- 列出本次涉及的所有 .py / .html / .js / migration / schema 文件 -->

-

## 四报告（缺一不可）

> 模板见 [`docs/agent-framework/templates/`](../../docs/agent-framework/templates/)
> 检查器：[`scripts/agent_framework/check_reports.py`](../scripts/agent_framework/check_reports.py)
> **任一报告未填或状态非绿 → CI 拒绝 merge**

### 报告 1/4：需求对齐（Dev Agent 填写）

> 矩阵覆盖率 < 100% 阻塞 PR。

| ID | 需求（摘要） | 改动文件 | 单测 | 契约测试 | UI 测试 | 状态 |
|----|-----------|---------|------|---------|---------|------|
| R-001 | | | | | | ✅/⚠️/❌ |
| R-002 | | | | | | |

**不在范围内（刻意排除）**：

**假设与依赖**：

完整报告：[链接到本 PR 评论或附件]

### 报告 2/4：测试报告（CI 自动产出）

<!-- CI workflow 会自动运行并在此处贴 artifact 链接 -->

- [ ] 五层测试全绿（`tests/known_failures.txt` 零新增）
- [ ] 覆盖率 ≥ 基线
- 完整报告：CI artifact `02-test-report.md`

### 报告 3/4：代码质量（CI 自动产出）

<!-- CI workflow 自动产出 -->

- [ ] ruff 零新增告警
- [ ] 复杂度不恶化（最高圈复杂度 ≤ 20）
- [ ] 安全扫描零新增高危
- 完整报告：CI artifact `03-code-quality.md`

### 报告 4/4：本地部署预演（Dev Agent 半自动）

> 模板：[`docs/agent-framework/templates/04-local-deploy-preview.md`](../../docs/agent-framework/templates/04-local-deploy-preview.md)
> 命令：`bash scripts/agent_framework/preview.sh` → 产出 `04-local-deploy-preview.md`

- [ ] 环境对齐检查通过
- [ ] 关键路径冒烟 100% 绿
- [ ] 资源基线正常
- 完整报告（PR 路径）：`reports/pr-<num>/04-local-deploy-preview.md`

## Checklist

- [ ] 报告 01 完整填写，矩阵覆盖率 = 100% 或给出 N/A 理由
- [ ] CI 全绿（`agent-framework-reports` check 通过）
- [ ] 报告 04 已贴本 PR 路径或评论
- [ ] `git diff origin/main -- '*.py'` 通过 ruff 检查
- [ ] 触及架构子系统时同步更新 `docs/ARCHITECTURE.md`（门禁：pre-commit hook）
- [ ] **未**提交运行时文件：`.venv/`、`*.db`、`*.log`、截图、Playwright 调试文件

## 风险与回滚

<!-- 简短说明本次改动可能引入的风险，及对应的回滚动作 -->

**风险**：

**回滚**：

---

<!-- Dev Agent: <name> <commit-sha> -->
<!-- 报告生成时间: <ISO8601> -->