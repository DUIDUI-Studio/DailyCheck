# DR Runbook — 灾难恢复手册（P0-7）

> 生效：2026-10-10（M7）。RPO=24h / RTO=1h（SLA 承诺见 `docs/compliance/SLA.md`）。
> 前提：生产 = 单 VPS（`wdg_vps` = 112.124.18.246:33756，`/root/DailyCheck`，
> systemd unit `dailycheck`，gunicorn 127.0.0.1:8090）。

## 1. 备份体系

| 层 | 机制 | 位置 | 频率 |
|----|------|------|------|
| 迁移前备份 | `deploy_vps.sh` 步骤 1（每次发版） | `/root/backups/<ts>/db/` | 每次部署 |
| 每日全库备份 | `scripts/backup_all.sh`（cron） | `/root/backups/auto/<date>/` + tar 包 | 每日 |
| 异地副本 | `DAILYCHECK_UPLOAD_CMD`（如 rclone → OSS） | 对象存储 | 随每日备份 |

### 1.1 cron 安装（部署时执行一次）

```cron
# crontab -e  (root)
30 3 * * * DAILYCHECK_UPLOAD_CMD='rclone copy {archive} oss:dailycheck-backups/' /root/DailyCheck/scripts/backup_all.sh >> /root/backups/backup.log 2>&1
0 5 * * 1 /root/DailyCheck/scripts/verify_restore.sh >> /root/backups/drill.log 2>&1
```

（未配对象存储前先去掉 `DAILYCHECK_UPLOAD_CMD` 部分；异地副本是 RPO 的硬前提，
VPS 磁盘损坏时同机备份会一起丢。）

## 2. 故障场景与处置

### 2.1 发版后服务异常（最常见）
`deploy_vps.sh` 已自动回滚。若自动回滚也失败：
```bash
ssh wdg_vps
ls -t /root/backups/ | head -3          # 找最近一次部署备份
cd /tmp && mkdir rec && tar -xzf /root/backups/<ts>/code.tar.gz -C rec
rsync -a --delete --exclude '*.db' --exclude 'backups/' --exclude '.venv/' \
      --exclude '*.log' /tmp/rec/ /root/DailyCheck/
systemctl restart dailycheck && curl -f http://127.0.0.1:8090/health
```

### 2.2 数据误删 / 坏数据回滚（当天发现）
目标：回到今天凌晨备份。
```bash
ssh wdg_vps
systemctl stop dailycheck
cd /tmp && mkdir dbrec && tar -xzf /root/backups/auto/$(date +%F)/../dailycheck-db-$(date +%F).tar.gz -C dbrec
# 逐库确认后覆盖（⚠ 丢掉凌晨之后的数据 = RPO 代价）
rsync -a /tmp/dbrec/db/ /root/DailyCheck/db/
systemctl start dailycheck && curl -f http://127.0.0.1:8090/health
```
只回滚单个仓：仅覆盖对应的 `db/warehouses/<code>.db`。

### 2.3 VPS 整机失联
1. 新购 VPS，装 Python 3.12 + venv。
2. 从对象存储取最新 `dailycheck-db-*.tar.gz` + 最近一次发版代码（GitHub main 对应 commit）。
3. 按 `docs/DEV_ENV.md` 布局放置：`/root/DailyCheck`（代码）+ 还原 `db/`。
4. `pip install -r requirements.txt` → `flask migrate up` → systemd unit 参照
   `deploy/systemd/dailycheck-app.service`（改端口/用户）→ 启动 → `/health`。
5. DNS 切换。目标 1 小时内完成（RTO）。

## 3. 演练要求

- `verify_restore.sh` 每周自动跑（cron），失败即告警。
- **每季度人工演练一次 2.3 整机恢复**（在临时 VPS 上走全程），记录耗时到本文件。

## 4. 演练记录

| 日期 | 类型 | 耗时 | 结果 |
|------|------|------|------|
| 2026-10-10 | 还原+临时实例（dev 容器首演） | ~1 分钟 | 通过 |
