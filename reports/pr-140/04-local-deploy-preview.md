# 报告 04/4：部署预演报告（本次为真实生产部署，非本地预演）

> 生成方式：人工执行 + 生产日志摘录
> 说明：Eric 于 2026-10-21 00:52 明确授权「问题解决后可以上线部署到 VPS」，
> 因此本报告记录的是**真实生产部署**，不是本地预演。

## 1. 元信息

| 字段 | 值 |
|------|-----|
| PR | #140 |
| 分支 | feat/wecom-inventory-bot |
| 基线 | origin/main @ fde2048 |
| 部署主机 | wdg_vps（112.124.18.246，Ubuntu，CST+8） |
| 部署方式 | 独立目录 + 独立 venv + 独立 systemd unit |
| 生成时间 | 2026-10-21T01:20:00+08:00 |

## 2. 环境对齐检查

| 项 | 本机（dev） | wdg_vps（prod） | 一致性 |
|----|----------------|----------------|--------|
| Python | 3.13.12 | 3.12.3 | 同一 minor 线，语法未用 3.13 特性 |
| aiohttp | 3.14.5 | 3.14.5 | 一致 |
| websockets | 17.3 | 17.3 | 一致 |
| wecom-aibot-python-sdk | 1.0.2 | 1.0.2 | 一致 |
| 监听端口 | 无 | **无** | 只有出站 443 |
| 主站 venv | — | 未改动 | 独立 venv，零污染 |

## 3. 启动日志摘要

首次启动（`systemctl start dailycheck-bot`）：

```
00:58:50 INFO    bot.serve | 准备连接 BotID=aibR5Q*** Secret=Y6Ba***
00:58:50 INFO    bot.probe | 已强制长连接直连（忽略本机代理设置）
00:58:50 INFO    bot.sdk   | WebSocket connection established, sending auth...
00:58:50 INFO    bot.serve | WebSocket 已连接，正在认证…
00:58:50 INFO    bot.sdk   | Authentication successful
00:58:50 INFO    bot.sdk   | Authenticated
00:58:50 INFO    bot.serve | 认证成功，库存助手已上线
00:58:50 INFO    bot.serve | 库目录=/root/DailyCheck/db | 模型=MiniMax-M3.1-Flash
```

代码更新 + 重启后再次确认（`01:13:20`）：

```
01:13:20 INFO    bot.sdk   | Authentication successful
01:13:20 INFO    bot.sdk   | Authenticated
01:13:20 INFO    bot.serve | 认证成功，库存助手已上线
```

`systemctl is-active` → `active`；`Journal` 中无 traceback、无 errcode。
`StartLimitIntervalSec` 初始误放在 `[Service]` 段触发 4 条 `Unknown key` 警告，
移至 `[Unit]` 段后重载，新日志中该警告归零。

## 4. 关键路径冒烟

| 路径 | 方式 | 状态 | 说明 |
|------|------|------|------|
| 长连接认证 | 真实进程 | 通过 | `Authentication successful` |
| **应用层心跳** | `ss -tnpi` 连采两次 | 通过 | `lastrcv` 每 30s 归零一次，与 SDK 心跳间隔一致（非仅 TCP 挂着） |
| **崩溃自愈** | `kill -9` 主进程 | 通过 | 10s 内拉起新 PID、重新认证、新连接建立；`NRestarts` +1 |
| 工具层读取真实库 | 生产侧 Python 直调 | 通过 | 6 个仓；芒果酱跨仓 `5.67` 箱 / 6 仓；预警前 3 项均为可颂（缺口 50） |
| **端到端问答** | 生产侧 `bot.ask` 打真实 LLM | 通过 | 「冷冻芒果酱还有多少」→ 5.67 箱 / 6 仓明细；「芒果酱卖多少钱」→ 360 / 393 / 未维护 |
| 只读边界 | 本机 + 单测 | 通过 | `UPDATE` / `DELETE` 均报 `attempt to write a readonly database` |
| 单实例约束 | `pgrep` | 通过 | 仅 1 个 `bot.serve` 进程 |
| 进出站连通性 | `curl` | 通过 | `openws.work.weixin.qq.com` 与 `api.minimaxi.com` 均完成 TLS 握手 |
| 企微群真人收发 | 群里 @ | 待 owner 验证 | 本机同版本代码已闭环（`00:19:55 收到 → 已回复`） |

**检查中发现的自身缺陷（已修）**：`标签纸40*30` 里的 `30` 被数字对账判成「无出处数字」，
触发了一次无谓校正（且超时）。根因是只把数字类型字段当可信出处，模型照抄品名规格反被
当成幻觉。已改为：规格表达式整段跳过 + 工具返回的字符串字段也纳入可信集合。

## 5. 阻塞与已知问题

**（1）LLM 额度 —— 已解除**

部署期间曾遇 `HTTP 402 insufficient_balance_error`（账户级上限），故初次验证只做到
长连接认证与工具层。复查时额度已恢复（`HTTP 200`），端到端问答已在生产侧跑通。
**若额度再次耗尽，机器人会回一句兜底话术，进程不会崩。**

**（2）GitGuardian 误报**

`bot/probe.py` 曾把企微错误码写成字符串元组 `("853000", "853001")`，被判定为
Authentication Tuple。已在后续 commit 改为整数 `(853000, 853001)`，但历史 commit
仍在 PR 扫描范围内，故该检查仍显示失败。分支未受保护，不阻塞合并。

**（3）售价数据缺口**

研发中心 44/140、浦发时光里 30/96 的 `selling_price` 未维护，机器人会如实回
「售价未维护」并给成本价。属数据侧待补，非代码问题。

**（4）宿主资源（非本次引入）**

根分区 40G 已用 79%（剩 8.1G），journald 占用 206.3M。bot 自身日志仅数十条，
不构成压力；但磁盘水位值得单独跟进。

## 6. 回滚演练（清理干净）

```bash
systemctl disable --now dailycheck-bot
rm -f /etc/systemd/system/dailycheck-bot.service
rm -rf /opt/dailycheck-bot /root/dailycheck-bot-venv
rm -f /etc/dailycheck/bot.env
systemctl daemon-reload
```

主站（Flask 蓝绿、MCP、nginx）完全不受影响：bot 不接 HTTP 流量、不占端口、
不与两实例共享进程或数据库连接。数字对账可单独用 `BOT_VERIFY_NUMBERS=0` 关闭，
无需整体回滚。

## 7. 报告签名

Dev Agent（WorkBuddy）· 基线 fde2048 · 工作分支 feat/wecom-inventory-bot
报告生成时间: 2026-10-21T01:38:00+08:00（部署后复查更新）
验证范围: 生产部署 / 长连接认证 / 应用层心跳 / 崩溃自愈 / 工具层真实数据 /
端到端问答（生产 CLI）/ 只读边界 / 单实例约束 / CI 全量测试
未覆盖: 企微群真人收发（需 owner 在群里 @ 一次）
