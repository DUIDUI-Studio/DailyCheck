# GitHub ordinary 自动接入接口（source preparation）

本接口修复 receiver → inbox/watcher → reducer → sender → 既有 Hermes 订阅，以及既有 cron
发现路径。它不授予部署、merge、Issue closure、review 提交或凭据刷新权限；发布仍执行 workflow.md
已有 Prod 独立审查、exact-H CI、candidate/deploy-first、完整验证、真实观察、保护与原生 fence。
受信 `scripts/hosting.py` 与 B 保持逐字一致，原 guard 仍拒绝 GitHub。只有本次新接入 consumer
使用 `github_intake.require_work`；不改 producer manifest 或 workflow pins，也不扩大其他原 caller。

## 配置与阶段

未设配置时仍为 Gitea；GitHub 默认 shadow。ordinary live admission 另外需要：

- `DAILYCHECK_HOSTING_PROVIDER=github`、`DAILYCHECK_GITHUB_LANE=ordinary`。
- `DAILYCHECK_GITHUB_INTAKE_ENABLED=1`，缺省关闭。
- `DAILYCHECK_GITHUB_INTAKE_POLICY_FILE` 指向受信服务 owner 所有、0600 的受审策略文件。
- `DAILYCHECK_GITHUB_INTAKE_KEY_FILE` 指向同 owner 所有、0600、32–4096 字节的独立本地签名 key。
  此 key 与 GitHub webhook HMAC、Hermes route secret、provider credentials 分离，不经 argv/payload。

策略示例为 [`deploy/github-ordinary-intake.policy.example.json`](../../deploy/github-ordinary-intake.policy.example.json)。
它不可直接激活：`since_ms=0` 是无效 epoch，目标与 reviewer pins 均为空。键必须完整且无未知项：

| 字段 | 意义 |
|---|---|
| version / repository_id / repository | 固定 1 / 1448986421 / DUIDUI-Studio/DailyCheck |
| mode | quarantined 或 automatic |
| since_ms | 本次受审激活起点，整数 UTC 毫秒；更新策略 hash/epoch 使旧信封失效 |
| review_targets | 最多 16 项 `{pr:正整数,head:40位小写H}`；初期为空 |
| automatic_pr_range | quarantine 必须 null；automatic 可设 `{min,max}`，跨度最多 10000 |
| excluded_open_prs | 最多 100 个 PR；初期保留 15、17、18 |
| review_actor_ids | 最多 16 个受信专用 reviewer numeric IDs；quarantine 初期为空；automatic 必须非空，不靠 login 字符串 |

automatic 是受审的长期稳定规则：范围内的未来 PR/current H 可自动进入独立 review，仍检查
same repository、open/non-draft、当前 H/B、可信 workflow/run/App/job/check 两项成功、相关保护、
failed-H、native unknown/live-owner。它不是 release permit，也不改变现有发布权限。
初期 root exact-target quarantine 与暂停 backlog 隔离不是逐 PR 人工审批的永久政策。
本地合成 fixture 也支持无 policy 文件的 `DAILYCHECK_GITHUB_INTAKE_SINCE_MS` 和
`DAILYCHECK_GITHUB_INTAKE_TARGETS`（严格 JSON exact PR/H 数组）；该形式只有 quarantine，
不开放 automatic，也无默认 reviewer pin。

## 固定消费接口

receiver 仍先验证 GitHub HMAC，查询固定仓当前 PR；raw event 只是 hint，结论不作为 CI 权威。
ordinary 信封覆盖完整 typed body，保留原 source delivery ID；新 envelope 缺失、修改、旧 epoch、
shadow、错仓或未准入目标在消费者拒绝。PR 文本不会进入签名工作指令。

| 路由 | header event | dispatch_phase | 新增关联字段 |
|---|---|---|---|
| wdg-ci | check_suite | prod_review | trigger_id, admission_id, policy_sha256, PR/H/B, observed_ci_run_id/attempt, dispatch_claimed_at_ms |
| dailycheck-deploy | deploy_result | result_reconcile | trigger_id, admission_id, policy_sha256, outcome_id, generation, public_receipt, source_binding_sha256, publication_state |

