<!--
DailyCheck PR 模板（2026-10-15 重写，与门禁脚本对齐）
- 门禁：scripts/agent_framework/check_reports.py 要求 body 含 h2 级
  「## 报告 1/4」与「## Checklist」，且两段之间无 ❌。
- 因此占位状态用「❌ 待填」：没填 = 门禁红；全部改为 ✅ = 门禁绿。
- 旧模板的 h3「### 报告 1/4」与门禁不匹配（历史 bug），本版修正。
-->

## 报告 1/4：需求对齐

| ID | 需求 | 状态 |
|----|------|------|
| R-001 | （需求一句话描述；来源：需求 / Issue #N / prod 首演发现） | ❌ 待填 |
| R-002 | | ❌ 待填 |

**根因**：（为什么改。无则写「功能新增，无根因」）

**设计取舍**：（关键方案选择的理由。无则写「无」）

**不在范围（明确排除）**：（本 PR 明确不做的事，防止 scope creep）

## Checklist

- [ ] 报告 01 全部 R 条目已消解（表格内无「❌ 待填」）
- [ ] ruff 0 告警（仅改动文件）
- [ ] pytest 零新增失败（对比 `tests/known_failures.txt` 基线；清单只许收缩不许扩张）
- [ ] 相关测试已补/已更新；关键断言经变异验证（还原 bug → 测试真实 FAIL）
- [ ] 触及架构子系统已同步 `docs/ARCHITECTURE.md`（pre-commit arch 门禁强制）
- [ ] bash -n / dry-run 冒烟通过（若改动 shell 脚本）
- [ ] 未提交运行时文件（db/、backups/、logs/、.venv/、.workbuddy/ 等）

## 其他报告（CI 自动产出，无需填写）

- 报告 2/4 测试报告 + 报告 3/4 代码质量：CI 跑 `generate_reports.sh` 自动产出
- 报告 4/4 本地部署预演：CI 按 `PR_NUM` 自动生成占位
- 报告 5 生产验证：merge 后由 Prod Agent 跑 `scripts/blue_green/verify.sh` 自动落盘
  `backups/prod-verifications/<date>-pr-<num>.md`

## 风险与回滚

**风险**：（本次改动可能引入的风险。无则写「低；CI 门禁覆盖」）

**回滚**：（merge 后发现问题时的回退方式，如 revert commit / rollback.sh）

---

**Dev Agent: （名字） · HEAD （短SHA） · （YYYY-MM-DD HH:MM CST）**
