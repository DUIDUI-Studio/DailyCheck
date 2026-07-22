# MCP 工具数据计算逻辑对照表

> 生成日期：2026-07-21
> 覆盖工具：13 个 MCP 工具（items_list / items_detail / movements_list / restock_create / restock_list / outbound_create / outbound_list / outbound_rollback / warehouse_consumption / item_consumption / item_forecast / procurement_store / procurement_hub）

---

## 一、基础查询类（无计算）

### items_list
| 项目 | 说明 |
|------|------|
| 原始表 | `items` + `categories` LEFT JOIN（按 `category_id` 取品类名） |
| 计算逻辑 | 无计算，直接 SELECT 返回；LEFT JOIN 防止 category 缺失时丢行 |
| 输出字段 | `id, sku, name, category_id, category_name, current_stock, safety_stock, unit, unit_cost, gram_per_unit, updated_at` |

### items_detail
| 项目 | 说明 |
|------|------|
| 原始表 | `items` + `categories`（相关子查询，按 `category_id` 取品类名） |
| 计算逻辑 | 无，直接 SELECT 返回；带 `category_name` |
| 输出字段 | `id, sku, name, category_id, category_name, current_stock, safety_stock, unit, unit_cost, gram_per_unit, updated_at` |

### outbound_list
| 项目 | 说明 |
|------|------|
| 原始表 | `outbound_requests` + `items` JOIN |
| 计算逻辑 | 无，按 `created_at DESC` 返回 |
| 输出字段 | `id, item_id, item_name, quantity, reason, status, rolled_back, created_at` |

---

## 二、写入类（状态变更）

### restock_create
| 项目 | 说明 |
|------|------|
| 原始表 | `items`, `restock_requests`, `stock_movements` |
| 计算逻辑 | 1. INSERT `restock_requests`（status='入库'）<br>2. `items.quantity += qty`<br>3. INSERT `stock_movements(action='补货入库', delta=+qty)` |
| 输出字段 | `id, item_id, quantity, warehouse_code` |

### outbound_create
| 项目 | 说明 |
|------|------|
| 原始表 | `items`, `outbound_requests`, `stock_movements` |
| 计算逻辑 | 1. 校验 `qty ≤ current_qty`（不足则拦截）<br>2. INSERT `outbound_requests`（status='出库', rolled_back=0）<br>3. `items.quantity -= qty`<br>4. INSERT `stock_movements(action='出库', delta=-qty)`<br>5. 失效 `procurement_cache` 中该品记录 |
| 输出字段 | `id, item_id, quantity, warehouse_code` |

### outbound_rollback
| 项目 | 说明 |
|------|------|
| 原始表 | `items`, `outbound_requests`, `stock_movements` |
| 计算逻辑 | 1. 校验请求存在且未回退<br>2. `items.quantity += requested_quantity`<br>3. INSERT `stock_movements(action='出库回退', delta=+qty)`<br>4. `outbound_requests.rolled_back = 1`<br>5. 失效 `procurement_cache` |
| 输出字段 | `id, item_id, quantity` |

---

## 三、流水记录类

### movements_list
| 项目 | 说明 |
|------|------|
| 原始表 | `outbound_requests`（非回退）+ `stock_movements` |
| 计算逻辑 | 两表分别查询，合并结果集，按 `created_at DESC` 排序，最多 200 条 |
| 输出字段 | `id, type, item_id, item_name, qty, reason, created_at` |

### restock_list
| 项目 | 说明 |
|------|------|
| 原始表 | `stock_movements` + `items` JOIN |
| 计算逻辑 | 过滤 `action='restock'` 或 `'补货入库'`，按 `created_at DESC` |
| 输出字段 | `id, item_id, item_name, qty, reason, created_at` |

---

## 四、消耗分析类

