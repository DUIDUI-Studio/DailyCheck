# Deploy-first 设计：先部署验证、后合并

> 日期：2026-10-25
> 状态：待 Eric 评审（评审通过后才进入实施）
> 范围：DailyCheck 生产发布顺序（Prod Agent + 蓝绿脚本 + 工作流文档）
> 前置事实：`.github/workflows/ci.yml` 的 `deploy` job 已于 `f293746` 移除，CI 只做门禁与通知。

## 1. 问题

现行顺序是 **merge → 部署**（`workflow.md` §四/§五、`session-bootstrap.md`、`deploy_pipeline.sh` 均按此假定）。它有三个语义问题：

1. **issue 关得太早**。PR body 的 `Closes #N` 在 merge 时触发，生产验证还没跑；「关单」不等于「上线成功」。
2. **验证失败时 main 已脏**。蓝绿可以把流量切回蓝，但 main 里已经包含未通过生产验证的代码；要恢复得 revert，期间其他 PR 还基于脏 main。
3. **autofix 无处落脚**。`deploy_report` 的 `next=autofix` 语义是「在 PR 范围改代码」；merge 之后 PR 已关闭，只能另开 fix PR，闭环多一跳。

## 2. 目标与非目标

**目标**

- main 只包含已通过生产验证的代码。
- 验证失败时 PR 保持 open，autofix 直接作用于原 PR。
- 保持 `deployed.sha == origin/main` 不变量，`deploy_state.catchup` 语义不变。
- 不引入 release 分支、不要求直接 push main、不改分支保护规则。

**非目标**

- 不做多 PR 批量发布（仍一次一个 PR）。
- 不改蓝绿八项验证内容。
- 不改 CI 门禁内容（不新增 CI check；前置校验放在部署入口）。

## 3. 方案

### 3.1 新时序

```text
CI 全绿（PR head sha）
  → Prod Agent 独立审查（未合并）
  → deploy_pipeline.sh <PR> <sha> --candidate --merge-pr
       acquire 部署锁（= main 冻结）
       fetch refs/pull/<PR>/head → 校验 sha / PR open / 未 draft / 包含当前 main
       checkout --detach <sha>
       up → verify 八项 → promote → switch 5/50/100 → 观察窗 30min
       FF-only merge PR（POST /pulls/<N>/merge {"do":"fast-forward-only"}）
       git checkout main（此时 main == 已验证 sha）
       release success（deployed.sha = sha）
  → Gitea 按 Closes #N 自动关 issue（发生在验证通过之后）
```

失败路径（任一步红）：`switch.sh blue 100`（若已切流）→ `git checkout main` 恢复工作树 → `release failed` → PR 保持 open，`deploy_report` 的 autofix 直接在原 PR 上工作。

### 3.2 三条硬约束

| # | 约束 | 执行点 |
|---|------|--------|
| C1 | PR head 必须包含当前 `origin/main`（`git merge-base --is-ancestor origin/main <sha>`） | `deploy_pipeline.sh` 前置校验；不满足 exit 25，不碰蓝绿 |
| C2 | 验证期间 main 不允许任何 merge（包括纯文档 PR） | 部署锁视为「main 冻结」；Prod Agent merge 前先跑 `deploy_state.py status`，有活跃锁则等待/退出；FF 失败是最后一道网 |
| C3 | 合并必须 fast-forward-only | `deploy_merge.py` 固定 `{"do":"fast-forward-only"}`；main 动了就 exit 26 并回滚 |

C1 不放进 CI：放 CI 会要求每个 PR 在 CI 前保持最新，制造频繁 rebase 噪音；放部署入口只影响「真正要上生产」的那一刻。

### 3.3 不变量

- `deployed.sha == origin/main`：FF 合并后成立；`catchup` 无需改。
- 验证的代码 == 合并的代码 == 上线的代码：同一个 sha，无 merge commit 差异。
- 蓝绿设施只在部署锁内被动过；失败时 main 与蓝的磁盘代码都是已知好版本。

