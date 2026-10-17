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
#
# 2026-10-15 P0 事故加固（绿实例以 DRY_RUN=true 承接生产流量 44 分钟：
# 窗口期写操作全部「假成功」——HTTP 200 / 审计照记 / DB 不落盘）：
#   - DRY_RUN 守卫：凡切换后承接流量（weight>0）的实例，若处于 DRY_RUN=true
#     直接拒跑（exit 3）——先「绿转正」（关写隔离），标准步骤见 README。
#     检测优先级：进程真实环境（/proc/<pid>/environ）→ unit 配置
#     （systemctl show Environment）→ 无法判定（无 systemctl / unit 不存在）仅告警。
#   - conf 应用核验：sed 后逐行核对 4 条 server 行与期望一致（sed 未命中时
#     nginx -t 仍通过、reload 仍「成功」，但权重根本没变 —— 静默失败）。
#   - reload 失败显式回滚（旧版 set -e 直退，会留下 conf/运行态分歧）。
#   - 落点核验：100%/0% 边界用带 nonce 的探针核对流量真实落到目标实例的
#     access log（防「文件写了但 reload 没生效」的静默漂移）。
#
# 2026-10-16 Issue #55 修正（落点核验的 reload race）：
#   - nginx worker 在 reload 后需 1-3 秒才刷新 in-memory upstream，而旧实现
#     只发**一次**探针——这一次几乎必然落在旧配置（对侧），随后的 for 循环
#     只是反复 grep 同一个 nonce：已经打到对侧的请求不可能再改路由，于是
#     必然判「reload 未生效」→ 误回滚（生产实测：`switch.sh green 100` 被
#     误判失败自动回滚，部署只能人工 sleep 3 绕过）。
#   - 修法：每次尝试用**新 nonce 重发**探针，命中目标实例 access log 即成功；
#     单次尝试的轮询窗口内没命中就等 LAND_RETRY_SLEEP 秒（worker 刷新）再试，
#     最多 LAND_TRY_MAX 次。对侧命中不再一票判死（那是 race 的正常表现）——
#     只有「所有尝试都没命中目标 log，且至少一次落在对侧」才认定 reload 真的
#     没生效（回滚）；探针全异常 / 日志不可读仍只告警。
#
# 退出码：0 成功 / 1 运行期核验失败（已自动回滚）/ 2 布局守卫拒绝 /
#         3 DRY_RUN 守卫拒绝 / 64 参数错误

set -euo pipefail

# ============================================================
# 参数解析
# ============================================================
# 先扫 --help / -h
for arg in "$@"; do
  if [[ "$arg" == "-h" || "$arg" == "--help" ]]; then
    echo "usage: $0 <green|blue> [percent] [--dry-run]"
    echo "  --dry-run   只 echo 不真跑 nginx -t / reload"
    echo "  exit: 2=布局守卫拒绝 / 3=目标实例仍 DRY_RUN（先「绿转正」，见 README）"
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
# 2026-10-15 事故加固：DRY_RUN 守卫检查的 systemd unit + 落点核验读的 access log
BLUE_SERVICE_UNIT="${BLUE_SERVICE_UNIT:-dailycheck.service}"
GREEN_SERVICE_UNIT="${GREEN_SERVICE_UNIT:-dailycheck-green.service}"
BLUE_ACCESS_LOG="${BLUE_ACCESS_LOG:-/root/DailyCheck/access.log}"
GREEN_ACCESS_LOG="${GREEN_ACCESS_LOG:-/var/log/dailycheck/green-access.log}"

