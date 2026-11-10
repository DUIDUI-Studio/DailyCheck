# DailyCheck Agent 协作工作流

> 版本：2026-10-15 定稿（Eric 口述对齐 + 实况核对；同日二次修订——push 政策放开、merge 政策改为 Prod 判定）。2026-10-25 修订——新增 §四「merge 后删除源分支」与 §三批量清理豁免（Eric 拍板）。2026-10-27 修订——全局化「CI 绿不是终态」，要求手动与定时 Coordinator 跟踪到部署成功。
> 新/旧 agent session 接入本机制的提示词见 [`session-bootstrap.md`](session-bootstrap.md)。
> 本文档是 Dev / Coordinator / Prod / Owner 四方协作的权威口径；与本文冲突的口头约定以本文为准。
> 机制细节的演进史见 `docs/agent-framework/v0.1.md`。

## 一、角色分工

| 角色 | 载体 | 职责 | 绝不碰 |
|------|------|------|--------|
| **Dev Agent** | 本地开发机 | 开发、测试、提 PR（含「生产侧事项」段落，见 §三） | merge（由 Prod Agent 执行，§四）、生产环境（VPS 只读排查需 owner 授权） |
| **Coordinator / Triage** | 本地 Orca（手动或定时） | Issue 认领、分派 writer/reviewer、跟踪冲突/CI/部署终态、回流失败 | 业务代码（分派后由 writer 写）、merge、部署、生产环境、Issue 关闭 |
| **Prod Agent** | VPS (wdg_vps) | **merge 判定**、部署执行、生产验证、灰度切流、回滚、清理 | 仓库治理决策（主动改 main 内容、开非常规 PR；对 PR 内容有异议 → 打回/开 Issue，不自行改代码） |
| **Owner (Eric)** | — | 需求拍板、方案拍板、红线内政策的最终决定 | — |

需求来源两条：owner 直接提需求；或 Prod Agent 在生产验证中发现问题 → 开 Issue → 转 Dev。

## 二、端到端流程

```
① 需求/Issue ──► ② Dev 开发 + 本地门禁 ──► ③ 提 PR（模板 + 生产侧事项）──► ④ CI 门禁
                                                                              │ 全绿
   ┌──────────────────────────────────────────────────────────────────────────┘
   ▼
⑤ 自动交接 Prod Agent：独立审查 + 候选部署（§四）
   │
   ├─ 失败 ──► Coordinator 按 code/review/env 回流后重跑
   ▼
⑥ 部署 + 观察窗（蓝绿，§五）──► ⑦ merge / Issue 关闭 / 生产验证
   │
   └─ 部署结果成功 = 交付终态；CI 绿与 merge 均不是终态
```

### ② 开发 + 本地门禁（Dev Agent）

提交前 SOP，缺一不可：

1. 确认分支、确认改动范围
2. `ruff check` 仅改动文件零告警
3. pytest **零新增失败**：对比 `tests/known_failures.txt` 基线逐行比对（清单**只许收缩不许扩张**）
4. 关键断言做**变异验证**：把修复临时还原成 bug 版，确认新测试真实 FAIL（防恒真式假绿）
5. shell 脚本改动：`bash -n` + dry-run 冒烟
6. 触及架构子系统必须同步 `docs/ARCHITECTURE.md`（pre-commit hook 强制；helper：`scripts/arch_touched_sections.py`）

### ②′ 开工规范（Dev Agent，2026-10-24 起）

> 这一节约束「接到 issue 到第一次 commit 之间」。此前只有一句「确认分支、确认改动范围」，没有判据——规矩实际只活在个人记忆里，换 session、换 agent 就丢。标 **[机验]** 的进 CI（`check_pr_hygiene.py`），其余靠 review，但都是硬要求。

#### 认领与定范围

| # | 规则 |
|---|------|
| A1 | 开工前确认没人正在做：`git worktree list` + `git branch -r --list '*issue-<N>*'` + 读 issue 最后一条评论 |
| A2 | **先复现再改**。复现了贴证据；按 A/B/C 试过仍复现不了 → 在 issue 回帖说明，不猜着改 |
| A3 | 一个 PR 收不了就拆子 issue，不在一个 PR 里堆 |

#### 工作区隔离（硬要求）

| # | 规则 |
|---|------|
| B1 | 一律独立 worktree，**禁止在主工作树提交**：`git worktree add /tmp/dc-<N> -b <branch> origin/main` |
| B2 | 起点固定 `origin/main`，不用本地分支、不用 `HEAD` |
| B3 | 建完立刻验 upstream：`git rev-parse --abbrev-ref @{u}` 不得是 `github-legacy/*`。本仓 `origin` = Gitea、`github-legacy` 已废弃，无参数 `git push` 会推错地方（2026-10-24 实际发生） |
| B4 | 分支命名 `{fix\|feat\|chore\|docs\|ci\|test\|refactor\|perf}/issue-<N>-<slug>` **[机验 H1]** |
| B5 | 一个 issue 一个分支：分支号必须与 body 的 `Closes #N` 或 `Part-Of: #N` 一致 **[机验 H2]**；不带号的杂活必须写 `No-Issue: <理由>` **[机验 H3]** |
| B6 | 开工先跑一次全量测试并记下失败数（本地基线 14 条 known），否则分不清后面的红是不是自己造成的 |

