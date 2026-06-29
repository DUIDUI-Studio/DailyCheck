# 库存消耗预测 — 子项目 1 plan

**版本**：v1（2026-06-29）
**状态**：实施中。引用 spec `2026-06-29-forecast-design.md`。
**目标读者**：实施 agent。

---

## 0. 执行约定

- 一任务 = 一 commit。任务顺序 = 提交顺序。
- 每任务内部：先写失败测试（commit `test:`），再写最小实现（commit `feat:` / `fix:`），需要重构再单独 `refactor:`。
- 所有新代码必须 `ruff check` 通过（任务 0 添加 ruff 配置后强制）。
- 任务并行性：本 plan 中任务 5、6、7 彼此独立，可在同一个 commit 内实现多文件（仍算 1 commit = 1 task），但保持 commit 粒度按"用户可见行为"切。

---

## 1. 任务清单

### TASK 0 — 添加 ruff 配置

**目标**：项目从此能跑 `ruff check .`。
**改动**：`pyproject.toml`（新增）
**内容**：
```toml
[tool.ruff]
line-length = 100
target-version = "py310"

[tool.ruff.lint]
select = ["E", "F", "W", "I", "B", "UP"]
```

**验证**：`ruff check .` 退出码 0（对现有代码也必须 0；如果现有代码违反 E/F，先 `ruff check . --fix` 自动修，剩下的 `ruff check .` 必须 0；如有不归 E/F 类的，预留 ignore，不在本子项目内清理以免越界）。

**commit**：`chore(forecast): add ruff config (lock-step with subproject 1 spec)`

---

### TASK 1 — `compute_daily_avg` + `classify_confidence` 纯函数（RED → GREEN）

**目标**：`blueprints/forecast_pure.py` 内两个纯函数完整可用，单元测试全绿。

**RED 步骤**：写 `tests/test_forecast_pure.py`，覆盖：
- `compute_daily_avg([])` → 0.0
- 全部同日（10 条同日 1.0）→ 1.0
- 30 天线性均匀（每天 1.0）→ 1.0（等权但权重按距离衰减验证：每条权重 1..30，总量 30*1.0=30，加权平均 = 30/15 = 2.0 是不对的；**本测试的预期值需要落到 spec 锁定项**：spec 写 `weight_i = (30 - i)`，i=0..n-1，若 n=30，则 sum(weight) = 30+29+...+1 = 465；sum(weight*qty) = 1*30+1*29+...+1*1 = 465；平均 = 1.0）
- 稀疏（仅前 5 天各 1.0）→ 验证：i=0..4 权重 30,29,28,27,26 = 140，总消耗 5，daily_avg = 5/140 * 140 = 5? 不对，重新计算：daily_avg = sum(w*q) / sum(w) = (1*30+1*29+1*28+1*27+1*26) / 140 = 140/140 = 1.0
- 突发（最近 1 天 100，其他 6 天各 1.0）→ i=0 权重 30, qty=100 → 3000；i=1..6 权重 29..24，qty=1 → 159；sum(w*q) = 3159；sum(w) = 30+29+...+24 = 189；daily_avg = 3159/189 ≈ 16.71
- `classify_confidence(0..6)` → 'cold_start'
- `classify_confidence(7..13)` → 'low'
- `classify_confidence(14..29)` → 'medium'
- `classify_confidence(30..1000)` → 'high'

**GREEN 步骤**：实现两个函数。`compute_daily_avg` 接受 `list[tuple[datetime, float]]`，按"距今多少天"映射权重（今天 = 0，昨天 = 1，…）。`classify_confidence` 接受 `int`。

**验证**：`pytest -q tests/test_forecast_pure.py` 全绿。

**commit 序列**：
1. `test(forecast): RED pure fns compute_daily_avg + classify_confidence`
2. `feat(forecast): implement compute_daily_avg + classify_confidence`

---

### TASK 2 — `compute_forecast_total`（TASK 1 顺手补完，但单独 commit）

**目标**：`compute_forecast_total(daily_avg, horizon) -> float` 量化到 2 dp。

**RED**：测试 `(0, 0)` → 0.0；`(2.5, 14)` → 35.0；`(0.1, 3)` → 0.3（验精度）；`(1.234, 2)` → 2.47（验 quantize）。

**GREEN**：用 `Decimal(str(daily_avg * horizon)).quantize(Decimal('0.01'))` 后转 float。

**commit**：`feat(forecast): compute_forecast_total 2dp quantize`

---

### TASK 3 — `forecast_runs` schema（master.db 新表）

**目标**：`db/__init__.py` 的 `MASTER_SCHEMA` 加 `forecast_runs`；`init_master_db` 立即生效。

**RED**：在 `tests/test_forecast_scheduler.py` 写一个集成测试，断言 `get_master_db()` 后能查到 `forecast_runs` 表（用 `sqlite_master` 查询）。