# ============================================================
# 部署单飞守卫 + 进程级互斥（deploy-singleflight，2026-10-16）
# ============================================================
# 语义（仅 live；dry-run 完全跳过）：
#   - 目标为 green（承接流量方向）→ 需要活跃部署锁（或 UNLOCKED=1 逃生）；
#   - 目标为 blue（安全/回滚方向）→ 免校验（rollback.sh / verify 演练的
#     内嵌 switch 调用依赖此豁免，止损操作不得被部署锁阻塞）。
if [[ ${DRY_RUN_FLAG} -eq 0 ]]; then
  _DS_DIR="${DAILYCHECK_DEPLOY_STATE_DIR:-/var/lib/dailycheck-deploy}"
  _DS_SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
  if [[ "${TARGET}" == "green" ]]; then
    if [[ "${DAILYCHECK_DEPLOY_UNLOCKED:-0}" == "1" ]]; then
      echo "[switch] WARN: DAILYCHECK_DEPLOY_UNLOCKED=1 —— 跳过部署锁校验（人工操作）" >&2
    elif [[ ! -d "${_DS_DIR}" ]]; then
      echo "[switch] [deploy-lock] 状态目录 ${_DS_DIR} 不存在（单飞机制未激活）—— 跳过校验"
    elif [[ ! -f "${_DS_SCRIPT_DIR}/deploy_state.py" ]] || ! command -v python3 >/dev/null 2>&1; then
      echo "[switch] WARN: deploy_state.py / python3 不可用 —— 跳过部署锁校验" >&2
    else
      _ds_rc=0
      _ds_out="$(python3 "${_DS_SCRIPT_DIR}/deploy_state.py" check 2>&1)" || _ds_rc=$?
      if [[ ${_ds_rc} -eq 10 ]]; then
        { printf '%s switch.sh target=green %s\n' "$(date -u +%FT%TZ)" "${_ds_out}"; } \
          >> "${_DS_DIR}/audit.log" 2>/dev/null || true
      elif [[ ${_ds_rc} -eq 0 ]]; then
        echo "[switch] ERROR: 单飞机制已激活但无活跃部署锁 —— 拒绝切绿（exit 10）。" >&2
        echo "[switch] 请先由部署会话执行 deploy_state.py acquire；人工受控操作可设 DAILYCHECK_DEPLOY_UNLOCKED=1。" >&2
        exit 10
      else
        echo "[switch] WARN: 部署锁检查异常 rc=${_ds_rc}（${_ds_out}）—— 跳过校验" >&2
      fi
    fi
  fi

  if [[ "${DAILYCHECK_BG_LOCK_HELD:-0}" != "1" ]]; then
    _BG_LOCK_FILE="${DAILYCHECK_BG_LOCK_FILE:-/var/lock/dailycheck-bluegreen.lock}"
    if ! command -v flock >/dev/null 2>&1; then
      echo "[switch] WARN: flock 不可用 —— 进程级互斥跳过（会话级锁仍生效）" >&2
    elif ! mkdir -p "$(dirname "${_BG_LOCK_FILE}")" 2>/dev/null || ! : >>"${_BG_LOCK_FILE}" 2>/dev/null; then
      echo "[switch] WARN: 锁文件不可写（${_BG_LOCK_FILE}）—— 进程级互斥跳过" >&2
    else
      exec 9>"${_BG_LOCK_FILE}"
      if ! flock -n 9; then
        echo "[switch] ERROR: 另一蓝绿操作正在进行（${_BG_LOCK_FILE} 被占）—— 拒绝并发" >&2
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
# 辅助：回滚 conf + reload（reload / 探活 / 落点核验失败路径共用）
# ============================================================
_rollback_conf_and_reload() {
  if cp "${BACKUP}" "${NGINX_CONF}"; then
    if nginx -t && systemctl reload nginx; then
      echo "[switch] 回滚 reload OK，conf 恢复到切换前状态" >&2
    else
      echo "[switch] 回滚后 nginx -t / reload 仍失败，需人工介入" >&2
    fi
  else
    echo "[switch] 回滚 cp 失败，conf 未恢复，需人工介入" >&2
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
# DRY_RUN 守卫（2026-10-15 P0 事故）：切换后承接流量（weight>0）的任何
# 实例都必须具备真实写能力。DRY_RUN 下写操作被 db/dryrun.py 拦成
# log-only（HTTP 200 / 审计照记 / DB 不落盘 =「假成功」）——先例：绿实例
# 以此状态接管生产流量 44 分钟，全站写失效。命中直接拒跑（exit 3）。
#
# 检测优先级（取证强度从高到低）：
#   ① 进程真实环境 /proc/<pid>/environ（systemctl show MainPID 定位进程）
#   ② unit 配置 systemctl show Environment（进程不在时的配置实况）
#   ③ 无法判定（无 systemctl / unit 不存在）→ 仅告警放行（有探活兜底）
# ============================================================
_detect_dry_run() {
  local unit="$1" pid envs
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "unknown"
    return 0
  fi
  pid="$(systemctl show -p MainPID --value "${unit}" 2>/dev/null || true)"
  if [[ -n "${pid}" && "${pid}" != "0" && -r "/proc/${pid}/environ" ]]; then
    envs="$(tr '\0' '\n' < "/proc/${pid}/environ" 2>/dev/null || true)"
  else
    envs="$(systemctl show -p Environment --value "${unit}" 2>/dev/null || true)"
  fi
  if printf '%s\n' "${envs}" | grep -qiE '(^|[[:space:]])DRY_RUN=(true|1|yes)([[:space:]]|$)'; then
    echo "dry_run"
  elif [[ -n "${envs}" ]]; then
    echo "not_dry_run"
  else
    echo "unknown"
  fi
}

DRY_RUN_GUARD_BLOCKED_LIST=""
DRY_RUN_GUARD_UNKNOWN_LIST=""
if [[ "${BLUE_WEIGHT}" -gt 0 ]]; then
  case "$(_detect_dry_run "${BLUE_SERVICE_UNIT}")" in
    dry_run) DRY_RUN_GUARD_BLOCKED_LIST="${DRY_RUN_GUARD_BLOCKED_LIST}${DRY_RUN_GUARD_BLOCKED_LIST:+, }蓝 ${BLUE_SERVICE_UNIT}" ;;
    unknown) DRY_RUN_GUARD_UNKNOWN_LIST="${DRY_RUN_GUARD_UNKNOWN_LIST}${DRY_RUN_GUARD_UNKNOWN_LIST:+, }蓝 ${BLUE_SERVICE_UNIT}" ;;
  esac
