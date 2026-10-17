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
#   - 数据库：cp -aL ${APP_DIR}/db → ${GREEN_DB}（解引用软链，
#     Issue #28：绿 unit 须声明 DAILYCHECK_DB_DIR 指向同一目录）
#
# 设计原则：
#   - 蓝 :8090 不 kill（0 中断）
#   - 绿 :8091 起 gunicorn 2 workers + DRY_RUN=true；**用 restart 而非 start**
#     （Issue #38：start 对在跑的 unit 是 no-op → 新 DB + 旧代码）
#   - **活流量守卫（2026-10-17 事故）**：restart 绿之前先解析 nginx conf
#     （NGINX_CONF，默认 /etc/nginx/sites-enabled/dailycheck）判断绿是否在承流
#     ——`server 127.0.0.1:8091` 非 down 且 weight>0 即「在役」。在役则先探活蓝
#     （BLUE_HEALTH_URL），健康就 `switch.sh blue 100` 把流量挪走再重启绿；蓝不
#     健康则报错 exit 1（**不重启绿**）。conf 读不到 / 解析不了同样 exit 1
#     （不放行猜测）。根因：绿承接 100% 流量时原地 restart 绿 = 全站 502 +
#     （unit 仍带 DRY_RUN 时）把线上实例换回「写隔离 + 副本库」→ 写操作静默假成功。
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
SRC_DB_DIR="${DAILYCHECK_SRC_DB_DIR:-${APP_DIR}/db}"          # 真库源（master.db + warehouses/）
GREEN_PORT="${GREEN_PORT:-8091}"                              # 绿 gunicorn 端口
GREEN_MCP_PORT="${GREEN_MCP_PORT:-5101}"                      # 绿 MCP 端口
# Issue #28: 绿 DB 根 = 独立副本目录（绿 unit 的 DAILYCHECK_DB_DIR 必须指向它）
GREEN_DB="${GREEN_DB:-/var/lib/dailycheck-green/db}"
GREEN_UNIT="${GREEN_SERVICE_UNIT:-dailycheck-green.service}"
GREEN_MCP_UNIT="${GREEN_MCP_SERVICE_UNIT:-dailycheck-green-mcp.service}"
GREEN_HEALTH_URL="${GREEN_HEALTH_URL:-http://127.0.0.1:${GREEN_PORT}/health}"
GREEN_MCP_HEALTH_URL="${GREEN_MCP_HEALTH_URL:-http://127.0.0.1:${GREEN_MCP_PORT}/health}"
EVIDENCE_DIR="${EVIDENCE_BASE:-/var/log/dailycheck}"

# ============================================================
# 活流量守卫配置（2026-10-17 生产事故加固）
# ============================================================
# 事故形态：绿当时已承接 100% 流量（switch.sh green 100 之后、转正之前/之后），
# 此时再跑 up.sh 会**原地 restart 正在服务 100% 流量的绿**——重启窗口内全站
# 502/超时；若 unit 仍带 DRY_RUN=true 与副本库，重启后还会把线上实例换回
# 「写隔离 + 副本库」态，全站写静默变「假成功」（HTTP 200、库不落盘）。
# switch.sh 的 DRY_RUN 守卫只拦「切换动作」，拦不住这条路径，故在 up.sh 里加
# 前置判定：绿在承流 → 先探活蓝并切蓝（switch.sh blue 100），再重启绿。
NGINX_CONF="${NGINX_CONF:-${NGINX_DAILYCHECK_CONF:-/etc/nginx/sites-enabled/dailycheck}}"
BLUE_HEALTH_URL="${BLUE_HEALTH_URL:-http://127.0.0.1:8090/health}"
CURL_BIN="${CURL_BIN:-curl}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SWITCH_SCRIPT="${SWITCH_SCRIPT:-${SCRIPT_DIR}/switch.sh}"

