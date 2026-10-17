#!/usr/bin/env bash
# blue_green/promote.sh —— 绿转正（关写隔离 + DB 指回真库 + 重启）
#
# 用法: ./scripts/blue_green/promote.sh [--dry-run]
#
# 背景（2026-10-17 生产事故）：
#   绿实例以 DRY_RUN=true 启动是为了验证期写隔离（db/dryrun.py 把写操作拦成
#   log-only：HTTP 200、审计照记、库不落盘）。该状态**绝不能承接生产流量**。
#   转正是「关 DRY_RUN + DB 指回真库 + 重启」三步人工操作，此前只写在 README
#   里靠人记 —— 漏做一次就是全站写「假成功」。本脚本把这三步固化成可重放的
#   单一命令，并自带锁门禁、备份、失败回滚、健康复核、幂等。
#
# 语义对齐 README「绿转正（承接生产流量前必做）」段：
#   1. 备份 dailycheck-green.service / dailycheck-green-mcp.service（存在
#      EnvironmentFile 时一并备份），落到 BACKUP_DIR（默认
#      /var/lib/dailycheck-blue-green-backups，与 switch.sh 同目录，**不落**
#      /etc/systemd/system —— 目录里多出的 *.bak 不会被 systemd 加载，但仍
#      按 switch.sh 的既有约定统一存放）；
#   2. app unit 的 Environment=DRY_RUN=true 改为 false（**无该行则不加**：
#      DRY_RUN 本来就没开，视为已转正）；
#   3. 两个 unit（及存在的 env 文件）的 Environment=DAILYCHECK_DB_DIR=... 改为
#      ${APP_DIR}/db —— 验证期的独立副本 /var/lib/dailycheck-green/db 不再使用；
#   4. systemctl daemon-reload → restart dailycheck-green dailycheck-green-mcp；
#   5. 轮询健康：8091 /health 必须 200；5101 /health 返回 200 或 401 均视为活
#      （MCP 带 token 鉴权，活着的实例对无凭据探针返 401）；
#   6. 终局复核：`systemctl show -p Environment` 仍报 DRY_RUN=true（drop-in 覆盖）
#      → 明确告警（不自动回滚，restart 已完成；switch.sh 的 DRY_RUN 守卫会拒切）。
#
# 幂等：已转正（无 DRY_RUN=true 且 DB 已指向 ${APP_DIR}/db）→ 打印
#   `already promoted` 并 exit 0，**不**重复 restart（重启在役实例是事故本身）。
#
# 失败：任一步失败 → 用备份恢复被改的文件 + daemon-reload + 明确错误信息，exit 1。
#
# 退出码:
#   0   成功（已转正 / 本次转正完成且健康复核通过）
#   1   失败（文件缺失 / 改写未命中 / daemon-reload 失败 / restart 失败 / 健康超时；
#       已自动恢复备份）
#   10  无活跃部署锁（会话级单飞守卫拒绝；人工受控操作可设
#       DAILYCHECK_DEPLOY_UNLOCKED=1 跳过）
#   64  参数错误
#
# 可测试性（全部 env 可覆盖，默认值为生产值）：
#   GREEN_UNIT_PATH / GREEN_MCP_UNIT_PATH   unit 文件路径
#   GREEN_ENV_FILE / GREEN_MCP_ENV_FILE     env 文件路径（不存在则跳过）
#   SYSTEMD_UNIT_DIR                        unit 目录（未显式给 *_UNIT_PATH 时生效）
#   DAILYCHECK_APP_DIR                      真库所在工作目录（DB 指向 ${APP_DIR}/db）
#   SYSTEMCTL_BIN / CURL_BIN                命令二进制（单测前置 fake 脚本）
#   BACKUP_DIR                              备份目录
#   GREEN_HEALTH_URL / GREEN_MCP_HEALTH_URL / HEALTH_TRIES / HEALTH_INTERVAL
#   NGINX_CONF                              nginx 站点 conf（仅用于日志提示，不改写）
#   DAILYCHECK_DEPLOY_STATE_DIR / DAILYCHECK_BG_LOCK_FILE / DAILYCHECK_BG_LOCK_HELD
#
# 刻意不用 set -e：本脚本的失败路径需要「先恢复备份再退出」，而 set -e 会在
# 第一个失败处直接跳出、跳过恢复。所有失败都显式 `_fail_and_restore`。
set -uo pipefail

