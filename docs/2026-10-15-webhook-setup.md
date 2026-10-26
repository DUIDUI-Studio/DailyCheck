# DailyCheck Webhook 部署操作手册

> **⚠️ 2026-10-22 迁移提示（GitHub → 自托管 Gitea）**
>
> 本文记录的 receiver / inbox / watcher 链路设计**仍然有效**，但事件来源已换：
>
> - 事件头：Gitea 同时发 `X-GitHub-Event`（GitHub 兼容）与 `X-Gitea-Event`；签名头
>   `X-Hub-Signature-256: sha256=<hex>` 与 GitHub **格式完全一致**，验签代码不用改
> - **没有 `check_suite` 事件**，改用 `status` 事件（payload 含 `state`/`context`/`sha`，
>   但没有 PR 关联信息，需按 sha 反查 open PR）
> - PR 事件被细分：`pull_request`（open/close/reopen）+ `pull_request_sync`（推送新 commit），
>   评论走 `issue_comment` 且带 `X-GitHub-Event-Type: pull_request_comment`
> - **Gitea 默认禁止向内网地址投递 webhook**，需在 `app.ini` 配
>   `[webhook] ALLOWED_HOST_LIST = 172.28.129.215,172.28.129.216`
> - 内网直连：hub1 `172.28.129.216` → receiver `172.28.129.215:8443`，不走公网



> **目的**：Dev Agent 提完 PR 后，Gitea repo 自动通过 webhook 通知 VPS 上的 receiver，receiver 写入 `inbox.jsonl`，`dc-inbox-watcher` 订阅 inbox **≤5s** 触发 Prod Agent 入口跑 merge 决策。
>
> **目标读者**：owner / Prod Agent 手动部署。脚本走不走通不在 Dev 自动化范围（基础设施变更需 owner 拍板）。
>
> **设计文档**：`docs/agent-framework/case-studies/2026-10-15-self-contained-dev-loop.md` 第 6 章"蓝绿部署" + 第 10 章"复盘"。
>
> **预计耗时**：50-70 分钟（DNS / **443 放行核验** / **申请 hook 独立证书** / systemd / nginx / GitHub 配 webhook / §3.8 watcher+rotate）。
>
> **更新**：2026-10-16 增补 §3.8（inbox watcher + rotate，Issue #47）—— 链路「inbox.jsonl → Prod Agent」从「Prod 侧自行实现」变为本手册可部署的实装。
>
> **更新**：2026-10-16 首次部署完成后修正 —— ① 证书改**独立申请**（阿里云免费证书=单域名，不能给主证书加 SAN）；② 新增 **443 防火墙放行**前置与外部 vantage 复验；③ nginx 改**独立文件**部署；④ systemd `ProtectHome` 203/EXEC 踩坑记录。
>
> **更新**：2026-10-16 增补 **§3.10（CI 完成快通道）** —— webhook 补勾 "Check suites" 事件；`check_suite`（全绿）经同一 receiver → inbox → watcher 链路秒级唤醒 Prod Agent；新增 `wdg-ci` 订阅 + `webhook.py` / `scheduler.py` 两处 [SILENT] 抑制补丁（含「截断标记」容错，§3.8 步骤 2c）。
>
> **更新**：2026-10-16 深夜增补 **§3.11（审批跨会话修复）** —— 危险命令审批对 webhook 会话死锁的根治补丁（`gateway/run.py` + `tools/approval.py` 本地补丁）；修复后 Eric 在飞书回复 `/approve` 即可放行 Prod Agent 的危险命令。
>
> **更新**：2026-10-26 增补 **Issue #54 event reducer** —— `dc-notify-prod-agent`
> 先经 SQLite reducer 做 delivery/等价事件去重、短窗口合并、单 PR 租约和 429
> cooldown，再按事件唤醒 provider；见 §3.8a。
>
> **更新**：2026-10-26 **Issue #56 deterministic merge-ready gate** ——
> `pull_request` 只记账且永不唤醒 Hermes；CI 中间态/失败零 provider 请求；
> 只有 Gitea 确认 open / same head / mergeable / required checks 全绿时，
> 每个 `(repo, PR, head)` 才产生一次 `MERGE_READY → wdg-ci`。

## 1. 架构

```
Dev Agent (本地)                                VPS (112.124.18.246)
   ↓ POST /pulls（Gitea API）              ┌────────────────────────┐
Gitea Repo (hub1:3000 eric/dailycheck)     │  nginx :443            │
   ↓ Webhook: pull_request                 │   server_name hook.*   │
   ↓ POST + HMAC-SHA256 签名               │   proxy_pass ───────┐  │
   ↓                                        │   limit_req 防滥刷  │  │
hook.kjtang.cn:443 (新子域名)               │                      ▼  │
   ↓                                        │  ┌─────────────────────┐│
                                            │  │  dc-webhook-receiver ││
                                            │  │  Flask on 127.0.0.1  ││
                                            │  │  :8443               ││
                                            │  │  - HMAC 验签          ││
                                            │  │  - 幂等 (delivery_id)││
                                            │  │  - 反压 (inbox 满)   ││
                                            │  │  - 写 inbox.jsonl    ││
                                            │  └─────────────────────┘│
                                            │           │              │
                                            │           ▼              │
                                            │  /var/lib/dailycheck-    │
                                            │  prod-agent/inbox.jsonl  │
                                            │           │              │
                                            │           ▼              │
                                            │  dc-inbox-watcher        │
                                            │  （≤5s 触发，§3.8）       │
                                            │           │              │
                                            │           ▼              │
                                            │  dc-notify-prod-agent    │
                                            │  → Prod Agent 入口        │
                                            │  → 跑 merge 决策          │
                                            └────────────────────────┘
```

## 2. 前置确认（必做）

| # | 项 | 验证命令 / 操作 | 期望 |
|---|----|-----------------|------|
| 1 | 域名 A 记录可加 | 阿里云 DNS 控制台 | 当前 `kjtang.cn` / `www.kjtang.cn` 已解析 → 112.124.18.246 |
| 2 | **443 入站已放行** | 见下方「⚠️ 443 核验」 | 外部 vantage 可达（**本机 curl 不算数**） |
| 3 | 阿里云 SSL 可新增独立证书 | 阿里云 SSL 控制台 | 免费「个人测试证书」额度可用（每年 20 张） |
| 4 | /var/lib/dailycheck-prod-agent 目录 | `mkdir -p /var/lib/dailycheck-prod-agent && chown root:root` | 写入权限 + inbox 落盘 |
| 5 | .venv 路径正确 | `ls /root/DailyCheck/.venv/bin/gunicorn` | 存在 |
| 6 | 8443 端口空闲 | `ss -tlnp \| grep 8443` | 空（已被实测空闲） |

### ⚠️ 443 入站核验（2026-10-16 血泪教训，必做）

