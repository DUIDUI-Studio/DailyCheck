#!/usr/bin/env bash
# blue_green/rollback.sh —— 秒级切回蓝
#
# 用法: ./scripts/blue_green/rollback.sh [--dry-run]
#
# 状态: v0.4 实现（2026-10-14 Eric 拍板）
# 触发：绿环境出现异常（错误率上升、P95 超阈、健康检查失败）
# 行为：
#   1. 调 switch.sh blue 100（nginx upstream 切回 100% 蓝）
#   2. 取证：journalctl（绿 24h 内） + 绿 DB 拷贝到 evidence dir
#   3. 绿 systemd unit 保留运行（待人工调查后用 down.sh 清理）
# 设计：docs/agent-framework/v0.1.md §四.2 第 5 项（回滚演练）
#
# 严守不变量：
#   - 蓝 :8090 始终 active（即使绿挂了，流量全归蓝，0 中断）
#   - 切回 100% 蓝后，绿仍 inactive（不删，给取证留时间）

set -euo pipefail

# ============================================================
# 参数解析
# ============================================================
# 先扫 --help
for arg in "$@"; do
  if [[ "$arg" == "-h" || "$arg" == "--help" ]]; then
    echo "usage: $0 [--dry-run]"
    echo "  --dry-run  只 echo 不真跑"
    exit 0
  fi
done

DRY_RUN_FLAG=0

while [[ $# -gt 0 ]]; do
  case "${1}" in
    --dry-run) DRY_RUN_FLAG=1 ;;
    *) echo "unknown arg: ${1}" >&2; exit 64 ;;
  esac
  shift
done

# ============================================================
# 配置
# ============================================================
GREEN_SERVICE_UNIT="${GREEN_SERVICE_UNIT:-dailycheck-green.service}"
GREEN_MCP_SERVICE_UNIT="${GREEN_MCP_SERVICE_UNIT:-dailycheck-green-mcp.service}"
GREEN_DB="${GREEN_DB:-/var/lib/dailycheck-green}"
EVIDENCE_DIR="${EVIDENCE_BASE:-/var/log/dailycheck}"

# ============================================================
# 进程级互斥（纵深防御 · flock；dry-run / 已持锁时跳过）
# ============================================================
# rollback 是止损方向：不做会话级锁校验（不被部署锁阻塞，保留应急能力），
# 只用 flock 等待至多 30s —— 拿不到说明另一脚本正在动蓝绿设施，拒绝并发。
if [[ ${DRY_RUN_FLAG} -eq 0 && "${DAILYCHECK_BG_LOCK_HELD:-0}" != "1" ]]; then
  _BG_LOCK_FILE="${DAILYCHECK_BG_LOCK_FILE:-/var/lock/dailycheck-bluegreen.lock}"
  if ! command -v flock >/dev/null 2>&1; then
    echo "[rollback] WARN: flock 不可用 —— 进程级互斥跳过" >&2
  elif ! mkdir -p "$(dirname "${_BG_LOCK_FILE}")" 2>/dev/null || ! : >>"${_BG_LOCK_FILE}" 2>/dev/null; then
    echo "[rollback] WARN: 锁文件不可写（${_BG_LOCK_FILE}）—— 进程级互斥跳过" >&2
  else
    exec 9>"${_BG_LOCK_FILE}"
    if ! flock -w 30 9; then
      echo "[rollback] ERROR: 等待 30s 未获得蓝绿锁（另一操作进行中）—— 拒绝并发；确需强制回滚请先停掉相关脚本" >&2
      exit 10
    fi
    export DAILYCHECK_BG_LOCK_HELD=1
  fi
fi

# ============================================================
# 辅助
# ============================================================
_run() {
  if [[ ${DRY_RUN_FLAG} -eq 1 ]]; then
    echo "[DRY-RUN] would execute: $*"
  else
    "$@"
  fi
}

