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
#   - **静息态复位（步骤 3.0，Issue #28 闭环）**：`promote.sh` 转正会把绿 unit
#     改成「真实态」（DRY_RUN=false + DB 指回真库）并重启；故每一轮 up 都必须把
#     unit 复位成「验证态」——app unit `DRY_RUN=true`（写隔离）+ 两个 unit
#     `DAILYCHECK_DB_DIR=${GREEN_DB}`（副本库隔离）。缺这一步，第二轮 up 会在
#     3.5 守卫处 exit 1（fail-closed 停摆）；更糟的是「静默不复位」会让绿以真实
#     写能力跑副本库（写进将被丢弃的库）。
#     安全边界（三条，顺序不可调换）：
#       · 复位只在**流量已离开绿之后**执行 —— 由 2.5 保证（绿在役先
#         `switch.sh blue 100`；蓝不健康则 2.5 已 exit 1，根本到不了这里）；
#       · 复位 → daemon-reload → 3.5 断言 → restart（步骤 4）之间不插入别的动作：
#         「文件说 DRY_RUN=true、进程仍是转正态」的窗口越短越好；
#       · **绝不**在 `switch.sh green` 成功之后（绿已在承流）把 unit 改回
#         `DRY_RUN=true` —— 那是「文件 ≠ 运行态」：下一次任何 `systemctl
#         restart` 都会把在役实例悄悄变成写隔离。复位只在本脚本开头做，这是
#         唯一入口（promote.sh 是反向的唯一入口）。
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
# 静息态复位配置（步骤 3.0；与 promote.sh / switch.sh 同约定，全部可 env 覆盖）
# ============================================================
# promote.sh 改的是同一批文件，两边默认值必须逐字一致，否则「转正 → 下一轮 up」
# 会各自改不同的文件。
SYSTEMD_UNIT_DIR="${SYSTEMD_UNIT_DIR:-/etc/systemd/system}"
GREEN_UNIT_PATH="${GREEN_UNIT_PATH:-${SYSTEMD_UNIT_DIR}/${GREEN_UNIT}}"
GREEN_MCP_UNIT_PATH="${GREEN_MCP_UNIT_PATH:-${SYSTEMD_UNIT_DIR}/${GREEN_MCP_UNIT}}"
GREEN_ENV_FILE="${GREEN_ENV_FILE:-/etc/wdg/dailycheck-green.env}"
GREEN_MCP_ENV_FILE="${GREEN_MCP_ENV_FILE:-/etc/wdg/dailycheck-green-mcp.env}"
BACKUP_DIR="${BACKUP_DIR:-/var/lib/dailycheck-blue-green-backups}"

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
# 辅助：静息态（resting）判定 / 备份 / 改写 / systemd 视角复核
# ============================================================
# 「静息态」= 绿 unit 处于可验证态：app unit `DRY_RUN=true`（写隔离）+ 两个 unit
# 的 `DAILYCHECK_DB_DIR` 都指向 `${GREEN_DB}`（副本库隔离）。这是 promote.sh
# 转正前的状态，也是每轮 up.sh 必须复位的目标状态。

# 打印文件里某键在**生效行**上的全部取值（整行注释不算生效；一行一个，去引号）
_effective_values() {
  local f="$1" key="$2"
  [[ -f "${f}" ]] || return 0
  awk -v key="${key}" '
    {
      line = $0
      sub(/^[[:space:]]*#.*/, "", line)          # 整行注释 = 不生效
      pat = "(^|[[:space:]=\"])" key "=(\"[^\"]*\"|[^[:space:]\"]*)"
      while (match(line, pat)) {
        v = substr(line, RSTART, RLENGTH)
        i = index(v, key "=")
        v = substr(v, i + length(key) + 1)
        gsub(/"/, "", v)
        print v
        line = substr(line, RSTART + RLENGTH)
      }
    }
  ' "${f}" 2>/dev/null || true
}

# DRY_RUN 生效值是否**全为真**且至少一条
_dryrun_resting() {
  local f="$1" v found=0 vals
  vals="$(_effective_values "${f}" DRY_RUN)"
  while IFS= read -r v; do
    [[ -z "${v}" ]] && continue
    found=1
    case "$(printf '%s' "${v}" | tr '[:upper:]' '[:lower:]')" in
      true|1|yes) ;;
      *) return 1 ;;
    esac
  done <<< "${vals}"
  [[ ${found} -eq 1 ]]
}