**背景**：VPS 的 ufw 二进制已卸载（`dpkg -l ufw` 状态 `rc`），但**残留 iptables 链仍在生效**，
`INPUT` policy = DROP，唯一放行清单是 `ufw-user-input` 链。2026-10-15 主站 HTTPS 上线时
**漏了 443 放行** → `kjtang.cn` 的 443 对公网一直不可达（本地验证却全绿）。本次 webhook 部署一并修复。

**最高危陷阱**：**VPS 本机 `curl https://...` 走 `lo` 接口被直通，照样返回 200 —— 不能证明外网可达！**
（本次故障被本地验证整整掩盖一天。）外网可达性必须从**外部 vantage** 复验（§4 验收第 6 项）。

```bash
# 1. 查放行清单是否含 443（期望有输出；无输出 = 缺放行 = 外网 502/超时）
iptables -S ufw-user-input | grep -E 'dport 443'

# 2. 若缺，补放行（运行态）：
iptables -I ufw-user-input 1 -p tcp -m tcp --dport 443 -j ACCEPT

# 3. 持久化（不写文件则重启失效）——同步 /etc/iptables/rules.v4：
#    在已有 '--dport 80 -j ACCEPT' 行后补同格式 443 行（或 netfilter-persistent save 全量落盘）
netfilter-persistent save

# 4. 复验：外部 vantage（§4 验收第 6 项）；另跑 nginx -T 确认运行态
```

> - 不要用 `ufw allow 443`（二进制已卸载）；不要只改 `rules.v4` 不应用（运行态不变）。
> - `iptables -S` 会警告 `iptables-legacy tables present` —— 属残留表，**放行链以默认（nft 后端）输出为准**。
> - `netfilter-persistent.service` = enabled，重启时从 `/etc/iptables/rules.v4` 恢复 → 文件必须同步。

## 3. 实施步骤

### 3.1 DNS 加 A 记录（5 分钟）

1. 登录阿里云 DNS 控制台
2. 选择 `kjtang.cn` 域名
3. 添加记录：
   - 主机记录：`hook`
   - 记录类型：A
   - 记录值：`112.124.18.246`
   - TTL：10 分钟（短 TTL 调试期）
4. 验证：`dig +short hook.kjtang.cn` → 应返回 `112.124.18.246`

### 3.2 申请 hook.kjtang.cn 独立证书（15-30 分钟，含签发等待）

> ⚠️ **阿里云免费「个人测试证书」是单域名产品** —— **不能**给现有主站证书追加 SAN。
> hook 必须**单独申请**一张证书；主站证书完全不动（各自独立 90 天轮换，互不影响）。
> （2026-10-16 修正：原文写的「重签主证书加 SAN」在阿里云免费证书上行不通。）

1. 登录阿里云 SSL 控制台 → 「免费证书」→ 创建证书
2. 域名填 `hook.kjtang.cn`（DNS 自动验证要求域名在本账号 —— `kjtang.cn` 已满足）
3. 提交审核（DV 通常几分钟内签发；2026-10-16 实测签发，有效期至 **2027-01-13**）
4. 下载（nginx 格式 = pem + key 两个文件）
5. 上传到 VPS 并设权限：
   ```bash
   scp <cert>.pem root@112.124.18.246:/etc/nginx/ssl/hook.kjtang.cn.pem
   scp <key>.key  root@112.124.18.246:/etc/nginx/ssl/hook.kjtang.cn.key
   ssh root@112.124.18.246 "chown root:root /etc/nginx/ssl/hook.kjtang.cn.pem /etc/nginx/ssl/hook.kjtang.cn.key \
     && chmod 644 /etc/nginx/ssl/hook.kjtang.cn.pem && chmod 600 /etc/nginx/ssl/hook.kjtang.cn.key"
   ```
6. 验证（在 VPS 上）：
   ```bash
   openssl x509 -in /etc/nginx/ssl/hook.kjtang.cn.pem -noout -subject -dates
   # 期望：subject=CN=hook.kjtang.cn；notAfter 约 90 天后
   openssl x509 -in /etc/nginx/ssl/hook.kjtang.cn.pem -noout -text | grep -A1 'Subject Alternative Name'
   # 期望 SAN 含 DNS:hook.kjtang.cn
   ```
7. 记下到期日（90 天轮换）—— 届时重复本步骤重签，**不需动主站证书**。

### 3.3 写 WEBHOOK_SECRET 文件（1 分钟）

```bash
# 生成 32 字节强密码（base64 = 43 字符）
WEBHOOK_SECRET=$(openssl rand -base64 32)
echo "WEBHOOK_SECRET=${WEBHOOK_SECRET}" > /etc/dailycheck/webhook-secret
chown root:root /etc/dailycheck/webhook-secret
chmod 600 /etc/dailycheck/webhook-secret

# 记下这个 secret（下一步配置 GitHub webhook 要用）：
cat /etc/dailycheck/webhook-secret
```

**注意**：secret 不会出现在任何 git 仓库 / 文档 / prompt 里——只在 systemd unit 的 `EnvironmentFile=` 引用 + GitHub repo webhook 设置里。

### 3.4 部署 systemd unit（3 分钟）

```bash
# 1. 复制 unit
cp deploy/systemd/dc-webhook-receiver.service /etc/systemd/system/

# 2. 改路径：dev 仓库 unit 写的是 /root/DailyCheck/.venv（已对齐）
#    如果未来 /root/DailyCheck 路径变了，unit 也要改

# 3. 重新加载 + 启用
systemctl daemon-reload
systemctl enable dc-webhook-receiver.service
systemctl start dc-webhook-receiver.service

# ⚠️ 若 start 报 status=203/EXEC —— 是 ProtectHome 沙箱问题：
#    服务代码/venv 在 /root/DailyCheck 下，ProtectHome=true 会让 /root 不可见。
#    本仓库 unit 已修正为 ProtectHome=read-only（2026-10-16 实测踩坑）；
#    若从旧版本部署，先核对 unit 里这一行再 restart。

# 4. 探活
curl -sS http://127.0.0.1:8443/health
# 期望：{"inbox_path":"/var/lib/dailycheck-prod-agent/inbox.jsonl","inbox_size":0,"max_inbox_lines":1000,"status":"ok"}

# 5. 跟日志
tail -F /var/log/dailycheck/webhook-receiver-access.log
tail -F /var/log/dailycheck/webhook-receiver-error.log
```

### 3.5 部署 nginx 配置（5 分钟）

