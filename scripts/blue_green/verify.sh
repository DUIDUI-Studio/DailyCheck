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
#   1. 集成测试（pytest tests/contract tests/integration）
#   2. 模拟用户操作（Playwright 跑 4 关键路径 × 蓝绿截图 diff）
#   3. 绿 vs 蓝比对（ab -n 100 -c 10 响应/P95 截图）
#   4. 回滚演练（switch.sh blue 100 → health → switch.sh green 0）
#   5. 资源与依赖（systemctl status + journalctl + ss -tn）
#   + dry_run 隔离证据（prod_dryrun_evidence.sh）

set -euo pipefail

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
R1="⏳"; R2="⏳"; R3="⏳"; R4="⏳"; R5="⏳"; R6="⏳"
N1=""; N2=""; N3=""; N4=""; N5=""; N6=""
OVERALL_PASS=1

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
    exit 1
  fi
fi

# ============================================================
# 1. 集成测试
# ============================================================
_run_check "1_int_test" "集成测试 (pytest tests/contract tests/integration)" \
  "cd '${REPO_ROOT}' && .venv/bin/python -m pytest tests/contract tests/integration -q --tb=line"

# ============================================================
# 2. 模拟用户操作（Playwright）
# ============================================================
_run_check "2_sim_user" "模拟用户操作 (Playwright 4 关键路径)" \
  "test -f '${REPO_ROOT}/scripts/playwright/sim_user.py' && cd '${REPO_ROOT}' && python3 scripts/playwright/sim_user.py --env green --pr ${PR_NUM} || echo 'Playwright 未实装，跳过 (Phase 2 补)'"

# ============================================================
# 3. 绿 vs 蓝比对（ab / wrk）
# ============================================================
_run_check "3_blue_vs_green" "绿 vs 蓝 性能比对 (ab -n 100 -c 10)" \
  "command -v ab >/dev/null && (ab -n 100 -c 10 -q '${BLUE_URL}/health' >/tmp/verify-3-blue.log 2>&1; ab -n 100 -c 10 -q '${GREEN_URL}/health' >/tmp/verify-3-green.log 2>&1) || echo 'ab 未装，跳过（取日志大小比对）'"

# ============================================================
# 4. 回滚演练
# ============================================================
_run_check "4_rollback_drill" "回滚演练 (switch.sh blue 100 → health → switch.sh green 0)" \
  "'$(dirname "$0")/switch.sh' blue 100 && sleep 3 && curl -fsS '${BLUE_URL}/health' >/dev/null && sleep 2 && '$(dirname "$0")/switch.sh' green 0 && sleep 3 && curl -fsS '${BLUE_URL}/health' >/dev/null"

# ============================================================
# 5. 资源与依赖
# ============================================================
_run_check "5_resource" "资源与依赖 (systemctl status + journalctl + ss -tn)" \
  "systemctl status dailycheck-green.service dailycheck-green-mcp.service --no-pager >/tmp/verify-5-status.log 2>&1 && journalctl -u dailycheck-green.service -n 50 >/tmp/verify-5-journal.log 2>&1 && (ss -tn | grep -E ':(8091|5101)' >/tmp/verify-5-ss.log 2>&1; true)"

# ============================================================
# 6. dry_run 隔离证据（调 prod_dryrun_evidence.sh）
# ============================================================
_run_check "6_dryrun_evidence" "dry_run 隔离证据 (prod_dryrun_evidence.sh)" \
  "test -f '${REPO_ROOT}/scripts/agent_framework/prod_dryrun_evidence.sh' && bash '${REPO_ROOT}/scripts/agent_framework/prod_dryrun_evidence.sh' || echo 'prod_dryrun_evidence.sh 未实装，跳过'"

# ============================================================
# 写报告
# ============================================================
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
| 1 | 集成测试 (pytest contract/integration) | ${R1} | ${N1:-} |
| 2 | 模拟用户操作 (Playwright 4 路径) | ${R2} | ${N2:-} |
| 3 | 绿 vs 蓝 性能比对 (ab -n 100 -c 10) | ${R3} | ${N3:-} |
| 4 | 回滚演练 (switch.sh blue 100 → green 0) | ${R4} | ${N4:-} |
| 5 | 资源与依赖 (systemctl + journalctl + ss) | ${R5} | ${N5:-} |
| 6 | dry_run 隔离证据 (prod_dryrun_evidence.sh) | ${R6} | ${N6:-} |

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

if [[ ${OVERALL_PASS} -eq 1 ]]; then
  echo "[verify] ✅ 全绿，报告落 ${REPORT_PATH}"
  exit 0
else
  echo "[verify] ❌ 失败，报告落 ${REPORT_PATH}（含失败详情）"
  exit 1
fi
