#!/usr/bin/env bash
# blue_green/up.sh —— 起绿环境
#
# 用法: ./scripts/blue_green/up.sh <pr-num> <commit-sha> [--dry-run] [--skip-db-copy]
#
# 状态: v0.4 实现（2026-10-14 Eric 拍板）
# 设计依据：v0.1.md §四 + Prod Agent 反馈 §三 + 决策 1+2+3
#   - DRY_RUN 中间件（db/dryrun.py，PR #20 已 merge）
#   - systemd 复用（dailycheck-green.service，Prod Agent 步骤 8 已建）
#   - Nginx upstream :8091 + :5101（down 标记，Prod Agent 步骤 9 已配）
#   - 数据库：cp -a /var/lib/dailycheck → /var/lib/dailycheck-green
#
# 设计原则：
#   - 蓝 :8090 不 kill（0 中断）
#   - 绿 :8091 起 gunicorn 2 workers + DRY_RUN=true
#   - 健康检查：30 次 × 2s = 60s 超时；/health 期望 200 + JSON
#   - 失败时取证：journalctl → /var/log/dailycheck/green-up-fail-${PR_NUM}.log
#
# 边界 / 前置（已就位 · 详见 v0.1.md §八 步骤 8）：
#   - dailycheck-green.service 已 enable（不要脚本内 enable，避免覆盖变更）
#   - /var/lib/dailycheck-green/ 已 cp（或本次 up.sh 自 cp）
#   - Nginx upstream :8091 + :5101 已配置 down 标记
#   - /etc/wdg/dailycheck-green-mcp.env 已替换 token
#   - DRY_RUN 中间件 PoC 通过

set -euo pipefail

# ============================================================
# 参数解析
# ============================================================
# 先扫 --help / -h（不依赖 PR_NUM 位置）
for arg in "$@"; do
  if [[ "$arg" == "-h" || "$arg" == "--help" ]]; then
    echo "usage: $0 <pr-num> <commit-sha> [--dry-run] [--skip-db-copy]"
    echo "  --dry-run       只 echo 不真跑（CI 沙箱/单测用）"
    echo "  --skip-db-copy  不重 cp DB（假设 Prod Agent 已准备好）"
    exit 0
  fi
done

PR_NUM="${1:-}"
COMMIT_SHA="${2:-}"
DRY_RUN_FLAG=0
SKIP_DB_COPY=0

if [[ -z "$PR_NUM" || -z "$COMMIT_SHA" ]]; then
  echo "usage: $0 <pr-num> <commit-sha> [--dry-run] [--skip-db-copy]" >&2
  exit 64
fi

# 剩余参数解析（位置参数 3+）
shift 2
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN_FLAG=1 ;;
    --skip-db-copy) SKIP_DB_COPY=1 ;;
    *) echo "unknown arg: $1" >&2; exit 64 ;;
  esac
  shift
done

# ============================================================
# 配置（与 deploy_vps.sh 模板化约定对齐：DAILYCHECK_* env 变量可覆盖）
# ============================================================
APP_DIR="${DAILYCHECK_APP_DIR:-/root/DailyCheck}"             # 实测 host 路径
BLUE_DB="${DAILYCHECK_DB_ROOT:-/var/lib/dailycheck}"         # 蓝 DB 根
GREEN_PORT="${GREEN_PORT:-8091}"                              # 绿 gunicorn 端口
GREEN_MCP_PORT="${GREEN_MCP_PORT:-5101}"                      # 绿 MCP 端口
GREEN_DB="${GREEN_DB:-/var/lib/dailycheck-green}"             # 绿 DB 根
GREEN_UNIT="${GREEN_SERVICE_UNIT:-dailycheck-green.service}"
GREEN_MCP_UNIT="${GREEN_MCP_SERVICE_UNIT:-dailycheck-green-mcp.service}"
GREEN_HEALTH_URL="${GREEN_HEALTH_URL:-http://127.0.0.1:${GREEN_PORT}/health}"
GREEN_MCP_HEALTH_URL="${GREEN_MCP_HEALTH_URL:-http://127.0.0.1:${GREEN_MCP_PORT}/health}"
EVIDENCE_DIR="${EVIDENCE_BASE:-/var/log/dailycheck}"

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
# 主流程
# ============================================================
echo "[up] === 起绿环境 (PR #${PR_NUM} @ ${COMMIT_SHA:0:7}) ==="
echo "[up] mode: $([[ ${DRY_RUN_FLAG} -eq 1 ]] && echo 'dry-run' || echo 'live')"
echo "[up] db-copy: $([[ ${SKIP_DB_COPY} -eq 0 ]] && echo 'yes' || echo 'skip')"

# 1. 验证 APP_DIR 存在（dry-run 跳过 — dry-run 是测动作序列不是测环境）
if [[ ${DRY_RUN_FLAG} -eq 0 ]]; then
  if [[ ! -d "${APP_DIR}" ]]; then
    echo "[up] ERROR: APP_DIR not found: ${APP_DIR}" >&2
    exit 1
  fi
else
  echo "[up] [DRY-RUN] would check APP_DIR exists: ${APP_DIR}"
fi