```bash
# 1. 独立文件部署（⚠️ 不要追加进 dailycheck 文件——那个文件归蓝绿 switch.sh 管，
#    混写会被切换流程的备份/重写波及；独立文件与之完全解耦）
cp deploy/nginx/hook.kjtang.cn.conf /etc/nginx/sites-enabled/hook.kjtang.cn

# 2. 验证语法
nginx -t
# 期望：syntax is ok / test is successful
# 若报 limit_req_zone "not allowed here" / "duplicate" → 它被放进了 server{}，
#   必须在文件顶部 http 上下文（见配置文件头注释）

# 3. reload（流式，0 中断）
systemctl reload nginx

# 4. 本机路由验证（经 nginx → receiver，SNI 正确）
curl -sI --resolve hook.kjtang.cn:443:127.0.0.1 https://hook.kjtang.cn/health
# 期望：HTTP/2 200

# ⚠️ 本机 200 ≠ 外网可达（lo 直通陷阱，见 §2）→ 必须外部 vantage 复验（§4 第 6 项）
```

**验证 nginx 运行态（conf 文件 ≠ 运行态，写文件后 reload 失败会悄悄分歧）**：

```bash
nginx -T 2>/dev/null | grep -c 'server_name hook.kjtang.cn'   # 期望：2（443 + 80 两个 server）
```

### 3.6 配 Gitea repo Webhook（3 分钟）

**入口**：`http://172.28.129.216:3000/eric/dailycheck/settings/hooks`（VPS）
或 `http://hub01:3000/eric/dailycheck/settings/hooks`（本机 Tailscale）。

1. 「添加 Webhook」→ 类型选 **Gitea**（不是 Gogs，也不是 Slack 等）
2. 目标 URL：`https://hook.kjtang.cn/webhook`
3. HTTP 方法：`POST`；POST 内容类型：`application/json`
4. 密钥：粘贴步骤 3.3 的 `$WEBHOOK_SECRET`
5. 触发条件选「自定义事件」并**只勾这 4 个**：
   - `pull_request`（开/关/重开/改标题）
   - `pull_request_sync`（推新 commit —— 对应 GitHub 的 synchronize）
   - `pull_request_comment`（PR 评论）
   - `status`（commit 状态变化 = CI 完成快通道，§3.10）
6. 勾「激活」→ 添加

> ⚠️ 当前实例上这个 hook 是**一次配好、事件清单已含上述 4 项**的既有配置（hook #3）。
> 重建时照上表勾，别照 GitHub 的「Pull requests + Check suites」——Gitea 没有 Check suites。
>
> ⚠️ Gitea 默认拒绝向内网地址投递 webhook。本链路走公网 `hook.kjtang.cn`，不受限；
> 若改成内网直投，需在 `app.ini` 配 `[webhook] ALLOWED_HOST_LIST`。

### 3.7 端到端验证（5 分钟）

1. 打开 `http://172.28.129.216:3000/eric/dailycheck/settings/hooks` → 点进 hook #3
2. 「最近投递」应至少有 1 条 **ping/测试**记录；失败先修 443 可达性（见 §2）
3. 让 Dev Agent 提一个测试 PR（或 Web UI 上开一个空 PR）
4. 「最近投递」应出现 `pull_request` 记录，HTTP 响应 **202**；
   若为 **401** = 签名不对（secret 不一致）；**503** = inbox 满（反压生效，属预期）
5. 验证 inbox 落盘：
   ```bash
   tail -1 /var/lib/dailycheck-prod-agent/inbox.jsonl | python3 -m json.tool
   # 期望：含 pr_number / pr_title / pr_head_sha / pr_author 等字段
   ```
6. 验证 Prod Agent 订阅：见 §3.8（watcher 部署后 inbox 新行 ≤5s 触发）

### 3.8 部署 inbox watcher + rotate（10 分钟，Issue #47）

receiver 只负责「把事件落进 inbox」；本步部署「inbox → Prod Agent」的唤醒桥：
`dc-inbox-watcher`（常驻，≤5s 触发触发命令）+ `dc-inbox-rotate`（每日归档 >7 天旧行）。

