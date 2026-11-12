# Prod Agent 自检并修正 GitHub 流程配置

将本文完整转交给 VPS 上的 DailyCheck Prod Agent。本文是 Eric 要求的技能、配置与入口自检任务，不是部署命令。你要实际检查并修正本任务范围内的遗漏，不能只给建议或复述文件内容。

## 先确认任务与当前状态

你是 DailyCheck 现有 VPS Prod Agent。先保留自己的真实 Session、任务和已启动进程归属。文档中的旧执行记录只用于核对，不授予旧 Dispatch 或已消耗 release 的权限。发现原操作仍活跃或归属不明时，保留锁与证据，先协调，禁止重派同一部署。

本文件发布在独立文档分支。文档提交不是应用部署候选。应用候选仍以 PR13 的实际 head 及其对应审批、CI 为准。不要把运行中的代码 checkout 到本文所在的文档分支。

2026-11-12 的已核验基线如下。开始时重新读取实际配置，不能只相信本文预期值。

- GitHub 仓库：`DUIDUI-Studio/DailyCheck`，repository ID `1448986421`。
- PR：`13`，最后核验仍 open、未合并。源任务 Issue159 未完成，Issue164 保持 parked。
- 应用候选 H25：`24bad15501bcf5ee3abab20ed3ada510fca6947f`。
- 候选 tree：`ce9e6107b6bc0b6a60552e739beefaf504189903`。
- 当时 main：`207bf92976e57e64dc272bcab562cb7831dcf438`。
- H25 CI run：`43180699083`，attempt `1`，两项必需检查成功。已有14项基线失败没有扩张。
- 对应开发源：`128408407baa98f195eb9dbd837c8bc31ccb16a2`。
- CI 已用25次，上限30。普通部署调用累计7次；本文不授权第8次调用。
- GEN7 是真实失败结果，失败报告已恢复为 PR13 评论 `6813612971`。不得改写成成功或清除失败记录。
- 最后部署版本仍为 `152d3dd61a60052bbabc11fef54d45b8c9da371a`。这些旧状态不代替开始时的真实读回。

## 核对已替换的入口

使用受支持的 job、skill 和 Session 接口，不直接查询或修改会话数据库。对现有 job `02ee9e01cb13`，本次配置写入已正向退出0并完整回读。预期字段为：

- `script=None`，不再先运行旧 checkout 的发现脚本。
- `no_agent=False`，技能绑定为 `dailycheck-prod-agent`。
- 当前 prompt SHA256 为 `2aa17f0aedb9cd5da3ac80f7eda9cb93bdf3618b0aac2aadeb72541b2fe23bf9`。
- `enabled=False`、`state=paused`、`next_run_at=None`。
- schedule 为 `kind=once`、`run_at=None`，没有周期轮询资格。
- repeat 的真实 `completed` 计数保留，`times=None`，不会因有限重复次数删除这个永久 job。

原来的每30分钟 cron、仅发现旧提示词、`script=dailycheck_pr_poll.py` 与 `no_agent=True` 已从这个 job 替换。不要恢复这些旧设置。保留同一 job ID，不另建重复任务。其他6个 jobs、模型、provider、pool、最终投递目的地在本次更新中均保持不变。

当前配置文件包位于：

`/root/.hermes/scripts/.dailycheck-H25-paused-config-01/payload/`

先读其中的 `CURRENT-GITHUB-HANDOFF.md` 和 `MANIFEST.json`，再核对下列实际内容：

1. `entry.py` 是当前入口草稿，其 SHA256 为 `76c0180012535c34504aa8418c05a3706b494616f2448d7f5e2052bc840889d0`。
2. `phase-python.py` 的 SHA256 为 `84f2aa857459918f378e6fb6e7fb47c9d966ddd264de05580cca4a3b37d4a8f1`。
3. 当前工作流是该包内的 `docs/agent-framework/workflow.md`，SHA256 为 `3175830ac3891dcf12968bbb1d91eb57768e45855662a4708d4044dbfbe5b2f0`。安装 checkout 中的旧 B207 文档不是新流程依据。
4. 现有技能通过 `skill_view` 读出的正文为4198字节，SHA256 为 `34ba362f14116bafd7c9b3f3ce078130c0693c51cee5c1f0b96fc4b6c79c12ed`。文件可读不等于本 Session 已加载技能。要用实际加载记录和工具行为核对。
5. 核对包内七个 helper 的清单、大小和哈希。两个空 `__init__.py` 是独立包加载所需的标记，不要冒称它们是原仓库已有的 blob。

原始配置与写入后的完整 private 备份在同级 `receipts/`。这些文件可能包含私密元数据，只在 VPS 受控环境内使用，不上传、不贴进回复。已冻结的包与历史回执要保留。修正新版本时新建可追踪版本，再通过支持接口更新引用。

## 检查技能、执行环境与仓库指向

实际读取本 Session 可用技能、工具及执行环境，确认以下事项，逐项给证据：

- Git origin、API base、repo owner/name、repository ID 和角色令牌用途均指向当前 GitHub 仓库。Gitea 只保留既有回滚溯源用途，不能继续作为发现、审批、部署报告或默认回退入口。
- job 没有重新指向旧 poll、旧 report、旧 workflow 或旧发现提示词。明确区分已更新的 job 配置与仍位于 B207 的安装 checkout。
- 你知道实际工作目录、可用 terminal/process 工具、所选模型及 provider。配置中写了某工具或模型，不等于本 Session 真正可用；无法读取的字段标记 UNKNOWN，不猜。
- 仓库操作使用现有 `/root/.hermes/scripts/dailycheck_pr_poll.py --exec` 与规范 Python 环境。不要假设一次子进程刷新会永久修改后续 shell 的环境。
- 检查规范入口实际继承了代理、角色分离与凭证文件读取方式。不得在回复、命令行、trace 或公共仓库输出令牌、私钥、订阅地址、私密节点地址或完整认证头。

