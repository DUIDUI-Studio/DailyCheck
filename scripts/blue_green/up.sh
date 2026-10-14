#!/usr/bin/env bash
# blue_green/up.sh —— 起绿环境
#
# 用法: ./scripts/blue_green/up.sh <pr-num> <commit-sha>
#
# 状态: v0.2 骨架（systemd 路径，2026-10-13 修订）。
# 设计依据：v0.1.md §四 + Prod Agent 反馈 §三 + 决策 1+3（DRY_RUN 中间件 + systemd 复用）
#
# 前置条件（实装前必做）：
#   1. DRY_RUN 中间件 PoC 通过（app.py + blueprints 接入）
#   2. dailycheck-green.service systemd unit 创建（见 TODO）
#   3. /var/lib/dailycheck-green/ 目录创建（从蓝 cp）
#   4. /etc/nginx/sites-enabled/dailycheck 加 :8091 upstream block
#
# 设计原则：
#   - 蓝 :8090 不 kill（0 中断）
#   - 绿 :8091 起 gunicorn 2 workers + DRY_RUN=true
#   - 健康检查：/health 已存在（app.py:134）
#
# v0.3 修订（2026-10-13 Eric 拍板）：
#   - SOURCE_DIR 错位修正：/opt/dailycheck → /root/DailyCheck（实测 host 路径）
#   - 复用 deploy_vps.sh 的 DAILYCHECK_* env 变量约定
#   - 加绿 MCP :5101（实测蓝 MCP :5100）
#   - 所有路径/端口/单元走 env 变量 + 默认值，可覆盖

set -euo pipefail

PR_NUM="${1:-}"
COMMIT_SHA="${2:-}"
if [[ -z "$PR_NUM" || -z "$COMMIT_SHA" ]]; then
  echo "usage: $0 <pr-num> <commit-sha>" >&2
  exit 64
fi

# 配置（与 scripts/deploy_vps.sh 模板化约定对齐：DAILYCHECK_* env 变量可覆盖）
APP_DIR="${DAILYCHECK_APP_DIR:-/root/DailyCheck}"               # 实测 host 路径（不是 /opt）
BLUE_DB="${DAILYCHECK_DB_ROOT:-/var/lib/dailycheck}"            # 蓝 DB 根目录
GREEN_PORT="${GREEN_PORT:-8091}"                                # 绿 gunicorn 端口
GREEN_MCP_PORT="${GREEN_MCP_PORT:-5101}"                        # 绿 MCP 端口（实测蓝 :5100）
GREEN_DB="${GREEN_DB:-/var/lib/dailycheck-green}"               # 绿 DB 根目录
SOURCE_DIR="${APP_DIR}"                                         # 复用 APP_DIR
GREEN_UNIT="${GREEN_SERVICE_UNIT:-dailycheck-green.service}"
GREEN_MCP_UNIT="${GREEN_MCP_SERVICE_UNIT:-dailycheck-green-mcp.service}"
GREEN_ENV_FILE="${GREEN_ENV_FILE:-/etc/wdg/dailycheck-green.env}"
GREEN_MCP_ENV_FILE="${GREEN_MCP_ENV_FILE:-/etc/wdg/dailycheck-green-mcp.env}"

# ============================================================
# 骨架阶段：禁止执行
# ============================================================
echo "[up] NOT YET IMPLEMENTED — systemD 路径实装前禁止触发" >&2
echo "[up] 前置条件：" >&2
echo "[up]   1. DRY_RUN 中间件 PoC 通过" >&2
echo "[up]   2. ${GREEN_UNIT} systemd unit 创建" >&2
echo "[up]   3. ${GREEN_DB} 目录创建（从 ${BLUE_DB} cp）" >&2
echo "[up]   4. Nginx 加 :${GREEN_PORT} upstream block" >&2
echo "[up] 详见 scripts/blue_green/README.md" >&2
exit 78

# ============================================================
# 实现阶段（前置条件满足后取消上面 exit 78）
# ============================================================
#
# # 1. 部署源码到 SOURCE_DIR（若 commit SHA 变了；此处假设 Eric 已 git pull）
# cd "${SOURCE_DIR}"
# if [[ "$(git rev-parse HEAD)" != "$COMMIT_SHA" ]]; then
#   echo "[up] ERROR: SOURCE_DIR HEAD != commit-sha" >&2
#   exit 1
# fi
#
# # 2. cp 蓝 DB → 绿 DB（一次性，确保绿读到的不是生产写期间的脏数据）
# echo "[up] copying ${BLUE_DB} -> ${GREEN_DB} ..."
# rm -rf "${GREEN_DB}"
# cp -a "${BLUE_DB}" "${GREEN_DB}"
# chmod -R u+rw "${GREEN_DB}"
#
# # 3. 起绿 systemd unit（监听 :${GREEN_PORT} + DRY_RUN=true + DB_PATH=${GREEN_DB}")
# echo "[up] starting ${GREEN_UNIT} on :${GREEN_PORT} ..."
# systemctl start "${GREEN_UNIT}"
#
# # 4. 健康检查（/health 已存在 app.py:134，期望 200 + JSON）
# echo "[up] health check on :${GREEN_PORT}/health ..."
# for i in {1..30}; do
#   if curl -fsS "http://localhost:${GREEN_PORT}/health" >/dev/null 2>&1; then
#     echo "[up] ✅ green healthy (commit=${COMMIT_SHA:0:7}, port=${GREEN_PORT})"
#     exit 0
#   fi
#   sleep 2
# done
#
# echo "[up] health check timeout (60s)" >&2
# # 取证：保留绿 DB + journalctl
# journalctl -u "${GREEN_UNIT}" -n 100 > "/var/log/dailycheck/green-up-fail-${PR_NUM}.log" 2>/dev/null || true
# exit 1