#### 分支卫生

| # | 规则 |
|---|------|
| C1 | push 显式写 `git push origin <branch>`，不依赖 upstream —— worktree 的 upstream 可能指错 |
| C2 | **Gitea 保持既有行为**：生命周期内至少 merge 一次 `origin/main`；落后 >30 个提交时 CI 提示 **[机验 H4，只提示不拦]**。**GitHub native MERGE**：H tip 必须一父；GitHub queue 集成最新 main。未发布分支可 rebase；已发布 merge-tip 可普通 fast-forward 添加一父 child，或另开经审查新分支。不要求 force 更新已发布分支；`--force` / `--force-with-lease` 仍须 Owner 单独批准，此处没有该授权。 |

#### PR 颗粒度

判据不是「小」，是**能单独 revert 而不牵连其他修复**：

| # | 规则 |
|---|------|
| D1 | 一个 PR 只解一个 issue；多 issue 仅当**根因相同**才合并 |
| D2 | 顺手发现的其他 bug **另开 issue**，不在本 PR 修 |
| D3 | 行为变更与纯重构不混 —— 混了 review 看不出哪一行改了行为 |
| D4 | 门禁要求的文档同步（`ARCHITECTURE.md`）**必须**混；文档「改进」不能混 |
| D5 | （不采用）规模阈值（如 >8 文件 / >400 行须写 `Scope-Justification`）。阈值容易误伤，逼人为了过门禁硬拆 PR，反而破坏 D1-D4 真正要判的东西。owner 2026-10-24 明确排除 |
| D6 | 例外条款：**同根因的多点修改算一个 PR**，防止「一个 bug 修三处要开三个 PR」 |

反面教材：PR #22 把「红线机验 + CI diff 修复 + 文档三处」混成一个 PR，合并后无法单独回滚，而 Prod Agent 提的两条缺陷正好藏在这个混合体里（Issue #24 / #26）。

#### 收尾

| # | 规则 |
|---|------|
| F1 | PR body 三段齐全 + `Closes #N` **[机验]** |
| F2 | Dev 止于 PR，不 merge；Coordinator 接手跟踪到交付终态 |
| F3 | 在 issue 留阶段结论（不是关单，也不是交付终态） |
| F4 | worktree 用完删掉。2026-10-24 已积累 50+ 个 worktree、大半 `prunable` —— 每个 `git worktree list` 都变成噪音 |
| F5 | CI 绿后必须继续跟踪 Prod Agent；部署未成功时不得向 owner 报告“完成”，应报告 `awaiting_deploy` / `deploy_failed` |

#### 为什么不把这些全做成机验

| 条目 | 原因 |
|------|------|
| A1 / A2 | 是否真复现、有没有人认领，要读 issue 语义，判据不成立 |
| B1-B3 / B6 | CI 只看到 checkout 出来那份树，看不到本地 worktree 与 upstream |
| C1 | push 姿势发生在 CI 之外 |
| D1-D6 | 「是否一个可独立回滚单元」是语义判断，只能靠 review |
| F2 / F3 / F4 | merge 权与本地清理都在 CI 之外 |

### ③ 提 PR（模板强制）

- 新建 PR 自动带出 `.github/PULL_REQUEST_TEMPLATE.md`
- body 必含 h2 级「**## 报告 1/4：需求对齐**」+「**## Checklist**」+「**## 生产侧事项**」；R 表占位为「红叉 + 待填」，**没填 = 门禁红，全部换成对勾 = 门禁绿**
- ⚠️ 已知坑零（2026-10-24 owner 拍板 A 后确立，优先级最高）：上面三个标题的字面文本在 body 里**只能各出现一次**。门禁是 `pr_body.split("## 报告 1/4")[1]`，`split` 每遇一处匹配就切一刀 —— 注释里再写一次标题，`[1]` 就变成「两处标题之间的注释残余」，**R 表整段被挤到 `[2]`、完全不被检查**（漏检）；注释残余里若带红叉则反过来恒红、作者改 R 表无法自救。旧模板「一建 PR 就红且改代码无用」和 PR #10 改占位符字符后「R 表不填也绿」是同一个 bug 的两种表现。占位符**必须**用门禁认识的红叉，换成方块之类字符 = 门禁对未消解状态失明。两条都由 `tests/test_pr_template_gate.py` 锁死（含变异验证），`check_reports.py` 判定逻辑零改动
- ⚠️ 已知坑一：CI 用**事件时刻的 body 快照**（`github.event.pull_request.body`）。事后改 body（Gitea：`PATCH /api/v1/repos/{owner}/{repo}/pulls/{n}`）后，必须**推空 commit 触发新事件**，rerun 无效
- ⚠️ 已知坑二：「报告 1/4」段之后（直到「报告 2/4」标题，实践中 = 全文）**不许出现字面 ❌ 字符**——门禁靠它识别未消解状态，正文叙述里写 ❌（哪怕只是举例）也会被判定未消解。写「红叉」「未消解状态」等文字替代
- 本地自检捷径：`PR_BODY_FILE=/tmp/body.md python scripts/agent_framework/check_reports.py`

