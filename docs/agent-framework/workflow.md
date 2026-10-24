# DailyCheck Agent 协作工作流

> 版本：2026-10-15 定稿（Eric 口述对齐 + 实况核对；同日二次修订——push 政策放开、merge 政策改为 Prod 判定）。
> 新/旧 agent session 接入本机制的提示词见 [`session-bootstrap.md`](session-bootstrap.md)。
> 本文档是 Dev / Prod / Owner 三方协作的权威口径；与本文冲突的口头约定以本文为准。
> 机制细节的演进史见 `docs/agent-framework/v0.1.md`。

## 一、角色分工

| 角色 | 载体 | 职责 | 绝不碰 |
|------|------|------|--------|
| **Dev Agent** | 本地开发机 | 开发、测试、提 PR（含「生产侧事项」段落，见 §三） | merge（由 Prod Agent 执行，§四）、生产环境（VPS 只读排查需 owner 授权） |
| **Prod Agent** | VPS (wdg_vps) | **merge 判定**、部署执行、生产验证、灰度切流、回滚、清理 | 仓库治理决策（主动改 main 内容、开非常规 PR；对 PR 内容有异议 → 打回/开 Issue，不自行改代码） |
| **Owner (Eric)** | — | 需求拍板、方案拍板、红线内政策的最终决定 | — |

需求来源两条：owner 直接提需求；或 Prod Agent 在生产验证中发现问题 → 开 Issue → 转 Dev。

## 二、端到端流程

```
① 需求/Issue ──► ② Dev 开发 + 本地门禁 ──► ③ 提 PR（模板 + 生产侧事项）──► ④ CI 门禁
                                                                              │ 全绿
   ┌──────────────────────────────────────────────────────────────────────────┘
   ▼
⑤ Prod Agent merge 判定 + 执行（§四）──► ⑥ 部署（蓝绿，§五）──► ⑦ 收官/回滚
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
- body 必含 h2 级「**## 报告 1/4：需求对齐**」+「**## Checklist**」+「**## 生产侧事项**」；R 表占位为「⬜ 待填」，**全部消解为 ✅ 门禁才绿**
  （占位符**不能**用未消解记号字符：门禁按字面切段，模板会因此自带红色。2026-10-24 已修，`tests/test_pr_template_gate.py` 锁死）
- ⚠️ 已知坑一：CI 用**事件时刻的 body 快照**（`github.event.pull_request.body`）。事后改 body（Gitea：`PATCH /api/v1/repos/{owner}/{repo}/pulls/{n}`）后，必须**推空 commit 触发新事件**，rerun 无效
- ⚠️ 已知坑二：「报告 1/4」段之后（直到「报告 2/4」标题，实践中 = 全文）**不许出现字面 ❌ 字符**——门禁靠它识别未消解状态，正文叙述里写 ❌（哪怕只是举例）也会被判定未消解。写「红叉」「未消解状态」等文字替代
- 本地自检捷径：`PR_BODY_FILE=/tmp/body.md python scripts/agent_framework/check_reports.py`

### ③′ 「生产侧事项」段落的写法铁律

PR body 的「生产侧事项」段落是 Dev 留给 Prod Agent 的**唯一**部署指引，粒度必须守住：

- **只写规范流程**：本次变更属于哪类（代码/依赖/schema/配置/nginx/框架脚本）、影响哪些子系统、部署时应套用流程的哪些环节、需要哪些前置（如凭据 export）、观察窗应重点看什么类别的问题。
- **不写针对性操作脚本**：不给逐条命令、不给"Step 1/2/3"式的操作手册。Prod Agent 以 workflow.md §五的标准流程为基准自行编排动作——它的价值在于**用自己的视角独立发现问题**，照抄 Dev 的脚本会让 Dev 的盲区变成双方的盲区。
- **允许 Dev 声明风险**：可以写"本次触及 X，生产验证时该类变更常见的失败模式是 Y 类"，这是提示不是指令。
- Prod Agent 对 PR 内容本身有异议（设计/风险/范围）：**不 merge**，在 PR 评论打回并开 Issue 转 Dev；merge 判定权含否决权。

### ④ CI 门禁（Gitea 分支保护 `main` 强制）

| check | 内容 | 全绿标准 |
|-------|------|---------|
| `CI / test (pull_request)` | pytest 全量 + 零新增门禁 | `check_known_failures.py` exit 0（失败集 ⊆ known_failures.txt） |
| `CI / agent-framework-reports (pull_request)` | 四报告齐全性 | 报告 01（body 段落）+ 02（pytest gate）+ 03（ruff/bandit）+ 04（CI 自动生成） |
| `CI / deploy (pull_request)` | 生产部署 | **永不自动触发**（2026-10-11 Eric 拍板禁用，仅 workflow_dispatch；正式启用前置：至少一次手动 workflow_dispatch 演练跑通） |

报告体系：01 需求对齐（Dev 填）/ 02 测试（CI）/ 03 代码质量（CI）/ 04 本地部署预演（CI 占位）/ 05 生产验证（Prod Agent 的 verify.sh 自动落盘 `backups/prod-verifications/`）。

## 三、Push 政策（2026-10-15 Eric 放开）

- **普通 `git push`（新分支/已有分支推进）不再需要 owner 事先确认。**
- 仍需 owner 单独确认的操作：`--force` / `--force-with-lease`、删除远程分支、改分支保护规则。
- pre-push hook（`core.hooksPath` → `~/.workbuddy/git-hooks`）2026-10-19 起是**黑名单**：只拦「删除远程分支」和「非快进覆盖」，新分支/快进照常放行。
  仍建议 `git push --no-verify`（避开 hook 里的网络调用），但不再强制。

## 三″、平台命令对照（2026-10-22 迁至自托管 Gitea）

CI/CD 已从 GitHub 迁到 hub1 上的 Gitea 28.1（`http://172.28.129.216:3000`，VPC 内网）。
agent 侧不再用 `gh`，改为直连 Gitea REST API（`Authorization: token <token>`）。

