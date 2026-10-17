#!/usr/bin/env bash
# blue_green/down.sh —— 清理绿环境
#
# 用法: ./scripts/blue_green/down.sh [--keep-blue] [--dry-run]
#
# 默认行为：停绿 systemd + 删绿 DB + 删绿 unit + Nginx 切回 100% 蓝
# --keep-blue：仅停绿 systemd（蓝降 standby 7 天，保留绿 DB/取证）
#             Nginx 不动（绿仍 down，不接流量）
# --dry-run：只 echo 不真跑
#
# 状态: v0.4 实现（2026-10-14 Eric 拍板）
# 设计依据：v0.1.md §四.2 + v0.3 env 变量约定

set -euo pipefail

# ============================================================
# 参数解析
# ============================================================
# 先扫 --help
for arg in "$@"; do
  if [[ "$arg" == "-h" || "$arg" == "--help" ]]; then
    echo "usage: $0 [--keep-blue] [--dry-run]"
    echo "  --keep-blue  仅停绿 systemd，保留 DB 取证 7d"
    echo "  --dry-run    只 echo 不真跑"
    exit 0
  fi
done

KEEP_BLUE=0
DRY_RUN_FLAG=0

while [[ $# -gt 0 ]]; do
  case "${1}" in
    --keep-blue) KEEP_BLUE=1 ;;
    --dry-run) DRY_RUN_FLAG=1 ;;
    *) echo "unknown arg: ${1}" >&2; exit 64 ;;
  esac
  shift
done

# ============================================================
# 配置
# ============================================================
GREEN_UNIT="${GREEN_SERVICE_UNIT:-dailycheck-green.service}"
GREEN_MCP_UNIT="${GREEN_MCP_SERVICE_UNIT:-dailycheck-green-mcp.service}"
GREEN_DB="${GREEN_DB:-/var/lib/dailycheck-green}"
EVIDENCE_DIR="${EVIDENCE_BASE:-/var/log/dailycheck}"

# ============================================================
# 进程级互斥（纵深防御 · flock；dry-run / 已持锁时跳过）
# ============================================================
# down 是清理方向：不做会话级锁校验（清理/恢复场景不被部署锁阻塞），
# 只用 flock 等待至多 30s —— 拿不到说明另一脚本正在动蓝绿设施，拒绝并发。
if [[ ${DRY_RUN_FLAG} -eq 0 && "${DAILYCHECK_BG_LOCK_HELD:-0}" != "1" ]]; then
  _BG_LOCK_FILE="${DAILYCHECK_BG_LOCK_FILE:-/var/lock/dailycheck-bluegreen.lock}"
  if ! command -v flock >/dev/null 2>&1; then
    echo "[down] WARN: flock 不可用 —— 进程级互斥跳过" >&2
  elif ! mkdir -p "$(dirname "${_BG_LOCK_FILE}")" 2>/dev/null || ! : >>"${_BG_LOCK_FILE}" 2>/dev/null; then
    echo "[down] WARN: 锁文件不可写（${_BG_LOCK_FILE}）—— 进程级互斥跳过" >&2
  else
    exec 9>"${_BG_LOCK_FILE}"
    if ! flock -w 30 9; then
      echo "[down] ERROR: 等待 30s 未获得蓝绿锁（另一操作进行中）—— 拒绝并发；确需强制请先停掉相关脚本" >&2
      exit 10
    fi
    export DAILYCHECK_BG_LOCK_HELD=1
  fi
fi

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

echo "[down] === 清理绿环境 ==="
echo "[down] mode: $([[ ${DRY_RUN_FLAG} -eq 1 ]] && echo 'dry-run' || echo 'live')"
echo "[down] keep-blue: ${KEEP_BLUE}"

