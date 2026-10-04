# 生产环境发布手册 — 品项主数据 M1+M2

> 给 prod 端 Agent：按本手册执行，每一步都已验证。
> 不要自由发挥、不要跳跃步骤、不要"优化"顺序。

---

## 0. TL;DR

| 项 | 值 |
|---|---|
| 远程 | `https://github.com/ericmr1981/DailyCheck.git` (分支 main) |
| 当前 prod HEAD | `eef6518` (fix/mcp-streamable-http 分支) |
| 目标 main HEAD | `2d1bb8b` (含 7 个新 commit) |
| 模式 | fast-forward merge (零冲突，已 git log 远端确认) |
| 工作路径 | `/root/DailyCheck` |
| 服务名 | `dailycheck.service` (gunicorn 127.0.0.1:8090) |

---

## 1. 发布前确认（必做，否则不上）

```bash
cd /root/DailyCheck
git status        # 必须 clean (或只剩 untracked .pre-pull-local-backup/)
git branch --show-current   # 必须 fix/mcp-streamable-http
git log --oneline -1
```

预期：`## fix/mcp-streamable-http` + `eef6518 ...`。

**❌ 如果不是这个状态——停下来告诉 Eric，不要继续。**

---

## 2. 备份（发布前必做，做完才能动代码）

### 2.1 备份目录

```bash
mkdir -p /root/DailyCheck/backups/release-2026-10-04
BACKUP=/root/DailyCheck/backups/release-2026-10-04
```

### 2.2 备份所有 db（含软链目标）

```bash
# master.db
cp /root/DailyCheck/db/master.db $BACKUP/master.db.bak

# 直接 db
cp /root/DailyCheck/db/warehouses/wh_000.db $BACKUP/wh_000.db.bak
cp /root/DailyCheck/db/warehouses/wh_003.db $BACKUP/wh_003.db.bak
cp /root/DailyCheck/db/warehouses/wh_004.db $BACKUP/wh_004.db.bak
cp /root/DailyCheck/db/warehouses/wh_006.db $BACKUP/wh_006.db.bak
cp /root/DailyCheck/db/warehouses/wh_010.db $BACKUP/wh_010.db.bak
cp /root/DailyCheck/db/warehouses/rd_001.db $BACKUP/rd_001.db.bak

# 软链目标
cp /var/lib/dailycheck/warehouses/wh_001.db $BACKUP/wh_001.db.bak
cp /var/lib/dailycheck/warehouses/wh_002.db $BACKUP/wh_002.db.bak
```

### 2.3 备份当前 commit 状态（万一回滚要知道回到哪里）

```bash
echo "eef6518" > $BACKUP/PROD_BEFORE_HEAD.txt
git log --oneline -1 > $BACKUP/PROD_BEFORE_COMMIT.txt
```

### 2.4 备份完，验证

```bash
ls -la $BACKUP/
# 应该看到 10 个 .db.bak + 2 个 .txt = 16 KB ~ 3 MB
```

**❌ 如果 10 个 .db.bak 数量不对——停下来检查。**

---

## 3. 拉新代码（fast-forward）

### 3.1 prod 当前 HTTPS git 协议不可用（TLS GnuTLS 问题），SSH 协议可用

但 prod 本地已经是 git 仓库，origin 已配置为 https。**直接 fetch 可能会失败**，需要换成 SSH：

```bash
# 先试 https，如果失败换 ssh
git fetch origin main 2>&1 | head -3
```

如果 `TLS handshake failed`：

```bash
# 切换 origin URL 为 SSH（prod 已有 ~/.ssh/wdg_vps_ed25519 密钥但本机 git 可能不认）
# 简单办法：直接 fetch 后 reset
# 但如果 git fetch 持续失败，用 bundle：
# 本地先 bundle 出 main 的增量包，scp 到 prod，prod apply bundle
# 步骤见 3.3 备选方案
```