```bash
# 1. 触发命令适配器（把事件投递给 Prod Agent 入口）
cp deploy/scripts/dc-notify-prod-agent.sh /usr/local/bin/dc-notify-prod-agent
cp scripts/prod_agent/event_reducer.py /root/DailyCheck/scripts/prod_agent/event_reducer.py
chmod 755 /usr/local/bin/dc-notify-prod-agent
#    ⚠️ exec 行必须用 venv 绝对路径 /root/hermes-agent/.venv/bin/hermes ——
#    /root/hermes-agent/hermes 的 shebang 是 #!/usr/bin/env python3，在 watcher 的
#    PATH（/root/DailyCheck/.venv/bin 排第一）下解析到 DailyCheck venv（缺 PyYAML）
#    → 触发恒失败 rc=1 ModuleNotFoundError: No module named 'yaml'（2026-10-16 实测）。
#    默认实现 = Hermes webhook 订阅（见步骤 2）；若 Prod Agent 入口不同，
#    按脚本头部注释替换 exec 那一行即可，watcher 不需要改动。

# 2. （Hermes 侧，一次性）启用 webhook platform + 建订阅
#    读取路径有两条，必须双写（2026-10-16 实测）：
#      · CLI（hermes webhook list/subscribe/test）读 config.yaml 顶层 platforms；
#      · gateway 启动读 .env 的 WEBHOOK_* 环境变量。
#    a) /root/.hermes/config.yaml 顶层追加（注意是顶层，display.platforms 无关）：
#      platforms:
#        webhook:
#          enabled: true
#          extra:
#            host: "127.0.0.1"        # 仅本地（投递者是本机 watcher），勿暴露公网
#            port: 8644
#            secret: "<随机强密码>"
#    b) /root/.hermes/.env 追加（三键，secret 与上一致）：
#      WEBHOOK_ENABLED=true
#      WEBHOOK_PORT=8644
#      WEBHOOK_SECRET=<同一随机强密码>
#    重启 gateway 生效（root 用户级 systemd；重启会中断进行中的会话）：
XDG_RUNTIME_DIR=/run/user/0 systemctl --user restart hermes-gateway.service
ss -tlnp | grep 8644           # 期望 127.0.0.1:8644 LISTEN
#    然后确保 CI 订阅存在（CLI 一律走 venv 绝对路径，理由同步骤 1）。
#    Issue #56 起不再需要 wdg-pr；旧订阅可保留但新代码不会读取：
cd /root/hermes-agent
/root/hermes-agent/.venv/bin/hermes webhook subscribe wdg-ci \
  --prompt 'DailyCheck MERGE_READY: PR #{pr_number}，head {pr_head_sha}，required checks 已由 reducer 确认为全绿。走 dailycheck-prod-agent skill 完成 merge 前置检查与合并；无需动作 → 仅回 [SILENT]；有实质动作 → 最终回复恰好一行 ≤50 字轻提醒。' \
  --skills dailycheck-prod-agent
/root/hermes-agent/.venv/bin/hermes webhook list            # 确认 wdg-ci 在列
#    （prompt 文案可按需调整；中间态不再进入 Hermes，[SILENT] 仅供 agent 自身无动作时使用）

#    c) （2026-10-16 增，CI 快通道静默必需）[SILENT] 抑制补丁 ×2（均本地补丁）：
#       ① webhook 跨平台投递抑制（上游无此能力 —— 上游仅 cron 投递支持 [SILENT]，
#          webhook 会把它当普通文本原样发出）。在 gateway/platforms/webhook.py 的
#          send() 中、「Cross-platform delivery」分支之前插入：
#             _silent_check = content.strip().upper()
#             if "[SILENT]" in _silent_check or _silent_check.endswith("[SILENT"):
#                 logger.info("[webhook] Suppressed delivery for %s ([SILENT])", chat_id)
#                 return SendResult(success=True)
#       ② cron 投递抑制加截断容错（上游已有检查、但仅匹配完整标记）：cron/scheduler.py
#          tick() 投递判定改为：
#             _silent_check = deliver_content.strip().upper()
#             if should_deliver and success and (
#                 SILENT_MARKER in _silent_check or _silent_check.endswith("[SILENT")
#             ):
#          （兜底轮询的静默 tick 依赖此检查；scheduler 运行在 gateway 进程内，重启同生效）
#       ⚠️ 为何额外容忍 endswith("[SILENT")：模型（MiniMax-M3）会**间歇把标记输出成
#          "[SILENT"（丢右括号，finish_reason=stop）**——2026-10-16 实测，严格子串匹配
#          漏过 → 字面标记直接投给用户。两处统一静默判据 =「含完整 [SILENT] 或以其
#          截断形态结尾」。
#       改完重启 gateway 生效（同 a/b 的 restart 命令）。
#       ⚠️ 两处均为本地补丁，Hermes 重装/升级后需重打（旧版备份 *.bak-20261016-*）——
#          核对：grep -n "_silent_check" \
#                /root/hermes-agent/gateway/platforms/webhook.py \
#                /root/hermes-agent/cron/scheduler.py

# 3. watcher + rotate units
cp deploy/systemd/dc-inbox-watcher.service \
   deploy/systemd/dc-inbox-rotate.service \
   deploy/systemd/dc-inbox-rotate.timer \
   /etc/systemd/system/
#    ⚠️ 覆盖后核对两处 ProtectHome=read-only（旧版 unit 若为 =true，/root 被全屏蔽 →
#    203/EXEC 无限重启；2026-10-16 实测因此空转 1800+ 次）：
grep ProtectHome /etc/systemd/system/dc-inbox-watcher.service /etc/systemd/system/dc-inbox-rotate.service
systemctl daemon-reload
systemctl enable --now dc-inbox-watcher.service
systemctl enable --now dc-inbox-rotate.timer

# 4. 链路验证（秒级）
systemctl status dc-inbox-watcher --no-pager
journalctl -u dc-inbox-watcher -n 20 --no-pager   # 期望 [inbox_watcher] started: ...
echo '{"delivery_id":"test-1","event":"pull_request","action":"opened","repo_full_name":"eric/dailycheck","pr_number":1,"pr_title":"test","pr_head_sha":"deadbeef","received_at_ms":0}' \
  >> /var/lib/dailycheck-prod-agent/inbox.jsonl
sleep 3
journalctl -u dc-inbox-watcher -n 5 --no-pager     # 期望出现 triggered: delivery=test-1
# reducer 应返回 silent/pr_state_updated；Hermes 与 LLM 会话数均为 0

# 5. 手工跑一次 rotate（验证归档链路）
systemctl start dc-inbox-rotate.service
journalctl -u dc-inbox-rotate -n 5 --no-pager      # 期望 done: archived=N kept=M ...
systemctl list-timers dc-inbox-rotate.timer --no-pager   # 期望下次 00:30
```

**注意**：

- **首次部署 watcher 时，inbox 里已存在的历史条目会被立即触发一遍**（无状态文件 = 全部视为待触发；保守设计宁多勿漏）。不想打搅就先跑步骤 5 的 rotate 清旧行，或接受首次补触发（merge 判定幂等，无副作用）。
- inbox 是接收器反压闸门（≥1000 行时 receiver 返 503 拒新通知）；rotate 每日归档 >7 天旧行，让行数回落。归档文件 `inbox.archive.jsonl` 只增不删（低频，人工按需清理）。
- watcher 的触发失败会 WARNING + 指数退避重试（journal 可见）。⚠️ **已知限制（Issue #51）**：重试由主循环的「inbox 文件签名变化」gating —— inbox 无新行时退避条目**不会自行重试**（2026-10-16 实测延迟 1h：11:38 部署后失败，14:29 新事件写入才连带补发）。webhook 整体故障时由 **§3.9 兜底轮询**保底（≤30 分钟发现新 PR）。
- watcher 的 systemd 沙箱默认只放行 `/var/lib/dailycheck-prod-agent` + `/root/.hermes` 写权限；若替换触发命令且需要别的写路径，在 unit 的 `ReadWritePaths` 追加。

### 3.8a Event reducer / 持久队列（Issue #54）

`inbox.jsonl` 仍是**唯一事件源**：receiver 只写它，watcher 只读它；reducer 是该
事件进入 Prod Agent 前的状态机，不另开轮询/订阅，也不复制第二条 inbox。状态 DB
默认 `/var/lib/dailycheck-prod-agent/event_reducer.sqlite3`，使用 SQLite WAL、
事务 claim 与 partial unique index，保证同一 PR 最多一个 active trigger。

**输出与退出码**（watcher 兼容）：

| 输出 decision | 含义 | shell 退出 | watcher 行为 |
|---|---|---:|---|
| `suppress` | delivery 重复、等价事件已完成、坏输入或 stale head | 0 | 记 handled，不再重试 |
| `defer` | 已持久入队；可能因 debounce、同 PR active lease 或 429 cooldown 等待 | 75 | 非 0，按原退避重试 |
| `trigger` | 已原子 claim 一个 lease，随后投递 Hermes | 0（HTTP 2xx 入队 + complete 成功） | 记 handled |

`trigger` 的 stdout JSON 含 `route / trigger_id / lease / lease_expires_ms /
payload`。adapter 用 stdlib sender 向 `127.0.0.1:8644/webhooks/<route>` 提交
原样 JSON body，事件头取真实 `pull_request` / `check_suite` / `deploy_result`，
并使用 `trigger_id` 派生稳定的语义 `X-GitHub-Delivery`。HMAC secret 只从
`/root/.hermes/webhook_subscriptions.json` 对应 route 读取，不回显。
Hermes 返回 HTTP 2xx 只表示**已入队**（正常为 202），不代表 agent turn 已结束；
adapter 随即将 reducer 行标 `complete`。HTTP 429 的 immediate `Retry-After` 会写入
provider cooldown 和 `last_error`，其它 HTTP/传输失败也持久化为非 quota 重试。
**defer 不是成功消费**：事件一直在 DB 中，且 watcher 仍会重试；drain timer 只是
watcher 停机/重启时的第二 recovery 路径。

