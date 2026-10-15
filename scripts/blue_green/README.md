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
| `up.sh` | 起绿环境（cp DB + systemctl start dailycheck-green.service + 健康检查） | 骨架 + systemd TODO |
| `verify.sh` | 跑八项生产验证 | 骨架（占位报告，exit 0 警告） |
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
| `NGINX_DAILYCHECK_CONF` | `/etc/nginx/sites-enabled/dailycheck` | nginx 配置路径 |
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

标准步骤（`verify.sh` 全绿之后、切流量之前执行）：

1. **关写隔离**：`dailycheck-green.service` 删除 `DRY_RUN=true`（或改 `false`）；
2. **DB 指回生产真库**：绿两个 unit 的 env 文件（`GREEN_ENV_FILE` /
   `GREEN_MCP_ENV_FILE`）删除或改写 `DAILYCHECK_DB_DIR` → `${APP_DIR}/db`
   （验证期独立副本 `/var/lib/dailycheck-green/db` 不再使用）。
   ⚠️ 此步后绿具备真实写能力，其后任何流量都会直接写入生产库；
3. `systemctl daemon-reload && systemctl restart dailycheck-green dailycheck-green-mcp`；
4. 复验：`systemctl show dailycheck-green -p Environment --value` 不含 `DRY_RUN=true`；
5. 灰度切流量：`switch.sh green 5 → 50 → 100`（每步观察窗；100%/0% 边界自动做落点核验）。

**防线（switch.sh 内置，2026-10-15 事故后加固）**：

- **DRY_RUN 守卫**：切换后凡承接流量（weight>0）的实例仍处于 `DRY_RUN=true`
  → 直接拒跑（**exit 3**）并提示先完成绿转正——不会再有 DRY_RUN 实例静默
  接管生产写流量；
- **conf 应用核验**（sed 未命中 → 回滚拒跑）、**reload 失败显式回滚**、
  **落点核验**（100%/0% 边界用 nonce 探针核对流量真实落到目标实例 access log）。

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

# 4. 通过后切流量（5% → 50% → 100%）
./scripts/blue_green/switch.sh green 5
./scripts/blue_green/switch.sh green 50
./scripts/blue_green/switch.sh green 100

# 5. 观察窗 30 分钟无异常 → 合 PR
# 6. 蓝环境 standby 7 天后清理
./scripts/blue_green/down.sh --keep-blue
```

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