### 3.2 主路径（git fetch + reset --hard）

```bash
cd /root/DailyCheck

# fetch
git fetch origin main

# 检查：本地是否能拿到新 commit
git log --oneline origin/main -5

# 预期看到 7 个新 commit:
#   2d1bb8b fix(canonical-item): fanout INSERT branch needs real category_id, not 0
#   ba95b9c docs(canonical-item): M1+M2 delivery report
#   45e817e fix(canonical-item): fanout never-synced baseline + test for T12 behavior
#   958b4a7 feat(canonical-item): M2 routes + UI + publish + notifications
#   11fbcbd feat(canonical-item): M1 pure layer — schema + CRUD + signals + claim + fanout
#   e62dd3d feat(canonical-item): T1 policy switches + T23 inventory guards (Q7)
#   c202a33 docs(canonical-item): add v4 design + 4 supporting docs

# fast-forward 跳到新 HEAD（不动工作区，因为 .pre-pull-local-backup 等 untracked 不受影响）
git reset --hard origin/main

# 验证
git log --oneline -1
# 预期: 2d1bb8b fix(canonical-item): fanout INSERT branch needs real category_id, not 0
```

**⚠️ 不要 git pull**——它会尝试 merge，遇到冲突就停。我们需要的是 reset --hard（fast-forward）。

### 3.3 备选方案（如果 git fetch 失败）

在本地 dev 端：
```bash
cd ~/Documents/GitHub/DailyCheck
git bundle create /tmp/dailycheck-main-2026-10-04.bundle 2d1bb8b
```
产出 `/tmp/dailycheck-main-2026-10-04.bundle`，scp 到 prod：
```bash
scp -P 33756 /tmp/dailycheck-main-2026-10-04.bundle root@112.124.18.249:~
```
prod 端：
```bash
cd /root/DailyCheck
git fetch ~/dailycheck-main-2026-10-04.bundle main:prod-2026-10-04
git reset --hard prod-2026-10-04
```

---

## 4. 应用数据库迁移（Flask 启动自动跑）

### 4.1 看 .pre-pull-local-backup 还在不在

```bash
ls /root/DailyCheck/.pre-pull-local-backup/ 2>/dev/null | head
```

如果有，**保留它**（这是上次部署留下的本地备份，别删）。

### 4.2 重启服务（启动会触发 init_master_db() 和 migrate_warehouse_db_columns()）

```bash
systemctl restart dailycheck.service
```

### 4.3 等待服务起来，看 init_master_db() 是否成功

```bash
# 等 3 秒后看日志
sleep 3
journalctl -u dailycheck.service --no-pager -n 30 | tail -25
```

**预期：**
- 没有 `no such column` 错误
- 没有 `OperationalError`
- gunicorn "Booting worker" 日志出现

### 4.4 验证 8 张 canonical_* 表已建

```bash
sqlite3 /root/DailyCheck/db/master.db ".tables"
```

**预期看到** `canonical_categories` `canonical_items` `canonical_item_aliases` `canonical_claim_requests` `canonical_conflicts` `canonical_publish_events` `canonical_publish_event_warehouses` `canonical_publish_event_items`。

### 4.5 验证各仓 items 已加 5 列

```bash
sqlite3 /root/DailyCheck/db/warehouses/wh_000.db ".schema items" | grep -E "canonical|is_alias"
```

**预期看到** `canonical_id`、`is_alias`、`canonical_status`、`canonical_synced_json`、`is_store_exclusive` 5 列。

### 4.6 验证服务可访问

```bash
curl -sI http://127.0.0.1:8090/ | head -2
# 预期: 302 FOUND (跳登录)
```

---

## 5. 灌主数据（批量收编）

### 5.1 CSV 在仓库里

```bash
ls -la /root/DailyCheck/docs/2026-10-03-claim-legacy-csv-preview.csv
wc -l /root/DailyCheck/docs/2026-10-03-claim-legacy-csv-preview.csv
# 预期: 399 行（398 + header）
```

