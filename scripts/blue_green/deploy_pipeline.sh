#!/usr/bin/env bash
# blue_green/deploy_pipeline.sh —— merge 后的端到端部署编排（单命令）
#
# 用法: ./scripts/blue_green/deploy_pipeline.sh <pr-num> <commit-sha> [选项]
#
# 选项:
#   --session NAME          锁归属标签（默认 pipeline-<pr>），审计可追溯
#   --observe-min N         观察窗分钟数（默认 30；0 = 跳过）
#   --skip-verify           跳过 verify（仅限人工应急，会打 WARN）
#   --dry-run               只跑子脚本的 --dry-run，不 acquire / 不 release / 不碰流量
#   --heartbeat-interval N  后台心跳间隔秒（默认 120，须明显小于锁 TTL 1800s）
#   --sim-user / --sim-pass verify check 2 的探针账号（缺省沿用 env，再缺省自动生成）
#
# 退出码:
#   0  全链成功（deployed 锚点已推进）    64 参数/用法错误
#   10 锁被占用（另一部署在跑）           11 acquire 失败
#   20 up 失败   21 verify 失败   22 promote 失败   23 switch 失败   24 观察窗异常（已回滚）
#
# 为什么存在（2026-10-17 一天内四次同型故障）:
#   在此之前编排由「Prod Agent 会话读 SKILL.md 现场拼命令」承担，漏洞是结构性的：
#     ① switch 跑在 promote 前 → 被 DRY_RUN 守卫拒（顺序靠自然语言约束不可靠）
#     ② verify 10 分钟 / promote 7.5 分钟内无人打心跳 → 锁过期被合法抢占（PR #96）
#     ③ 会话空回复终止 → 绿已转正但流量还压在蓝，挂到人工接管（PR #97）
#     ④ 漏 release → deployed 锚点不前进，catchup 永远 behind
#   本脚本把顺序固化进代码、自管后台心跳、trap 保证收锁，四类故障在结构上消失。
#   Hermes 侧只需一行调用，不再有「步骤记忆」这个变量。
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
DS="${SCRIPT_DIR}/deploy_state.py"
PY="${PYTHON:-python3}"
STATE_DIR="${DAILYCHECK_DEPLOY_STATE_DIR:-/var/lib/dailycheck-deploy}"

LOG="${DAILYCHECK_PIPELINE_LOG:-/var/log/dailycheck/deploy-pipeline-$(date +%Y%m%d-%H%M%S).log}"
if ! { mkdir -p "$(dirname "${LOG}")" 2>/dev/null && : >>"${LOG}" 2>/dev/null; }; then
  LOG="/tmp/deploy-pipeline-$(date +%Y%m%d-%H%M%S).log"
  : >>"${LOG}" 2>/dev/null || die 64 "无法创建日志文件"
fi

PR=""
SHA=""
SESSION=""
OBSERVE_MIN=30
SKIP_VERIFY=0
DRY_RUN=0
HB_INTERVAL=120
SIM_USER="${SIM_USER_USER:-}"
SIM_PASS="${SIM_USER_PASSWORD:-}"

RELEASED=0
DS_READY=0
HB_PID=""
FAIL_STAGE=""

log()  { printf '[pipeline] %s\n' "$*"; }
die()  { local code=$1; shift; log "ERROR: $*"; exit "${code}"; }

usage() {
  cat <<'EOF'
用法: deploy_pipeline.sh <pr-num> <commit-sha> [选项]

  --session NAME          锁归属标签（默认 pipeline-<pr>）
  --observe-min N         观察窗分钟数（默认 30；0 = 跳过）
  --skip-verify           跳过八项验证（仅限人工应急）
  --dry-run               只跑子脚本 --dry-run，不碰锁与流量
  --heartbeat-interval N  后台心跳间隔秒（默认 120）
  --sim-user / --sim-pass verify 探针账号（缺省沿用 env，再缺省自动生成）

退出码: 0 成功 / 64 参数错 / 10 锁被占 / 11 acquire 失败
        20 up / 21 verify / 22 promote / 23 switch / 24 观察窗异常（已回滚）
EOF
}