`MERGE_READY` 保留为兼容 trigger 名；approval 在 Prod 被唤醒后产生。发送前仍实时回读 H/CI，
sender 必须与持久 claimed row 完整匹配并原子消耗一次 send intent；payload 本身不是重复发送能力。
Hermes 202 仅为 transport accepted，队列记录 awaiting_agent_receipt。丢响应/进程 lease 过期为
delivery_unknown，不自动重发；同 PR 新 H 在旧 review 未确认时也等待。
尚未写 send intent 的已知未发送失败只在最多三次 claim 预算内重新核验，不消耗新的发布许可。
排队期间 CI 暂态或全局原生锁忙保持 pending；实际 claim 使用重新查询的 CI 版本。

review 完成只从可信 GitHub 正式 review 读取：精确仓/PR/full dispatched H、正整数 review ID、
显式 pinned numeric actor 且 distinct PR author、APPROVED/CHANGES_REQUESTED、submitted_at 不早于
实际 claim，关联签名 trigger/admission 与未变的原 CI run/attempt。COMMENTED、DISMISSED、PENDING、
旧 H/旧时间/错 actor/畸形/不可用均 UNKNOWN。正式 BLOCKER 要提交 CHANGES_REQUESTED 并保留私有
原因；之后新 H 可以重新审查，旧 H 不触发 candidate。APPROVED 也不触发重复发布。

reporter 只从现有 durable outcome/publication authority/source binding 派生新 ordinary feedback，
禁止历史 outcome 早于 epoch；generation/receipt/source binding 必须匹配当前 durable 事实。
public 仍使用 typed renderer；日志/原因在原私有诊断。publication unavailable 为 unconfirmed，
原 poll `_report_block` 与 cron 输出可对账的告警，不清 failed-H、不重复 deploy/grant。
Coordinator 仍需读实际 forge typed report、checks/reviews 与 private source；聊天/202 都不证明交付。

## Cron 与远端 adoption 门禁

受审固定前门为 `scripts/prod_agent/github_dispatch.py`。`--policy-preflight` 只读策略/key：
无 API、无 SQLite/claim、无 process/refresh；准入返回 0，OFF/空/无效返回 78，输出 typed JSON。
未来 root wrapper 必须先调用此端口，获准后才按单独授权的固定机制 refresh，再执行同一前门；
前门会再次核对策略、实时事实并通过既有 adapter/reducer claim/发送，不绕开持久去重。
现有 `pr_poll.py` ordinary CLI 复用此门；OFF 时只保留 publication 对账观察，不生成部署工作。

有目标的实际 dispatch 另需既有私有 audit 目录：固定 root frontdoor 将当前 invocation 的
0700 observation 目录作为 `DAILYCHECK_GITHUB_DISPATCH_AUDIT_DIR` 传给子进程；目录缺失/
不安全在 API/child 前失败，OFF/空准入和纯 preflight 不写诊断。每个已退出 adapter 在解析或
非零异常前保留0600 JSON证据，stdout/stderr以base64保存每流最多64KiB，另记完整长度/hash。
外层状态只含phase/native rc、安全分类与证据basename/hash；raw和child reason留私有目录，
不得粘贴至公共报告/提示。记录失败也停止，不补发/重试。新SOURCE必须与传递此目录的固定
frontdoor一起审查采用；原有stdout/stderr/native wait回执仍保留，历史已丢stderr无法重建。

远端安装/配置 owner 必须另行审查：固定 source/hash/import closure 与 credential 顺序，receiver
及 watcher 的 provider/repo/env 与 inbox 路径，旧 Gitea router/prompt 改为 typed ordinary phase，
保持 check_suite→wdg-ci 与 deploy_result→dailycheck-deploy，cron `no_agent=true`。调查实测 job
02ee9e01cb13 是 hourly `0 * * * *`，不是文档的 30 分钟；未来 cadence 与 before-refresh wrapper
均由独立 owner 处理。本源码不修改 cron/unit/webhook/secret/protection 或实际 runtime，不证明其安装。

验证入口：`tests/test_github_ordinary_intake.py` 使用真实 receiver、shell adapter、reducer、sender、
SQLite、HTTP 与 OS flock，GitHub/Hermes/阶段设施都是 owned fake；无真实 provider wake、token mint、
SSH、生产/business data。现有 transport/shadow/publication/pipeline 测试继续覆盖兼容边界。
