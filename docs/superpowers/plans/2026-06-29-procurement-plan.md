# 门店采购建议 + 总仓采购建议 — 子项目 2 plan

**版本**：v1（2026-06-29）
**状态**：实施中。引用 spec `2026-06-29-procurement-design.md`。
**前置**：子项目 1 已合并（`/forecast/*` 接口与 `daily_avg` 数据契约稳定）。
**目标读者**：实施 agent。

---

## 0. 执行约定

- 一任务 = 一 commit。任务顺序 = 提交顺序。
- 每任务内部：先写失败测试（commit `test:`），再写最小实现（commit `feat:` / `fix:`）。
- ruff config 已存在（子项目 1 引入），新代码 ruff clean。
- 出库/入库/盘点/补货**触发的 invalidation** 在 §0.2 单独列。

---

## 1. 任务清单

### TASK 0 — 基础设施确认

**目标**：开干前确认子项目 1 的 `/forecast/item/<id>` 接口稳定（这是数据源）。
**动作**：
- `pytest -q` 全绿（应 ≥ 98 passed，含子项目 1 的 56 个新测试）
- 读 `blueprints/forecast.py` 的 `_fetch_outbound_rows` 确认返回结构

**验证**：`pytest -q` exit 0。

**commit**：无（preflight check）。

---

### TASK 1 — 纯函数：`compute_safety_stock` + `compute_suggested_qty`（RED → GREEN）

**RED**：写 `tests/test_procurement_pure.py`，覆盖 spec §7.1 全部用例。
**GREEN**：`blueprints/procurement_pure.py` 实现 2 个函数。

**commit 序列**：
1. `test(procurement): RED safety_stock + suggested_qty pure fns`
2. `feat(procurement): implement compute_safety_stock + compute_suggested_qty`

---

### TASK 2 — 纯函数：`aggregate_hub`（RED → GREEN）

**RED**：测试聚合函数
- 多门店同一品项 → `total_suggested_qty` = sum
- `stores_needing` = count of `suggested_qty > 0`
- `stores_detail` 包含每个门店的 (code, suggested_qty)
- 空输入 → 空 list

**GREEN**：实现。

**commit**：`feat(procurement): aggregate_hub pure fn`

---

### TASK 3 — schema：`procurement_config` + `procurement_cache`（RED → GREEN）

**目标**：master.db 两张新表。

**RED**：3 个集成测试
- `procurement_config` 表存在 + 默认 cover_days=14 + min_absolute=0
- `procurement_cache` 表存在 + 复合主键
- 写一行 → 读回 → 字段稳定

**GREEN**：在 `db/__init__.py` 的 `MASTER_SCHEMA` 追加两张表 + CREATE INDEX。

**commit**：`feat(procurement): procurement_config + procurement_cache schema`

---

### TASK 4 — `GET /procurement/store` 路由（RED → GREEN）

**目标**：`/procurement/store?warehouse_code=<code>` 返回 spec §1.1 契约 200。

**RED**：写 `tests/test_procurement_route.py`：
- 已知仓库 + 已知品项 + 有 outbound → 200 + 出现在 items
- `warehouse_code` 缺失 → 400 + `{"error": "warehouse_code_required"}`
- 未知 warehouse_code → 404 + `{"error": "not_found"}`
- cold_start 品项 → 不出现
- safety_stock > 0 但 current_qty 充足 → 出现在 items 但 suggested_qty=0

**GREEN**：
- `blueprints/procurement.py::procurement_store`
- 调 `get_forecast_data(item_id)` 拿 daily_avg（**避免再次查询 outbound，直接复用 forecast 接口或抄它的查询**；spec 决定用**直接 SQL 查询**以避免 HTTP 自调用）
- 调 `compute_safety_stock` + `compute_suggested_qty`
- 过滤 cold_start
- 返回 JSON

**commit**：`feat(procurement): GET /procurement/store route`

---

### TASK 5 — `in_transit_qty` 计算（RED → GREEN）

**目标**：spec §0.5 描述的"未完成 restock_requests 求和"。

**RED**：测试
- 创建一个 item + 一个 status='提交' 的 restock_request（qty=5）→ `in_transit_qty=5`
- 同一个 item + 一个 status='已到货' 的 restock_request → `in_transit_qty=0`（已完成的忽略）

**GREEN**：在 `procurement_store` view 内联调 `SELECT SUM(requested_quantity) FROM restock_requests WHERE item_id=? AND status NOT IN ('已到货', '已取消') AND rolled_back=0`。

**commit**：`feat(procurement): in_transit_qty from open restock_requests`

---

### TASK 6 — `GET /procurement/hub` 路由（RED → GREEN）

**目标**：spec §1.2 契约。

**RED**：测试
- 2 个仓库 1 个品项（都在冷启动后）→ `total_suggested_qty` = 2 店 sum
- 空 warehouse_codes → 400
- 1 个仓库 → 1 个门店出现在 `stores_detail`

**GREEN**：
- 查 master.db 拿所有 warehouse_code
- 对每个 code 调 `procurement_store` 的内部计算逻辑（**抽公共函数** `compute_store_procurement(wh_code)`，view 和 hub 都调它）
- 调 `aggregate_hub`
- 返回 JSON

**commit**：`feat(procurement): GET /procurement/hub + shared compute fn`

---

### TASK 7 — 缓存 + invalidation（RED → GREEN）

**目标**：spec §5 描述的事件触发 invalidation + 5s 内可读到新值。

**RED**：3 个测试
- 首次 GET → 写 cache（invalid=0）
- 标记某品项 invalid → 再次 GET → 重新计算
- 标记 invalid 但 5s 内再次 GET → 仍重算（因为 invalid=1）

