#!/usr/bin/env bash
# blue_green/deploy_pipeline.sh —— 端到端部署编排（单命令；merge 后 / deploy-first 候选）
#
# 用法: ./scripts/blue_green/deploy_pipeline.sh <pr-num> <commit-sha> [选项]
#
# 选项:
#   --session NAME          锁归属标签（默认 pipeline-<pr>），审计可追溯
#   --observe-min N         观察窗分钟数（默认 10；0 = 跳过）
#   --skip-verify           跳过 verify（仅限人工应急，会打 WARN）
#   --dry-run               只跑子脚本的 --dry-run，不 acquire / 不 release / 不碰流量
#   --heartbeat-interval N  后台心跳间隔秒（默认 120，须明显小于锁 TTL 1800s）
#   --sim-user / --sim-pass verify check 2 的探针账号（缺省沿用 env，再缺省自动生成）
#   --candidate             候选模式：fetch PR head、校验包含 main、checkout 后再部署
#   --merge-pr              观察窗通过后 FF-only 合并 PR（需与 --candidate 同用）
#
# 退出码:
#   0  全链成功（deployed 锚点已推进）    64 参数/用法错误
#   10 锁被占用（另一部署在跑）           11 acquire 失败
#   20 up 失败   21 verify 失败   22 promote 失败   23 switch 失败   24 观察窗异常（已回滚）
#   25 candidate 前置不满足               26 FF 合并失败（已回滚蓝）
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
# F4（2026-10-18 复盘）：优先仓库 venv 的 python，而非裸 python3。
# 根因：VPS 系统 python3 的 dist-packages/scripts 是**常规包**，import 时
# 优先级高于仓库的 namespace 包 scripts/（PEP 420 namespace 仅在全路径都
# 找不到常规包时生效）→ 末尾 render_dashboard 的 `-m scripts.dailycheck_dashboard`
# 必报 No module，钩子长期静默失败、只 WARN 靠 15min timer 兜底（掩盖了坏）。
# verify.sh 早已 venv 优先（DC_PYTHON_BIN），此处对齐同一口径。
# venv 不存在时（dev 沙盒 / 测试）退回 python3；PYTHON 环境变量仍可显式覆盖。
if [[ -n "${PYTHON:-}" ]]; then
  PY="${PYTHON}"