**GREEN**：在 `MASTER_SCHEMA` 末尾追加 `forecast_runs` CREATE TABLE；调用 `init_master_db()` 不报错。

**commit**：`feat(forecast): forecast_runs table in master.db`

---

### TASK 4 — `/forecast/item/<id>` 路由（蓝基本体）

**目标**：`GET /forecast/item/<item_id>?horizon_days=14` 返回 §1 契约 200。

**RED**：`tests/test_forecast_route.py` 写：
- 已知 item，3 条 outbound（每条 5.0）→ 200；`data_status="ok"` 或 "cold_start"（看 n_records 落在哪）；`forecast_total` 数字合理
- 未知 item → 404 + `{"error": "not_found"}`
- `horizon_days=0` → 400 + `{"error": "invalid_horizon"}`
- `horizon_days=91` → 400
- `horizon_days=abc` → 400
- item 没有任何 outbound → `data_status="cold_start"`，`daily_avg=0`，`forecast_total=0`
- 仅 5 条 outbound（< 7）→ `data_status="cold_start"`

**GREEN**：实现 `blueprints/forecast.py`：
- 从 `g.warehouse_db_path` 读 warehouse_code（从 session 的 warehouse_id 反查 master.db）
- 校验 `horizon_days` 整数且 1..90
- 查 `items WHERE id=?` → 404 if not found
- 查过去 30 天的 `outbound_requests WHERE item_id=? AND rolled_back=0 AND created_at >= datetime('now','-30 days')`
- 调纯函数算 `daily_avg` / `forecast_total` / `confidence` / `data_status`
- 返回 JSON

**commit**：`feat(forecast): GET /forecast/item/<id> route`

---

### TASK 5 — `/forecast/product/<id>` 路由

**目标**：与 item 同构，源数据为 `production_runs.rolled_back=0` 的 `output_qty`。

**RED**：测试镜像 TASK 4（5 条 production_run → cold_start；30+ 条 → high；未知 product → 404）

**GREEN**：在同 blueprint 内加 view，复用纯函数

**commit**：`feat(forecast): GET /forecast/product/<id> route`

---

### TASK 6 — `POST /forecast/recompute` 手动重算（幂等）

**目标**：连续两次 POST 返回相同 `last_run_id`。

**RED**：测试连续 2 次 POST → 第二次 `last_run_id` == 第一次。

**GREEN**：实现语义 = "如果当前没有 running run，插一条 status='success' 的新行；否则返回现有 running/success 行的 id"。

**commit**：`feat(forecast): POST /forecast/recompute idempotent`

---

### TASK 7 — `forecast_last_success_at` 加到 `/health`

**目标**：`GET /health` JSON 输出含 `forecast_last_success_at` 字段（或保持文本响应但不影响 health 测点；**spec 选 JSON 化以承载该字段**）。

**RED**：测试 `GET /health` → 200，JSON 含 `forecast_last_success_at` 字段（值可 null）。

**GREEN**：把 `/health` 路由从返回 `"ok"` 文本改为返回 JSON `{status: "ok", forecast_last_success_at: <iso or null>}`。**注意：这是行为变更**，spec 锁定项 §1 节的"健康端点契约"未在 PRD 锁定；本子项目认为该变更必要用于 §3.6 可观测性，**但会同时让旧调用方（如果有）继续读到 "ok" 状态**——通过先尝试 JSON 解析来保持兼容？这里选择**纯 JSON** 并在 PR 描述里标注 break-by-design（`/health` 之前只返回纯文本 "ok"，改成 JSON 化）。

**commit**：`feat(forecast): /health returns JSON with forecast_last_success_at`

---

### TASK 8 — 线程调度器 + 重启恢复

**目标**：进程启动时启动后台线程，每日 03:00 跑一次（同时支持 `POST /forecast/recompute` 触发）；进程启动时把"上次未完成"的 run 标记为 failed。

**RED**：
- 测试 `_start_scheduler()` 启动后 5 秒内能查到一条新 run（用 `time.sleep` + monkeypatched 时间）
- 测试"上次未完成"恢复：手工插一条 `status='running', finished_at=NULL` 的 run，再调用 `_recover_orphaned_runs()`，断言它变 `status='failed', finished_at=<now>`

**GREEN**：实现：
- `blueprints/forecast.py` 内 `_start_scheduler()` 启动 `threading.Thread(daemon=True, target=_scheduler_loop)`；循环每秒检查 `datetime.now().hour == 3 and .minute == 0`，满足时调 `_run_daily_forecast()`，防重入用 module-level lock
- `_run_daily_forecast()`：插 `forecast_runs(status='running')`，遍历所有仓库（查 master.db `warehouses`），对每个仓库 `outbound_requests` 跑一遍"取数 + 算"（**不算 item 级缓存，只统计 processed 数量**），更新 `items_processed`，`finished_at = now`，`status = 'success'`
- `_recover_orphaned_runs()`：查 `status='running' AND finished_at IS NULL`，改为 `'failed'`
- `create_app()` 末尾调用 `_recover_orphaned_runs()` + `_start_scheduler()`