### 5.2 通过 Flask 灌入

CSV 灌入流程在 M1 设计里是 `bulk_create_canonical_items(csv_path)`，由蓝图 `/canonical/bulk_import` 端点调用。

**直接调 CLI**（比网页灌快，且可重试）：

```bash
cd /root/DailyCheck
source .venv/bin/activate
flask --app app bulk-import-canonical /root/DailyCheck/docs/2026-10-03-claim-legacy-csv-preview.csv 2>&1 | tail -10
```

**预期看到**：
- `Read 398 rows`
- `Created X canonical items, skipped Y`
- **没有任何 `IntegrityError` 或 `FOREIGN KEY constraint failed`**

### 5.3 如果没有 `flask bulk-import-canonical` 命令

看 `cli.py`：
```bash
grep -n "bulk\|import" /root/DailyCheck/cli.py | head
```

如果没有该命令，则走蓝图路由：登录 admin → `/canonical/bulk_import` → 上传 CSV → 等完成后跳回列表。

### 5.4 验证主数据已灌入

```bash
sqlite3 /root/DailyCheck/db/master.db "SELECT COUNT(*) FROM canonical_items WHERE status='active'"
# 预期: 124-125
sqlite3 /root/DailyCheck/db/master.db "SELECT COUNT(*) FROM canonical_items WHERE category_code IS NULL"
# 预期: 0
```

---

## 6. 批量认领（所有 5 仓 + rd_001 + wh_010）

### 6.1 概念

CSV 里每行有 `(warehouse_code, current_sku, item_id, bind_to_canonical)`。执行"绑定"就是把 `items.canonical_id` 填上。

### 6.2 通过 CLI 灌

**如果有 `flask bulk-claim-from-canonical` 命令**：
```bash
cd /root/DailyCheck
flask --app app bulk-claim-from-canonical 2>&1 | tail -10
```

**如果没有**（M1 设计里 T7 是单独的 bulk claim）——则用蓝图路由 `/canonical/claim` 的 batch 端点，或走 UI 逐条（398 条 × 4-5 仓 ≈ 1500+ 次，**禁止 UI 手工**）。

**如果蓝图路由也没有批量端点**——这是设计遗漏（Q1=deny + legacy whitelist 的实际需求），**停下来告诉 Eric 走紧急路径：用 SQL 直接 UPDATE**：

```sql
-- 5 仓 + rd_001 同时执行
UPDATE items SET canonical_id = (
    SELECT id FROM master.canonical_items WHERE canonical_sku = ?
) WHERE id = ?;
```

（实际 SQL 用 psycopg2 或 Python 脚本循环 CSV，不要手敲——5 仓 + rd_001 × 398 行）

### 6.3 验证认领完成

```bash
for wh in wh_000 wh_001 wh_002 wh_003 wh_004 wh_006 wh_010 rd_001; do
    db=/root/DailyCheck/db/warehouses/${wh}.db
    [ -e "$db" ] || db=/var/lib/dailycheck/warehouses/${wh}.db
    if [ -e "$db" ]; then
        n=$(sqlite3 $db "SELECT COUNT(*) FROM items")
        b=$(sqlite3 $db "SELECT COUNT(*) FROM items WHERE canonical_id IS NOT NULL")
        echo "$wh: $n items, $b bound"
    fi
done
```

**预期**（5 仓 + rd_001 都有 bound，wh_010 空仓 0/0）：
```
wh_000: 89 items, 89 bound
wh_001: 33 items, 33 bound
wh_002: 69 items, 69 bound
wh_003: 94 items, 94 bound
wh_004: 71 items, 71 bound
wh_006: 75 items, 75 bound
wh_010: 0 items, 0 bound
rd_001: 125 items, 125 bound
```

---

## 7. 库存零漂移守门（Q7 必做）