# ============================================================
# 参数解析
# ============================================================
for arg in "$@"; do
  if [[ "$arg" == "-h" || "$arg" == "--help" ]]; then
    echo "usage: $0 [--dry-run]"
    echo "  --dry-run  只打印将要做的事（不写文件、不调 systemctl / curl）"
    echo "  exit: 0=成功/已转正 1=失败（已回滚） 10=无部署锁 64=参数错"
    exit 0
  fi
done

DRY_RUN_FLAG=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN_FLAG=1 ;;
    *) echo "unknown arg: $1" >&2; exit 64 ;;
  esac
  shift
done

# ============================================================
# 配置（与 up.sh / verify.sh 的 DAILYCHECK_* / GREEN_* 约定对齐）
# ============================================================
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="${DAILYCHECK_APP_DIR:-/root/DailyCheck}"
TARGET_DB_DIR="${APP_DIR}/db"
SYSTEMD_UNIT_DIR="${SYSTEMD_UNIT_DIR:-/etc/systemd/system}"
GREEN_UNIT_NAME="${GREEN_SERVICE_UNIT:-dailycheck-green.service}"
GREEN_MCP_UNIT_NAME="${GREEN_MCP_SERVICE_UNIT:-dailycheck-green-mcp.service}"
GREEN_UNIT_FILE="${GREEN_UNIT_PATH:-${SYSTEMD_UNIT_DIR}/${GREEN_UNIT_NAME}}"
GREEN_MCP_UNIT_FILE="${GREEN_MCP_UNIT_PATH:-${SYSTEMD_UNIT_DIR}/${GREEN_MCP_UNIT_NAME}}"
GREEN_ENV_FILE="${GREEN_ENV_FILE:-/etc/wdg/dailycheck-green.env}"
GREEN_MCP_ENV_FILE="${GREEN_MCP_ENV_FILE:-/etc/wdg/dailycheck-green-mcp.env}"
BACKUP_DIR="${BACKUP_DIR:-/var/lib/dailycheck-blue-green-backups}"
NGINX_CONF="${NGINX_CONF:-${NGINX_DAILYCHECK_CONF:-/etc/nginx/sites-enabled/dailycheck}}"
GREEN_HEALTH_URL="${GREEN_HEALTH_URL:-http://127.0.0.1:8091/health}"
GREEN_MCP_HEALTH_URL="${GREEN_MCP_HEALTH_URL:-http://127.0.0.1:5101/health}"
HEALTH_TRIES="${HEALTH_TRIES:-30}"
HEALTH_INTERVAL="${HEALTH_INTERVAL:-2}"
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-systemctl}"
CURL_BIN="${CURL_BIN:-curl}"

_TS="$(date -u +%Y%m%dT%H%M%SZ)"
_MANIFEST="${BACKUP_DIR}/promote-${_TS}.manifest"

