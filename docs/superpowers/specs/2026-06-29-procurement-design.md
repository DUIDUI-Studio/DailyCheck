# 门店采购建议 + 总仓采购建议 — 子项目 2 spec

**版本**：v1（2026-06-29）
**状态**：实施中。引用总览 PRD §2.2。
**前置依赖**：子项目 1（`/forecast/*` 接口与 `daily_avg` 数据契约）。
**目标读者**：实施 agent。

---

## 0. 引用与本 spec 自决

引用 PRD：
- §1.1 A2 — 库存变动后自动重算
- §1.1 A3 — 补货预警自动推送（**注意：自动化推送 = 子项目 2 的"采纳"动作触发建议计算；推送到总仓待办属于子项目 3 通知总线；本子项目只负责算**）
- §2.1 — 依赖预测接口
- §2.2.2 — 数据契约（**PRD 锁定**）
- §2.2.3 — 安全库存公式（**PRD 锁定**）
- §2.2.4 — 采纳动作仅生成 CSV（**PRD 锁定**）
- §2.2.5 — 重算时机（事件触发 + 5 秒缓存）
- §2.2.6 — 异常表
- §2.2.7 — 测试要点
- §3.2/3.3/3.5/3.6

本 spec 自决项：

1. **`min_absolute` 配置位置**：master.db 新表 `procurement_config`（单行），列：`cover_days INT NOT NULL DEFAULT 14`, `min_absolute REAL NOT NULL DEFAULT 0`。品项级覆盖不在本子项目范围（PRD 写"可在品项级别覆盖"，本子项目**只实现全局默认**，未来可加 `items.min_absolute_override` 列）。
2. **CSV 列**：`item_id, item_name, suggested_qty, unit, note`（PRD 锁定 4 列）。
3. **CSV 落盘位置**：`tmp/procurement_acceptance_<warehouse_code>_<timestamp>.csv`（PRD 路径），`tmp` 用 `tempfile.gettempdir()`。
4. **重算缓存**：5 秒内可读到新值即可（PRD §2.2.5）。**实现**：每次出库/入库/盘点/补货申请完成后，标记 `procurement_cache` 表中该品项 invalid。`/procurement/store` 调用时按 invalid → 重新算 → 写入缓存。否则读缓存。
5. **`in_transit_qty` 来源**：`restock_requests` 表中 `status NOT IN ('已到货', '已取消')` 的 `requested_quantity` 求和。理由：项目无独立"在途"表。
6. **补货预警 (A3)**：**本子项目不实现**推送动作（属于子项目 3 通知事件总线的范围）。仅在 `procurement_cache` 表里维护 `needs_restock: bool` 字段，**子项目 3 通过读这个表生成通知事件**。
7. **CSV 文件** UTF-8 BOM + 逗号分隔（与项目现有 `/summary/export` 行为一致，参考 `test_summary_export.py`）。

---

## 1. 数据契约（与 PRD 2.2.2 一致）

### 1.1 `/procurement/store?warehouse_code=<code>`

```
GET /procurement/store?warehouse_code=wh_001
→ 200 {
    "warehouse_code": "wh_001",
    "computed_at": "2026-06-29T03:00:00Z",
    "items": [
        {
            "item_id": 123,
            "item_name": "木樨子油",
            "current_qty": 0.7,
            "in_transit_qty": 0,
            "daily_avg": 0.05,
            "forecast_total_horizon": 0.7,
            "safety_stock": 1.0,
            "suggested_qty": 1.0
        }
    ]
}
```

### 1.2 `/procurement/hub?warehouse_codes=wh_001,wh_002`

```
GET /procurement/hub?warehouse_codes=wh_001,wh_002
→ 200 {
    "computed_at": "...",
    "items": [
        {
            "item_id": 123,
            "item_name": "木樨子油",
            "total_suggested_qty": 5.0,
            "stores_needing": 3,
            "stores_detail": [
                {"warehouse_code": "wh_001", "suggested_qty": 1.0}
            ]
        }
    ]
}
```

### 1.3 采纳 → CSV

```
POST /procurement/store/accept
{
    "warehouse_code": "wh_001"
}
→ 200 {
    "ok": true,
    "filename": "procurement_acceptance_wh_001_20260629T103045.csv",
    "item_count": 5,
    "download_url": "/procurement/store/accept/download?filename=..."
}
```

---

## 2. 安全库存公式（PRD 锁定）

```
safety_stock = max(daily_avg * cover_days, min_absolute)
suggested_qty = max(0, ceil(safety_stock - current_qty - in_transit_qty))
```

- `cover_days` 默认 14（master.db `procurement_config`）
- `min_absolute` 默认 0（同上）
- `ceil` 保证建议数量向上取整（不接受小数件）

---

## 3. 冷启动行为（与子项目 1 对齐）