| 动作 | 原 GitHub 命令 | 现在（Gitea API） |
|---|---|---|
| 列 open PR | `gh pr list --state open` | `GET /api/v1/repos/{o}/{r}/pulls?state=open` |
| 看单个 PR | `gh pr view N` | `GET /api/v1/repos/{o}/{r}/pulls/N` |
| 建 PR | `gh pr create --draft` | `POST /api/v1/repos/{o}/{r}/pulls`，body `{head, base, title, body}` |
| 改 PR body | `gh pr edit N --body` | `PATCH /api/v1/repos/{o}/{r}/pulls/N`，body `{"body": "..."}` |
| 查 check 状态 | `gh pr checks` | `GET /api/v1/repos/{o}/{r}/commits/{sha}/status` |
| merge | `gh pr merge N --merge` | `POST /api/v1/repos/{o}/{r}/pulls/N/merge`，body `{"do":"merge"}`（**小写**，swagger 的 required 字段名就是 `do`） |
| 发评论 | `gh pr comment N` | `POST /api/v1/repos/{o}/{r}/issues/N/comments` |
| 开 issue | `gh issue create` | `POST /api/v1/repos/{o}/{r}/issues` |

三个必知差异：

1. **status context 带事件后缀**。Gitea 的 check 名叫 `CI / test (pull_request)`，不是裸 `test`。
   分支保护的 `status_check_contexts` 必须写全名，否则永远判定「未全绿」，merge 被拒
   （实测报错 `Not all required status checks successful`）。
2. **`github.event.pull_request.body` 仍然可用**。workflow 语法完全兼容，PR body 门禁无需改造（已实测跑绿）。
3. **没有 `check_suite` 事件**。Gitea 用 `status` 事件代替（每次 commit status 变化触发一次），
   payload 里没有 PR 关联信息，需按 `sha` 反查 open PR。

### 三″.1 地址与凭据（选错地址会直接 000）

| 你在哪 | API 根 | 判断方法 |
|---|---|---|
| 生产 VPS / 蓝绿实例（与 hub1 同 VPC `172.28.129.0/20`） | `http://172.28.129.216:3000/api/v1` | `ip -4 addr \| grep 172.28.129` 有输出 |
| 本机（Tailscale） | `http://hub01:3000/api/v1` | `tailscale status` 能看到 `hub01` |
| 公网 | **不可用**（ufw 关闭） | — |

- Token 两个、各专用，不要混用：**Dev Agent** 用于代码/PR/issue；**Prod Agent** 用于合并与部署判定。
- 本机 token 在 `~/.config/gitea-token`；生产在 `/etc/dailycheck/gitea-token`（0600 root，
  `dc-webhook-receiver.service` 通过 `GITEA_TOKEN_FILE=` 指向它，已实测可调 API）。
  ⚠️ `dailycheck_pr_poll.py` 的**默认**路径仍是 `/root/.gitea-token`（该文件不存在）——
  直接跑 poller 会认证失败，必须显式 `GITEA_TOKEN_FILE=/etc/dailycheck/gitea-token`。
  排查「快通道 / 轮询没反应」时先看这两处路径。