# 改写规则（POSIX ERE，BSD sed -E / GNU sed -E 通用）：
#   - 前缀允许 行首 / 空白 / `=` / `"` —— unit 里是 `Environment=DRY_RUN=true`，
#     也可能写成 `Environment="DAILYCHECK_DB_DIR=/x"` 这种带引号形态；
#   - 值支持两种形态：裸值 `[^[:space:]"]*` 与引号包裹 `"[^"]*"`（**必须**作为
#     一个整体匹配掉：只写裸值形态时，`DAILYCHECK_DB_DIR="/x"` 会在 `"` 前命中
#     零长度 → 插进新路径却留下旧值，实测产出
#     `DAILYCHECK_DB_DIR=/root/DailyCheck/db"/var/lib/dailycheck-green/db"`）；
#   - 分隔符用 `#` 而非 `|`：BSD sed 会把 `s|...|...|` 里未转义的 `|` 当分隔符切段
#     （实测报 "bad regex '(^': parentheses not balanced"），而 ERE 里恰恰要用 `|`。
_SED_DRYRUN_QUOTED='s#(^|[[:space:]=])DRY_RUN="(true|True|TRUE|1|yes|Yes|YES)"#\1DRY_RUN="false"#'
_SED_DRYRUN='s#(^|[[:space:]="])DRY_RUN=(true|True|TRUE|1|yes|Yes|YES)#\1DRY_RUN=false#'
_SED_DBDIR="s#(^|[[:space:]=\"])DAILYCHECK_DB_DIR=(\"[^\"]*\"|[^[:space:]\"]*)#\1DAILYCHECK_DB_DIR=${TARGET_DB_DIR}#"

# ============================================================
# 辅助：dry-run
# ============================================================
_run() {
  if [[ ${DRY_RUN_FLAG} -eq 1 ]]; then
    echo "[DRY-RUN] would execute: $*"
  else
    "$@"
  fi
}

