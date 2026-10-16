# DailyCheck Webhook 部署操作手册

> **目的**：Dev Agent 提完 PR 后，GitHub repo 自动通过 webhook 通知 VPS 上的 receiver，receiver 写入 `inbox.jsonl`，`dc-inbox-watcher` 订阅 inbox **≤5s** 触发 Prod Agent 入口跑 merge 决策。
>
> **目标读者**：owner / Prod Agent 手动部署。脚本走不走通不在 Dev 自动化范围（基础设施变更需 owner 拍板）。
>
> **设计文档**：`docs/agent-framework/case-studies/2026-10-15-self-contained-dev-loop.md` 第 6 章"蓝绿部署" + 第 10 章"复盘"。
>
> **预计耗时**：40-60 分钟（DNS / 证书重签 / systemd / nginx reload / GitHub 配 webhook / §3.8 watcher+rotate）。
>
> **更新**：2026-10-16 增补 §3.8（inbox watcher + rotate，Issue #47）—— 链路「inbox.jsonl → Prod Agent」从「Prod 侧自行实现」变为本手册可部署的实装。

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
| 2 | 阿里云 SSL 证书可加 SAN | 阿里云 SSL 控制台 | 当前证书 `kjtang.cn` 阿里云 DV，2027-01-03 到期 |
| 3 | /var/lib/dailycheck-prod-agent 目录 | `mkdir -p /var/lib/dailycheck-prod-agent && chown root:root` | 写入权限 + inbox 落盘 |
| 4 | .venv 路径正确 | `ls /root/DailyCheck/.venv/bin/gunicorn` | 存在 |
| 5 | 8443 端口空闲 | `ss -tlnp \| grep 8443` | 空（已被实测空闲） |

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

### 3.2 重新签证书加 SAN `hook.kjtang.cn`（15-30 分钟，含签发等待）

1. 登录阿里云 SSL 控制台
2. 找到当前 `kjtang.cn` 证书 → 「续费」或「重新申请」
3. 域名列表：保留 `kjtang.cn` / `www.kjtang.cn`，**追加 `hook.kjtang.cn`**
4. 提交审核（DV 证书通常几分钟内签发）
5. 下载证书（nginx 格式 = pem + key）
6. **生产备份现证书**：
   ```bash
   cp /etc/nginx/ssl/kjtang.cn.pem /etc/nginx/ssl/kjtang.cn.pem.bak.$(date +%Y%m%d-%H%M%S)
   cp /etc/nginx/ssl/kjtang.cn.key /etc/nginx/ssl/kjtang.cn.key.bak.$(date +%Y%m%d-%H%M%S)
   ```
7. 替换证书：
   ```bash
   # 新证书覆盖
   cp <new-cert>.pem /etc/nginx/ssl/kjtang.cn.pem
   cp <new-key>.key  /etc/nginx/ssl/kjtang.cn.key
   chown root:root /etc/nginx/ssl/kjtang.cn.{pem,key}
   chmod 644 /etc/nginx/ssl/kjtang.cn.pem
   chmod 600 /etc/nginx/ssl/kjtang.cn.key
   ```
8. 验证证书含 hook 域名：
   ```bash
   openssl x509 -in /etc/nginx/ssl/kjtang.cn.pem -noout -text | grep -A1 'Subject Alternative Name'
   # 应含：DNS:kjtang.cn, DNS:www.kjtang.cn, DNS:hook.kjtang.cn
   ```

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

# 4. 探活
curl -sS http://127.0.0.1:8443/health
# 期望：{"inbox_path":"/var/lib/dailycheck-prod-agent/inbox.jsonl","inbox_size":0,"max_inbox_lines":1000,"status":"ok"}

# 5. 跟日志
tail -F /var/log/dailycheck/webhook-receiver-access.log
tail -F /var/log/dailycheck/webhook-receiver-error.log
```

### 3.5 部署 nginx 片段（5 分钟）

```bash
# 1. 在 /etc/nginx/sites-enabled/dailycheck 末尾追加 hook.kjtang.cn 段
#    （debian 系 nginx 主配置是 include sites-enabled/*，追加到现有文件即可）
cat deploy/nginx/hook.kjtang.cn.snippet.conf >> /etc/nginx/sites-enabled/dailycheck

# 2. 验证语法
nginx -t
# 期望：syntax is ok / test is successful

# 3. reload（流式，0 中断）
systemctl reload nginx

# 4. 探活（外部域名）
curl -I https://hook.kjtang.cn/health
# 期望：HTTP/2 200
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
   - **只勾 "Pull requests"**（不要 issue、不要 push 等）
8. Active: ✓
9. 「Add webhook」

### 3.7 端到端验证（5 分钟）

1. 打开 https://github.com/ericmr1981/DailyCheck/settings/hooks
2. 找到刚加的 webhook → 「Recent Deliveries」→ 应该是空
3. 让 Dev Agent 提一个测试 PR（或手动在 GitHub UI 上开一个空 PR）
4. 回到 webhook 详情页 → 应看到一条 delivery 记录，HTTP response 202
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
#    默认实现 = Hermes webhook 订阅（见步骤 2）；若 Prod Agent 入口不同，
#    按脚本头部注释替换 exec 那一行即可，watcher 不需要改动。

