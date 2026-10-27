# DailyCheck Orca 模型与角色编排

> 版本：2.0（2026-10-26）
> 机器配置：[`orca-model-routing.json`](orca-model-routing.json)
> 校验与回退命令：`python3 scripts/agent_framework/model_routing.py`
> 流程权威：[`workflow.md`](workflow.md)

本文定义 DailyCheck 在 Orca 中的模型分层、角色边界、基础设施 fallback、
升级条件和并发上限。本文不改变 Issue、PR、CI、merge、部署与红线的既有政策；
发生冲突时以 `workflow.md` 为准。

## 设计原则

- Orca 是顶层调度者，负责 Run、Task、Dispatch、依赖、等待和资源回收。
- 模型承担认知工作；确定性脚本负责测试、门禁、merge 和部署。
- 一个工作树同一时间只有一个代码写者。
- Reviewer 必须使用独立 session，只读，不修复自己发现的问题。
- 普通任务不默认使用 `max`；只有可识别的风险触发升级。
- Qwen 必须使用 cc-switch 生成的完整 stack 模型 ID。裸 ID 可能会被代理回退到
  `deepseek-flash`。
- CC Switch 聚合模式可以承担 provider 热切换；本文件负责 Orca 角色级的
  显式模型 fallback，避免任务在未知模型上继续。

## 默认角色路由

| 角色 | Agent | 主模型 | 第一备用 | 第二备用 | Effort |
|---|---|---|---|---|---:|
| Coordinator | Codex | `deepseek-flash` | `ccs-tencent/deepseek-v4.1-flash-1017` | `ccs-minimax/MiniMax-M3.1-Flash` | medium |
| Triage | Claude | `ccs-claude-qianwenai--qwen3.8-flash[1M]` | `ccs-claude-tencent--mimo-v2.6-flash[1M]` | `ccs-claude-qwen--MiniMax-M3.1-Flash[1M]` | low |
| Planner | Claude | `ccs-claude-qianwenai--qwen3.8-max[1M]` | `ccs-claude-tencent--kimi-k3.1-pro[1M]` | `ccs-claude-qwen--MiniMax-M3` | medium/high |
| Developer | Codex | `deepseek-flash` | `ccs-tencent/deepseek-v4.1-flash-1017` | `ccs-minimax/MiniMax-M3.1-Flash` | medium |
| Complex Developer | Codex | `deepseek-flash` | `ccs-tencent/deepseek-v4.1-flash-1017` | `ccs-tencent/hy3` | high |
| Reviewer | Claude | `ccs-claude-qwen--MiniMax-M3` | `ccs-claude-qianwenai--qwen3.8-max[1M]` | `ccs-claude-tencent--kimi-k3.1-pro[1M]` | high |
| Escalation | Claude | `ccs-claude-qianwenai--qwen3.8-max[1M]` | `ccs-claude-tencent--kimi-k3.1-pro[1M]` | `ccs-claude-tencent--glm-5.3[1M]` | max |
| Release | Prod Agent | 不调用 LLM | 不适用 | 不适用 | — |

完整模型、effort 和备用顺序以
[`orca-model-routing.json`](orca-model-routing.json) 为准，本文表格便于人读。

模型 ID 含 `[`、`]`，在 shell 中必须加引号。禁止裸 Qwen ID：

```text
qwen3.8-flash
qwen3.8-max
qwen3.7-plus
qwen3.5-plus
```

## Fallback 触发条件

只有基础设施问题触发模型切换：

- 启动阶段明确失败，且没有残留 writer；
- 401/403 认证失败；
- 429、配额耗尽或 provider overloaded；
- 连接失败、请求超时；
- provider 明确不可用；
- Orca 回执中的 `launch.effective` 与声明的目标模型不一致。

以下情况**不得**通过换模型绕过：

- Reviewer 报告 BLOCKER；
- 测试或 CI 失败；
- 模型输出质量不达标；
- 参数错误、上下文过长等确定性输入错误；
- `failedStage=unknown`、`outcome_unknown` 或仍有残留终端。

未知失败必须先由 Coordinator 读取 `failedStage` 和 `residualResources`，确认
是否已有 writer 或终端占用；确认前不得启动备用模型。

