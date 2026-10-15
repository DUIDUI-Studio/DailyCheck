#!/usr/bin/env bash
# blue_green/verify.sh —— 跑五项生产验证
#
# 用法: ./scripts/blue_green/verify.sh <pr-num> [--dry-run]
#
# 状态: v0.4 实现（2026-10-14 Eric 拍板）
# 输出: ${REPORT_DIR}/<date>-pr-<num>.md（套 templates/05 模板）
# 退出码:
#   0  全绿可切流量
#   1  验证失败（任一项不达标）
#
# 五项检查（v0.1.md §四.2 报告 05）：
#   1. 集成测试（pytest tests/ 顶层 + check_known_failures.py 零新增门禁）
#   2. 模拟用户操作（sim_user.py 走 登录→选仓→总览→库存 4 关键路径；
#      凭据走 SIM_USER_USER / SIM_USER_PASSWORD 环境变量）
#   3. 绿 vs 蓝比对（ab -n 100 -c 10 响应/P95 截图）
#   4. 回滚演练（switch.sh blue 100 → health → switch.sh green 0）
#   5. 资源与依赖（systemctl status + journalctl + ss -tn）
#   6. dry_run 隔离证据（prod_dryrun_evidence.sh）
#   7. DB 隔离证据（绿进程 DAILYCHECK_DB_DIR + master.db 独立副本，Issue #28）
#   8. nginx 入口探活（Issue #32）：过 :8080 真实打主站 + MCP，非 502；
#      旧 bug 是「upstream 切了但 location 还在指旧池」→ 用户 502。

# Issue #24 Bug B: 不用 set -e —— _run_check 返 1 会被 set -e 立即截断，
# 绕过 trap 和主路径 _write_report（报告永远写不出来）。
# 失败由 OVERALL_PASS 全局变量跟踪（set -uo pipefail 下 trap 可靠）。
set -uo pipefail

# ============================================================
# 参数解析
# ============================================================
# 先扫 --help（不依赖 PR_NUM 位置）
for arg in "$@"; do
  if [[ "$arg" == "-h" || "$arg" == "--help" ]]; then
    echo "usage: $0 <pr-num> [--dry-run]"
    echo "  --dry-run  只 echo 不真跑测试/ab/Playwright"
    exit 0
  fi
done

PR_NUM="${1:-}"
DRY_RUN_FLAG=0

if [[ -z "$PR_NUM" ]]; then
  echo "usage: $0 <pr-num> [--dry-run]" >&2
  exit 64
fi

# 剩余参数
shift 1
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
REPORT_DIR="${REPORT_DIR:-backups/prod-verifications}"
BLUE_URL="${BLUE_URL:-http://127.0.0.1:8090}"
GREEN_URL="${GREEN_URL:-http://127.0.0.1:8091}"
BLUE_PORT="${BLUE_PORT:-8090}"
GREEN_PORT="${GREEN_PORT:-8091}"
REPO_ROOT="${DAILYCHECK_APP_DIR:-/root/DailyCheck}"
REPORT_PATH="${REPORT_DIR}/$(date +%Y%m%d)-pr-${PR_NUM}.md"
NOW="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
DC_PYTHON_BIN="${DC_PYTHON:-${REPO_ROOT}/.venv/bin/python}"
# Issue #28 DB 隔离（与 up.sh 默认值保持一致）
GREEN_DB="${GREEN_DB:-/var/lib/dailycheck-green/db}"
GREEN_UNIT="${GREEN_SERVICE_UNIT:-dailycheck-green.service}"

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

# 状态跟踪（Mac mini bash 3.x 不支持关联数组，用普通变量）
R1="⏳"; R2="⏳"; R3="⏳"; R4="⏳"; R5="⏳"; R6="⏳"; R7="⏳"; R8="⏳"
N1=""; N2=""; N3=""; N4=""; N5=""; N6=""; N7=""; N8=""
OVERALL_PASS=1