elif [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
  PY="${REPO_ROOT}/.venv/bin/python"
else
  PY="python3"
fi
DEFAULT_STATE_DIR="/var/lib/dailycheck-deploy"
DEFAULT_PROD_AGENT_INBOX="/var/lib/dailycheck-prod-agent/inbox.jsonl"
STATE_DIR="${DAILYCHECK_DEPLOY_STATE_DIR:-${DEFAULT_STATE_DIR}}"

LOG="${DAILYCHECK_PIPELINE_LOG:-/var/log/dailycheck/deploy-pipeline-$(date +%Y%m%d-%H%M%S).log}"
if ! { mkdir -p "$(dirname "${LOG}")" 2>/dev/null && : >>"${LOG}" 2>/dev/null; }; then
  LOG="/tmp/deploy-pipeline-$(date +%Y%m%d-%H%M%S).log"
  : >>"${LOG}" 2>/dev/null || die 64 "无法创建日志文件"
fi

PR=""
SHA=""
SESSION=""
OBSERVE_MIN=10
# F5（2026-10-18 复盘）：机制要求的观察窗下限。低于此值不阻断部署（人工应急
# 确实需要能压缩），但必须落一条 audit 事件——让「观察窗被压缩」从"没人知道"
# 变成 Dashboard 可见。#111 实测压到 10min 却无任何留痕，正是复盘要消灭的静默降级。
OBSERVE_MIN_REQUIRED=10
SKIP_VERIFY=0
DRY_RUN=0
HB_INTERVAL=120
SIM_USER="${SIM_USER_USER:-}"
SIM_PASS="${SIM_USER_PASSWORD:-}"

RELEASED=0
DS_READY=0
HB_PID=""
FAIL_STAGE=""
CANDIDATE=0
MERGE_PR=0
TREE_MOVED=0
ORIG_REF=""

log()  { printf '[pipeline] %s\n' "$*"; }
die()  { local code=$1; shift; log "ERROR: $*"; exit "${code}"; }

# ---------------------------------------------------------------- 结构化事件
# 把「部署过程中的异常/非常规动作」写成一行结构化事件到 STATE_DIR/audit.log，
# 供运维仪表盘消费（2026-10-18 复盘：让 Eric 不必 review 也能在 Dashboard 看到
# 工作树漂移 / 观察窗压缩 / 仪表盘钩子失败这些「静默降级」信号）。
# best-effort：状态目录不存在或写失败都不阻塞部署主链（只 log 不 die）。
_audit_event() {
  local kind="$1" detail="${2:-}"
  local audit_file="${STATE_DIR}/audit.log"
  [[ -d "${STATE_DIR}" ]] || return 0
  printf '%s pipeline-event kind=%s pr=%s sha=%s session=%s %s\n' \
    "$(date -u +%FT%TZ)" "${kind}" "${PR}" "${SHA:0:12}" "${SESSION}" "${detail}" \
    >>"${audit_file}" 2>/dev/null || log "WARN: audit 事件写入失败（kind=${kind}，不影响部署）"
}

usage() {
  cat <<'EOF'
用法: deploy_pipeline.sh <pr-num> <commit-sha> [选项]

  --session NAME          锁归属标签（默认 pipeline-<pr>）
  --observe-min N         观察窗分钟数（默认 10；0 = 跳过）
  --skip-verify           跳过八项验证（仅限人工应急）
  --dry-run               只跑子脚本 --dry-run，不碰锁与流量
  --heartbeat-interval N  后台心跳间隔秒（默认 120）
  --sim-user / --sim-pass verify 探针账号（缺省沿用 env，再缺省自动生成）
  --candidate             候选模式：fetch PR head、校验包含 main、checkout 后再部署
  --merge-pr              观察窗通过后 FF-only 合并 PR（需与 --candidate 同用）

退出码: 0 成功 / 64 参数错 / 10 锁被占 / 11 acquire 失败
        20 up / 21 verify / 22 promote / 23 switch / 24 观察窗异常（已回滚）
        25 candidate 前置不满足 / 26 FF 合并失败（已回滚蓝）
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
    --candidate)          CANDIDATE=1; shift ;;
    --merge-pr)           MERGE_PR=1; shift ;;
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
[[ "${MERGE_PR}" -eq 1 && "${CANDIDATE}" -eq 0 ]] && die 64 "--merge-pr 需要同时给 --candidate"

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

# 部署结果写回 PR + 投递 agent 事件（2026-10-25）。
# 为什么挂在 cleanup 而不是各 exit 点：脚本有十来条退出路径（die 64 / 10 / 11 /
# run_stage 20~23 / observe 24 / 幂等跳过 / 成功），逐条插桩必然漏。cleanup 本
# 来就拿得到 $? 和 FAIL_STAGE，是唯一能覆盖全部路径的位置。
# 为什么不用 issue：issue 会被巡检 agent 当需求认领，拿「nginx 配置漂了」去自动
# 改代码比没人管更坏。PR 评论留档（没人抢），事件流投给 agent —— 人不在这条
# 链路上，没有「通知谁去处理」这种推送。
_writeback_pr() {
  local rc=$1
  [[ "${DAILYCHECK_DEPLOY_REPORT:-1}" == "0" ]] && return 0
  [[ -n "${PR}" && -n "${SHA}" ]] || return 0
  local reporter="${SCRIPT_DIR}/deploy_report.py"
  if [[ ! -f "${reporter}" ]]; then
    log "WARN: deploy_report.py 缺失，跳过 PR 回写（不影响部署结果）"
    return 0
  fi
  # 事件文件与唤醒队列都跟着状态目录走：STATE_DIR 在测试/演练里会被指到
  # 临时目录；此时若收件箱仍是生产默认值，就把它改投到 STATE_DIR 下。显式
  # 指定的非默认收件箱保持原样，生产默认 STATE_DIR 的行为也不变（Issue #38）。
  local ev="${DAILYCHECK_DEPLOY_EVENT_LOG:-${STATE_DIR}/events.jsonl}"
  local inbox="${DAILYCHECK_PROD_AGENT_INBOX:-${DEFAULT_PROD_AGENT_INBOX}}"
  if [[ "${STATE_DIR}" != "${DEFAULT_STATE_DIR}" \
        && "${inbox}" == "${DEFAULT_PROD_AGENT_INBOX}" ]]; then
    inbox="${STATE_DIR}/inbox.jsonl"
  fi
  local flag=""
  [[ "${DRY_RUN}" -eq 1 ]] && flag="--dry-run"
  if DAILYCHECK_DEPLOY_EVENT_LOG="${ev}" \
     DAILYCHECK_PROD_AGENT_INBOX="${inbox}" \
     "${PY}" "${reporter}" write --pr "${PR}" --sha "${SHA}" --rc "${rc}" \
       --stage "${FAIL_STAGE:-}" --log "${LOG}" --repo "${REPO_ROOT}" ${flag} \
       >>"${LOG}" 2>&1; then
    log "PR #${PR} 部署结果已写回（rc=${rc}）"
  else
    log "WARN: PR 回写失败（不影响部署结果）"
  fi
  return 0
}