**commit**：`feat(forecast): scheduler + orphaned run recovery`

---

### TASK 9 — 权限：staff 不可访问

**目标**：`/forecast/*` 路径仅 admin / manager 可见。

**RED**：测试用 `staff` role 的 logged_client → 403

**GREEN**：在 blueprint 内加 `@require_role('admin', 'manager')` 装饰器，或用现有 `permissions.py` 工具。复用 `permissions.py` 现有装饰器（如有）；若无，新建 `require_role` 在 `blueprints/forecast.py` 局部实现。

**commit**：`feat(forecast): require_role admin/manager for /forecast`

---

### TASK 10 — 最小 UI 页面（让 E2E 有目标）

**目标**：`templates/forecast.html` 列出当前仓库的 items（最多 20 条），每条显示日均 + 预测总量；admin 可见"手动重算"按钮，点击后 POST `/forecast/recompute`。

**RED**：test_client 渲染 `/forecast` → 200，含 "手动重算" 按钮 + 至少一个 item 名。

**GREEN**：写模板 + view 渲染 context。CSS 复用 `static/style.css`（不新增）。

**commit**：`feat(forecast): /forecast html page + manual recompute button`

---

### TASK 11 — 集成层错误兜底（DB 锁 3 次重试 + access.log + /health 标记）

**目标**：模拟 DB 锁 → 跑批标记为 failed + `/health` JSON 包含 `forecast_db_lock_failures: int` 字段。

**RED**：mock `sqlite3.connect` 抛 `OperationalError("database is locked")` 3 次 → 跑批后查 `forecast_runs` 一条 `status='failed'` + `access.log` 含 "forecast_lock" + `GET /health` 包含 `forecast_db_lock_failures >= 1`

**GREEN**：在 `_run_daily_forecast()` 包 `OperationalError` 捕获 3 次重试，仍失败则写 `access.log`（app.logger.warning） + `forecast_runs.status='failed'` + 累加到 `access.log` 文本（**不**新增表，复用现有 access.log 路径）

**commit**：`feat(forecast): db lock retry + access.log + health counter`

---

### TASK 12 — 端到端验证 + PR 描述

**目标**：跑全套 pytest + ruff，写 PR 描述到 `docs/superpowers/prs/2026-06-29-forecast.md`，push 草稿 PR。

**步骤**：
1. `pytest -q` 跑完，截末尾 20 行
2. `ruff check .` 跑完，截全部输出
3. `git diff main...HEAD --name-only` 收集文件清单
4. 尝试 Playwright E2E（环境可达 → 截图；不可达 → 标注 manual）
5. 写 PR 描述
6. `git push -u origin feat/forecast`
7. `gh pr create --draft --body-file ...`

**commit**：本任务不产生 commit（不污染提交历史）。

---

## 2. 任务依赖图

```
TASK 0 ──► TASK 1 ──► TASK 2 ──► TASK 4 ──┐
                  │                       ├─► TASK 8 ──► TASK 11 ──► TASK 12
                  └─► TASK 3 ──► TASK 6 ──┤                ▲
                                          │                │
                              TASK 5 ─────┤                │
                                          │                │
                              TASK 7 ─────┤                │
                                          │                │
                              TASK 9 ─────┘                │
                                                            │
                              TASK 10 ──────────────────────┘
```

线性主链：0 → 1 → 2 → 3 → 4 → 6 → 7 → 8 → 9 → 10 → 11 → 12
TASK 5 可在 TASK 4 后插入（与 6/7/8 并行开发，但每个仍是独立 commit）。

---

## 3. 验证门（最终）

- `pytest -q` → 0 失败
- `ruff check .` → 0 失败
- `git log feat/forecast --oneline` 显示 ≥ 12 个 commit（按 TASK 切）
- `gh pr view` 显示 PR draft 已开，body 含所有引用 PRD §2.1 子节
- 草稿 PR 描述"Open questions / risks" 列出：
  1. "消耗 = outbound_requests 单一来源" 的 spec 自决项（如有变更请求，owner 应在此回）
  2. "/health 改 JSON 化" 是 break-by-design
  3. E2E 是否实际跑通的说明

---

## 4. 风险与回退点

- **TASK 8 调度器**：threading 实现，单进程 OK；若未来用 gunicorn 多 worker 会变成每 worker 都跑一遍。spec 接受此限制（多 worker 会在 access.log 出现重复 run 行，但 idempotent 不影响业务）。**风险**会在 PR 描述中标注。
- **TASK 7 /health 改 JSON**：可能影响外部 health 探针。在 PR 描述中标注 break。
- **TASK 9 权限**：现有 `permissions.py` 装饰器需先 read 才能复用；若无，本任务本地实现。