fi
if [[ "${GREEN_WEIGHT}" -gt 0 ]]; then
  case "$(_detect_dry_run "${GREEN_SERVICE_UNIT}")" in
    dry_run) DRY_RUN_GUARD_BLOCKED_LIST="${DRY_RUN_GUARD_BLOCKED_LIST}${DRY_RUN_GUARD_BLOCKED_LIST:+, }绿 ${GREEN_SERVICE_UNIT}" ;;
    unknown) DRY_RUN_GUARD_UNKNOWN_LIST="${DRY_RUN_GUARD_UNKNOWN_LIST}${DRY_RUN_GUARD_UNKNOWN_LIST:+, }绿 ${GREEN_SERVICE_UNIT}" ;;
  esac
fi

if [[ -n "${DRY_RUN_GUARD_BLOCKED_LIST}" ]]; then
  echo "[switch] ⛔ DRY_RUN 守卫触发：以下承接流量的实例处于 DRY_RUN（写操作会被吞成「假成功」）：" >&2
  echo "[switch]   ${DRY_RUN_GUARD_BLOCKED_LIST}" >&2
  echo "[switch]   —— 2026-10-15 生产事故（44 分钟全站写失效）即由此造成" >&2
  echo "[switch]   —— 处置：先「绿转正」（关 DRY_RUN + DB 指向真库 + restart），" >&2
  echo "[switch]      标准步骤见 scripts/blue_green/README.md「绿转正」" >&2
  exit 3
fi
if [[ -n "${DRY_RUN_GUARD_UNKNOWN_LIST}" ]]; then
  echo "[switch] DRY_RUN 守卫：无法判定以下实例的 DRY_RUN 状态（仅告警放行）：${DRY_RUN_GUARD_UNKNOWN_LIST}" >&2
else
  echo "[switch] DRY_RUN 守卫 OK：承接流量的实例均非 DRY_RUN（具备真实写能力）"
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
# conf 应用核验（2026-10-15 事故加固）：sed 未命中/部分命中时
# nginx -t 仍然通过、reload 仍然「成功」，但权重根本没变 ——
# 逐行核对 4 条 server 行与期望完全一致，不一致立即回滚拒跑。
# ============================================================
_verify_conf_applied() {
  local line
  for line in "${BLUE_LINE}" "${GREEN_LINE}" "${BLUE_MCP_LINE}" "${GREEN_MCP_LINE}"; do
    if ! grep -qF "${line}" "${NGINX_CONF}"; then
      echo "[switch] conf 应用核验失败：未找到期望行「${line}」（sed 未命中）" >&2
      return 1
    fi
  done
  return 0
}

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
  if ! _verify_conf_applied; then
    echo "[switch] conf 应用核验失败，rolling back ..." >&2
    cp "${BACKUP}" "${NGINX_CONF}"
    exit 1
  fi
  echo "[switch] conf 应用核验 OK：4 条 server 行与期望一致"
