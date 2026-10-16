# DailyCheck Webhook 部署操作手册

> **目的**：Dev Agent 提完 PR 后，GitHub repo 自动通过 webhook 通知 VPS 上的 receiver，receiver 写入 `inbox.jsonl`，`dc-inbox-watcher` 订阅 inbox **≤5s** 触发 Prod Agent 入口跑 merge 决策。
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
> **更新**：2026-10-16 增补 **§3.10（CI 完成快通道）** —— webhook 补勾 "Check suites" 事件；`check_suite`（全绿）经同一 receiver → inbox → watcher 链路秒级唤醒 Prod Agent；新增 `dailycheck-ci` 订阅 + `webhook.py` / `scheduler.py` 两处 [SILENT] 抑制补丁（含「截断标记」容错，§3.8 步骤 2c）。

## 1. 架构

```
Dev Agent (本地)                                VPS (112.124.18.246)
   ↓ gh pr create                          ┌────────────────────────┐
GitHub Repo (ericmr1981/DailyCheck)        │  nginx :443            │
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

### 3.6 配 GitHub repo Webhook（3 分钟）

1. 打开 https://github.com/ericmr1981/DailyCheck/settings/hooks
2. 「Add webhook」
3. Payload URL: `https://hook.kjtang.cn/webhook`
4. Content type: `application/json`
5. Secret: 粘贴步骤 3.3 里的 `$WEBHOOK_SECRET` 值
6. SSL verification: Enable（默认）
7. 「Which events would you like to trigger this webhook?」
   - 选择 "Let me select individual events"
   - **勾 "Pull requests" + "Check suites"**（2026-10-16 起两个都勾：前者 = PR 生命周期触点，后者 = CI 完成快通道 §3.10；不要 issue、不要 push 等）
8. Active: ✓
9. 「Add webhook」

### 3.7 端到端验证（5 分钟）

1. 打开 https://github.com/ericmr1981/DailyCheck/settings/hooks
2. 找到刚加的 webhook → 「Recent Deliveries」→ 应至少有 1 条 **ping** delivery（GitHub 添加时自动发）；若 ping 失败（红叉），先修通 ping（通常是 443 可达性，见 §2）再做下一步
3. 让 Dev Agent 提一个测试 PR（或手动在 GitHub UI 上开一个空 PR）
4. 回到 webhook 详情页 → 应看到一条 `pull_request` delivery 记录，HTTP response 202
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
#    然后建订阅（CLI 一律走 venv 绝对路径，理由同步骤 1；两个订阅都要建）：
cd /root/hermes-agent
/root/hermes-agent/.venv/bin/hermes webhook subscribe dailycheck-pr \
  --prompt 'DailyCheck PR 通知: PR #{pr_number} {action} by {pr_author} ({pr_head_sha}) —— 走 dailycheck-prod-agent skill 判定 merge。最终回复=恰好一行 ≤50 字轻提醒（会原样推送飞书）。' \
  --skills dailycheck-prod-agent
/root/hermes-agent/.venv/bin/hermes webhook subscribe dailycheck-ci \
  --prompt 'DailyCheck CI 完成通知: PR #{pr_number}，head {pr_head_sha}。走 dailycheck-prod-agent skill 判定 merge；若两必需 check 未全绿或无需动作 → 仅回 [SILENT]（不发消息）；有实质动作（merge/上报）→ 最终回复恰好一行 ≤50 字轻提醒。' \
  --skills dailycheck-prod-agent
/root/hermes-agent/.venv/bin/hermes webhook list            # 确认 dailycheck-pr + dailycheck-ci 都在列
#    （prompt 文案可按需调整；⚠️ [SILENT] 中间态静默依赖下一步 2c 的补丁）

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
echo '{"delivery_id":"test-1","event":"pull_request","action":"opened","repo_full_name":"ericmr1981/DailyCheck","pr_number":1,"pr_title":"test","pr_head_sha":"deadbeef","received_at_ms":0}' \
  >> /var/lib/dailycheck-prod-agent/inbox.jsonl
