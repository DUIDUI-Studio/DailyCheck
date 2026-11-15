# DailyCheck 执行方式与模型选择

> 项目流程权威：[`workflow.md`](workflow.md)；采纳记录：[`orca-overlay.md`](orca-overlay.md)。
> 模型、CLI、effort 与额度池唯一来源：`~/.agents/skills/orca-model-routing/{SKILL.md,routing.json}`。
> 本文不维护项目模型表，也不要求每项任务使用 Orca。

## 先选择执行方式

1. 默认当前主执行者连续完成完整结果，包括理解、诊断、设计、相关实现、修复、验证和交付跟进。
2. 有值得交接的独立辅助成果时使用原生 subagent；短命令、常规测试和紧密关联的调试留在主执行者。
3. 跨 CLI/账户/环境、独立工作区、正式独立性或受监督生命周期确有需要时使用 Orca worker。用户明确要求 Orca 时遵从；多文件、耗时长或调用技能不是自动升级理由。
4. 只有多个独立工作流确需管理时才设置专职 Coordinator。主执行者管理助手时仍是 writer。

Orca 是可选的执行设施，管理实际创建的 Run / Task / Dispatch；其版本匹配指南负责生命周期。
原生助手不拥有 Orca Dispatch 身份。worker 必须遵守自身 Task 的委派范围，不递归创建未授权 worker。

## 再选择模型

- 已有会话不为套用角色表而重启。确认需要新 agent 后，按完整任务难度、工具能力、上下文、稳定性及总完成成本选择获准模型。
- Codex 使用官方订阅实际支持的 OpenAI 模型，不接入第三方或自动付费 API 回退。
- Claude Code 主要指 CLI，通过 CCSwitch 使用实际配置的模型 ID。Qwen、DeepSeek、GLM 等候选不局限于 Desktop 的 Haiku/Sonnet/Opus 别名；配置存在不等于已验证可调用或更省钱。
- 主会话和助手可分别选型，以宿主实际支持为准；不自动继承最强模型，也不为省单次费用把完整任务切成细碎角色。
- `score_complexity.py` 是可选建议，不能阻断派发或把评分故障当成复杂任务证据；无评分时由执行者依据任务和获准路线判断。
- 新 Orca 启动时核对请求与实际 CLI/model/effort/pool；不一致先查原因，不切换共享提供商影响其他会话。恢复与升级遵守全局策略和任务显式预算。

## 项目不变量

共享工作树同一时间一个写者。正式 Reviewer 使用独立只读会话，不修复自己发现的问题。
Dev 保留原目标及返工责任；merge、部署和 Issue 关闭由 Prod Agent 执行。用户明确限制、
生产 attempt fence 和已有 HOLD 不因本次默认调整而失效。CI green 不等于交付。

## 历史资产

2026-11-03 起项目模型表作废，2026-11-15 采纳完整任务执行方式：

- `orca-model-routing.json` 标记 `deprecated: true`，只供审计。
- `scripts/agent_framework/model_routing.py` 对 deprecated 文件退出码 2，不生成 launch 命令。
- 不使用旧 `model_routing.py next`、固定角色接力或评分失败自动选复杂路线；历史设计查 Git 历史。