- **权限不足返回 403，不是 401**。看到 403 先查 token scope，别急着判定 token 失效。
- 分支保护 `main`：禁止直推（`enable_push=false`），必需 check =
  `CI / test (pull_request)` + `CI / agent-framework-reports (pull_request)`。

### 三″.2 五个高频坑（都实测过）

1. **别从旧分支开 PR**。GitHub 时代的旧分支自带旧 `ci.yml`（其中一步要去 github.com 下载 Python），
   在 hub1 上会超时重试，CI 能跑 1.5 小时。开 PR 前先 `git rebase origin/main`。
2. **runner 并发 = 1**。单 PR 的 CI 约 25–40 分钟，同时开多个大改动只会互相排队。
   别把 CI 当第一道关，本地先跑 ruff + pytest。
3. **`GET /pulls/{N}/merge` 是陷阱**：对已合并的 PR 也返回 204。判断是否已合并用
   `GET /pulls/{N}` 的 `state` + `merged` 字段。
4. **CI 卡在 `Checkout code` 7 分钟后失败 = 基础设施问题**（runner 与 Gitea 连通性），
   报 owner，不要改代码。
5. **登录后被踢回登录页** = `ROOT_URL` 与访问协议不一致导致 Cookie 带 `Secure` 被丢弃，
   报 owner 改 `app.ini`，不要自己动。

## 四、Merge 政策（2026-10-15 Eric 二次拍板：Prod merge，替代同日早先的候选 A）

**Prod Agent 在 CI 全绿后判定并执行 merge**（Gitea：`POST /api/v1/repos/{owner}/{repo}/pulls/{num}/merge`，body `{"do":"merge"}`），merge 与部署由同一角色贯通。

Prod Agent 的 merge 判定 = 独立审查，不是橡皮图章：

1. 确认 CI 两必需 check 全绿（test + agent-framework-reports）
2. 读 PR 的「报告 1/4」（根因/取舍/不在范围）与「生产侧事项」，**用自己的生产视角评估**：变更类别、影响面、部署风险是否有未声明项
3. 有异议 → 不 merge，PR 评论打回 + 开 Issue 转 Dev；无异议 → merge 并按 §五 部署

红线清单——以下 PR 仍须上报 owner 拍板，Prod Agent 不得自行 merge：

1. 扩张 `tests/known_failures.txt`（清单本身只许收缩，出现扩张即流程违规）
2. 改 `.github/workflows/*`（CI/部署流水线自身）
3. 改分支保护规则 / ruleset
4. 删除或弱化 `docs/ARCHITECTURE.md` 铁律
5. owner 在需求中标注「需拍板」的变更
6. **改本工作流文档自身 / merge 政策**（防止角色自我扩权）

### 红线的执行方式（2026-10-24 起，从自述改为机验）

上表此前全靠自觉。2026-10-24 实测发现 PR #13 改了 `.github/workflows/ci.yml`
（红线 2）被 Prod Agent 自行合并，无人拦截 —— 因为 `check_reports.py` 只看 PR
body 格式，完全不看改动文件路径。

现在红线 1/2/3/5/6 由 `scripts/agent_framework/check_redlines.py` 机验，跑在
`agent-framework-reports` job 内。命中且无授权 → 该 check 红 → Prod Agent 合不动。

| 红线 | 判据 | 机验 |
|------|------|------|
| 1 扩张 `known_failures.txt` | `git diff --numstat` 新增行 > 0（只删不算） | 是 |
| 2 改 `.github/workflows/*` | 改动路径前缀 | 是 |
| 3 改分支保护 | 改 `branch-protection.md`；ruleset 本体在 Gitea 服务端，仓库内无文件 | 部分 |
| 4 删/弱化 `ARCHITECTURE.md` 铁律 | 需判断语义 | **否，靠 review** |
| 5 owner 标注「需拍板」 | 正文含该字样却无授权声明 | 是 |
| 6 改 workflow.md / 本套指令 | 改动路径精确匹配 | 是 |

**豁免**：PR body 写一行 `Owner-Authorization: <日期> <谁> <授权内容>`。这是自述、
无法证伪，但能让越线留下痕迹，事后可审计谁引用过哪条授权。

**刻意不机验红线 4**：`ARCHITECTURE.md` 每次子系统改动都要同步，按路径拦会制造
大量噪音，反而训练出「先绕过再说」的习惯 —— 噪音比漏拦更伤门禁。


设计依据（Eric 2026-10-15 口述）：Dev 在 PR 里只给**规范流程级**的部署信息（§三′），不替 Prod 写操作脚本——避免 Prod Agent 完全参照 Dev 的视角执行而发现不了 Dev 自己的盲区。merge 判定权交给生产端，使「合不合」由最终承担生产风险的一方决定；质量权威仍是 CI 门禁，owner 拍板仍是红线兜底。

