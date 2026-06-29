# 库存消耗预测 — 子项目 1 spec

**版本**：v1（2026-06-29）
**状态**：实施中。引用总览 PRD §2.1。
**目标读者**：本子项目实施者（agent / 工程师）。

---

## 0. 引用与本 spec 决断

引用 PRD：
- §1.1 A1 — 每日自动跑
- §1.2 — 跨库约束（按店独立预测）
- §2.1.2 — 数据契约（**PRD 锁定**）
- §2.1.3 — 范围与边界（**PRD 锁定**）
- §2.1.4 — 异常表（**PRD 锁定**）
- §2.1.5 — 必须自动化
- §2.1.6 — 测试要点
- §3.2 — TDD 强制
- §3.3 — 三层测试
- §3.5 — 验证门
- §3.6 — 可观测性
- §8 — 锁定项速查

本 spec 自决项（非 PRD 锁定）：

1. **"消耗"的来源**：库存品的日均消耗 = 过去 30 天内 `outbound_requests.rolled_back = 0` 的 `requested_quantity` 之和，**不**包含生产消耗中的原料扣减。
   - 理由：库存品有两类动因（出库 / 生产），PRD 2.1.3 写"覆盖对象：items + products 的成品销售预测"。对 items 来说，仓库视角下"消耗"是「离开了库存」的所有动作；但仓库盘点（`stocktakes`）只调整账面不动库存。`outbound_requests` 与 `production_run_items.actual_qty` 都是「真消耗」。本 spec 选 **`outbound_requests` 单一来源**，避免双源加总带来的口径混淆。
   - 风险：未来如果用户质疑"为什么生产扣减不算消耗"，需要回来重读本 spec。**owner review 时务必确认此决断**。
2. **"产品销售预测"的代理量**：用 `production_runs.rolled_back = 0` 的 `output_qty` 累加。理由：本系统无"销售订单"表，`production_runs` 产出即"产品被做出去了"。
3. **"加权日均"的算法**：线性递减权重，最近 1 天权重最高，向 30 天前线性衰减到 1。
   - 公式：`daily_avg = sum(weight_i * qty_i) / sum(weight_i)`，`weight_i = (30 - i)`，`i = 0..n-1`。
   - 满足 PRD "加权平均"语义，但**不**承诺与未来 ML 模型结果一致。
4. **`forecast_total`**：`daily_avg * horizon_days`，**保留 2 位小数**。
5. **缓存层**：每次预测调用都跑 SQL 重算，**不**单独维护 `forecast_cache` 表。`forecast_runs` 只记录批量跑批的状态。
6. **路径**：
   - `/forecast/item/<item_id>?horizon_days=14`（库存品）
   - `/forecast/product/<product_id>?horizon_days=14`（产品）
   - `/forecast/recompute`（POST，admin/manager 触发手动重算 — 仅做幂等的"已完成"标记，不重算各 item，因每次请求已即时算）

---

## 1. 数据契约（与 PRD 2.1.2 一致，逐字复述）

```
GET /forecast/item/<item_id>?horizon_days=14
GET /forecast/product/<product_id>?horizon_days=14
→ 200 {
    "item_id": 123,                              // 或 product_id
    "warehouse_code": "wh_001",
    "horizon_days": 14,
    "daily_avg": 2.5,
    "forecast_total": 35.0,
    "confidence": "low|medium|high",
    "computed_at": "2026-06-29T03:00:00Z",
    "data_status": "ok|cold_start"
}
```

字段细化（spec 自决，不修改 PRD 契约）：

| 字段 | 类型 | 来源 |
|---|---|---|
| `daily_avg` | float, 2 dp | 加权公式，cold_start 时为 0 |
| `forecast_total` | float, 2 dp | `daily_avg * horizon_days`，cold_start 时为 0 |
| `confidence` | str | 由 `n_records` 决定：< 7 → cold_start 不返回；7-13 → low；14-29 → medium；≥ 30 → high |
| `computed_at` | str (UTC) | 服务器当前时间（ISO 8601，Z 结尾） |
| `data_status` | str | `ok` / `cold_start` |

---

## 2. 冷启动（PRD 2.1.3 锁定）

- 过去 30 天内**消耗记录数** < 7 → `data_status=cold_start`。
- `daily_avg` 和 `forecast_total` 强制为 0。
- 响应 200 仍然返回结构（**不**返回 404），UI 据此显示"数据积累中"。
- **不**使用全平台均值、**不**使用人工初值填充（PRD 锁定）。

---

## 3. 异常（PRD 2.1.4 锁定，本 spec 仅做错误体格式细节）

| 场景 | 状态 | 响应体 |
|---|---|---|
| 未知 item_id / product_id | 404 | `{"error": "not_found"}` |
| `horizon_days < 1` 或 `> 90` 或非整数 | 400 | `{"error": "invalid_horizon"}` |
| 库存品 / 产品已被禁用 | 410 | `{"error": "gone"}`（本版本未引入"禁用"字段，恒不触发；schema 演进后启用） |
| 数据库锁定 | 重试 3 次（指数退避 50/100/200 ms）；仍失败 → 写 `access.log` + `/admin/health` 标记 `"forecast_db_lock_failures"` 计数 +1 |

---

## 4. 必须自动化（PRD 1.1 A1 / 2.1.5）

