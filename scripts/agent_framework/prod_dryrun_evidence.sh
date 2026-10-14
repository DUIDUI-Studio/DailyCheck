#!/usr/bin/env bash
# scripts/agent_framework/prod_dryrun_evidence.sh
# 端到端 dry_run 隔离证据采集
#
# 用途：验证绿环境（DRY_RUN=true）的写操作不污染生产 DB。
# 方法：跑前采集 → 跑 5 项写 → 跑后采集 → diff 必须为 0 → 通过。
#
# 用法：
#   bash scripts/agent_framework/prod_dryrun_evidence.sh
#
# 前置条件：
#   1. 已实施 DRY_RUN 中间件（app.py + blueprints 写路径走 if DRY_RUN: log_only return）
#   2. dailycheck-green.service 已起（:8091 + DRY_RUN=true）
#   3. 蓝环境 (:8090) 正常运行（基准对比）
#
# 决策依据：v0.1.md §四.2 报告 05 "写流量隔离证据" + Prod Agent 反馈 §三.4
# 设计：方案 B（DRY_RUN 中间件）—— 详见 docs/agent-framework/v0.1.md §八

set -uo pipefail

# ============================================================
# 骨架阶段：禁止执行，待 DRY_RUN 中间件 PoC 通过后实装
# ============================================================
echo "[evidence] NOT YET IMPLEMENTED — DRY_RUN 中间件 PoC 通过后再实装" >&2
echo "[evidence] 前置条件：" >&2
echo "[evidence]   1. app.py + blueprints 写路径加 if DRY_RUN: log_only" >&2
echo "[evidence]   2. dailycheck-green.service systemd unit（:8091 + DRY_RUN=true）" >&2
echo "[evidence]   3. 蓝 (:8090) 正常运行" >&2
echo "[evidence] 详见 docs/agent-framework/v0.1.md §八" >&2
exit 78

# ============================================================
# 实现阶段（PoC 通过后取消上面 exit 78 并取消下面注释）
# ============================================================
#
# BLUE_URL="http://127.0.0.1:8090"
# GREEN_URL="http://127.0.0.1:8091"
# BLUE_DB="/var/lib/dailycheck"
# GREEN_DB="/var/lib/dailycheck-green"
# EVIDENCE_DIR="/var/log/dailycheck/dryrun-evidence/$(date +%Y%m%d-%H%M%S)"
# mkdir -p "${EVIDENCE_DIR}"
#
# # 步骤 1：跑前采集（蓝 + 绿同源 DB，采集应一致）
# echo "[evidence] step 1/5: pre-run snapshot ..."
# TABLES=(users items categories inventory_transactions production_batches store_orders)
# for table in "${TABLES[@]}"; do
#   sqlite3 "${BLUE_DB}/master.db" "SELECT count(*) FROM ${table}" \
#     > "${EVIDENCE_DIR}/pre-master-${table}.txt" 2>/dev/null || true
# done
# # 每个仓 db
# for wh_db in "${BLUE_DB}/warehouses/"*.db; do
#   wh_name=$(basename "${wh_db}" .db)
#   for table in items production; do
#     sqlite3 "${wh_db}" "SELECT count(*) FROM ${table}" \
#       > "${EVIDENCE_DIR}/pre-${wh_name}-${table}.txt" 2>/dev/null || true
#   done
# done
#
# # 步骤 2：跑 5 项写操作（通过绿环境 HTTP）
# echo "[evidence] step 2/5: 5 write operations on green ..."
#
# # 2.1 创建用户（蓝侧 admin session，绿测 API）
# # 2.2 创建品项
# # 2.3 入库
# # 2.4 出库
# # 2.5 盘点
#
# for op in "create_user" "create_item" "restock" "outbound" "stocktake"; do
#   echo "[evidence]   -> ${op}"
#   # curl -X POST "${GREEN_URL}/api/${op}" ... | tee "${EVIDENCE_DIR}/op-${op}.json"
#   # 期望：返回 200 但 DB 不变；DRY_RUN=true 时 INSERT 走 log_only
# done
#
# # 步骤 3：跑后采集（同 step 1）
# echo "[evidence] step 3/5: post-run snapshot ..."
# for table in "${TABLES[@]}"; do
#   sqlite3 "${BLUE_DB}/master.db" "SELECT count(*) FROM ${table}" \
#     > "${EVIDENCE_DIR}/post-master-${table}.txt" 2>/dev/null || true
# done
# for wh_db in "${BLUE_DB}/warehouses/"*.db; do
#   wh_name=$(basename "${wh_db}" .db)
#   for table in items production; do
#     sqlite3 "${wh_db}" "SELECT count(*) FROM ${table}" \
#       > "${EVIDENCE_DIR}/post-${wh_name}-${table}.txt" 2>/dev/null || true
#   done
# done
#
# # 步骤 4：diff 必须为 0
# echo "[evidence] step 4/5: diff check ..."
# DIFF_COUNT=0
# for f in "${EVIDENCE_DIR}"/pre-*.txt; do
#   base=$(basename "$f")
#   if ! diff -q "$f" "${EVIDENCE_DIR}/post-${base#pre-}" >/dev/null; then
#     echo "[evidence] ❌ ${base} 不一致" >&2
#     DIFF_COUNT=$((DIFF_COUNT + 1))
#   fi
# done
#
# # 步骤 5：出报告
# echo "[evidence] step 5/5: report ..."
# if [[ ${DIFF_COUNT} -eq 0 ]]; then
#   cat > "${EVIDENCE_DIR}/report.md" <<EOF
# # dry_run 隔离证据（$(date -u +"%Y-%m-%dT%H:%M:%SZ")）
#
# - 蓝 DB（基准）: ${BLUE_DB}
# - 绿 DB（DRY_RUN=true）: ${GREEN_DB}
# - 5 项写操作: 已跑
# - 采集 diff: **0 条不一致** ✅
# - 证据目录: ${EVIDENCE_DIR}
#
# 结论：DRY_RUN 隔离生效，绿环境未污染生产 DB。
# EOF
#   echo "[evidence] ✅ 通过：DIFF_COUNT=0，证据落 ${EVIDENCE_DIR}/report.md"
#   exit 0
# else
#   echo "[evidence] ❌ 失败：DIFF_COUNT=${DIFF_COUNT}"
#   exit 1
# fi