per-PR lease 默认 **1,200,000 ms（20 分钟）**，必须长于 Hermes
`agent.gateway_timeout`。这样 gateway 已收 202 但 agent 仍在运行时，lease 到期的
drain 不会再为该 PR 生成第二个 active dispatch。若生产把
`agent.gateway_timeout` 调高，必须同步调大 `DAILYCHECK_REDUCER_LEASE_TTL_MS`。

**安装与 rollout**：

```bash
# 0. 前置：确认两个 provider route 存在，并确认 gateway 的 inactivity timeout 小于
#    1,200,000 ms lease。缺订阅时不要覆盖生产 adapter。
/root/hermes-agent/.venv/bin/hermes webhook list | grep -q '◆ wdg-ci'
/root/hermes-agent/.venv/bin/hermes webhook list | grep -q '◆ dailycheck-deploy'

# 1. 先确认 reducer 可运行且 DB 健康（不会调用 provider）
/root/DailyCheck/.venv/bin/python \
  /root/DailyCheck/scripts/prod_agent/event_reducer.py status

# 2. 部署 adapter、HTTP sender、breaker 与 units，但先不启动 timer
cp deploy/scripts/dc-notify-prod-agent.sh /usr/local/bin/dc-notify-prod-agent
cp scripts/prod_agent/event_reducer.py \
   scripts/prod_agent/hermes_webhook_sender.py \
   scripts/prod_agent/provider_breaker.py \
   /root/DailyCheck/scripts/prod_agent/
cp deploy/systemd/dc-prod-agent-drain.service \
   deploy/systemd/dc-prod-agent-drain.timer \
   deploy/systemd/dc-prod-agent-provider-breaker.service \
   deploy/systemd/dc-prod-agent-provider-breaker.timer \
   /etc/systemd/system/
chmod 755 /usr/local/bin/dc-notify-prod-agent
systemctl daemon-reload

# 3. 手工验证三个 decision（使用隔离 DB；不会碰生产 inbox）
printf '%s\n' '{"delivery_id":"reducer-smoke-1","event":"pull_request","action":"opened","repo_full_name":"eric/dailycheck","pr_number":1,"pr_head_sha":"deadbeefdeadbeef"}' \
  | DAILYCHECK_EVENT_REDUCER_DB=/tmp/reducer-smoke.sqlite3 \
    /usr/local/bin/dc-notify-prod-agent
# 首次期望 rc=75 / decision=defer（debounce）
# 同一行按 watcher 退避重试时期望 trigger；完成后重复投递期望 suppress

# 4. 启用 breaker，再重启 watcher，随后启用 drain timer
systemctl enable --now dc-prod-agent-provider-breaker.timer
systemctl restart dc-inbox-watcher.service
systemctl enable --now dc-prod-agent-drain.timer

# 5. 观测
/root/DailyCheck/.venv/bin/python scripts/prod_agent/event_reducer.py status
/root/DailyCheck/.venv/bin/python scripts/prod_agent/event_reducer.py inspect --pr 54
journalctl -u dc-prod-agent-drain -n 50 --no-pager
journalctl -u dc-prod-agent-provider-breaker -n 50 --no-pager
```

**cooldown / recovery 检查**：`status` 的 `provider.state=open` 且
`cooldown_until_ms` 在未来表示 429 熔断。外部 breaker 每 30 秒只读 Hermes
`errors.log` 的新增完整行；5 分钟内 3 条 provider 429/quota 错误打开 15 分钟
cooldown，若日志有 `Retry-After` / `X-RateLimit-Reset` 则延长但封顶 1 小时。
它的 cursor 与 pending trip 原子落盘，重启不重放旧日志。到期后 drain 会把状态转
half-open；每次 drain 事务最多 claim 一个 due work，但并发 drain 可各自 claim
不同 PR 的一条，**不是全局单探针**。成功即 closed。`inspect --pr N` 中
`claimed + lease_expires_ms` 是当前 active；若 `last_error=lease_expired`，说明
中断已被下一次 drain 回收并重新排队。不要手工改 DB 行来“修”卡住状态，先等 TTL
或运行 `--drain`。

**rollback**：停 `dc-prod-agent-drain.timer` 与
`dc-prod-agent-provider-breaker.timer`，恢复上一版
`/usr/local/bin/dc-notify-prod-agent`，重启 `dc-inbox-watcher`。SQLite 文件与
breaker cursor 保留为审计/后续恢复资料；watcher 的 `inbox_watcher_state.json`
与 delivery-ID 安全逻辑不动，因此回滚不会扩大重复触发窗口。不要把 reducer DB
变回事件源，也不要复制 `inbox.jsonl`——两者都会重新引入双源竞态。

### 3.9 部署兜底轮询（10 分钟，Issue #47 / #51）

webhook 快通道（§3.3-3.8）的保底通道：Prod 侧 Hermes cron **每 30 分钟**直接查 Gitea `GET /api/v1/repos/eric/dailycheck/pulls?state=open`——快通道整体故障（receiver 挂 / watcher 卡 / gateway 重启窗口 / GitHub 投递失败 / §3.8 的 retry-gating）时仍能在 ≤30 分钟发现新 PR。

脚本 `deploy/scripts/dailycheck_pr_poll.py`（与 Prod 部署版逐字一致），静默语义（skill `no-agent-cron-script-pattern`）：**无 open PR → 零 stdout**（agent 回 `[SILENT]`，无投递）；有 open PR → 输出清单（agent 走 dailycheck-prod-agent skill 判定）；查询失败 → ⚠️ 告警输出（保底通道自身故障必须可见，不得静默）。

```bash
# 1. 脚本部署（Hermes cron 只允许 HERMES_HOME/scripts/ 内的脚本）
cp deploy/scripts/dailycheck_pr_poll.py /root/.hermes/scripts/dailycheck_pr_poll.py
chmod 755 /root/.hermes/scripts/dailycheck_pr_poll.py

# 2. 手动验证两种输出路径
/root/hermes-agent/.venv/bin/python /root/.hermes/scripts/dailycheck_pr_poll.py
#    无 open PR → 零输出（静默）；DCPOLL_REPO=cli/cli ... 借活跃仓库验证「有 PR」输出格式

# 3. 注册 cron job（30 分钟）
cd /root/hermes-agent
/root/hermes-agent/.venv/bin/hermes cron create "*/30 * * * *" \
  "这是 DailyCheck PR 兜底轮询（webhook 快通道失效时的保底发现通道）。脚本输出 = 当前 open PR 清单；无 open PR 时脚本静默，你也仅回 [SILENT]，不发任何消息。有 open PR → 按 dailycheck-prod-agent skill 走 merge 判定流程（CI 两必需 check 全绿才进队列；红线文件命中转 owner）。静默优先：无待处理事项不输出任何内容。" \
  --name "DailyCheck PR 兜底轮询" --deliver local --skill dailycheck-prod-agent --script dailycheck_pr_poll.py

# 4. 立即触发一次并验证静默
/root/hermes-agent/.venv/bin/hermes cron run <job-id>   # 下一个 scheduler tick（≤60s）执行
ls -t /root/.hermes/cron/output/<job-id>/               # 期望产出 <ts>.md，结尾 Response = [SILENT]
```

