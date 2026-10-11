#!/usr/bin/env bash
# backup_all.sh — 每日全库备份（cron 调用；deploy_vps.sh 迁移前备份是同一 API 的另一个调用方）。
#
# 动作：master.db + db/ 下所有仓库 db 逐个 sqlite3 backup API 热备
#       → 全部 PRAGMA integrity_check 校验 → 打 tar.gz → 清理过期备份。
# 退出码非 0 = 有备份或校验失败（cron 环境可接告警）。
#
# 环境变量：
#   DAILYCHECK_APP_DIR   应用目录（默认 /root/DailyCheck）
#   DAILYCHECK_BACKUP_ROOT 备份根目录（默认 /root/backups/auto）
#   DAILYCHECK_RETENTION_DAYS 保留天数（默认 14）
#   DAILYCHECK_UPLOAD_CMD  可选：备份完成后执行的上传命令模板，
#                          其中 {archive} 会被替换为 tar 包路径。
#                          例: 'rclone copy {archive} oss:dailycheck-backups/'
set -euo pipefail

APP_DIR="${DAILYCHECK_APP_DIR:-/root/DailyCheck}"
BACKUP_ROOT="${DAILYCHECK_BACKUP_ROOT:-/root/backups/auto}"
RETENTION_DAYS="${DAILYCHECK_RETENTION_DAYS:-14}"
UPLOAD_CMD="${DAILYCHECK_UPLOAD_CMD:-}"
DAY=$(date +%Y-%m-%d)
DEST="$BACKUP_ROOT/$DAY"
PY="$APP_DIR/.venv/bin/python"
[ -x "$PY" ] || PY=$(command -v python3)

log() { echo "[backup $(date +%F\ %T)] $*"; }
fail() { echo "[backup] FAIL: $*" >&2; exit 1; }

[ -d "$APP_DIR/db" ] || fail "db 目录不存在: $APP_DIR/db"

# 1) 热备全库 + 完整性校验（一步完成）
log "热备 db/ 全库 → $DEST"
mkdir -p "$DEST"
"$PY" - "$APP_DIR" "$DEST" <<'PYEOF'
import sqlite3, sys
from pathlib import Path

app_dir, dest = Path(sys.argv[1]), Path(sys.argv[2])
dbs = sorted(p for p in (app_dir / "db").glob("**/*.db") if p.is_file())
if not dbs:
    sys.exit("db/ 下没有 .db 文件")
for db in dbs:
    rel = db.relative_to(app_dir)
    out = dest / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(out)
        try:
            src.backup(dst)
            verdict = dst.execute("PRAGMA integrity_check").fetchone()[0]
            if verdict != "ok":
                sys.exit(f"integrity_check 失败: {rel} → {verdict}")
        finally:
            dst.close()
    finally:
        src.close()
    print(f"[backup]   {rel} OK ({out.stat().st_size} bytes)")
PYEOF

# 2) 打包
log "打包"
ARCHIVE="$BACKUP_ROOT/dailycheck-db-$DAY.tar.gz"
tar -czf "$ARCHIVE" -C "$DEST" .
log "archive: $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1))"

# 3) 可选上传（异地副本）
if [ -n "$UPLOAD_CMD" ]; then
    # shellcheck disable=SC2086
    log "上传异地副本"
    ${UPLOAD_CMD/\{archive\}/"$ARCHIVE"}
fi

# 4) 过期清理
log "清理 ${RETENTION_DAYS} 天前的备份"
find "$BACKUP_ROOT" -maxdepth 1 -name 'dailycheck-db-*.tar.gz' -mtime +"$RETENTION_DAYS" -delete
find "$BACKUP_ROOT" -maxdepth 1 -type d -name '20*' -mtime +"$RETENTION_DAYS" -exec rm -rf {} +

log "完成"
