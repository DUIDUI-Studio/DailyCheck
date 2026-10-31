# 运维模型效能评估：工作树事实与交付证据

Issue #102，2026-10-31。根据 Owner 批准的 `worktree-model-eval-design-v1.md`
落地；本文记录最终实现及明确边界。设计稿中的演示工作树、PR、模型和数字不进入运行产物。

## 产品层级与交互

`/ops` 第 6 节默认每棵工作树一行，展开到 Worker → Dispatch → 会话用量事件。
桌面固定首列、表格容器横滚；手机变为工作树摘要卡片，事件表仅在明细内滚动。
按钮、原生 details、输入及选择器均支持键盘。默认近 7 天，支持今日、近 30 天、
自定义日期和全生命周期，以及搜索、角色、实际模型、交付状态、新鲜度筛选；
支持最近活动、Token、Worker 数、累计运行时长、名称排序和分页。
汇总针对全部筛选结果，关联 PR 跨树再次去重，非当前页小计；日桶的最近活动使用 last_at。
角色 × 实际模型 × 复杂度仅为同一事实集的辅助分析，不作模型排名。
原始事件及完整 ID 保留在账本，页面仅内嵌执行/会话/实际模型/CST 日用量桶，
含源事件数与首末引用。所有 UI 窗口为完整 CST 日期，因此日桶总量精确且无截断；
运行区间不压缩。避免每 5 分钟 no-store 刷新重复传输所有原始 usage metadata。

主行展示工作树/分支/生命周期质量、PR 状态、Worker 与执行/显式重试、角色、
实际模型集合、Token 已知合计、Worker 累计运行、工作树历时、交付进展和覆盖。
明细保留完整身份/路径、请求与实际模型、effort、Task/Issue/PR、起止、
provider session、事件 ID、计数器重置、新鲜度与源警告。

## 采集与持久化

保持本机采集 → JSONL → 传输 → SQLite → 自包含静态 HTML → 平台超管 `/ops`
链路。没有新建常驻服务或客户端 API。`blueprints/ops.py` 的访问控制、no-store、
5 分钟浏览器刷新和现有 15 分钟报表刷新机制不变。

`model_eval.collect` 默认通过公开 Orca CLI 只读扩充现有 automation 派单，
分页读取本仓库的 worker-list，再按 dispatch 调 worker-show、按工作树缓存 worktree-show。
不读取 Orca 内部数据库。全局 transcript 发现后只保留本项目已记录路径或当前 git worktree 清单证明的 cwd，包含主目录 root 会话；陌生/未知 cwd 不默认归 DailyCheck，输出只记录过滤数量，不含其他项目细节。CLI 由 `ORCA_CLI_COMMAND`、开发环境、平台规则解析；
不能读取时保留源警告和旧事实，`--no-orca` 用于离线输入与测试。
worker-list scope 非 all 时明确记录其他 Run 未覆盖，不假装全量。

JSONL 保留旧字段，`schema_version=2` 增量追加：

- 身份：`row_id/dispatch_id/task_id/retry_of/run_id/worker_instance_id/session_id`；
  `repo_id/host_id/worktree_id/worktree_instance_id/worktree_name/branch`。
- 生命周期：`worktree_created_at/worktree_deleted_at/started_at/ended_at/running/observed_at`。
- 来源：`lifecycle_source/collected_at/source_warnings/usage_schema_version`。
- 用量：`actual_models/usage_events[]`，每事件保留唯一 ID、provider session、
  可得的 Task/Dispatch 关联、UTC 时间、实际模型、互斥 Token 分项及完整性。

SQLite 为原 `dispatch_ledger` 增量加 `facts_json`，保留旧列和主键，迁移可重复执行。
按明确 Dispatch ID 优先去重，旧行按 worker/UTC ts 回退。缺时间的 v2 事实用独立
存储哨兵满足旧表约束，查询及 DB 失败时的内存回退均恢复 fact_ts/null，哨兵绝不成为业务时间。
历史实例、事件不依赖当前 git worktree 列表，因此删除工作树不会删除历史。
本机输出也保留过去事实：源消失时保留旧身份/用量并标记，不能把旧用量刷新成新鲜。

## 身份与归属口径

工作树键优先 repo + host + 生命周期 instance ID；同实例复用合并，同路径重建分开。
当前 worktree 元数据仅在可证明创建时间早于 Dispatch 时用于识别该次执行；
否则退回 repo + host + 归一化路径，显式标「历史路径归组 · 近似」。
历史缺 host 为 unknown，不声称准确生命周期。无路径记录进入未归属分组。

Worker 按 host + processIncarnation 去重；终端句柄、Dispatch ID 不作 Worker 实例。
缺实例时列「身份未知执行」；未归属 provider 会话单列，不能冒充 Worker。
执行按 Dispatch 去重，只有 `retry_of` 才计显式重试；同 Issue 多角色是协作。
角色仅取显式元数据，缺失为 unknown，不根据模型、标题或正文猜测。

用量关联优先用户记录中注入 preamble 的唯一精确 Task/Dispatch ID（只提取标识，
绝不执行文本），或明确 provider session ID；长会话切换任务时按事件分段。
无明确链接时只允许 cwd 相同且事件落入唯一可证明的执行区间。
同树并发/缺区间/无事件时间且无显式链接时保留未归属会话，不重复摊给 worker。
文件 mtime 只用于候选文件发现，绝不作为用量归属、执行开始或结束证据。

PR 按 repo + number 去重，跨树汇总再去重；首先用显式 PR，次选准确 repo + head_ref
等于记录分支。不从 Issue 字符串推断 PR。关联 PR 是活动记录涉及的集合，
状态截至快照；它不是窗口内创建、合并或交付吞吐。PR 拉取失败保持状态未知。