# ============================================================
# 辅助：读取（全部走 awk，不落 pipefail 的 SIGPIPE 陷阱）
# ============================================================
# 是否含生效的 DRY_RUN=true / 1 / yes（整行注释不算：注释掉的 DRY_RUN 不生效）
_file_has_dryrun_true() {
  awk '
    { line = $0; sub(/^[[:space:]]*#.*/, "", line) }
    tolower(line) ~ /(^|[[:space:]="])dry_run="?(true|1|yes)"?([[:space:]"]|$)/ { found = 1 }
    END { exit(found ? 0 : 1) }
  ' "$1"
}

# 打印文件里所有生效的 DAILYCHECK_DB_DIR 取值（一行一个，去引号）
_dbdir_values() {
  awk '
    { line = $0; sub(/^[[:space:]]*#.*/, "", line) }
    {
      while (match(line, /DAILYCHECK_DB_DIR=("[^"]*"|[^[:space:]"]*)/)) {
        v = substr(line, RSTART, RLENGTH)
        sub(/^DAILYCHECK_DB_DIR=/, "", v)
        gsub(/"/, "", v)
        print v
        line = substr(line, RSTART + RLENGTH)
      }
    }
  ' "$1"
}

# 需要转正？（0 = 需要；1 = 已转正）
_needs_promotion() {
  local f vals v
  for f in "${GREEN_UNIT_FILE}" "${GREEN_MCP_UNIT_FILE}" "${GREEN_ENV_FILE}" "${GREEN_MCP_ENV_FILE}"; do
    [[ -f "$f" ]] || continue
    if _file_has_dryrun_true "$f"; then
      echo "[promote] 待转正：$(basename "$f") 仍含 DRY_RUN=true" >&2
      return 0
    fi
    vals="$(_dbdir_values "$f")"
    while IFS= read -r v; do
      [[ -z "$v" ]] && continue
      if [[ "$v" != "${TARGET_DB_DIR}" ]]; then
        echo "[promote] 待转正：$(basename "$f") DAILYCHECK_DB_DIR=${v} != ${TARGET_DB_DIR}" >&2
        return 0
      fi
    done <<< "${vals}"
  done
  return 1
}

# ============================================================
# 辅助：备份 / 恢复 / 改写
# ============================================================
_backup_file() {
  local src="$1" base dst
  base="$(basename "$src")"
  dst="${BACKUP_DIR}/${base}.pre-promote.${_TS}.bak"
  if [[ ${DRY_RUN_FLAG} -eq 1 ]]; then
    echo "[DRY-RUN] would cp -a ${src} ${dst}"
    return 0
  fi
  if ! mkdir -p "${BACKUP_DIR}"; then
    echo "[promote] ERROR: 备份目录不可创建: ${BACKUP_DIR}" >&2
    return 1
  fi
  if ! cp -a "$src" "$dst"; then
    echo "[promote] ERROR: 备份失败: ${src} → ${dst}" >&2
    return 1
  fi
  if ! printf '%s\t%s\n' "$src" "$dst" >> "${_MANIFEST}"; then
    echo "[promote] ERROR: 备份清单写入失败: ${_MANIFEST}" >&2
    return 1
  fi
  echo "[promote] 已备份 ${src} → ${dst}"
  return 0
}

_restore_all() {
  [[ -f "${_MANIFEST}" ]] || return 0
  local src bak rc=0
  echo "[promote] --- 回滚：按备份清单恢复 ---" >&2
  while IFS=$'\t' read -r src bak; do
    [[ -z "${src}" ]] && continue
    if cp -a "${bak}" "${src}"; then
      echo "[promote] 已恢复 ${src} ← ${bak}" >&2
    else
      echo "[promote] ERROR: 恢复失败 ${src} ← ${bak}（需人工介入）" >&2
      rc=1
    fi
  done < "${_MANIFEST}"
  return ${rc}
}

# 改写文件（sed → 临时文件 → mv；**不用 sed -i**：GNU 与 BSD 的 -i 参数形态
# 不同（macOS 需要 `sed -i ''`），临时文件法两边一致，且 mv 是原子的。
_rewrite_in_place() {
  local f="$1" tmp
  tmp="${f}.promote-tmp.$$"
  if ! sed -E -e "${_SED_DRYRUN_QUOTED}" -e "${_SED_DRYRUN}" -e "${_SED_DBDIR}" "${f}" > "${tmp}"; then
    echo "[promote] ERROR: sed 改写失败: ${f}" >&2
    rm -f "${tmp}"
    return 1
  fi
  if ! mv "${tmp}" "${f}"; then
    echo "[promote] ERROR: 改写结果落回失败: ${f}" >&2
    rm -f "${tmp}"
    return 1
  fi
  return 0
}

# ============================================================
# 辅助：健康探活（curl 可替换，便于用 fake 命令做单测）
# ============================================================
_http_code() {
  local url="$1"
  "${CURL_BIN}" -s -o /dev/null -w '%{http_code}' --max-time 5 "${url}" 2>/dev/null || true
}

# _wait_healthy <label> <url> <accepted-codes-regex>
_wait_healthy() {
  local label="$1" url="$2" accept="$3" i=0 code=""
  while [[ ${i} -lt ${HEALTH_TRIES} ]]; do
    i=$((i + 1))
    code="$(_http_code "${url}")"
    if [[ -n "${code}" ]] && printf '%s\n' "${code}" | grep -qE "^(${accept})$"; then
      echo "[promote] ${label} healthy after ${i}×${HEALTH_INTERVAL}s（HTTP ${code}）"
      return 0
    fi
    echo "[promote] ${label} 探活 ${i}/${HEALTH_TRIES}: HTTP ${code:-无响应}（${url}）"
    if [[ ${i} -lt ${HEALTH_TRIES} ]]; then
      sleep "${HEALTH_INTERVAL}"
    fi
  done
  return 1
}

# ============================================================
# 辅助：失败收口（恢复备份 + daemon-reload + exit 1）
# ============================================================
_fail_and_restore() {
  echo "[promote] ERROR: $1" >&2
  _restore_all || true
  if [[ ${DRY_RUN_FLAG} -eq 0 ]]; then
    echo "[promote] 恢复后 daemon-reload（让 systemd 读回旧 unit）" >&2
    "${SYSTEMCTL_BIN}" daemon-reload \
      || echo "[promote] WARN: 恢复后 daemon-reload 失败，需人工执行 systemctl daemon-reload" >&2
  fi
  echo "[promote] 转正失败，环境已回滚到转正前状态；绿仍在写隔离态，禁止切流量。" >&2
  exit 1
}

# ============================================================
# 部署单飞守卫 + 进程级互斥（与 up.sh / verify.sh / switch.sh 同款）
# ============================================================
# 转正会 restart 生产实例，绝不能与另一个部署会话重叠；故沿用同一套门禁：
#   - dry-run 完全跳过（演练/CI 行为不变）
#   - 状态目录不存在 → 单飞机制未激活，放行（向后兼容窗口）
#   - DAILYCHECK_DEPLOY_UNLOCKED=1 → 人工逃生舱（警告放行）
#   - 有活跃锁 → 放行（记审计行）；无活跃锁 → exit 10
# 注：promote 不是 PR 粒度操作，故 `check` 不带 --pr（与 switch.sh 一致）。
if [[ ${DRY_RUN_FLAG} -eq 0 ]]; then
  _DS_DIR="${DAILYCHECK_DEPLOY_STATE_DIR:-/var/lib/dailycheck-deploy}"
  if [[ "${DAILYCHECK_DEPLOY_UNLOCKED:-0}" == "1" ]]; then
    echo "[promote] WARN: DAILYCHECK_DEPLOY_UNLOCKED=1 —— 跳过部署锁校验（人工操作）" >&2
  elif [[ ! -d "${_DS_DIR}" ]]; then
    echo "[promote] [deploy-lock] 状态目录 ${_DS_DIR} 不存在（单飞机制未激活）—— 跳过校验"
  elif [[ ! -f "${SCRIPT_DIR}/deploy_state.py" ]] || ! command -v python3 >/dev/null 2>&1; then
    echo "[promote] WARN: deploy_state.py / python3 不可用 —— 跳过部署锁校验" >&2
  else
    _ds_rc=0
    _ds_out="$(python3 "${SCRIPT_DIR}/deploy_state.py" check 2>&1)" || _ds_rc=$?
    if [[ ${_ds_rc} -eq 10 ]]; then
      if ! { printf '%s promote.sh %s\n' "$(date -u +%FT%TZ)" "${_ds_out}"; } \
             >> "${_DS_DIR}/audit.log" 2>/dev/null; then
        echo "[promote] WARN: audit.log 写入失败（不影响转正流程）" >&2
      fi
    elif [[ ${_ds_rc} -eq 0 ]]; then
      echo "[promote] ERROR: 单飞机制已激活但无活跃部署锁 —— 拒绝转正（exit 10）。" >&2
      echo "[promote] 请先由部署会话执行 deploy_state.py acquire；人工受控操作可设 DAILYCHECK_DEPLOY_UNLOCKED=1。" >&2
      exit 10
    elif [[ ${_ds_rc} -eq 12 ]]; then
      echo "[promote] ERROR: 部署锁属于另一个 PR（${_ds_out}）—— 拒绝并发转正" >&2
      exit 10
    else
      echo "[promote] WARN: 部署锁检查异常 rc=${_ds_rc}（${_ds_out}）—— 跳过校验" >&2
    fi
  fi

  if [[ "${DAILYCHECK_BG_LOCK_HELD:-0}" != "1" ]]; then
    _BG_LOCK_FILE="${DAILYCHECK_BG_LOCK_FILE:-/var/lock/dailycheck-bluegreen.lock}"
    if ! command -v flock >/dev/null 2>&1; then
      echo "[promote] WARN: flock 不可用 —— 进程级互斥跳过（会话级锁仍生效）" >&2
    elif ! mkdir -p "$(dirname "${_BG_LOCK_FILE}")" 2>/dev/null || ! : >>"${_BG_LOCK_FILE}" 2>/dev/null; then
      echo "[promote] WARN: 锁文件不可写（${_BG_LOCK_FILE}）—— 进程级互斥跳过" >&2
    else
      exec 9>"${_BG_LOCK_FILE}"
      if ! flock -n 9; then
        echo "[promote] ERROR: 另一蓝绿操作正在进行（${_BG_LOCK_FILE} 被占）—— 拒绝并发" >&2
        exit 10
      fi
      export DAILYCHECK_BG_LOCK_HELD=1
    fi
  fi
fi

# ============================================================
# 主流程
# ============================================================
echo "[promote] === 绿转正（关写隔离 + DB 指回真库 + 重启） ==="
echo "[promote] mode: $([[ ${DRY_RUN_FLAG} -eq 1 ]] && echo 'dry-run' || echo 'live')"
echo "[promote] app unit:  ${GREEN_UNIT_FILE}"
echo "[promote] mcp unit:  ${GREEN_MCP_UNIT_FILE}"
echo "[promote] app env:   ${GREEN_ENV_FILE}$([[ -f ${GREEN_ENV_FILE} ]] || echo '（不存在，跳过）')"
echo "[promote] mcp env:   ${GREEN_MCP_ENV_FILE}$([[ -f ${GREEN_MCP_ENV_FILE} ]] || echo '（不存在，跳过）')"
echo "[promote] 目标真库:   ${TARGET_DB_DIR}"
echo "[promote] nginx conf: ${NGINX_CONF}（本脚本不改写，仅提示）"

# 1. 前置：app unit 必须存在（否则无从改起）；mcp unit / env 文件可选
if [[ ! -f "${GREEN_UNIT_FILE}" ]]; then
  echo "[promote] ERROR: app unit 不存在: ${GREEN_UNIT_FILE}" >&2
  echo "[promote]   可用 GREEN_UNIT_PATH 覆盖路径；绿 unit 未建时无需转正。" >&2
  exit 1
fi
if [[ ! -f "${GREEN_MCP_UNIT_FILE}" ]]; then
  echo "[promote] WARN: mcp unit 不存在（${GREEN_MCP_UNIT_FILE}）—— 跳过该 unit" >&2
fi

# 2. 幂等：已转正直接退出，绝不重启在役实例
if ! _needs_promotion; then
  echo "[promote] already promoted —— 绿已是真实写能力（DRY_RUN=false，DB=${TARGET_DB_DIR}），不重复重启。"
  exit 0
fi

# 3. 备份
for _f in "${GREEN_UNIT_FILE}" "${GREEN_MCP_UNIT_FILE}" "${GREEN_ENV_FILE}" "${GREEN_MCP_ENV_FILE}"; do
  [[ -f "${_f}" ]] || continue
  _backup_file "${_f}" || _fail_and_restore "备份失败：${_f}"
done

# 4. 改写 DRY_RUN / DAILYCHECK_DB_DIR
for _f in "${GREEN_UNIT_FILE}" "${GREEN_MCP_UNIT_FILE}" "${GREEN_ENV_FILE}" "${GREEN_MCP_ENV_FILE}"; do
  [[ -f "${_f}" ]] || continue
  if [[ ${DRY_RUN_FLAG} -eq 1 ]]; then
    echo "[DRY-RUN] would rewrite ${_f}: DRY_RUN=true→false；DAILYCHECK_DB_DIR=* → ${TARGET_DB_DIR}"
  else
    _rewrite_in_place "${_f}" || _fail_and_restore "改写失败：${_f}"
    echo "[promote] 已改写 ${_f}"
  fi
done

# 5. 改写核验（sed 未命中 = 静默失败，必须显式拦下；这正是事故的形态）
if [[ ${DRY_RUN_FLAG} -eq 0 ]]; then
  if _needs_promotion; then
    _fail_and_restore "改写后仍检测到未转正项（sed 未命中或还有别处覆盖 DRY_RUN / DAILYCHECK_DB_DIR）"
  fi
  echo "[promote] 改写核验通过：无 DRY_RUN=true，DB 全部指向 ${TARGET_DB_DIR}"
else
  echo "[DRY-RUN] would re-verify: 无 DRY_RUN=true 且 DAILYCHECK_DB_DIR 全部为 ${TARGET_DB_DIR}"
fi

# 6. daemon-reload
echo "[promote] daemon-reload ..."
if ! _run "${SYSTEMCTL_BIN}" daemon-reload; then
  _fail_and_restore "systemctl daemon-reload 失败"
fi

# 7. 重启两个绿 unit（一次命令，任一失败即整体失败）
echo "[promote] restart ${GREEN_UNIT_NAME} ${GREEN_MCP_UNIT_NAME} ..."
if ! _run "${SYSTEMCTL_BIN}" restart "${GREEN_UNIT_NAME}" "${GREEN_MCP_UNIT_NAME}"; then
  _fail_and_restore "systemctl restart ${GREEN_UNIT_NAME} ${GREEN_MCP_UNIT_NAME} 失败"
fi

# 8. 健康复核：8091 必须 200；5101 返 200 或 401 均视为活
if [[ ${DRY_RUN_FLAG} -eq 1 ]]; then
  echo "[DRY-RUN] would poll ${GREEN_HEALTH_URL}（期望 200）与 ${GREEN_MCP_HEALTH_URL}（期望 200/401）"
else
  if ! _wait_healthy "Flask :8091" "${GREEN_HEALTH_URL}" "200"; then
    _fail_and_restore "Flask 健康检查超时（${GREEN_HEALTH_URL} 连续 ${HEALTH_TRIES} 次未返 200）"
  fi
  if ! _wait_healthy "MCP :5101" "${GREEN_MCP_HEALTH_URL}" "200|401"; then
    _fail_and_restore "MCP 健康检查超时（${GREEN_MCP_HEALTH_URL} 连续 ${HEALTH_TRIES} 次未返 200/401）"
  fi

  # 9. 终局复核（取证强度最高的那一层）：systemd 视角的有效环境里不得再有
  #    DRY_RUN=true。本脚本只改写 GREEN_UNIT_FILE / GREEN_MCP_UNIT_FILE /
  #    GREEN_ENV_FILE；若生产另挂了 drop-in（`*.service.d/*.conf`）把 DRY_RUN
  #    设回 true，上面基于文件的核验看不出来。这里只**告警不自动回滚**——
  #    restart 已完成，回滚会再制造一个不确定态；而真正防线的 switch.sh
  #    DRY_RUN 守卫（exit 3）会拦下切换，不会出现「假成功」。告警的目的是让
  #    人先修 drop-in，而不是让脚本猜。
  _env_from_systemd="$("${SYSTEMCTL_BIN}" show -p Environment --value "${GREEN_UNIT_NAME}" 2>/dev/null || true)"
  if [[ -z "${_env_from_systemd}" ]]; then
    echo "[promote] WARN: 读不到 systemd 侧的 Environment（${GREEN_UNIT_NAME}）—— 取证跳过；建议人工复核"
  elif printf '%s\n' "${_env_from_systemd}" \
       | grep -qiE '(^|[[:space:]="])DRY_RUN=(true|1|yes)'; then
    echo "[promote] WARN: systemd 视角 ${GREEN_UNIT_NAME} 仍报 DRY_RUN=true —— 存在本脚本未覆盖的覆盖源" >&2
    echo "[promote] WARN:   （典型：/etc/systemd/system/${GREEN_UNIT_NAME}.d/*.conf drop-in 或另一个 EnvironmentFile）" >&2
    echo "[promote] WARN:   处置：清掉该覆盖源后重跑 promote.sh；confirm 之前**禁止** switch.sh green。" >&2
    echo "[promote] WARN:   （switch.sh 的 DRY_RUN 守卫会以 exit 3 拒切，写操作不会被静默吞掉）" >&2
  else
    echo "[promote] systemd 侧复核通过：${GREEN_UNIT_NAME} 的有效环境不含 DRY_RUN=true"
  fi
fi

echo "[promote] 绿已转正（DRY_RUN=false，DB=${TARGET_DB_DIR}）。"
echo "[promote] 下一步：switch.sh green 5 → 50 → 100（灰度观察窗）；回退 switch.sh blue 100。"
exit 0