#### MCP 影响评估（Issue #105，Owner 授权的维护门禁）

业务功能 PR 必须保留模板的 MCP JSON 声明，按「新增 / 更新 / 不变 / 暂不开放」逐项列出
工具名称、原因、测试命令与结果。不变/暂不开放允许 tools=[]，但不能省略理由和验证证据；
Web 页面无需自动开放 MCP。复杂 PR 可列多个 changes，工具名称不可重复、不可未知。

`check_mcp_impact.py` 对 main 参考提交与 HEAD **先取 Git merge-base 再 diff**，包括删除和
重命名两端。blueprints、db、migrations、mcp_server、bot、config、permissions 路径以及
所有非 tests 的 Python/SQL（含未来共享计算模块）及运行时依赖清单保守触发评估；纯文档/测试改动无声明可跳过，
但若提供声明则必须有效。依赖清单包括 requirements*.txt/.in、requirements/constraints 目录、
pyproject.toml、setup.cfg、常见 Python lock/环境与解释器版本文件；只改 SDK 依赖也不能漏评估。
缺声明、非法类别、未知工具、空理由/测试证据、占位符均拒绝。
本地命令：`python scripts/agent_framework/check_mcp_impact.py --base origin/main --body-file /tmp/body.md`。
CI 用事件中的 main base SHA，不允许按 PR 自选比较基线。

工具名取 merge-base 与 HEAD 的**完整统一台账并集**，因此新工具可正常声明为「新增」。
名称只要求非空字符串、无重复、与台账精确匹配，不额外发明小写命名限制；实际登记有效性由
原有 contracts checker 负责，已登记的 FutureRead 或 _private 等 SDK 合法名称同样可声明。
实际台账/输入 schema/输出契约/ACL 等记录变化必须逐工具声明新增或更新，不许用「不变」或
「暂不开放」遮盖。删除/重命名旧工具归更新，必须另填非空 compatibility，说明调用者迁移、
旧名保留或其他明确处理；新名归新增。这里校验处置声明，实际兼容验证仍由专项测试负责。

同一个既有 `agent-framework-reports` 必需 job 先跑
`python -m scripts.mcp_contracts --check`（全部实现、完整 tools 与维护文档一致性），再跑影响
检查；任何一步非零即失败，不添加 branch protection check、不扩大 known_failures、不改 Bot
批准池或生产权限。影响检查只读取 Git 中的 JSON，不执行历史版本代码或访问业务库。
统一台账生成 CLI 维持原样：变更登记后先 `python -m scripts.mcp_contracts --write`，提交产物。
正常/拒绝示例见 [`mcp-impact.md`](mcp-impact.md)。

### ③′ 「生产侧事项」段落的写法铁律

PR body 的「生产侧事项」段落是 Dev 留给 Prod Agent 的**唯一**部署指引，粒度必须守住：

- **只写规范流程**：本次变更属于哪类（代码/依赖/schema/配置/nginx/框架脚本）、影响哪些子系统、部署时应套用流程的哪些环节、需要哪些前置（如凭据 export）、观察窗应重点看什么类别的问题。
- **不写针对性操作脚本**：不给逐条命令、不给"Step 1/2/3"式的操作手册。Prod Agent 以 workflow.md §五的标准流程为基准自行编排动作——它的价值在于**用自己的视角独立发现问题**，照抄 Dev 的脚本会让 Dev 的盲区变成双方的盲区。
- **允许 Dev 声明风险**：可以写"本次触及 X，生产验证时该类变更常见的失败模式是 Y 类"，这是提示不是指令。
- Prod Agent 对 PR 内容本身有异议（设计/风险/范围）：**不 merge**，在 PR 评论打回并开 Issue 转 Dev；merge 判定权含否决权。

### ④ CI 门禁（Gitea 分支保护 `main` 强制）

