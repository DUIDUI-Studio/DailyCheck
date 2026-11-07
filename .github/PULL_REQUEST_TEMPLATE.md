<!--
DailyCheck PR 模板 v4（2026-10-24）
- 门禁 scripts/agent_framework/check_reports.py 要求 body 含三个 h2 段：
  需求对齐 / Checklist / 生产侧事项，且「需求对齐」段内不得出现未消解记号。
- R 表占位用「红叉 + 待填」（字符本身见下方 R 表）：**没填 = 门禁红，这是预期行为**；
  把每一行的占位换成对勾 = 门禁绿。
- ⚠️ 除 R 表占位外，本文件任何位置（含本注释、含 Checklist 行）都**不能**写
  门禁标题的字面文本，也不能写未消解记号字符：门禁按字面切段，注释里出现标题
  会把切点提前，随后注释里的记号被算成违规 —— 这时作者把 R 表全部填好也**永远
  转不绿**（2026-10-24 实测，只改 R 表不能自救，必须改注释）。需要指代时写
  「红叉」「未消解状态」等文字，别写字符。
- 「生产侧事项」只写规范流程粒度（变更类别/影响面/前置/观察重点），
  不写逐条操作脚本——Prod Agent 依 workflow.md §五 标准流程自行编排。
  写法铁律见 workflow.md §三′。
-->

## 报告 1/4：需求对齐

| ID | 需求 | 状态 |
|----|------|------|
| R-001 | （需求一句话描述；来源：需求 / Issue #N / prod 首演发现） | ❌ 待填 |
| R-002 | | ❌ 待填 |

**根因**：（为什么改。无则写「功能新增，无根因」）

**设计取舍**：（关键方案选择的理由。无则写「无」）

**不在范围（明确排除）**：（本 PR 明确不做的事，防止 scope creep）

## MCP 影响评估

按「新增 / 更新 / 不变 / 暂不开放」填写，可在 changes 中列多项。tools 使用统一台账的
准确名称；不变/暂不开放可为空，但必须解释原因并列测试命令与结果。新增工具写新台账名称；
删除/重命名的旧名称归「更新」，额外填写 compatibility（调用者迁移/保留旧名等处理）。
不要求每个 Web 页面开放 MCP。详见 workflow.md 的 MCP 影响评估规则。

<!-- mcp-impact:v1 -->
```json
{
  "version": 1,
  "changes": [
    {
      "category": "不变",
      "tools": [],
      "reason": "待填：为什么本 PR 不改变 MCP",
      "evidence": ["待填：测试命令与结果"]
    }
  ]
}
```
<!-- /mcp-impact -->

## Checklist

- [ ] MCP 影响评估已填写，工具台账/文档一致性与影响门禁通过
- [ ] 报告 01 全部 R 条目已消解（表格内无未消解红叉）
- [ ] ruff 0 告警（仅改动文件）
- [ ] pytest 零新增失败（对比 `tests/known_failures.txt` 基线；清单只许收缩不许扩张）
- [ ] 相关测试已补/已更新；关键断言经变异验证（还原 bug → 测试真实 FAIL）
- [ ] 触及架构子系统已同步 `docs/ARCHITECTURE.md`（pre-commit arch 门禁强制）
- [ ] bash -n / dry-run 冒烟通过（若改动 shell 脚本）
- [ ] 未提交运行时文件（db/、backups/、logs/、.venv/、.workbuddy/ 等）

## 生产侧事项

<!-- ⚠️ 必填。写给 Prod Agent 的 merge 判定与部署参考。 -->

**变更类别**：（代码逻辑 / 新增依赖 / DB schema / 配置文件 / nginx / 框架与部署脚本 / 纯文档——可多选）

**影响子系统**：（触达的模块，如 canonical / store-ordering / 蓝绿框架 / MCP；无则写「无——纯文档」）

**部署参考**：（本次变更应套用标准蓝绿流程的哪些环节；需要的前置，如凭据 export / unit 变更；观察窗建议重点关注的失败模式类别。**只写规范流程粒度，不写逐条操作脚本**——写法铁律见 `docs/agent-framework/workflow.md` §三′）

**风险声明**：（Dev 已知的风险点。无则写「低；CI 门禁覆盖」）

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