## 4. 具体改动

### 4.1 `scripts/blue_green/deploy_pipeline.sh`

新增两个显式开关（默认关闭，保持旧行为可回退）：

- `--candidate`：启用候选模式。前置步骤在 acquire 之后、幂等检查之前：
  1. `git -C "$REPO_ROOT" fetch origin refs/pull/<PR>/head`
  2. 校验 `FETCH_HEAD == SHA`，否则 exit 25
  3. `git fetch origin main` + `git merge-base --is-ancestor origin/main "$SHA"`，否则 exit 25
  4. 调 Gitea API 确认 PR `state=open`、`head.sha == SHA`、`base.ref == main`、非 draft，否则 exit 25
  5. 记录 `ORIG_REF`，`git checkout --detach "$SHA"`，置 `TREE_MOVED=1`
- `--merge-pr`：观察窗通过后、`release success` 之前调用 `deploy_merge.py merge --pr "$PR" --sha "$SHA"`；失败 → `switch.sh blue 100` → `FAIL_STAGE="merge"` → exit 26。

cleanup 扩展：

- `TREE_MOVED=1` 且 `rc != 0` → `git checkout main`（恢复已知好代码），失败只 WARN。
- 成功路径在 merge 后 `git checkout main`，并断言 `HEAD == SHA`（FF 后 main 就是该 sha）。
- `--dry-run` 下只做 C1/PR 校验，不 checkout、不 merge。

现有 `deploy_state` acquire/release、心跳、F1 漂移守卫、观察窗逻辑不动。

### 4.2 新增 `scripts/blue_green/deploy_merge.py`

stdlib-only 的 FF 合并助手，复用 `deploy_report.py` 的 Gitea 配置口径：

- env：`FEEDBACK_GITEA_API`（默认 `http://172.28.129.216:3000`）、`FEEDBACK_GITEA_REPO`（默认 `eric/dailycheck`）、`DAILYCHECK_GITEA_TOKEN_FILE`（默认 `/etc/dailycheck/gitea-token`）。
- 流程：`GET /pulls/<N>` 校验状态与 head sha → `POST /pulls/<N>/merge {"do":"fast-forward-only"}` → 再 `GET /pulls/<N>` 确认 `merged=true` 且 `merge_commit_sha` 存在。
- 退出码：0 成功 / 30 非快进（main 动了）/ 31 前置不匹配（PR 关闭、sha 不符、draft）/ 32 API 或 token 失败 / 64 参数错。helper 的退出码不复用 `deploy_state.py` 的 10-12，避免归因混淆。
- 输出单行 JSON（含 `pr` / `sha` / `merged_sha` / `reason`），便于 `deploy_report` 事件流记录。

### 4.3 归因与回写

`deploy_triage_pure.py` 新增：

- `rc 25`（candidate 前置不满足）→ `KIND_ENV` / `SCOPE_OUT` / `NEXT_ENVFIX`，理由「候选分支落后 main 或 sha 不符，先 rebase 再部署」。
- `rc 26`（FF 合并失败）→ `KIND_ENV` / `SCOPE_OUT` / `NEXT_ENVFIX`，理由「main 在验证期间被改动，已回滚，重跑」。
- `stage="candidate"` / `stage="merge"` 写入事件流与 PR 评论。

`deploy_report.py` 无需结构改动，沿用 `triage()` 的输出。

### 4.4 文档同步（同一实施 PR）

- `docs/agent-framework/workflow.md` §四/§五：merge 改为验证后的 FF；红线 6 路径，实施 PR 必须带 `Owner-Authorization:` 行。
- `docs/agent-framework/session-bootstrap.md`：Prod Agent 常驻指令改为 deploy-first。
- `docs/ARCHITECTURE.md` §3.4/§3.8：补 deploy-first 时序、C1-C3、新退出码。
- `scripts/blue_green/README.md`：命令示例与失败路径。
- `deploy/scripts/dc-notify-prod-agent.sh` 注释：`dailycheck-ci` 的含义从「触发 merge 判定」改为「触发候选部署判定」。