| check | 内容 | 全绿标准 |
|-------|------|---------|
| `CI / test (pull_request)` | pytest 全量 + 零新增门禁 | `check_known_failures.py` exit 0（失败集 ⊆ known_failures.txt） |
| `CI / agent-framework-reports (pull_request)` | 四报告齐全性 + MCP 台账/影响评估 | 报告 01（body 段落）+ 02（pytest gate）+ 03（ruff/bandit）+ 04（CI 自动生成）+ MCP 台账/文档一致、影响声明有效 |
| `CI / deploy (pull_request)` | 生产部署 | **永不自动触发**（2026-10-11 Eric 拍板禁用，仅 workflow_dispatch；正式启用前置：至少一次手动 workflow_dispatch 演练跑通） |

报告体系：01 需求对齐（Dev 填）/ 02 测试（CI）/ 03 代码质量（CI）/ 04 本地部署预演（CI 占位）/ 05 生产验证（Prod Agent 的 verify.sh 自动落盘 `backups/prod-verifications/`）。

### GitHub native-only CI 与 Prod gate（Issue164，本地 seam；未激活）

GitHub 的 H 是当前公开 PR head；B 是 merge_group 原始事件创建基线；G 是实际 tested /
deployed group；P 是实际 Git graph 直接前驱。保留有序成员、authenticated repo/queue/entry
和原私有 composite/source-map digest，不能从分支拼写猜成员，也不能把所有 H 替换成 G。

MERGE only：H 一父、G 至少二父且成员 H 都在 ancestry。`test` 与
`agent-framework-reports` 两个原 check 在 H/G 均覆盖完整门禁；每个 workflow 的全量
pytest 仅由 test 运行一次，reports 复用 exact repo/workflow blob/run/attempt/event/H或G/B/
member manifest 和 digest 绑定的原 native/session/hash proof，实际执行全部报告/成员门禁。
reports always 运行并显式拒绝缺失/失败/跳过 upstream；其 attestation 链接实际可信 test
artifact/envelope，不创建自己的 pytest session 或退出码。成员四报告/body/MCP/redline/hygiene
义务不减。现有 main push/workflow_dispatch test 开销保留，本次不改触发器或分片。
Prod 验证 approved workflow/check/run/attempt/App/evidence identity，不只看同名 green。
approved producer 还必须包含实际执行的 helper closure、known baseline 与固定 runtime。
protected policy pin 和 approved YAML literal pin 独立固定 manifest Git blob；不从 candidate
HEAD 或 PR body 选择 producer。helper/runtime 更新需新的独立批准 pin；缺 pin 即拒绝。
native job 的平台 controller 不执行 candidate code；pytest 在无网络、无 daemon socket、
nonroot/read-only runtime 容器运行，controller 在容器外捕获 native exit/output。reports 复用
exact test capture，在隔离容器执行全部报告/成员门禁，再由 fresh verifier 核验；失败、
skip、cancel、timeout、缺 session/provenance 不能作为 green。candidate log 不能注入原生 attestation。
preview 的 trusted-mode 依赖来自固定 runtime/authority，candidate app 仅为显式被测对象，
不从其 venv 选取 gate 依赖。固定 supervisor 通过正常返回与进程 exit 同时确认 T11/MCP/
Python gate 完成；native0、早退或 stdout 成功文本不能单独证明完成。默认 Gitea 入口保留。
queue 的普通 unbuilt tail 不属于 ready G prefix；included graph/成员仍严格验证。
当前 manifest 仅批准 bounded local linux/arm64 fixture，hosted CI/amd64/Prod 安装未证明，
真实 activation 保持 OFF；不将 synthetic parser/unit proof 或本地 fixture 报为部署成功。
PR body 是数据。只取消同 PR 旧 CI；group 以 event/G/run 独立并发，不取消 production。

原生队列可并行验证当前及一个 lookahead。G2 含尚未 merge 的 G1 可以先 CI；Prod 必须等
front/main/deployed=P=G1 且前驱已 merge/anchored。相同 G2 的旧 B 不必等于新的 main；
重建 G 必须新 proof。queue merge limit=1 不证明 singleton 内容；GitHub 独占集成顺序。

### ④′ 自动交付跟随契约（2026-10-27 Eric 拍板，全局适用）

CI 两必需 check 全绿后，Prod Agent 会自动接手独立审查和候选部署。**CI 绿不是
交付终态，merge 也不是。** Dev / Coordinator / 所有定时任务必须按同一状态机跟进：