- **每日凌晨自动跑**全平台预测：使用 Flask 进程内 scheduler（`apscheduler` 或自实现 `threading.Timer` 轮询）。本 spec 选 **threading-based 自实现**，避免新增重量依赖；实现位于 `blueprints/forecast.py` 内的 `_start_scheduler()`，在 `create_app()` 末尾惰性启动。
- **跑批状态表 `forecast_runs`**（master.db 新表）：

  ```sql
  CREATE TABLE forecast_runs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      started_at TEXT NOT NULL,
      finished_at TEXT,
      status TEXT NOT NULL,        -- 'running' | 'success' | 'failed'
      items_processed INTEGER NOT NULL DEFAULT 0,
      error_message TEXT
  );
  ```

- **失败可见性**：`/admin/health` 显示 `forecast_last_success_at`（来自最近一条 `status='success'` 的 `finished_at`）。本子项目**不**在 `/admin/health` 加新页面，仅在现有 health 输出后追加一节。

---

## 5. 错误处理与可恢复性（PRD 1.4）

- **不可逆动作**：跑批完成后写入 `forecast_runs`（即使全部 item 失败，也至少写一条 run 记录失败状态）。**不**写"预测快照"——结果按需实时算，无需快照。
- **可回滚动作**：无（预测是只读计算）。
- **失败兜底**：
  - DB 锁 3 次重试仍失败 → 写 `access.log`，并把当次 run 记 `status='failed'`，`error_message` 写锁错误摘要。
  - scheduler 进程崩溃 → 下次启动 `create_app()` 时检查 `forecast_runs` 最后一条 status='running' 但 `finished_at IS NULL` 的记录，标记为 `failed` 并写 error_message "scheduler_restart"。

---

## 6. 测试矩阵（PRD 2.1.6 映射）

### 6.1 单元（无 DB / 无 Flask）

- `compute_daily_avg(movements: list[tuple[datetime, float]]) -> float`
  - 空列表 → 0
  - 全部同日 → 等于该日总量 / 1（高权重集中）
  - 线性均匀（每条同量）→ 等于"总量 / 30"（验证分母语义）
  - 稀疏（只前 5 天有数据）→ 权重仅落在前 5 天
  - 突发（最近一天是其他天总和的 10 倍）→ 验证最近权重确实高于远期
- `compute_forecast_total(daily_avg, horizon) -> float`（2 dp）
- `classify_confidence(n_records) -> 'low'|'medium'|'high'|'cold_start'`
  - 0-6 → cold_start
  - 7-13 → low
  - 14-29 → medium
  - ≥ 30 → high

### 6.2 集成（Flask test_client + logged_client）

- `GET /forecast/item/<existing_id>` → 200，结构与字段类型稳定（jsonschema 校验）
- `GET /forecast/product/<existing_id>` → 200，同上
- `GET /forecast/item/<unknown_id>` → 404 + `{"error": "not_found"}`
- `GET /forecast/item/<id>?horizon_days=0` → 400 + `{"error": "invalid_horizon"}`
- `GET /forecast/item/<id>?horizon_days=91` → 400
- `GET /forecast/item/<id>?horizon_days=abc` → 400
- 冷启动 fixture（< 7 条） → `data_status=cold_start`，`daily_avg=0`，`forecast_total=0`
- `POST /forecast/recompute` → 200，返回 `{ok: true, last_run_id: int}`，可重复点击结果一致（同一 run_id）
- 写动作幂等：连续两次 `POST /forecast/recompute` 不创建新 run，第二次返回相同 `last_run_id`

### 6.3 E2E（Playwright）

- admin 登录 → `/forecast` 页 → 看到一个 item 的日均/预测 → 点击"手动重算" → toast "重算完成"
- staff 登录 → `/forecast` 不可见（权限拦截）

> E2E 必须在能连 Playwright 时跑；环境不具备时在 PR 描述中标注"manual verification needed"，**不**伪造截图。

---

## 7. 文件清单（仅本子项目新增/修改）

- 新增 `blueprints/forecast.py`（blueprint + scheduler）
- 新增 `blueprints/forecast_pure.py`（纯函数模块，单元测试目标）
- 修改 `app.py`（注册 forecast blueprint + scheduler 启动）
- 修改 `db/__init__.py`（`forecast_runs` 表 + `init_master_db` 内创建）
- 修改 `permissions.py`（admin/manager 可访问，staff 不可）
- 新增 `tests/test_forecast_pure.py`（单元）
- 新增 `tests/test_forecast_route.py`（集成）
- 新增 `tests/test_forecast_scheduler.py`（scheduler 行为）
- 新增 `pyproject.toml`（ruff 配置）
- 新增 `templates/forecast.html`（最小化 UI：列表 + 手动重算按钮）
- 修改 `blueprints/_helpers.py` 或新建 `blueprints/health.py`（追加 `forecast_last_success_at` 到 health 输出）

---

## 8. 验收门（PRD 3.5）

1. `pytest -q` 全绿
2. `ruff check .` 全绿
3. `/admin/health` 中 `forecast_last_success_at` 字段存在
4. 手动启动应用，按 §6.3 清单逐条点击
5. PR 描述包含：本节文件清单 + pytest 末尾输出 + E2E 截图或"manual verification needed"声明 + 引用 PRD §2.1
