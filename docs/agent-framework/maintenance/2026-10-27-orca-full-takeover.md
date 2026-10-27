# DailyCheck Orca 全自动接管记录 — 2026-10-27

> Orchestration Run：`run_9fe9fbd21e6a`
> 人读路由：[`../orca-model-orchestration.md`](../orca-model-orchestration.md)
> 机器路由：[`../orca-model-routing.json`](../orca-model-routing.json)
> 流程权威：[`../workflow.md`](../workflow.md)

本文是 2026-10-26 至 10-27 Orca 接管 DailyCheck 开发维护后的当前运维基线。历史过程文档如与本文冲突，以本文记录的 merged main、线上 anchor 和实时检查结果为准。

## 1. 最终状态

- merged main：`551005e701db8c38290d7c0b64b73e1c2ba065eb`
- 生产仓库：`/root/DailyCheck`，clean，`main@551005e`
- 生产流量：green `:8091` weight=100，blue `:8090` down
- deployed anchor：`551005e701db8c38290d7c0b64b73e1c2ba065eb`
- deploy catchup：`caught-up`
- 主站入口、webhook receiver、Hermes gateway：分别返回 `302`、`200`、`200`
- 部署锁：已释放

## 2. 已完成的 P0 与门禁

### Issue #59 / PR #60：移除 Gitea 不支持的 artifact v4

- Gitea Actions 不支持 `upload-artifact@v4` / `download-artifact@v4`，旧链路导致 reports job 下载失败后重跑全量 pytest，单次额外消耗约 18–32 分钟。
- 新链路只在 test job 抽取 `FAILED`/`ERROR`、collection/interrupted 标记和最后 5 行，base64 后通过 `needs.test.outputs` 交给 reports job。
- reports job 在 `/tmp/ci-pytest/pytest-output.txt` 重建摘要并设置 `PYTEST_REUSE_FILE`；缺失、损坏或超过 48 KiB 均 fail closed，不再静默回退全量 pytest。
- 红线 2（修改 CI workflow）已在 Issue/PR body 记录 Owner Authorization，并由 redline gate 机验。
- PR #60 已合并，Issue #59 已关闭。

### Issue #56 / PR #57：确定性 MERGE_READY 门禁

- `pull_request` 生命周期事件只更新 reducer state，不再产生 Hermes/LLM 调用。
- `check_suite` / `status` 中间态、失败、缺失、malformed、重复事件均静默；只有 Gitea 确认 PR open、head 未变、mergeable 且 `test` + `agent-framework-reports` 全部 success 后，才产生一次 `MERGE_READY`。
- 新 head 会失效旧 head 的 pending/claimed trigger 与 lease；旧 head 的迟到 complete 会被拒绝。
- `deploy_result -> dailycheck-deploy` 路由保留。
- reducer SQLite 增加可幂等迁移的 `merge_ready_at_ms`、`merge_ready_trigger_id`、`gate_reason`。
- dashboard 读取真实 reducer queue/provider/merge-ready 状态；不可用时显式显示 unavailable，不伪造 0。
- PR #57 已合并，Issue #56 已关闭。

### CI 实测

- PR #60 / run #107（id 120）：reports job `3m15s`，其中 generate 步骤 `7s`；日志确认复用 test job 摘要并跳过 pytest。
- PR #57 / run #111（id 124）：test job `27m56s`（pytest 步骤 `25m03s`，内部 `25m01s`，`14 failed / 2459 passed`，全部为 known baseline）；reports job `1m40s`，generate 步骤 `5s`。
- merged-main push run #112（id 125）：success。
- 全量 verify：8/8 通过，30 分钟观察窗零 BLOCKED。

## 3. Orca 角色与模型编排

机器配置是唯一可执行口径：

- Developer：Codex + `deepseek-flash` / medium。
- Reviewer：独立只读 session；主模型 `ccs-claude-qwen--MiniMax-M3` / high。
- Triage：Claude + `ccs-claude-qianwenai--qwen3.8-flash[1M]` / low。
- Planner / Escalation：Claude + `ccs-claude-qianwenai--qwen3.8-max[1M]`。
- Release：Prod Agent，不调用 LLM；由确定性脚本执行 merge/deploy。

