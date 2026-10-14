#!/usr/bin/env bash
# blue_green/switch.sh —— 切流量（nginx upstream weight）
#
# 用法: ./scripts/blue_green/switch.sh <green|blue> [percent]
#
# 状态: v0.2 骨架（systemd 路径，2026-10-13 修订）
# 行为：改 /etc/nginx/sites-enabled/dailycheck 的 upstream weight，
#       然后 nginx -t && systemctl reload nginx（流式重载，0 中断）。
#
# 示例：
#   switch.sh green 5     # 绿 5% / 蓝 95%
#   switch.sh green 50    # 绿 50% / 蓝 50%
#   switch.sh green 100   # 绿 100% / 蓝 0%
#   switch.sh blue  100   # 蓝 100% / 绿 0%（回滚）
#
# 前置条件：
#   1. nginx dailycheck 配置已加 upstream block（见 README §待补）
#   2. weight 范围 0-100，初始蓝 100 / 绿 0
#
# 设计：每次切换 nginx reload（流式），不丢请求。
#
# v0.3 修订（2026-10-13 Eric 拍板）：
#   - nginx 配置路径走 env 变量 + 默认值（NGINX_DAILYCHECK_CONF）

set -euo pipefail

TARGET="${1:-}"
PERCENT="${2:-100}"

case "${TARGET}" in
  green|blue) ;;
  *) echo "usage: $0 <green|blue> [percent]" >&2; exit 64 ;;
esac

if ! [[ "${PERCENT}" =~ ^[0-9]+$ ]] || [[ "${PERCENT}" -lt 0 || "${PERCENT}" -gt 100 ]]; then
  echo "[switch] percent must be 0-100" >&2
  exit 64
fi

# 配置（与 deploy_vps.sh 模板化约定对齐）
NGINX_CONF="${NGINX_DAILYCHECK_CONF:-/etc/nginx/sites-enabled/dailycheck}"
NGINX_BACKUP="${NGINX_CONF}.bak.$(date +%Y%m%d-%H%M%S)"

# ============================================================
# 骨架阶段：禁止执行
# ============================================================
echo "[switch] NOT YET IMPLEMENTED — nginx upstream 配置补完后才能触发" >&2
echo "[switch] 前置条件：" >&2
echo "[switch]   1. ${NGINX_CONF} 加 :8091 upstream block" >&2
echo "[switch]   2. weight 字段格式：\`server 127.0.0.1:8091 weight=N;\`" >&2
echo "[switch] 详见 scripts/blue_green/README.md" >&2
exit 78

# ============================================================
# 实现阶段（前置条件满足后取消上面 exit 78）
# ============================================================
#
# GREEN_WEIGHT="${PERCENT}"
# BLUE_WEIGHT=$((100 - PERCENT))
#
# echo "[switch] target=${TARGET} (green=${GREEN_WEIGHT}% blue=${BLUE_WEIGHT}%)"
#
# # 备份当前配置（防 sed 出错）
# cp "${NGINX_CONF}" "${NGINX_BACKUP}"
#
# # 改 weight（sed 替换两行）
# sed -i.bak \
#   -e "s|server 127.0.0.1:8090 weight=[0-9]*;|server 127.0.0.1:8090 weight=${BLUE_WEIGHT};|" \
#   -e "s|server 127.0.0.1:8091 weight=[0-9]*;|server 127.0.0.1:8091 weight=${GREEN_WEIGHT};|" \
#   "${NGINX_CONF}"
#
# # 测试 + reload（流式，0 中断）
# if nginx -t; then
#   systemctl reload nginx
#   echo "[switch] ✅ nginx reloaded, green=${GREEN_WEIGHT}% blue=${BLUE_WEIGHT}%"
# else
#   echo "[switch] ❌ nginx -t fail, rolling back ..." >&2
#   cp "${NGINX_BACKUP}" "${NGINX_CONF}"
#   nginx -t && systemctl reload nginx
#   exit 1
# fi
#
# # 验证：curl 看看是不是按 weight 走
# sleep 2
# for i in {1..5}; do
#   curl -fsS -o /dev/null -w "  green_resp=%{http_code}\n" "http://localhost:8080/?_probe=$i" || true
# done