# DAILYCHECK_DB_DIR 生效值是否全为 ${GREEN_DB}
#   $2=require 1 → 必须存在该行（unit 必须显式声明；缺失即隔离从未生效）
#   $2=require 0 → 可缺省（env 文件不写该键 = 不覆盖，允许）
_db_ok() {
  local f="$1" require="$2" v count=0 vals
  vals="$(_effective_values "${f}" DAILYCHECK_DB_DIR)"
  while IFS= read -r v; do
    [[ -z "${v}" ]] && continue
    count=$((count + 1))
    [[ "${v}" == "${GREEN_DB}" ]] || return 1
  done <<< "${vals}"
  if [[ "${require}" == "1" && ${count} -eq 0 ]]; then
    return 1
  fi
  return 0
}

# 整组文件是否已处于静息态（0 = 是）
_resting_ok() {
  local f
  _dryrun_resting "${GREEN_UNIT_PATH}" || return 1
  for f in "${GREEN_UNIT_PATH}" "${GREEN_MCP_UNIT_PATH}"; do
    [[ -f "${f}" ]] || continue
    _db_ok "${f}" 1 || return 1
  done
  for f in "${GREEN_ENV_FILE}" "${GREEN_MCP_ENV_FILE}"; do
    [[ -f "${f}" ]] || continue
    _db_ok "${f}" 0 || return 1
    # systemd 语义：EnvironmentFile 的取值**覆盖** unit 的 Environment=。env 文件
    # 里写着 DRY_RUN=false 时，只在 unit 里置 true 等于没置。
    if [[ "${f}" == "${GREEN_ENV_FILE}" && -n "$(_effective_values "${f}" DRY_RUN)" ]]; then
      _dryrun_resting "${f}" || return 1
    fi
  done
  return 0
}

_TS="$(date -u +%Y%m%dT%H%M%SZ)"
_UP_MANIFEST="${BACKUP_DIR}/up-resting-${_TS}.manifest"

_backup_file() {
  local src="$1" dst
  dst="${BACKUP_DIR}/$(basename "${src}").pre-up.${_TS}.bak"
  if ! mkdir -p "${BACKUP_DIR}"; then
    echo "[up] ERROR: 备份目录不可创建: ${BACKUP_DIR}" >&2
    return 1
  fi
  if ! cp -a "${src}" "${dst}"; then
    echo "[up] ERROR: 备份失败: ${src} → ${dst}" >&2
    return 1
  fi
  if ! printf '%s\t%s\n' "${src}" "${dst}" >> "${_UP_MANIFEST}"; then
    echo "[up] ERROR: 备份清单写入失败: ${_UP_MANIFEST}" >&2
    return 1
  fi
  echo "[up] 已备份 ${src} → ${dst}"
  return 0
}

_restore_all() {
  [[ -f "${_UP_MANIFEST}" ]] || return 0
  local src bak rc=0
  echo "[up] --- 回滚：按备份清单恢复复位前的 unit ---" >&2
  while IFS=$'\t' read -r src bak; do
    [[ -z "${src}" ]] && continue
    if cp -a "${bak}" "${src}"; then
      echo "[up] 已恢复 ${src} ← ${bak}" >&2
    else
      echo "[up] ERROR: 恢复失败 ${src} ← ${bak}（需人工介入）" >&2
      rc=1
    fi
  done < "${_UP_MANIFEST}"
  return ${rc}
}

# 复位失败收口：恢复备份 + daemon-reload + exit 1
# 绝不「改了一半就继续 restart」——半个静息态 = 绿跑在「真实写能力 + 副本库」上。
_fail_restore() {
  echo "[up] ERROR: $1" >&2
  _restore_all || true
  systemctl daemon-reload \
    || echo "[up] WARN: 恢复后 daemon-reload 失败，需人工执行 systemctl daemon-reload" >&2
  echo "[up] 静息态复位未完成：unit 已回滚到本次改动前状态；绿**未**重启，流量仍在蓝。" >&2
  echo "[up] 处置：按上面的原因修好后重跑 up.sh（复位是幂等的，重跑安全）。" >&2
  exit 1
}