echo "[rollback] === 秒级切回蓝 ==="
echo "[rollback] mode: $([[ ${DRY_RUN_FLAG} -eq 1 ]] && echo 'dry-run' || echo 'live')"
echo "[rollback] trigger: $(date -u +'%Y-%m-%dT%H:%M:%SZ')"

# ============================================================
# 1. 切流量到 100% 蓝（核心动作 — 秒级）
# ============================================================
echo "[rollback] step 1/4: switch.sh blue 100 ..."
if [[ ${DRY_RUN_FLAG} -eq 0 ]]; then
  "$(dirname "$0")/switch.sh" blue 100
  SWITCH_RC=$?
  if [[ ${SWITCH_RC} -ne 0 ]]; then
    echo "[rollback] ❌ switch.sh failed (rc=${SWITCH_RC})，回滚未完成" >&2
    echo "[rollback] 需人工查 nginx /etc/nginx/sites-enabled/dailycheck" >&2
    exit 1
  fi
else
  echo "[DRY-RUN] would call switch.sh blue 100"
fi

# ============================================================
# 2. 取证：journalctl 24h
# ============================================================
echo "[rollback] step 2/4: journalctl 取证 ..."
if [[ ${DRY_RUN_FLAG} -eq 0 ]]; then
  _run mkdir -p "${EVIDENCE_DIR}"
  TS="$(date +%Y%m%d-%H%M%S)"
  EVIDENCE_SUBDIR="${EVIDENCE_DIR}/rollback-${TS}"
  _run mkdir -p "${EVIDENCE_SUBDIR}"
  _run journalctl -u "${GREEN_SERVICE_UNIT}" --since "24 hours ago" > "${EVIDENCE_SUBDIR}/flask.log" 2>/dev/null || true
  _run journalctl -u "${GREEN_MCP_SERVICE_UNIT}" --since "24 hours ago" > "${EVIDENCE_SUBDIR}/mcp.log" 2>/dev/null || true
  echo "[rollback] 取证落 ${EVIDENCE_SUBDIR}/"
else
  echo "[DRY-RUN] would journalctl -u ${GREEN_SERVICE_UNIT} --since 24h"
  echo "[DRY-RUN] would journalctl -u ${GREEN_MCP_SERVICE_UNIT} --since 24h"
fi

# ============================================================
# 3. 取证：绿 DB 拷贝
# ============================================================
echo "[rollback] step 3/4: 绿 DB 取证拷贝 ..."
if [[ ${DRY_RUN_FLAG} -eq 0 ]]; then
  if [[ -d "${GREEN_DB}" ]]; then
    _run cp -a "${GREEN_DB}" "${EVIDENCE_SUBDIR}/db" || true
    echo "[rollback] 绿 DB 已 cp 到 ${EVIDENCE_SUBDIR}/db"
  else
    echo "[rollback] ⚠️ ${GREEN_DB} 不存在（绿没起过或已清）"
  fi
else
  echo "[DRY-RUN] would cp -a ${GREEN_DB} ${EVIDENCE_DIR}/rollback-*/db"
fi

# ============================================================
# 4. 写回滚报告
# ============================================================
echo "[rollback] step 4/4: rollback report ..."
if [[ ${DRY_RUN_FLAG} -eq 0 ]]; then
  cat > "${EVIDENCE_SUBDIR}/rollback-report.md" <<EOF
# 回滚事件 ($(date -u +'%Y-%m-%dT%H:%M:%SZ'))

- 触发: $0 (人工/监控)
- 切流量: 100% 蓝 (switch.sh blue 100)
- 绿 systemd: 保留运行（取证）
- 证据目录: ${EVIDENCE_SUBDIR}

待人工调查后用 down.sh 清理。
EOF
  echo "[rollback] ✅ 已切回 100% 蓝，证据落 ${EVIDENCE_SUBDIR}/rollback-report.md"
  echo "[rollback] 绿 systemd unit 仍运行，待人工调查后用 down.sh 清理"
else
  echo "[DRY-RUN] would write rollback-report.md"
fi

echo "[rollback] ✅ done"
exit 0