## 分别验证 Git 与 GitHub API

Git 地址是 `https://github.com/DUIDUI-Studio/DailyCheck.git`。API base 是 `https://api.github.com`，仓库前缀是 `/repos/DUIDUI-Studio/DailyCheck`。现有 VPS 代理入口是 `http://127.0.0.1:17891`。

此前一次直接 SSH 测试在空临时仓库拉取 PR13，Git 用时4.836607秒、原生退出0，提交与 tree 匹配 H25。该次使用现有代理、HTTP/1.1、Git protocol0、`--depth=1`，没有 App 或角色刷新。它只证明当时的 Git 代码传输成功，不代替认证 API 验证，也不证明网络永久稳定。

使用现有规范客户端与凭证流程，做有界的真实 API 读取，至少覆盖当前 PR 信息、对应必需 CI 检查与实际使用的 compare 元数据请求。需要的凭证更新只能走已批准的原有角色流程，不另发明登录、导出或降级路径。记录请求类别、原生退出码、HTTP 状态、CONNECT、目标 TLS 验证、时序及响应大小。只保留非敏感摘要，不输出响应正文或认证头。

compare 已改为固定 `?per_page=1&page=2`，避免下载不需要的文件补丁，同时保留原有 ahead、base、merge-base 判据。不要改回大响应，也不要延长15秒请求限制、20秒父等待或90秒 Git 限制来掩盖故障。HTTP200、TLS成功与响应完整是不同事实。一次读请求失败后先记录具体阶段，不能盲目重试、重新部署或关掉证书验证。

普通 HTTP 代理使用 CONNECT 通道承载 HTTPS。不能仅凭使用代理就认定握手信息丢失。要区分代理连接、目标 TLS、HTTP 状态与响应下载阶段。保留现有网络安全设置，不扩大任何证书例外。

## 修正已知遗漏并验证运行机制

本任务允许你自行修正本范围内可逆的技能、配置、入口引用、明确工作目录和必要的小型入口适配。先保存原状态，再实际修改和读回。无需反复请求普通目录、读取或局部修正批准。涉及应用源码、共享调度器或全局 runtime 修改时，沿用现有写者、独立审查和测试流程，不在生产上直接试改。

以下问题尚未完成，不能因为新配置落盘就勾选通过：

1. 入口和 phase wrapper 新增了无条件 Python optimization 拒绝，但目前只有语法与结构核对。用私有小型控制证明优化模式不能跳过 wrong-head 或 continuation 校验，原有正常 exec、PID、PGID、FD、stdin 和退出码语义保持。
2. 现有 once job 在真正运行完成后才消费，崩溃重启可能再次启动。证明实际入口在接受业务执行前持久消费同一有限许可，或实现并审查最小修正。不要靠启动后再 pause 的提示词承诺代替防重机制。
3. Session 结束或调度线程超时不证明部署进程退出。核对实际 native process registry、wait/reap、deadline 与完成事件接收者，证明同一进程由同一负责人保留到退出。不要把 launch 返回0或模型自报结果当原生退出。
4. 启动器的5400秒等待超时不能遗留失去归属的部署子进程。证明已有完整子进程收尾机制，或通过小型可控子进程测试并审查最小修正。UNKNOWN 保留锁与证据，不产生重试许可。
5. 完成前的 merge/report 需要有效角色凭证。证明晚阶段经过规范刷新并由新客户端读取，而非只看文件年龄、旧 token 或一条配对日志。

Owner 不要求持续监控项目。不要加定时项目扫描、Session 查询、通知轮询或假进度心跳。现有原生进程 wait、特定命令 deadline、部署锁的安全心跳和真实600秒观察窗各有用途，不能因其中出现时间间隔就删除。主动通知专项已被 Owner 暂停，本任务不实现或修复它。

## 保持部署边界并回报实际结果

完成自检与本范围修正后，保留 job 暂停，报告是否满足下一次真实启动条件。本文没有消费或发放 ordinary8，不授权为了验收制造部署、审批、评论或业务状态。后续应用交付仍按现有有限 release、独立当前 head 审查、完整验证与真实观察流程执行。不要凭本文直接 trigger job。

保留 PR13、Issue159、Issue164、GEN7 失败报告、历史计数与回滚版本。不得扩张 known14、绕过锁、force push、修改分支保护、扩大权限或证书例外。权限拒绝要原样报告，不绕过。

最终回复给 Eric：

- 读取了哪些真实技能、配置、工作流和 Session 信息，哪些仍 UNKNOWN。
- 找到并实际改了哪些遗漏，给出前后差异、提交或版本、完整回读结果和回滚位置。
- Git 与实际 API 请求分别是否成功，失败在哪个阶段。给出安全的状态码、时序和大小证据。
- 单次启动防重、长任务退出归属、deadline、晚阶段凭证是否已验证，不能用配置截图替代。
- PR 是否合并、Issue 是否关闭、CI 与部署是否成功。本任务结束时若未部署，就明确写未部署。

临时文件只放本任务专属私有目录，登记精确路径并在所属进程正向退出后清理。禁止按通配符、mtime 或不确定归属清理系统临时目录。