# 2. 验证 git HEAD 一致（live 模式必查；dry-run 跳过）
#
# 注意：COMMIT_SHA 是短 SHA (7 chars)，git rev-parse HEAD 返 full SHA (40 chars)
# 直接比较永远不等。统一用 --short 让两边都 7 chars。
# 接受 full SHA 输入：--short 会截断到 7 chars 比较（SHA 唯一性保证安全）。
if [[ ${DRY_RUN_FLAG} -eq 0 ]]; then
  cd "${APP_DIR}"
  ACTUAL_HEAD="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
  COMMIT_SHA_SHORT="${COMMIT_SHA:0:7}"
  if [[ "${ACTUAL_HEAD}" != "${COMMIT_SHA_SHORT}" ]]; then
    echo "[up] ERROR: APP_DIR HEAD (${ACTUAL_HEAD}) != COMMIT_SHA (${COMMIT_SHA_SHORT})" >&2
    echo "[up]   Eric 需要先 git pull + checkout ${COMMIT_SHA}" >&2
    exit 1
  fi
else
  echo "[up] [DRY-RUN] would cd ${APP_DIR} && verify git HEAD == ${COMMIT_SHA:0:7}"
fi

# 3. cp 蓝 DB → 绿 DB
if [[ ${SKIP_DB_COPY} -eq 0 ]]; then
  echo "[up] copying ${BLUE_DB} -> ${GREEN_DB} ..."
  _run rm -rf "${GREEN_DB}"
  _run cp -a "${BLUE_DB}" "${GREEN_DB}"
  _run chmod -R u+rw "${GREEN_DB}"
else
  echo "[up] --skip-db-copy: using existing ${GREEN_DB}"
  if [[ ${DRY_RUN_FLAG} -eq 0 && ! -d "${GREEN_DB}" ]]; then
    echo "[up] ERROR: --skip-db-copy 但 ${GREEN_DB} 不存在" >&2
    exit 1
  fi
fi

# 4. 起绿 systemd unit
echo "[up] starting ${GREEN_UNIT} on :${GREEN_PORT} ..."
_run systemctl start "${GREEN_UNIT}"
echo "[up] starting ${GREEN_MCP_UNIT} on :${GREEN_MCP_PORT} ..."
_run systemctl start "${GREEN_MCP_UNIT}"

# 5. 健康检查（30 × 2s = 60s 超时）
HEALTH_OK=0
echo "[up] health check ${GREEN_HEALTH_URL} ..."
if [[ ${DRY_RUN_FLAG} -eq 1 ]]; then
  echo "[up] [DRY-RUN] would loop 30 × 2s curl ${GREEN_HEALTH_URL}"
  HEALTH_OK=1
else
  for i in {1..30}; do
    if curl -fsS "${GREEN_HEALTH_URL}" >/dev/null 2>&1; then
      echo "[up] ✅ Flask healthy after ${i}×2s"
      HEALTH_OK=1
      break
    fi
    sleep 2
  done
fi

# 6. MCP 健康（独立端口）
MCP_OK=0
if [[ ${HEALTH_OK} -eq 1 ]]; then
  echo "[up] MCP health check ${GREEN_MCP_HEALTH_URL} ..."
  if [[ ${DRY_RUN_FLAG} -eq 1 ]]; then
    echo "[up] [DRY-RUN] would loop 5 × 2s curl ${GREEN_MCP_HEALTH_URL}"
    MCP_OK=1
  else
    for i in {1..5}; do
      # MCP 端点可能没有 /health 路由，只验 socket listen
      if ss -tln 2>/dev/null | grep -q ":${GREEN_MCP_PORT} "; then
        echo "[up] ✅ MCP listening :${GREEN_MCP_PORT}"
        MCP_OK=1
        break
      fi
      sleep 2
    done
  fi
fi

# 7. 失败时取证
if [[ ${HEALTH_OK} -eq 0 || ${MCP_OK} -eq 0 ]]; then
  echo "[up] health check timeout / failed" >&2
  if [[ ${DRY_RUN_FLAG} -eq 0 ]]; then
    echo "[up] 取证：journalctl + DB snapshot → ${EVIDENCE_DIR}/"
    _run mkdir -p "${EVIDENCE_DIR}"
    _run journalctl -u "${GREEN_UNIT}" -n 200 > "${EVIDENCE_DIR}/green-up-fail-${PR_NUM}-flask.log" 2>/dev/null || true
    _run journalctl -u "${GREEN_MCP_UNIT}" -n 200 > "${EVIDENCE_DIR}/green-up-fail-${PR_NUM}-mcp.log" 2>/dev/null || true
    if [[ -d "${GREEN_DB}" ]]; then
      _run cp -a "${GREEN_DB}" "${EVIDENCE_DIR}/green-up-fail-${PR_NUM}-db" || true
    fi
  fi
  echo "[up] 取证完成（PR_NUM=${PR_NUM}）"
  exit 1
fi

echo "[up] ✅ 绿环境就绪（PR #${PR_NUM} @ ${COMMIT_SHA:0:7}，端口 :${GREEN_PORT} + :${GREEN_MCP_PORT}）"
exit 0