sleep 3
journalctl -u dc-inbox-watcher -n 5 --no-pager     # 期望出现 triggered: delivery=test-1
# Prod Agent 侧（Hermes）应同时收到 dailycheck-pr 订阅的会话

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

### 3.9 部署兜底轮询（10 分钟，Issue #47 / #51）

webhook 快通道（§3.3-3.8）的保底通道：Prod 侧 Hermes cron **每 30 分钟**直接查 `gh pr list --state open`——快通道整体故障（receiver 挂 / watcher 卡 / gateway 重启窗口 / GitHub 投递失败 / §3.8 的 retry-gating）时仍能在 ≤30 分钟发现新 PR。

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
- **轻提醒（2026-10-16 增）**：prod 的该 job 与 `dailycheck-pr` / `dailycheck-ci` 两订阅的 deliver 均为 `feishu:<chat_id>`（有实质动作时直推 ≤3 行轻提醒；静默 tick 仍被 [SILENT] 抑制，依赖 §3.8 步骤 2c 两补丁）；配 Hermes `config.yaml` `cron.wrap_response: false` 去掉 cron 包装头。⚠️ 本文档第 3 步命令里的 `--deliver local` 是最初创建参数，prod 现状已改为 feishu（以本条为准）。

### 3.10 CI 完成快通道（2026-10-16 增，check_suite）

**目标**：CI 全绿 → **秒级**触发 Prod Agent 判定 merge（此前 merge 环节靠 §3.9 兜底轮询接棒，最坏 ≤30 分钟延迟）。

**链路**：GitHub `check_suite`（completed + success + 有 PR 关联）→ receiver（§3.6 同一 webhook，Events 补勾 "Check suites"）→ inbox.jsonl → watcher（§3.8，unit `--actions` 含 `completed`）→ `dc-notify-prod-agent.sh`（按 `event` 路由 `dailycheck-ci` 订阅）→ Prod Agent（全绿即 merge；中间态回 `[SILENT]` 静默不打扰）。

**部署变更点**（相对 §3.3-3.9 的增量，代码已在本 repo）：

| # | 位置 | 变更 |
|---|------|------|
| 1 | GitHub webhook Events | 补勾 "Check suites"（§3.6 步骤 7） |
| 2 | `scripts/blue_green/webhook_receiver.py` | 支持 check_suite：`completed` + `conclusion=success` + 有 PR 关联才落盘（其余 200 ignored） |
| 3 | `deploy/systemd/dc-inbox-watcher.service` | `--actions opened,synchronize,reopened,completed` |
| 4 | `deploy/scripts/dc-notify-prod-agent.sh` | 按 `event` 字段路由订阅（check_suite → dailycheck-ci；其余 → dailycheck-pr）；exec 行 venv 绝对路径（同 §3.8 步骤 1） |
| 5 | Hermes 侧 | 订阅 `dailycheck-ci`（§3.8 步骤 2）+ gateway `webhook.py` [SILENT] 补丁（§3.8 步骤 2c） |

**验证**（真 PR 的 CI 全绿后）：

```bash
# 1. GitHub 侧：webhook Recent Deliveries 出现 check_suite 类型 delivery，202
# 2. receiver 侧：
tail -1 /var/lib/dailycheck-prod-agent/inbox.jsonl | python3 -m json.tool
#    期望："event": "check_suite"、"check_conclusion": "success"、含 pr_number
# 3. watcher 侧：
journalctl -u dc-inbox-watcher -n 10 --no-pager | grep triggered
# 4. Prod Agent 侧：merge 时间戳 − CI 完成时间 < 1 分钟（对比旧链路 ≤30 分钟）
```

**注意**：