# ---------------------------------------------------------------- 参数
while [[ $# -gt 0 ]]; do
  case "$1" in
    --session)            SESSION="${2:-}"; shift 2 ;;
    --observe-min)        OBSERVE_MIN="${2:-}"; shift 2 ;;
    --heartbeat-interval) HB_INTERVAL="${2:-}"; shift 2 ;;
    --sim-user)           SIM_USER="${2:-}"; shift 2 ;;
    --sim-pass)           SIM_PASS="${2:-}"; shift 2 ;;
    --skip-verify)        SKIP_VERIFY=1; shift ;;
    --dry-run)            DRY_RUN=1; shift ;;
    -h|--help)            usage; exit 0 ;;
    -*)                   die 64 "未知选项: $1" ;;
    *)
      if   [[ -z "${PR}"  ]]; then PR="$1"
      elif [[ -z "${SHA}" ]]; then SHA="$1"
      else die 64 "多余参数: $1"; fi
      shift ;;
  esac
done

[[ -n "${PR}" && -n "${SHA}" ]] || die 64 "用法: deploy_pipeline.sh <pr-num> <commit-sha> [选项]"
[[ "${PR}" =~ ^[0-9]+$ ]]       || die 64 "PR 编号必须是数字：${PR}"
[[ "${OBSERVE_MIN}" =~ ^[0-9]+$ ]] || die 64 "--observe-min 需整数：${OBSERVE_MIN}"
[[ "${HB_INTERVAL}" =~ ^[1-9][0-9]*$ ]] || die 64 "--heartbeat-interval 需正整数：${HB_INTERVAL}"

# SHA 归一化：本仓能解析就取完整 40 位；解析不了但形如短 sha 则原样接受（测试/离线）
_resolved="$(git -C "${REPO_ROOT}" rev-parse --verify "${SHA}^{commit}" 2>/dev/null || true)"
if [[ -n "${_resolved}" ]]; then
  SHA="${_resolved}"
elif [[ ! "${SHA}" =~ ^[0-9a-f]{7,40}$ ]]; then
  die 64 "SHA 无法解析且不像 commit sha：${SHA}"
fi
[[ -n "${SESSION}" ]] || SESSION="pipeline-${PR}"

log "PR #${PR} sha=${SHA:0:12} session=${SESSION} dry-run=${DRY_RUN} observe=${OBSERVE_MIN}min"
log "log=${LOG}"

# ---------------------------------------------------------------- 收锁
_release() {
  local result=$1 note="${2:-}"
  if "${PY}" "${DS}" release --result "${result}" --sha "${SHA}" --note "${note}" >>"${LOG}" 2>&1; then
    RELEASED=1
    log "release(${result}) 完成"
  else
    log "WARN: release(${result}) 失败 —— 锁可能需人工 deploy_state.py status 确认"
  fi
}

cleanup() {
  local rc=$?
  if [[ -n "${HB_PID}" ]] && kill -0 "${HB_PID}" 2>/dev/null; then
    kill -TERM "${HB_PID}" 2>/dev/null || true
    wait "${HB_PID}" 2>/dev/null || true
  fi
  if [[ "${RELEASED}" -eq 0 && "${DS_READY}" -eq 1 ]]; then
    if [[ ${rc} -eq 0 ]]; then
      _release success
    else
      _release failed "${FAIL_STAGE:-aborted} rc=${rc}"
    fi
  fi
}
trap cleanup EXIT

# ---------------------------------------------------------------- 幂等
if [[ "${DRY_RUN}" -eq 0 ]]; then
  _dep="$("${PY}" "${DS}" status 2>/dev/null | awk '/^deployed[[:space:]]*:/{print $3}')"
  if [[ -n "${_dep}" && "${_dep}" == "${SHA:0:12}" ]]; then
    log "已部署 ${SHA:0:12}（deployed 锚点一致），跳过"
    exit 0
  fi
fi

# ---------------------------------------------------------------- 抢锁 + 心跳
if [[ "${DRY_RUN}" -eq 1 ]]; then
  log "dry-run：跳过 acquire / 心跳 / release（不碰部署状态）"
else
  _acq_rc=0
  _acq_out="$("${PY}" "${DS}" acquire --pr "${PR}" --sha "${SHA}" \
              --session "${SESSION}" --phase "pipeline-start" 2>&1)" || _acq_rc=$?
  case "${_acq_rc}" in
    0)  DS_READY=1; log "已持锁" ;;
    10) die 10 "部署锁被占用：${_acq_out}" ;;
    *)  die 11 "acquire 失败 rc=${_acq_rc}：${_acq_out}" ;;
  esac
  # 后台常驻续约：这是「阶段耗时 > TTL」的结构解，不依赖任何人在中途记得打点
  "${PY}" "${DS}" heartbeat --loop --interval "${HB_INTERVAL}" --phase "pipeline-running" \
    >>"${LOG}" 2>&1 &
  HB_PID=$!
  log "后台心跳已启动 pid=${HB_PID} interval=${HB_INTERVAL}s"