else
  echo "[DRY-RUN] would nginx -t + conf 应用核验（4 条 server 行）"
fi

# ============================================================
# reload（流式，0 中断）
# ============================================================
echo "[switch] nginx reload ..."
if [[ ${DRY_RUN_FLAG} -eq 0 ]]; then
  if ! systemctl reload nginx; then
    echo "[switch] nginx reload 失败，自动回滚备份 + reload ..." >&2
    _rollback_conf_and_reload
    exit 1
  fi
else
  echo "[DRY-RUN] would systemctl reload nginx（失败自动回滚）"
fi

# ============================================================
# post-switch 探活（Issue #32）：过 nginx 入口 :8080 真实打主站 + MCP。
# 旧 bug 是「upstream 切了但 location 还在指旧池」→ reload 后用户 502。
# 探活非 502 才算成功；任一 5xx/超时自动 cp 备份回滚 + reload 恢复
# （失败也 exit 非零，让 verify.sh check 8 知道）。
# ============================================================
NGINX_ENTRY="${NGINX_ENTRY:-http://127.0.0.1:8080}"
if [[ ${DRY_RUN_FLAG} -eq 1 ]]; then
  echo "[DRY-RUN] would probe ${NGINX_ENTRY}/ 与 ${NGINX_ENTRY}/api/mcp/（非 502 才算成功）"
  echo "[DRY-RUN] would 落点核验（仅 100%/0% 边界：带 nonce 探针重发核对目标实例 access log，最多 ${LAND_TRY_MAX:-5} 次）"
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
  _rollback_conf_and_reload
  exit 1
fi