cleanup() {
  local rc=$?
  if [[ -n "${HB_PID}" ]] && kill -0 "${HB_PID}" 2>/dev/null; then
    kill -TERM "${HB_PID}" 2>/dev/null || true
    wait "${HB_PID}" 2>/dev/null || true
  fi
  if [[ "${TREE_MOVED}" -eq 1 && ${rc} -ne 0 ]]; then
    _restore_main || true
  fi
  if [[ "${RELEASED}" -eq 0 && "${DS_READY}" -eq 1 ]]; then
    if [[ ${rc} -eq 0 ]]; then
      _release success
    else
      _release failed "${FAIL_STAGE:-aborted} rc=${rc}"
    fi
  fi
  # 放最后：release 会再写几行日志，先回写就漏掉它们。且 _writeback_pr 内部
  # 全程 best-effort —— 回写失败绝不能把一次成功部署变成红。
  _writeback_pr "${rc}"
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

# ---------------------------------------------------------------- 候选模式
_merge_helper() {
  local merger="${DAILYCHECK_DEPLOY_MERGE_BIN:-${SCRIPT_DIR}/deploy_merge.py}"
  "${PY}" "${merger}" "$@"
}

_restore_main() {
  if git -C "${REPO_ROOT}" fetch --quiet origin main \
     && git -C "${REPO_ROOT}" checkout --quiet -B main origin/main; then
    TREE_MOVED=0
    log "✓ 工作树已恢复到 origin/main"
    return 0
  fi
  log "WARN: 恢复 origin/main 失败 —— 工作树可能不在已知好版本（${REPO_ROOT}）"
  return 1
}

_prepare_candidate() {
  log "→ candidate 前置校验 PR #${PR} sha=${SHA:0:12}"
  if ! git -C "${REPO_ROOT}" fetch --quiet origin "refs/pull/${PR}/head"; then
    log "✗ fetch refs/pull/${PR}/head 失败"
    return 1
  fi
  local fetched
  fetched="$(git -C "${REPO_ROOT}" rev-parse FETCH_HEAD 2>/dev/null || true)"
  if [[ "${fetched}" != "${SHA}" ]]; then
    log "✗ FETCH_HEAD=${fetched:0:12} 与传入 sha=${SHA:0:12} 不一致"
    return 1
  fi
  if ! git -C "${REPO_ROOT}" fetch --quiet origin main; then
    log "✗ fetch origin main 失败"
    return 1
  fi
  if ! git -C "${REPO_ROOT}" merge-base --is-ancestor origin/main "${SHA}"; then
    log "✗ 候选分支未包含 origin/main —— 先 rebase/merge main 再部署"
    return 1
  fi
  if ! _merge_helper check --pr "${PR}" --sha "${SHA}" >>"${LOG}" 2>&1; then
    log "✗ Gitea PR 前置校验失败（closed / draft / head sha 不符）"
    return 1
  fi
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    log "dry-run：候选前置校验通过，不 checkout"
    return 0
  fi
  ORIG_REF="$(git -C "${REPO_ROOT}" symbolic-ref --short -q HEAD \
    || git -C "${REPO_ROOT}" rev-parse HEAD)"
  if ! git -C "${REPO_ROOT}" checkout --quiet --detach "${SHA}"; then
    log "✗ checkout ${SHA:0:12} 失败"
    return 1
  fi
  TREE_MOVED=1
  log "✓ candidate checkout ${SHA:0:12}（原 ref: ${ORIG_REF}）"
  return 0
}

_merge_candidate() {
  log "→ FF 合并 PR #${PR}"
  local out rc
  out="$(_merge_helper merge --pr "${PR}" --sha "${SHA}" 2>&1)"
  rc=$?
  printf '%s\n' "${out}" >>"${LOG}"
  if [[ ${rc} -ne 0 ]]; then
    log "✗ FF 合并失败 rc=${rc}：${out}"
    log "  回滚流量到蓝，main 不动"
    bash "${SCRIPT_DIR}/switch.sh" blue 100 >>"${LOG}" 2>&1 \
      || log "WARN: 回滚蓝失败，见 ${LOG}"
    return 1
  fi
  log "✓ FF 合并完成：${out}"
  _restore_main || true
  local head
  head="$(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null || echo unknown)"
  if [[ "${head}" != "${SHA}" ]]; then
    log "WARN: 合并后本地 HEAD=${head:0:12} != ${SHA:0:12}（远端 main 已正确，下次部署会重新 fetch）"
  fi
  return 0
}

# ---------------------------------------------------------------- 观察窗
observe_window() {
  local minutes=$1
  local tick="${DAILYCHECK_PIPELINE_TICK_SEC:-300}"
  local probe="${DAILYCHECK_PIPELINE_PROBE_URL:-http://127.0.0.1:8080/}"
  local green_unit="${DAILYCHECK_PIPELINE_GREEN_UNIT:-dailycheck-green}"
  local curl_bin="${DAILYCHECK_PIPELINE_CURL:-curl}"
  local jctl_bin="${DAILYCHECK_PIPELINE_JOURNALCTL:-journalctl}"
  if [[ ${minutes} -le 0 ]]; then
    log "观察窗跳过（--observe-min 0）"
    return 0
  fi
  local rounds=$(( (minutes + 4) / 5 ))
  [[ ${rounds} -lt 1 ]] && rounds=1
  # BLOCKED 统计起点 = 观察窗开始时刻。⚠️ 不能按「总观察时长」回溯：promote
  # 之前绿实例仍在 DRY_RUN 下承接 verify 探针，那批 [dry_run] BLOCKED 会落进
  # 窗口，使首次检查（+5min）必然误判「写隔离没关」并回滚蓝。2026-10-17
  # PR #101 首次完整跑观察窗即命中（rc 24，回滚后新代码没上线）。
  local since_ts
  since_ts="$(date '+%Y-%m-%d %H:%M:%S')"
  local i code blocked elapsed_sec=0 elapsed_min
  for (( i=1; i<=rounds; i++ )); do
    sleep "${tick}"
    elapsed_sec=$(( elapsed_sec + tick ))
    elapsed_min=$(( elapsed_sec / 60 ))
    [[ ${elapsed_min} -lt 1 ]] && elapsed_min=1
    _hb "observe-${elapsed_min}m"
    code="$("${curl_bin}" -s -o /dev/null -w '%{http_code}' --max-time 10 "${probe}" 2>/dev/null)"
    code="${code:-000}"
    blocked="$("${jctl_bin}" -u "${green_unit}" --since "${since_ts}" 2>/dev/null \
               | grep -c '\[dry_run\] BLOCKED' || true)"
    blocked="${blocked:-0}"
    log "观察 +${elapsed_min}min: probe=${code} blocked=${blocked}（BLOCKED 统计自 ${since_ts}）"
    if [[ "${blocked}" -gt 0 || ( "${code}" != "302" && "${code}" != "200" ) ]]; then
      log "✗ 观察窗异常 —— 回滚蓝"
      # 回滚输出必须写独立文件：与主日志共用同一文件时，父进程 stdout 的缓冲
      # 会在 append offset 上覆盖子进程刚写下的内容，把上面两行诊断吃掉，
      # 事后只剩「已回滚」而无缘由。
      local rb_log="${LOG}.rollback"
      if bash "${SCRIPT_DIR}/switch.sh" blue 100 >>"${rb_log}" 2>&1; then
        log "已回滚蓝（回滚日志 ${rb_log}）"
      else
        log "WARN: 回滚亦失败，见 ${rb_log}"
      fi
      FAIL_STAGE="observe"
      exit 24
    fi
  done
}

# ---------------------------------------------------------------- 主链
DRY_FLAG=""
[[ "${DRY_RUN}" -eq 1 ]] && DRY_FLAG="--dry-run"

if [[ "${CANDIDATE}" -eq 1 ]]; then
  run_stage 25 candidate _prepare_candidate
fi

run_stage 20 up bash "${SCRIPT_DIR}/up.sh" "${PR}" "${SHA}" ${DRY_FLAG}

if [[ "${SKIP_VERIFY}" -eq 1 ]]; then
  log "WARN: --skip-verify —— 跳过八项验证（仅限人工应急）"
  _audit_event "skip-verify" "八项验证被跳过（人工应急）"
else
  [[ -n "${SIM_USER}" ]] || SIM_USER="sim_user"
  if [[ -z "${SIM_PASS}" ]]; then
    # Issue #123：token_urlsafe 约 1.5% 以 '-' 开头，会被 click 当成选项
    # （verify.sh create-user 报 "No such option '-i'" → check 2 假红）。
    # 固定字母前缀保证首字符安全，熵不变（常量前缀 + 18 字节随机）。
    SIM_PASS="$("${PY}" -c 'import secrets; print("dc" + secrets.token_urlsafe(18))')"
    log "verify 探针凭据：自动生成（每次部署一次性，不落盘）"
  fi
  export SIM_USER_USER="${SIM_USER}"
  export SIM_USER_PASSWORD="${SIM_PASS}"

  # ------------------------------------------------------------ F1 漂移守卫
  # #110 首轮 verify 判红（195 failed + BuildError）的真因不是代码，是 verify
  # 跑的 10 分钟里另一会话合了 #111 并 git pull，**工作树被换**：check1 的
  # pytest 读到混合态（进程已 import 旧模块、模板读新文件）。绿实例（check2）
  # 的 WorkingDirectory 由 systemd 写死为 REPO_ROOT，同样受漂移影响。
  #
  # 脚本层能做的：verify 前后各取一次工作树 HEAD，漂移即 fail-fast，把
  # 「神秘的 195 failed」变成「工作树在 verify 中被改动」这条明确原因。
  # ⚠️ 不能用 run_stage：它在 verify 失败时直接 exit，而 #110 正是 verify 因
  # 漂移而失败——那样漂移检测永远跑不到，恰好错过要诊断的场景。故这里手动
  # 捕获 verify rc，无论成败都先判漂移，再决定退出码与归因。
  # （让绿实例也跑 pinned 代码需改生产 systemd unit，属 owner 决策，见 PR body。）
  _head_before="$(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null || echo unknown)"
  log "→ verify"
  _hb "verify-start"
  bash "${SCRIPT_DIR}/verify.sh" "${PR}" ${DRY_FLAG} >>"${LOG}" 2>&1
  _verify_rc=$?
  _head_after="$(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null || echo unknown)"

  if [[ "${_head_before}" != "${_head_after}" \
        && "${_head_before}" != "unknown" && "${_head_after}" != "unknown" ]]; then
    # 漂移优先归因：无论 verify 红绿，工作树被换都让结果不可信。
    log "✗ 工作树在 verify 期间被改动（HEAD ${_head_before:0:12} → ${_head_after:0:12}）"
    log "  verify 结果不可信：check1 pytest / check2 绿实例都可能读到混合态"
    log "  （进程 import 旧模块 + 模板读新文件 = #110 的 195 failed 真因）。"
    log "  根因：部署锁只护脚本、护不住 git pull。处理：等 release 后再 pull，"
    log "        或对本 sha 重跑一轮部署。"
    _audit_event "worktree-drift" \
      "verify 期间 HEAD ${_head_before:0:12}->${_head_after:0:12}，结果不可信"
    FAIL_STAGE="verify-drift"
    exit 21
  fi

  if [[ ${_verify_rc} -ne 0 ]]; then
    FAIL_STAGE="verify"
    log "✗ verify 失败 (rc=${_verify_rc})"
    log "--- 日志尾部 ${LOG} ---"
    tail -n 20 "${LOG}" 2>/dev/null | sed 's/^/    /'
    exit 21
  fi
  _hb "verify-done"
  log "✓ verify"
fi

run_stage 22 promote bash "${SCRIPT_DIR}/promote.sh" ${DRY_FLAG}
run_stage 23 switch  bash "${SCRIPT_DIR}/switch.sh" green 100 ${DRY_FLAG}

if [[ "${DRY_RUN}" -eq 1 ]]; then
  log "dry-run 结束：不进入观察窗、不推进 deployed 锚点"
  exit 0
fi

# F5：观察窗被压缩到机制下限以下时留痕（不阻断——人工应急确实需要能压缩，
# 但「压了却没人知道」正是复盘要消灭的静默降级；#111 实测压到 10min 无留痕）。
if [[ "${OBSERVE_MIN}" -lt "${OBSERVE_MIN_REQUIRED}" ]]; then
  log "WARN: 观察窗 ${OBSERVE_MIN}min < 机制要求 ${OBSERVE_MIN_REQUIRED}min —— 已记 audit"
  _audit_event "observe-window-shortened" \
    "observe=${OBSERVE_MIN}min < required=${OBSERVE_MIN_REQUIRED}min"
fi

observe_window "${OBSERVE_MIN}"

if [[ "${MERGE_PR}" -eq 1 && "${DRY_RUN}" -eq 0 ]]; then
  run_stage 26 merge _merge_candidate
fi

_release success

# ---------------------------------------------------------------- 仪表盘钩子
# 部署成功后立即刷新运维仪表盘（2026-10-17 owner 定：不装常驻 webhook 触发器，
# 用「脚本内钩子 + systemd timer 兜底」两段式）。
# 为什么钩子放脚本里、不放 Prod Agent 的指令里：
#   会话随时可能在中途终止（#97 实测：promote 后会话死掉，流量压在蓝 7 分钟），
#   而本脚本一定会跑到最后一行 —— 把「记得刷新报表」从自然语言约束变成代码。
# 为什么 || 兜底 + timer：渲染依赖 gh/审计日志，任一不可用都不能让部署变红。
render_dashboard() {
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    return 0
  fi
  if [[ "${DAILYCHECK_DASHBOARD_HOOK:-1}" == "0" ]]; then
    log "仪表盘钩子已禁用（DAILYCHECK_DASHBOARD_HOOK=0）"
    return 0
  fi
  log "→ 刷新运维仪表盘"
  if ( cd "${REPO_ROOT}" && "${PY}" -m scripts.dailycheck_dashboard render \
         --trigger-kind deploy --trigger-event-id "pr-${PR}" ) >>"${LOG}" 2>&1; then
    log "✓ 仪表盘已刷新"
  else
    log "WARN: 仪表盘刷新失败 —— 不影响部署结果；dc-dashboard-refresh.timer 会在 15 分钟内补上"
    # F4：失败落 audit。此前钩子因 PY=python3 撞上 dist-packages 遮蔽而**长期
    # 静默失败**，只 WARN 到易被忽略的日志、靠 timer 兜底掩盖。留一条结构化
    # 事件，Dashboard 才能发现「钩子一直是坏的」，而不是等 timer 补上就以为没事。
    _audit_event "dashboard-hook-failed" "render 失败，依赖 timer 兜底（见 ${LOG}）"
  fi
  return 0
}
render_dashboard

log "✅ 全链完成 PR #${PR} sha=${SHA:0:12}"
exit 0
