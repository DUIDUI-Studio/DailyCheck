#!/usr/bin/env bash
# blue_green/switch.sh —— 切流量（单 upstream 内 weight/down 互转，Issue #32 Option 2）
#
# 用法: ./scripts/blue_green/switch.sh <green|blue> [percent] [--dry-run]
#
# 行为（nginx 布局 = scripts/blue_green/nginx-site.conf 模板）：
#   switch.sh green 5    → 绿 weight=5,   蓝 weight=95     （真实灰度）
#   switch.sh green 50   → 绿 weight=50,  蓝 weight=50
#   switch.sh green 100  → 绿 weight=100, 蓝 down
#   switch.sh blue  100  → 蓝 weight=100, 绿 down（回滚语义）
#
# 关键设计（2026-10-14 Eric 拍板 down 关键字；2026-10-15 Issue #32 重构）：
#   - nginx 布局是【单 upstream 蓝绿同池】：location 的 proxy_pass 永远指向
#     dailycheck / dailycheck-mcp，切换只动 server 行 —— 不碰 location
#   - 布局守卫：conf 若是旧布局（location 指 dailycheck-blue/green）直接拒跑，
#     否则 upstream 切换对真实流量无效（Issue #32 的 502 根因）
#   - post-switch 探活：reload 后过 nginx 入口真实打主站 + MCP，非 502 才算
#     成功；失败自动回滚备份再 reload
#   - 用 nginx "down" 关键字标记"不参与负载"（weight=0 nginx 1.24 拒绝）
#   - nginx -t 验证语法（防 sed 错配）；失败时 cp 备份回滚

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
# Issue #24 Bug A: 备份绝不能写进 sites-enabled/ —— nginx 主配置
# `include sites-enabled/*` 会把 *.bak 当配置加载，duplicate upstream 永久 fail。
# 统一落到独立备份目录（可用 BACKUP_DIR 覆盖）。
BACKUP_DIR="${BACKUP_DIR:-/var/lib/dailycheck-blue-green-backups}"
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

# 保证 backup 目录存在（不依赖 nginx 任何行为；dry-run 只回显）
_ensure_backup_dir() {
  if [[ ${DRY_RUN_FLAG} -eq 0 ]]; then
    mkdir -p "${BACKUP_DIR}"
  else
    echo "[DRY-RUN] would mkdir -p ${BACKUP_DIR}"
  fi
}
_ensure_backup_dir

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
# 布局守卫（Issue #32）：conf 必须是单 upstream 形态（location 永远指
# dailycheck / dailycheck-mcp）。若是旧布局（location 指 dailycheck-blue/green）
# 直接拒跑 —— 上游切了真实流量也不动（502 必现）。
#
# 判定：grep "proxy_pass http://dailycheck-blue" 或 "dailycheck-green" 命中
# = 旧布局；proxy_pass http://dailycheck;" 配合 "proxy_pass http://dailycheck-mcp;"
# = 新布局。
# ============================================================
if [[ -f "${NGINX_CONF}" ]]; then
  if grep -qE 'proxy_pass http://dailycheck-(blue|green)[\s;]' "${NGINX_CONF}"; then
    echo "[switch] 旧布局守卫触发：${NGINX_CONF} 仍指向 dailycheck-blue/green upstream" >&2
    echo "[switch]   —— 重构为 scripts/blue_green/nginx-site.conf 模板后重跑" >&2
    echo "[switch]   —— 旧布局下切换 upstream 对真实流量无效（Issue #32 502 根因）" >&2
    exit 2
  fi
  if ! grep -qE 'proxy_pass http://dailycheck-mcp[\s;]' "${NGINX_CONF}"; then
    echo "[switch] 布局守卫：${NGINX_CONF} 缺 dailycheck-mcp upstream，拒跑" >&2
    exit 2
  fi
  if ! grep -qE 'proxy_pass http://dailycheck[\s;]' "${NGINX_CONF}"; then
    echo "[switch] 布局守卫：${NGINX_CONF} 缺 dailycheck upstream，拒跑" >&2
    exit 2
  fi
  echo "[switch] 布局守卫 OK：单 upstream 模型（location 指 dailycheck / dailycheck-mcp）"
else
  echo "[switch] 布局守卫跳过：${NGINX_CONF} 不存在（dev/沙箱允许）"
fi

# ============================================================
# 备份 + sed 替换（覆盖 weight=N 和 down 两种状态）
# ============================================================
BACKUP="${BACKUP_DIR}/dailycheck.$(date +%Y%m%d-%H%M%S).bak"
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

# ============================================================
# post-switch 探活（Issue #32）：过 nginx 入口 :8080 真实打主站 + MCP。
# 旧 bug 是「upstream 切了但 location 还在指旧池」→ reload 后用户 502。
# 探活非 502 才算成功；任一 5xx/超时自动 cp 备份回滚 + reload 恢复
# （失败也 exit 非零，让 verify.sh check 8 知道）。
# ============================================================
NGINX_ENTRY="${NGINX_ENTRY:-http://127.0.0.1:8080}"
if [[ ${DRY_RUN_FLAG} -eq 1 ]]; then
  echo "[DRY-RUN] would probe ${NGINX_ENTRY}/ 与 ${NGINX_ENTRY}/api/mcp/（非 502 才算成功）"
  echo "[switch] ✅ nginx reloaded, green=${GREEN_WEIGHT}% blue=${BLUE_WEIGHT}%"
  exit 0
fi

echo "[switch] post-switch 探活：${NGINX_ENTRY}/ 与 ${NGINX_ENTRY}/api/mcp/ ..."

# 取 http_code（--max-time 5；HEAD 也可，这里 GET 是因为 MCP 端不一定支持 HEAD）
_probe() {
  local url="$1" code
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "${url}" 2>/dev/null || echo 000)"
  echo "  ${url} → HTTP ${code}"
  # 200/2xx/3xx/4xx 都算"通"（业务 4xx 不是 nginx 502）；
  # 5xx 与 000/connection refused = 入口不可用，自动回滚
  if [[ "${code}" =~ ^5 ]] || [[ "${code}" == "000" ]]; then
    return 1
  fi
  return 0
}

PROBE_FAIL=0
_probe "${NGINX_ENTRY}/" || PROBE_FAIL=1
_probe "${NGINX_ENTRY}/api/mcp/" || PROBE_FAIL=1

if [[ ${PROBE_FAIL} -ne 0 ]]; then
  echo "[switch] 探活失败，自动回滚备份 + reload ..." >&2
  if cp "${BACKUP}" "${NGINX_CONF}"; then
    if nginx -t && systemctl reload nginx; then
      echo "[switch] 回滚 reload OK，conf 恢复到切换前状态" >&2
    else
      echo "[switch] 回滚后 nginx -t / reload 仍失败，需人工介入" >&2
    fi
  else
    echo "[switch] 回滚 cp 失败，conf 未恢复，需人工介入" >&2
  fi
  exit 1
fi

echo "[switch] ✅ nginx reloaded + 探活通过, green=${GREEN_WEIGHT}% blue=${BLUE_WEIGHT}%"
exit 0