fi

# 主流程阶段打点（后台循环只在第一拍写 phase，这里不会被盖回去）
_hb() {
  [[ "${DS_READY}" -eq 1 ]] || return 0
  "${PY}" "${DS}" heartbeat --phase "$1" >>"${LOG}" 2>&1 || true
}

# ---------------------------------------------------------------- 阶段执行
run_stage() {
  local code=$1 name=$2; shift 2
  log "→ ${name}"
  _hb "${name}-start"
  local rc
  "$@" >>"${LOG}" 2>&1
  rc=$?
  if [[ ${rc} -ne 0 ]]; then
    FAIL_STAGE="${name}"
    log "✗ ${name} 失败 (rc=${rc})"
    log "--- 日志尾部 ${LOG} ---"
    tail -n 20 "${LOG}" 2>/dev/null | sed 's/^/    /'
    exit "${code}"
  fi
  _hb "${name}-done"
  log "✓ ${name}"
  return 0
}

# ---------------------------------------------------------------- 观察窗
observe_window() {
  local minutes=$1
  local tick="${DAILYCHECK_PIPELINE_TICK_SEC:-300}"
  local probe="${DAILYCHECK_PIPELINE_PROBE_URL:-http://127.0.0.1:8080/}"
  if [[ ${minutes} -le 0 ]]; then
    log "观察窗跳过（--observe-min 0）"
    return 0
  fi
  local rounds=$(( (minutes + 4) / 5 ))
  [[ ${rounds} -lt 1 ]] && rounds=1
  local i code blocked
  for (( i=1; i<=rounds; i++ )); do
    sleep "${tick}"
    _hb "observe-$((i*5))m"
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "${probe}" 2>/dev/null)"
    code="${code:-000}"
    blocked="$(journalctl -u dailycheck-green --since "${minutes} min ago" 2>/dev/null \
               | grep -c 'BLOCKED' || true)"
    blocked="${blocked:-0}"
    log "观察 +$((i*5))min: probe=${code} blocked=${blocked}"
    if [[ "${blocked}" -gt 0 || ( "${code}" != "302" && "${code}" != "200" ) ]]; then
      log "✗ 观察窗异常 —— 回滚蓝"
      bash "${SCRIPT_DIR}/switch.sh" blue 100 >>"${LOG}" 2>&1 || log "WARN: 回滚亦失败"
      FAIL_STAGE="observe"
      exit 24
    fi
  done
}

# ---------------------------------------------------------------- 主链
DRY_FLAG=""
[[ "${DRY_RUN}" -eq 1 ]] && DRY_FLAG="--dry-run"

run_stage 20 up bash "${SCRIPT_DIR}/up.sh" "${PR}" "${SHA}" ${DRY_FLAG}

if [[ "${SKIP_VERIFY}" -eq 1 ]]; then
  log "WARN: --skip-verify —— 跳过八项验证（仅限人工应急）"
else
  [[ -n "${SIM_USER}" ]] || SIM_USER="sim_user"
  if [[ -z "${SIM_PASS}" ]]; then
    SIM_PASS="$("${PY}" -c 'import secrets; print(secrets.token_urlsafe(18))')"
    log "verify 探针凭据：自动生成（每次部署一次性，不落盘）"
  fi
  export SIM_USER_USER="${SIM_USER}"
  export SIM_USER_PASSWORD="${SIM_PASS}"
  run_stage 21 verify bash "${SCRIPT_DIR}/verify.sh" "${PR}" ${DRY_FLAG}
fi

run_stage 22 promote bash "${SCRIPT_DIR}/promote.sh" ${DRY_FLAG}
run_stage 23 switch  bash "${SCRIPT_DIR}/switch.sh" green 100 ${DRY_FLAG}

if [[ "${DRY_RUN}" -eq 1 ]]; then
  log "dry-run 结束：不进入观察窗、不推进 deployed 锚点"
  exit 0
fi

observe_window "${OBSERVE_MIN}"
_release success
log "✅ 全链完成 PR #${PR} sha=${SHA:0:12}"
exit 0