# 1. 停绿 systemd（保留取证）
echo "[down] stopping ${GREEN_UNIT} ..."
_run systemctl stop "${GREEN_UNIT}" || echo "[down] ${GREEN_UNIT} 未运行，跳过 stop"
echo "[down] stopping ${GREEN_MCP_UNIT} ..."
_run systemctl stop "${GREEN_MCP_UNIT}" || echo "[down] ${GREEN_MCP_UNIT} 未运行，跳过 stop"

# 2. 取证：journalctl → evidence dir
if [[ ${DRY_RUN_FLAG} -eq 0 ]]; then
  _run mkdir -p "${EVIDENCE_DIR}"
  TS="$(date +%Y%m%d-%H%M%S)"
  _run journalctl -u "${GREEN_UNIT}" --since "24 hours ago" > "${EVIDENCE_DIR}/green-down-${TS}-flask.log" 2>/dev/null || true
  _run journalctl -u "${GREEN_MCP_UNIT}" --since "24 hours ago" > "${EVIDENCE_DIR}/green-down-${TS}-mcp.log" 2>/dev/null || true
  echo "[down] 取证：${EVIDENCE_DIR}/green-down-${TS}-*.log"
fi

# 3. 备份 systemd unit（以备重建）
if [[ ${DRY_RUN_FLAG} -eq 0 && -f "/etc/systemd/system/${GREEN_UNIT}" ]]; then
  _run cp "/etc/systemd/system/${GREEN_UNIT}" "${EVIDENCE_DIR}/${GREEN_UNIT}.down-${TS}.bak" || true
  _run cp "/etc/systemd/system/${GREEN_MCP_UNIT}" "${EVIDENCE_DIR}/${GREEN_MCP_UNIT}.down-${TS}.bak" 2>/dev/null || true
fi

# 4. Nginx 切回 100% 蓝（保险起见——绿停止后流量也应 100% 蓝）
if [[ ${DRY_RUN_FLAG} -eq 0 ]]; then
  NGINX_CONF="${NGINX_DAILYCHECK_CONF:-/etc/nginx/sites-enabled/dailycheck}"
  if [[ -f "${NGINX_CONF}" ]]; then
    if grep -q "server 127.0.0.1:8091 down" "${NGINX_CONF}" || \
       grep -q "server 127.0.0.1:8091 weight=" "${NGINX_CONF}"; then
      echo "[down] Nginx 切回 100% 蓝（保险）..."
      "$(dirname "$0")/switch.sh" blue 100 || echo "[down] ⚠️ switch.sh failed, manual nginx 调整"
    else
      echo "[down] Nginx 已不含绿配置，跳过"
    fi
  fi
else
  echo "[DRY-RUN] would check Nginx and call switch.sh blue 100 if needed"
fi

# 5. 删 systemd unit（幂等：unit 删了不影响 dailycheck.service）
if [[ ${DRY_RUN_FLAG} -eq 0 ]]; then
  echo "[down] removing systemd unit files ..."
  _run rm -f "/etc/systemd/system/${GREEN_UNIT}"
  _run rm -f "/etc/systemd/system/${GREEN_MCP_UNIT}"
  _run systemctl daemon-reload
else
  echo "[DRY-RUN] would rm /etc/systemd/system/${GREEN_UNIT}"
  echo "[DRY-RUN] would rm /etc/systemd/system/${GREEN_MCP_UNIT}"
  echo "[DRY-RUN] would systemctl daemon-reload"
fi

# 6. 删 / 留 DB
if [[ ${KEEP_BLUE} -eq 0 ]]; then
  echo "[down] removing ${GREEN_DB} ..."
  _run rm -rf "${GREEN_DB}"
else
  echo "[down] --keep-blue: 保留 ${GREEN_DB}（7d standby 由 cron 清理）"
  if [[ ${DRY_RUN_FLAG} -eq 0 && ! -d "${GREEN_DB}" ]]; then
    echo "[down] ⚠️ --keep-blue 但 ${GREEN_DB} 不存在（noop）" >&2
  fi
fi

echo "[down] ✅ done (keep_blue=${KEEP_BLUE})"
exit 0
