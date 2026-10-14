#!/usr/bin/env bash
# blue_green/rollback.sh —— 秒级切回蓝
#
# 用法: ./scripts/blue_green/rollback.sh
#
# 状态: v0.2 骨架（systemd 路径，2026-10-13 修订）
# 触发：绿环境出现异常（错误率上升、P95 超阈、健康检查失败）
# 行为：nginx upstream 切回 蓝100% / 绿0%（即调用 switch.sh blue 100），
#       绿 systemd unit 留着取证（待人工调查）。
# 设计：docs/agent-framework/v0.1.md §四.2 第 5 项（回滚演练）
#
# 设计原则：
#   - **不停绿 systemd unit**（保留取证状态）
#   - 仅切流量到 100% 蓝（绿色保留但不服务）
#   - 取证快照：journalctl + DB 拷贝（如果 DB 写入了虽然 DRY_RUN 应该没写）
#   - 告警 Eric
#
# v0.3 修订（2026-10-13 Eric 拍板）：
#   - 所有路径走 env 变量 + 默认值（EVIDENCE_DIR / GREEN_SERVICE_UNIT）

set -euo pipefail

# 配置
EVIDENCE_BASE="${EVIDENCE_BASE:-/var/log/dailycheck}"
GREEN_SERVICE_UNIT="${GREEN_SERVICE_UNIT:-dailycheck-green.service}"

# ============================================================
# 骨架阶段：禁止执行
# ============================================================
echo "[rollback] NOT YET IMPLEMENTED — switch.sh 实装后才能触发" >&2
echo "[rollback] 实际行为（实装后）：" >&2
echo "[rollback]   1. 调 switch.sh blue 100（nginx upstream 切回 100% 蓝）" >&2
echo "[rollback]   2. 绿 systemd unit 保留运行（取证状态）" >&2
echo "[rollback]   3. 抓 journalctl -u dailycheck-green 最近 1000 行" >&2
echo "[rollback]   4. cp /var/lib/dailycheck-green 到 /var/log/dailycheck/evidence/" >&2
echo "[rollback]   5. 通知 Eric（Telegram / 邮件）" >&2
echo "[rollback] 详见 scripts/blue_green/README.md" >&2
exit 78

# ============================================================
# 实现阶段（switch.sh 实装后取消上面 exit 78）
# ============================================================
#
# EVIDENCE_DIR="/var/log/dailycheck/rollback-$(date +%Y%m%d-%H%M%S)"
# mkdir -p "${EVIDENCE_DIR}"
#
# echo "[rollback] 切流量到 100% 蓝 ..."
# "$(dirname "$0")/switch.sh" blue 100
#
# echo "[rollback] 抓绿 systemd 日志 ..."
# journalctl -u dailycheck-green.service --since "1 hour ago" > "${EVIDENCE_DIR}/journalctl.log" 2>/dev/null || true
#
# echo "[rollback] cp 绿 DB 留证 ..."
# if [[ -d /var/lib/dailycheck-green ]]; then
#   cp -a /var/lib/dailycheck-green "${EVIDENCE_DIR}/" || true
# fi
#
# cat > "${EVIDENCE_DIR}/rollback-report.md" <<EOF
# # 回滚事件（$(date -u +"%Y-%m-%dT%H:%M:%SZ")）
#
# - 触发: $0（人工 / 监控告警）
# - 切流量: 100% 蓝
# - 绿 systemd: 保留（取证）
# - 证据目录: ${EVIDENCE_DIR}
#
# 待人工调查后清理。
# EOF
#
# echo "[rollback] ✅ 已切回 100% 蓝，证据落 ${EVIDENCE_DIR}"
# echo "[rollback] 待人工调查后清理绿 systemd + DB"