### warehouse_consumption
| 项目 | 说明 |
|------|------|
| 原始表 | `items`, `categories`, `outbound_requests`, `production_run_items`, `production_runs`<br>+ `stocktakes` + `stocktake_batches`（用于 `warehouse_turnover`） |
| 消耗来源 | `outbound_requests.requested_quantity`（非生产原料、非回退）<br>+ `production_run_items.actual_qty`（非回退）|
| **daily_avg** | `consume_qty / window_days`（窗口总天数:7、14 或 30） |
| **turnover_rate** | `consume_qty / current_quantity`（消耗量占当前库存比例，2 位小数） |
| **consume_pct** | `(consume_qty / 仓库窗口总消耗) × 100`（该品占全库总消耗百分比） |
| 支持参数 | `days`（默认 30）、`sort_by`（qty/value/turnover/name） |
| 输出结构 | **dict**（`{"items": [...], "warehouse_turnover": {...}}`），非 array —— breaking change |
| `items[]` 字段 | `rank, item_id, sku, name, category_name, unit, current_stock, safety_stock, consume_qty, active_days, daily_avg, turnover_rate, consume_pct, first_date, last_date` |
| `warehouse_turnover` 字段 | 见下表 |

#### `warehouse_turnover` 字段（30 天财务周转率）

| 子字段 | 类型 | 含义 |
|---|---|---|
| `window_days` | int | 固定 `30`（不随入参 `days` 变） |
| `warehouse_cogs_value` | float | `Σ(各品 cogs_value)`，每个 cogs_value = 30 天消耗 × 当前 `unit_cost` |
| `warehouse_avg_inventory_value` | float | `Σ(各品 avg_inventory × unit_cost)`，各品 avg 用 `get_inventory_turnover` 的盘点加权 |
| `turnover_value` | float \| null | `warehouse_cogs_value / warehouse_avg_inventory_value` |
| `items_with_turnover` | int | 实际贡献聚合的 item 数（≥2 锚点、unit_cost > 0） |
| `items_total` | int | 仓库总 item 数 |
| `data_quality` | string | `none`（全无贡献）/ `medium`（部分贡献）/ `high`（全部贡献） |
| `method` | string | 固定 `"stocktake_weighted_sum"` |

| 项目 | 说明 |
|---|---|
| **聚合公式** | `turnover_value = Σ(各品 cogs_value) / Σ(各品 avg_inventory × unit_cost)` —— 金额口径,各品权重按金额自然分布 |
| 跳过条件 | (a) item 无盘点锚点；(b) 窗口内 <2 个锚点；(c) `unit_cost <= 0` |
| 已知偏差 | (a) 各品的 `avg_inventory` 各自用本地盘点窗口加权,跨 item 边界不完全对齐；(b) `unit_cost` 用当前值代历史成本 → 涨价品历史 COGS 被高估；(c) 单品 `avg_inventory` 本身只在 ≥2 锚点时才有意义 |
| 不能算的情况 | 全仓库所有 item 都没锚点 → `turnover_value=null`、`data_quality="none"` |

### item_consumption
| 项目 | 说明 |
|------|------|
| 原始表 | 同 `warehouse_consumption`（消耗窗）+ `stocktakes` + `stocktake_batches`（仅当 `include_turnover=True`） |
| 窗口 | 四个独立窗口：`7天`、`14天`、`30天`、`月度`（28天近似） |
| **daily_avg** | `qty / window_days`（7天窗口=7，14天窗口=14，30天窗口=30，月度=28） |
| 周度分解 | 近 4 周分别统计出库 + 生产消耗量 |
| 冷启动规则 | 7 天窗口内无记录 → 返回 `{qty:0, daily_avg:0}` |
| **可选: `inventory_turnover`** | 盘点加权平均库存 + COGS 周转率。计算见下表。`include_turnover=false` 默认不返回 |
| 输出字段 | `item_id, sku, name, category_name, unit, current_stock, safety_stock, consume_7d{...}, consume_14d{...}, consume_30d{...}, consume_monthly{...}, weekly[{...}], inventory_turnover?{...}` |

#### `inventory_turnover` 字段（opt-in，盘点锚定）