### 4.5 Prod Agent skill（VPS 侧，仓库外）

发布顺序：

1. 先上脚本 + 测试（默认关闭，旧流程不受影响）。
2. 由一次普通 merge-first 部署把它们带到 VPS。
3. 再改 Prod Agent skill：CI 全绿 → 审查 → 调 `deploy_pipeline.sh <PR> <sha> --candidate --merge-pr`；不再单独调 merge API。
4. 最后改 `workflow.md` 等文档（与 skill 同批生效）。

回退：skill 改回旧调用、脚本开关去掉即可；`deployed.sha` 与 main 仍一致。

## 5. 测试计划

- `tests/test_deploy_pipeline.py` 扩：
  - `--candidate`：sha 不符 / 落后 main / PR draft / PR closed → exit 25，且不调 up.sh。
  - checkout 发生在 up 之前；`--dry-run` 不 checkout。
  - `--merge-pr`：调用顺序为 `observe → merge → release`；merge 失败 → 调 `switch.sh blue 100`、`release failed`、exit 26。
  - cleanup 在失败路径恢复 `git checkout main`；成功路径断言 `HEAD == SHA`。
- 新增 `tests/test_deploy_merge.py`：FF 成功 / 非快进 / PR 关闭 / sha 不符 / token 缺失 / API 500 / 响应非 JSON / 脱敏，全部用假 HTTP server 或 monkeypatch。
- `tests/test_deploy_report.py`：新增 rc 25/26 的归因与 action 断言。
- 关键断言做变异验证：去掉 C1、把 merge 挪到 observe 之前、FF 改成 merge、失败不恢复工作树，各自打红对应用例。

## 6. 风险与未决项

| 风险 | 缓解 |
|------|------|
| 验证 40–60 分钟内 main 被 merge（人为绕过冻结） | FF 失败 → 自动回滚蓝 + exit 26；PR 保持 open；不产生脏 main |
| 绿实例仍与蓝共享 `/root/DailyCheck` 工作树 | 本方案不改变这一点；失败时 `checkout main` 恢复已知好代码。彻底的 pinned worktree 是独立议题（F1 残余缺口） |
| Gitea 版本不支持 `fast-forward-only` | 实施前先探测；不支持则**不启用 deploy-first**（继续 merge-first），不做「merge + 校验树」的半吊子降级——那会破坏 `deployed.sha == main` 锚点 |
| PR 频繁落后 main | C1 只在部署入口拦，不影响日常 CI；PR 合并前必须 rebase，成本落在 Prod Agent 判定时 |
| 纯文档 PR 跳过部署 | 仍需在部署锁空闲时 merge；Prod Agent skill 明确「先查 `deploy_state.py status`」 |

**未决项（实施前需确认）**

1. ~~`FAST-FORWARD-ONLY` 在当前 Gitea（hub01）版本是否可用~~ —— **已确认可用**：hub01 Gitea `28.1.0`，仓库设置 `allow_fast_forward_only_merge=true`（2026-10-25 只读 API 核对）。
2. Prod Agent 的 token 是否有 `write:repository`（合并所需）；没有则补 scope。
3. 失败回滚时是否保留绿实例（`down.sh` 还是留待下次 up 复用）。

## 7. 验收标准

- 一个真实 PR 走完：CI 绿 → 部署候选 → verify 八项 → 切流 → FF 合并 → issue 自动关闭；`deployed.sha == origin/main`。
- 人为制造 C1 失败（PR 落后 main）：部署在 up 之前 exit 25，蓝绿零改动，PR 仍 open。
- 人为制造 FF 失败（验证期间 merge 另一个 PR）：自动切回蓝，exit 26，main 不含候选代码。
- 全量 pytest 与 `ruff check` 零新增失败。
