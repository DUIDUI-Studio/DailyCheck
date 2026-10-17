# Blue-Green Deployment Scripts

> **状态**：v0.3 骨架（2026-10-13 修订）。v0.2 基础上补：env 变量约定（与 deploy_vps.sh 对齐）+ MCP 端口规划 + 工作目录错位修正。
>
> **v0.1 → v0.2 修订依据**：[Prod Agent 反馈](https://scnzlitcwg39.feishu.cn/drive/folder/SnItfxKQcl8h2cdQtllcVmRynsd)（wdg_vps 实测）。
>
> **v0.2 → v0.3 修订依据**：SSH VPS 实测，发现 4 处 v0.2 假设错位（见 §v0.3 patch 修订表）。

## 设计目标

按 [`docs/agent-framework/v0.1.md`](../../docs/agent-framework/v0.1.md) §四，Prod Agent 拉 PR 后起绿环境跑验证，通过后切流量。蓝环境保留为 7 天 standby。

## VPS 真实架构（2026-10-13 实测）

| 项 | 蓝（生产） | 绿（候选） |
|----|----------|----------|
| 服务 | `dailycheck.service`（gunicorn `-w 2 --threads 4 -b 127.0.0.1:8090`） | `dailycheck-green.service`（待建） |
| MCP | `dailycheck-mcp.service`（0.0.0.0:5100） | `dailycheck-green-mcp.service`（待建，0.0.0.0:5101） |
| Env 文件 | `/etc/wdg/dailycheck-mcp.env`（含 `DAILYCHECK_MCP_TOKEN`） | `/etc/wdg/dailycheck-green-mcp.env`（待建） |
| DB | `/var/lib/dailycheck/master.db` + `warehouses/*.db`（多文件） | `/var/lib/dailycheck-green/`（待 cp） |
| 端口 | gunicorn 127.0.0.1:8090 + MCP 0.0.0.0:5100 + nginx :8080 反代 | gunicorn 127.0.0.1:8091 + MCP 0.0.0.0:5101 + nginx upstream weight |
| 写隔离 | 正常写 | DRY_RUN=true（中间件 log-only） |
| 工作目录 | `/root/DailyCheck`（实测，非 `/opt/dailycheck`） | 同 |
| 进程管理 | systemd | systemd |

> **注**：蓝 gunicorn 配置 `-w 2 --threads 4`——2 workers + 1 master = 3 进程。CLAUDE.md 写"2 workers"对，Prod Agent 反馈写"3 workers"误读。

## 当前文件

| 文件 | 用途 | 状态 |
|------|------|------|
| `up.sh` | 起绿环境（cp DB + systemctl **restart** dailycheck-green.service + 健康检查；**绿在役时先切蓝**） | 骨架 + systemd TODO |
| `verify.sh` | 跑八项生产验证 | 骨架（占位报告，exit 0 警告） |
| `promote.sh` | **绿转正**（关 DRY_RUN + DB 指回真库 + daemon-reload/restart + 健康复核；幂等 + 备份 + 失败回滚） | 已实装（2026-10-17 事故后新增） |
| `switch.sh` | 切流量（nginx upstream weight + reload） | 骨架 + systemd TODO |
| `rollback.sh` | 秒级切回蓝（weight=0 + 不停绿进程） | 骨架 + systemd TODO |
| `down.sh` | 清理绿（systemctl stop + 可选 rm DB） | 骨架 + systemd TODO |

## 端口/路由约定

| 路径 | 端口 | 角色 |
|------|------|------|
| 蓝 gunicorn | 127.0.0.1:8090 | 当前生产 |
| 绿 gunicorn | 127.0.0.1:8091 | 待验证 |
| 蓝 MCP | 0.0.0.0:5100 | 现行 MCP HTTP |
| 绿 MCP | 0.0.0.0:5101 | 待验证 MCP HTTP |
| Nginx | 0.0.0.0:8080 | 反代入口，`/etc/nginx/sites-enabled/dailycheck` |
| Nginx upstream | :8090 + :8091 | 灰度切换点（weight 调整） |

## env 变量约定（v0.3 引入 · 与 deploy_vps.sh 模板化对齐）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DAILYCHECK_APP_DIR` | `/root/DailyCheck` | 蓝/绿共享工作目录（deploy_vps.sh 同名） |
| `DAILYCHECK_DB_ROOT` | `/var/lib/dailycheck` | 蓝 DB 根 |
| `DAILYCHECK_SERVICE_UNIT` | `dailycheck` | 蓝 systemd unit（deploy_vps.sh 同名） |
| `DAILYCHECK_HEALTH_URL` | `http://127.0.0.1:8090/health` | 蓝 health URL（deploy_vps.sh 同名） |
| `GREEN_PORT` | `8091` | 绿 gunicorn 端口 |
| `GREEN_MCP_PORT` | `5101` | 绿 MCP 端口 |
| `GREEN_DB` | `/var/lib/dailycheck-green` | 绿 DB 根 |
| `GREEN_SERVICE_UNIT` | `dailycheck-green.service` | 绿 systemd unit |
| `GREEN_MCP_SERVICE_UNIT` | `dailycheck-green-mcp.service` | 绿 MCP unit |
| `GREEN_ENV_FILE` | `/etc/wdg/dailycheck-green.env` | 绿 Flask env 文件 |
| `GREEN_MCP_ENV_FILE` | `/etc/wdg/dailycheck-green-mcp.env` | 绿 MCP env 文件（模板：蓝 `/etc/wdg/dailycheck-mcp.env`） |
| `NGINX_DAILYCHECK_CONF` | `/etc/nginx/sites-enabled/dailycheck` | nginx 配置路径（`up.sh` 活流量守卫另支持 `NGINX_CONF` 覆盖，同值） |
| `BLUE_HEALTH_URL` | `http://127.0.0.1:8090/health` | `up.sh` 绿在役时切蓝前探活蓝 |
| `SYSTEMD_UNIT_DIR` / `GREEN_UNIT_PATH` / `GREEN_MCP_UNIT_PATH` | `/etc/systemd/system` + `dailycheck-green{,-mcp}.service` | `promote.sh` 改写的 unit 文件路径 |
| `SYSTEMCTL_BIN` / `CURL_BIN` | `systemctl` / `curl` | `promote.sh` 命令二进制（单测用 fake） |
| `HEALTH_TRIES` / `HEALTH_INTERVAL` | `30` / `2` | `promote.sh` 健康轮询次数 / 间隔秒 |
| `REPORT_DIR` | `backups/prod-verifications` | verify.sh 报告输出 |
| `EVIDENCE_BASE` | `/var/log/dailycheck` | rollback.sh 取证目录 |
| `BLUE_URL` | `http://127.0.0.1:8090` | verify.sh 蓝基准 URL |
| `GREEN_URL` | `http://127.0.0.1:8091` | verify.sh 绿候选 URL |
| `BLUE_SERVICE_UNIT` | `dailycheck.service` | switch.sh DRY_RUN 守卫检查的蓝 unit |
| `BLUE_ACCESS_LOG` | `/root/DailyCheck/access.log` | switch.sh 落点核验读蓝 access log |
| `GREEN_ACCESS_LOG` | `/var/log/dailycheck/green-access.log` | switch.sh 落点核验读绿 access log |

## 写隔离（决策 1：方案 B）

绿环境通过 `DRY_RUN=true` 环境变量启动。`app.py` + `blueprints/*` 写路径走中间件：

```python
if current_app.config.get("DRY_RUN"):
    log.warning(f"[dry_run] BLOCKED write: {sql}")  # WARNING 级：生产 root logger 默认吞 INFO
    return []  # 或 raise DryRunError
```

中间件需要：
1. `DRY_RUN` 配置读取（`config.py` + 环境变量）
2. blueprints（items / inventory / outbound / production / restock / stocktake）所有 ORM 写路径接入
3. PoC：加 1 个 blueprint 验证后跑全量 pytest（必须仍 16 failed / 718+ passed，零新增）

中间件完成后用 [`prod_dryrun_evidence.sh`](../agent_framework/prod_dryrun_evidence.sh) 验证端到端隔离。

> ⚠️ **写隔离 ≠ 可承接流量**：拦截日志必须保持 **WARNING** 级（`[dry_run] BLOCKED`
> 前缀）——INFO 会被生产日志配置吞掉（2026-10-15 事故取证困难根因）。切流量
> 的 `switch.sh` 已内置 **DRY_RUN 守卫**：承接侧未「转正」会拒跑（exit 3），
> 标准步骤见下方「绿转正」。

## 绿转正（承接生产流量前必做）

绿实例以 `DRY_RUN=true` 启动（写隔离：`db/dryrun.py` 把 INSERT/UPDATE/DELETE
拦成 log-only——HTTP 200、审计照记、DB 不落盘），该状态**只用于验证期**，绝不能
承接生产流量。2026-10-15 生产事故（绿实例接管流量 44 分钟、全站写操作「假成功」）
即由此造成。

### 标准用法（`verify.sh` 全绿之后、切流量之前执行）

```bash
# 部署会话已 acquire 锁的前提下（无锁 → exit 10；人工操作可 DAILYCHECK_DEPLOY_UNLOCKED=1）
./scripts/blue_green/promote.sh --dry-run   # 先看它要做哪三件事（不写文件、不调 systemctl）
./scripts/blue_green/promote.sh             # 真跑：关写隔离 + DB 指回真库 + daemon-reload/restart + 健康复核
```

`promote.sh` 把原来靠人记的三步固化成一条可重放的命令（2026-10-17 事故：漏做转正
= 全站写静默假成功，且绿在役时 `up.sh` 重启绿会把线上实例换回写隔离态）：

1. **备份**：`dailycheck-green.service` / `dailycheck-green-mcp.service`（以及存在的
   `GREEN_ENV_FILE` / `GREEN_MCP_ENV_FILE`）→ `BACKUP_DIR`（默认
   `/var/lib/dailycheck-blue-green-backups`），并写清单供失败回滚；
2. **关写隔离**：app unit 的 `Environment=DRY_RUN=true` 改为 `false`
   ——**无该行则不加**（等于本来就没开，视为已转正）；
3. **DB 指回生产真库**：两个 unit（及存在的 env 文件）的 `DAILYCHECK_DB_DIR=...`
   → `${APP_DIR}/db`（`DAILYCHECK_APP_DIR` 可覆盖）。验证期独立副本
   `/var/lib/dailycheck-green/db` 不再使用。
   ⚠️ 此步后绿具备真实写能力，其后任何流量都会直接写入生产库；
4. `systemctl daemon-reload` → `restart dailycheck-green dailycheck-green-mcp`；
5. **健康复核**：轮询 `:8091/health` 必须 200；`:5101/health` 返回 200 或 401 均视为活
   （MCP 带 token 鉴权，活着的实例对无凭据探针返 401）；
6. 手动复验（可选，等价于脚本步骤 5）：
   `systemctl show dailycheck-green -p Environment --value` 不含 `DRY_RUN=true`；
7. 灰度切流量：`switch.sh green 5 → 50 → 100`（每步观察窗；100%/0% 边界自动做落点核验）。

**幂等**：已转正（无 `DRY_RUN=true` 且 DB 已指向 `${APP_DIR}/db`）→ 打印
`already promoted` 并 exit 0，**不会**再 restart 在役实例（重启在役实例本身就是事故）。

**失败**：任一步失败（文件缺失 / sed 未命中 / daemon-reload / restart / 健康超时）
→ 按备份清单恢复被改文件 + 再 `daemon-reload` + 明确错误信息，exit 1。宁可留在
写隔离态（不接流量），也不留「半转正」。

**退出码**：`0` 成功/已转正、`1` 失败（已回滚）、`10` 无活跃部署锁、`64` 参数错。

**可测试性**：unit / env 路径、`SYSTEMCTL_BIN`、`CURL_BIN`、`BACKUP_DIR`、健康 URL
与节奏参数全部 env 可覆盖（默认为生产值），单测用 fake 命令驱动：
`tests/test_blue_green_promote_guard.py`。

### 绿在役时 `up.sh` 会先切蓝（2026-10-17 事故加固）

`up.sh` 重启绿之前会解析 nginx conf（`NGINX_CONF`，默认
`/etc/nginx/sites-enabled/dailycheck`）判断绿是否正在承流
（`server 127.0.0.1:8091` 非 `down` 且 `weight>0`）：

| 判定 | `up.sh` 行为 |
|------|-------------|
| **serving**（绿在役） | 先探活蓝（`BLUE_HEALTH_URL`，须 200）→ 健康则 `switch.sh blue 100` 把流量挪走 → 再重启绿；蓝不健康 → **exit 1 且不重启绿** |
| **idle**（`down` / `weight=0` / 不在 upstream） | 与加固前一致，直接重启 |
| **unknown**（conf 读不到 / 无 `upstream dailycheck` 块 / `weight` 值不可解析） | **exit 1 拒绝执行**，提示人工核对 nginx 布局 |

不允许静默降级：判断不了「重启会不会打断在役实例」时，脚本选择不动。

**防线（switch.sh 内置，2026-10-15 事故后加固）**：

- **DRY_RUN 守卫**：切换后凡承接流量（weight>0）的实例仍处于 `DRY_RUN=true`
  → 直接拒跑（**exit 3**）并提示先完成绿转正——不会再有 DRY_RUN 实例静默
  接管生产写流量；
- **conf 应用核验**（sed 未命中 → 回滚拒跑）、**reload 失败显式回滚**、
  **落点核验**（100%/0% 边界用 nonce 探针核对流量真实落到目标实例 access log）。
  ⚠️ **探针会重发（Issue #55）**：nginx worker 在 reload 后需 1-3 秒刷新
  in-memory upstream，探针首次命中旧配置属正常——脚本会**换新 nonce 重发**
  （默认最多 5 次，常态化等待预算约 10 秒），命中目标 access log 才算通过；
  回滚判据是「**所有尝试**都没命中目标 log **且**至少一次落在对侧」（仅一次
  落对侧不判死，那是刷新窗口内的正常现象）。初版只发一次探针 → 撞上刷新窗口
  就必然误回滚（生产实测 `switch.sh green 100` 被误判失败，需人工 `sleep 3`
  绕过），勿回退该重发逻辑；`LAND_TRY_MAX=1` 会退回该行为并打 WARNING。
  ⚠️ **前置耦合**：`BLUE_ACCESS_LOG` / `GREEN_ACCESS_LOG` 必须与 nginx 实况一致（默认蓝
  `/root/DailyCheck/access.log`、绿 `/var/log/dailycheck/green-access.log`）；**改 nginx 布局
  或 access_log 路径后必须同步这两个变量**。日志读不到时行为是「无法判定 → 仅告警不阻断」，
  属**显式降级**（不会假绿，但也不会拦下切换），出现该告警请人工核对落点。

回退：`switch.sh blue 100`（绿降 standby，不影响蓝）。

> 若不打算续用蓝绿路径：`down.sh` 清理绿实例即可，无需走转正。

## 待补（v0.3 阶段）

- [ ] `dailycheck-green.service` systemd unit（模板见 up.sh TODO + 蓝 `dailycheck.service`）
- [ ] `dailycheck-green-mcp.service` systemd unit（模板：蓝 `dailycheck-mcp.service` + `EnvironmentFile=/etc/wdg/dailycheck-mcp.env`）
- [ ] `/etc/wdg/dailycheck-green.env` + `/etc/wdg/dailycheck-green-mcp.env`
- [ ] `/var/lib/dailycheck-green/` 目录（cp 自蓝）
- [ ] `app.py` DRY_RUN 中间件 + blueprints 接入（方案 B PoC，1-2d）
- [ ] Nginx `/etc/nginx/sites-enabled/dailycheck` 加 :8091 + :5101 upstream blocks
- [x] `verify.sh` 八项实装（集成 + 模拟用户 + 绿蓝比对 + 回滚演练 + 资源基线 + dry_run 隔离 + DB 隔离 + nginx 入口探活）
- [ ] `scripts/agent_framework/prod_dryrun_evidence.sh` 实装（取消 exit 78）
- [ ] 蓝环境 standby 7 天自动清理 cron
- [ ] `vps_self_heal.py` 监控清单补 `dailycheck-green.service`（Phase 2）

## 使用流程

```bash
# 1. 起绿环境
./scripts/blue_green/up.sh pr-123 abc1234

# 2. 跑八项生产验证（出 templates/05 报告）
#    check 2 需要一次性模拟账号凭据；verify.sh 会在**绿库**内幂等建号
#    （`--admin`，蓝库零写入）——无需事先手工 create-user。
export SIM_USER_USER=sim_user_v1
read -rsp 'SIM_USER_PASSWORD: ' SIM_USER_PASSWORD; export SIM_USER_PASSWORD  # 值现场输入，不入仓库
./scripts/blue_green/verify.sh pr-123
#    ⚠️ 该账号随绿库存在，切流量后会进生产；切完按 down.sh / 账号停用流程处置。

# 3. dry_run 隔离证据采集
./scripts/agent_framework/prod_dryrun_evidence.sh

# 4. 绿转正（关写隔离 + DB 指回真库 + restart）—— 漏这步就是「全站写假成功」
./scripts/blue_green/promote.sh --dry-run
./scripts/blue_green/promote.sh

# 5. 通过后切流量（5% → 50% → 100%）
./scripts/blue_green/switch.sh green 5
./scripts/blue_green/switch.sh green 50
./scripts/blue_green/switch.sh green 100

# 6. 观察窗 30 分钟无异常 → 合 PR
# 7. 蓝环境 standby 7 天后清理
./scripts/blue_green/down.sh --keep-blue
```

> ⚠️ **转正之后不要再直接跑 `up.sh` 起新绿**：绿在承流时 `up.sh` 会先探活蓝并
> `switch.sh blue 100` 把流量挪走再重启绿（见上文「绿在役时 up.sh 会先切蓝」）。
> 若蓝已不可用，`up.sh` 会直接 exit 1 拒绝执行 —— 此时先恢复蓝，别硬重启绿。

## 与现有 deploy_vps.sh 的关系

- `deploy_vps.sh`：手工部署 + 回滚（kill 蓝 gunicorn → 重启），**不是蓝绿**。保留作为应急 / 旧路径。
- `blue_green/*`：自治路径，Prod Agent 主用。**保留蓝 gunicorn 不 kill**（0 中断），起绿 on :8091 → 验证 → 切流量 100% → 蓝降 standby → 绿升为新蓝。
- **过渡期两条路并存**：日常 prod 仍走 deploy 流程；引入新代码（蓝绿目标场景）走 blue_green。
- **env 变量约定已统一**（v0.3）：deploy_vps.sh 的 `DAILYCHECK_*` 与 blue_green 的 `GREEN_*` 在脚本间可共享覆盖。
- 后续 v0.4 目标：`deploy_vps.sh` 改造成"起绿"语义（与 `up.sh` 复用同一份 systemd 启动逻辑），不再 kill 蓝。

## 与 vps_self_heal 的关系

- `vps_self_heal.py` 监控蓝 `:8090` 的 dailycheck.service，**不监控绿 `:8091` / MCP :5100**。
- Phase 2 加 `dailycheck-green.service` 时要确认是否补进 vps_self_heal 检查清单——避免绿挂了没人发现但生产还在走蓝。
- v0.3 阶段无需改 vps_self_heal（绿还没起），但 up.sh 起来后应当给 Eric 发通知（待接入）。

## v0.3 patch 修订表（2026-10-13）

| # | v0.2 错位 | v0.3 修正 | 依据 |
|---|-----------|----------|------|
| 1 | `SOURCE_DIR="/opt/dailycheck"` | `APP_DIR="${DAILYCHECK_APP_DIR:-/root/DailyCheck}"` | ssh wdg_vps 实测：host 无 `/opt/dailycheck` |
| 2 | 没 MCP 端口规划 | 蓝 :5100 / 绿 :5101 + 双 unit + 双 env 文件 | 实测蓝 MCP 监听 0.0.0.0:5100 |
| 3 | 路径硬编码 | 所有路径走 env 变量 + 默认值 | 复用 `scripts/deploy_vps.sh` 已建立的 `DAILYCHECK_*` 约定 |
| 4 | 没提 deploy_vps.sh 模板化 | README 加 env 变量约定表 + deploy_vps.sh 关系强化 | deploy_vps.sh 已规范化 env，覆盖层清晰 |
| 5 | cron 路径 `/opt/dailycheck/...` | `/root/DailyCheck/...`（实测 host） | ssh wdg_vps 实测 |
| 6 | 误标"gunicorn 3 workers" | `-w 2 --threads 4`（实测 systemd 配置） | `systemctl cat dailycheck.service` |

## 安全提示（2026-10-13 实测）

读 `/etc/wdg/dailycheck-mcp.env` 时看到明文 `DAILYCHECK_MCP_TOKEN`。**建议 Eric**：

1. `chmod 600 /etc/wdg/dailycheck-mcp.env`（如未）
2. 评估 token 是否需要 rotate（即使对话历史含 token 痕迹）
3. 同样审查 `/etc/wdg/dailycheck-*.env` 系列文件权限