| 子字段 | 类型 | 含义 |
|---|---|---|
| `window_days` | int | 窗口天数（7/14/30，由 `turnover_days` 决定） |
| `avg_inventory` | float \| null | 窗口内加权平均库存。窗口内盘点锚点 <2 → null |
| `current_inventory` | float | 当前 `items.quantity`（窗口结束时刻） |
| `cogs_value` | float | 窗口内消耗 × 当前 `unit_cost`（COGS 近似，schema 不存每次成本） |
| `turnover_value` | float \| null | `cogs_value / avg_inventory`，avg 为 null 时也是 null |
| `anchors_in_window` | int | 窗口内有效盘点锚点数（排除 `rolled_back=1` 的批次） |
| `anchors_total` | int | 该 item 全部时间的有效锚点数（用于上下文） |
| `data_quality` | string | `none`（无锚点）/ `medium`（1 个锚点）/ `high`（≥2 个锚点） |
| `method` | string | 固定 `"stocktake_weighted_avg"` |

| 项目 | 说明 |
|---|---|
| 锚点来源 | `stocktakes.previous_quantity` × `stocktake_batches.created_at`，过滤 `rolled_back=0` |
| 加权方式 | 相邻锚点之间，前一个锚点的 qty 覆盖整个 gap，权重 = gap 天数；`weighted_avg = Σ(qty×gap) / Σ(gap)` |
| 边界处理 | 窗口起点 prepend 最早锚点的 qty（保守外推）；窗口终点 append 当前 qty |
| **COGS 公式** | `Σ(出库数量 × unit_cost) + Σ(生产领料 × unit_cost)`，窗口内，rolled_back=0 出库排除 |
| **turnover 公式** | `cogs_value / avg_inventory` —— 窗口期内的"库存被消耗几轮" |
| 不能算的情况 | 窗口内 `<2` 个有效锚点 → `avg_inventory=null`、`turnover_value=null` |
| 已知偏差 | (a) `unit_cost` 用当前值代历史成本 → 涨价品历史 COGS 被高估；(b) 全量盘点才能可靠,部分盘点只能给"该品"信号 |

---

## 五、预测与采购类（核心计算）

### item_forecast
| 项目 | 说明 |
|------|------|
| 原始表 | `outbound_requests`, `production_run_items`, `production_runs` |
| 数据窗口 | 近 30 天，非回退记录 |
| **权重公式** | `weight = 30 - days_ago`（线性衰减，越近权重越高） |
| **daily_avg** | `Σ(weight × qty) / Σ(weight)`，结果量化至 2 位小数 |
| **forecast_total** | `daily_avg × horizon_days`，量化至 2 位小数 |
| **置信度** | `0-6 条 → cold_start`<br>`7-13 条 → low`<br>`14-29 条 → medium`<br>`≥30 条 → high` |
| 支持参数 | `horizon_days`（默认 14） |
| 输出字段 | `item_id, warehouse_code, horizon_days, daily_avg, forecast_total, confidence, computed_at, data_status` |

### procurement_store
| 项目 | 说明 |
|------|------|
| 原始表 | `items`, `outbound_requests`, `restock_requests`, `procurement_cache` |
| 冷启动过滤 | 30 天内出库记录 < 7 条的品直接跳过，不参与计算 |
| **daily_avg** | 同 `item_forecast`（线性衰减加权平均） |
| **in_transit_qty** | `Σ(restock_requests.requested_quantity)`（status 不属于"已到货"/"已取消"的在途总量） |
| **safety_stock** | `max(daily_avg × cover_days, min_absolute)`，量化至 2 位小数 |
| **suggested_qty** | `ceil(max(0, safety_stock - current_qty - in_transit_qty))`，向上取整，最小为 0 |
| 配置参数 | `cover_days`（默认 14）、`min_absolute`（默认 0.0） |
| 结果缓存 | 写入 `master.procurement_cache` 表；过滤 `suggested_qty ≤ 0` 的品 |
| 输出字段 | `warehouse_code, computed_at, items[{item_id, item_name, current_qty, in_transit_qty, daily_avg, forecast_total_horizon, safety_stock, suggested_qty}]` |