# ============================================================
# 部署单飞守卫 + 进程级互斥（deploy-singleflight，2026-10-16）
# ============================================================
# A. 会话级锁校验（仅 live；dry-run 完全跳过，保持演练/CI 行为不变）：
#    - 状态目录不存在          -> 单飞机制未激活，放行（向后兼容窗口）
#    - DAILYCHECK_DEPLOY_UNLOCKED=1 -> 人工逃生舱（警告放行）
#    - 有活跃锁且 PR 匹配       -> 放行（记审计行）
#    - 无活跃锁 / 锁属他 PR     -> 拒绝（exit 10）
# B. 进程级 flock（纵深防御；父脚本已持锁时经 DAILYCHECK_BG_LOCK_HELD 继承跳过）。
if [[ ${DRY_RUN_FLAG} -eq 0 ]]; then
  _DS_DIR="${DAILYCHECK_DEPLOY_STATE_DIR:-/var/lib/dailycheck-deploy}"
  _DS_SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
  if [[ "${DAILYCHECK_DEPLOY_UNLOCKED:-0}" == "1" ]]; then
    echo "[up] WARN: DAILYCHECK_DEPLOY_UNLOCKED=1 —— 跳过部署锁校验（人工操作）" >&2
  elif [[ ! -d "${_DS_DIR}" ]]; then
    echo "[up] [deploy-lock] 状态目录 ${_DS_DIR} 不存在（单飞机制未激活）—— 跳过校验"
  elif [[ ! -f "${_DS_SCRIPT_DIR}/deploy_state.py" ]] || ! command -v python3 >/dev/null 2>&1; then
    echo "[up] WARN: deploy_state.py / python3 不可用 —— 跳过部署锁校验" >&2
  else
    _ds_rc=0
    _ds_out="$(python3 "${_DS_SCRIPT_DIR}/deploy_state.py" check --pr "${PR_NUM}" 2>&1)" || _ds_rc=$?
    if [[ ${_ds_rc} -eq 10 ]]; then
      { printf '%s up.sh pr=%s %s\n' "$(date -u +%FT%TZ)" "${PR_NUM}" "${_ds_out}"; } \
        >> "${_DS_DIR}/audit.log" 2>/dev/null || true
    elif [[ ${_ds_rc} -eq 12 ]]; then
      echo "[up] ERROR: 部署锁属于另一个 PR（${_ds_out}）—— 拒绝并发部署" >&2
      exit 10
    elif [[ ${_ds_rc} -eq 0 ]]; then
      echo "[up] ERROR: 单飞机制已激活但无活跃部署锁（PR #${PR_NUM}）。" >&2
      echo "[up] 请先执行: python3 scripts/blue_green/deploy_state.py acquire --pr ${PR_NUM} --sha ${COMMIT_SHA}" >&2
      echo "[up] 人工受控操作可设 DAILYCHECK_DEPLOY_UNLOCKED=1 跳过。" >&2
      exit 10
    else
      echo "[up] WARN: 部署锁检查异常 rc=${_ds_rc}（${_ds_out}）—— 跳过校验" >&2
    fi
  fi

  if [[ "${DAILYCHECK_BG_LOCK_HELD:-0}" != "1" ]]; then
    _BG_LOCK_FILE="${DAILYCHECK_BG_LOCK_FILE:-/var/lock/dailycheck-bluegreen.lock}"
    if ! command -v flock >/dev/null 2>&1; then
      echo "[up] WARN: flock 不可用 —— 进程级互斥跳过（会话级锁仍生效）" >&2
    elif ! mkdir -p "$(dirname "${_BG_LOCK_FILE}")" 2>/dev/null || ! : >>"${_BG_LOCK_FILE}" 2>/dev/null; then
      echo "[up] WARN: 锁文件不可写（${_BG_LOCK_FILE}）—— 进程级互斥跳过" >&2
    else
      exec 9>"${_BG_LOCK_FILE}"
      if ! flock -n 9; then
        echo "[up] ERROR: 另一蓝绿操作正在进行（${_BG_LOCK_FILE} 被占）—— 拒绝并发" >&2
        exit 10
      fi
      export DAILYCHECK_BG_LOCK_HELD=1
    fi
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

# ============================================================
# 辅助：绿是否正在承接流量（nginx conf 解析，2026-10-17 事故加固）
# ============================================================
_http_code() {
  local url="$1"
  "${CURL_BIN}" -s -o /dev/null -w '%{http_code}' --max-time 5 "${url}" 2>/dev/null || true
}