| 状态 | 判据 | 当前 owner / 动作 |
|---|---|---|
| `ci_failed` | 任一必需 check 红 | Dev/Coordinator：原 Issue writer 修复并重新 Review/CI |
| `merge_conflict` | PR `mergeable=false` | Coordinator：原 Issue writer 同步最新 `origin/main`，修冲突，推进同一 PR |
| `review_blocker` | Reviewer 报 BLOCKER | Coordinator：writer 修复，新 Reviewer session 复审 |
| `awaiting_prod` | CI 全绿，PR 仍 open，等待 Prod Agent 审查/合并 | Coordinator：继续跟踪，不得结束为“完成” |
| `awaiting_deploy` | PR 已 merge，尚无成功部署报告 | Coordinator：继续查询部署报告和 Issue 状态 |
| `deploy_failed` | Prod Agent 回写失败 | 按归因回流：代码问题回 writer；环境/流程问题回 Prod Agent；不得盲改 PR |
| `delivered` | PR merged + Issue closed + 两项 CI success + 部署成功报告 | Coordinator：记录终态、清理 worktree 并向 owner 汇报 |

规则：

- 手动 Coordinator 在 CI 绿后继续跟踪，不把会话停在 `awaiting_prod`。
- 定时任务不要求单次运行占满 40–60 分钟部署窗口，但必须把队列状态持久化为
  `awaiting_prod` / `awaiting_deploy` / `deploy_failed` / `delivered`，由后续运行持续对账；
  禁止在 CI 绿或 merge 时直接把队列项标成 `done`。
- 日报/巡检必须区分“等部署”“部署失败”“已交付”；缺少部署报告时报告等待，
  不得推断成功。
- 只有 Prod Agent 可以 merge、部署、关闭 Issue；Coordinator 只跟踪与回流。

## 三、Push 政策（2026-10-15 Eric 放开）

- **普通 `git push`（新分支/已有分支推进）不再需要 owner 事先确认。**
- 仍需 owner 单独确认的操作：`--force` / `--force-with-lease`、删除远程分支、改分支保护规则。
- 豁免（2026-10-25 Eric 拍板）：`scripts/cleanup-merged-branches.sh` 的**批量清理**（只删已合入 main 的分支，merge-base 判定）视为已获授权，脚本内 `--no-verify` 即本次拍板的凭证姿势。单条手动删分支仍需逐次确认。
- pre-push hook（`core.hooksPath` → `~/.workbuddy/git-hooks`）2026-10-19 起是**黑名单**：只拦「删除远程分支」和「非快进覆盖」，新分支/快进照常放行。
  仍建议 `git push --no-verify`（避开 hook 里的网络调用），但不再强制。

## 三″、平台命令对照（2026-10-22 迁至自托管 Gitea）

CI/CD 已从 GitHub 迁到 hub1 上的 Gitea 28.1（`http://172.28.129.216:3000`，VPC 内网）。
agent 侧不再用 `gh`，改为直连 Gitea REST API（`Authorization: token <token>`）。

| 动作 | 原 GitHub 命令 | 现在（Gitea API） |
|---|---|---|
| 列 open PR | `gh pr list --state open` | `GET /api/v1/repos/{o}/{r}/pulls?state=open` |
| 看单个 PR | `gh pr view N` | `GET /api/v1/repos/{o}/{r}/pulls/N` |
| 建 PR | `gh pr create --draft` | `POST /api/v1/repos/{o}/{r}/pulls`，body `{head, base, title, body}` |
| 改 PR body | `gh pr edit N --body` | `PATCH /api/v1/repos/{o}/{r}/pulls/N`，body `{"body": "..."}` |
| 查 check 状态 | `gh pr checks` | `GET /api/v1/repos/{o}/{r}/commits/{sha}/status` |
| merge | `gh pr merge N --merge` | `POST /api/v1/repos/{o}/{r}/pulls/N/merge`，body `{"do":"merge"}`（**小写**，swagger 的 required 字段名就是 `do`） |
| 发评论 | `gh pr comment N` | `POST /api/v1/repos/{o}/{r}/issues/N/comments` |
| 开 issue | `gh issue create` | `POST /api/v1/repos/{o}/{r}/issues` |

三个必知差异：

1. **status context 带事件后缀**。Gitea 的 check 名叫 `CI / test (pull_request)`，不是裸 `test`。
   分支保护的 `status_check_contexts` 必须写全名，否则永远判定「未全绿」，merge 被拒
   （实测报错 `Not all required status checks successful`）。
2. **`github.event.pull_request.body` 仍然可用**。workflow 语法完全兼容，PR body 门禁无需改造（已实测跑绿）。
3. **没有 `check_suite` 事件**。Gitea 用 `status` 事件代替（每次 commit status 变化触发一次），
   payload 里没有 PR 关联信息，需按 `sha` 反查 open PR。

### 三″.1 地址与凭据（选错地址会直接 000）

| 你在哪 | API 根 | 判断方法 |
|---|---|---|
| 生产 VPS / 蓝绿实例（与 hub1 同 VPC `172.28.129.0/20`） | `http://172.28.129.216:3000/api/v1` | `ip -4 addr \| grep 172.28.129` 有输出 |
| 本机（Tailscale） | `http://hub01:3000/api/v1` | `tailscale status` 能看到 `hub01` |
| 公网 | **不可用**（ufw 关闭） | — |