**注意**：

- Hermes 调度器**忽略 `no_agent` 字段**（见 skill `no-agent-cron-script-pattern` 的 CRITICAL REALITY NOTE）——每 tick 仍起一个 agent 会话（脚本输出注入 prompt 头部）。静默 tick 的代价 = 一次 LLM call，可接受（既有 fail2ban / SSH 监测 job 同模式）。
- 脚本必须在 `HERMES_HOME/scripts/` 内（scheduler 强制校验，防任意路径执行）。
- `DCPOLL_REPO` 环境变量仅用于测试输出格式；生产 cron 不设置（默认 `ericmr1981/DailyCheck`）。
- 该 job 是 webhook 快通道的**冗余保底**，不是替代——两条通道独立，都指向同一 `dailycheck-prod-agent` skill 判定流程（merge 判定幂等）。
- **轻提醒（2026-10-16 增；2026-10-26 更新）**：兜底 cron 与 `wdg-ci` / `dailycheck-deploy` 的 deliver 均为 `feishu:<chat_id>`（有实质动作时直推 ≤3 行轻提醒；静默 tick 仍被 [SILENT] 抑制，依赖 §3.8 步骤 2c 两补丁）；配 Hermes `config.yaml` `cron.wrap_response: false` 去掉 cron 包装头。⚠️ 本文档第 3 步命令里的 `--deliver local` 是最初创建参数，prod 现状已改为 feishu（以本条为准）。

### 3.10 CI merge-ready 快通道（2026-10-16 增；2026-10-22 Gitea `status`；2026-10-26 Issue #56 改为确定性 gate）

**目标**：PR 生命周期不唤醒 LLM；CI running/failure/partial 静默记账；同一 PR/head 第一次达到确定可合并条件时才向 `wdg-ci` 产生一次 `MERGE_READY`。

**链路**：Gitea `status` → receiver（§3.6 同一 webhook，Events 勾 `status`）→ inbox.jsonl → watcher（§3.8，unit `--actions` 含 `completed`）→ `dc-notify-prod-agent.sh` → reducer。reducer 查询 Gitea PR 与 commit statuses，要求 PR open、head 未变、mergeable、所有 required contexts success；只有全绿转换进入 `wdg-ci`，`deploy_result` 继续走 `dailycheck-deploy`。`pull_request` 只更新 reducer state，不进入 provider queue。

**部署变更点**（相对 §3.3-3.9 的增量，代码已在本 repo）：

| # | 位置 | 变更 |
|---|------|------|
| 1 | Gitea webhook Events | 勾 `status`（§3.6 步骤 5） |
| 2 | `scripts/blue_green/webhook_receiver.py` | `status` / completed `check_suite` 按 sha 关联到 open PR 即落盘（包含 running/failure/partial）；无 PR 的 main CI 仍 ignored。确定性判断全部移到 reducer |
| 3 | `deploy/systemd/dc-inbox-watcher.service` | `--actions opened,synchronize,reopened,completed` |
| 4 | `scripts/prod_agent/merge_readiness.py` | stdlib-only Gitea helper；复用 `GITEA_TOKEN` / `GITEA_TOKEN_FILE` / `GITEA_URL`，请求有 timeout、不跟随 3xx、不记录 token。配置 `DAILYCHECK_REQUIRED_CONTEXTS`（逗号分隔，大小写敏感；默认 `test,agent-framework-reports`） |
| 5 | `deploy/scripts/dc-notify-prod-agent.sh` + `scripts/prod_agent/hermes_webhook_sender.py` | provider 只允许 `MERGE_READY → wdg-ci` 与 `deploy_result → dailycheck-deploy`；`pull_request` / `wdg-pr` 在 reducer、adapter、sender 三层拒绝。用 route secret 做 HMAC，HTTP 202 入队后 complete |
| 6 | Hermes 侧 | 需要 `wdg-ci` 订阅（§3.8 步骤 2）；既然中间态不再进入 Hermes，`[SILENT]` gateway 补丁不再是 merge gate 的正确性依赖 |

**验证**（真 PR 的 CI 全绿后）：

```bash
# 1. Gitea 侧：webhook 最近投递出现 status 类型 delivery，202
# 2. receiver 侧：
tail -1 /var/lib/dailycheck-prod-agent/inbox.jsonl | python3 -m json.tool
#    期望：含 pr_number/head/check_conclusion；event 字段仍为 "check_suite"
#          （向下游兼容），source_event="status"
# 3. watcher 侧：
journalctl -u dc-inbox-watcher -n 10 --no-pager | grep triggered
# 4. reducer 侧：
/root/DailyCheck/.venv/bin/python /root/DailyCheck/scripts/prod_agent/event_reducer.py \
  --db /var/lib/dailycheck-prod-agent/event_reducer.sqlite3 status
#    期望：pending/claimed 与 provider cooldown 可见；merge_ready 只列当前就绪 PR
# 5. 单元证据（部署前/PR 验证）：
python3 -m pytest tests/test_merge_readiness.py tests/test_event_reducer.py \
  tests/test_provider_breaker.py tests/test_dashboard_reducer.py -q
```

**注意**：

- 一个 PR 的 CI 过程会产生多次 status/check 事件。reducer 每次都只更新 `pr_state` 且不调用 Hermes；`merge_ready_at_ms` 一旦写入，新的重复 delivery 直接 suppress。malformed Gitea response、token 缺失、网络超时均 fail-closed。
- `check_suite` / `status` 的 failure/running/partial 已进入 reducer state，但不会产生 `MERGE_READY`；旧 head 的迟到事件为 `stale_head`。`pull_request / synchronize / reopened` 会记录新 head 并让旧 head 的 pending/claimed lease 失效。
- 已存在的 `wdg-pr` route secret 不需要修改或删除；新代码不再读取它。是否清理该 Hermes 订阅属于独立运维动作，本变更不触碰现有 route secret。

### 3.11 审批跨会话修复（2026-10-16 深夜，本地补丁）

> **背景**：Prod Agent 由 webhook 会话驱动。危险命令（如 `git reset --hard`）触发 Hermes 审批门后，审批请求虽投递到飞书，但 `/approve` 按「发起会话」解析 → **webhook 会话的待审批永远无人可批** → 每次等满 5 分钟超时 BLOCKED（skill Pitfall 17 的根因，表现为「永久 block」）。
>
> **修复**：为 `/approve` / `/deny` 增加**跨会话 fallback** —— 自身会话无待审批时，自动解析**其它会话中最老的待审批**（自身会话优先，正常会话原语义不变）→ Eric 在飞书直接回复 `/approve` 即可放行 webhook 会话的危险命令。

