#!/usr/bin/env bash
# deploy_vps.sh — 在目标机上执行（ci.yml deploy job 通过 `ssh "bash -s <rel>" < 本文件` 调起）。
#
# 语义：备份数据库 → 备份当前代码 → rsync 切换 → flask migrate up →
#       systemctl 重启 → 健康检查 → 任一步失败自动回滚代码并重启。
# 铁律：数据库目录（db/ backups/ *.log *.db）在任何 rsync 中都不触碰；
#       回滚只回滚代码；schema 回退走 backups/ 里的 sqlite 备份（runbook）。
#
# 环境变量（默认值对准生产 VPS）：
#   DAILYCHECK_APP_DIR      应用目录        /root/DailyCheck
#   DAILYCHECK_SERVICE_UNIT systemd unit    dailycheck
#   DAILYCHECK_HEALTH_URL   健康检查        http://127.0.0.1:8090/health
#   DAILYCHECK_BACKUP_ROOT  备份根目录      /root/backups
#   DAILYCHECK_HEALTH_TIMEOUT 健康检查窗口秒数 60
#   $1                      新代码目录（已解压的 release）
set -euo pipefail

APP_DIR="${DAILYCHECK_APP_DIR:-/root/DailyCheck}"
SERVICE_UNIT="${DAILYCHECK_SERVICE_UNIT:-dailycheck}"
HEALTH_URL="${DAILYCHECK_HEALTH_URL:-http://127.0.0.1:8090/health}"
BACKUP_ROOT="${DAILYCHECK_BACKUP_ROOT:-/root/backups}"
HEALTH_TIMEOUT="${DAILYCHECK_HEALTH_TIMEOUT:-60}"
SRC="${1:?用法: bash deploy_vps.sh <新代码目录>}"

TIMESTAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_DIR="$BACKUP_ROOT/$TIMESTAMP"
RSYNC_EXCLUDES=(--exclude '*.db' --exclude 'backups/' --exclude '.venv/'
                --exclude '*.log' --exclude '.git/' --exclude 'node_modules/'
                --exclude '.workbuddy/' --exclude '.claude/')

log() { echo "[deploy] $*"; }
die() { echo "[deploy] FAIL: $*" >&2; exit 1; }

rollback() {
  log "回滚代码 → ${CODE_BACKUP}"
  local tmp
  tmp=$(mktemp -d)
  tar -xzf "$CODE_BACKUP" -C "$tmp"
  rsync -a --delete "${RSYNC_EXCLUDES[@]}" "$tmp/" "$APP_DIR/"
  rm -rf "$tmp"
  systemctl restart "$SERVICE_UNIT"
  log "已回滚并重启。请人工排查，数据库备份在 ${BACKUP_DIR}/db"
}

# 1) 数据库热备份（Python sqlite3 backup API，在线安全，不依赖 sqlite3 CLI）
log "1/5 备份数据库 → $BACKUP_DIR/db"
PY="$APP_DIR/.venv/bin/python"
[ -x "$PY" ] || PY=$(command -v python3)
"$PY" - "$BACKUP_DIR" "$APP_DIR" <<'PYEOF'
import sqlite3, sys
from pathlib import Path

backup_dir, app_dir = Path(sys.argv[1]), Path(sys.argv[2])
dbs = sorted(p for p in app_dir.glob("db/**/*.db") if p.is_file())
if not dbs:
    print("[deploy]   警告: db/ 下没有 .db 文件", flush=True)
for db in dbs:
    rel = db.relative_to(app_dir)
    dest = backup_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(db)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    print(f"[deploy]   backed up {rel}", flush=True)
PYEOF

# 2) 当前代码备份（回滚源）。⚠ 只排数据（*.db）与大目录，db/ Python 包必须进备份，
#    否则回滚会恢复出缺包的代码树（演练踩过：2026-10-10 dev 演练）。
log "2/5 备份当前代码"
CODE_BACKUP="$BACKUP_DIR/code.tar.gz"
tar --exclude='./*.db' --exclude='./backups' --exclude='./.venv' \
    --exclude='./node_modules' --exclude='./.git' --exclude='./.claude' \
    --exclude='./.workbuddy' --exclude='./issue-reports' --exclude='./graphify-out' \
    -czf "$CODE_BACKUP" -C "$APP_DIR" .

# 3) rsync 切换代码（--delete 清理已删除文件；*.db/log/venv 等排除项保护数据不被覆盖或删除）
log "3/5 rsync 新代码 $SRC → $APP_DIR"
rsync -a --delete "${RSYNC_EXCLUDES[@]}" "$SRC/" "$APP_DIR/"

# 4) schema 迁移（失败即回滚）
log "4/5 flask migrate up"
if ! (cd "$APP_DIR" && ./.venv/bin/python -m flask --app app migrate up); then
  log "迁移失败"
  rollback
  die "迁移失败，已回滚"
fi

# 5) 重启 + 健康检查（失败即回滚）
log "5/5 重启 $SERVICE_UNIT + 健康检查"
systemctl restart "$SERVICE_UNIT"
ok=0
for _ in $(seq 1 $((HEALTH_TIMEOUT / 2))); do
  sleep 2
  if curl -fsS "$HEALTH_URL" > /dev/null 2>&1; then ok=1; break; fi
done

if [ "$ok" = "1" ]; then
  log "OK — 新版本上线成功，备份在 $BACKUP_DIR"
  exit 0
fi

log "健康检查 ${HEALTH_TIMEOUT}s 内未通过"
rollback
die "健康检查失败，已回滚"