- Token 两个、各专用，不要混用：**Dev Agent** 用于代码/PR/issue；**Prod Agent** 用于合并与部署判定。
- 本机 token 在 `~/.config/gitea-token`；生产在 `/etc/dailycheck/gitea-token`（0600 root，
  `dc-webhook-receiver.service` 通过 `GITEA_TOKEN_FILE=` 指向它，已实测可调 API）。
  ⚠️ `dailycheck_pr_poll.py` 的**默认**路径仍是 `/root/.gitea-token`（该文件不存在）——
  直接跑 poller 会认证失败，必须显式 `GITEA_TOKEN_FILE=/etc/dailycheck/gitea-token`。
  排查「快通道 / 轮询没反应」时先看这两处路径。
- **权限不足返回 403，不是 401**。看到 403 先查 token scope，别急着判定 token 失效。
- 分支保护 `main`：禁止直推（`enable_push=false`），必需 check =
  `CI / test (pull_request)` + `CI / agent-framework-reports (pull_request)`。

### 三″.2 五个高频坑（都实测过）

1. **别从旧分支开 PR**。GitHub 时代的旧分支自带旧 `ci.yml`（其中一步要去 github.com 下载 Python），
   在 hub1 上会超时重试，CI 能跑 1.5 小时。开 PR 前先 `git rebase origin/main`。
2. **runner 并发 = 1**。单 PR 的 CI 约 25–40 分钟，同时开多个大改动只会互相排队。
   别把 CI 当第一道关，本地先跑 ruff + pytest。
3. **`GET /pulls/{N}/merge` 是陷阱**：对已合并的 PR 也返回 204。判断是否已合并用
   `GET /pulls/{N}` 的 `state` + `merged` 字段。
4. **CI 卡在 `Checkout code` 7 分钟后失败 = 基础设施问题**（runner 与 Gitea 连通性），
   报 owner，不要改代码。
5. **登录后被踢回登录页** = `ROOT_URL` 与访问协议不一致导致 Cookie 带 `Secure` 被丢弃，
   报 owner 改 `app.ini`，不要自己动。

## 四、Merge 政策（2026-10-25 Eric 拍板：候选部署通过后 FF merge；原 2026-10-15 Prod merge）

**Prod Agent 在 CI 全绿后自动判定并执行「候选部署」**（2026-10-25 起，deploy-first；
2026-10-27 起自动交接要求 Coordinator 持续跟随）：CI 两必需 check 全绿 → 自动交接
Prod Agent → 独立审查 → 调 `deploy_pipeline.sh <PR> <sha> --candidate --merge-pr`。
验证、切流、观察通过后由脚本执行 fast-forward-only merge；merge 前任何一步失败，
PR 保持 open，不产生未验证的 main 提交。merge 与部署仍由同一角色贯通。
**自动交接不代表终态**：Coordinator 必须按 §④′ 跟踪到 `delivered`，或把失败准确
回流给代码 writer / 环境 owner。

Prod Agent 的 merge 判定 = 独立审查，不是橡皮图章：

1. 确认 CI 两必需 check 全绿（test + agent-framework-reports）
2. 读 PR 的「报告 1/4」（根因/取舍/不在范围）与「生产侧事项」，**用自己的生产视角评估**：变更类别、影响面、部署风险是否有未声明项
3. 有异议 → 不部署、PR 评论打回 + 开 Issue 转 Dev；无异议 → 进入 §五 候选部署
4. **merge 后删除源分支**（本地 + 远程，2026-10-25 Eric 拍板）：§五 的 FF merge 把工作并入 main 后，源分支留着只会污染列表、误导后续判断 → 随即删除源分支。Gitea PR 界面勾选「合并后删除分支」或手动删除；漏删的存量用 `scripts/cleanup-merged-branches.sh` 批量清理（只删已合入 main 的分支，merge-base 判定，安全）。注意：merge 用的 API `POST /pulls/{num}/merge` 不删分支，删除是独立的 `DELETE /repos/{o}/{r}/branches/{branch}`

红线清单——以下 PR 仍须上报 owner 拍板，Prod Agent 不得自行 merge：

1. 扩张 `tests/known_failures.txt`（清单本身只许收缩，出现扩张即流程违规）
2. 改 `.github/workflows/*`（CI/部署流水线自身）
3. 改分支保护规则 / ruleset
4. 删除或弱化 `docs/ARCHITECTURE.md` 铁律
5. owner 在需求中标注「需拍板」的变更
6. **改本工作流文档自身 / merge 政策**（防止角色自我扩权）

### 红线的执行方式（2026-10-24 起，从自述改为机验）

