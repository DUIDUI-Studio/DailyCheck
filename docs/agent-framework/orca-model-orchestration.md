# DailyCheck Orca 模型与角色编排

> **2026-11-03：项目级模型路由表已作废。**
> 唯一来源：`~/.agents/skills/orca-model-routing/{SKILL.md,routing.json}`
> 项目采纳记录：[`orca-overlay.md`](orca-overlay.md) §5
> 流程权威：[`workflow.md`](workflow.md)

本文只保留 DailyCheck 的角色边界和 Orca 使用约束。模型、effort 和 fallback
由用户全局策略决定；本文不再维护独立的模型表。

## 角色边界

- Orca 是顶层调度者，负责 Run、Task、Dispatch、依赖、等待和资源回收。
- 模型承担认知工作；确定性脚本负责测试、门禁、merge 和部署。
- 一个工作树同一时间只有一个代码写者。
- Reviewer 必须使用独立 session，只读，不修复自己发现的问题。
- 普通任务不默认使用 `max`；升级条件由全局策略定义。
- Dev 不 merge；Prod 负责 merge / 部署；红线由 Owner 拍板。

## 路由解析

1. 任务类型按全局 `task_kinds` 解析角色。
2. 复杂度由 `score_complexity.py` 打分（阈值 6.5）；失败时 fail safe 到
   `developer_complex`。
3. 角色到 agent / model / effort / quota pool / fallback 的映射，由全局
   `routing.json` 决定。
4. 启动后比较 `launch.requested` 与 `launch.effective`；不一致按全局策略处理。
5. 本项目不再生成独立的 fallback 命令，也不再使用
   `model_routing.py next`。

## 已作废

- `docs/agent-framework/orca-model-routing.json`：标记 `deprecated: true`，
  仅供审计，不参与派活。
- `scripts/agent_framework/model_routing.py`：遇到 deprecated 文档直接
  fail closed（退出码 2），不生成 `worker-start` 命令。
- 旧的角色模型表和 fallback 顺序不再有效；需要历史信息时查 git 历史。

## 历史

2026-10-26 版本曾定义项目级主模型/备用模型表。2026-11-03 起由用户全局策略
取代，项目侧只保留采纳记录和角色边界。
