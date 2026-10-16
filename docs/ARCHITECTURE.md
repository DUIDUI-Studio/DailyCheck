# DailyCheck 技术架构

> **本文件是 Eric 拍板的硬性要求**：每次开发前必读，每次 commit 触及架构子系统时必同步。
> 门禁：`scripts/git-hooks/pre-commit`（已装）— 触及子系统但本文件未在同 commit 改动 → 拒绝。
> 辅助：`scripts/arch_touched_sections.py` — 基于 `git diff --cached` 提示需更新章节。
> 维护成本：< 5 分钟/次。**不维护的代价**：下一个开发者必然踩坑。

---

## 目录

1. [项目一句话](#1-项目一句话)
2. [系统拓扑（容器视角）](#2-系统拓扑容器视角)
3. [部署拓扑](#3-部署拓扑)
4. [数据架构](#4-数据架构)
5. [应用层模块图](#5-应用层模块图)
6. [蓝图依赖关系](#6-蓝图依赖关系)
7. [请求生命周期](#7-请求生命周期)
8. [鉴权架构](#8-鉴权架构)
9. [MCP 架构](#9-mcp-架构)
10. [业务领域模型](#10-业务领域模型)
11. [PWA / 移动端](#11-pwa--移动端)
12. [测试架构](#12-测试架构)
13. [关键约定与坑](#13-关键约定与坑)
14. [pre-existing 风险清单](#14-pre-existing-风险清单)
15. [文档索引](#15-文档索引)

---

## 1. 项目一句话

**单租户、多仓、带主数据治理的轻量库存管理系统，对 AI Agent 通过 MCP 协议开放 13 个工具。**

- **目标用户**：连锁餐饮/零售的小企业（5–50 仓规模），现场用手机做日常操作。
- **核心问题**：让一家公司能管 N 个门店/DCN/R&D 的库存，并让 AI Agent 读得到。
- **核心约束**：多租户 PoC 已落地（2026-10-10，轻方案：tenants 注册表 + 三表 tenant_id +
  请求级租户闸；canonical 扇出/订单/MCP 全链路租户化为 PoC 后续阶段）。

---

## 2. 系统拓扑（容器视角）

```mermaid
flowchart TB
    subgraph Browser["浏览器 / PWA"]
        PWA["Service Worker<br/>离线缓存 + manifest"]
        MobileCSS["mobile-first CSS<br/>(1612 行)"]
    end

    subgraph FlaskProc["Flask 进程 (gunicorn -w 2)"]
        AppFactory["create_app()"]
        BP["16 个蓝图<br/>(auth/core/items/canonical/store_ordering/...)"]
        Perm["@require_login / @require_role /<br/>@require_platform_admin / @require_warehouse_type"]
        CLI["Flask CLI<br/>(init-master / create-warehouse / ... / mcp)"]
        Schedule["forecast 定时调度器<br/>(带 fcntl flock)"]
    end

    subgraph MCPProc["MCP 进程 (uvicorn)"]
        AuthMW["AuthMiddleware<br/>Bearer → agent_tokens"]
        Streamable["StreamableHTTP / SSE"]
        Tools["13 个 MCP 工具<br/>(items_list / outbound_create / ... / procurement_hub)"]
    end

    subgraph Storage["SQLite 文件 (bind mount)"]
        Master["master.db<br/>users / warehouses / canonical_items /<br/>forecast_runs / notifications / agent_tokens /<br/>recipe_versions / store_orders / ..."]
        WH1["db/warehouses/wh_001.db<br/>(品类 / 库存品 / stock_movements /<br/>restock_requests / outbound_requests /<br/>products / product_bom / production_runs /<br/>recipes / recipe_items / audit_log)"]
        WH2["db/warehouses/wh_002.db<br/>(同 schema，独立数据)"]
        WHN["... wh_003/004/006/010/rd_001"]
    end

    Browser <-->|HTTP :8080| FlaskProc
    Browser <-->|HTTP :5100<br/>Bearer token| MCPProc

    FlaskProc -->|sqlite3| Master
    FlaskProc -->|sqlite3 per-request| WH1
    FlaskProc -->|sqlite3 per-request| WH2
    FlaskProc -->|sqlite3 per-request| WHN

    MCPProc -->|sqlite3 read-only| Master
    MCPProc -->|sqlite3 read-only| WH1
    MCPProc -->|sqlite3 read-only| WH2
    MCPProc -->|sqlite3 read-only| WHN
```

---

## 3. 部署拓扑

### 3.1 Dev 环境

```mermaid
flowchart LR
    Host["macOS host<br/>(~/Documents/GitHub/DailyCheck)"]
    Container["wdg-systemd 容器<br/>(systemd 进程管理器)"]
    FlaskUnit["dailycheck-app.service<br/>:8080 --debug (reloader)"]
    MCPUnit["dailycheck-mcp.service<br/>:5100 HTTP/SSE"]
    DB1["db/master.db"]
    DB2["db/warehouses/*.db"]

    Host -->|bind mount /opt/dailycheck| Container
    Host -.->|同一份 SQLite 文件| DB1
    Host -.->|同一份 SQLite 文件| DB2
    Container --> FlaskUnit
    Container --> MCPUnit
    FlaskUnit --> DB1
    FlaskUnit --> DB2
    MCPUnit --> DB1
    MCPUnit --> DB2
```

**关键**：bind mount 实时同步，**改 .py Flask 自动 reload**，**MCP 进程无 reloader**，改 `mcp_server/*.py` 必须手工 `systemctl restart dailycheck-mcp`。

### 3.2 Prod 环境

```mermaid
flowchart LR
    GitHub["GitHub Actions<br/>(.github/workflows/ci.yml)"]
    VPS["VPS 112.124.18.246:33756 root<br/>/root/DailyCheck"]
    Gunicorn["gunicorn -w 2 --threads 4<br/>-b 127.0.0.1:8090 app:app<br/>(**非 wdg-systemd**)"]
    SQLite["SQLite 文件 (非 bind mount)"]
    Secrets["/etc/dailycheck/app.env<br/>0600 — 强 secret + DAILYCHECK_ENV=production"]

    GitHub -->|SSH: tar → releases/ → scripts/deploy_vps.sh<br/>（备份→切换→migrate→健康检查→失败自动回滚）<br/>**前置 CI test 通过**| VPS
    VPS --> Gunicorn
    Gunicorn --> SQLite
    Secrets -.->|EnvironmentFile| Gunicorn
```

### 3.3 ⚠️ 部署重大差异（必读）

| 维度 | Dev | Prod |
|------|-----|------|
| **时区** | UTC | **Asia/Shanghai (CST+8)**，unit 未设 TZ |
| **数据库路径** | bind mount（容器/host 同一文件） | 本地文件系统 |
| **进程管理器** | systemd in container | gunicorn -D（无 systemd） |
| **Flask reload** | `--debug` 自动 reload | 无 |
| **MCP 进程** | systemd unit | **未部署**（MCP 只在 dev 跑） |
| **部署方式** | mount + edit | `ci.yml` test 通过后走 `scripts/deploy_vps.sh`：sqlite 热备全库 + 代码备份 → rsync 切换（`*.db`/log/venv 永不触碰）→ `flask migrate up` → systemd 重启 → `/health` 探活 → 失败自动回滚代码并重启。**db/ Python 包必须进代码备份**（2026-10-10 dev 演练踩坑） |
| **备份/DR** | 无 | `scripts/backup_all.sh`（cron 每日，integrity_check + tar + 可选异地）+ `scripts/verify_restore.sh`（每周还原演练）+ `docs/runbook-dr.md`（RPO=24h/RTO=1h） |
| **入口端口** | :8080 / :5100 | :8090（仅 Flask） |

### 3.4 蓝绿部署（Phase 1 实装，2026-10-13 起）

Prod 专用（dev 不涉及）。`scripts/blue_green/` 5 脚本：`up.sh`（起绿：DB `cp -a` + systemd **restart** + 30×2s 健康检查）→ `verify.sh`（八项生产验证，报告落 `backups/prod-verifications/`）→ `switch.sh`（nginx upstream weight/down 切流量，`nginx -t` 验证 + 失败自动还原备份）→ `rollback.sh`（秒级切回蓝 + journalctl/DB 取证）→ `down.sh`（绿下线，`--keep-blue` 保留 DB 7 天）。端口：蓝 :8090/:5100，绿 :8091/:5101。全脚本支持 `--dry-run`（只回显不执行）。

**铁律**：
- **nginx 备份绝不写 `sites-enabled/`** —— 主配置 `include sites-enabled/*` 会把 `.bak` 当配置加载 → duplicate upstream 永久 fail（Issue #24 Bug A）。统一落 `BACKUP_DIR`（默认 `/var/lib/dailycheck-blue-green-backups/`）。
- **`verify.sh` 禁用 `set -e`** —— check 失败由 `OVERALL_PASS` 跟踪，`trap _write_report EXIT` 兜底保证任何失败路径都写报告（Issue #24 Bug B）。
- **check 1 禁止兜底** —— 真跑 pytest（`-rf` 产出 FAILED 行）+ 复用 `scripts/check_known_failures.py` 判「失败集 ⊆ known_failures.txt」，不允许把失败兜成 PASS。
- **check 2 禁止静默跳过** —— `scripts/playwright/sim_user.py`（纯 stdlib）真走 登录→选仓→总览→库存→健康报表 5 路径（含 CSRF 回填）；凭据走 `SIM_USER_USER` / `SIM_USER_PASSWORD` 环境变量，缺失直接 FAIL。末条 `/report/health`（Issue #38 补）需 **manager+ 账号**（平台 admin 放行），403 按失败处理不降级；同时校验落点须停在 `/report/health`——会话丢失会 302 到 `/login` 再返回 200，只看 status 会假绿。
- **check 2 凭据必须 provision 进「绿」库（Issue #34）** —— sim_user 的 HTTP 凭据校验发生在**绿进程持有的库**上，而 verify.sh 跑的 wrapper shell 不在绿 unit 环境里。因此 `_ensure_sim_user_in_green_db()` 必须用**显式 `DAILYCHECK_DB_DIR=${GREEN_DB}` 覆盖**（子 shell 内赋值，禁止继承调用者环境）幂等 ensure 账号，且必须带 **`--admin`**（非 admin 用户在选仓页只看到 `warehouse_users` 绑定的仓，而 create-user 不写绑定 → 空选仓页 → 探针路径 3 必失败）；探针前再复核「绿库有该账号 + 密码匹配 + 未停用（`is_active`）+ 有可见仓」（覆盖 up.sh 快照里同名不同密码 / 已停用 / 非 admin 账号的情形），并在**写库之前**先用「路径形态 + inode 相异」双闸自证 `${GREEN_DB}` 不是蓝库（check 7 排在 check 2 之后，误配时否则会先把一个已知口令的 admin 写进 production 真库）。**裸跑 `flask create-user` 会写蓝库（production 真库）** —— 绿库查无此人导致路径 2 恒 401，实测见 Issue #34；任何新增的「verify 里要碰 DB 的步骤」都必须照此 pin env 到目标侧库。
- **DB 隔离（Issue #28）** —— 蓝绿不许共享物理库：`config.py` 在 import 期读 `DAILYCHECK_DB_DIR` env（Flask 与 MCP 同源覆盖）；`up.sh` 从真库 `${APP_DIR}/db` 用 `cp -aL` 解软链拷独立副本，且守卫绿 unit 必须声明 `DAILYCHECK_DB_DIR` 与拷贝目标一致，否则拒启；`verify.sh` check 7 取三重证据（绿进程 environ 声明 + master.db 非软链 + 蓝/绿 inode 相异）。**改部署布局（软链/数据目录迁移）必须保持这三个语义**。**语义边界（勿与「绿转正」冲突）**：本条约束的是**验证期**（绿以 `DRY_RUN=true` 承接 0 流量）；一旦按下方 DRY_RUN 铁律走「绿转正」承接生产流量，绿必须把 `DAILYCHECK_DB_DIR` 改指生产真库 `${APP_DIR}/db` —— 转正后蓝绿同库是**设计预期**（单写者：蓝 standby 不写），此时 check 7 的「inode 相异」不再适用，写安全改由 DRY_RUN 守卫保证；转正后重跑 `verify.sh` 若 check 7 报红，属预期而非回归。**同源影响 check 2**：`_ensure_sim_user_in_green_db()` 之所以能靠「路径形态 + inode 相异」双闸拒绝写蓝库，前提同样是验证期的库隔离；转正后该前提消失（`GREEN_DB` 变量值仍在、实际库已是真库，双闸退化为假保证）。因此**转正后如需复跑 `verify.sh`，应先在蓝绿框架下回蓝（`switch.sh blue 100`）并恢复验证期布局**，或在明确「已知同库」的前提下判读 check 2 / check 7 的红。
- **nginx 单 upstream 蓝绿同池（Issue #32）** —— `location` 的 `proxy_pass` 永远指向合并的 `dailycheck` / `dailycheck-mcp` upstream，蓝绿是同 upstream 内的两个 `server` 节点；切流量只改 server 行的 `weight=N` / `down` —— **switch.sh 不动 location 也不新增 `dailycheck-blue/green` upstream**（旧布局下 location 硬编码，upstream 切了真实流量不动 → 502 必现，Issue #32 实测）。三道防线：① `switch.sh` 头部布局守卫 grep `proxy_pass http://dailycheck-(blue|green)` 命中旧布局直接拒跑（exit 2），强制 Prod Agent 先按 `scripts/blue_green/nginx-site.conf` 模板重构；② `switch.sh` 尾部 post-switch 探活，过 nginx 入口（默认 `http://127.0.0.1:8080`，可用 `NGINX_ENTRY` 环境变量覆盖，dry-run 路径也透传）取 `http_code`，5xx/000 自动 cp 备份回滚 + reload；③ `verify.sh` check 8 终态保险，再过一遍入口探活捕获 reload → observe window 期间的状态漂移。**改 nginx 布局必须保持单 upstream 模型**，新增独立 upstream 必须同时承担布局守卫与探活的改造。
- **DRY_RUN 实例绝不承接流量（Issue #41，2026-10-15 P0 事故）** —— 绿实例的 `DRY_RUN=true` 写隔离只允许存在于验证期：窗口期曾发生「绿实例接管生产流量 44 分钟、全部写操作被吞」（HTTP 200 / 审计照记 / DB 不落盘）。`switch.sh` 四道防线：① **DRY_RUN 守卫** —— 切换后凡承接流量（weight>0）的实例检测到 `DRY_RUN=true`（取证次序：进程 `/proc/<pid>/environ` → unit `systemctl show Environment`；无法判定仅告警）即拒跑（exit 3），必须先按 README「绿转正」（关 DRY_RUN + DB 指回真库 + restart）再切；② **conf 应用核验** —— sed 后逐行核对 4 条 server 行与期望一致（sed 未命中时 `nginx -t` 仍过、reload 仍「成功」而权重根本没变，静默失败）；③ **reload 失败显式回滚**（旧版 `set -e` 直退会留下 conf/运行态分歧）；④ **落点核验** —— 100%/0% 边界用带 nonce 的探针核对流量真实落到目标实例 access log（对侧命中=回滚，日志不可读=仅告警）。**「绿转正」标准步骤见 `scripts/blue_green/README.md`**。
- 端口 0 权重不被 nginx 1.24 接受，用 `down` 关键字标记不参与负载。
- **起绿必须 `systemctl restart`（Issue #38）** —— `up.sh` 步骤 4 对绿 Flask / MCP 两个 unit 一律 `restart`（未跑则起、在跑则重启）：`start` 对**已在运行**的 unit 是 no-op，而步骤 3 已把新 DB 拷进绿库 → 变成「新 DB + 旧代码」，verify / switch 判定的对象一半新一半旧。两处 restart 必须带 `|| true`（`set -euo pipefail` 直退会跳过步骤 7 的 journalctl + DB 取证，失败现场就丢了），失败交给步骤 5/6 健康检查判红；另须在 restart 前回显 `systemctl is-active` 的 pre-state（「新库 + 旧码」只有对照 pre-state 才看得出），该回显要绑到 live 分支的命令本体而不是 dry-run 的 echo 文案。改 up.sh 启动段必须同时保住这四个语义（restart / `|| true` / 取证可达 / pre-state 真实查询）。
- 单测：`tests/test_blue_green_implementation.py`（80 用例，bash -n / dry-run 回显 / 参数解析 / 关键不变量 / sim_user 假服务器全流程（含 Issue #38 路径 5：500 拦截 / 403 提示 / 落点校验防假绿）/ 绿库账号 provision（Issue #34：env pin 覆盖语义 + `--admin` + 可见仓/停用/密码三条复核 + 写库前「拒绝写蓝库」双闸（路径形态 + inode），函数按标记抽取真跑）/ config env 覆盖；关键断言逐条经变异验证）。**夹具凭据一律不写字面量**：口令用 `secrets.token_hex()` 运行期生成（`_SIM_PW` / `_OTHER_PW`）——`password="<字面量>"` 会被 GitGuardian 的 Generic Password 规则判为泄露、直接把 PR 检查打红，而断言从不依赖取值。另有 `tests/test_blue_green_dryrun_guard.py`（10 用例：DRY_RUN 守卫拦截/放行/无法判定三态、conf 应用核验、落点核验（命中/对侧回滚/不可读告警）、灰度跳过；live 全流程跑 switch.sh，PATH 前置 fake systemctl/nginx + 本地假入口服务器；关键断言逐条经变异验证）。两文件合计 90 用例（80 + 10）。
- **协作流程与门禁**：Dev 提 PR 必含「生产侧事项」段（`check_reports.py` 强制，缺失即红），只写规范流程粒度不写操作脚本；**merge 由 Prod Agent 判定执行**（CI 全绿 + 独立审查，异议打回），红线清单见 `docs/agent-framework/workflow.md` §四。

### 3.5 PR Webhook 接收器（2026-10-15 实装）

Dev Agent 提完 PR 后，**GitHub repo → webhook → VPS `dc-webhook-receiver` → `inbox.jsonl` → `dc-inbox-watcher`（≤5s 触发）→ Prod Agent 入口 → 跑 merge 决策**。秒级通知，替代 30 分钟轮询。

**组件**（`scripts/blue_green/webhook_receiver.py`，Flask 127.0.0.1:8443）：
- **HMAC-SHA256 验签**（`X-Hub-Signature-256` + `WEBHOOK_SECRET` env）—— 拒伪 + 拒重放，constant-time 防时序攻击
- **事件过滤**：`pull_request`（action 仅 `{opened, synchronize, reopened}`，`closed` / `edited` / `labeled` 等跳过）+ `check_suite`（action 仅 `completed`，且仅「有 PR 关联 + conclusion=success」落盘 —— CI 全绿秒级唤醒快通道，2026-10-16 增）
- **幂等**：按 `X-GitHub-Delivery` 去重（GitHub 会重试 6 次，receiver 必须幂等否则 inbox 写脏）
- **反压**：`inbox_size ≥ MAX_INBOX_LINES`（默认 1000）返回 503，GitHub 暂停重试（指数退避 1s/2s/.../1h）
- **inbox 落盘**：每行 JSON，append-only，含 `delivery_id / event / action / pr_number / pr_title / pr_head_sha / pr_base_ref / pr_author / repo_full_name / received_at_ms`；`check_suite` 记录追加 `check_conclusion / check_head_branch / check_suite_id`（payload 没有的字段如 pr_title / pr_author 填空串，字段形状与 pull_request 记录对齐）

**部署**（owner / Prod Agent 手动，不进 Dev 自动化）：
- **前置：443 入站必须放行**（2026-10-16 实测踩坑）——残留 ufw 链 + `INPUT` policy DROP 布局下，`ufw-user-input` 须含 `tcp dpt:443 ACCEPT` 且 `/etc/iptables/rules.v4` 同步补行；**本机 `curl` 走 `lo` 接口被直通——200 不能证明外网可达**，必须外部 vantage 复验（详见手册 §2/§4）
- DNS：`hook.kjtang.cn` A 记录 → 112.124.18.246（与主站同 IP）
- 证书：hook **独立**阿里云免费 DV 证书（单域名，`/etc/nginx/ssl/hook.kjtang.cn.{pem,key}`，90 天轮换）——免费证书不能给主证书加 SAN，主站证书不动
- systemd：`deploy/systemd/dc-webhook-receiver.service`（监听 127.0.0.1:8443，单 worker，gunicorn，Restart=always 5s）
- nginx：`deploy/nginx/hook.kjtang.cn.conf` 部署为**独立文件** `/etc/nginx/sites-enabled/hook.kjtang.cn`（与蓝绿 `switch.sh` 解耦：布局守卫只检查 dailycheck 文件；443 TLS 终结 + proxy_pass，limit_req 30r/s 防滥刷；`limit_req_zone` 只能放 http 上下文，勿放 `server{}` 内）
- WEBHOOK_SECRET：32 字节强密码，写 `/etc/dailycheck/webhook-secret`（600 root），**不出现 git 仓库 / 文档 / prompt**（仅 systemd unit `EnvironmentFile=` 引用 + GitHub repo webhook 设置）
- GitHub repo：Settings → Webhooks → Add，URL `https://hook.kjtang.cn/webhook`，Secret 同上，Events 勾 **"Pull requests" + "Check suites"**（后者 = CI 完成快通道，2026-10-16 增）

**生产侧操作手册**：`docs/2026-10-15-webhook-setup.md`（50-70 分钟手动部署；DNS / **443 放行核验** / 申请 hook 独立证书 / systemd / nginx reload / GitHub 配 webhook + 端到端验证）。

**测试**：`tests/test_webhook_receiver.py`（19 用例：HMAC 三态 / event 过滤 / action 白名单 / 幂等 / 反压 / inbox 字段完整性 / `/health` 端点 / check_suite 六态（success 落盘 + check_* 字段 / 非 success 拒绝 / 无 PR 拒绝 / 非 completed 拒绝 / action 白名单分叉 / 幂等）；全部经变异验证真 FAIL —— 删 HMAC 验签 / 删反压 / 删幂等 / 删 conclusion 过滤 / 删 no_pr 过滤 / 白名单误加 opened 各自对应测试 FAIL）。

**Prod Agent 集成（2026-10-16 实装，Issue #47）**：`scripts/prod_agent/inbox_watcher.py` 常驻订阅 `inbox.jsonl`，新条目 **≤5s** 内触发 `--trigger-cmd`（事件 JSON 原样走 stdin；样例 `deploy/scripts/dc-notify-prod-agent.sh` 把事件投给 Prod Agent 入口，默认指向 Hermes webhook 订阅）。要点：

- **tail 语义**（纯 stdlib、跨平台）：轮询 `stat(inode, size, mtime)` 检测变更（默认 0.5s 间隔），只处理以 `\n` 结尾的完整行（半行留待下轮）——不用 inotify（Linux-only）、不引三方库
- **去重语义**：全文件扫描 + `handled_ids` 状态集合（原子写 `inbox_watcher_state.json`）—— 重启 / rotate 换 inode / 停机期间新增等任何时序下，每条 `delivery_id` 至多触发一次、未触发过的必然补触发
- **机械过滤**（纵深防御）：仅 `repo_full_name == ericmr1981/DailyCheck` 且 action 属白名单（unit 显式传 `--actions opened,synchronize,reopened,completed`，`completed` 供 check_suite 事件使用）的条目触发；draft 等业务语义交 Prod Agent 判定，本脚本不预判
- **失败重试**：触发非 0 退出 / 超时 → 指数退避（1s→60s 封顶）+ WARNING 留痕，绝不静默丢弃。⚠️ **已知限制（Issue #51）**：重试被主循环的「inbox 文件签名变化」gating——inbox 无新行时退避条目不会自行重试（2026-10-16 部署实测延迟 1h，直到下一事件写入才连带补发）；由下方兜底轮询保底
- **兜底轮询（2026-10-16 实装，§3.9）**：Prod 侧 Hermes cron 每 30 分钟跑 `dailycheck_pr_poll.py`（repo 同源 `deploy/scripts/`）直接查 `gh pr list --state open`——webhook 快通道整体故障时的保底发现通道；无 open PR 零 stdout（agent 回 `[SILENT]` 无投递），有 PR 走同一 `dailycheck-prod-agent` skill 判定
- **CI 完成快通道（2026-10-16 增）**：`check_suite`（CI 全绿）事件走同一 inbox → watcher 链路，`dc-notify-prod-agent.sh` 按事件类型路由订阅（check_suite → `dailycheck-ci`，其余 → `dailycheck-pr`）——CI 一绿即触发 Prod Agent 判定 merge（此前 merge 环节需等兜底轮询接棒，最坏 ≤30 分钟）。中间态（suite 绿但两必需 check 未齐）由 Prod Agent 回 `[SILENT]` 抑制投递。⚠️ webhook 通道的 `[SILENT]` 抑制 = gateway `gateway/platforms/webhook.py` 的**本地补丁**（上游出厂仅 cron 投递支持），Hermes 重装/升级后需重打，见手册 §3.8 步骤 3.5
- **归档（防反压挂死）**：`scripts/prod_agent/inbox_rotate.py` + 每日 timer 把 >7 天旧行迁到 `inbox.archive.jsonl`（rename+重建+收敛回填，与 receiver 并发不丢行）—— inbox 是反压闸门（≥1000 行 receiver 返 503），只增不减会挂死新通知
- **部署**：`deploy/systemd/dc-inbox-watcher.service`（Restart=always / 5s）+ `dc-inbox-rotate.{service,timer}`（每日 00:30）。⚠️ 两个要点（2026-10-16 实测）：unit 的 `ProtectHome` 必须 ≤ `read-only`（`=true` 屏蔽整个 `/root` → `203/EXEC` 无限重启）；触发命令走 Hermes 时必须用 venv 绝对路径 `/root/hermes-agent/.venv/bin/hermes`（`#!/usr/bin/env python3` wrapper 在 watcher 的 PATH 下会解析到 DailyCheck venv，缺 yaml）。手册见 `docs/2026-10-15-webhook-setup.md` §3.8/§3.9
- **测试**：`tests/test_inbox_watcher.py`（30 用例：完整行/半行/U+2028 不误切 / 过滤 / 去重（重启不重复、停机补触发）/ 失败退避 / rotate 七态 / 真实子进程 ≤5s 端到端 / 确定性「旧 fd 窗口」收敛回填；关键断言经变异验证真 FAIL —— 删去重 / 删半行等待 / 失败走成功分支 / 删尾部回填 / 边界反转各自对应测试 FAIL）

### 3.6 项目任务 Dashboard 自动生成（2026-10-16 起）

**目标**：每次 PR webhook 触发时，自动拉 gh + git 状态渲染 `dashboard.html` + 落 SQLite 快照到历史库——替代之前的"手写一次 / 不动"快照，让状态有真正的"上次 vs 现在"差。

**组件**：
- **核心包** `scripts/dailycheck_dashboard/`（dev 端，可 commit）：
  - `__main__.py`：CLI 三子命令 — `render`（拉数据 + 渲染 + 落 db）/ `history`（列最近 N 快照）/ `diff`（最近两次 KPI 对比）
  - `db.py`：SQLite 封装（`snapshots` 主表 + `issues_snapshot` / `prs_snapshot` / `worktrees_snapshot` 三个子表 + `schema_version` 幂等迁移；autocommit 模式 + WAL + `synchronous=NORMAL`）
  - `fetchers.py`：gh CLI 包装（`issue list --state all --json` / `pr list --state all --json`，字段驼峰→snake_case 规范化）+ git worktree 解析（porcelain 格式）+ inbox 统计
  - `render.py`：HTML 渲染（`string.Template` 内嵌 CSS，零三方依赖，KPI 卡片差异行用 `↑N` / `↓N` / `→` 三态箭头）
  - **零外部依赖**：纯 stdlib（`sqlite3` / `subprocess` / `json` / `pathlib` / `string.Template`）
- **触发 listener** `deploy/scripts/dailycheck_dashboard_on_pr.py`（VPS 端，systemd 拉起）：订阅 `inbox.jsonl`，收到 PR 事件（`{opened,synchronize,reopened}`）→ 调 `python -m scripts.dailycheck_dashboard render --trigger-kind pr_xxx --trigger-event-id <delivery_id>`。**与 `dc-inbox-watcher` 完全独立、互不干扰**（各自一份 handled_ids state-file；同一 inbox.jsonl 多读者，写者只有 receiver）
- **systemd 单元** `deploy/systemd/dc-dashboard-generator.service`（套 `dc-inbox-watcher.service` 模板：`Restart=always` / `RestartSec=5` / `ProtectHome=read-only` / `ReadWritePaths=/var/lib/dailycheck-dashboard,/var/lib/dailycheck-prod-agent`；触发命令走 venv 绝对路径）

**数据布局**：
- **SQLite db**（VPS）：`/var/lib/dailycheck-dashboard/dashboard.db` —— 单文件、`synchronous=NORMAL` 适配 flush 频次
- **HTML 快照**（VPS）：`/var/lib/dailycheck-dashboard/snapshots/<id>.html` + `/var/lib/dailycheck-dashboard/latest.html`（latest 同步刷新供 nginx / curl 读）
- **dev 副本**（可选）：脚本 `--db` / `--html-dir` / `--html-latest` 参数全部可覆盖；Mac 本地手动跑也走同一份代码
- **dashboard.html 不入 git**：`.gitignore` 已隐式 ignore `reports/` 整目录，dev 端手写产物天然不污染 git history

**触发链路**（与 §3.5 复用 + 独立 listener）：
```
PR webhook → dc-webhook-receiver → inbox.jsonl (append)
                                ↓
                  ┌──────────────┴─────────────┐
                  ↓                             ↓
        dc-inbox-watcher (已有)        dc-dashboard-generator (新)
        → 触发 Prod Agent              → 调 dashboard render
        → 决策 merge / HOLD            → 写 db + 刷 latest.html
```

**KPI 字段**（每次 render 都重算，存进 `summary_json` 用于 diff）：
- `open_issues` — `state=OPEN` 的 Issue 数
- `merged_7d_total` — 距今 ≤7 天合并的 PR（按 `merged_at` 严格过滤，不是 gh 总 merged 数）
- `worktrees_total` — 当前 `git worktree list --porcelain` 行数
- `closed_unmerged` — `state=CLOSED` 的 PR 数（含历史停滞 DRAFT）

**diff 子命令**：列最近两次快照的 KPI 差值（如 `open_issues: 4 → 5 (+1)`）。dashboard.html KPI 卡片底部也直接显示 `↑N` / `↓N` / `→` 差异箭头，**v1 不做完整差异视图**（成本/价值不匹配）

**为什么 db 放 VPS 不放 dev**：
1. 触发在 VPS 端，db 与触发同侧 = 零网络跳转
2. 避免 dev 多机器 db 漂移（Eric 现有 Mac + 任何未来的 dev 容器都只读不写）
3. 路径不在 git 仓库内 = 不污染 `db/`，不污染 git history

**设计要点**：
- **inbox 多 listener 共存**：dashboard trigger 跟 inbox-watcher 共享同一 inbox.jsonl，**只读不写**，互不阻塞。inbox 的写者仍只是 receiver（append-only）
- **handled_ids 独立 state**：dashboard trigger 用 `dashboard_trigger_state.json`（与 inbox-watcher 的 `inbox_watcher_state.json` 物理隔离），同一 delivery_id 各自去重
- **退避重试**：触发非 0 / 超时 → 指数退避（2s→120s 封顶，比 inbox-watcher 的 1s→60s 更宽容，因 render 偶尔慢）+ WARNING 留痕
- **run-as-unit**：单实例（Type=simple，inbox append-only 无并发需求）；`MemoryMax=512M`（dashboard render 子进程 + gh 子进程峰值）

**响应式排版（2026-10-16 起，手机 / iPad 适配）**：
- 产物为**单文件自包含 HTML**，内嵌 CSS 三档断点：**≤480px 小屏手机** / **≤760px 手机**（KPI 2 列、任务卡单列、nav 单行横滚、字号与留白收紧、`env(safe-area-inset-bottom)` 防 iPhone 底栏遮挡）/ **≤1024px 平板档**（iPad 竖屏仅留白微调，KPI 保持 4 列桌面布局）
- **表格横滚容器 `.tbl`**：所有 `<table>` 由渲染函数包 `<div class="tbl">`（`overflow-x:auto` + 小屏 `min-width`）——页面自身永不横向溢出（375 / 414 / 768 / 1024 四档实测 `scrollWidth == innerWidth`）
- 网格用 `minmax(0,1fr)` 而非裸 `1fr`（防长内容撑破列）；长文本 `overflow-wrap:break-word`；行内徽章改为 `flex-wrap` 容器（防 `white-space:nowrap` 徽章撑爆窄屏）

**手动出活**（验证用）：`python3 -m scripts.dailycheck_dashboard render --trigger-kind manual` —— 与 PR 触发的产物格式完全一致，仅 `trigger_kind` 字段差异

**部署（owner 手动）**：
1. `mkdir -p /var/lib/dailycheck-dashboard/snapshots` + `chown -R root:root /var/lib/dailycheck-dashboard`
2. `cp deploy/scripts/dailycheck_dashboard_on_pr.py /usr/local/bin/dc-dashboard-trigger` + `chmod 755`
3. `cp deploy/systemd/dc-dashboard-generator.service /etc/systemd/system/`
4. `systemctl daemon-reload && systemctl enable --now dc-dashboard-generator.service`
5. `journalctl -u dc-dashboard-generator -f` 跟日志；触发链路手测：append 一行 mock JSON 到 `inbox.jsonl`，观察 journal + `/var/lib/dailycheck-dashboard/latest.html` 刷新

**已知限制**：
- ⚠️ **dashboard 内容只覆盖实时数据章节**（Issue / PR / Worktree / Inbox）—— 静态章节（"模型用量说明" / "专家使用" / "风险与遗留"）按 v1 暂不纳入，原因是这些信息更迭慢 + 写死在 dashboard 会与 MEMORY.md / docs/ 形成单一事实漂移。v2 再考虑拉 MEMORY.md 末段 / agent-framework 元数据
- ⚠️ **inbox.jsonl rotate 后的历史不在 dashboard 视角内**：rotate 把 >7 天旧行迁到 `inbox.archive.jsonl`，dashboard 启动时只扫当前 inbox（`lines` / `deliveries` 数会显示下降）。rotate 前后的连续性需要查 archive
- ⚠️ **diff 视角 v1 只到 KPI 行**：`history` / `diff` 子命令能查完整历史，但 dashboard.html 自身不展示「上次 vs 这次」对比视图（KPI 卡片底差异行是 v1 唯一差视图）

---

### 3.7 报告 HTML 视图（手机 / iPad 阅读，2026-10-16 起）

**目标**：把 agent-framework 报告（Markdown）渲染成**自包含、响应式**的 HTML —— 供手机 / iPad 直接打开阅读；**Markdown 原件为准，HTML 只是只读视图产物**（Markdown 不带版式，窄屏可读性由渲染器决定、不可控；HTML 才能把版式握在手里）。

**工具**：`scripts/agent_framework/report_html.py`（依赖 `pandoc` 作为渲染引擎；缺失时退出码 64 + 安装指引）

**命令**：
```bash
# 转换（目录递归全部 *.md，跳过隐藏目录）
python3 scripts/agent_framework/report_html.py convert reports/ issue-reports/

# 生成索引页（自动先转换缺失 / 过期的 HTML）
python3 scripts/agent_framework/report_html.py index reports/ issue-reports/ -o reports/index.html
```

**产物**：
- `reports/*.html` / `reports/pr-<n>/04-*.html` / `issue-reports/*.html` —— 与被渲染的 md **同目录同名**（`X.md` → `X.html`）
- `reports/index.html` 聚合入口：**📊 仪表盘**（置顶）+ **📋 PR 门禁报告** + **🔍 Issue 巡检报告** 三组，条目按 mtime 倒序
- ⚠️ 全部为视图产物（**不入 git**，与 dashboard.html 同约定）；重新生成幂等覆盖

**技术要点**：
- 渲染 = `pandoc -f gfm -t html5 --wrap=none` + 后处理（`<table>` 自动包 `.tbl` 横滚容器）
- 版式与 §3.6 dashboard **同一视觉语言**：同一 `:root` 变量、同款三档断点（≤480 / ≤760 / ≤1024）、同款表格与代码块样式
- 标题提取：**跳过代码块**（防 `# shell 注释` 误判为一级标题），级联 `# ` → `## ` → 文件名
- 跨目录链接用 `os.path.relpath` 相对索引页计算（`reports/index.html` → `../issue-reports/2026-10-16.html` 可跳转）

---

## 4. 数据架构

### 4.1 整体形态

```
db/
├── master.db                          # 单文件，全局（含 tenants 注册表）
├── warehouses/                        # 租户 1（存量单租户）的仓
│   ├── wh_001.db  (中央仓 storefront)
│   ├── wh_002.db  (新世界 storefront)
│   ├── wh_003.db  (新天地 storefront)
│   ├── wh_004.db  (韩国街 storefront)
│   ├── wh_006.db  (浦发时光里 storefront)
│   ├── wh_010.db  (泰柯富阳 storefront)
│   ├── wh_000.db  (配送中心 distribution_center)
│   └── rd_001.db  (研发 rd)
└── tenants/<tid>/warehouses/*.db      # 租户 N（PoC 起新租户的放置路径）
```

### 4.2 master.db 核心表（ER 简图）

```mermaid
erDiagram
    USERS ||--o{ WAREHOUSE_USERS : "per-warehouse role"
    USERS ||--o{ AGENT_TOKENS : "created_by"
    USERS ||--o{ NOTIFICATIONS : "user_id"
    WAREHOUSES ||--o{ WAREHOUSE_USERS : "warehouse_id"
    WAREHOUSES ||--o{ STORE_ORDERS : "store / dc"
    CANONICAL_CATEGORIES ||--o{ CANONICAL_ITEMS : "category_code"
    CANONICAL_ITEMS ||--o{ CANONICAL_ITEM_ALIASES : "alias"
    CANONICAL_ITEMS ||--o{ CANONICAL_CLAIM_REQUESTS : "claim"
    CANONICAL_ITEMS ||--o{ CANONICAL_CONFLICTS : "freeze on fanout"
    CANONICAL_ITEMS ||--o{ CANONICAL_PUBLISH_EVENTS : "fanout"
    CANONICAL_ITEMS ||--o{ STORE_ORDER_CART_ITEMS : "by canonical_id"
    CANONICAL_ITEMS ||--o{ STORE_ORDER_ITEMS : "by canonical_id"
    RECIPE_VERSIONS ||--o{ RECIPE_PUBLISH_EVENTS : "rd → storefront"
    ITEM_PUBLISH_EVENTS ||--o{ ITEM_PUBLISH_EVENT_ITEMS : "rd → storefront"
    AGENT_TOKENS ||--o{ ACCESS_LOG : "MCP access"
    ROLES ||--o{ WAREHOUSE_USERS : "role code"
    ROLES ||--o{ ROLE_PERMISSIONS : "角色 = 权限点集合"
    PERMISSIONS ||--o{ ROLE_PERMISSIONS : "permission_code"
    PERMISSIONS ||--o{ PERMISSION_ROUTES : "点 → HTTP 路由（Phase B）"

    USERS {
        int id PK
        string username
        string password_hash "werkzeug pbkdf2"
        int is_admin "平台管理员"
        int is_active "启用/停用 (v2 账号管理)"
        int tenant_id FK "P0-1 多租户 PoC；默认 1"
        string created_at
        string last_login_at
    }
    ROLES {
        int id PK
        string code "staff|manager|admin|viewer|finance"
        int tenant_id
        int is_builtin "内置包=1"
        int rank "阶梯 1/2/3；旁支 NULL"
    }
    PERMISSIONS {
        string code PK "order.approve / report.cost.view"
        string category
        string risk "normal|sensitive|critical"
    }
    ROLE_PERMISSIONS {
        int role_id FK
        string permission_code FK
    }
    PERMISSION_ROUTES {
        string permission_code FK
        string method
        string path_pattern "Web / MCP 共用词汇表"
    }
    WAREHOUSES {
        int id PK
        string code UK "wh_002 / wh_000 / rd_001"
        string name
        string db_path
        string warehouse_type "storefront|rd|distribution_center"
    }
    WAREHOUSE_USERS {
        int user_id FK
        int warehouse_id FK
        string role "staff|manager|admin|viewer|finance（code，非 FK）"
    }
    CANONICAL_ITEMS {
        int id PK
        string canonical_sku UK "IC-000001"
        string name
        string category_code FK
        string unit "锁死"
        real gram_per_unit "锁死"
        string status "active|disabled (Q3=deactivate_only)"
        real selling_price "Q6=canonical_managed"
    }
    CANONICAL_PUBLISH_EVENTS {
        int id PK
        string status "pending|partial|complete|failed"
        string target_warehouse_codes_json
        string backup_paths_json "Q7 措施①"
    }
    AGENT_TOKENS {
        int id PK
        string token_hash "pbkdf2"
        string token_prefix "索引前缀 8 字符"
        string encrypted_token "DB at rest 加密"
        string allowed_read_paths_json
        string allowed_write_paths_json
        string allowed_warehouse_codes_json
        string revoked_at
    }
    STORE_ORDERS {
        int id PK
        string order_no UK
        string store_warehouse_code FK
        string dc_warehouse_code FK
        string status "pending|approved|shipped|delivered|cancelled"
        real shipping_fee "v3.1 锁定"
    }
```

> **权限精细化新增表（2026-10-13 起）**：`permissions` / `roles` / `role_permissions` / `permission_routes` / `approval_policies`（5 张，均 `CREATE TABLE IF NOT EXISTS`，对存量 master.db 为 no-op 追加；由 `init_master_db()` 幂等播种）。角色包与权限点目录的**单一事实来源**是 `permissions_manifest.py`（守卫 / 管理 UI / 测试四方共用）。详 §8.7。

### 4.3 warehouse.db 核心表（ER 简图）

```mermaid
erDiagram
    CATEGORIES ||--o{ ITEMS : "category_id"
    ITEMS ||--o{ STOCK_MOVEMENTS : "item_id"
    ITEMS ||--o{ STOCKTAKES : "item_id"
    ITEMS ||--o{ RESTOCK_REQUESTS : "item_id"
    ITEMS ||--o{ OUTBOUND_REQUESTS : "item_id"
    ITEMS ||--o{ ADJUSTMENT_REQUESTS : "item_id"
    ITEMS ||--o{ PRODUCT_BOM : "item_id"
    ITEMS ||--o{ IC_RECIPE_ITEMS : "item_id"
    ITEMS ||--o{ RECIPE_ITEMS : "item_id"
    PRODUCTS ||--o{ PRODUCT_BOM : "product_id"
    PRODUCTS ||--o{ PRODUCTION_RUNS : "product_id"
    PRODUCTION_RUNS ||--o{ PRODUCTION_RUN_ITEMS : "run_id"
    IC_RECIPES ||--o{ IC_RECIPE_ITEMS : "ic_recipe_id"
    RECIPES ||--o{ RECIPE_ITEMS : "recipe_id"
    STOCKTAKE_BATCHES ||--o{ STOCKTAKES : "batch_id"

    CATEGORIES {
        int id PK
        string name "FIXED_CATEGORIES 9 个"
        string canonical_code "Spec §2.3 nullable"
    }
    ITEMS {
        int id PK
        string sku UK
        string name
        int category_id FK
        real quantity
        real safety_stock
        string unit
        real unit_cost
        real gram_per_unit
        string aux_unit
        real aux_rate
        real selling_price "Q6=canonical_managed 后由主数据下发"
        int canonical_id "Q1-Q4 跨仓键"
        int is_alias "0|1"
        string canonical_status "主数据下发后写入"
        int is_active "P0-10 单仓启停（扇出永不覆盖）"
        int is_orderable "v3.1 DC 可订开关"
        int is_store_exclusive
        int price_follow_canonical
    }
    PRODUCTS {
        int id PK
        string name UK
        string unit
    }
    RECIPES {
        int id PK
        string name UK
        string output_unit
        real output_qty
        real sale_price
    }
    RECIPE_ITEMS {
        int id PK
        int recipe_id FK
        int item_id "source_type=item"
        int ic_recipe_id "source_type=ic_recipe"
        string source_type "item|ic_recipe"
        real qty_per_unit "⚠️ 多态语义不一致 pre-existing bug"
    }
    AUDIT_LOG {
        int id PK
        int user_id
        string username
        string action
        string target_type
        int target_id
        string detail
        string created_at
    }
```

### 4.4 Schema 迁移约定（铁律）

```mermaid
flowchart LR
    Start["get_warehouse_db()<br/>每个请求调"] --> ReadVersion{"PRAGMA<br/>user_version<br/>>= WAREHOUSE_SCHEMA_VERSION?"}
    ReadVersion -->|Yes| Skip["跳过（单次 PRAGMA 读）"]
    ReadVersion -->|No| Migrate["migrate_warehouse_db_columns()<br/>PRAGMA table_info 守卫<br/>+ idempotent ALTER<br/>+ 创建索引"]
    Migrate --> Stamp["PRAGMA user_version = 2"]
    Stamp --> Skip
```

- **`WAREHOUSE_SCHEMA_VERSION = 2`**，改了 ALTER 块必须 bump。
- **新增 items 列只加到 `migrate_warehouse_db_columns()`，不加进 `WAREHOUSE_SCHEMA`**（tests 断言 WAREHOUSE_SCHEMA 不含 is_orderable）。
- **`init_master_db()`** 用 `fcntl.flock` 保护 gunicorn 多 worker 并发 ALTER。
- **所有绕过 `get_warehouse_db()` 直连 sqlite3 的代码（CLI / 测试 / MCP）必须先调 migrate**。
- **P0-6（2026-10-10 起）**：master.db 显式迁移走 `flask migrate status|up|down`（`migrations/__init__.py` 注册表 + `migrations_runner.py`，PRAGMA user_version 门控，每步前 sqlite3 backup API 热备到 `backups/migrations/`）。`init_master_db()` 幂等 ALTER 降级为兜底；**新 schema 变更一律加迁移条目**。回归 `tests/test_migration_runner.py`（9 用例）。

---

## 5. 应用层模块图

```mermaid
flowchart TB
    subgraph Entry["入口层"]
        App["app.py create_app()<br/>注册 18 蓝图 + PWA + /health"]
        CLI2["cli.py register_cli()<br/>init-master / create-warehouse / ... / mcp"]
    end

    subgraph Shared["共享层（被所有 blueprint 依赖）"]
        Config["config.py<br/>FIXED_CATEGORIES / ROLE_RANK /<br/>CANONICAL_POLICY / BACKUP 路径"]
        DB["db/__init__.py<br/>get_master_db / get_warehouse_db /<br/>init_master_db / migrate_warehouse_db_columns"]
        Perm["permissions.py<br/>@require_login / @require_role /<br/>@require_platform_admin / @require_warehouse_type"]
        Helpers["blueprints/_helpers.py<br/>parse_qty / fmt_qty / fmt_money /<br/>register_template_context (注入 is_admin / is_rd / is_storefront)"]
    end

    subgraph Blueprints["蓝图层 (视图 + 表单处理)"]
        Auth["auth.py<br/>login/logout/warehouse_picker<br/>PWA manifest+sw"]
        Core["core.py<br/>仪表盘 + 品类管理"]
        Items["items.py<br/>库存品 CRUD + 库存视图"]
        Canonical["canonical.py<br/>主数据 CRUD + 扇出"]
        StoreOrdering["store_ordering.py<br/>门店↔DC 订货 + 审批 + 发货 + 收货"]
        RecipeCost["recipe_cost.py<br/>配方 (产品 BOM + ic_recipe + recipe) + 成本"]
        Forecast["forecast.py<br/>预测 + 定时调度"]
        Procurement["procurement.py<br/>采购建议"]
        Notifications["notifications.py<br/>应用内通知"]
        Stocktake["stocktake.py<br/>盘点批次"]
        Restock["restock.py<br/>入库申请"]
        Outbound["outbound.py<br/>出库申请"]
        Production["production.py<br/>生产录入 + CSV"]
        Adjustment["adjustment.py<br/>库存调整 (⚠️ 未注册到 app.py)"]
        Consumption["consumption.py<br/>消耗分析"]
        Reports["reports.py<br/>入库/出库报表 + 库存健康仪表盘"]
        Users["users.py<br/>用户管理"]
        ImportItems["import_items.py<br/>CSV 导入库存品"]
        AgentTokens["agent_tokens.py<br/>平台管理员令牌管理"]
    end

    subgraph PureLogic["纯逻辑层 (无 Flask 依赖，易测)"]
        ItemsPure["items_pure.py"]
        CanonicalPure["canonical_pure.py (3180 行，最复杂)"]
        StoreOrderingPure["store_ordering_pure.py (2197 行)"]
        RecipeCostPure["recipe_cost_pure.py"]
        RecipeImportPure["recipe_import_pure.py<br/>门店存量配方导入（products→ic_recipes）"]
        ForecastPure["forecast_pure.py"]
        ProcurementPure["procurement_pure.py"]
        NotificationsPure["notifications_pure.py"]
        PublishRecipePure["publish_recipe_pure.py"]
        ReportsHealthPure["reports_health_pure.py<br/>FIFO 账龄重放 / DOS / ABC / BOM 超耗"]
    end

    Entry --> Shared
    Blueprints --> Shared
    Blueprints -.->|复用| PureLogic
    PureLogic --> Config
```

**约定**：`xxx.py`（蓝图，含 Flask 视图）和 `xxx_pure.py`（纯函数/类，便于测试和复用）成对出现。改业务规则时**优先改 pure**，再让蓝图调它。

**UI 导航结构（2026-10-13 导航模块化起）**：面向用户的信息架构收敛为 **7 模块** —
总览 / 库存管理 / 生产管理 / 门店订货 / 采购 / 通知 / 管理员，三个载体（桌面侧边栏 /
移动底栏 / `/land` 模块卡片）共用同一套模块归属与角色口径。归属决策与载体规则见
[`docs/2026-10-13-nav-modules.md`](2026-10-13-nav-modules.md)。

---

## 6. 蓝图依赖关系

```mermaid
flowchart LR
    classDef root fill:#fef3c7,stroke:#f59e0b,stroke-width:2px
    classDef pure fill:#dbeafe,stroke:#2563eb
    classDef bp fill:#e0e7ff,stroke:#4f46e5

    Shared["config / db / permissions<br/>(_helpers)"]:::root

    Auth[auth.py]:::bp --> Shared
    Auth -->|audit()| RecipeCost
    Items[items.py]:::bp --> Shared
    Items --> ItemsPure[items_pure.py]:::pure
    Canonical[canonical.py]:::bp --> Shared
    Canonical --> CanonicalPure[canonical_pure.py]:::pure
    StoreOrdering[store_ordering.py]:::bp --> Shared
    StoreOrdering --> StoreOrderingPure[store_ordering_pure.py]:::pure
    StoreOrderingPure --> Procurement[procurement.py]:::bp
    StoreOrderingPure --> NotificationsPure[notifications_pure.py]:::pure
    StoreOrderingPure --> ProcurementPure[procurement_pure.py]:::pure
    RecipeCost[recipe_cost.py]:::bp --> Shared
    RecipeCost --> RecipeCostPure[recipe_cost_pure.py]:::pure
    RecipeCost --> RecipeImportPure[recipe_import_pure.py]:::pure
    RecipeCost --> PublishRecipePure[publish_recipe_pure.py]:::pure
    RecipeCost -->|audit()| Auth
    Forecast[forecast.py]:::bp --> Shared
    Forecast --> ForecastPure[forecast_pure.py]:::pure
    Procurement --> ProcurementPure
    Notifications[notifications.py]:::bp --> Shared
    Notifications --> NotificationsPure
    Users[users.py]:::bp --> Shared
    AgentTokens[agent_tokens.py]:::bp --> Shared
    Reports[reports.py]:::bp --> Shared
    Reports --> ReportsHealthPure[reports_health_pure.py]:::pure
    Reports -->|master.db 只读| StoreOrders["store_orders（门店履约）"]
    Others["stocktake/restock/outbound/production/<br/>adjustment/consumption/<br/>core/import_items"]:::bp --> Shared
```

**依赖原则**：
- 蓝图层可以调 **pure 层 + 其他蓝图层**（如 `recipe_cost` 调 `auth.audit()`）。
- **pure 层不依赖 Flask 上下文**（不 import flask），便于测试和跨场景复用。
- `store_ordering_pure` 是跨最多业务的纯模块（订货同时触发采购缓存重建 + 通知事件）。

---

## 7. 请求生命周期

### 7.1 Web（Cookie 会话）

```mermaid
sequenceDiagram
    autonumber
    participant B as Browser
    participant F as Flask (gunicorn worker)
    participant H as auth.before_app_request
    participant V as 蓝图视图函数
    participant DB as SQLite

    B->>F: GET /items
    F->>H: 触发 before_app_request
    H->>H: 读 session[user_id/warehouse_id]
    alt 未登录
        H-->>B: 302 /login (PUBLIC_ENDPOINTS 例外)
    else 已登录
        H->>DB: SELECT * FROM users WHERE id=?
        H->>DB: SELECT * FROM warehouses WHERE id=?
        H->>DB: SELECT role FROM warehouse_users WHERE ...
        H->>DB: migrate_warehouse_db_columns() (幂等, < 1ms)
        Note over H: g.user / g.warehouse / g.role / g.warehouse_db_path 就绪
        H->>V: 调用视图
        V->>V: @require_login / @require_role 检查
        V->>DB: 业务查询 (get_warehouse_db() → 当前仓库 sqlite3)
        DB-->>V: 行数据
        V-->>B: render_template + Jinja2 + CSS
    end
    F->>DB: teardown_appcontext → close_dbs()
```

**AJAX 双路径契约**：`static/ajax-form.js` 给请求打 `X-Requested-With: fetch` + `Accept: application/json`。`register_ajax_handlers(app)` 注册的 after_request hook 检测到这两个头时，把视图的 3xx redirect 改写成 200 JSON 信封 `{ok, location, message}`，同时 drain Flask flash 进 `message`；非 AJAX 路径（浏览器整页 POST / curl / MCP）保留 302 redirect 字节级不变。详见 §13.7。

### 7.2 MCP（HTTP Bearer）

```mermaid
sequenceDiagram
    autonumber
    participant A as AI Agent
    participant ASGI as Starlette (uvicorn)
    participant MW as AuthMiddleware
    participant T as MCP Tool Handler
    participant DB as SQLite

    A->>ASGI: POST /api/mcp/ {jsonrpc:2.0, method:"tools/call", params:{name:"outbound_create", arguments:{...}}}
    ASGI->>MW: AuthMiddleware.__call__
    MW->>MW: 解析 Authorization: Bearer <token>
    alt env token 匹配
        MW->>MW: 放行（dev only）
    else agent_tokens 表匹配
        MW->>DB: SELECT * FROM agent_tokens WHERE token_prefix=? AND revoked_at IS NULL
        MW->>MW: pbkdf2 校验 token_hash
        MW->>MW: set_current_auth(ctx) via ContextVar
        MW->>ASGI: 放行到 next
    else 都失败
        MW-->>A: 401 {"error":"unauthorized"}
    end
    ASGI->>T: 路由到 tools/call 处理器
    T->>T: resolve_ctx() → AuthContext
    T->>T: check_warehouse(ctx, wh_code)
    T->>T: check_path(ctx, method, path)
    T->>DB: 业务查询（read 或 write）
    DB-->>T: 行数据
    T-->>A: JSON-RPC result
```

---

## 8. 鉴权架构

### 8.1 三种身份

| 身份 | 用途 | 凭证 | 鉴权位置 |
|------|------|------|----------|
| **Web 用户** | 浏览器访问 | Flask session cookie（HttpOnly + SameSite=Lax，prod Secure） | `auth.before_app_request` |
| **MCP Bearer** | AI Agent 通过 HTTP 调用 | `Authorization: Bearer <token>` | `mcp_server/main.py:AuthMiddleware` |
| **CLI** | `flask ...` 命令 | 无（进程级，信任部署环境） | 直接 sqlite3 |

### 8.2 装饰器

| 装饰器 | 文件 | 作用 |
|--------|------|------|
| `@require_login` | `permissions.py` | 必须登录 + 已选仓库（除非 view 在 `WAREHOUSE_EXEMPT`） |
| `@require_role(min_role)` | 同上 | 必须达 `min_role`（staff=1/manager=2/admin=3）；`is_admin` 平台管理员 bypass |
| `@require_platform_admin` | 同上 | 必须 `g.user.is_admin=1`（不绕过） |
| `@require_warehouse_type(*types)` | 同上 | 当前仓库类型必须在白名单（rd/storefront/dc） |
| `storefront_only` / `distribution_center_only` | 同上 | 上述的白名单语义糖 |
| `@require_permission(*codes)` | 同上 | **权限点（RBAC）**：当前用户须持有全部 codes（AND）；语义与 `require_role` 对齐（登录→超管 bypass→选仓→权限） |
| `@require_any_permission(*codes)` | 同上 | 任一 code 命中即放行（OR），用于「owner 或提权」场景 |

### 8.3 鉴权关键路径

```mermaid
flowchart TB
    Request[HTTP Request] --> Check{endpoint 在<br/>PUBLIC_ENDPOINTS?}
    Check -->|Yes| Skip[跳过鉴权]
    Check -->|No| HasUser{session 有 user_id?}
    HasUser -->|No| Login[302 → /login]
    HasUser -->|Yes| LoadUser[加载 g.user/warehouse/role]
    LoadUser --> Decorator{视图装饰器}
    Decorator -->|@require_login| NeedWh[需要 warehouse_db_path]
    Decorator -->|@require_role| RoleCheck{role 达 min_role?}
    Decorator -->|@require_platform_admin| AdminCheck{is_admin?}
    Decorator -->|@require_warehouse_type| TypeCheck{wh_type 匹配?}
    NeedWh --> RoleCheck
    RoleCheck -->|Yes| Execute[执行视图]
    RoleCheck -->|No| Flash403[flash + abort 403]
    AdminCheck --> TypeCheck
    TypeCheck --> Execute
```

**注意**：`@require_role` 和 `@require_platform_admin` 都允许 `is_admin=1` 通过。`@require_platform_admin` 反而**要求** `is_admin`，这是有意区分（前者"在这个仓库里权限够" vs 后者"全局管理权限"）。

### 8.6 租户隔离（P0-1 轻方案 PoC，2026-10-10 起）

- `master.db`：`tenants` 注册表 + `users/warehouses/canonical_items.tenant_id`（默认 1 = 存量单租户，老库 ALTER 自动回填）。
- 请求上下文：`g.tenant_id`（auth 钩子从 user 行取）。
- 三道闸：① 钩子加载仓库时校验 `warehouse.tenant_id == user.tenant_id`（`is_admin` 平台管理员豁免）② picker 只列本租户 ③ `/select-warehouse/<id>` 租户闸先行（跨租户脏绑定 → 403）。
- 新仓放置：租户 1 保持 `db/warehouses/`；租户 N 落 `db/tenants/<N>/warehouses/`（`flask create-warehouse --tenant N`）。
- **PoC 未覆盖（后续阶段）**：canonical 扇出/订单/MCP token/报表查询的租户化过滤；当前仅列/读仓与选择入口强制。回归 `tests/test_tenant_isolation.py`（8 用例）。
- **账号管理（v2 2026-10-12 起）补完**：`/users` 路由全套按 tenant 过滤 —— `list_users` 接受 `?tenant_id=`（admin 跨租户可指定，非 admin 强制 scope 到 `g.tenant_id`）。见 §8.7。

### 8.7 账号管理 v2（feat/user-admin-v2，2026-10-12 起）

- **页面重写**：Notion/Linear 风格 —— 顶部工具栏（搜索/角色/租户/启停/排序/新建）+ 紧凑表格（一行一用户，6 列）+ 右侧 360px 抽屉（编辑/创建/批量绑定/启停/删除）。`templates/users.html` 重写，`static/users-admin.css` + `static/users-admin.js` 新增（零依赖）。
- **schema**：`users.is_active INTEGER NOT NULL DEFAULT 1` + 复合索引 `idx_users_tenant_active (tenant_id, is_active)`。PRAGMA 守卫做幂等 ALTER（`db/__init__.py:init_master_db`）。
- **登录拦截**：`blueprints/auth.py:login` 在密码校验之前先查 `is_active=0`，命中返回 403 + flash「账号已停用，请联系平台管理员」。不进入失败计数（避免已停用账号仍触发锁定）。
- **新 endpoint**：
  - `POST /users/<id>/toggle-active`：切换 `is_active`（不能停用自己 —— 防锁出唯一 admin；可重新启用）。
  - `POST /users/bulk-bind`：body=`user_ids[] + warehouse_id + role`，`INSERT OR IGNORE` 保证幂等（已绑定的 (user, warehouse) 静默跳过），无 user 时 flash 报错。
- **list_users 查询参数**：`q`（≥2 字 LIKE）/ `role`（用户在任一仓库持有该角色）/ `tenant_id` / `is_active` / `sort`（白名单 id/username/last_login_at，可加 `-` 前缀降序）/ `page` / `page_size`（默认 20、上限 100）。`?format=json` 显式开启返回 JSON envelope（默认浏览器走 HTML）。**sort 白名单必须比对「剥离 `-` 之后」的列名**：先去掉前导 `-`（`sort[1:] if sort.startswith("-")`）再查集合，否则 `-last_login_at` 会落到 fallback `id` 且方向仍是 DESC（降序静默变 id 倒序）。
- **守卫统一**：移除 `blueprints/users.py:_require_admin()`，全 view 改用现成的 `@require_platform_admin` 装饰器（`permissions.py:54`）。`agent_tokens.py` 的同名内联守卫本次不动（独立 PR 范围）。
- **测试**：`tests/test_users_admin_v2.py`（40 用例）覆盖搜索/筛选/启停/批量绑定/租户过滤/守卫/停用登录拦截/分页/排序（升降序 + 白名单兜底）/创建权限矩阵默认值/`/users` 不可缓存（含 `sw.js` 缓存边界的静态断言）/schema 迁移幂等/既有 endpoint 回归。Spec：`docs/decisions/SPEC-user-admin-v2.md`。⚠️ 抽屉 JS（`users-admin.js`）无 pytest 覆盖：`escapeHtml`、radio 默认值、刷新时序、SW 缓存这类缺陷只能靠浏览器冒烟（Playwright + 本地实例：admin 登录 + 选仓后断言 URL 不含 `format=json`、每仓库恰 1 个 `:checked`、改库后同 URL 取到新值、离线导航落 `offline.html`）——后续应把冒烟脚本沉淀为自动化用例。
- **工具栏刷新机制（2026-10-13 修正）**：筛选/翻页走 `fetch(带筛选参数的 /users)` + `DOMParser`，只替换 `#users-tbody` 与 `.users-pagination`，`history.replaceState` 同步地址栏；表格/分页标记仍由 Jinja 渲染（JS 不重复实现行渲染，避免两套标记漂移）。三条硬约束：① **禁止 `window.location.assign` 到带 `?format=json` 的 URL**——那会把整个管理页换成裸 JSON（曾发生：点一下筛选/翻页页面就没了）；② 待替换的分页节点会被整块换掉，`click` 监听必须挂 `document`，插回时锚点用 `.users-table-wrap`（`tbody.insertAdjacentHTML` 落在 table 上下文会触发 foster parenting，分页条被塞进表格内部）；③ 只认最后一次请求（`refreshSeq` 序号），change 分支不 debounce，旧响应晚回来不得覆盖新结果。URL 参数以地址栏现有值为基底（`page_size` 等非工具栏字段得以保留），并强制 `delete('format')`。④ 页面内 `fetch` 的 HTML **必须不可缓存**：这正是当初踩到 SW cache-first 的原因，见 §11「SW 缓存边界」。两层各自的作用：HTTP/代理层靠 `Cache-Control: no-store`；SW 的 Cache API 层靠 `sw.js` 的「只缓存同源 `/static/`」范围判断（**no-store 对它无效**）。
- **抽屉 JS 约定（2026-10-13 修正）**：`static/users-admin.js` 用 `innerHTML` 拼抽屉内容，所有来自 DB 的字符串（用户名/仓库名/租户名）必须过 `escapeHtml()`——实体表必须是真实体（`'&': '&amp;'` 等），写成裸字符等于空转、构成存储型 HTML 注入。抽屉里的编辑目标 uid 必须由调用方把被点击的 `tr.users-row` **显式传给 `renderEditDrawer(row)`**（uid 在函数内从 `data-user-id` 取出），不要经 `drawerBody` 的 DOM 属性中转（那段 html 在属性写入之前就拼好，会拿到空的或上一个用户的 id → 批量绑定绑错人）。**同一 radio group 内只能有一处 `checked`**：浏览器取文档序靠后的那个，创建抽屉的权限矩阵曾因此把"不动矩阵直接创建"变成"给新账号授上每个仓库的 manager"（v1 默认是「无权限」，必须保持）。
- **明确不做**（v2 不引入）：自助改密（无"个人中心"页）、租户 CRUD（PoC 仅做了字段）、agent_tokens 页面重写、修改用户名、审计 UI（`admin_audit.log` 仍是 JSONL）。

### 8.4 CSRF 保护（2026-10-10 起）

- `app.py` 挂 `CSRFProtect`（Flask-WTF）；`_csrf_testing_gate` 在 `CSRFProtect` **之前**注册（Flask before_request 按 FIFO 执行），`TESTING=True` 时默认关闭 CSRF（存量测试零改动），专测设 `WTF_CSRF_FORCE_ENABLED=True` 打开。
- 所有 `method="post"` 表单均带 `{{ csrf_token() }}`（`scripts/add_csrf_tokens.py` 注入 + `--check` 校验，新增模板后跑一次）。
- MCP（`/api/mcp/`）在独立 Starlette 进程、Bearer 鉴权，不经 Flask CSRF。
- 回归：`tests/test_csrf.py`（7 用例）。

### 8.5 Rate limiting（P0-4，2026-10-10 起）

- **全局**：`app.py` 内存滑动窗口（`rate_limit_pure.SlidingWindowLimiter`），每 IP 600 次/分钟（env `DAILYCHECK_RATE_LIMIT_PER_MIN` 可调），`/health`/static 豁免；TESTING 默认关（`RATE_LIMIT_FORCE_ENABLED` 打开）；超限 429。gunicorn 多 worker 各自计数、重启清零（v1 约定，Redis 升级路径预留）。
- **登录锁定**：master.db `login_throttle` 表（跨 worker），(ip, username) 维度，15 分钟窗内连错 5 次 → 锁 30 分钟；成功登录清零。**时间一律 UTC naive**（`datetime.utcnow()`），与进程时区无关，规避生产 CST/UTC 坑。
- **MCP per-token 配额**：`agent_tokens.rate_limit` 列（NULL=`MCP_DEFAULT_RATE_LIMIT`=120/分钟），`AuthMiddleware` 内存滑动窗口按 token_id 限速，超限 429；单进程内共享、重启清零。
- 回归：`tests/test_rate_limit.py`（15 用例）。

### 8.7 权限点（RBAC）精细化管理（2026-10-13 起）

**模型**：角色 = **权限点集合**。25 个权限点目录（order 5 / stock 6 / item 6 / report 3 / admin 5）定义于 `permissions_manifest.py::PERMISSION_CATALOG`；5 个内置角色包（`ROLE_PACKAGES`）——阶梯包 `staff(10) ⊆ manager(17) ⊆ admin(18)`，旁支包 `viewer(3)` / `finance(4)`（`rank=NULL`，不参与阶梯）。**单一事实来源** `permissions_manifest.py` 被守卫 / 管理 UI / 测试四方共用（守卫用 `@require_permission`，测试用 `tests/test_route_permission_matrix.py` 校验覆盖与不变式）。

**解析（`permissions.py::current_permissions()`）**：每请求一次 JOIN 查 master.db（`warehouse_users → roles → role_permissions`），结果缓存到 `g.perm_set`——**禁进程级缓存**（gunicorn 多 worker 会各自漂移）；**fail-closed**：任何异常 → 空集 → 403 + 日志；`is_admin=1` 返回全目录哨兵（超管直通）。未知角色 code（JOIN 空集）→ 403。

**存储**（master.db，`init_master_db()` 幂等播种）：`permissions` / `roles` / `role_permissions` / `permission_routes`（Web 与 MCP 共用词汇表，Phase B 填充）/ `approval_policies`（Phase 3 扩展点，空表）。启动断言 `_verify_builtin_packages()`（空包 / 阶梯破坏 / 权限数契约 / 越界点 / 孤儿点）**告警不 raise**（防单点数据问题拒绝启动）。改包后须 `flask reseed-builtin-roles` 显式重播种（`INSERT OR IGNORE` 不更新已存在行）。

**成本脱敏 / 成本读访问**（服务端，`blueprints/cost_redaction_pure.py`）：报表导出 `export_consumption` / `export_summary` 对无 `report.cost.view` 者把成本列留空（列结构不变）；**配方成本读接口**（`recipe_cost` 5 个 CSV 导出 + `api_cost` + `overview` 总览页）挂 `report.cost.view`（staff/viewer 403）；`/summary`、`/inventory` 页**不脱敏**（Eric 终裁④，口径例外）。另 `users.html` 页脚已注明「仓管理(该仓全权，品项删除除外)」（对齐终裁②：`item.delete` 为平台级）。

**兼容层**：`require_role` 阶梯与权限点**并存**——本期未迁移的路由（canonical 管理、import_items、stocktake CSV、forecast 非 index、procurement、store_ordering DC list/ship、reports 营业额写）保持 `require_role`/`require_platform_admin`，语义零变化。回归：`tests/test_route_permission_matrix.py`（49 用例）+ `tests/test_master_db_migrate.py`（RBAC 7 用例）。设计见 `docs/2026-10-12-permission-spec.md` / `-architecture.md` / `-implementation-plan.md`。

**管理 UI（阶段 1）**：`users.list_users` 向 `templates/users.html` 注入 `role_options`（`[{code,label}]`，**来源 `permissions_manifest`，顺序 = `BUILTIN_ROLE_CODES`**（staff→manager→admin→viewer→finance），`label` 取自 `ROLE_META`）；模板的角色选项与标签**一律渲染 `role_options`**——模板不得硬编码角色名单/中文名。**绑定校验与展示同源**：`blueprints/users.py::VALID_ROLES = set(BUILTIN_ROLE_CODES)`（5 包，取代旧 `ROLE_RANK` 仅 3 阶梯包）——管理员可把用户绑定到旁支角色 `viewer`/`finance`；未知角色 code 由 `_parse_binding` 在绑定层先拒（与运行时 JOIN 空集 fail-closed 呼应）。角色标签样式见 `static/role-tags.css`（语义：实心=可写 / 描边=只读；`viewer` 虚线、`finance` teal 描边、平台管理员琥珀）。阶段 2 的 `/admin/permissions` 权限矩阵视图见 Spec §7。

---

## 9. MCP 架构

### 9.1 进程拓扑

```mermaid
flowchart LR
    subgraph MCP["mcp_server/ 进程 (uvicorn :5100)"]
        Main["main.py<br/>lifespan + AuthMiddleware + 健康检查"]
        Protocol["protocol/<br/>server.py + tools/"]
        Service["service/<br/>auth / inventory / inbound /<br/>outbound / consumption /<br/>forecast / procurement /<br/>canonical"]
        Data["data/<br/>master.py / warehouse.py / unit_of_work.py"]
        Infra["infra/<br/>errors.py / access_log.py"]
    end

    Main --> Protocol
    Protocol --> Service
    Protocol --> Infra
    Service --> Data
    Data --> Config["config.py (root)<br/>共享路径常量"]
```

### 9.2 24 个 MCP 工具

| 工具 | 服务模块 | 路径 ACL | 仓库范围 |
|------|----------|----------|----------|
| `items_list` | inventory | read | per-token |
| `items_detail` | inventory | read | per-token |
| `movements_list` | inventory | read | per-token |
| `restock_create` | inbound | write | per-token |
| `restock_list` | inbound | read | per-token |
| `outbound_create` | outbound | write | per-token |
| `outbound_list` | outbound | read | per-token |
| `outbound_rollback` | outbound | write | per-token |
| `warehouse_consumption` | consumption | read | per-token |
| `item_consumption` | consumption | read | per-token |
| `item_forecast` | forecast | read | per-token |
| `procurement_store` | procurement | read | per-token |
| `procurement_hub` | procurement | read | per-token |
| `canonical_list` | canonical | read | 全局（跨仓） |
| `canonical_detail` | canonical | read | 全局（跨仓） |
| `canonical_categories` | canonical | read | 全局（跨仓） |
| `canonical_fanout_targets` | canonical | read | 全局（跨仓） |
| `canonical_conflicts` | canonical | read | 全局（跨仓） |
| `canonical_fanout_event` | canonical | read | 全局（跨仓） |
| `canonical_create` | canonical | write | 全局（跨仓） |
| `canonical_update` | canonical | write | 全局（跨仓） |
| `canonical_claim` | canonical | write | per-token |
| `canonical_unclaim` | canonical | write | per-token |
| `canonical_fanout` | canonical | write | 显式传 warehouse_codes |
| `canonical_resolve_conflict` | canonical | write | 全局（跨仓） |
| `canonical_set_binding_active` | canonical | write | per-token |

### 9.3 双传输模式

| 模式 | 入口 | 鉴权 | 用途 |
|------|------|------|------|
| **stdio** | `flask mcp` | 进程 env `DAILYCHECK_MCP_TOKEN` | 本地 Claude Code |
| **HTTP/SSE + StreamableHTTP** | `python -m mcp_server` (HTTP 模式) | Bearer → agent_tokens 表（per-token 仓库/路径 ACL） | 远程 Agent |

**HTTP 入口路径**：
- `/sse`（legacy SSE）
- `/messages/`（SSE POST）
- `/api/mcp/`（StreamableHTTP，**推荐**）
- `/health`（不需鉴权）

### 9.4 鉴权回退链

```mermaid
flowchart TB
    Req[HTTP Request with Bearer] --> Env{DAILYCHECK_MCP_TOKEN<br/>env 匹配?}
    Env -->|Yes| Pass1[放行（dev/兼容）]
    Env -->|No| Table[查 agent_tokens 表<br/>WHERE token_prefix=?<br/>AND revoked_at IS NULL]
    Table --> Found{找到 + pbkdf2 通过?}
    Found -->|Yes| SetCtx[set_current_auth(ctx)<br/>记录 ContextVar]
    Found -->|No| Legacy[查 NULL 前缀的历史行<br/>回退路径]
    Legacy --> Found
    Found -->|No| Deny[401 unauthorized]
    SetCtx --> Pass2[放行到工具处理器]
    Pass1 --> Pass2
    Pass2 --> Handler[工具 handler 用 resolve_ctx() 取 ctx]
    Handler --> ACL{check_warehouse +<br/>check_path 通过?}
    ACL -->|Yes| Exec[执行业务]
    ACL -->|No| Forbidden[403 ForbiddenError]
```

---

## 10. 业务领域模型

### 10.1 仓库类型

| 类型 | 用途 | 行为 |
|------|------|------|
| `storefront` | 门店（有实体库存） | 出入库/盘点/补货可见 |
| `rd` | 研发中心 | **无库存**；主数据管理；ic_recipe 编写 |
| `distribution_center` | 配送中心 | 给门店供货；store_ordering 审批/发货 |

### 10.2 Canonical 主数据治理（Q1-Q7）

| 策略 | 值（v4） | 含义 |
|------|----------|------|
| Q1 门店自建新主数据 | `deny` | 门店不能自建全新品项（白名单 = 空） |
| Q2 门店自治字段 | `open` | 允许门店自定义字段 |
| Q3 主数据删除 | `deactivate_only` | 只允许停用，禁止物理删除 |
| Q4 扇出冲突 | `freeze` | 冻结冲突字段，人工处理 |
| Q6 价格归属 | `canonical_managed` | 售价/采购价收归主数据 + 扇出下发 |
| Q7 库存零丢失 | `strict` | 扇出前自动备份；写库前 `shutil.copy2`；禁止真库写测试 |

> **Q7 `guard_real_db` 软链穿透防护（2026-10-15）**：prod 部署把 `db/warehouses/*.db` 做成指向 `/var/lib/dailycheck/warehouses/` 的软链，`resolve()` 会穿透使路径比较失配、guard 放行（verify.sh check1 在 prod 首演实测抓到）。guard 现对**未 resolve 的 absolute parent** 再比一次，任一命中即拒——改部署布局（换数据目录/软链形态）时必须保持此语义。

### 10.3 Store Ordering 状态机

```mermaid
stateDiagram-v2
    [*] --> pending: cart.submit
    pending --> approved: dc.manager.review
    pending --> cancelled: staff.cancel / admin.cancel
    pending --> rejected: dc.manager.reject
    approved --> shipped: dc.staff.ship (全部明细发齐)
    approved --> cancelled: dc.manager.cancel / admin.cancel
    shipped --> delivered: store.manager.receive (全部明细收齐)
    delivered --> [*]
    cancelled --> [*]
    rejected --> [*]

    note right of approved
        部分发货期间订单停在 approved
        收货门禁 = status in (approved, shipped)
        收货上限 = max(0, min(shipped, ordered) − fulfilled)
    end note

    note right of shipped
        shipped_quantity > 0 才转
        否则保持 approved
        DC 发货禁止超库存（v3 A7 反转）
    end note
```

### 10.4 Canonical 扇出流程

```mermaid
flowchart TB
    RD["研发 rd_001<br/>编辑主数据"] --> Save["canonical_items.status/name/...<br/>变更"]
    Save --> Detail["/canonical/detail"]
    Detail --> Fanout["/canonical/fanout<br/>触发扇出"]
    Fanout --> Backup["Q7 措施①<br/>shutil.copy2 到 backups/warehouses/"]
    Backup --> Loop{对每个目标仓}
    Loop --> Apply["apply_canonical_to_warehouse<br/>INSERT/UPDATE items 行"]
    Apply --> Conflict{字段冲突?}
    Conflict -->|Yes| Freeze["canonical_conflicts 表<br/>status=open (Q4 freeze)"]
    Conflict -->|No| Success[success]
    Freeze --> Manual["人工 review"]
    Manual --> Loop
    Success --> Event["canonical_publish_event_items<br/>applied_fields_json"]
    Event --> Loop
    Loop -->|完成| Done[event.status = complete/partial]
```

**两道闸门**（`fac7691` 定稿）：
1. **总闸** = `canonical_items.status='active'`（主数据侧）
2. **分闸** = 仓内 `items.is_orderable=1`（DC 单独控制可订）

主数据 disable 后**不自动扇出到仓内**（`is_active` 仍是 1）；仓内列表要等手动 `/canonical/fanout` 才生效。

**跨页跳转契约 — 筛选条件透传**（`feat/canonical-list-filter-preserve`，2026-10-12）：
- list → edit/detail：`url_for` 把当前 `q/category/status` 拼到 query string（URL 即状态、可分享）
- edit POST 成功：直跳带筛选的 list（不再过 detail），避免「保存→详情→后退」两步走
- edit POST 缺筛选字段：`referer` 同源 list URL 兜底（外部直链进 edit 时不丢上下文）
- detail 页头部「← 返回筛选结果」（有筛选时）/「← 返回品项列表」（无筛选时）
- 目的：用户在筛选结果里点编辑/查看 → 保存 → 直接回到原筛选上下文，不丢工作流

---

## 11. PWA / 移动端

```mermaid
flowchart LR
    subgraph Client["浏览器 / 手机"]
        HTML["Jinja2 模板<br/>(mobile-first)"]
        CSS["static/style.css<br/>1612 行"]
        SW["static/sw.js<br/>Service Worker (72 行)"]
        Manifest["static/manifest.webmanifest<br/>zh-CN / standalone"]
        Icons["static/icons/icon-192.png<br/>icon-512.png"]
    end

    subgraph Server["Flask"]
        Static["Flask static 路由"]
        PWARP["blueprints/auth.py:pwa_bp<br/>service_worker + webmanifest"]
    end

    HTML --> CSS
    HTML -.->|注册| SW
    HTML -.->|link rel=manifest| Manifest
    Manifest --> Icons
    SW -.->|cache| Static
    HTML -->|HTTP :8080| Server
    Server -->|SW + manifest + icons| HTML
```

**现状**：
- ✅ Service Worker 离线缓存 + PWA install
- ✅ CSRF token 全表单覆盖（P0-3，2026-10-10）
- ✅ manifest.webmanifest（standalone、minimal-ui、theme_color teal）
- ❌ **无 push notifications**

**SW 缓存边界（2026-10-13 收窄，重要）**：`sw.js` 的 cache-first 分支**只对同源 `/static/` 生效**，其余非导航 GET（HTML 片段、JSON、接口）一律 network-only（不 respondWith，交给浏览器直连）。导航请求仍 network-first + 离线回落 app shell / `offline.html`。
- 反例（已实测复现，勿回退）：收窄前对所有非导航 GET 无差别 `cache-first` + `cache.put`。一旦页面内 `fetch` 一个 HTML 页面（`users-admin.js` 的工具栏就是这么刷 `/users?<筛选>` 的），该页面会被写进 Cache API，后果两条 —— ① 服务端数据变了列表仍返回旧副本且不报错；② 缓存副本按 URL 命中即返回、**不经过服务端鉴权**，匿名会话也能拿到 200 的管理员页面（响应自带的 `Vary: Cookie` 挡不住 Cache API 匹配，实测拿到含 `bulk01` 的完整页）。
- `/users` 的 HTML 与 `?format=json` 响应都带 `Cache-Control: no-store`（`blueprints/users.py:_no_store`）。⚠️ **no-store 只约束浏览器 HTTP 缓存与中间代理，对 Cache API 无效**——实测 `cache.put()` 照常落盘（MDN 的 `Cache.put` 异常清单里没有 no-store 规则）。所以**在这条「非导航 GET」路径上，唯一防线是上面那条范围判断**（`tests/test_users_admin_v2.py` 有字面量 + 顺序双断言钉住它）；no-store 只是 HTTP 层的附加收益，不要据此放宽范围判断。
  - 范围之外：sw.js 的**导航分支**仍有 `caches.match(req)` 离线回落，会把带会话的 SSR HTML 按 URL 命中返回而不经鉴权（APP_SHELL 存量问题，与本 PR 解耦另开 issue）。范围判断只解决非导航 GET 这条路。
  - 唯一例外：`_helpers.py` 的 AJAX 3xx 信封（`X-Requested-With: fetch` 时返回的 `{ok, location, message}`）是新响应对象、不带 no-store；它不含管理员数据，无可利用面，登记备查。
- **改 `/static/` 下的资源必须 bump 模板里的 `?v=`（或 bump `CACHE_NAME`）**：`/static/` 走 cache-first 且不回源，暖缓存客户端会永久拿到旧文件。
- 改 `sw.js` 的缓存策略记得 bump `CACHE_NAME`（当前 `dailycheck-v5`）：activate 时按名字清旧缓存，否则已中毒的缓存条目会一直活着。
- ❌ **无 background sync**（离线写不能排队）
- ❌ iOS Safari PWA 有已知限制（storage quota、push）

**导航三载体（2026-10-13 导航模块化）**：
- 桌面侧边栏（`base.html:aside.sidebar`）：7 个 `.nav-group` 模块分组（`nav-group-title` + 链接）；
  模块内无可见子项则整块不渲染；管理员模块置底。
- 移动底栏（`base.html:nav.mobile-nav`）：≤5 个高频模块入口 + 「更多」→ `/land`；
  flex 布局（角色不同条目数 3~5 动态），不再固定 5 列 grid。
- 模块索引页（`land.html`，`/land`，no_sidebar）：每模块一张 `section.land-card`，按角色过滤；
  **卡内 `ul.land-links` 必须列全该角色在该模块可见的每一个页面**（不是只链模块首屏）。
- ⚠️ **不变量（有测试固化）**：三载体的模块归属与角色口径逐条一致；
  `<961px` 时侧边栏 `display:none`，**「更多」→ `/land` 是移动端唯一全模块入口**，
  故 `/land` 一旦漏列二级页，该页在移动端即整页无路可达（v0.1 曾漏 汇总/盘点/入库/出库 4 页）。
  守卫用例：`tests/test_nav_integration.py`（模块集行列式 + 移动端可达性）。
- ⚠️ 角色口径一律取 `_helpers.register_template_context` 注入变量，禁止 block 内 `{% set %}`（§13.2）。
- 改 `static/*` 后按约定 bump 模板里的 `?v=`（本次 `style.css?v=16` / `login.html?v=12`）。
- **总览模块单链接级角色门（2026-10-15 起）**：总览组内「库存健康」链接单独包
  `{% if is_admin or g.role.role in ('manager','admin') %}`——同一 nav-group 内
  按链接级可见性过滤的既有先例（同「门店订货」组内 `品项管理` 的写法），
  侧栏与 `/land` 两载体条件逐字一致，双向不变量由 `tests/test_reports_health.py::TestNavCarriers` 钉住。

---

## 12. 测试架构

```mermaid
flowchart TB
    subgraph Pytest["pytest (59 个测试文件, 977 用例；2026-10-15 实测)"]
        Pure["test_*_pure.py<br/>纯逻辑单测<br/>~60% 用例"]
        Route["test_*_route.py<br/>视图集成测试<br/>~30% 用例"]
        E2E["test_*_e2e.py<br/>端到端 ~5%"]
        Acceptance["test_acceptance_*.py<br/>验收脚本 39 用例全绿"]
        Aux["test_aux_unit.py /<br/>test_master_db_migrate.py<br/>pre-existing 失败来源"]
        Ajax["test_ajax_form.py<br/>行内 AJAX 双路径契约<br/>6 用例"]
        Perm["test_route_permission_matrix.py<br/>路由×角色权限矩阵<br/>49 用例（A 覆盖 / B deny-allow / C 不变式）"]
        BlueGreen["test_blue_green_implementation.py (80)<br/>+ test_blue_green_dryrun_guard.py (10)<br/>蓝绿 5 脚本：bash -n / dry-run / DRY_RUN 守卫 / 落点核验<br/>宿主 systemd 由 fake 钉死（§12 环境无关性）"]
    end

    subgraph Conftest["tests/conftest.py"]
        LoggedClient["logged_client<br/>管理员 session"]
        StaffClient["staff_client<br/>staff 角色"]
        RoleClients["manager_client /<br/>viewer_client / finance_client<br/>（权限精细化）"]
        SeedHelpers["_seed_item / _seed_outbound /<br/>_seed_production_consumption"]
    end

    subgraph MCPTest["tests/mcp_server/"]
        MCPPure["test_*_pure.py"]
        MCPRoute["test_*_route.py"]
        MCPData["test_data/test_warehouse.py"]
        MCPService["test_service/test_inbound.py<br/>+ test_canonical.py<br/>(26 用例 mock)"]
    end

    Pytest --> Conftest
    Pure --> Conftest
    Route --> Conftest
```

**运行**：
```bash
~/.local/bin/python3.12 -m pytest --basetemp=/tmp/pytest-dc   # 必须带 basetemp
```

**基线（2026-10-10）**：18 failed / 750 passed — 失败集与各次新开发严格逐行一致（owner SOP「零新增对比法」）。`feat/inline-form-ajax`（2026-10-11）新增 `test_ajax_form.py` 6 用例全过，零新增失败。`feat/user-admin-v2`（PR #17，2026-10-12）新增 `test_users_admin_v2.py` 40 用例，基线升至 **18 failed / 796 passed**，零新增失败。

**基线更新（2026-10-13，`feat/nav-modules`，rebase 至 `f00e490` 后）**：16 failed / 801 passed — `test_nav_integration.py` 旧断言（staff 应见采购）随导航模块化修正并扩充为 12 用例（796→801），失败集 18→16；`tests/known_failures.txt` 同步删 2 行（清单收缩为 16，与实际失败集逐条一致）。零新增。⚠️ 复用旧 basetemp（`/tmp/pytest-dc`）会因沙箱清理不完全随机出现 1~3 条 `sqlite3.OperationalError` ERROR；换全新 basetemp 即消失，与代码无关。

**整改基线（2026-10-14，`fix/pr-18-nav-modules-defects`）**：14 failed / 768 passed（CI 口径 `--ignore=tests/mcp_server`）。导航用例 12→57（`nav_client_factory` 角色矩阵 + 三载体链接集相等 + 全链接可达性 + 移动端可达性守卫），门禁 exit 0。

**权限精细化基线（2026-10-15，`feat/permission-granularity`，并流 main `9bb9d99` 后）**：14 failed / 928 passed（CI 口径；本地全量 16 failed / 1006 passed）。新增 `test_route_permission_matrix.py` 49 用例（A 覆盖 / B deny-allow / C 不变式）、`test_master_db_migrate.py` RBAC 迁移 +7 用例、`tests/conftest.py` 角色夹具（`manager_client` / `viewer_client` / `finance_client`）；并流后 `url_map` 实测 151 endpoint，Test A 双向断言无遗漏 / 无残留。失败集与 `known_failures.txt` 逐行一致，零新增，门禁 exit 0。

⚠️ **基线数字必须写明口径**（两套数字不能混用）：
- **CI 口径** = `pytest tests/ --ignore=tests/mcp_server`：14 failed / 928 passed。CI 以此为准，`known_failures.txt` 的 `mcp_server` 两行在 CI 里永不触发（属超集）。
- **本地全量** = `pytest tests/`（本地 python3.12 能跑 mcp 测试：2 failed / 78 passed）：16 failed / 1006 passed。
- 「零新增对比法」比对的是**失败集逐行**，不是 passed 总数；引用总数时必须带口径，否则跨环境对比会误判。

**CI 门禁（2026-10-10 起，`.github/workflows/ci.yml`）**：
- ruff（仅改动 .py，pin 0.16.10）→ pytest 全量（`--ignore=tests/mcp_server`，容器门控）→ `scripts/check_known_failures.py` 零新增断言。
- `tests/known_failures.txt` = 已知失败清单，**只许收缩不许扩张**（修复一个删一行）。
- **防假绿（2026-10-11 起）**：门禁对 pytest 收集错误 / 空输出 / 0 passed 一律拒绝放行（收集错误不产生 FAILED 行，坏依赖曾致门禁空过假绿）。测试三方依赖（如 openpyxl）必须进 requirements.txt。
- `deploy` job `needs: test`：CI 不过不部署（PR 触发不部署）。
- **生产部署触发（2026-10-11 起）**：仅 `workflow_dispatch` 手动执行，push main **不**自动部署。启用自动部署的前置条件：VPS 安装备份 cron + 异地副本 + 一次真实部署演练。
- **`agent-framework-reports` 门禁（2026-10-15 起）**：`check_reports.py` 要求 PR body 含 h2「报告 1/4」「Checklist」「生产侧事项」三段且无未消解 ❌；CI 读事件时刻 body 快照，事后补 body 须推空 commit。

**测试的环境无关性（2026-10-15 起，Issue #44）**：用例结果不得取决于「在哪台机器上跑」。最脆的一维是**宿主 systemd**：`switch.sh` 的 DRY_RUN 守卫（PR #42）以 `systemctl show -p MainPID / -p Environment --value <unit>` 取证，于是 dev/CI（**无可用 systemd** —— dev 机没有 `systemctl` 命令；CI 镜像有该命令，但 systemd 未作 PID 1 致取证为空；两者都判「无法判定」→ 仅告警放行）与生产 VPS（`dailycheck-green.service` 固定 `Environment=DRY_RUN=true`，绿未「转正」→ 按设计 `exit 3` 拒跑）结论相反 —— 同一批 `test_blue_green_implementation.py` 用例在 prod 全红、在 dev/CI 全绿（Issue #44 现场 = 跑 `verify.sh` 时 check 1 报 5 个 fail）。**约定**：凡调用 `scripts/blue_green/*.sh` 的用例，必须把宿主 systemd 态**显式钉死**，不许默认「本机没有 systemctl」。`test_blue_green_implementation.py` 用模块级 autouse fixture `_fake_systemctl` 前置 fake `systemctl`（`Environment` 恒 `DRY_RUN=false` =「绿转正后的合法生产态」→ 守卫判 `not_dry_run` 放行），使用例回到「验证切流量动作本身」的原始意图；守卫自身行为由 `test_blue_green_dryrun_guard.py` 专项覆盖（该文件自建按 unit 注入 `Environment` 的 fake）。两处 fake 均只接管 `show`，其余子命令 `exit 0`，故 `journalctl` 等真命令缺失仍能让 `verify.sh` 的失败路径用例保持原语义。

**Pre-existing 16 failed 分布**（2026-10-13 起）：
- `mcp_server×2 / aux_unit×2 / forecast_pure×8 / items_route×2 / procurement_route×1 / recipe_cost_route×1`
- （`nav_integration×2` 已于 2026-10-13 随导航模块化修复并移除）

---

## 13. 关键约定与坑

### 13.1 Schema 迁移

- ✅ 任何新列**只**加到 `migrate_warehouse_db_columns()`，**不**进 `WAREHOUSE_SCHEMA`（tests 断言）
- ✅ 改 ALTER 块必须 bump `WAREHOUSE_SCHEMA_VERSION`（当前 = 2）
- ✅ master.db 任何 schema 变更同步进 `init_master_db()`，否则 prod 首请求 `no such column`
- ✅ `init_master_db()` 用 `fcntl.flock` 保护 gunicorn 多 worker

### 13.2 Jinja2 陷阱

- ❌ **不要**在 `base.html` sidebar block 里 `{% set is_admin = ... %}` — block scope 不传子模板
- ✅ `is_admin` / `is_rd` / `is_storefront` 必须从 `blueprints/_helpers.py:register_template_context` 注入
- ✅ `current_tenant_id`（v2 账号管理）同样从 `register_template_context` 注入；缺失时回落 `None`，不要在模板里假设存在
- ✅ 仓库类型权威来源：`g.warehouse.warehouse_type`

### 13.3 时区不一致（**生产 bug**）

- **dev 容器 = UTC**（`/etc/timezone`）
- **prod 进程 = CST+8**（unit 未设 TZ）
- `datetime.now()` = 进程时区；`datetime('now')` SQL = UTC
- **时间类 bug 在 dev 不可复现、生产必现**

### 13.4 数量精度

- 所有数量用 `Decimal` + 2dp 量化（`_helpers.parse_qty` / `fmt_qty`）
- float 会被 `0.1+0.2=0.30000000000000004` 污染

### 13.5 主数据铁律

- `canonical_pure.NEVER_TOUCH_COLUMNS` 列出扇出永不覆盖的字段（仓内 `is_active` / `is_orderable` 等）
- `fanout` 必须先备份（`backups/warehouses/{code}-{tag}.db`）

### 13.6 dev 副本验证套路

```python
# 1. 复制 db
shutil.copytree('/opt/dailycheck/db', tmp/'db')
# 2. 在 import app 之前覆盖路径（auth.py 用 from config import BASE_DIR 在导入期绑定）
import config, db
config.BASE_DIR = tmp; config.MASTER_DB = tmp/'db/master.db'
config.WAREHOUSE_DB_DIR = tmp/'db/warehouses'
db.MASTER_DB = config.MASTER_DB; db.WAREHOUSE_DB_DIR = config.WAREHOUSE_DB_DIR
# 3. import app 并设 session
from app import create_app
app = create_app()
with app.test_client() as c, c.session_transaction() as s:
    s['user_id'] = ...; s['warehouse_id'] = ...
# 4. 测试
# 5. shutil.rmtree(tmp)
```

⚠️ **坑**：手动 `Cookie: session=<签名串>` 在 `test_client` 里**不被识别**，必须用 `session_transaction()`。

### 13.7 AJAX 双路径契约（2026-10-11 起）

`static/ajax-form.js` + `register_ajax_handlers(app)` 联合实现"行内小动作不再整页重画"。

**约定**：
- 视图函数**不感知** AJAX / 非 AJAX 差异，依旧 `redirect(url) + flash(msg)`（PRG 模式不变）。
- 客户端 marker：`X-Requested-With: fetch`（主） OR `Accept: application/json`（forward-compat）—— 任一即可。
- 服务端 after_request hook 仅在 3xx 命中时改写；4xx / 5xx 原样透传（让 `fetch().ok=false` 走 alert 失败分支）。
- ⚠️ **坑**：after_request hook **必须返回 Response 对象**，不能返回 `(jsonify(...), 200)` tuple。Flask 后处理 `response.vary.add("Cookie")` 对 tuple AttributeError。

**模板侧**：
- 行内 form 加 `data-ajax`；删除/回退类给行 `<tr>` / `<div>` 加 `data-row`，点完该行消失。
- toggle 类（启用停用、角色保存、标记已读）显式 `data-ajax-mode="reload-stay"` —— reload 但用 sessionStorage 缓存 scrollY + pageshow 还原，**不能**用默认 remove（会把状态文字一起删）。
- `onsubmit="return confirm(...)"` 在 ajax 路径下失效，必须迁移到 `data-ajax-confirm`。

**非 AJAX 路径**（curl / Python 测试 / MCP / 浏览器整页 POST）—— 302 redirect 字节级不变。

### 13.8 时间戳解析（**生产 500 来源**，Issue #38）

- **生产存量存在无秒形态**：2026-10-15 全 6 仓 + master 普查，只有 `wh_000.db` 的 `stock_movements.created_at` 与 `restock_requests.created_at` 各有 **54 行** 是 16 字符 `YYYY-MM-DD HH:MM`（跨度 2025-11-28 → 2026-10-13，**仍在产生**）。应用层所有写入点都走 `_helpers.now()` 的 19 字符格式，故来源是人工 / ops SQL。
- **读侧一律用 `reports_health_pure.parse_event_ts()`**，禁止裸 `datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")`——严格匹配撞上无秒数据即 `ValueError` → `/report/health` 500（`replay_item_layers` 与 `compute_stocktake_quality` 两个站点都会炸）。
- **未知形态必须响亮失败**：`parse_event_ts` 抛 `ValueError` 且消息带原始值（原始值进 traceback 是本次事故能在 prod 一眼定位的原因）。**不许在调用点大面积 `try/except` 吞掉**——FIFO 账龄重放依赖每条流水的时间，静默跳过一条事件会把呆滞金额算错（错得无声无息，比 500 更难发现）。
- **唯一豁免：聚类型指标**。均值 / 占比类（如 `compute_fulfillment` 的平均履约周期）丢一个样本只让分母变小，**不会**像 FIFO 重放那样让整体结果错位，故允许 `except ValueError: continue`——但**必须留 WARNING 日志**（`logger.warning` 带原始异常消息），保持「不静默丢数据」这条线。
- **混排形态下 SQL 侧字典序比较在分钟粒度内非严格时序**：`created_at >= ?` / `>= recent_start`（`reports_health_pure.py` 的窗口过滤）是**字符串**比较，16 字符值在同一分钟内字典序排在 19 字符值之前。影响仅限 cutoff 前后 1 分钟内的行被不对称算入，量级可忽略，但别据此做严格时序推断。
- **不用 `datetime.fromisoformat` 兜底**：3.11+ 它会接受带时区偏移的字符串并返回 aware datetime，与调用方的 naive `now` 相减会 `TypeError`。本模块的时间语义是「本地 naive 字符串字典序」，不引入 aware 值。

### 13.9 DRY_RUN 拦截日志级别（2026-10-15 起，Issue #41）

- `db/dryrun.py` 的写拦截日志必须是 **WARNING** 级、带 `[dry_run] BLOCKED` 可检索前缀（`tests/test_dryrun_middleware.py` 有断言，降级即红）
- 原因：app 不设 `logging.basicConfig`（只有 `mcp_server/main.py` 设 INFO）→ 生产 root logger 默认阈值 WARNING，**INFO 级拦截日志被静默吞掉**——2026-10-15 事故取证时被吞写清单只能靠实例 access log 反推
- 约束：新增任何「必须留痕」的运维事件日志照此办理（WARNING + 可检索前缀），**不得**降级为 INFO/DEBUG

---

## 14. pre-existing 风险清单

| 风险 | 严重度 | 状态 | 处理 |
|------|--------|------|------|
| 16 failed 测试（本地全量口径，2026-10-13 起） | 中 | pre-existing | 商业化前必须归零 |
| 时区不一致 | **高** | pre-existing | 多租户改造前必修 |
| 配方成本 qty 语义不一致 | **高** | **已修复（2026-10-16，`feat/recipe-rd-upgrade`）** | ic_recipe 引用行 qty 统一为**克**（`_save_recipe` output_qty 累计 / pure 成本层 / 前端 JS / BOM CSV 四处对齐，旧「份」语义废弃）。两库 recipes 表均 0 行，无存量迁移。详见 `docs/2026-10-16-recipe-rd-upgrade.md` §D1 |
| 全仓 11 处 `strptime(..., "%Y-%m-%d %H:%M:%S")` 严格匹配 | 中 | pre-existing（2026-10-15 登记，Issue #38 同源） | `app.py:171` / `blueprints/consumption.py:27,67` / `blueprints/forecast.py:80,100` / `blueprints/procurement.py:185` / `rate_limit_pure.py:89,109` / `mcp_server/data/warehouse.py:265,316,676`（另有 `blueprints/store_ordering.py:91` 是 `%Y-%m-%d` 纯日期形态，同类不同款）。**当前无生产数据触发**（2026-10-15 普查显示它们读的 `outbound_requests` / `production_runs` / `rate_limit` 等列全为 19 字符），故 Issue #38 只收敛了 `reports_health_pure` 的 4 个调用点 / 3 个代码站点（`replay_item_layers`、`compute_stocktake_quality`、`compute_fulfillment`）；行号按 2026-10-15 `52516a8` 校准，改动这些模块时按符号名而非行号定位。后续改动这些模块的时间解析时按 §13.8 同一 helper 语义处理 |
| **绿环境以 `DRY_RUN=true` 承载真实流量 → 写操作被静默丢弃** | **高（已实际发生）** | 事故已止血；**根治已交付待 merge**（Issue #41 → PR #42） | prod nginx 已 `blue down / green weight=100`（自 ~16:53 起），而 `dailycheck-green.service` 声明 `Environment=DRY_RUN=true` → `DryRunConnection` 把 INSERT/UPDATE/DELETE 全部拦截 + 不 commit，用户侧仍收到 302 成功。**已确认丢失**：2026-10-15 18:11:35/37/43 三次 `POST /production/submit`（product_id=15）全部 302 但真库零落库。**结构性成因**：§3.4 的切流量流程没有任何一步清掉绿环境的 `DRY_RUN`。**处置**：生产侧已 `switch.sh blue 100` 止血（运行态 blue 100 / green down）；**根治**= §3.4 铁律新增的 `switch.sh` DRY_RUN 守卫（承接流量且 `DRY_RUN=true` → 拒跑 exit 3），已随 Issue #41 修复交付（PR #42）。⚠️ **下一次切流量前必须先按 `scripts/blue_green/README.md`「绿转正」关闭绿的写隔离**，否则会被守卫拒跑（预期行为） |
| `adjustment.py` 未注册 | 低 | pre-existing | 路由 404，前向兼容保留 |
| `fix/mcp-streamable-http` 落后主分支 | **高** | pre-existing | prod 不含本轮任何 P0 收敛 |
| `mcp-server` 进程 prod 未部署 | 中 | pre-existing | 仅 dev 有 |
| 导航是「隐藏式」而非「强制式」权限 | 中 | pre-existing（2026-10-14 登记） | 多处「路由权限 ≠ 导航可见性」：`/canonical/list` 仅 `@require_login`（任何登录角色可直连，导航只给 admin/rd）；`/store-ordering/{admin/orders,admin_report,shipping-rules}` 为 `@require_role("admin")`（仓库角色）而导航 gate 是平台 `is_admin`（单仓 `role=admin` 可直连但不显示）；对称地 DC 用户实际可访问 `/inventory` 但导航不展示。需在权限颗粒度项目中按统一权限模型收敛 |
| 侧边栏 `<h1>` 与模块标题同名 | 低 | pre-existing（2026-10-14 登记） | `base.html` 侧栏 `<h1>库存管理</h1>` 与「库存管理」模块标题重复；文案由 owner 定 |
| `/production*` 页无侧栏 | 低 | pre-existing（2026-10-14 登记） | `templates/production/{products,runs,product_edit,session}.html` 把 `sidebar` block 覆盖为空 → 桌面端在这些页没有导航（只能靠返回/页内链接）。本次新增的「生产记录」入口沿用同布局。移动端不受影响（`mobile-nav` 未被覆盖） |
| PWA push / background sync 缺失 | 中 | pre-existing | 商业化差异化卖点 |
| `sw.js` 导航分支的离线回落缓存不过鉴权 | 中 | pre-existing | `caches.match(req)` 会把安装时用会话凭据抓的 SSR HTML 按 URL 命中返回（缓存在同 profile 的离线/共享设备场景可读到他人页面）。与 §11「SW 缓存边界」的收窄范围无关（那条只覆盖非导航 GET），待另开 issue |

---

## 15. 文档索引

### 必读

- [`CLAUDE.md`](../CLAUDE.md) — 项目权威文档（dev 环境、目录、提交规范）
- [`AGENT.md`](../AGENT.md) — 精简开发指南
- [`docs/DEV_ENV.md`](DEV_ENV.md) — wdg-systemd 容器部署细节
- [`docs/mcp-configuration.md`](mcp-configuration.md) — MCP 接入
- [`docs/agent-framework/workflow.md`](agent-framework/workflow.md) — Dev/Prod/Owner 三方协作工作流（角色、门禁、merge 政策、蓝绿部署、应急）

### 部署 / 运维

- [`scripts/blue_green/README.md`](../scripts/blue_green/README.md) — 蓝绿部署操作手册（5 脚本用法 / 验证矩阵 / **「绿转正」标准步骤**，2026-10-15 事故后增补）

### 业务领域

- [`docs/2026-10-09-item-master-unify-plan.md`](2026-10-09-item-master-unify-plan.md) — canonical 主数据统一（P0-1~13）
- [`docs/2026-10-03-canonical-item-design.md`](2026-10-03-canonical-item-design.md) — 主数据原始设计
- [`docs/2026-10-03-canonical-item-prd.md`](2026-10-03-canonical-item-prd.md) — 主数据 PRD
- `docs/2026-10-04-store-ordering-*` — 订货流程 5 篇文档 + 4 张 mermaid
- `docs/2026-10-05-store-ordering-*` — v2/v3 迭代

### UI / 导航

- [`docs/2026-10-13-nav-modules.md`](2026-10-13-nav-modules.md) — 导航模块化 7 模块 IA（归属决策 + 三载体规则）

### 评估 / 计划

- [`docs/2026-10-10-commercialization-gap-assessment.md`](2026-10-10-commercialization-gap-assessment.md) — 商业化差距评估
- [`docs/2026-10-10-commercialization-p0-dev-test-plan.md`](2026-10-10-commercialization-p0-dev-test-plan.md) — P0 收敛开发/测试计划
- [`docs/runbook-dr.md`](runbook-dr.md) — 灾难恢复手册（备份/还原/整机重建）

### 集成

- `docs/integrations/dailycheck-mcp/` — MCP 接入文档 + examples

### 工作记忆

- [`.workbuddy/memory/MEMORY.md`](../.workbuddy/memory/MEMORY.md) — 跨 session 项目约定
- `.workbuddy/memory/YYYY-MM-DD.md` — 每日工作日志
- [issue-reports/](../issue-reports/) — 自动生成的 issue 检查报告（55 份）

---

## 附：维护本文件的工作流

每次 commit 触及下列子系统时，**必须**同步更新对应章节：

| 触及文件 | 须更新章节 |
|----------|-----------|
| `app.py` / `config.py` / `permissions.py` | §2 §3 §8 |
| `db/*.py` | §4 |
| `blueprints/auth.py` | §7 §8 |
| `blueprints/items*.py` | §5 §6 §10 |
| `blueprints/canonical*.py` | §5 §6 §10.2 §10.4 |
| `blueprints/store_ordering*.py` | §5 §6 §10.3 |
| `blueprints/recipe_cost*.py` | §5 §6 |
| `blueprints/forecast*.py` / `procurement*.py` / `notifications*.py` | §5 §6 |
| `blueprints/production.py` / `stocktake.py` / `restock.py` / `outbound.py` / `adjustment.py` / `consumption.py` / `reports.py` | §5 §6 |
| `blueprints/users.py` / `import_items.py` / `agent_tokens.py` | §5 §6 §8 |
| `mcp_server/*.py` | §9 |
| `templates/**` | §5 §11 |
| `static/**` | §11 |
| `tests/**` | §12 |
| `.github/workflows/*` / `deploy/**` / `scripts/**` | §3 §12 §附 |
| `docs/ARCHITECTURE.md`（本文件） | 自更新 |

**门禁脚本**：`scripts/git-hooks/pre-commit` 已装。运行 `python scripts/arch_touched_sections.py` 看本 commit 该改哪些章节。