# 2. （Hermes 侧，一次性）启用 webhook platform + 建订阅
#    在 /root/.hermes/config.yaml 加：
#      platforms:
#        webhook:
#          enabled: true
#          extra:
#            host: "127.0.0.1"        # 仅本地（投递者是本机 watcher），勿暴露公网
#            port: 8644
#            secret: "<随机强密码>"
#    重启 gateway 生效，然后建订阅：
cd /root/hermes-agent
./hermes webhook subscribe dailycheck-pr \
  --prompt 'DailyCheck PR 通知: PR #{pr_number} {action} by {pr_author} ({pr_head_sha}) —— 走 dailycheck-prod-agent skill 判定 merge' \
  --skills dailycheck-prod-agent
./hermes webhook list          # 确认 dailycheck-pr 在列

# 3. watcher + rotate units
cp deploy/systemd/dc-inbox-watcher.service \
   deploy/systemd/dc-inbox-rotate.service \
   deploy/systemd/dc-inbox-rotate.timer \
   /etc/systemd/system/
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
- watcher 的触发失败会 WARNING + 指数退避重试（journal 可见）；**30 分钟兜底轮询不变**——webhook 链路整体故障时仍按原节奏发现新 PR。
- watcher 的 systemd 沙箱默认只放行 `/var/lib/dailycheck-prod-agent` + `/root/.hermes` 写权限；若替换触发命令且需要别的写路径，在 unit 的 `ReadWritePaths` 追加。

## 4. 验收 checklist

| # | 项 | 命令 / 验证 |
|---|----|-------------|
| 1 | DNS 解析 | `dig +short hook.kjtang.cn` |
| 2 | 证书含 hook 域名 | `openssl x509 -in /etc/nginx/ssl/kjtang.cn.pem -noout -text \| grep -A1 'Subject Alternative Name'` |
| 3 | systemd 单元 active | `systemctl status dc-webhook-receiver.service` |
| 4 | /health 200 | `curl -sS http://127.0.0.1:8443/health` |
| 5 | nginx conf 验语法 | `nginx -T 2>&1 \| grep -A1 'server_name hook.kjtang.cn'` |
| 6 | 外部 443 探活 | `curl -I https://hook.kjtang.cn/health` |
| 7 | inbox 落盘 | `cat /var/lib/dailycheck-prod-agent/inbox.jsonl`（dev 测 PR 后应至少 1 行） |
| 8 | 限速规则生效 | `head /var/log/nginx/hook.kjtang.cn.error.log`（看有无 503 限速日志） |
| 9 | 单元测试通过 | `pytest tests/test_webhook_receiver.py tests/test_inbox_watcher.py -q`（CI 自动跑） |
| 10 | GitGuardian pass | PR CI GitGuardian check pass |
| 11 | watcher unit active | `systemctl status dc-inbox-watcher` |
| 12 | inbox 新行 ≤5s 触发 | §3.8 步骤 4：手工投递 → journal 出现 `triggered: delivery=test-1`（≤5s） |
| 13 | rotate timer 已启用 | `systemctl list-timers dc-inbox-rotate.timer`（下次 00:30） |
| 14 | 端到端：PR → webhook → inbox → watcher → Prod Agent | 提一个测试 PR → GitHub Delivery 202 → watcher journal `triggered:` → Prod Agent 侧收到事件

## 5. 风险与回滚

| 风险 | 缓解 | 回滚 |
|------|------|------|
| 证书重签期 webhook 不可达 | **先签好新证书再切 nginx**（步骤 3.2 顺序） | 用 .bak 还原证书 + `systemctl reload nginx` |
| nginx 语法错 reload 失败 | 步骤 3.5 强制 `nginx -t` 验证 | `cp /etc/nginx/sites-enabled/dailycheck.bak /etc/nginx/sites-enabled/dailycheck` + reload |
| receiver 进程挂掉 | systemd `Restart=always`（5s 自动拉起） | `systemctl restart dc-webhook-receiver.service` |
| inbox 写满 1000 | 反压返回 503，GitHub 暂停重试 | 手动 rotate inbox → 移到 `processed.jsonl` |
| HMAC secret 泄漏 | secret 写 `/etc/dailycheck/webhook-secret`（600 root），不出现 prompt/git/log | GitHub repo webhook 设置里点「Redeliver」+ 重新生成 secret + 更新 systemd EnvironmentFile + 重启 receiver |
| 旧 webhook 重投同一 delivery | 幂等按 delivery_id（receiver 200 duplicate） | 不需处理，receiver 自动忽略 |

## 6. 监控

- **inbox rotate（已实装，#47）**：`dc-inbox-rotate.timer` 每日 00:30 归档 >7 天条目到 `inbox.archive.jsonl`
- **watcher 触发失败**：`journalctl -u dc-inbox-watcher | grep WARNING` 非空即链路告警（脚本内退避重试中；30 分钟兜底轮询不变）
- **inbox size 观察**：`curl -sS http://127.0.0.1:8443/health` 的 `inbox_size`（持续逼近 1000 = rotate 失效需排查）
- 后续可加：Prometheus 暴露 `/metrics`：inbox size / 处理延迟 / 5xx 计数
- 后续可加：systemd `WatchdogSec`：receiver 30s 内必须发一次心跳，超时重启
- 后续可加：logrotate：`/var/log/dailycheck/webhook-receiver-*.log` 按天 rotate，保留 30 天
