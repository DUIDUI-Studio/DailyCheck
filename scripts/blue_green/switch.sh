#!/usr/bin/env bash
# blue_green/switch.sh —— 切流量（nginx upstream weight / down 互转）
#
# 用法: ./scripts/blue_green/switch.sh <green|blue> [percent] [--dry-run]
#
# 行为：
#   switch.sh green 5    → 绿 weight=5,   蓝 weight=95
#   switch.sh green 50   → 绿 weight=50,  蓝 weight=50
#   switch.sh green 100  → 绿 weight=100, 蓝 down
#   switch.sh blue  100  → 蓝 weight=100, 绿 down（回滚语义）
#
# 关键设计（2026-10-14 Eric 拍板）：
#   - 用 nginx "down" 关键字标记"不参与负载"（weight=0 nginx 1.24 拒绝）
#   - sed 替换 server 行的 weight= 字段
#   - nginx -t 验证语法（防 sed 错配）
#   - 失败时 cp 备份回滚（蓝仍 100% 在线）

set -euo pipefail

# ============================================================
# 参数解析
# ============================================================
# 先扫 --help / -h
for arg in "$@"; do
  if [[ "$arg" == "-h" || "$arg" == "--help" ]]; then
    echo "usage: $0 <green|blue> [percent] [--dry-run]"
    echo "  --dry-run   只 echo 不真跑 nginx -t / reload"
    exit 0
  fi
done

TARGET="${1:-}"
PERCENT="${2:-100}"
DRY_RUN_FLAG=0

case "${TARGET}" in
  green|blue) ;;
  *) echo "usage: $0 <green|blue> [percent] [--dry-run]" >&2; exit 64 ;;
esac

if ! [[ "${PERCENT}" =~ ^[0-9]+$ ]] || [[ "${PERCENT}" -lt 0 || "${PERCENT}" -gt 100 ]]; then
  echo "[switch] percent must be 0-100" >&2
  exit 64
fi

# 剩余参数解析
shift 2
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN_FLAG=1 ;;
    *) echo "unknown arg: $1" >&2; exit 64 ;;
  esac
  shift
done

# ============================================================
# 配置
# ============================================================
NGINX_CONF="${NGINX_DAILYCHECK_CONF:-/etc/nginx/sites-enabled/dailycheck}"
BLUE_PORT="${BLUE_PORT:-8090}"
GREEN_PORT="${GREEN_PORT:-8091}"
BLUE_MCP_PORT="${BLUE_MCP_PORT:-5100}"
GREEN_MCP_PORT="${GREEN_MCP_PORT:-5101}"

# ============================================================
# 辅助：dry-run 模式
# ============================================================
_run() {
  if [[ ${DRY_RUN_FLAG} -eq 1 ]]; then
    echo "[DRY-RUN] would execute: $*"
  else
    "$@"
  fi
}

# ============================================================
# 计算 weight + 边界转换（weight=0 → down）
# ============================================================
if [[ "${TARGET}" == "green" ]]; then
  GREEN_WEIGHT="${PERCENT}"
  BLUE_WEIGHT=$((100 - PERCENT))
else
  BLUE_WEIGHT="${PERCENT}"
  GREEN_WEIGHT=$((100 - PERCENT))
fi

_render_line() {
  local port="$1" weight="$2"
  if [[ ${weight} -eq 0 ]]; then
    echo "server 127.0.0.1:${port} down;"
  else
    echo "server 127.0.0.1:${port} weight=${weight};"
  fi
}

BLUE_LINE="$(         _render_line ${BLUE_PORT}      ${BLUE_WEIGHT})"
GREEN_LINE="$(        _render_line ${GREEN_PORT}     ${GREEN_WEIGHT})"
BLUE_MCP_LINE="$(     _render_line ${BLUE_MCP_PORT}  ${BLUE_WEIGHT})"
GREEN_MCP_LINE="$(    _render_line ${GREEN_MCP_PORT} ${GREEN_WEIGHT})"

echo "[switch] target=${TARGET} (green=${GREEN_WEIGHT}% blue=${BLUE_WEIGHT}%)"
echo "[switch] nginx_conf: ${NGINX_CONF}"
echo "[switch] blue line:    ${BLUE_LINE}"
echo "[switch] green line:   ${GREEN_LINE}"
echo "[switch] blue-mcp:     ${BLUE_MCP_LINE}"
echo "[switch] green-mcp:    ${GREEN_MCP_LINE}"

# ============================================================
# 备份 + sed 替换（覆盖 weight=N 和 down 两种状态）
# ============================================================
BACKUP="${NGINX_CONF}.bak.$(date +%Y%m%d-%H%M%S)"
_run cp "${NGINX_CONF}" "${BACKUP}"
echo "[switch] backup: ${BACKUP}"

_run sed -i \
  -e "s|server 127.0.0.1:${BLUE_PORT} weight=[0-9]*;|${BLUE_LINE}|" \
  -e "s|server 127.0.0.1:${BLUE_PORT} down;|${BLUE_LINE}|" \
  -e "s|server 127.0.0.1:${GREEN_PORT} weight=[0-9]*;|${GREEN_LINE}|" \
  -e "s|server 127.0.0.1:${GREEN_PORT} down;|${GREEN_LINE}|" \
  -e "s|server 127.0.0.1:${BLUE_MCP_PORT} weight=[0-9]*;|${BLUE_MCP_LINE}|" \
  -e "s|server 127.0.0.1:${BLUE_MCP_PORT} down;|${BLUE_MCP_LINE}|" \
  -e "s|server 127.0.0.1:${GREEN_MCP_PORT} weight=[0-9]*;|${GREEN_MCP_LINE}|" \
  -e "s|server 127.0.0.1:${GREEN_MCP_PORT} down;|${GREEN_MCP_LINE}|" \
  "${NGINX_CONF}"

# ============================================================
# nginx -t 验证（防 sed 错配）
# ============================================================
echo "[switch] nginx -t ..."
if [[ ${DRY_RUN_FLAG} -eq 0 ]]; then
  if ! nginx -t; then
    echo "[switch] nginx -t failed, rolling back ..." >&2
    cp "${BACKUP}" "${NGINX_CONF}"
    exit 1
  fi
else
  echo "[DRY-RUN] would nginx -t; on failure cp backup & exit 1"
fi

# ============================================================
# reload（流式，0 中断）
# ============================================================
echo "[switch] nginx reload ..."
_run systemctl reload nginx

echo "[switch] ✅ nginx reloaded, green=${GREEN_WEIGHT}% blue=${BLUE_WEIGHT}%"
exit 0