上表此前全靠自觉。2026-10-24 实测发现 PR #13 改了 `.github/workflows/ci.yml`
（红线 2）被 Prod Agent 自行合并，无人拦截 —— 因为 `check_reports.py` 只看 PR
body 格式，完全不看改动文件路径。

现在红线 1/2/3/5/6 由 `scripts/agent_framework/check_redlines.py` 机验，跑在
`agent-framework-reports` job 内。命中且无授权 → 该 check 红 → Prod Agent 合不动。

| 红线 | 判据 | 机验 |
|------|------|------|
| 1 扩张 `known_failures.txt` | `git diff --numstat` 新增行 > 0（只删不算） | 是 |
| 2 改 `.github/workflows/*` | 改动路径前缀 | 是 |
| 3 改分支保护 | 改 `branch-protection.md`；ruleset 本体在 Gitea 服务端，仓库内无文件 | 部分 |
| 4 删/弱化 `ARCHITECTURE.md` 铁律 | 需判断语义 | **否，靠 review** |
| 5 owner 标注「需拍板」 | 正文含该字样却无授权声明 | 是 |
| 6 改 workflow.md / 本套指令 | 改动路径精确匹配 | 是 |

**豁免**：PR body 写一行 `Owner-Authorization: <日期> <谁> <授权内容>`。这是自述、
无法证伪，但能让越线留下痕迹，事后可审计谁引用过哪条授权。

**刻意不机验红线 4**：`ARCHITECTURE.md` 每次子系统改动都要同步，按路径拦会制造
大量噪音，反而训练出「先绕过再说」的习惯 —— 噪音比漏拦更伤门禁。


设计依据（Eric 2026-10-15 口述）：Dev 在 PR 里只给**规范流程级**的部署信息（§三′），不替 Prod 写操作脚本——避免 Prod Agent 完全参照 Dev 的视角执行而发现不了 Dev 自己的盲区。merge 判定权交给生产端，使「合不合」由最终承担生产风险的一方决定；质量权威仍是 CI 门禁，owner 拍板仍是红线兜底。

> 历史注记：2026-10-15 早先曾拍板候选 A（Dev 自行 merge + 红线上报），同日改为本政策。

## 五、部署机制（Prod Agent，蓝绿）

入口即 §四：Prod Agent 审查通过后直接进入**候选部署**（验证通过才 merge，merge 与部署同角色贯通，无交接间隙）。

前置一次性（已就位）：绿 systemd unit 声明 `Environment=DAILYCHECK_DB_DIR=/var/lib/dailycheck-green/db`（Issue #28）。

**部署单飞（2026-10-16 起，机制细节见 `docs/ARCHITECTURE.md` §3.8）**：部署是 40-60 分钟长流程，而 webhook 事件天生并发——同一时刻全系统至多一个部署会话在动蓝绿设施；「欠账」由状态事实（`deployed.sha` vs `origin/main`）判定，不靠会话排队。

```
deploy_state.py acquire --pr <N> --sha <PR head sha>
             ├─ rc 10（他人在部署）→ 不排队、直接结束，欠账留待下一次 catchup（兜底轮询 ≤30min 保底）
             └─ rc 0 → 持锁进入部署（长步骤间 deploy_state.py heartbeat --token <t> --phase <阶段>）
fetch refs/pull/<N>/head → 校验 sha 包含 origin/main → checkout --detach <sha> → up.sh <pr> <sha>
             ├─ 守卫：绿 unit 必须声明 DAILYCHECK_DB_DIR 且 = 拷贝目标，否则拒启
             ├─ cp -aL 真库 ${APP_DIR}/db → /var/lib/dailycheck-green/db（独立副本）
             └─ 起绿 :8091/:5101 + 健康检查（绿 unit 声明 DRY_RUN=true = 验证期写隔离）
         → verify.sh <pr> 八项（任一 ❌ 即 FAIL 并自动写报告）
             1 pytest 零新增门禁   2 sim_user 五路径   3 ab 性能比对
             4 回滚演练            5 资源与依赖        6 dry_run 隔离证据
             7 DB 隔离证据（environ + 非软链 + inode 相异）
             8 nginx 入口终态探活（主站 + MCP，非 502）
         → 【绿转正】承接流量前必做（Issue #41）：关 DRY_RUN + DAILYCHECK_DB_DIR 指回
             ${APP_DIR}/db + restart 绿两个 unit + 复验；标准 5 步见 scripts/blue_green/README.md
             ⚠️ 未转正时 switch.sh 的 DRY_RUN 守卫会拒跑（exit 3）——这是预期拦截，不是故障
         → switch.sh 灰度 5% → 50% → 100%（每档观察；100%/0% 边界自动做落点核验）
         → 观察窗 ≥10min → 收官（绿=生产，蓝 standby）
         → FF-only merge PR（deploy_merge.py；锁内冻结 main）
             merge 后 fetch origin main + checkout -B main origin/main，保证本地树 == 远端 main
             失败：switch.sh blue 100 → 工作树恢复 origin/main → exit 26（PR 保持 open）
         → deploy_state.py release --token <t> --result success（推进 deployed 锚点）
             失败：release --result failed —— 同 sha 不自动重试（防坏 main 死循环），留 last_attempt 供追查
```

