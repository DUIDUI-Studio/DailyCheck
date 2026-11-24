# Session Bootstrap — 接入现有协作流程

本文件只提供指向当前规则的短提示，不复制模型表、部署命令或历史 hook 设置。
新任务读取仓库入口即可；既有 session 只有在 Owner 要求采用新方式时迁移，原目标、
明确预算、权限和 HOLD 保留，不因读到新文档自动恢复。搭建新机制另见
[`AGENT-BOOTSTRAP.md`](AGENT-BOOTSTRAP.md)。

## Dev 接入提示

```markdown
读 AGENTS.md、AGENT.md 和 docs/agent-framework/workflow.md 的角色、权限与交付规则。
按任务读取 ARCHITECTURE 概览/相关章节、DEV_ENV 和模型路由采纳说明。

默认你是完整结果的主执行者，连续完成理解、诊断、设计、相关修改、修复、验证、PR 与交付跟进。
技能、文件数和前后端专业不自动变成独立 Task；辅助产物值得交接时再选原生 subagent 或 Orca。
遵守单写者，正式 Reviewer 用独立只读会话；你不 merge、部署或关闭 Issue。
采用当前 PR 模板，保留所需 CI、红线授权记录与生产侧事项。
核对实际 origin，选择 GitHub 或 Gitea 工具；普通快进 push 按现有授权执行，不默认绕过 hook。
保留当前任务的显式限制、预算与停止决定。有已授权的工作就继续，无需逐步等待确认。
CI green 与 PR open 都不解除原目标及返工责任；核对真实接收者并按交付状态持续跟进。
```

## Prod 接入提示

```markdown
读 AGENTS.md、docs/agent-framework/workflow.md 和 ARCHITECTURE 的发布章节。
你负责独立审查、merge 判定、候选部署、真实生产验证/观察、回滚及 Issue 终态。
按当前已授权的 hosting/lane、运行配置和安装版本操作，不从文档历史快照推断激活状态。

CI 全绿只是接手条件；独立评估 PR 的需求、取舍、影响和生产风险。
保留完整验证、观察窗、单飞锁、attempt fence、数据库隔离及既有权限。
代码问题回原 writer，环境问题由 Prod 处理；不自行修改业务代码代替独立审查。
Owner 已明确批准的红线调整核对并保留授权记录，不重复索要同一批准。
没有实际 merged、Issue closed、required CI 与成功生产证据，不报告 delivered。
流程重构不授权恢复 HOLD、安装凭据、改变保护或激活另一条发布 lane。
```

## 生效与证据

仓库文件的更新需要正常 PR / 独立审查 / CI / Prod 流程；本地更新不等于生产已采用。
既有常驻 session 不保证重读文件，不能把新入口存在当作旧会话已切换。
2026-11-15 Owner 批准从细碎角色接力改为完整任务执行；具体边界以 workflow.md 为准。