基础设施失败（401/403、429/配额、超时、连接失败、5xx、provider unavailable、effective model 不一致）才允许 fallback。Reviewer BLOCKER、CI 失败、测试失败和未知残留 writer 不得靠换模型绕过。

实际演练：

- 一次 Tencent `HTTP 402` 配额耗尽被分类为 `infrastructure_failure`。
- 同一 Task 通过 `model_routing.py next` 从 Tencent DeepSeek V4.1 Flash 切换到 MiniMax-M3.1-Flash，未新建 Run/Task，并完成 release 复核。

## 4. 单写者与审查纪律

- 一个 worktree 同时只有一个代码写者。
- Reviewer 只读；发现 BLOCKER 后由 Developer 修复，再开新的 Reviewer session。
- PR merge 使用独立 release Task，重新查询 live head/base、required checks、PR body、redline 和 diff scope 后才执行。
- Gitea 凭据只通过 credential helper/本机 token 文件在进程内读取，不打印。
- 所有人工 CI 取消都必须有“旧 head 已失效”或“树与已验证 head 等价”的证据，不取消无关用户 PR。

## 5. 生产 ops-plane 基线

- `/root/DailyCheck` 与 merged main 同步，working tree clean。
- `/usr/local/bin/dc-notify-prod-agent` 与仓库 `deploy/scripts/dc-notify-prod-agent.sh` 哈希一致：
  `adfe7bf5668b04e78a9ba1e344efefbd75e589d719cd0e1f27864dcdc6047c63`。
- webhook routes 保持 `wdg-pr` / `wdg-ci` / `dailycheck-deploy`，route 配置文件权限 `0600`；本次 rollout 未改 secret。
- `dc-webhook-receiver.service`、`dc-inbox-watcher.service` active。
- 用户级 `dc-prod-agent-drain.timer`、`dc-prod-agent-provider-breaker.timer`、`hermes-webhook-session-reaper.timer`、`hermes-gateway.service` active。
- reducer DB：`/var/lib/dailycheck-prod-agent/event_reducer.sqlite3`，schema migration 已验证。
- 本次 rollout 报告：
  `/var/lib/dailycheck-rollbacks/issue56-20261027T004342+0800-LTLgmh/rollout-report.md`
- 本次 pipeline 日志：
  `/var/log/dailycheck/deploy-pipeline-20261027-004540.log`

## 6. 维护 automation

- 名称：`DailyCheck Weekday Maintenance Triage`
- ID：`9e8a8657-a0b0-464a-8f28-18acab125275`
- 时间：Asia/Shanghai 工作日 09:00
- 模型：`ccs-claude-qianwenai--qwen3.8-flash[1M]` / low
- 模式：现有 `重编排` workspace，每次 fresh session
- 手动首跑：automation run #1 completed，只创建一个新 session；完成后会话正常回收，无重复 session
- 状态：enabled

首跑识别出的当前工作队列：

1. Issue #47：BOM 删除索引错位可能静默改坏配方用量，优先 Developer。
2. PR #42：落后 main 且 checks 红，需返 Dev rebase 后重跑。
3. Owner Gates：#45（PR #43 redline 放行方式）、#39（verify 是否实跑 LLM）、#37（bot 配置台范围）。

## 7. 当前剩余边界

- 自动 CI artifact 传递与 MERGE_READY 门禁已上线，但 Issue/PR 的“业务优先级裁决”和 owner redline 仍由人决定。
- `wdg-pr` route 仍存在以保持配置兼容；新 notifier/sender 不允许把 `pull_request` 路由到 provider，因此不会重新唤醒 Hermes。
- Gitea 单个 2 vCPU runner host（capacity=2）仍是主要等待来源；下一阶段基于新 CI 数据评估 test-job 并行化、测试分片、缓存和依赖安装优化。

## 8. 已知约束

- `tests/known_failures.txt` 只许收缩，不许扩张。
- 不修改 branch protection / workflow 政策绕过门禁。
- 不 force-push，不删除 merged PR 分支，不重试同一失败 SHA 的 deploy。
- 生产变更必须有独立 release 复核、备份、回滚路径和可核验健康结果。