# 输出 serving / idle / unknown（**只读**，不碰 conf）
#
#   serving —— 绿在承流（重启会打断在役实例）
#   idle    —— 绿不承流（down / weight=0 / 不在 upstream）
#   unknown —— 无法判定（conf 读不到、缺 dailycheck upstream、weight 值不可解析）
#
# unknown 一律**拒绝执行**（见主流程 2.5）：此刻重启绿可能直接打断在役实例，
# 静默放行等于把判断权交给运气。
_green_traffic_state() {
  local line lines state="idle" w
  if [[ ! -r "${NGINX_CONF}" ]]; then
    echo "unknown"
    return 0
  fi
  # 旧布局（location 直指 dailycheck-green upstream）：绿恒在承流，保守判定。
  # 该布局下 switch.sh 会以 exit 2 拒跑（Issue #32），所以守卫命中后会安全失败。
  if grep -qE 'proxy_pass[[:space:]]+http://dailycheck-green([[:space:];]|$)' "${NGINX_CONF}" 2>/dev/null; then
    echo "serving"
    return 0
  fi
  # 拿不到 app upstream 块 → 不是本套布局，无从判定
  if ! grep -qE '^[[:space:]]*upstream[[:space:]]+dailycheck[[:space:]]*\{' "${NGINX_CONF}" 2>/dev/null; then
    echo "unknown"
    return 0
  fi
  lines="$(grep -E '^[[:space:]]*server[[:space:]]+127\.0\.0\.1:'"${GREEN_PORT}"'([[:space:];]|$)' "${NGINX_CONF}" 2>/dev/null || true)"
  if [[ -z "${lines}" ]]; then
    echo "idle"
    return 0
  fi
  while IFS= read -r line; do
    [[ -z "${line}" ]] && continue
    # `down` → 该 server 不参与负载
    if [[ "${line}" =~ (^|[[:space:]])down([[:space:];]|$) ]]; then
      continue
    fi
    if [[ "${line}" =~ (^|[[:space:]])weight=[[:space:]]*([0-9]+) ]]; then
      w="${BASH_REMATCH[2]}"
      if [[ ${w} -gt 0 ]]; then
        state="serving"
      fi
    elif [[ "${line}" =~ (^|[[:space:]])weight= ]]; then
      # weight= 后面不是数字 → 解析不了就拒绝执行
      echo "unknown"
      return 0
    else
      # 无 weight 声明 → nginx 默认 weight=1（在承流）
      state="serving"
    fi
  done <<< "${lines}"
  echo "${state}"
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

# 2.5 活流量守卫（2026-10-17 生产事故）
#
# 时机刻意放在「改任何东西之前」：一旦开拷 DB / 重启 unit，绿的在役状态就已被
# 破坏，再想"先切蓝"已无处可退（蓝可能还在跑旧代码/旧库）。
#   serving → 探活蓝，健康则 switch.sh blue 100 把流量挪走，再继续重启绿；
#             蓝不健康 → 明确报错 exit 1，**绝不**重启在役的绿；
#   idle    → 绿不在承流（down / weight=0），行为与加固前一致；
#   unknown → 读不到 / 解析不了 conf → 拒绝执行（人工核对）。
if [[ ${DRY_RUN_FLAG} -eq 1 ]]; then
  echo "[up] [DRY-RUN] would resolve green 承流状态 from ${NGINX_CONF}"
  echo "[up] [DRY-RUN] would probe ${BLUE_HEALTH_URL} and run switch.sh blue 100 if green 在役"
else
  GREEN_TRAFFIC_STATE="$(_green_traffic_state)"
  echo "[up] green 承流状态: ${GREEN_TRAFFIC_STATE}（conf=${NGINX_CONF}）"
  case "${GREEN_TRAFFIC_STATE}" in
    serving)
      echo "[up] green 在役（nginx 正给 :${GREEN_PORT} 分流量）—— 先切蓝，避免重启打断在役实例"
      BLUE_CODE="$(_http_code "${BLUE_HEALTH_URL}")"
      if [[ "${BLUE_CODE}" != "200" ]]; then
        echo "[up] ERROR: 蓝 ${BLUE_HEALTH_URL} 不健康（HTTP ${BLUE_CODE:-无响应}）" >&2
        echo "[up]   —— 既不能切蓝，就不得重启在役的绿：本次 up.sh 中止，未做任何改动。" >&2
        echo "[up]   —— 处置：先恢复蓝（systemctl restart dailycheck）再重跑 up.sh。" >&2
        exit 1
      fi
      echo "[up] 蓝健康（${BLUE_HEALTH_URL} 200）→ switch.sh blue 100 把流量从绿挪走"
      if ! _run bash "${SWITCH_SCRIPT}" blue 100; then
        echo "[up] ERROR: switch.sh blue 100 失败 —— 绿仍在承流，禁止重启绿。" >&2
        echo "[up]   —— 处置：人工核对 switch.sh 输出（可能 nginx reload 失败），修复后重跑。" >&2
        exit 1
      fi
      echo "[up] 流量已切到蓝（green weight=0），可安全重启绿"
      ;;
    idle)
      echo "[up] green 未承流（down / weight=0 / 不在 upstream）—— 直接重启不打断线上"
      ;;
    *)
      echo "[up] ERROR: 无法判定绿是否在承接流量（conf=${NGINX_CONF}）" >&2
      echo "[up]   —— 读不到 conf / 缺 dailycheck upstream / weight 值不可解析。" >&2
      echo "[up]   —— 此刻重启绿可能直接打断在役实例，拒绝执行；请人工核对 nginx 布局。" >&2
      exit 1
      ;;
  esac