## 执行机制

1. Coordinator 从 JSON 或 `show` 命令取得角色主模型。
2. 主模型启动后，比较回执中的 `launch.requested` 与 `launch.effective`。
3. 发生允许 fallback 的失败时，先确认原 Dispatch 已安全结束，再记录
   `task_id`、失败 `dispatch_id`、失败模型和失败类别。
4. 使用同一 Task 和 `--retry-of` 启动下一模型。
5. 每次尝试重新校验 requested/effective。
6. 声明过的备用模型全部耗尽后停止并报告基础设施问题；不得继续临时换模型。

查看角色路由：

```bash
python3 scripts/agent_framework/model_routing.py show \
  --role developer \
  --json
```

分类 `worker-start` 失败回执：

```bash
python3 scripts/agent_framework/model_routing.py classify \
  --result-file /tmp/worker-start-result.json \
  --exit-code 1 \
  --requested-model deepseek-flash \
  --json
```

输出 `fallback_allowed=true` 才允许生成并执行下一模型命令；
`fallback_allowed=false` 必须按原失败处理。

生成下一模型的同 Task 重试命令：

```bash
python3 scripts/agent_framework/model_routing.py next \
  --role developer \
  --failed-model deepseek-flash \
  --task task_123 \
  --retry-of ctx_failed \
  --worktree current \
  --json
```

`next` 只生成命令，不直接启动 Worker。Coordinator 必须先完成失败分类、writer
停止或既有 Dispatch 结算判定，再执行输出中的命令。

## 标准执行波次

```text
Issue / 用户反馈
      ↓
Triage · Qwen3.8-Flash · low
      ↓
复杂度闸门
  ├─ 简单 → Developer · Codex medium
  └─ 复杂 → Planner · Qwen3.8-Max medium
                ↓
           Developer Complex · Codex high
      ↓
Reviewer · MiniMax-M3 high
  ├─ PASS → PR / CI
  └─ BLOCKER → 原 Developer session 修复
                       ↓
                 新 Reviewer session 复审
      ↓
CI 全绿
      ↓
Prod Agent merge + 蓝绿部署
```

任一步发生允许的基础设施失败时，在上述波次内按该角色的 fallback 顺序重试。
Review BLOCKER 仍返回原 Developer session 修复，不触发 fallback。

## 升级条件

启动 Planner：

- 涉及两个以上运行模块；
- 两次定向复现尝试后根因仍不明确；
- 触及 SQL JOIN、迁移、权限或删除语义；
- 同时影响 Flask、模板、MCP 或持久化契约。

启动 Escalation：

- Reviewer 连续两轮发现同类 BLOCKER；
- 可能出现生产数据损坏或安全问题；
- 触及 Owner 红线或产品语义变更；
- 高 effort 连续两轮没有新增证据。

第三次失败不得继续盲目换模型；必须补复现、日志、数据或请 Owner 决策。

## 成本与并发

- 最多 2 个开发 Worker；
- 最多 1 个 Reviewer；
- 最多 1 个大型 PR 进入 CI；
- 每个 Task 最多使用 2 个 fallback；
- Worker 阶段只跑定向 pytest；
- 提 PR 前跑一次全量测试；
- Reviewer 只读取 Issue、diff、相关测试和必要架构章节；
- 不默认把整个仓库塞进 1M 上下文；
- 同一 Issue 默认最多两次自动修复尝试。

## 模型生效验证

每次启动后必须检查：

```text
launch.requested
launch.effective
```

主模型等于 requested 时继续；不一致时视为模型未生效。若当前 fallback 已被
显式声明且顺序一致，可以记录后继续；任何未声明替换都视为基础设施故障。

直接调用模型时还应检查响应体的 `model` 字段。每次 fallback 必须保留尝试索引、
requested、effective、task_id、dispatch_id 和失败类别，便于统计与审计。

## 变更记录

| 日期 | 版本 | 变更 |
|---|---|---|
| 2026-10-26 | 2.0 | 增加角色级模型 fallback、基础设施失败分类、同 Task 重试命令与校验脚本 |
