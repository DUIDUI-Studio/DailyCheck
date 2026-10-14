# DailyCheck Agent 协作工作流

> 版本：2026-10-15 定稿（Eric 口述对齐 + 实况核对）。
> 本文档是 Dev / Prod / Owner 三方协作的权威口径；与本文冲突的口头约定以本文为准。
> 机制细节的演进史见 `docs/agent-framework/v0.1.md`。

## 一、角色分工

| 角色 | 载体 | 职责 | 绝不碰 |
|------|------|------|--------|
| **Dev Agent** | 本地开发机 | 开发、测试、提 PR、（merge 政策见 §四） | 生产环境（VPS 只读排查需 owner 授权） |
| **Prod Agent** | VPS (wdg_vps) | 部署执行、生产验证、灰度切流、回滚、清理 | 仓库治理决策（改 main 内容、开非常规 PR） |
| **Owner (Eric)** | — | 需求拍板、方案拍板、红线内政策的最终决定 | — |

需求来源两条：owner 直接提需求；或 Prod Agent 在生产验证中发现问题 → 开 Issue → 转 Dev。

## 二、端到端流程

```
① 需求/Issue ──► ② Dev 开发 + 本地门禁 ──► ③ 提 PR（模板）──► ④ CI 门禁
                                                              │ 全绿
   ┌──────────────────────────────────────────────────────────┘
   ▼
⑤ merge（权限归属见 §四）──► ⑥ Prod Agent 部署（蓝绿）──► ⑦ 收官/回滚
```

### ② 开发 + 本地门禁（Dev Agent）

提交前 SOP，缺一不可：

1. 确认分支、确认改动范围
2. `ruff check` 仅改动文件零告警
3. pytest **零新增失败**：对比 `tests/known_failures.txt` 基线逐行比对（清单**只许收缩不许扩张**）
4. 关键断言做**变异验证**：把修复临时还原成 bug 版，确认新测试真实 FAIL（防恒真式假绿）
5. shell 脚本改动：`bash -n` + dry-run 冒烟
6. 触及架构子系统必须同步 `docs/ARCHITECTURE.md`（pre-commit hook 强制；helper：`scripts/arch_touched_sections.py`）

### ③ 提 PR（模板强制）

- 新建 PR 自动带出 `.github/PULL_REQUEST_TEMPLATE.md`
- body 必含 h2 级「**## 报告 1/4：需求对齐**」+「**## Checklist**」；R 表占位为「❌ 待填」，**全部消解为 ✅ 门禁才绿**
- ⚠️ 已知坑：CI 用**事件时刻的 body 快照**（`github.event.pull_request.body`）。事后 `gh pr edit` 补内容后，必须**推空 commit 触发新事件**，rerun 无效
- 本地自检捷径：`PR_BODY_FILE=/tmp/body.md python scripts/agent_framework/check_reports.py`

### ④ CI 门禁（分支保护 ruleset "Main" 强制）

| check | 内容 | 全绿标准 |
|-------|------|---------|
| `test` | pytest 全量 + 零新增门禁 | `check_known_failures.py` exit 0（失败集 ⊆ known_failures.txt） |
| `agent-framework-reports` | 四报告齐全性 | 报告 01（body 段落）+ 02（pytest gate）+ 03（ruff/bandit）+ 04（CI 自动生成） |
| `deploy` | 生产部署 | **永不自动触发**（2026-10-11 Eric 拍板禁用，仅 workflow_dispatch；正式启用前置：备份 cron + 异地副本 + 真实部署演练） |

报告体系：01 需求对齐（Dev 填）/ 02 测试（CI）/ 03 代码质量（CI）/ 04 本地部署预演（CI 占位）/ 05 生产验证（Prod Agent 的 verify.sh 自动落盘 `backups/prod-verifications/`）。

## 三、Push 政策（2026-10-15 Eric 放开）

- **普通 `git push`（新分支/已有分支推进）不再需要 owner 事先确认。**
- 仍需 owner 单独确认的操作：`--force` / `--force-with-lease`、删除远程分支、改分支保护规则。
- pre-push hook（`core.hooksPath` 指向的脚本）恒 `exit 1`，推分支用 `git push --no-verify`——这不属于绕过，是既定姿势。

## 四、Merge 政策（2026-10-15 Eric 拍板：候选 A）

**Dev Agent 在 CI 全绿后常规 merge**（`gh pr merge <num> --merge`），无需 owner 逐个确认。

红线清单——以下 PR 仍须上报 owner 拍板，Dev 不得自行 merge：

1. 扩张 `tests/known_failures.txt`（清单本身只许收缩，出现扩张即流程违规）
2. 改 `.github/workflows/*`（CI/部署流水线自身）
3. 改分支保护规则 / ruleset
4. 删除或弱化 `docs/ARCHITECTURE.md` 铁律
5. owner 在需求中标注「需拍板」的变更

设计依据：merge ≠ 部署。main 只是集成分支，生产风险由 Prod Agent 的 verify 七项 + 灰度 + rollback 兜住；质量权威是 CI 门禁而非人审；Prod merge 会混淆「仓库治理」与「生产操作」的角色边界，故不采用（候选 B 已否决）。

## 五、部署机制（Prod Agent，蓝绿）

前置一次性（已就位）：绿 systemd unit 声明 `Environment=DAILYCHECK_DB_DIR=/var/lib/dailycheck-green/db`（Issue #28）。

```
git pull → up.sh <pr> <sha>
             ├─ 守卫：绿 unit 必须声明 DAILYCHECK_DB_DIR 且 = 拷贝目标，否则拒启
             ├─ cp -aL 真库 ${APP_DIR}/db → /var/lib/dailycheck-green/db（独立副本）
             └─ 起绿 :8091/:5101 + 健康检查
         → verify.sh <pr> 七项（任一 ❌ 即 FAIL 并自动写报告）
             1 pytest 零新增门禁   2 sim_user 四路径   3 ab 性能比对
             4 回滚演练            5 资源与依赖        6 dry_run 隔离证据
             7 DB 隔离证据（environ + 非软链 + inode 相异）
         → switch.sh 灰度 5% → 50% → 100%（每档观察）
         → 观察窗 ≥30min → 收官（绿=生产，蓝 standby）
```

- **任何异常 → `rollback.sh` 秒回蓝**（Issue #28 修复生效后为代码+数据双回；此前仅回代码）
- 铁律四条 + DB 隔离铁律见 `docs/ARCHITECTURE.md` §3.4
- 已知注意：蓝 standby 进程内存里是旧代码，**重新启用蓝前必须 restart**

## 六、应急

| 场景 | 动作 |
|------|------|
| 绿不健康 / verify 红 | 停，`rollback.sh` 回蓝，取证（journalctl + 报告自动落盘）后报 owner + Dev |
| verify 报告未生成 | Issue #24 Bug B 已修（trap EXIT 兜底）；若再现 = 框架回归，立即报 Dev |
| nginx config 异常 | 备份只许在 `/var/lib/dailycheck-blue-green-backups/`；`sites-enabled/` 内出现 .bak 立即 mv 走并 `nginx -t` |