**GREEN**：
- view 内：先读 cache，invalid=0 → 直接返回；invalid=1 或无 → 重算 + 写 cache
- 暴露 `mark_procurement_invalid(item_id)` 函数供其他 blueprint 调用
- 同时在 `procurement_config` 缺省时插入 cover_days=14, min_absolute=0

**commit**：`feat(procurement): cache + invalidation helper`

---

### TASK 8 — invalidation 钩子接入 4 个事件源（RED → GREEN）

**目标**：出库/入库/盘点/补货完成后自动 mark invalid。

**RED**：4 个测试
- `POST /outbound` approve → 该品项 `procurement_cache.invalid=1`
- `POST /restock` approve → 同上
- `POST /stocktake` approve → 同上（盘点改变 current_qty）
- `POST /adjustment` approve → 同上

**策略**：**不**直接修改 4 个 blueprint 文件——这是**非本子项目代码修改**。改用"事件总线"模式：
- 在 `blueprints/_helpers.py` 加一个 `procurement_invalidate(item_id)` 函数
- 4 个 blueprint 各自在 approve 处加一行调用（**最小化**改动：1 行 import + 1 行调用）

**commit**：`feat(procurement): hook invalidation into outbound/restock/stocktake/adjustment`

---

### TASK 9 — `POST /procurement/store/accept` CSV 导出（RED → GREEN）

**目标**：spec §1.3 + §0.7。

**RED**：测试
- POST accept → 200 + `filename` + `item_count` + `download_url`
- 下载链接 → CSV，列 = 4 (item_id, item_name, suggested_qty, unit, note)
- UTF-8 BOM 在文件头
- CSV 文件存在于 tmp 目录

**GREEN**：
- view：调 `compute_store_procurement` 拿 items
- 用 `csv` 模块写文件 → `tempfile.NamedTemporaryFile(delete=False, suffix='.csv', prefix=f'procurement_acceptance_{wh_code}_', dir=tempfile.gettempdir())`
- 第一字节写 `﻿`（BOM）
- 4 列：item_id, item_name, suggested_qty, unit, note
- 响应含 filename + download_url
- 第二个路由 `GET /procurement/store/accept/download?filename=<name>` → `send_from_directory(tempfile.gettempdir(), filename, as_attachment=True)`

**commit**：`feat(procurement): POST /accept + CSV download`

---

### TASK 10 — 权限：staff 可见本店，manager/admin 可见全平台（RED → GREEN）

**目标**：spec §1.3 权限矩阵 + PRD §1.3。

**RED**：
- staff 调用 `/procurement/store?warehouse_code=<other>` → 403
- staff 调用 `/procurement/store?warehouse_code=<own>` → 200
- manager/admin → 任何 warehouse_code 200
- staff 接受（POST accept）→ 200（自己的店）

**GREEN**：
- 在 blueprint 上加 `@require_role("staff")`（最低 staff 即能看）
- 加 helper `_check_warehouse_access(wh_code)`：admin 跳过；其他查 `warehouse_users` 是否有该 (user, warehouse)

**commit**：`feat(procurement): per-warehouse access check`

---

### TASK 11 — 最小 HTML 页面（让 E2E 有目标）（RED → GREEN）

**目标**：spec §8 templates。

**RED**：
- `GET /procurement/store` (HTML, no json) → 200 + 列出 items
- 包含 "采纳" 按钮（POST /procurement/store/accept）

**GREEN**：2 个 templates + 2 个 view 路由（HTML 版）

**commit**：`feat(procurement): /procurement/store + /procurement/hub HTML pages`

---

### TASK 12 — 集成验证 + PR 描述

**目标**：跑全套 pytest + ruff，写 PR 描述 + push draft PR。

**commit**：无（不污染提交历史）。

---

## 2. 任务依赖图

```
TASK 0 ──► TASK 1 ──► TASK 2 ──► TASK 3 ──► TASK 4 ──► TASK 5 ──┐
                                                              ├─► TASK 6 ──► TASK 7 ──► TASK 8
                                                                                      │
                                                              TASK 9 ──► TASK 10 ───┤
                                                                                     │
                                                              TASK 11 ──► TASK 12 ◄──┘
```

TASK 5 可在 TASK 4 之后或合并进 TASK 4。TASK 8 必须在 TASK 7 之后（依赖 helper）。
TASK 11 独立，可在 TASK 7 后做。

---

## 3. 验证门

- `pytest -q` → 0 失败（预期 ≥ 110 passed，比子项目 1 多 ~12 个新测试）
- 新代码 ruff clean
- `git log feat/procurement --oneline` 显示 ≥ 12 个 commit
- 草稿 PR body 含：
  - 引用 PRD §2.2
  - 文件清单
  - pytest 末尾输出
  - 6 个 spec 自决项显式声明（与子项目 1 PR 风格一致）
  - Open questions：CSV 临时文件 24h 清理（**未实现**，仅在 PR 描述中标注 future work）

---

## 4. 风险

- **TASK 8 invalidation 钩子接入 4 个 blueprint** — 这是跨子项目代码修改，必须用最小化（一行调用）。任何 4 个 blueprint 内的意外 break 都立刻 bail（按 runbook 规则）。
- **CSV 临时文件** — 24h 清理未实现，PR 描述标注 future work。如 owner 要求本子项目做，需要加一个 scheduler tick + os.unlink 逻辑，工作量 1 个 TASK。
- **5s TTL vs invalidation** — PRD §2.2.5 写"5 秒内可读到新值即可"。spec 选 invalidation（更精确、更少写库）。如果 owner 倾向 TTL（更简单），改 spec 即可——1 行代码差异。