- **守卫语义**（up/verify/switch 拒并发时 exit 10）：up / verify 需活跃锁且 PR 匹配；switch 仅 `green` 目标需活跃锁（`blue` 方向豁免——回滚演练内嵌 switch 不被锁阻塞）；**rollback / down 免锁校验**（应急止损永不被部署锁阻塞）。`--dry-run` 全部跳过；人工受控操作可设 `DAILYCHECK_DEPLOY_UNLOCKED=1`（警告放行）
- **激活顺序（一次性）**：先更新 Prod Agent skill（会话侧协议）→ 再 `init` 建状态目录激活。反序会出现「机制已激活、旧 skill 不握锁 → up.sh 拒跑」
- **任何异常 → `rollback.sh` 秒回蓝**（Issue #28 修复生效后为代码+数据双回；此前仅回代码）
- 铁律四条 + DB 隔离铁律见 `docs/ARCHITECTURE.md` §3.4
- 已知注意：蓝 standby 进程内存里是旧代码，**重新启用蓝前必须 restart**

### GitHub native-only Prod 生命周期（Issue164，本地 seam；未激活）

**2026-11-10 Owner 定向 ordinary cutover（D1，本地实现，默认 OFF）**：保留现有 Prod
Agent、部署主机和 §五 deploy-first exact-H FF-only 蓝绿链路；本次窄范围 hosting
切换不以以下未激活 native/controller 架构为必经路径。仅显式 ordinary lane 可通过
既有定时 poll 发现 PR/H/B；Prod 独立审查、两必需 CI、完整 verify 与真实 ≥10min
观察、原 attempt fence 均保留。最终仅允许 Prod 专用显式 finalization 凭据执行单次
GraphQL `updateRefs` 原子 batch：main B→H、同仓 head ref H→H，均 expected OID /
force=false；还须实际重读 PR merged 且 main=H 才收成功。间接 merged 不替代 review /
checks；当前 GitHub main 保护未证明激活，生产切换前须独立对齐保护，不得绕过。
这只授权本地 adapter 与本段说明，不授权安装凭据、修改保护、push、merge 或部署。

Prod 独立 review → 在既有 attempt fence 下 checkout G → 全部现有蓝绿 verification /
切流量 / ≥10min 真实观察 → durable verified journal → 最终 live 重读 → durable grant_intent
→ dedicated isolated Prod check transport → GitHub 异步 merge → exact-G 对账 → 原子锚点/outcome
→ typed public v2 H/B/G 报告 → Prod 对账源 Issue。不能为拿 receipt 提前调用普通 release。

Owner 已接受：完整独立验证后授权 exact immutable G；同 G 出队再入队可复用该许可，grant
发出后不能保证撤回已接受的异步 merge。放行前 H/B/成员/binding/policy/review/CI attempt
或出队变化均使旧 proof 无效。此决策不启用 App/保护/生产；没有 signing key、环境 boolean
或 caller success JSON 激活入口，具体新增传输只接受 owned loopback fixture。

HTTP child、异步 merge、main/runtime/anchor reconciliation 全程持有既有 OS fence；process
死亡后仍有 durable unresolved-grant barrier。TTL、clear-lock、state 删除/损坏或 ordinary
cleanup 不能放行下一生产。恢复取同一 fence，只读事实，不重复 deploy/grant、不 refresh
绿色；未知/incident 保留 reconcile_required，交 Prod/operator。仅 exact merged G、main G、
runtime G、完整 observation/checks 写一次成功 anchor/outcome；publication 不证明 merge。
应急 rollback 仍可用，但不撤销已接受 grant。Gitea 继续原 FF helper 与现有部署/报告行为。
Coordinator 继续按 delivered 契约跟踪，不 merge、不部署、不关闭 Issue。

## 六、应急

| 场景 | 动作 |
|------|------|
| 绿不健康 / verify 红 | 停，`rollback.sh` 回蓝，取证（journalctl + 报告自动落盘）后报 owner + Dev |
| verify 报告未生成 | Issue #24 Bug B 已修（trap EXIT 兜底）；若再现 = 框架回归，立即报 Dev |
| nginx config 异常 | 备份只许在 `/var/lib/dailycheck-blue-green-backups/`；`sites-enabled/` 内出现 .bak 立即 mv 走并 `nginx -t` |
