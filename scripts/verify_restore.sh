#!/usr/bin/env bash
# verify_restore.sh — 备份还原演练（建议每周 cron 一次）。
#
# 取最近一份 dailycheck-db-*.tar.gz → 还原到临时目录 → 起临时 gunicorn 实例
# （独立端口）→ /health 探活 + 关键表抽查 → 清理。任何一步失败退出码非 0。
#
# 环境变量：
#   DAILYCHECK_APP_DIR   应用目录（默认 /root/DailyCheck，借用其代码与 venv）
#   DAILYCHECK_BACKUP_ROOT 备份根目录（默认 /root/backups/auto）
#   DAILYCHECK_DRILL_PORT  演练实例端口（默认 8091）
#   DAILYCHECK_ARCHIVE   指定演练用的 tar 包（默认取最新一份）
set -euo pipefail

APP_DIR="${DAILYCHECK_APP_DIR:-/root/DailyCheck}"
BACKUP_ROOT="${DAILYCHECK_BACKUP_ROOT:-/root/backups/auto}"
DRILL_PORT="${DAILYCHECK_DRILL_PORT:-8091}"
ARCHIVE="${DAILYCHECK_ARCHIVE:-}"

log() { echo "[drill $(date +%F\ %T)] $*"; }
fail() { echo "[drill] FAIL: $*" >&2; exit 1; }
cleanup() {
  [ -n "${GUNICORN_PID:-}" ] && kill "$GUNICORN_PID" 2>/dev/null || true
  [ -n "${DRILL_DIR:-}" ] && rm -rf "$DRILL_DIR"
}
trap cleanup EXIT

# 1) 选备份包
if [ -z "$ARCHIVE" ]; then
  ARCHIVE=$(ls -1t "$BACKUP_ROOT"/dailycheck-db-*.tar.gz 2>/dev/null | head -1)
  [ -n "$ARCHIVE" ] || fail "找不到备份包: $BACKUP_ROOT/dailycheck-db-*.tar.gz"
fi
[ -f "$ARCHIVE" ] || fail "备份包不存在: $ARCHIVE"
log "演练备份包: $ARCHIVE"

# 2) 组装演练实例。
# ⚠ config.BASE_DIR = Path(__file__).resolve().parent —— 软链会被 resolve 到
#   真实 APP_DIR，演练实例就会读到线上库。所以：单文件一律拷贝；db/ 用真实目录
#   （.py 从 APP_DIR 拷入，.db 由备份包解压提供）；纯代码目录才可软链。
DRILL_DIR=$(mktemp -d /tmp/dailycheck-drill-XXXX)
for item in blueprints mcp_server templates static migrations; do
  ln -s "$APP_DIR/$item" "$DRILL_DIR/$item"
done
# 根目录 .py 全量拷贝（app/cli/config/permissions/rate_limit_pure/…）
cp "$APP_DIR"/*.py "$DRILL_DIR/" 2>/dev/null || true
for item in requirements.txt pyproject.toml; do
  [ -f "$APP_DIR/$item" ] && cp "$APP_DIR/$item" "$DRILL_DIR/"
done
mkdir -p "$DRILL_DIR/db"
cp "$APP_DIR"/db/*.py "$DRILL_DIR/db/" 2>/dev/null || true
tar -xzf "$ARCHIVE" -C "$DRILL_DIR"
log "已还原到 $DRILL_DIR"

# 3) 结构校验：master 存在、user_version、关键表抽查
PY="$APP_DIR/.venv/bin/python"
[ -x "$PY" ] || PY=$(command -v python3)
"$PY" - "$DRILL_DIR" <<'PYEOF'
import sqlite3, sys
from pathlib import Path

root = Path(sys.argv[1])
master = root / "db" / "master.db"
if not master.exists():
    sys.exit("还原后无 master.db")
conn = sqlite3.connect(master)
users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
warehouses = conn.execute("SELECT COUNT(*) FROM warehouses").fetchone()[0]
version = conn.execute("PRAGMA user_version").fetchone()[0]
conn.close()
if users == 0:
    sys.exit("users 表为空——疑似坏备份")
print(f"[drill]   master.db: users={users}, warehouses={warehouses}, user_version={version}")
PYEOF

# 4) 起临时实例 + 健康检查
log "起临时实例 :$DRILL_PORT"
cd "$DRILL_DIR"
"$APP_DIR/.venv/bin/gunicorn" -w 1 -b "127.0.0.1:$DRILL_PORT" app:app \
    --timeout 30 --error-logfile /tmp/drill-error.log \
    > /tmp/drill-access.log 2>&1 &
GUNICORN_PID=$!
ok=0
for _ in $(seq 1 15); do
  sleep 2
  if curl -fsS "http://127.0.0.1:$DRILL_PORT/health" > /dev/null 2>&1; then
    ok=1
    break
  fi
done
[ "$ok" = "1" ] || fail "演练实例健康检查未通过（看 /tmp/drill-error.log）"
log "健康检查通过 — 备份可还原、可启动"