> 历史注记：2026-10-15 早先曾拍板候选 A（Dev 自行 merge + 红线上报），同日改为本政策。

## 五、部署机制（Prod Agent，蓝绿）

入口即 §四：Prod Agent merge 后直接进入部署流程（merge 与部署同角色贯通，无交接间隙）。

前置一次性（已就位）：绿 systemd unit 声明 `Environment=DAILYCHECK_DB_DIR=/var/lib/dailycheck-green/db`（Issue #28）。

**部署单飞（2026-10-16 起，机制细节见 `docs/ARCHITECTURE.md` §3.8）**：部署是 40-60 分钟长流程，而 webhook 事件天生并发——同一时刻全系统至多一个部署会话在动蓝绿设施；「欠账」由状态事实（`deployed.sha` vs `origin/main`）判定，不靠会话排队。

```
deploy_state.py acquire --pr <N> --sha <merge sha>
             ├─ rc 10（他人在部署）→ 不排队、直接结束，欠账留待下一次 catchup（兜底轮询 ≤30min 保底）
             └─ rc 0 → 持锁进入部署（长步骤间 deploy_state.py heartbeat --token <t> --phase <阶段>）
git pull → up.sh <pr> <sha>
             ├─ 守卫：绿 unit 必须声明 DAILYCHECK_DB_DIR 且 = 拷贝目标，否则拒启
             ├─ cp -aL 真库 ${APP_DIR}/db → /var/lib/dailycheck-green/db（独立副本）
             └─ 起绿 :8091/:5101 + 健康检查（绿 unit 声明 DRY_RUN=true = 验证期写隔离）
         → verify.sh <pr> 八项（任一 ❌ 即 FAIL 并自动写报告）
             1 pytest 零新增门禁   2 sim_user 五路径   3 ab 性能比对
             4 回滚演练            5 资源与依赖        6 dry_run 隔离证据
             7 DB 隔离证据（environ + 非软链 + inode 相异）
             8 nginx 入口终态探活（主站 + MCP，非 502）
         → 【绿转正】承接流量前必做（Issue #41）：关 DRY_RUN + DAILYCHECK_DB_DIR 指回
             ${APP_DIR}/db + restart 绿两个 unit + 复验；标准 5 步见 scripts/blue_green/README.md
             ⚠️ 未转正时 switch.sh 的 DRY_RUN 守卫会拒跑（exit 3）——这是预期拦截，不是故障
         → switch.sh 灰度 5% → 50% → 100%（每档观察；100%/0% 边界自动做落点核验）
         → 观察窗 ≥30min → 收官（绿=生产，蓝 standby）
         → deploy_state.py release --token <t> --result success（推进 deployed 锚点）
             失败：release --result failed —— 同 sha 不自动重试（防坏 main 死循环），留 last_attempt 供追查
```

- **守卫语义**（up/verify/switch 拒并发时 exit 10）：up / verify 需活跃锁且 PR 匹配；switch 仅 `green` 目标需活跃锁（`blue` 方向豁免——回滚演练内嵌 switch 不被锁阻塞）；**rollback / down 免锁校验**（应急止损永不被部署锁阻塞）。`--dry-run` 全部跳过；人工受控操作可设 `DAILYCHECK_DEPLOY_UNLOCKED=1`（警告放行）
- **激活顺序（一次性）**：先更新 Prod Agent skill（会话侧协议）→ 再 `init` 建状态目录激活。反序会出现「机制已激活、旧 skill 不握锁 → up.sh 拒跑」
- **任何异常 → `rollback.sh` 秒回蓝**（Issue #28 修复生效后为代码+数据双回；此前仅回代码）
- 铁律四条 + DB 隔离铁律见 `docs/ARCHITECTURE.md` §3.4
- 已知注意：蓝 standby 进程内存里是旧代码，**重新启用蓝前必须 restart**

## 六、应急

| 场景 | 动作 |
|------|------|
| 绿不健康 / verify 红 | 停，`rollback.sh` 回蓝，取证（journalctl + 报告自动落盘）后报 owner + Dev |
| verify 报告未生成 | Issue #24 Bug B 已修（trap EXIT 兜底）；若再现 = 框架回归，立即报 Dev |
| nginx config 异常 | 备份只许在 `/var/lib/dailycheck-blue-green-backups/`；`sites-enabled/` 内出现 .bak 立即 mv 走并 `nginx -t` |
