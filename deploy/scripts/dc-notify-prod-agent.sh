#!/bin/bash
# deploy/scripts/dc-notify-prod-agent.sh —— inbox 事件 → Prod Agent 唤醒适配器
#
# 角色：dc-inbox-watcher 的 --trigger-cmd。stdin = inbox 事件 JSON（原样一行）。
# 本脚本把事件投递给 Prod Agent 的入口；退出码非 0 会被 watcher 视为触发失败
# （退避重试 + WARNING 留痕）。
#
# 默认实现指向 Hermes 的 webhook 订阅（VPS 上的 agent 框架；示例，非指定），
# 按事件类型路由到两个订阅：
#   check_suite  → dailycheck-ci   （CI 完成快通道：全绿即触发 merge 判定；
#                                   prompt 内置 [SILENT] 静默语义，避免中间态打扰）
#   其余          → dailycheck-pr   （PR 生命周期触点：opened / synchronize / reopened）
# 前置：
#   1. Hermes webhook platform 已启用（config.yaml platforms.webhook.enabled=true）
#   2. 两个订阅已建立（dailycheck-pr / dailycheck-ci），详见部署手册 §3.8
#
# 若 Prod Agent 侧用别的机制（cron / 消息队列 / 自研入口），只改本脚本实现，
# watcher 不需要任何改动。
#
# ⚠️ exec 行必须用 venv 绝对路径 /root/hermes-agent/.venv/bin/hermes ——
#    /root/hermes-agent/hermes 的 shebang 是 #!/usr/bin/env python3，在 watcher 的
#    PATH（/root/DailyCheck/.venv/bin 排第一）下解析到 DailyCheck venv（缺 PyYAML）
#    → 触发恒失败 rc=1 ModuleNotFoundError: No module named 'yaml'（2026-10-16 实测）。
#
# 安装：cp deploy/scripts/dc-notify-prod-agent.sh /usr/local/bin/dc-notify-prod-agent
#      chmod 755 /usr/local/bin/dc-notify-prod-agent
# 调试：
#   echo '{"delivery_id":"t1","event":"pull_request","action":"opened","pr_number":1,"pr_head_sha":"deadbeef","repo_full_name":"ericmr1981/DailyCheck"}' \
#     | /usr/local/bin/dc-notify-prod-agent; echo "rc=$?"
#   echo '{"delivery_id":"t2","event":"check_suite","action":"completed","pr_number":1,"pr_head_sha":"deadbeef","repo_full_name":"ericmr1981/DailyCheck","check_conclusion":"success"}' \
#     | /usr/local/bin/dc-notify-prod-agent; echo "rc=$?"

set -euo pipefail

payload=$(cat)
if [ -z "$payload" ]; then
    echo "dc-notify-prod-agent: empty payload on stdin" >&2
    exit 2
fi

# 按事件类型路由订阅（解析失败 → event 空 → 落 dailycheck-pr 默认通道）
event=$(printf '%s' "$payload" | /root/DailyCheck/.venv/bin/python -c \
    'import json, sys; print((json.load(sys.stdin) or {}).get("event", ""))' 2>/dev/null || true)
case "$event" in
    check_suite) sub="dailycheck-ci" ;;
    *)           sub="dailycheck-pr" ;;
esac

# —— Prod Agent 入口（示例：Hermes webhook 订阅；替换点）——
exec /root/hermes-agent/.venv/bin/hermes webhook test "$sub" --payload "$payload"