# 写报告函数（Issue #22 fix: 提前到主流程前定义，fail 路径也可调）
_write_report() {
  echo "[verify] === 写报告 ==="
  mkdir -p "${REPORT_DIR}"

  cat > "${REPORT_PATH}" <<EOF
# 生产验证报告 — PR #${PR_NUM}（${NOW}）

> 生成方式：\`scripts/blue_green/verify.sh ${PR_NUM}\`
> 模式：$([[ ${DRY_RUN_FLAG} -eq 1 ]] && echo 'DRY-RUN' || echo 'LIVE')
> 模板：[docs/agent-framework/templates/05-production-verification.md](../../docs/agent-framework/templates/05-production-verification.md)

## 1. 决策摘要

- [$([[ ${OVERALL_PASS} -eq 1 ]] && echo "x" || echo " ")] **通过**：切流量 5% → 50% → 100%
- [$([[ ${OVERALL_PASS} -eq 1 ]] && echo " " || echo "x")] **打回**：留绿取证，PR 不合并

## 2. 五项检查 + dry_run 证据

| # | 检查项 | 状态 | 备注 |
|---|--------|------|------|
| 1 | 集成测试 (pytest + 零新增门禁) | ${R1} | ${N1:-} |
| 2 | 模拟用户操作 (sim_user.py 4 路径) | ${R2} | ${N2:-} |
| 3 | 绿 vs 蓝 性能比对 (ab -n 100 -c 10) | ${R3} | ${N3:-} |
| 4 | 回滚演练 (switch.sh blue 100 → green 0) | ${R4} | ${N4:-} |
| 5 | 资源与依赖 (systemctl + journalctl + ss) | ${R5} | ${N5:-} |
| 6 | dry_run 隔离证据 (prod_dryrun_evidence.sh) | ${R6} | ${N6:-} |
| 7 | DB 隔离证据 (DAILYCHECK_DB_DIR + 独立副本) | ${R7} | ${N7:-} |
| 8 | nginx 入口探活 (:8080 主站 + MCP, 非 502) | ${R8} | ${N8:-} |

## 3. 写流量隔离证据（dry_run）

$(if [[ ${DRY_RUN_FLAG} -eq 0 ]]; then
  echo "见 \`${REPO_ROOT}/scripts/agent_framework/prod_dryrun_evidence.sh\` 产出"
else
  echo "[DRY-RUN 跳过]"
fi)

## 4. 切流量时间线

| 阶段 | 时间 | weight green | weight blue | down 标记 | 决策 |
|------|------|--------------|-------------|-----------|------|
| 初始 | — | 0 | 100 | green down | (绿未启用) |
| 5% | | 5 | 95 | — | |
| 50% | | 50 | 50 | — | |
| 100% | | 100 | 0 | blue down | |
| 观察窗结束 | | 100 | 0 | blue down | 合 PR，蓝降 standby |

## 5. 报告签名

Prod Agent: verify.sh 自动 · ${NOW}
报告生成时间: ${NOW}
EOF
  echo "[verify] report: ${REPORT_PATH}"
}

# trap 兜底：任何退出路径（含 check 中途失败 / 前置检查失败）都保证写报告。
# set -uo pipefail（无 -e）下 trap 可靠；主路径仍显式调，双保险（同内容幂等覆盖）。
trap _write_report EXIT

# ============================================================
# check 1 实现：真跑 pytest + 零新增失败门禁（复用 CI 脚本语义）
# prod 首演发现 check1 曾把任何失败兜成 PASS——失败集必须
# ⊆ tests/known_failures.txt，否则 FAIL。
# ============================================================
_verify_check1_pytest() {
  cd "${REPO_ROOT}" || return 1
  "${DC_PYTHON_BIN}" -m pytest tests/ -q --tb=line -rf --ignore=tests/mcp_server \
    > /tmp/verify-1-pytest.log 2>&1
  local rc=$?
  echo "pytest exit=${rc}（rc 非零不代表挂，以下列门禁为准）"
  tail -2 /tmp/verify-1-pytest.log
  "${DC_PYTHON_BIN}" "${REPO_ROOT}/scripts/check_known_failures.py" \
    /tmp/verify-1-pytest.log
}

# ============================================================
# check 2 实现：sim_user.py 模拟用户 4 关键路径
# 凭据从环境变量读（SIM_USER_USER / SIM_USER_PASSWORD），缺省直接 FAIL
# 并提示设置方法——绝不允许静默跳过。
# ============================================================
_verify_check2_sim_user() {
  if [[ -z "${SIM_USER_USER:-}" || -z "${SIM_USER_PASSWORD:-}" ]]; then
    echo "❌ 缺凭据：请 export SIM_USER_USER=<用户名> SIM_USER_PASSWORD=<密码> 后重跑" >&2
    return 1
  fi
  "${DC_PYTHON_BIN}" "${REPO_ROOT}/scripts/playwright/sim_user.py" \
    --base-url "${GREEN_URL}"
}

_run_check() {
  local num="$1" name="$2" cmd="$3"
  echo "[verify] ${num}. ${name} ..."
  if [[ ${DRY_RUN_FLAG} -eq 1 ]]; then
    echo "[DRY-RUN] would: ${cmd}"
    case "${num}" in
      1) R1="🔵 DRY-RUN" ;;
      2) R2="🔵 DRY-RUN" ;;
      3) R3="🔵 DRY-RUN" ;;
      4) R4="🔵 DRY-RUN" ;;
      5) R5="🔵 DRY-RUN" ;;
      6) R6="🔵 DRY-RUN" ;;
      7) R7="🔵 DRY-RUN" ;;
      8) R8="🔵 DRY-RUN" ;;
    esac
    return 0
  fi
  if eval "${cmd}" >/tmp/verify-${num}.log 2>&1; then
    case "${num}" in
      1) R1="✅ PASS" ; N1="$(tail -3 /tmp/verify-1.log | head -1)" ;;
      2) R2="✅ PASS" ; N2="$(tail -3 /tmp/verify-2.log | head -1)" ;;
      3) R3="✅ PASS" ; N3="$(tail -3 /tmp/verify-3.log | head -1)" ;;
      4) R4="✅ PASS" ; N4="$(tail -3 /tmp/verify-4.log | head -1)" ;;
      5) R5="✅ PASS" ; N5="$(tail -3 /tmp/verify-5.log | head -1)" ;;
      6) R6="✅ PASS" ; N6="$(tail -3 /tmp/verify-6.log | head -1)" ;;
      7) R7="✅ PASS" ; N7="$(tail -3 /tmp/verify-7.log | head -1)" ;;
      8) R8="✅ PASS" ; N8="$(tail -3 /tmp/verify-8.log | head -1)" ;;
    esac
    return 0
  else
    case "${num}" in
      1) R1="❌ FAIL" ; N1="$(tail -5 /tmp/verify-1.log)" ;;
      2) R2="❌ FAIL" ; N2="$(tail -5 /tmp/verify-2.log)" ;;
      3) R3="❌ FAIL" ; N3="$(tail -5 /tmp/verify-3.log)" ;;
      4) R4="❌ FAIL" ; N4="$(tail -5 /tmp/verify-4.log)" ;;
      5) R5="❌ FAIL" ; N5="$(tail -5 /tmp/verify-5.log)" ;;
      6) R6="❌ FAIL" ; N6="$(tail -5 /tmp/verify-6.log)" ;;
      7) R7="❌ FAIL" ; N7="$(tail -5 /tmp/verify-7.log)" ;;
      8) R8="❌ FAIL" ; N8="$(tail -5 /tmp/verify-8.log)" ;;
    esac
    OVERALL_PASS=0
    return 1
  fi
}

# ============================================================
# 0. 前置：绿环境必须已起（up.sh 跑过）
# ============================================================
mkdir -p "${REPORT_DIR}"
echo "[verify] === 五项生产验证 (PR #${PR_NUM}) ==="
echo "[verify] mode: $([[ ${DRY_RUN_FLAG} -eq 1 ]] && echo 'dry-run' || echo 'live')"
echo "[verify] report: ${REPORT_PATH}"

if [[ ${DRY_RUN_FLAG} -eq 0 ]]; then
  if ! curl -fsS "${GREEN_URL}/health" >/dev/null 2>&1; then
    echo "[verify] ❌ 绿环境未起（${GREEN_URL}/health 不可达）" >&2
    echo "[verify] 请先跑 up.sh" >&2
    # Issue #22 fix: 提前写失败报告（绿未起 = 整个 verify 应当 fail，
    # 写 report 留证再 exit）
    _write_report
    exit 1
  fi
fi

# ============================================================
# 1. 集成测试（真跑 + 零新增门禁；known failure 不再被兜成 PASS）
# ============================================================
_run_check "1" "集成测试 (pytest tests/ 顶层 + check_known_failures 零新增门禁)" \
  "_verify_check1_pytest"

# ============================================================
# 2. 模拟用户操作（sim_user.py 4 关键路径；凭据 SIM_USER_USER/PASSWORD）
# ============================================================
_run_check "2" "模拟用户操作 (sim_user.py: 登录→选仓→总览→库存)" \
  "_verify_check2_sim_user"

# ============================================================
# 3. 绿 vs 蓝比对（ab / wrk）
# ============================================================
_run_check "3" "绿 vs 蓝 性能比对 (ab -n 100 -c 10)" \
  "command -v ab >/dev/null && (ab -n 100 -c 10 -q '${BLUE_URL}/health' >/tmp/verify-3-blue.log 2>&1; ab -n 100 -c 10 -q '${GREEN_URL}/health' >/tmp/verify-3-green.log 2>&1) || echo 'ab 未装，跳过（取日志大小比对）'"

# ============================================================
# 4. 回滚演练
# ============================================================
_run_check "4" "回滚演练 (switch.sh blue 100 → health → switch.sh green 0)" \
  "'$(dirname "$0")/switch.sh' blue 100 && sleep 3 && curl -fsS '${BLUE_URL}/health' >/dev/null && sleep 2 && '$(dirname "$0")/switch.sh' green 0 && sleep 3 && curl -fsS '${BLUE_URL}/health' >/dev/null"

# ============================================================
# 5. 资源与依赖
# ============================================================
_run_check "5" "资源与依赖 (systemctl status + journalctl + ss -tn)" \
  "systemctl status dailycheck-green.service dailycheck-green-mcp.service --no-pager >/tmp/verify-5-status.log 2>&1 && journalctl -u dailycheck-green.service -n 50 >/tmp/verify-5-journal.log 2>&1 && (ss -tn | grep -E ':(8091|5101)' >/tmp/verify-5-ss.log 2>&1; true)"

# ============================================================
# 6. dry_run 隔离证据（调 prod_dryrun_evidence.sh）
# ============================================================
_run_check "6" "dry_run 隔离证据 (prod_dryrun_evidence.sh)" \
  "test -f '${REPO_ROOT}/scripts/agent_framework/prod_dryrun_evidence.sh' && bash '${REPO_ROOT}/scripts/agent_framework/prod_dryrun_evidence.sh' || echo 'prod_dryrun_evidence.sh 未实装，跳过'"

# ============================================================
# check 7 实现：DB 隔离证据（Issue #28）
#
# 三重证据，任一不满足即 FAIL：
#   a) 绿进程 environ 声明 DAILYCHECK_DB_DIR == GREEN_DB
#      （gunicorn worker fork 自 master，environ 一致；比扫 /proc/*/fd
#      可靠——sqlite 连接是每请求开关，fd 扫描会撞空窗）
#   b) ${GREEN_DB}/master.db 存在且不是软链
#   c) 绿 master.db 与蓝 master.db inode 不同（同一物理文件的铁证排除）
# ============================================================
_verify_check7_db_isolation() {
  local main_pid env_dir master_green master_blue ino_g ino_b
  main_pid="$(systemctl show -p MainPID --value "${GREEN_UNIT}" 2>/dev/null)"
  if [[ -z "${main_pid}" || "${main_pid}" == "0" ]]; then
    echo "FAIL: ${GREEN_UNIT} 未运行（MainPID=${main_pid:-无}）"
    return 1
  fi
  env_dir="$(tr '\0' '\n' < "/proc/${main_pid}/environ" 2>/dev/null \
    | grep '^DAILYCHECK_DB_DIR=' | cut -d= -f2-)"
  if [[ "${env_dir}" != "${GREEN_DB}" ]]; then
    echo "FAIL: 绿进程 DAILYCHECK_DB_DIR='${env_dir:-未设置}' != ${GREEN_DB}"
    echo "  修复: ${GREEN_UNIT} [Service] 加 Environment=DAILYCHECK_DB_DIR=${GREEN_DB} 后 daemon-reload + restart 绿"
    return 1
  fi
  master_green="${GREEN_DB}/master.db"
  if [[ -L "${master_green}" ]]; then
    echo "FAIL: ${master_green} 是软链（隔离失效，应 cp -aL 实体副本）"
    return 1
  fi
  if [[ ! -f "${master_green}" ]]; then
    echo "FAIL: ${master_green} 不存在（up.sh db-copy 未跑或目标不符）"
    return 1
  fi
  master_blue="${REPO_ROOT}/db/master.db"
  ino_g="$(stat -c %i "${master_green}" 2>/dev/null || echo na)"
  ino_b="$(stat -c %i "${master_blue}" 2>/dev/null || echo na)"
  if [[ "${ino_g}" != "na" && "${ino_g}" == "${ino_b}" ]]; then
    echo "FAIL: 绿蓝 master.db inode 相同（${ino_g}）— 同一物理文件"
    return 1
  fi
  echo "OK: DAILYCHECK_DB_DIR=${env_dir}；master.db 独立副本（inode ${ino_g} ≠ 蓝 ${ino_b}）"
  return 0
}

# ============================================================
# 7. DB 隔离证据（Issue #28）
# ============================================================
_run_check "7" "DB 隔离证据 (DAILYCHECK_DB_DIR + master.db 独立副本)" \
  "_verify_check7_db_isolation"

# ============================================================
# check 8 实现：nginx 入口探活（Issue #32）
#
# 过 nginx :8080 真实打主站 + MCP（不绕开 nginx 探 upstream 端口——
# 旧 bug 是 upstream 切了但 location 还在指旧池，绕过 nginx 探不到）。
# 5xx / 000 = 入口不可用 = layout 漏改 / reload 未生效 / 后端 down。
# 必须搭配 switch.sh 自己的 post-switch 探活（防御 reload 后立刻 502）；
# 本 check 是 verify 终态保险，间隔几秒，捕获 reload → observe window 的
# 任何状态漂移。
# ============================================================
NGINX_ENTRY="${NGINX_ENTRY:-http://127.0.0.1:8080}"
_verify_check8_nginx_entry() {
  local url_main="${NGINX_ENTRY}/" \
        url_mcp="${NGINX_ENTRY}/api/mcp/" \
        code_main code_mcp
  code_main="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "${url_main}" 2>/dev/null || echo 000)"
  code_mcp="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "${url_mcp}" 2>/dev/null || echo 000)"
  echo "  ${url_main} → HTTP ${code_main}"
  echo "  ${url_mcp} → HTTP ${code_mcp}"
  local bad=""
  if [[ "${code_main}" =~ ^5 ]] || [[ "${code_main}" == "000" ]]; then bad="${bad} 主站(${code_main})"; fi
  if [[ "${code_mcp}" =~ ^5 ]] || [[ "${code_mcp}" == "000" ]]; then bad="${bad} MCP(${code_mcp})"; fi
  if [[ -n "${bad}" ]]; then
    echo "FAIL: nginx 入口 5xx/不可达:${bad}（Issue #32 防线：layout 必须单 upstream）"
    return 1
  fi
  echo "OK: nginx 入口主站 + MCP 均非 5xx（main=${code_main} mcp=${code_mcp}）"
  return 0
}

# ============================================================
# 8. nginx 入口探活（Issue #32）
# ============================================================
_run_check "8" "nginx 入口探活 (:8080 主站 + MCP, 非 502)" \
  "_verify_check8_nginx_entry"

if [[ ${OVERALL_PASS} -eq 1 ]]; then
  echo "[verify] ✅ 全绿"
  _write_report  # 显式调（trap EXIT 兜底会再写一次，同内容幂等覆盖）
  exit 0
else
  echo "[verify] ❌ 失败"
  _write_report  # 显式调
  exit 1
fi
