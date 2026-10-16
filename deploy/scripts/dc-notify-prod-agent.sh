#!/bin/bash
# deploy/scripts/dc-notify-prod-agent.sh —— inbox 事件 → Prod Agent 唤醒适配器
#
# 角色：dc-inbox-watcher 的 --trigger-cmd。stdin = inbox 事件 JSON（原样一行）。
# 本脚本把事件投递给 Prod Agent 的入口；退出码非 0 会被 watcher 视为触发失败
# （退避重试 + WARNING 留痕）。
#
# 默认实现指向 Hermes 的 webhook 订阅（VPS 上的 agent 框架；示例，非指定）：
#   hermes webhook test <订阅名> --payload "$(cat)"
# 前置：
#   1. Hermes webhook platform 已启用（config.yaml platforms.webhook.enabled=true）
#   2. 订阅已建立：
#      hermes webhook subscribe dailycheck-pr \
#        --prompt 'DailyCheck PR 通知: PR #{pr_number} {action} by {pr_author} —— 走 dailycheck-prod-agent skill 判定 merge' \
#        --skills dailycheck-prod-agent
#
# 若 Prod Agent 侧用别的机制（cron / 消息队列 / 自研入口），只改本脚本实现，
# watcher 不需要任何改动。
#
# 安装：cp deploy/scripts/dc-notify-prod-agent.sh /usr/local/bin/dc-notify-prod-agent
#      chmod 755 /usr/local/bin/dc-notify-prod-agent
# 调试：
#   echo '{"delivery_id":"t1","action":"opened","pr_number":1,"pr_head_sha":"deadbeef","repo_full_name":"ericmr1981/DailyCheck"}' \
#     | /usr/local/bin/dc-notify-prod-agent; echo "rc=$?"

set -euo pipefail

payload=$(cat)
if [ -z "$payload" ]; then
    echo "dc-notify-prod-agent: empty payload on stdin" >&2
    exit 2
fi

# —— Prod Agent 入口（示例：Hermes webhook 订阅；替换点）——
exec /root/hermes-agent/hermes webhook test dailycheck-pr --payload "$payload"