## 用量、时间窗和时长

Token = 非缓存输入 + 输出 + 缓存读取 + 缓存写入。
推理已在输出内，仅显示「其中推理」，不重复求和或计价。
Claude 按会话/message.id 去重。Codex 现行 thread 累计快照与 legacy token_count
二选一，禁止叠加两源；有事件时间取增量、重放零增量不计、计数器回退建立新段；
无事件时间取每线程末次总量，模型切换无法拆分时显示 unsplittable，不挂到末模型。
多模型事件只计入实际模型段；缺总量/分项为 null，仅展示已知合计及分项覆盖；Claude 缺缓存字段同样不解释为零。
无事件 ID 的历史整次总量若可能与重新归属的事件重叠，保留原值于
legacy_usage_audit 并在详情说明，不重复加进已知合计，覆盖仍显示缺口；
不同实例/路径/显式会话可证明独立的历史以及来源失败但无重叠的已知量继续保留。
成本不是主视图指标；已有采集成本为 CNY，占位零价或缺价保留 null。

日期按 Asia/Shanghai 解释，转换成 UTC 左闭右开边界。
事件按发生时间归窗，历史整次总量按派单时间归档并显式说明，不能解释为精确日用量。
没有任何可用时间的记录只在全生命周期显示。角色/模型筛选作用到匹配执行/事件；
关联 PR 相应取子集。多模型执行没有可审计的模型运行区间，因此模型筛选后不分摊
整次运行时长，保留未知。

执行时长只认 Orca dispatchedAt/completedAt；结束缺失时，只有 projection.liveness
证明 agent live 且仍 in_progress，才能计到 observed_at 快照。
PTY observation live 不证明 agent 正在工作；unverifiable 不延伸到任意 now。
不使用 PR merged 时间、末条 transcript、mtime 补结束。
同 Worker 区间先求并集，再跨 Worker 累加；并行累计可大于工作树历时。
历时为已知最早开始到已知最后结束，包含间隔；窗口模式截断，缺测量标已知下界。
缺 Worker 身份时不能确保跨执行区间是否属于同实例，因此不计入 Worker 累计。

覆盖分别展示身份、模型、Token 分项、时长；新鲜度取源 collected_at，而非 SQLite
updated_at 或页面生成时间。超过 30 分钟为陈旧，源不可用/时间未知单列。
源发现只覆盖本机本仓库可发现的派单/工作树；无法发现的直接 root 会话明确是盲区。

## 交付状态

保留 active、ci_unknown、closed_unmerged、awaiting_prod、awaiting_deploy、
deploy_failed、delivered，混合 PR 显示各状态数，不用一个成功徽章覆盖未完成 PR。

只有 PR merged、关联 Issue 已关、当前 head SHA 的两个 required contexts 均 success、
对应 production/merge SHA 的成功部署与验证、至少 30 分钟完整观察窗成功且有引用，
才可 delivered。旧 SHA 检查、仅 merge、仅 rc=0、缺观察窗均不够。
既有 fetch_prs 保留 head/merge SHA，不额外为每个 PR 调 API。
读取现有 `/var/lib/dailycheck-deploy/events.jsonl` 的 pr/sha/rc/ts/comment 引用作为补充；
非零 rc 可标失败，零 rc 不能单独证明交付。
当前 PR 列表与部署事件不足以提供全部 CI/Issue/观察证据，所以正常缺证据时诚实
显示 awaiting/unknown，不会凭新 UI 编造 delivered。

## 运行接线与责任边界

2026-10-31 只读核对：本机已有每日 09:00 cron，从
`/Users/ericmr/Documents/GitHub/DailyCheck` 的 `.venv/bin/python` 运行 collect，随后
scp 直传 `/var/lib/dailycheck-dashboard/ledger/ledger.jsonl`。该主目录当时 main 为
`9d8928d`，尚无本次功能。仅本功能 worktree 的 `/tmp` 冒烟不代表该入口已升级。

提供 `python -m scripts.model_eval.publish --destination <已批准 SSH 别名>` 作为
同一 cron 的确定性入口：先 collect 成功，再上传唯一 `.upload-*` 文件，最后在
远端原子 rename 到既有 ledger 路径；失败非零退出，不发布半份文件。
本地文件锁防同一账本多个 publisher 并发。目标参数拒绝 shell 字符；不创建远端
目录、不安装调度、不重启服务。`--dry-run` 只打印 argv，不采集、不传输。

启用前置由 owner/Prod 按现有运维职责完成：主目录更新到合并后的 main；复用现有
cron 改用 publish，并将采集与同步周期对齐既有 15 分钟报表刷新；验证本机 Orca CLI
可用、SSH 非交互凭据与目标目录权限。每日一次的旧频率不能满足 30 分钟新鲜度，
页面会明确陈旧，不能通过放宽口径掩盖。Prod 验证目的端原子替换、权限、刷新及
真实覆盖；开发者仅提供可审阅入口、dry-run 和 mock 测试，不操作 VPS 或修改 cron。

## 验证

原生 tests/test_worktree_eval.py 覆盖实例/路径重建、PR/Worker/执行去重、重试链、
并行与区间并集、窗口 UTC 边界、reasoning 子项、部分字段、模型切换、累计/重置、
并发归属、preamble 长会话、旧 schema 迁移、未知结束、PTY 反例、分页/source 降级、
新鲜度、HTML 转义、交付证据和原子发布失败路径。旧采集/账本测试更新为新事实语义。
关键断言需变异杀死；真实 `/ops` 使用临时库、代表性 fixtures 和实际本机采集产物做
Playwright 投影一致性、筛选/展开/排序、桌面/手机/平板无溢出及无 JS 错误验证。
最终执行仓库当前 CI 范围全量 pytest + check_known_failures，禁止扩张基线。