**改动文件**（均本地补丁，Hermes 重装/升级后需重打；旧版备份后缀 `*.bak-20261016-crosssession`）：

| 文件 | 改动 |
|------|------|
| `tools/approval.py` | `_ApprovalEntry` 增 `created` 时间戳（最老优先排序用）；新增 `oldest_blocking_session()` |
| `gateway/run.py` | `_handle_approve_command` / `_handle_deny_command` 增跨会话 fallback + 提示文案（含 `cross-session:` 日志行） |

**重打方式**（升级后）：

```
python3 /root/hermes-agent/local-patches/2026-10-16-crosssession-approval/apply.py
# 幂等保护：已打过会直接报错退出，不重复修改
```

**验证判据**：

```
grep -E "cross-session" /root/.hermes/logs/agent.log   # 发生过跨会话解析时出现（User approved/denied ... (cross-session: ...)）
```

**注意**：

- 跨会话 fallback 仅在本会话无待审批时生效——正常飞书会话（含飞书审批按钮）行为完全不变。
- 无人值守窗口（5 分钟无人回复）仍会 BLOCKED；此时按 skill Pitfall 17 的 `git stash`（不带 `-u`）+ `git pull --ff-only` 绕行。

## 4. 验收 checklist

| # | 项 | 命令 / 验证 |
|---|----|-------------|
| 1 | DNS 解析 | `dig +short hook.kjtang.cn` → `112.124.18.246` |
| 2 | 443 放行 + 持久化 | `iptables -S ufw-user-input \| grep -E 'dport 443'` 有输出；`grep 'dport 443' /etc/iptables/rules.v4` 有行 |
| 3 | 证书为 hook 独立证书 | `openssl x509 -in /etc/nginx/ssl/hook.kjtang.cn.pem -noout -subject -dates`（CN=hook.kjtang.cn） |
| 4 | systemd 单元 active | `systemctl status dc-webhook-receiver.service` |
| 5 | /health 200（本机直连 receiver） | `curl -sS http://127.0.0.1:8443/health` |
| 6 | **外网 443 可达（必须外部 vantage）** | **非 VPS 网络** curl `https://hook.kjtang.cn/health` → 200；或 check-host.net 多节点复验。⚠️ VPS 本机 curl 走 lo 直通会假阳性（§2） |
| 7 | nginx 运行态含 hook server | `nginx -T 2>/dev/null \| grep -c 'server_name hook.kjtang.cn'` → 2 |
| 8 | inbox 落盘 | `cat /var/lib/dailycheck-prod-agent/inbox.jsonl`（dev 测 PR 后应至少 1 行） |
| 9 | 限速规则生效 | `head /var/log/nginx/hook.kjtang.cn.error.log`（看有无 503 限速日志） |
| 10 | 单元测试通过 | `pytest tests/test_webhook_receiver.py tests/test_inbox_watcher.py -q`（CI 自动跑） |
| 11 | GitGuardian pass | PR CI GitGuardian check pass |
| 12 | watcher unit active | `systemctl status dc-inbox-watcher` |
| 13 | inbox 新行 ≤5s 触发 | §3.8 步骤 4：手工投递 → journal 出现 `triggered: delivery=test-1`（≤5s） |
| 14 | rotate timer 已启用 | `systemctl list-timers dc-inbox-rotate.timer`（下次 00:30） |
| 15 | 端到端：PR → webhook → inbox → watcher → Prod Agent | 提一个测试 PR → GitHub Delivery 202 → watcher journal `triggered:` → Prod Agent 侧收到事件 |
| 16 | 兜底轮询 job active | `hermes cron list` → 「DailyCheck PR 兜底轮询」（`*/30`，deliver local，skill dailycheck-prod-agent） |
| 17 | 兜底轮询静默语义 | `hermes cron run <id>` 一次 → `cron/output/<id>/<ts>.md` 结尾 `Response = [SILENT]`（无 open PR 时） |
| 18 | CI 状态落盘 | PR 中间态/失败/成功事件均出现 `"event":"check_suite"` inbox 行；main CI 仍不落盘（§3.10） |
| 19 | MERGE_READY transition | 全绿**首次**产生一条 `trigger_name=MERGE_READY`；重复/旧 head 零新增 provider 投递（§3.10） |
| 20 | [SILENT] 抑制生效 | 中间态判定不产生飞书消息；`grep 'Suppressed delivery' /path/agent.log` 或日志无声即成功 |
| 21 | 审批跨会话放行（§3.11） | webhook 会话触发危险命令 → 飞书回复 `/approve` → `agent.log` 出现 `cross-session` 行 + 该命令解冻执行 |

**2026-10-16 首次部署实测结果**（全链路已通，供下次部署对照）：

- DNS ✅（本机/8.8.8.8/1.1.1.1/223.5.5.5 均解析至 112.124.18.246）
- 外部 vantage（check-host.net 4/4 节点）✅ hook 与主站 443 均可达（主站 443 系本次一并修复的历史故障）
- 测试 PR #49 → GitHub delivery `pull_request/opened` **202**（1.66s）→ inbox.jsonl 完整落盘 → receiver access log 可查 → PR 已关闭
- redeliver ping delivery：修复前 502 → 修复后 **200**（0.68s）

**2026-10-16 §3.8 / §3.9 部署实测（第二批次：唤醒链，Issue #47）**：

- watcher 修复 `ProtectHome=true` → `read-only` 后 `active (running)`（此前 203/EXEC 空转 1800+ 次）；rotate 手动跑 `archived=0 kept=2` ✅
- Hermes webhook platform 启用（config.yaml 顶层 `platforms` + .env `WEBHOOK_*` 双写）→ `127.0.0.1:8644` LISTEN ✅
- 订阅 `wdg-pr` 建成；`hermes webhook test` → **202**；`agent.log` 确认会话生成（15s 内响应、api_calls=2）✅
- 端到端（真实补发 + 测试）：inbox 3 事件（PR #49/#50 真实通知 + 测试事件）→ journal 全部 `triggered:` → Prod Agent 3 会话全部正确判定（#49 已关闭→静默；#50 已 merge→确认；测试事件→识破不动作）✅
- 兜底轮询 job `02ee9e01cb13` 首跑静默 `[SILENT]` ✅；**实测暴露 watcher retry-gating 缺陷 → Issue #51**（PR #49/#50 通知延迟 1h 才补发，根因见 §3.8 注意第 3 条）

**2026-10-16 §3.10 部署实测（第三批次：check_suite 快通道 + [SILENT] 泄漏修复）**：