- 一个 PR 的 CI 过程通常产生**多个 check_suite**（每个 workflow 一个 + 第三方 App 如 GitGuardian），每个 success 的 suite 完成都会触发一次判定——前面 suite 完成时两必需 check 未齐 → Prod Agent 回 `[SILENT]`（**不产生飞书消息**）；最后一个凑齐全绿的 suite 才触发 merge + 一行轻提醒。多跑几次判定是幂等且静默的，属预期。
- `conclusion=failure` 的 suite **不转发**（无 merge 动作可做；失败由 Dev 侧修复，兜底轮询照常看板）。
- [SILENT] 抑制依赖两处本地补丁（§3.8 步骤 2c：gateway `webhook.py` + cron `scheduler.py`，均含「标记丢右括号」截断容错）；补丁缺失时会往飞书发字面 "[SILENT]" 消息——见 §5 风险表。

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
| 18 | check_suite 快通道落盘 | 真 PR CI 全绿后 inbox 出现 `"event":"check_suite"` 行 / GitHub delivery 202（§3.10） |
| 19 | CI 快通道秒级 merge | 全绿 suite 完成 → merge 时间差 < 1 分钟（§3.10 验证第 4 条） |
| 20 | [SILENT] 抑制生效 | 中间态判定不产生飞书消息；`grep 'Suppressed delivery' /path/agent.log` 或日志无声即成功 |

**2026-10-16 首次部署实测结果**（全链路已通，供下次部署对照）：

- DNS ✅（本机/8.8.8.8/1.1.1.1/223.5.5.5 均解析至 112.124.18.246）
- 外部 vantage（check-host.net 4/4 节点）✅ hook 与主站 443 均可达（主站 443 系本次一并修复的历史故障）
- 测试 PR #49 → GitHub delivery `pull_request/opened` **202**（1.66s）→ inbox.jsonl 完整落盘 → receiver access log 可查 → PR 已关闭
- redeliver ping delivery：修复前 502 → 修复后 **200**（0.68s）

**2026-10-16 §3.8 / §3.9 部署实测（第二批次：唤醒链，Issue #47）**：

- watcher 修复 `ProtectHome=true` → `read-only` 后 `active (running)`（此前 203/EXEC 空转 1800+ 次）；rotate 手动跑 `archived=0 kept=2` ✅
- Hermes webhook platform 启用（config.yaml 顶层 `platforms` + .env `WEBHOOK_*` 双写）→ `127.0.0.1:8644` LISTEN ✅
- 订阅 `dailycheck-pr` 建成；`hermes webhook test` → **202**；`agent.log` 确认会话生成（15s 内响应、api_calls=2）✅
- 端到端（真实补发 + 测试）：inbox 3 事件（PR #49/#50 真实通知 + 测试事件）→ journal 全部 `triggered:` → Prod Agent 3 会话全部正确判定（#49 已关闭→静默；#50 已 merge→确认；测试事件→识破不动作）✅
- 兜底轮询 job `02ee9e01cb13` 首跑静默 `[SILENT]` ✅；**实测暴露 watcher retry-gating 缺陷 → Issue #51**（PR #49/#50 通知延迟 1h 才补发，根因见 §3.8 注意第 3 条）

**2026-10-16 §3.10 部署实测（第三批次：check_suite 快通道 + [SILENT] 泄漏修复）**：

- 合成 check_suite（completed/success/PR #997）e2e：receiver **202**（19:40:56）→ watcher `triggered:` **≤1s** → Prod Agent 会话 10.3s → gateway `Suppressed delivery ... ([SILENT])` → 飞书**零消息** ✅
- 修复 [SILENT] 字面泄漏（16:30–19:01 兜底轮询 5 次静默 tick 泄漏到飞书）：根因 = 模型间歇输出**截断标记** `[SILENT`（丢右括号）→ 严格子串匹配漏过；按步骤 2c 打两处截断容错补丁后复测通过
- GitHub hook Events 已含 `check_suite`；真 PR 的 CI 全绿触发即首验（见 §4 第 18/19 项）

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
