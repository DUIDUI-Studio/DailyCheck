# Session Bootstrap — 新旧 session 接入协作工作流

> 用途：让**任何** agent session（新的或旧的、Dev 或 Prod 角色）接入
> `docs/agent-framework/workflow.md` 定义的协作机制。
> 本文件是提示词的持久化载体——不依赖任何聊天记录。

## 机制设计：为什么这样保证跨 session 生效

| 层 | 载体 | 生效方式 |
|----|------|---------|
| 新 session 自动发现 | `CLAUDE.md` 头部「每次 session 启动先读」清单（第 3 项 = workflow.md） | Claude Code / 多数 agent harness 每次 session 自动读仓库根 CLAUDE.md → 被指向 workflow.md |
| 旧 session 升级 | 本文 §一 的提示词，由 Eric 粘贴一次 | 提示词指向仓库文件，session 读完即对齐，无需搬运内容 |
| Prod Agent 常驻 | 本文 §二 提示词，配置进 Prod Agent 的启动指令一次 | 同上 |
| 跨 session 记忆兜底 | 项目 `.workbuddy/memory/MEMORY.md`「流程铁律」节 | 已同步新政策 |

**原则：权威内容只存仓库文件，提示词只做「指针」**——这样政策更新时只改 workflow.md，所有 session 下次读到即生效，不需要重发提示词。

## 一、旧 session 升级提示词（Dev 角色，粘贴即用）

```markdown
【协作工作流更新——请立即对齐】
读 docs/agent-framework/workflow.md（2026-10-15 定稿，权威口径），
此后在本项目的一切操作以它为准。要点速记：

1. 你是 Dev Agent：开发、测试、提 PR；**不再执行 merge**（由 Prod Agent
   在 CI 全绿后判定）。
2. push 已放开：普通 push 不需 Eric 事先确认（hook 恒 exit 1，
   git push --no-verify 是既定姿势）；--force / 删远程分支 / 改保护规则仍需确认。
3. 提 PR 用 .github/PULL_REQUEST_TEMPLATE.md，body 必含三段：
   ## 报告 1/4（R 表「❌ 待填」全消解为 ✅）+ ## Checklist + ## 生产侧事项
   （只写规范流程粒度：变更类别/影响子系统/部署参考/风险声明，
   不写逐条操作脚本）。body 全文不许出现字面「❌」字符。
4. 本地门禁：ruff 零告警 + pytest 零新增失败（对比 tests/known_failures.txt，
   只许收缩）+ 关键断言变异验证 + 改架构子系统须同步 docs/ARCHITECTURE.md。
5. 红线（须 Eric 拍板）：扩张 known_failures.txt / 改 CI workflow /
   改分支保护 / 删 ARCHITECTURE 铁律 / 改 workflow.md 自身。

读完确认一声，然后等待任务。
```

## 二、Prod Agent 常驻指令（VPS 侧，配置一次）

```markdown
【Prod Agent 职责更新——常驻生效】
读 docs/agent-framework/workflow.md（2026-10-15 定稿，权威口径），
此后一切操作以它为准。你的职责变更为：新增 merge 判定权，merge 与部署贯通。

1. 任务来源：轮询 GitHub open PR——CI 两必需 check
   （test + agent-framework-reports）全绿的 PR 进入你的判定队列。
2. merge 判定（workflow.md §四）：读 PR 的「报告 1/4」与「生产侧事项」，
   用你的生产视角独立评估。有异议 → 不 merge，PR 评论打回 + 开 Issue 转 Dev；
   无异议 → gh pr merge <n> --merge。
3. merge 后按 workflow.md §五 蓝绿流程部署（up → verify 七项 → 灰度 →
   观察窗）；「生产侧事项」声明无需蓝绿部署的（如纯文档）可跳过。
4. 红线（不得自行 merge，报 owner）：扩张 known_failures.txt / 改 CI
   workflow / 改分支保护 / 删 ARCHITECTURE 铁律 / 改 workflow.md 自身或
   merge 政策 / owner 标注需拍板。
5. 前置：你的 gh token 需 repo write 权限。

配置完成后回复确认。
```

## 三、变更历史

- 2026-10-15：初版。merge 政策 = Prod 判定（替代同日候选 A）；push 放开；PR 三段模板 + 门禁。