### procurement_hub
| 项目 | 说明 |
|------|------|
| 原始表 | 所有仓库的 `procurement_store` 结果汇聚 |
| **total_suggested_qty** | `Σ(store.suggested_qty)`（跨仓库汇总） |
| **stores_needing** | `COUNT(store where suggested_qty > 0)`（需要该品的门店数） |
| **stores_detail** | `[(warehouse_code, suggested_qty), ...]`（各仓库明细列表） |
| 排序规则 | 按 `total_suggested_qty` 降序 |
| 输出字段 | `computed_at, items[{item_id, item_name, total_suggested_qty, stores_needing, stores_detail}]` |

---

## 六、汇总总表

| 工具 | 原始表 | 核心计算 |
|------|--------|---------|
| `items_list` | `items` + `categories` | LEFT JOIN 取 `category_name`，无计算 |
| `items_detail` | `items` | 无，直接返回 |
| `movements_list` | `outbound_requests` + `stock_movements` | 合并后按时间排序 |
| `restock_create` | `items` + `restock_requests` + `stock_movements` | `quantity += qty` |
| `restock_list` | `stock_movements` | 过滤 `action='restock'` |
| `outbound_create` | `items` + `outbound_requests` + `stock_movements` | `quantity -= qty`，失效采购缓存 |
| `outbound_list` | `outbound_requests` | 无计算，直接返回 |
| `outbound_rollback` | `items` + `outbound_requests` + `stock_movements` | `quantity += qty`，标记 `rolled_back=1` |
| `warehouse_consumption` | `items` + `outbound_requests` + `production_run_items` + `stocktakes`/`stocktake_batches` | `daily_avg = consume_qty / window_days`（7、14 或 30），`active_days`、`turnover_rate`、`consume_pct`；`warehouse_turnover = Σ cogs / Σ avg_inventory_value`（30 天，固定） |
| `item_consumption` | 同上 + 可选 `stocktakes`/`stocktake_batches` | 三窗口（7d/30d/月度）`daily_avg = qty / window_days`（7/30/28）+ 周度分解；opt-in `include_turnover=true` 加 `inventory_turnover`（盘点加权平均 + COGS 周转） |
| `item_forecast` | 同上 | 加权平均 `daily_avg`（线性衰减）+ 置信度分类 |
| `procurement_store` | `items` + `outbound_requests` + `restock_requests` | `safety_stock`、`in_transit_qty`、`suggested_qty` |
| `procurement_hub` | 所有仓库 `procurement_store` | 跨仓汇总 `total_suggested_qty`、`stores_needing` |

---

## 七、关键公式速查

```
# 加权平均（日均消耗，预测/采购共用）
daily_avg = Σ((30 - days_ago) × qty) / Σ(30 - days_ago)

# 安全库存
safety_stock = max(daily_avg × cover_days, min_absolute)

# 建议采购量
suggested_qty = ceil(max(0, safety_stock - current_qty - in_transit_qty))

# 库周转率
turnover_rate = consume_qty / current_quantity

# 消耗占比
consume_pct = (consume_qty / 仓库总消耗) × 100

# 盘点加权平均库存（item_consumption opt-in）
avg_inventory = Σ(qty_i × gap_i) / Σ(gap_i)   # gap_i = 锚点 i 到下一锚点的天数

# 财务库存周转率（item_consumption opt-in，COGS 近似）
turnover_value = cogs_value / avg_inventory
cogs_value = Σ(出库qty + 生产领料qty) × 当前 unit_cost   # 窗口内、rolled_back=0

# 仓库级财务周转率（warehouse_consumption 必出）
warehouse_turnover_value = Σ(各品 cogs_value) / Σ(各品 avg_inventory × unit_cost)
                            # 30 天,各品 cogs/avg 见上,unit_cost > 0 才参与
```