fi

# 3. cp 真库 → 绿独立副本（Issue #28）
#
# 旧版 `cp -a /var/lib/dailycheck → /var/lib/dailycheck-green` 是无效动作：
# 两边都是遗留目录，cp 把源拷进了目标内部，蓝绿实际共享同一物理库。
# 新版从真库 ${APP_DIR}/db 拷（-L 解引用 warehouses/*.db 软链），
# 且要求绿 unit 用 DAILYCHECK_DB_DIR 指向该副本（下方步骤 3.5 守卫）。
if [[ ${SKIP_DB_COPY} -eq 0 ]]; then
  echo "[up] copying real db tree ${SRC_DB_DIR} -> ${GREEN_DB} (dereference symlinks) ..."
  _run rm -rf "${GREEN_DB}"
  _run cp -aL "${SRC_DB_DIR}" "${GREEN_DB}"
  _run chmod -R u+rw "${GREEN_DB}"
else
  echo "[up] --skip-db-copy: using existing ${GREEN_DB}"
  if [[ ${DRY_RUN_FLAG} -eq 0 && ! -d "${GREEN_DB}" ]]; then
    echo "[up] ERROR: --skip-db-copy 但 ${GREEN_DB} 不存在" >&2
    exit 1
  fi
fi

# 3.5 隔离前置守卫（Issue #28）：绿 unit 必须声明 DAILYCHECK_DB_DIR
# 且等于本次拷贝目标——否则绿会共享蓝的物理库，隔离从未生效。
if [[ ${DRY_RUN_FLAG} -eq 0 ]]; then
  UNIT_DB_DIR="$(systemctl cat "${GREEN_UNIT}" 2>/dev/null \
    | grep -o 'DAILYCHECK_DB_DIR=[^ "]*' | head -1 | cut -d= -f2- || true)"
  if [[ -z "${UNIT_DB_DIR}" ]]; then
    echo "[up] ERROR: ${GREEN_UNIT} 未声明 DAILYCHECK_DB_DIR — 绿将共享蓝的物理库（Issue #28 隔离失效）" >&2
    echo "[up]   修复: unit [Service] 段加 Environment=DAILYCHECK_DB_DIR=${GREEN_DB}，然后 systemctl daemon-reload" >&2
    exit 1
  fi
  if [[ "${UNIT_DB_DIR}" != "${GREEN_DB}" ]]; then
    echo "[up] ERROR: unit DAILYCHECK_DB_DIR=${UNIT_DB_DIR} != 本次拷贝目标 ${GREEN_DB} — 绿会读到旧副本" >&2
    exit 1
  fi
  echo "[up] ✅ 绿 unit DB 隔离声明确认: DAILYCHECK_DB_DIR=${UNIT_DB_DIR}"
else
  echo "[up] [DRY-RUN] would verify ${GREEN_UNIT} declares DAILYCHECK_DB_DIR=${GREEN_DB}"
fi

# 4. 起绿 systemd unit
#
# Issue #38 框架 gap：原来只 `start`，而 `systemctl start` 对**已在运行**的 unit
# 是 no-op —— 绿进程继续跑旧代码，却已经把新 DB 拷进来了（「新库 + 旧码」），
# 之后 verify / switch 全在一半新的环境上做判定。`restart` 语义 = 未跑则起、
# 在跑则重启，正是本步骤要的。
#
# 刻意 `|| true` 而不让 `set -e` 直退：restart 失败（unit 不存在 / 启动即崩）
# 交给步骤 5/6 健康检查判失败，再由步骤 7 落 journalctl + DB 取证——`set -e`
# 当场退出会跳过取证，而「没有现场的失败」正是本次事故排查时最缺的东西。
if [[ ${DRY_RUN_FLAG} -eq 0 ]]; then
  PRE_GREEN_STATE="$(systemctl is-active "${GREEN_UNIT}" 2>/dev/null || true)"
  echo "[up] ${GREEN_UNIT} pre-state: ${PRE_GREEN_STATE:-unknown}（在跑 → 必须 restart，否则留旧代码）"
else
  PRE_GREEN_STATE="unknown"
  echo "[up] [DRY-RUN] would query systemctl is-active ${GREEN_UNIT}"
fi
echo "[up] restarting ${GREEN_UNIT} on :${GREEN_PORT} ..."
_run systemctl restart "${GREEN_UNIT}" || true
echo "[up] restarting ${GREEN_MCP_UNIT} on :${GREEN_MCP_PORT} ..."
_run systemctl restart "${GREEN_MCP_UNIT}" || true

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