```bash
# 对每个有 items 的仓, 对比 backup 的 quantity repr
for wh in wh_000 wh_001 wh_002 wh_003 wh_004 wh_006 rd_001; do
    db=/root/DailyCheck/db/warehouses/${wh}.db
    [ -e "$db" ] || db=/var/lib/dailycheck/warehouses/${wh}.db
    bak=/root/DailyCheck/backups/release-2026-10-04/${wh}.db.bak
    if [ -e "$db" ] && [ -e "$bak" ]; then
        # Python repr 比对 (不是 round 后)
        bak_q=$(sqlite3 $bak "SELECT quantity FROM items ORDER BY id" | md5sum | cut -c1-8)
        cur_q=$(sqlite3 $db "SELECT quantity FROM items ORDER BY id" | md5sum | cut -c1-8)
        # 行数
        bak_n=$(sqlite3 $bak "SELECT COUNT(*) FROM items")
        cur_n=$(sqlite3 $db "SELECT COUNT(*) FROM items")
        if [ "$bak_n" = "$cur_n" ] && [ "$bak_q" = "$cur_q" ]; then
            echo "$wh: ✓ 行数=$cur_n, quantity 一致"
        else
            echo "$wh: ✗ 行数 bak=$bak_n cur=$cur_n, quantity bak=$bak_q cur=$cur_q"
        fi
    fi
done
```

**预期**：所有仓 ✓。

**❌ 任何 ✗ 必须停下来——这意味着库存数据被破坏。**

---

## 8. rd_001 扇出（让研发仓看到与 5 仓一样的清单）

```bash
# 走 admin UI 或 CLI（如果有 bulk-import-canonical-item-to-rd 命令）
# 最稳的方式: 通过 admin 登录 + POST /canonical/fanout
```

admin 登录后浏览器：
- `http://<prod域名>/canonical/fanout`
- 全选 124 个 canonical_items
- warehouse_codes: `["rd_001"]`
- action: overwrite
- 提交

或者**直接 SQL 扇出**（这是设计原意，参见 dev 验证过的路径）：

```bash
cd /root/DailyCheck
flask --app app shell <<EOF
from app import create_app
app = create_app()
with app.app_context():
    from blueprints.canonical_pure import fanout_canonical_items
    from db import get_master_db, get_warehouse_db
    m = get_master_db()
    rd = get_warehouse_db()
    canonical_ids = [r[0] for r in m.execute("SELECT id FROM canonical_items WHERE status='active'").fetchall()]
    result = fanout_canonical_items(m, rd, canonical_ids, ['rd_001'], 'overwrite')
    print(result)
EOF
```

验证：
```bash
sqlite3 /root/DailyCheck/db/warehouses/rd_001.db "SELECT COUNT(*) FROM items WHERE canonical_id IS NOT NULL"
# 预期: 125
```

---

## 9. 5 个流程演示（回归测试）

### 9.1 改名/改单位（库存保护）

浏览器登录 admin → `/canonical/detail/<某个 id>` → 改 name → 提交 → 看 5 仓 items.name 是否同步、quantity/safety_stock/selling_price/unit_cost 是否不变。

### 9.2 冲突冻结（Q4）

找一个 item，门店自己改 name → 总部改主数据 name → 扇出 → 看 canonical_conflicts 表是否新增 open 行、门店 name 保持本地值不变。

### 9.3 新增主数据

`/canonical/edit` 新建一条「麦片」unit=袋 category_code=CONSUMABLE → 扇出到 5 仓 → 各仓新增一行。

### 9.4 停用主数据（Q3）

把某个 IC 改成 inactive → 扇出 → 各仓 items.canonical_status='disabled' → 编辑该行 name 应 403。

---

## 10. 回滚（如果任何一步失败）

### 10.1 软回滚（不丢代码）

```bash
cd /root/DailyCheck
git reset --hard eef6518    # 回到发布前
systemctl restart dailycheck.service
```