- 合成 check_suite（completed/success/PR #997）e2e：receiver **202**（19:40:56）→ watcher `triggered:` **≤1s** → Prod Agent 会话 10.3s → gateway `Suppressed delivery ... ([SILENT])` → 飞书**零消息** ✅
- 修复 [SILENT] 字面泄漏（16:30–19:01 兜底轮询 5 次静默 tick 泄漏到飞书）：根因 = 模型间歇输出**截断标记** `[SILENT`（丢右括号）→ 严格子串匹配漏过；按步骤 2c 打两处截断容错补丁后复测通过
- GitHub hook Events 已含 `check_suite`；真 PR 的 CI 全绿触发即首验（见 §4 第 18/19 项）

**2026-10-16 深夜 §3.11 部署实测（第四批次：审批跨会话修复）**：

- 复现（补丁前）：webhook 会话触发 `git reset --hard` 进审批 → 另一会话发 `/approve` → `No pending command to approve.`（22:56 复现死锁，待审批等满 5 分钟超时）
- 补丁后（approve）：审批请求 23:00:42 发出 → 另一会话 `/approve` 23:00:43 跨会话命中（`cross-session:` 日志行）→ 命令 23:00:48 解冻执行成功（实测约 1.6s 解冻）
- 补丁后（deny）：`/deny` 跨会话命中 → agent 收到拦截、命令未执行（23:01）
- 负例（回归）：无待审批时 `/approve` → `No pending command to approve.`（文案不变）
- 附带：skill `dailycheck-prod-agent` Pitfall 17 已同步更新（不再描述为「永久 block」）

## 5. 风险与回滚

| 风险 | 缓解 | 回滚 |
|------|------|------|
| **443 防火墙缺放行 → 外网不可达**（症状：外网 502/超时；⚠️ VPS 本机 curl 假阳性 200） | §2 前置核验（`iptables -S ufw-user-input` + 持久化文件） + §4 第 6 项**外部 vantage** 复验 | 无需回滚（补放行即修复；本次已修复并持久化） |
| **hook 证书轮换期 webhook 短暂不可达**（替换文件 + reload 的瞬间） | 先下载签发好的新证书再替换，独立证书不动主站；90 天周期提前重签 | 还原旧 `.pem/.key`（替换前先 `.bak` 备份）+ `systemctl reload nginx` |
| nginx 语法错 reload 失败 | 步骤 3.5 强制 `nginx -t` 验证 | **独立文件**：`rm /etc/nginx/sites-enabled/hook.kjtang.cn` + `systemctl reload nginx`（删文件即还原，不动主站） |
| receiver 进程挂掉 | systemd `Restart=always`（5s 自动拉起） | `systemctl restart dc-webhook-receiver.service` |
| **receiver 起不来 `status=203/EXEC`** | unit `ProtectHome=read-only`（已修正，服务代码在 /root 下） | 核对 unit 该行后 `systemctl daemon-reload && systemctl restart` |
| inbox 写满 1000 | 反压返回 503，GitHub 暂停重试 | 手动 rotate inbox → 移到 `processed.jsonl` |
| HMAC secret 泄漏 | secret 写 `/etc/dailycheck/webhook-secret`（600 root），不出现 prompt/git/log | GitHub repo webhook 设置里点「Redeliver」+ 重新生成 secret + 更新 systemd EnvironmentFile + 重启 receiver |
| 旧 webhook 重投同一 delivery | 幂等按 delivery_id（receiver 200 duplicate） | 不需处理，receiver 自动忽略 |
| **watcher 触发失败后无新事件 → 退避重试不发动**（Issue #51，实测延迟 1h） | §3.9 兜底轮询 30 分钟保底；journal WARNING 可见 | 修复见 Issue #51（主循环加 pending-retry 条件，pending-retry PR） |
| **Hermes gateway 重启窗口**（订阅热加载前）事件落 inbox 但未触发 | watcher 状态文件保留未处理条目（重启后补触发）+ §3.9 兜底轮询 | 无需处理；确认 `ss -tlnp \| grep 8644` 恢复 LISTEN |
| **[SILENT] 补丁被 Hermes 升级/重装冲掉**（症状：中间态判定开始往飞书发字面 `[SILENT]` / `[SILENT` 消息） | §3.8 步骤 2c 重打两处补丁；`grep -n '_silent_check' /root/hermes-agent/gateway/platforms/webhook.py /root/hermes-agent/cron/scheduler.py` 核对 | 无需回滚；补丁缺失只影响静默语义，事件不丢（兜底轮询照常） |
| **审批跨会话补丁被 Hermes 升级/重装冲掉**（症状：Prod Agent 危险命令重新死锁——每次超时 `BLOCKED: Command timed out`） | §3.11 重打：`python3 /root/hermes-agent/local-patches/2026-10-16-crosssession-approval/apply.py`；判据 `grep -c oldest_blocking_session /root/hermes-agent/tools/approval.py`（应 ≥1） | 无需回滚；补丁缺失只影响 webhook 会话审批（飞书原生会话审批按钮不受影响） |

## 6. 监控

- **证书轮换（90 天周期，两份独立证书）**：hook 独立证书 2026-10-16 签发 → **2027-01-13 到期**；主站证书 → **2027-01-03 到期**。到期前重签（hook 走 §3.2）。远程查看到期时间：`echo | openssl s_client -connect hook.kjtang.cn:443 -servername hook.kjtang.cn 2>/dev/null | openssl x509 -noout -dates`
- **inbox rotate（已实装，#47）**：`dc-inbox-rotate.timer` 每日 00:30 归档 >7 天条目到 `inbox.archive.jsonl`
- **watcher 触发失败**：`journalctl -u dc-inbox-watcher | grep WARNING` 非空即链路告警（脚本内退避重试中；注意 Issue #51 gating 限制——inbox 无新行时不自行重试，由 §3.9 兜底轮询 ≤30 分钟保底）
- **兜底轮询健康（§3.9）**：`hermes cron list` 看「DailyCheck PR 兜底轮询」last run `ok`；`cron/output/<id>/*.md` 连续出现 ⚠️ 输出 = 轮询通道故障（gh/token/网络）需排查
- **Hermes webhook adapter 健康**：`ss -tlnp | grep 8644` 有 LISTEN；`agent.log` 中 `[webhook] POST` 出现频率 = 快通道实际投递频率
- **inbox size 观察**：`curl -sS http://127.0.0.1:8443/health` 的 `inbox_size`（持续逼近 1000 = rotate 失效需排查）
- 后续可加：Prometheus 暴露 `/metrics`：inbox size / 处理延迟 / 5xx 计数
- 后续可加：systemd `WatchdogSec`：receiver 30s 内必须发一次心跳，超时重启
- 后续可加：logrotate：`/var/log/dailycheck/webhook-receiver-*.log` 按天 rotate，保留 30 天