# ============================================================
# 落点核验（2026-10-15 事故加固 + 2026-10-16 Issue #55 修正）：仅 100%/0%
# 边界做确定性断言 —— 核对带 nonce 的探针真实落进「应承接实例」的 access
# log（防「conf 写了但 reload 没生效 / 流量没真切」的静默漂移）。灰度中
# （1-99%）路由随机，跳过；access log 不可读时仅告警不回滚。
#
# Issue #55：探针必须**重发**。旧实现只发一次探针再反复 grep 同一 nonce，
# 撞上 nginx worker 刷新窗口（1-3 秒）时必然误判失败并回滚。详见文件头。
#
# 返回值：0=命中目标实例 / 1=只落对侧（reload 未生效，调用方回滚）/
#         2=无法判定（探针异常 / 日志不可读，调用方仅告警）
#
# 注：本函数经调用方 `... || LAND_RC=$?` 调用 —— bash 在该上下文内会**临时忽略
# errexit**，函数体内单条命令失败不会中断脚本。所有分支都显式 `return`，判定
# 语义完全由返回码承载，不要依赖函数内的 set -e 行为。
# ============================================================
_landing_verify() {
  local land_log="$1" other_log="$2" land_desc="$3" entry="$4"
  local try_max="${LAND_TRY_MAX:-5}"          # 探针重发次数上限
  local poll_max="${LAND_POLL_MAX:-4}"        # 单次探针的日志轮询次数
  local poll_step="${LAND_POLL_STEP:-0.25}"   # 每次轮询间隔（秒）
  local retry_sleep="${LAND_RETRY_SLEEP:-1}"  # 两次探针之间等 worker 刷新的秒数
  local same_log=0
  local hit_target=0 hit_other=0 probe_ok=0
  local probe_try probe_poll nonce code

  if [[ ${try_max} -lt 2 ]]; then
    echo "[switch] 落点核验警告：LAND_TRY_MAX=${try_max}（<2）= 只发一次探针，reload 后 worker 刷新窗口内会重现 Issue #55 的误回滚（仅调试用，勿用于生产）" >&2
  fi

  if [[ -f "${land_log}" && -f "${other_log}" && "${land_log}" -ef "${other_log}" ]]; then
    same_log=1
  fi

  echo "[switch] 落点核验：期望命中 ${land_desc} access log（log=${land_log}）..."
  for ((probe_try = 1; probe_try <= try_max; probe_try++)); do
    nonce="dc_probe_$(date +%s)_$$_${RANDOM}_${probe_try}"
    code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "${entry}/health?${nonce}=1" 2>/dev/null || echo 000)"
    echo "  [落点核验] 第 ${probe_try}/${try_max} 次探针 nonce=${nonce} → HTTP ${code}"
    if [[ "${code}" =~ ^5 || "${code}" == "000" ]]; then
      # 入口还没起来 / 5xx：本轮探针无效，等下一轮（不据此回滚——前置探活已管 5xx）
      if [[ ${probe_try} -lt ${try_max} ]]; then sleep "${retry_sleep}"; fi
      continue
    fi
    probe_ok=1
    for ((probe_poll = 1; probe_poll <= poll_max; probe_poll++)); do
      sleep "${poll_step}"
      if [[ -f "${land_log}" ]] && grep -qF "${nonce}" "${land_log}"; then
        hit_target=1
        break
      fi
      if [[ ${same_log} -eq 0 && -f "${other_log}" ]] && grep -qF "${nonce}" "${other_log}"; then
        hit_other=1
        break  # 本次探针已判明走向对侧 → 换新 nonce 再试（race 的正常表现）
      fi
    done
    if [[ ${hit_target} -eq 1 ]]; then break; fi
    echo "    [落点核验] 本次探针未命中目标 log（worker 可能尚未刷新），重发..."
    if [[ ${probe_try} -lt ${try_max} ]]; then sleep "${retry_sleep}"; fi
  done

  if [[ ${hit_target} -eq 1 ]]; then
    echo "[switch] 落点核验 OK：流量真实落在 ${land_desc}（nonce 命中其 access log，第 ${probe_try} 次探针）"
    if [[ ${hit_other} -eq 1 ]]; then
      echo "[switch]   注：此前有探针落在对侧实例 access log —— reload 后 nginx worker 刷新窗口内的正常现象（已重发并命中目标，非故障）"
    fi
    return 0
  fi
  if [[ ${hit_other} -eq 1 ]]; then
    echo "[switch] 落点核验失败：${try_max} 次探针均未命中目标实例 access log，且探针出现在对侧实例 access log（reload 未生效 / 流量未按权重路由）" >&2
    return 1
  fi
  if [[ ${probe_ok} -eq 1 ]]; then
    echo "[switch] 落点核验：无法判定（access log 不可读或 nonce 未见于两侧）——仅告警" >&2
  else
    echo "[switch] 落点核验：探针请求异常（HTTP 5xx / 000，${try_max} 次均未拿到响应），无法核验——仅告警" >&2
  fi
  echo "[switch]   目标 log=${land_log}" >&2
  echo "[switch]   对侧 log=${other_log}" >&2
  return 2
}

if [[ ${GREEN_WEIGHT} -eq 100 || ${BLUE_WEIGHT} -eq 100 ]]; then
  if [[ ${GREEN_WEIGHT} -eq 100 ]]; then
    LAND_LOG="${GREEN_ACCESS_LOG}"; LAND_OTHER_LOG="${BLUE_ACCESS_LOG}"; LAND_DESC="绿 :${GREEN_PORT}"
  else
    LAND_LOG="${BLUE_ACCESS_LOG}"; LAND_OTHER_LOG="${GREEN_ACCESS_LOG}"; LAND_DESC="蓝 :${BLUE_PORT}"
  fi
  LAND_RC=0
  _landing_verify "${LAND_LOG}" "${LAND_OTHER_LOG}" "${LAND_DESC}" "${NGINX_ENTRY}" || LAND_RC=$?
  case ${LAND_RC} in
    0) ;;  # 落点核验 OK（函数内已回显）
    1) echo "[switch] 自动回滚备份 + reload ..." >&2
       _rollback_conf_and_reload
       exit 1 ;;
    *) ;;  # 2 = 无法判定：仅告警，不阻断（日志盲区不该挡住部署）
  esac
else
  echo "[switch] 落点核验跳过：灰度 ${GREEN_WEIGHT}%/${BLUE_WEIGHT}%（路由随机，无法做确定性落点断言）"
fi

echo "[switch] ✅ nginx reloaded + 探活/核验通过, green=${GREEN_WEIGHT}% blue=${BLUE_WEIGHT}%"
exit 0