- `data_status == 'cold_start'` 的品项 → **不出现在建议列表**（PRD §2.2.6："cold_start 的品项 suggested_qty = 0（不基于未稳定的预测给建议）"）。
- 实现：filter `WHERE f.data_status = 'ok' AND safety_stock > current_qty + in_transit_qty`。

---

## 4. 异常（PRD 2.2.6）

| 场景 | 行为 |
|---|---|
| `warehouse_code` 不存在 | 404 + `{"error": "not_found"}` |
| 无任何品项需要补 | 200 + `items: []` |
| CSV IO 失败 | 500 + flash + access.log |
| `cold_start` 品项 | 过滤掉（不出现在 items） |

---

## 5. 必须自动化（PRD §1.1 A2）

**事件触发的重算**（不是 5 秒 TTL，是 invalidation）：

- 出库请求提交（POST `/outbound`）→ 标记该品项 `procurement_cache.invalid=1`
- 入库（restock approve）→ 标记
- 盘点提交 → 标记
- 补货申请创建 → 标记
- 预测接口（TASK 8 子项目 1 的 scheduler）每日跑完后 → **批量标记所有品项 invalid**（因为 daily_avg 变化）

**缓存表** `procurement_cache`（**master.db**，跨店聚合需要）：

```sql
CREATE TABLE procurement_cache (
    item_id INTEGER NOT NULL,
    warehouse_code TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    daily_avg REAL NOT NULL,
    current_qty REAL NOT NULL,
    in_transit_qty REAL NOT NULL,
    safety_stock REAL NOT NULL,
    suggested_qty REAL NOT NULL,
    invalid INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (item_id, warehouse_code)
);
```

**5 秒可读到新值**（PRD §2.2.5）：GET `/procurement/store` 命中缓存且 `invalid=0` → 直接返回；`invalid=1` → 重算并写缓存。如果调用方连续两次 GET 间隔 < 5s 但中间无 invalid 事件，第二次仍读旧值（这是 PRD 允许的语义）。

---

## 6. 错误处理与可恢复性

- **不可逆动作**：CSV 落盘（`tempfile`）。如果用户不下载，**保留 24h 后由系统清理**（PR 描述标注）。
- **可回滚动作**：无（本子项目不写任何业务表，只读 + 写 cache + 写 CSV 临时文件）。
- **失败兜底**：CSV IO 失败 → 500 + `access.log` 含 `procurement_csv_fail` + 缓存不变（用户可重试）。

---

## 7. 测试矩阵

### 7.1 单元

- `compute_safety_stock(daily_avg, cover_days, min_absolute)`：
  - `daily_avg=0` → `min_absolute`
  - `daily_avg=0.1, cover_days=14` → `1.4`
  - `daily_avg=0.05, cover_days=14, min_absolute=1.0` → `1.0`（max）
  - `daily_avg=2.0, cover_days=14, min_absolute=0` → `28.0`
- `compute_suggested_qty(safety_stock, current_qty, in_transit_qty)`：
  - 全 0 → 0
  - `safety=5, current=2, in_transit=1` → `ceil(2) = 2`
  - `safety=5, current=10, in_transit=0` → `0` (max(0, -5))
  - `safety=0.1, current=0, in_transit=0` → `1` (ceil 向上)
- `aggregate_hub(store_reports)`：聚合 + 排序 + 字段计算

### 7.2 集成

- 已知仓库 + 已知品项 + 有消耗 → 出现在 items
- 未知 warehouse_code → 404
- cold_start 品项 → 不出现
- 提交一次出库 → 5 秒内 procurement_cache invalid 标记生效（虽然我们用 invalidation 而非 TTL，但 integration test 验 invalidation 路径）
- `POST /procurement/store/accept` → 生成 CSV，列数 = 4，UTF-8 BOM

### 7.3 E2E

- staff 登录 → `/procurement/store` 看到建议 → 点击采纳 → 下载 CSV

---

## 8. 文件清单

- 新增 `blueprints/procurement.py`（blueprint + 路由）
- 新增 `blueprints/procurement_pure.py`（纯函数）
- 修改 `db/__init__.py`（`procurement_cache` + `procurement_config` schema）
- 修改 `app.py`（注册 blueprint）
- 修改 `blueprints/outbound.py`（approve 时 mark invalid）— 标记最小化
- 修改 `blueprints/restock.py`（approve 时 mark invalid）
- 修改 `blueprints/stocktake.py`（approve 时 mark invalid）
- 修改 `blueprints/adjustment.py`（如有，则 mark invalid）
- 新增 `tests/test_procurement_pure.py`
- 新增 `tests/test_procurement_route.py`
- 新增 `tests/test_procurement_cache.py`
- 新增 `templates/procurement_store.html`（最小）
- 新增 `templates/procurement_hub.html`（最小）

---

## 9. 验收门

1. `pytest -q` 全绿
2. 新代码 ruff clean
3. CSV 列数 = 4，BOM 正确
4. 手动启动应用，按 §7.3 清单逐条点击
5. PR 描述引用 PRD §2.2
