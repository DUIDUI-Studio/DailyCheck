#!/usr/bin/env bash
# blue_green/verify.sh —— 跑五项生产验证
#
# 用法: ./scripts/blue_green/verify.sh <pr-num>
#
# 状态: v0.2 骨架（systemd 路径，2026-10-13 修订）
# 输出: backups/prod-verifications/<date>-pr-<num>.md（套 templates/05 模板）
# 退出码:
#   0  全绿可切流量
#   1  验证失败
#   78 骨架阶段，未实装（占位）
#
# ⚠️ Prod Agent 反馈警告：v0.1 阶段此脚本 exit 0 但实际生成占位报告
#   （5 项检查全 ⚠️ TODO），形同空跑。v0.2 改为 exit 78 明确拒绝触发。
#
# 设计依据：v0.1.md §四.2 五项检查 + templates/05-production-verification.md
#
# v0.3 修订（2026-10-13 Eric 拍板）：
#   - 所有路径/URL 走 env 变量 + 默认值（GREEN_URL / REPORT_DIR）

set -euo pipefail

PR_NUM="${1:-}"
if [[ -z "$PR_NUM" ]]; then
  echo "usage: $0 <pr-num>" >&2
  exit 64
fi

# 配置（与 deploy_vps.sh 模板化约定对齐）
REPORT_DIR="${REPORT_DIR:-backups/prod-verifications}"
REPORT_PATH="${REPORT_DIR}/$(date +%Y%m%d)-pr-${PR_NUM}.md"
GREEN_URL="${GREEN_URL:-http://127.0.0.1:${GREEN_PORT:-8091}}"
BLUE_URL="${BLUE_URL:-http://127.0.0.1:${BLUE_PORT:-8090}}"

# ============================================================
# 骨架阶段：禁止执行
# ============================================================
echo "[verify] NOT YET IMPLEMENTED — 五项检查实装后才能触发" >&2
echo "[verify] 待实装的检查项：" >&2
echo "[verify]   1. 集成测试 (pytest tests/contract tests/integration)" >&2
echo "[verify]   2. 模拟用户操作 (Playwright 关键路径 × 蓝绿互存 diff)" >&2
echo "[verify]   3. 绿 vs 蓝比对 (ab -n 1000 -c 10 + 截图 diff < 阈值)" >&2
echo "[verify]   4. 回滚演练 (switch.sh blue 100 → health → switch.sh green 0)" >&2
echo "[verify]   5. 资源与依赖 (systemctl status + journalctl + ss -tn)" >&2
echo "[verify]   + dry_run 隔离证据 (调 prod_dryrun_evidence.sh)" >&2
echo "[verify] 详见 scripts/blue_green/README.md + docs/agent-framework/templates/05-production-verification.md" >&2
exit 78

# ============================================================
# 实现阶段（五项实装后取消上面 exit 78）
# ============================================================
#
# mkdir -p "$(dirname "${REPORT_PATH}")"
#
# # ----- 1. 集成测试 -----
# echo "[verify] step 1/5: 集成测试 ..."
# cd /opt/dailycheck
# source .venv/bin/activate
# pytest tests/contract tests/integration -q --tb=short > /tmp/verify-int-${PR_NUM}.log 2>&1
# INT_RESULT=$?
#
# # ----- 2. 模拟用户操作 -----
# echo "[verify] step 2/5: 模拟用户操作 ..."
# # Playwright 跑 4 条路径（login → items → restock → outbound）绿蓝双跑 + 截图
# # python3 scripts/playwright/sim_user.py --env blue --pr ${PR_NUM}
# # python3 scripts/playwright/sim_user.py --env green --pr ${PR_NUM}
# # diff < 0.5% 通过
#
# # ----- 3. 绿 vs 蓝比对 -----
# echo "[verify] step 3/5: 绿 vs 蓝比对 ..."
# # ab -n 1000 -c 10 http://127.0.0.1:8090/  (蓝基准)
# # ab -n 1000 -c 10 http://127.0.0.1:8091/  (绿)
# # P95 diff ≤ 20%
#
# # ----- 4. 回滚演练 -----
# echo "[verify] step 4/5: 回滚演练 ..."
# "$(dirname "$0")/switch.sh" blue 100
# sleep 5
# curl -fsS http://localhost:8090/health >/dev/null || { echo "蓝 health fail"; exit 1; }
# "$(dirname "$0")/switch.sh" green 0
# sleep 5
# curl -fsS http://localhost:8091/health >/dev/null || { echo "绿 health fail"; exit 1; }
# # 切回原始状态（5% 绿）
# "$(dirname "$0")/switch.sh" green 5
#
# # ----- 5. 资源与依赖 -----
# echo "[verify] step 5/5: 资源与依赖 ..."
# systemctl status dailycheck-green.service > /tmp/verify-status-${PR_NUM}.log 2>&1
# journalctl -u dailycheck-green.service --since "5 min ago" > /tmp/verify-journal-${PR_NUM}.log 2>&1
# # 无 error 级别日志
#
# # ----- 出报告 -----
# cat > "${REPORT_PATH}" <<EOF
# # 生产验证报告 — PR #${PR_NUM}（$(date -u +"%Y-%m-%dT%H:%M:%SZ")）
#
# | 检查项 | 状态 | 备注 |
# |--------|------|------|
# | 1. 集成测试 | $([[ ${INT_RESULT} -eq 0 ]] && echo "✅" || echo "❌") | exit=${INT_RESULT} |
# | 2. 模拟用户操作 | TODO | |
# | 3. 绿 vs 蓝比对 | TODO | |
# | 4. 回滚演练 | TODO | |
# | 5. 资源与依赖 | TODO | |
# | 6. dry_run 证据 | TODO（调 prod_dryrun_evidence.sh） | |
#
# ## 决策
# - [ ] 通过 → 切流量 5% → 50% → 100%
# - [ ] 打回 → 留绿取证，PR 不合并
# EOF
#
# if [[ ${INT_RESULT} -eq 0 ]]; then
#   exit 0
# else
#   exit 1
# fi