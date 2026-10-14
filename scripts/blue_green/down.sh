#!/usr/bin/env bash
# blue_green/down.sh —— 清理绿环境
#
# 用法: ./scripts/blue_green/down.sh [--keep-blue]
#
# 状态: v0.2 骨架（systemd 路径，2026-10-13 修订）
#
# 默认行为：停绿 + 删绿 DB + 删绿 unit
# --keep-blue：仅停绿 systemd（PR 合入后蓝降 standby 7 天，保留绿取证）
#
# 触发：
#   - 验证失败后清理（取证已导出）
#   - PR 合入 + 7 天 standby 期到期（需 cron 触发，见 §cron）
#   - 人工取消（Eric 主动）
#
# cron 示例（7 天 standby 到期）：
#   0 0 * * * /root/DailyCheck/scripts/blue_green/cron_standby_expire.sh  # 待补
#
# v0.3 修订（2026-10-13 Eric 拍板）：
#   - 所有路径走 env 变量 + 默认值（GREEN_SERVICE_UNIT / GREEN_DB）
#   - cron 路径 /opt → /root（实测 host 路径）

set -euo pipefail

KEEP_BLUE=0
for arg in "$@"; do
  case "${arg}" in
    --keep-blue) KEEP_BLUE=1 ;;
    *) echo "unknown arg: ${arg}" >&2; exit 64 ;;
  esac
done

# 配置（与 deploy_vps.sh 模板化约定对齐）
GREEN_UNIT="${GREEN_SERVICE_UNIT:-dailycheck-green.service}"
GREEN_DB="${GREEN_DB:-/var/lib/dailycheck-green}"

# ============================================================
# 骨架阶段：禁止执行
# ============================================================
echo "[down] NOT YET IMPLEMENTED — 手工 systemctl stop 仍可用" >&2
echo "[down] 实际行为（实装后）：" >&2
echo "[down]   1. systemctl stop ${GREEN_UNIT}" >&2
if [[ "${KEEP_BLUE}" -eq 1 ]]; then
  echo "[down]   2. 保留 ${GREEN_DB} 取证（7 天 standby 期到期才删）"
else
  echo "[down]   2. rm -rf ${GREEN_DB}"
fi
echo "[down] 详见 scripts/blue_green/README.md" >&2
exit 78

# ============================================================
# 实现阶段（up.sh 实装后取消上面 exit 78）
# ============================================================
#
# echo "[down] stopping ${GREEN_UNIT} ..."
# systemctl stop "${GREEN_UNIT}" 2>&1 | head -10 || true
#
# if [[ "${KEEP_BLUE}" -eq 1 ]]; then
#   echo "[down] --keep-blue: ${GREEN_DB} 保留 7 天 standby"
# else
#   echo "[down] removing ${GREEN_DB} ..."
#   rm -rf "${GREEN_DB}"
# fi
#
# echo "[down] ✅ done (keep_blue=${KEEP_BLUE})"