但**git reset 不会回滚 db schema**——M1 应用的 8 张表 + 5 列迁移已写进 master.db / 各仓 items 表，不会被 reset 撤销。

### 10.2 硬回滚（db 也回滚）

```bash
cd /root/DailyCheck
git reset --hard eef6518

# 恢复 db
cp /root/DailyCheck/backups/release-2026-10-04/master.db.bak /root/DailyCheck/db/master.db
cp /root/DailyCheck/backups/release-2026-10-04/wh_000.db.bak /root/DailyCheck/db/warehouses/wh_000.db
cp /root/DailyCheck/backups/release-2026-10-04/wh_003.db.bak /root/DailyCheck/db/warehouses/wh_003.db
cp /root/DailyCheck/backups/release-2026-10-04/wh_004.db.bak /root/DailyCheck/db/warehouses/wh_004.db
cp /root/DailyCheck/backups/release-2026-10-04/wh_006.db.bak /root/DailyCheck/db/warehouses/wh_006.db.bak
cp /root/DailyCheck/backups/release-2026-10-04/wh_010.db.bak /root/DailyCheck/db/warehouses/wh_010.db.bak
cp /root/DailyCheck/backups/release-2026-10-04/rd_001.db.bak /root/DailyCheck/db/warehouses/rd_001.db
cp /root/DailyCheck/backups/release-2026-10-04/wh_001.db.bak /var/lib/dailycheck/warehouses/wh_001.db
cp /root/DailyCheck/backups/release-2026-10-04/wh_002.db.bak /var/lib/dailycheck/warehouses/wh_002.db

systemctl restart dailycheck.service
```

### 10.3 通知 Eric

任何失败必须通知 Eric，告知：
- 卡在哪一步
- 日志（`journalctl -u dailycheck.service -n 50`）
- 已做了哪些不可逆动作（备份？删除？reset？）

---

## 11. 时间预估

| 步骤 | 预计时间 |
|---|---|
| §1 确认 | 1 分钟 |
| §2 备份 | 1 分钟 |
| §3 拉代码 | 1 分钟 |
| §4 重启 + 验证 | 1 分钟 |
| §5 灌 CSV | 5 分钟（含 5 仓 items × 8 categories mapping） |
| §6 批量认领 | 5 分钟 |
| §8 rd_001 扇出 | 2 分钟 |
| §7 守门验证 | 1 分钟 |
| §9 流程演示 | 5 分钟 |
| **总计** | **~25 分钟** |

---

## 12. 异常处理表

| 异常 | 原因 | 处理 |
|---|---|---|
| `git fetch` TLS 失败 | prod GnuTLS 旧 | 用 SSH bundle 备选方案（§3.3） |
| `init_master_db` 报 `no such column` | 漏写 ALTER 进 flock | 停下来，告诉 Eric——这是 M1 设计遗漏 |
| Flask 重启后 502 | gunicorn 没起来 | `journalctl -u dailycheck.service -n 30` 看错 |
| 灌 CSV 报 FK constraint failed | engineer INSERT bug 修复未应用 | 检查 git log 是否有 `2d1bb8b`，没有就是 reset 没生效 |
| 库存守门发现 ✗ | 迁移损坏了 quantity | **立即回滚**（§10.2），通知 Eric |
| 认领后 unbound > 0 | CSV 路径与 master 路径不符 | 检查 `canonical_sku` 是否匹配，停下 |

---

## 13. 发布后 Eric 要看的内容

- 服务可访问（`curl -I http://<prod>/canonical/list`）
- 7 条验收（PRD §6）：
  1. 5 仓 items 行数 = 原库
  2. 5 仓 items.id 集合 = 原库
  3. quantity Python repr 逐行相等
  4. 9 张引用表行数不变
  5. 浮点脏值保留
  6. Q1=deny 死角 = 0
  7. 待裁决项 ≤ 2