# 改写 unit / env 文件为静息态取值（原子写：awk → 临时文件 → mv）
#
# 参数：文件 / rewrite_dry（1=改写 DRY_RUN，0=不动）/ insert_dry（1=缺行则插入）/
#       insert_db（1=缺行则插入）。两类文件语义不同：
#   app unit  = 1 1 1（DRY_RUN 必须有且为 true，DB 必须指向副本库）
#   mcp unit  = 0 0 1（MCP 不看 DRY_RUN，但 DB 必须指向副本库）
#   env 文件  = 1 0 0（有则改（EnvironmentFile 覆盖 unit），无则不发明）
#
# 为什么用 awk 而不是 promote.sh 那套 sed：
#   1. 除「改值」外还要「缺行则插入到 [Service] 段首」—— sed 的 `a\` 命令在
#      GNU（`a text`）与 BSD（`a\<换行>text`）上语法不同，是同一类不可移植陷阱；
#   2. 必须跳过整行注释（`#Environment=DRY_RUN=false` 不得被改写）—— sed 要靠
#      `b` 跳转 + 多个 -e 拼接，易错难读；
#   3. 一遍扫描同时完成三种变换；awk 已是本仓库既有依赖（promote.sh 的
#      _file_has_dryrun_true / _dbdir_values 同样用 awk）。
# 仍沿用 promote.sh 的「临时文件 + mv」而非 `-i`：GNU/BSD 的 -i 参数形态不同
# （macOS 要 `-i ''`），临时文件法两边一致，且 mv 是原子的，不留半个文件。
_reset_rewrite_file() {
  local f="$1" rewrite_dry="$2" insert_dry="$3" insert_db="$4" tmp
  tmp="${f}.up-tmp.$$"
  if ! awk -v dbdir="${GREEN_DB}" -v rewrite_dry="${rewrite_dry}" \
           -v insert_dry="${insert_dry}" -v insert_db="${insert_db}" '
    function set_val(line, key, want, newval,   pat, out, m, i, rs, rl) {
      if (want != "1") return line
      pat = "(^|[[:space:]=\"])" key "=(\"[^\"]*\"|[^[:space:]\"]*)"
      out = ""
      while (match(line, pat)) {
        rs = RSTART; rl = RLENGTH
        m = substr(line, rs, rl)
        i = index(m, key "=")
        out = out substr(line, 1, rs - 1) substr(m, 1, i - 1) key "=" newval
        line = substr(line, rs + rl)
      }
      return out line
    }
    { raw[NR] = $0 }
    END {
      n = NR
      has_dry = 0; has_db = 0; is_unit = 0
      for (i = 1; i <= n; i++) {
        if (raw[i] ~ /^\[Service\][[:space:]]*$/) is_unit = 1
        l = raw[i]
        sub(/^[[:space:]]*#.*/, "", l)
        if (l ~ /(^|[[:space:]="])DRY_RUN=("[^"]*"|[^[:space:]"]*)/) has_dry = 1
        if (l ~ /(^|[[:space:]="])DAILYCHECK_DB_DIR=("[^"]*"|[^[:space:]"]*)/) has_db = 1
      }
      need_dry = (insert_dry == "1" && is_unit == 1 && has_dry == 0)
      need_db  = (insert_db == "1" && is_unit == 1 && has_db == 0)
      inserted = 0
      for (i = 1; i <= n; i++) {
        l = raw[i]
        if (l ~ /^[[:space:]]*#/) { print l; continue }      # 注释行原样输出
        l = set_val(l, "DRY_RUN", rewrite_dry, "true")
        l = set_val(l, "DAILYCHECK_DB_DIR", "1", dbdir)
        print l
        if (inserted == 0 && l ~ /^\[Service\][[:space:]]*$/) {
          if (need_dry == 1) print "Environment=DRY_RUN=true"
          if (need_db  == 1) print "Environment=DAILYCHECK_DB_DIR=" dbdir
          inserted = 1
        }
      }
    }
  ' "${f}" > "${tmp}"; then
    echo "[up] ERROR: awk 改写失败: ${f}" >&2
    rm -f "${tmp}"
    return 1
  fi
  if ! mv "${tmp}" "${f}"; then
    echo "[up] ERROR: 改写结果落回失败: ${f}" >&2
    rm -f "${tmp}"
    return 1
  fi
  return 0
}

# systemd 视角复核（drop-in / 另一个 EnvironmentFile 覆盖的那一层）：
# 读不到 → 仅告警（与 promote.sh / switch.sh 的「unknown 只告警」同口径）；
# 读到**反证**（DRY_RUN=false 或 DB 指向别处）→ fail-closed。
_systemd_view_ok() {
  local unit="$1" check_dry="$2" envs dbv
  envs="$(systemctl show -p Environment --value "${unit}" 2>/dev/null || true)"
  if [[ -z "${envs}" ]]; then
    echo "[up] WARN: 读不到 systemd 侧 Environment（${unit}）—— 取证跳过，建议人工复核" >&2
    return 0
  fi
  # 用 herestring 而不是 `printf | grep -q`：`set -o pipefail` 下 grep -q 提前命中即
  # 关闭管道，printf 收到 SIGPIPE 会让整条管道返回 141 —— 条件被判假、守卫静默放行
  # （fail-open）。herestring 没有上游进程，规避这一整类陷阱。
  if [[ "${check_dry}" == "1" ]] \
     && grep -qiE '(^|[[:space:]="])DRY_RUN=(false|0|no)([[:space:]"]|$)' <<< "${envs}"; then
    echo "[up] ERROR: systemd 视角 ${unit} 仍报 DRY_RUN=false —— 存在本脚本未覆盖的覆盖源" >&2
    echo "[up]   （典型：/etc/systemd/system/${unit}.d/*.conf drop-in 或另一个 EnvironmentFile）" >&2
    echo "[up]   —— 此刻 restart 会让绿以**真实写能力**跑副本库：写进将被丢弃的库。" >&2
    echo "[up]   —— 本脚本紧接着会做一次 daemon-reload（让 systemd 重读磁盘上的 unit）；" >&2
    echo "[up]      确认已清掉 drop-in / 多余 EnvironmentFile 后重跑 up.sh 即可。" >&2
    return 1
  fi
  dbv="$(grep -oE 'DAILYCHECK_DB_DIR=("[^"]*"|[^[:space:]]*)' <<< "${envs}" \
    | sed -n '1p' | cut -d= -f2- | tr -d '"' || true)"
  if [[ -n "${dbv}" && "${dbv}" != "${GREEN_DB}" ]]; then
    echo "[up] ERROR: systemd 视角 ${unit} DAILYCHECK_DB_DIR=${dbv} != ${GREEN_DB}" >&2
    echo "[up]   —— 绿会读写真库/旧副本，隔离失效；拒绝继续。" >&2
    return 1
  fi
  return 0
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

# 3.0 复位为静息态（resting）—— Issue #28 闭环；顺序：# 2.5 已把流量切走之后
#
# 为什么必须在 2.5 之后：本步骤会让「文件里的 unit」变成 DRY_RUN=true。若绿仍在
# 承流，此刻任何一次 restart（本脚本步骤 4、或别的会话、或 systemd 自己
# Restart=always）都会把在役实例变成「写隔离 + 副本库」→ 全站写静默假成功
# （2026-10-15/17 两次事故同形态）。故：先切流量，再改文件，改完立刻 reload +
# 断言 + restart，中间不插入别的动作。
#
# 幂等：已是静息态 → 只打印 already resting，**不写文件、不 daemon-reload**
# （重复 reload 会让正在跑的 unit 重新读配置，是没必要的不确定态来源）。
if [[ ${DRY_RUN_FLAG} -eq 1 ]]; then
  echo "[up] [DRY-RUN] would reset green units to resting:"
  echo "[up] [DRY-RUN]   ${GREEN_UNIT_PATH}: DRY_RUN=true, DAILYCHECK_DB_DIR=${GREEN_DB}"
  echo "[up] [DRY-RUN]   ${GREEN_MCP_UNIT_PATH}: DAILYCHECK_DB_DIR=${GREEN_DB}"
  echo "[up] [DRY-RUN] would back up to ${BACKUP_DIR} then systemctl daemon-reload"
else
  if [[ ! -f "${GREEN_UNIT_PATH}" ]]; then
    echo "[up] ERROR: 绿 app unit 不存在: ${GREEN_UNIT_PATH}" >&2
    echo "[up]   —— 复位需要可改写的 unit 文件（fail-closed，不猜路径）。" >&2
    echo "[up]   —— 可用 GREEN_UNIT_PATH / SYSTEMD_UNIT_DIR 覆盖；unit 未建时先按 README 建好。" >&2
    exit 1
  fi
  if [[ ! -f "${GREEN_MCP_UNIT_PATH}" ]]; then
    echo "[up] WARN: 绿 MCP unit 不存在（${GREEN_MCP_UNIT_PATH}）—— 跳过该 unit 复位" >&2
  fi

  if _resting_ok; then
    echo "[up] already resting —— 绿 unit 已是静息态（DRY_RUN=true，DAILYCHECK_DB_DIR=${GREEN_DB}），不改文件、不 daemon-reload"
  else
    for _f in "${GREEN_UNIT_PATH}" "${GREEN_MCP_UNIT_PATH}" "${GREEN_ENV_FILE}" "${GREEN_MCP_ENV_FILE}"; do
      [[ -f "${_f}" ]] || continue
      _backup_file "${_f}" || _fail_restore "备份失败：${_f}"
    done

    _reset_rewrite_file "${GREEN_UNIT_PATH}" 1 1 1 \
      || _fail_restore "改写失败：${GREEN_UNIT_PATH}"
    echo "[up] 已改写 ${GREEN_UNIT_PATH}（DRY_RUN=true，DAILYCHECK_DB_DIR=${GREEN_DB}）"

    if [[ -f "${GREEN_MCP_UNIT_PATH}" ]]; then
      _reset_rewrite_file "${GREEN_MCP_UNIT_PATH}" 0 0 1 \
        || _fail_restore "改写失败：${GREEN_MCP_UNIT_PATH}"
      echo "[up] 已改写 ${GREEN_MCP_UNIT_PATH}（DAILYCHECK_DB_DIR=${GREEN_DB}）"
    fi
    for _f in "${GREEN_ENV_FILE}" "${GREEN_MCP_ENV_FILE}"; do
      [[ -f "${_f}" ]] || continue
      _reset_rewrite_file "${_f}" 1 0 0 || _fail_restore "改写失败：${_f}"
      echo "[up] 已改写 ${_f}（EnvironmentFile 覆盖 unit Environment=，必须同步）"
    done

    # 改写核验：awk 未命中 / 注释行被误命中 / 多处声明打架都要在这里拦下 ——
    # 「改了但没生效」正是本类事故的形态，不能只信命令成功。
    _resting_ok \
      || _fail_restore "改写后仍非静息态（DRY_RUN 非 true 或 DAILYCHECK_DB_DIR != ${GREEN_DB}）"
    echo "[up] 复位核验通过：DRY_RUN=true 且 DAILYCHECK_DB_DIR=${GREEN_DB}"

    systemctl daemon-reload || _fail_restore "systemctl daemon-reload 失败"
    echo "[up] daemon-reload 完成（systemd 已读入静息态 unit）"
  fi
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

# 3.5 隔离守卫（Issue #28）—— 改写后的 fail-closed 断言
#
# 由「前置检查」升级为「复位后断言」：3.0 已经负责把 unit 写成静息态，本步骤
# 只负责**证明它真的成了**，不通过就 exit 1（不信任何一步的返回值）。
# 三层取证，从「我写了什么」到「systemd 实际会用什么」：
#   ① 文件生效行（我们刚写的文件）—— DRY_RUN=true 且 DAILYCHECK_DB_DIR=${GREEN_DB}
#   ② systemd 视角（systemctl show -p Environment）—— 抓 drop-in / 另一个
#      EnvironmentFile 的覆盖（文件写了 true、systemd 仍解析出 false 的典型场景）
#   ③ 缺失即失败：unit 未声明 DB → 绿共享蓝的物理库，隔离从未生效
# 注意：这里**不**看 /proc/<pid>/environ —— 此刻还没 restart（进程仍是转正态），
# 进程环境判的是上一轮的事实，会把「正确的复位」误判成失败。
if [[ ${DRY_RUN_FLAG} -eq 0 ]]; then
  _resting_ok || _fail_restore "静息态断言失败（文件生效行不符：DRY_RUN 非 true 或 DAILYCHECK_DB_DIR != ${GREEN_DB}）"
  echo "[up] ✅ 文件生效行确认: DRY_RUN=true, DAILYCHECK_DB_DIR=${GREEN_DB}"

  _systemd_view_ok "${GREEN_UNIT}" 1 || _fail_restore "systemd 视角复核失败（${GREEN_UNIT}）"
  if [[ -f "${GREEN_MCP_UNIT_PATH}" ]]; then
    _systemd_view_ok "${GREEN_MCP_UNIT}" 0 || _fail_restore "systemd 视角复核失败（${GREEN_MCP_UNIT}）"
  fi
  echo "[up] ✅ 隔离守卫通过: 绿以写隔离（DRY_RUN=true）跑副本库（DAILYCHECK_DB_DIR=${GREEN_DB}）"
else
  echo "[up] [DRY-RUN] would assert: DRY_RUN=true 且 DAILYCHECK_DB_DIR=${GREEN_DB}（文件 + systemd 视角）"
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
