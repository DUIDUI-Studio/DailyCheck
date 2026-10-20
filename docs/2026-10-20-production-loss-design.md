# 生产损耗分析与口味消耗速度 · 设计定稿

> 2026-10-20 立项。基于本地 6 仓实测数据（wh_002 / wh_003 / wh_004 / wh_006 有生产数据）。
> 目标：把「生产损耗」从盘点事后补记，变成生产当期可观测的指标；并把各口味消耗速度接进库存健康表。
> 关联：`docs/2026-10-15-inventory-health-report-plan.md`（现有健康表）、`reports_health_pure.py`、
> `blueprints/reports.py`、`templates/report_health.html`。

---

## 0. 先说结论：一个必须先纠正的前提

原始需求是「把配料消耗记录与出库记录对比，算生产损耗误差率」。**按现有实现，这个差值恒等于 0，做出来会是一个永远显示 0 的死指标。**

实测（4 个有生产数据的仓，全期）：

| 仓 | run_items 行 | 生产领料出库行 | 一一对应 | 数量不一致 | 孤儿行 |
|----|------------|--------------|---------|-----------|--------|
| wh_002 | 823 | 823 | 823 | 0 | 0 |
| wh_003 | 890 | 890 | 890 | 0 | 0 |
| wh_004 | 376 | 376 | 376 | 0 | 0 |
| wh_006 | 75 | 75 | 75 | 0 | 0 |

原因在 `blueprints/production.py:293-315`：生产提交时对每行配料**同时**写三条记录 ——
`production_run_items.actual_qty`、`outbound_requests`（`reason='生产领料(run=#N)'`）、
`stock_movements`（`action='生产消耗'`）。出库记录是配料消耗的**副本**，不是独立证据链。

第二个反直觉事实：**BOM 计划 vs 实际也几乎恒等**。wh_003 全部 890 行里，
881 行 `planned_qty == actual_qty`（98.99%）。原因在 `production.py:272`：
`actual = parse_qty(raw) if raw != "" else planned` —— 表单不填实际用量，系统直接拿计划量顶上。

剩下 9 行**不是"轻微超耗"，而是清一色的配方漏配**：

| 品项 | 次数 | planned_qty | actual_qty | 涉及产品 |
|------|------|------------|-----------|---------|
| PP072 海盐 | 7 | **0** | 0.01 | 焦糖海盐、黑胡椒蜂蜜海盐 |
| PP116 稳定剂 | 2 | **0** | 0.01 | A 大白兔 |

即 BOM 里 `qty_per_unit` 被配成 0，但生产时每次都实际投 0.01。现有 `_overuse_pct()`
对这种情况返回硬编码的 100%，逐 run 聚合后又被其他原料稀释成 0~10%，
**聚合口径把"配方漏配"这个结构性问题掩盖成了"轻微超耗"** —— 这正是本次要求
「精确定位到品项」的直接原因（见 §8）。

**损耗真实存在，只是全部沉在盘点里**：

| 仓 | 期间 | 生产消耗量 | 盘亏量 | 盘亏/生产 |
|----|------|-----------|--------|----------|
| wh_002 | 2026-09 | 58.69 | 48.95 | 0.83 |
| wh_004 | 2026-10 | 25.85 | 48.90 | **1.89** |
| wh_006 | 2026-10 | 45.35 | 28.23 | 0.62 |

同期还有一条更刺眼的：**无原因手工出库与生产领料同量级**（wh_003 逐月）：

| 月份 | 生产领料出库 | 无原因手工出库 | 盘点盘亏 |
|------|-------------|--------------|---------|
| 2026-07 | 104.90 | 87.10 | 88.20 |
| 2026-08 | 79.12 | 71.74 | 131.39 |
| 2026-09 | 74.01 | 57.11 | 2387.69 |
| 2026-10 | 47.62 | 49.40 | 53.49 |

**结论：生产领料只覆盖了一半的消耗；另一半走了「不选原因的手工出库」，剩下沉到盘点。
这才是用户所说的「不应在手动盘点时才补充误差」的准确病因。**

设计与实现口径必须建立在这一点上，否则指标方向会错。下面按「四层归因」重定义。

---

## 1. 损耗归因四层模型（核心口径）

损耗不是单一数字，四个层的**归因不同、处置动作不同**。健康表应分层展示，只把最后一层合成一个「生产损耗率」。

| 层 | 名称 | 数据源 | 回答什么问题 | 健康值 |
|----|------|--------|------------|--------|
| **L0** | 单据一致性 | `production_run_items` ↔ `outbound_requests` | 双写有没有断裂（回退不对称 / 删除残留 / 人工 SQL） | 未匹配行 = 0 |
| **L1** | 配方偏差 | `planned_qty` vs `actual_qty`（**仅手工填报行**） | 生产当期投料偏离配方多少 | 超耗率 ≤ 阈值 |
| **L2** | 旁路消耗 | `outbound_requests` 中非生产、非盘点的出库 | 有多少料没走生产单就出去了 | 占比 ≤ 阈值 |
| **L3** | 账实损耗 | `stocktakes.diff < 0` 与期间理论结存 | 盘点一次性补记了多少无法解释的差 | 补记率 ≤ 阈值 |

```
期间理论投入金额 ──┬── L1 配方偏差（生产当期可见）
                  ├── L2 旁路消耗（生产当期可见）
                  ├── 已登记损耗事件（新增，生产当期可见）
                  └── L3 未解释盘亏（事后补记，目标压到接近 0）
```

**「生产损耗率」= (L1 超耗 + L2 旁路 + L3 未解释) ÷ 期间理论投入金额**，
但页面上必须拆成四个数字并列，不合成单值 —— 合成值会掩盖"哪一层在恶化"。

### 1.1 与现有 `overuse_pct` 的关系

现有 `compute_production()["overuse_pct"]` ≈ L1，但有两个缺陷，新设计要修：

1. **没排除默认值行**：`actual=planned` 的 881 行把分母灌大，把真实偏差稀释掉。
2. **没有覆盖率**：当只有 9 行是真实填报，`overuse_pct` 再准也只代表 1% 的样本。
   → 新口径必须同时输出 `l1_coverage`（真实填报行数 / 总行数），并在页面上显式标注。

---

## 2. 字段设计

### 2.1 迁移约束（铁律）

- 新列一律进 `migrate_warehouse_db_columns()` 的 PRAGMA 守卫块，**不要**只写进 `WAREHOUSE_SCHEMA`。
- 新表用 `CREATE TABLE IF NOT EXISTS` 写进 `WAREHOUSE_SCHEMA`，但**必须 bump
  `WAREHOUSE_SCHEMA_VERSION`（2 → 3）**，否则已打 stamp 的库会在 `if version >= N: return` 处直接跳过。
- 所有 ALTER 必须可重复执行（PRAGMA 判列），不得在 `WAREHOUSE_SCHEMA` 里写 ALTER。

### 2.2 `production_run_items` 增两列

```sql
-- 区分「操作员真填了实际用量」和「系统拿计划量兜底」。
-- 这是救活 L1 的唯一开关：不加这列，L1 永远是 0。
ALTER TABLE production_run_items ADD COLUMN planned_default INTEGER NOT NULL DEFAULT 1;
-- 显式外键到双写的出库行，替代 reason 字符串解析（见 L0）。
ALTER TABLE production_run_items ADD COLUMN outbound_req_id INTEGER;
```

- `planned_default`：`production.py` 里 `raw == ""` 分支写 1，否则 0。**历史行回填 1（保守）**。
- `outbound_req_id`：双写时把 `cur.lastrowid` 回填进来。历史行用 reason 解析回填（见 2.5）。
- 索引：`CREATE INDEX idx_pri_outbound ON production_run_items(outbound_req_id)`。

### 2.3 `outbound_requests` 增一列

```sql
-- 结构化出库来源。现状靠 reason LIKE 字符串判断（5 处硬编码），脆弱且不可索引。
ALTER TABLE outbound_requests ADD COLUMN source_type TEXT;
-- 'production' | 'stocktake_loss' | 'manual' | NULL(历史未分类)
```

回填规则（顺序敏感，先匹生产再匹盘点）：

```sql
UPDATE outbound_requests SET source_type = 'production'
 WHERE source_type IS NULL AND reason LIKE '生产领料(run=#%';
UPDATE outbound_requests SET source_type = 'stocktake_loss'
 WHERE source_type IS NULL AND reason LIKE '盘点审核%盘亏';
UPDATE outbound_requests SET source_type = 'manual'
 WHERE source_type IS NULL;
```

写入侧三处同步改：`production.py:301`（production）、`stocktake.py:565`（stocktake_loss）、
`outbound.py:103`（manual）。`source_type` 一旦落地，`reports_health_pure.py` 里
`reason LIKE '生产领料(run=#%'` 的 4 处排除条件都应改用它（保留 reason 作人类可读备注）。

### 2.4 新表：生产损耗事件（把 L3 前移到生产当期）

```sql
CREATE TABLE IF NOT EXISTS production_loss_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER,                    -- 可空：非批次损耗（如整桶过期）
    item_id     INTEGER NOT NULL,
    loss_qty    REAL NOT NULL,               -- 基础单位，正数
    unit_cost   REAL NOT NULL DEFAULT 0,     -- 快照，避免历史成本漂移
    loss_type   TEXT NOT NULL,               -- '配料损耗'|'成品报废'|'称量误差'|'试吃'|'过期'|'其他'
    reason      TEXT,
    created_by  TEXT,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES production_runs(id),
    FOREIGN KEY (item_id) REFERENCES items(id)
);
CREATE INDEX IF NOT EXISTS idx_loss_events_created ON production_loss_events(created_at);
CREATE INDEX IF NOT EXISTS idx_loss_events_run ON production_loss_events(run_id);
```

设计要点：
- **unit_cost 快照**：`items.unit_cost` 会变，历史损耗金额不能跟着变（与 `production_run_items` 现有做法一致 —— 它不存成本，靠实时 join，这是既有缺陷，新表不再复制）。
- **loss_qty 计入库存扣减**：写事件时必须同时写 `stock_movements(action='生产损耗', delta=-qty)`
  并扣 `items.quantity`，否则会和盘点再次重复计。**这是实现时的第一号坑。**
- 若暂不想改库存流转，退化方案：`production_loss_events` 只作为**归因标签**，
  不扣库存，仅用于分摊盘亏 —— 但那样 L3 还是兜底，收益减半。建议首版就扣。

### 2.5 盘点归因（可选，二期）

`stocktake_batches` 增 `loss_cause TEXT`（`'生产损耗'|'失窃'|'记录错误'|'未查明'`），
审核盘亏时由审批人下拉选择。作用是让 L3 内部可拆解，判断"补记"到底是漏记还是真损耗。
一期可以先不做，先看 L3 占比。

### 2.6 L0 历史回填 SQL

```sql
-- 把历史 run_items 关联到对应出库行（reason 解析，仅迁移用一次）
UPDATE production_run_items
   SET outbound_req_id = (
        SELECT o.id FROM outbound_requests o
         WHERE o.reason = '生产领料(run=#' || production_run_items.run_id || ')'
           AND o.item_id = production_run_items.item_id
         LIMIT 1)
 WHERE outbound_req_id IS NULL;
```

⚠️ `SUBSTR` 解析 reason 时注意 `'生产领料(run=#'` 是 **10 个字符**（4 汉字 + 6 ASCII），
起始位是 11 不是 12。写错一位会得到一堆孤儿行（本项目实测踩过：731/823 误报孤儿）。

---

## 3. 计算口径

### 3.1 通用铁律（沿用现有健康表约定）

- 时间过滤**一律 Python 侧 `datetime.now()` 绑定参数**，禁止 SQL `datetime('now')`（CST/UTC 坑）。
  ⚠️ 既有 `blueprints/core.py:_compute_summary_metrics` 第 103-108 行仍在用 `datetime('now')`，
  新代码不要照抄；顺手修掉是独立的低风险 PR。
- 时间列解析一律走 `reports_health_pure.parse_event_ts()`，兼容 16/19 字符两种形态。
- 阈值全部落模块级常量。
- **三态原则**（沿用 #118/#120 铁律）：`deployed` / `absent` / `unknown`。
  分母为 0 或无数据 → 输出 `None`，页面显示「—」，**绝不显示 0**；同时计入 `blind_spots`。

### 3.2 L0 单据一致性

```python
def compute_document_integrity(db, cutoff, now) -> dict:
    """
    window 内未回滚 run 的明细行，与 outbound_requests(source_type='production') 对账。

    matched           : outbound_req_id 非空且该 outbound 存在且未回滚的行数
    orphan_run_item   : run_item 无对应出库行（双写断了）
    orphan_outbound   : 生产类出库行找不到 run_item（人工删了 run 明细）
    qty_delta_value   : Σ|actual_qty − ob_qty| × unit_cost   ← 金额化，避免跨单位不可比
    state             : 'deployed' | 'absent'(窗口无生产) | 'unknown'(reason 无法解析)
    """
```

判据：`orphan_run_item + orphan_outbound == 0` 且 `qty_delta_value == 0` → 绿灯。
非 0 时不要报"损耗"，报"单据异常"，并列出具体 run_id —— 这是数据修复工单，不是经营指标。

### 3.3 L1 配方偏差（仅真实填报行）

```
l1_planned_value = Σ planned_qty × unit_cost     [planned_default = 0]
l1_actual_value  = Σ actual_qty  × unit_cost     [planned_default = 0]
l1_over_value    = Σ max(actual − planned, 0) × unit_cost
l1_overuse_pct   = l1_over_value / l1_planned_value × 100
l1_coverage      = COUNT(planned_default=0) / COUNT(*) × 100
```

边界：
- `planned_default = 1` 的行**计入分母会稀释指标**，必须排除（这正是现有 `overuse_pct` 的问题）。
- `l1_planned_value == 0`（BOM 缺行、`planned_qty=0` 但 `actual>0`）→ 单列为"无计划消耗"，
  不进百分比分母，避免除零与虚高。
- `l1_coverage < 30%` → 指标降级为 `unknown`，页面显示「—」，辅注「仅 N% 批次填报了实际用量」。

### 3.4 L2 旁路消耗

```
l2_value      = Σ o.requested_quantity × i.unit_cost
                WHERE o.source_type = 'manual' AND o.rolled_back = 0 AND o.created_at >= cutoff
l2_share_pct  = l2_value / (l2_value + 生产消耗金额) × 100
l2_rows       = 按 reason 分组 TOP N（空 reason 单独一组）
```

阈值建议：`BYPASS_WARN_PCT = 20`（超过两成消耗脱离生产单 → 黄）。
实测基线：wh_003 近四月手工出库 57~87（生产领料 47~105），**占比稳定在 45%~48%**，
远超阈值 —— 说明这是系统性问题，不是个别漏录。首版上线时大概率整片飘红，这是对的。

补充口径：`l2` 里 `reason` 为空的行单独统计为 `l2_blank_reason`，
这类是"操作员连原因都懒得选"，与"选了'新世界'/'消耗+1'等自由文本"处置方式不同。

### 3.5 L3 账实损耗

**首版（推荐，低风险）：按盘点批次配对的区间法**

对每个 `status='approved'` 的批次 B（按 `created_at` 排序），取上一批次 P 到 B 之间：

```
期间理论消耗金额 = Σ(该区间 stock_movements 中 action ∈ CONSUME_ACTIONS 的 -delta × unit_cost)
期间盘亏金额     = Σ|diff| × unit_cost    WHERE diff < 0 AND batch_id = B
期间盘盈金额     = Σ diff  × unit_cost    WHERE diff > 0 AND batch_id = B   ← 不冲抵，单列
期间损耗事件金额 = Σ loss_qty × unit_cost  (production_loss_events, 同区间)
l3_unexplained   = 期间盘亏金额 − 期间损耗事件金额
l3_rate_pct      = l3_unexplained / 期间理论消耗金额 × 100
```

边界（每一条都会在真实数据上触发）：
1. **盘盈不得冲抵盘亏**。盘盈多是"上次出库多记"，盘亏是"料真的少了"，数量语义不同，必须分列。
   实测 wh_003 全期盘亏 −3066.38 / 盘盈 +5630.75，一冲抵就是净增，指标直接失去意义。
2. **单条异常要隔离**。wh_003 2026-09 有一条 `牛轧糖味 2208.06 桶` 盘亏，占当月盘亏 92%，
   与同 SKU 期间消耗量级差两个数量级 —— 这是录入错误，不是损耗。
   规则：单条盘亏 > `max(该 SKU 期间消耗 × 5, 该仓 P99 单条盘亏)` → 标 `suspected_entry_error`，
   不计入 `l3_unexplained`，单独列在"待核对"清单。
3. **批次未 approved 不参与**（`status != 'approved'` 跳过），否则审核前就能看到半成品指标。
4. **回滚批次要排除**（`rolled_back = 1`）。
5. 首末批次不成区间（前面没有盘点基期）→ `state='unknown'`，不硬算。

### 3.6 口味消耗速度

**为什么用产量代理销量**：本系统没有成品库存流转。实测 `冰激凌成品` 类目仅 2 个 SKU
（`迷你冰糕`、`冰激凌成品-通用`），`quantity ≈ 0`，全期只有 10 条出库记录（16 盒）。
即产出即售出，`production_runs.output_qty` 是可用的需求代理。

```python
def compute_flavor_velocity(db, now, days=30) -> dict:
    """
    对每个 product 输出：
      output_qty      : 窗口内未回滚 run 的产出一致性合计
      runs            : 批次数
      active_days     : 有产出的自然日数
      daily_avg       : output_qty / days            ← 含无生产日（真实节奏）
      active_day_avg  : output_qty / active_days     ← 只看开工日（单批规模）
      share_pct       : output_qty / 全部口味 output_qty × 100
      material_cost   : Σ actual_qty × unit_cost（该口味专属）
      cost_per_unit   : material_cost / output_qty
      prev_daily_avg  : 上一窗口同口径
      trend_pct       : (daily_avg − prev_daily_avg) / prev_daily_avg × 100  [需 days >= 14]
    """
```

两个日均必须**同时展示**，原因（实测 wh_003 近 60 天）：

| 口味 | 批次 | 产量 | 活跃日 | 自然日日均 | 活跃日日均 |
|------|------|------|--------|-----------|-----------|
| A 大白兔 | 9 | 16.0 | 7 | 0.27 | 2.29 |
| 金芒雪芭 | 14 | 15.0 | 13 | 0.25 | 1.15 |
| A 榛子巧克力 | 8 | 16.0 | 8 | 0.27 | 2.00 |

只看自然日日均 → 三者几乎并列；只看活跃日日均 → 大白兔(2.29) 是金芒雪芭(1.15) 的两倍。
前者反映"这个口味多久出现一次"，后者反映"每次做多少"。**受欢迎程度更接近前者，
单批规模更接近后者**，混为一谈会得出相反结论。

分母口径：`days` 用**自然日**（不是窗口内首末产出日的跨度），否则新上架口味会被系统性高估。

补充维度（可选，一期可不上）：`cost_per_unit` 实测跨度极大 ——
`A 大溪地香草荚` 2.03 / `A 大白兔` 2.67 / `金芒雪芭` 15.84 / `草莓大福` 45.59（元/件），
相差 20 倍。同一"产量"在金额上完全不等价，所以口味排序表要并列 `产量` 与 `原料成本` 两列，
只用产量排序会让"低产量高成本"的高价值口味被埋掉。

---

## 4. 与现有库存健康表的整合方式

### 4.1 代码落点（零新增路由）

| 位置 | 改动 |
|------|------|
| `reports_health_pure.py` | 新增常量 4 个 + 新函数 `compute_document_integrity()` / `compute_production_loss()` / `compute_flavor_velocity()`；**不改动**现有 8 个函数 |
| `blueprints/reports.py::_build_health_context` | 追加两个 key：`loss`、`flavors`；`constants` 里追加 3 个阈值供模板展示 |
| `templates/report_health.html` | KPI 行尾部加 1 张卡；生产分析卡下方插入 2 个 section；风险明细加第 6 段 |
| `blueprints/reports.py::health_export` | 追加段 6「损耗归因」、段 7「口味消耗速度」 |
| `blueprints/production.py` | 三处改造：`planned_default` 标记、`outbound_req_id` 回填、可选的损耗登记表单 |
| `blueprints/outbound.py` | 写 `source_type='manual'` |
| `blueprints/stocktake.py` | 写 `source_type='stocktake_loss'` |
| `db/__init__.py` | 见 §2.1（新表进 schema + 3 个 ALTER 进迁移块 + version bump） |

**权限**：沿用 `/report/health` 的 `manager+`，不新增权限点。
成本相关字段（`material_cost` / `l2_value`）已经在该页可见，与 `report.cost.view` 脱敏策略一致 ——
若后续要把损耗金额单独开给非成本可见角色，再加独立权限点，一期不做。

**性能**：现有 `_build_health_context` 单次请求已跑 9 个聚合查询；新增 3 个函数各自是基于时间窗的
单趟聚合，预计增加 3~5 个查询。单仓量级（items ≈ 100，movements ≈ 2000）可忽略，
不需要缓存；但 `compute_production_loss` 的按批次配对是 Python 侧循环，**必须把 stocktakes 一次查全
再分组**，不要 N+1。

### 4.2 页面结构（插入位置）

```
KPI 行：库存金额 | 可售天数 | 低于安全 | 呆滞 | 超储 | 【新增】生产损耗率
① 消耗趋势       ② 生产分析（现有）
【新增】③ 生产损耗归因（四层分段条 + 三段小卡）
【新增】④ 口味消耗速度（排行表 + 趋势箭头）
… 现有 ④~⑦ 顺延
⑧ 风险明细：… 现有 5 段 + 【新增】6) 未解释损耗 TOP / 单据异常
⑨ SKU 明细总表（不动）
⑩ 生产明细（不动，但 overuse_pct 口径改为排除 planned_default，并加显式覆盖率标注）
```

### 4.3 展示方案细节

**KPI 卡：生产损耗率**

```
┌─────────────────────────────┐
│ 生产损耗率（窗口）           │
│ 38.2%            [kpi-warn] │
│ 配方 1.2% · 旁路 45.0% · 补记 0.0% │
└─────────────────────────────┘
```
- 主值 = `(l1_over + l2 + l3_unexplained) / 期间理论投入`；
- 副行给出三层拆分，让 Eric 一眼看出"是旁路在爆炸，不是配方"；
- `l1_coverage < 30%` 时主值显示「—」，副行写「配方口径覆盖不足（N% 批次填报）」；
- 阈值 `LOSS_ALERT_PCT = 5`，超过即红。

**区块 ③ 生产损耗归因**

横向 100% 堆叠条（纯 CSS flex，零 JS，与现有 `.abc-bar` 同手法）：

```
理论投入 ¥12,480
[配方偏差 1.2%][旁路消耗 45.0%][已登记损耗 0.0%][未解释盘亏 0.0%][正常消耗 53.8%]
```

条下三张小卡并列：`配方偏差（含覆盖率）` / `旁路消耗（含空原因行数）` / `未解释盘亏（含疑似录入错误条数）`。
数据为 0 时显示「无」，不为 0 但链路缺失时显示「—」并标灰。

**区块 ④ 口味消耗速度**

| 口味 | 产量 | 批次 | 自然日日均 | 活跃日日均 | 份额 | 原料成本 | 单件成本 | 环比 |
|------|------|------|-----------|-----------|------|---------|---------|------|
| A 榛子巧克力 | 16.0 | 8 | 0.27 | 2.00 | 12.4% | ¥1,075.84 | ¥7.63 | ▲ 12% |
| 金芒雪芭 | 15.0 | 14 | 0.25 | 1.15 | 11.6% | ¥1,963.95 | ¥15.84 | ▼ 3% |

- 环比用 `▲/▼` 字符 + 颜色（涨红跌绿，符合国内习惯；本表是"消耗速度上升"不是股价，用中性色 + 箭头即可）；
- 默认按 `daily_avg` 降序，允许 `?flavor_sort=` 白名单切换（照 `SKU_SORT_WHITELIST` 的写法）；
- 批次 < 3 的行加灰点标记"样本不足"，不显示环比。

**风险明细第 6 段：未解释损耗 / 单据异常**

两条并列子表：
1. `未解释盘亏 TOP`：品项 / 盘亏量 / 金额 / 盘点批次 / 疑似录入错误标记（红）
2. `单据异常`：run_id / 品项 / 差量 / 类型（孤儿 run_item / 孤儿出库）→ 链到生产记录页

**CSV 段 6/7**

```
损耗归因, 值
配方偏差金额, …
配方偏差率%, …
配方口径覆盖率%, …     ← 必须导出，否则外部看到 1.2% 会误判
旁路消耗金额, …
旁路占比%, …
未解释盘亏金额, …
生产损耗率%, …

口味, 产量, 批次数, 自然日日均, 活跃日日均, 份额%, 原料成本, 单件成本, 环比%
```

---

## 5. 落地分期

| 期 | 内容 | 风险 | 依赖 |
|----|------|------|------|
| **P0** | `source_type` 列 + 回填 + 三处写入改造；L2 旁路消耗指标；口味消耗速度区块 | 低（纯读+一列写） | 无 |
| **P1** | `planned_default` 列 + `production.py` 标记；L1 口径修正（含覆盖率）；`overuse_pct` 改口径 | 中（改既有指标口径，历史数字会变） | P0 |
| **P2** | `production_loss_events` 表 + 生产录入页损耗登记 + 库存扣减联动 | 中高（涉及库存流转，写错→账实再分叉） | P1 |
| **P3** | L3 批次区间配对 + 疑似录入错误隔离；`stocktake_batches.loss_cause` | 中（口径复杂，需与财务对数） | P2 |

P0 单独可交付，且立刻能回答"一半消耗去哪了"。**建议先上 P0**，看到 L2 真实占比再决定 P2 的损耗登记
表单要不要做得更重。

### 5.1 实现顺序（P0 内）

1. `db/__init__.py`：加列 + 回填 + bump version → 跑 `pytest --basetemp=/tmp/pytest-dc tests/`
   确认失败集零净增（基线 ≈21F/1645P）。
2. 三处写入改造（`production.py` / `outbound.py` / `stocktake.py`）+ 单测断言 `source_type` 落值。
3. `reports_health_pure.compute_production_loss()`（先只做 L2）+ `compute_flavor_velocity()`，
   纯函数单测用内存 sqlite fixture。
4. `_build_health_context` 接两个 key + 模板两块 + CSV 两段。
5. `tests/test_nav_integration.py` 若断言了健康页文案需同步（历史踩坑：改模板文案必挂）。

---

## 6. 验收口径

| 检查项 | 判据 |
|--------|------|
| 迁移幂等 | 同一库连跑两次 `migrate_warehouse_db_columns`，无异常、无重复回填 |
| 版本闸门 | 旧库（user_version=2）升级后 `source_type` 全部非 NULL |
| L0 自洽 | 改造后新产生一条生产记录 → `orphan_run_item=0` 且 `outbound_req_id` 非空 |
| L1 覆盖率 | 新录入时留空实际用量 → `planned_default=1`，`l1_coverage` 下降而非指标虚高 |
| L2 守恒 | `l2_value + 生产消耗金额 + 盘亏金额 ≈ 期间窗口消耗金额`（容差 1%） |
| 三态 | 无生产的仓（wh_000）页面上损耗指标显示「—」，不显示 0 |
| 回归 | `scripts/check_known_failures.py <输出>` exit 0；失败集零净增 |
| 跨仓 | true 缺陷修复后回滚必须让用例变红（变异验证） |

---

## 7. 已知风险与不做的事

1. **历史数据不可追溯**：v3 之前所有 `production_run_items` 无法区分"真填"和"默认"，
   回填为 `planned_default=1` 后 L1 历史覆盖率 = 0。页面须明确写"该指标自 v3 上线起有效"，
   不要给历史趋势线 —— 会给出一条从 0 突然跳起的假曲线。
2. **不引入成品库存模型**：本设计用产量代理销量，前提是"产出即售出"。
   一旦上线成品库存（`products` 进入 `items` 流转），口味速度必须改用成品出库重算，
   否则会高估"生产了但没卖"的口味。**这是本设计最大的隐含假设，须在文档与页脚都写明。**
3. **不用 reason 字符串做长期口径**：`source_type` 落地后，所有 `LIKE '生产领料(run=#%'`
   都应下线。存量 4 处 + 未来新增，每多一处就多一个漏改点。
4. **不做跨仓汇总**：沿用现有健康表"单仓视角"，多仓对比另开报表。
5. **不自动判定失窃**：L3 只输出"未解释"，归因留给盘点审批人，不写死结论。

---

# 第二部分 · 品项级误差定位与归因判定

> 2026-10-20 增补。§1 的四层模型解决「损耗分几类」，本部分解决「每一块钱的误差归到哪个品项、
> 成因是什么、凭什么这么判」。**layer（四层，回答"在哪一环"）与 cause（三分类，回答"为什么"）
> 是两个正交维度，不可互相替代。**

## 8. 品项唯一标识

误差要能被追踪和比较概率，标识必须**仓内唯一 + 跨仓可比 + 抗重建**。

| 层级 | 字段 | 唯一性实测 | 用途 |
|------|------|-----------|------|
| 业务键 | `items.sku` | UNIQUE NOT NULL，5 仓共 516 行，0 空值、0 重复 | **台账主标识** |
| 仓库内码 | `items.id` | 自增主键 | 仅用于 join，**不入台账** |
| 跨仓键 | `items.canonical_id → canonical_items.canonical_sku` | 本地快照库无该表（未跑 canonical 迁移），生产有 | 跨仓误差概率对比 |

台账落 `item_sku`（不是 `item_id`）的理由：`items.id` 在 `db/clone.py`、`db/import_items.py`
重建库时会变，一旦变，历史误差条目全部错位，而 `sku` 是业务长期稳定的。

跨仓汇总时：
- `canonical_sku` 非空 → 用它聚合；
- 为空（旧库/未映射）→ 退化为 `sku` 单仓口径，并在输出里标 `cross_warehouse: degraded`。
  不静默混用两种口径。

`item_name` / `unit` / `unit_cost` 一律**快照写入台账**，不实时 join —— 品项改名或调价后，
历史误差的判定理由必须仍能复现（与 `production_loss_events.unit_cost` 快照同一原则）。

## 9. 误差台账表

```sql
CREATE TABLE IF NOT EXISTS item_loss_ledger (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    warehouse_code    TEXT NOT NULL,
    item_sku          TEXT NOT NULL,          -- 唯一品项标识
    canonical_sku     TEXT,                   -- 跨仓键，可空
    item_name         TEXT NOT NULL,          -- 快照
    category_name     TEXT,
    unit              TEXT NOT NULL,
    unit_cost         REAL NOT NULL DEFAULT 0,

    layer             TEXT NOT NULL,          -- 'L0'|'L1'|'L2'|'L3'
    source_table      TEXT NOT NULL,          -- 原始表名，溯源
    source_id         INTEGER NOT NULL,       -- 原始行 id
    run_id            INTEGER,                -- 生产批次（L0/L1）
    batch_id          INTEGER,                -- 盘点批次（L3）
    event_at          TEXT NOT NULL,          -- 业务发生时间（非检测时间）

    err_qty           REAL NOT NULL,          -- 恒正
    err_value         REAL NOT NULL,          -- err_qty × unit_cost
    delta_sign        INTEGER NOT NULL,       -- -1 误差为亏 / +1 为盈

    cause_primary     TEXT NOT NULL,          -- human|recipe|process|unattributed
    cause_secondary   TEXT,                   -- 次因，可空
    cause_code        TEXT NOT NULL,          -- 细粒度原因码，见 §10.2
    confidence        REAL NOT NULL DEFAULT 0,
    evidence_json     TEXT NOT NULL,          -- 证据快照（结构化）
    reason_text       TEXT NOT NULL,          -- 人话判定理由，见 §11

    self_reversed     INTEGER NOT NULL DEFAULT 0,   -- 盘盈盘亏配对冲销，见 §12.1
    resolved          INTEGER NOT NULL DEFAULT 0,
    resolved_by       TEXT,
    resolved_at       TEXT,
    detected_at       TEXT NOT NULL,

    UNIQUE (warehouse_code, layer, source_table, source_id, cause_code)
);
CREATE INDEX IF NOT EXISTS idx_ledger_sku    ON item_loss_ledger(warehouse_code, item_sku);
CREATE INDEX IF NOT EXISTS idx_ledger_event  ON item_loss_ledger(event_at);
CREATE INDEX IF NOT EXISTS idx_ledger_cause  ON item_loss_ledger(cause_primary);
```

设计要点：

1. **`UNIQUE(warehouse, layer, source_table, source_id, cause_code)`** 让台账可**幂等重算** ——
   每次进入健康页或每日跑批都重放窗口内数据，不会重复计数。这是"可追踪"的前提。
2. **一条源记录可产生多条台账**（主因 + 次因各一条），但 `cause_primary` 只有一个。
   页面上默认按主因统计，次因留作钻取，避免重复计数。
3. **`event_at` 与 `detected_at` 分开**：盘点 10-12 发现 9-07 的问题，归期应按 `event_at`。
4. `err_value` 允许为 0（`unit_cost=0` 的 8 个原料品项），但必须同时落
   `cause_code='P-ZERO-COST'` + `evidence_json` 里记 `value_state: 'unknown'`，
   **不得显示为 0 元**（避坑 #118/#120：计数为 0 先证明链路活着）。

## 10. 归因判定

### 10.1 证据分级：机制证据 > 统计证据

这是整套判定可靠性的根基，必须先立规矩。

| 级别 | 定义 | 例子 | 单独出现时的置信度上限 |
|------|------|------|---------------------|
| **M 机制证据** | 数据结构层面能直接证明成因 | BOM `qty_per_unit=0`；出库 `reason` 为空；品项不在任何 BOM；`unit_cost=0` | 0.85 |
| **S 统计证据** | 只能说明相关性，可能是第三因素 | 两次数值接近；偏差方向一致；误差集中在某操作员 | 0.45 |

**硬规则：`confidence >= 0.8` 的判定必须至少含一条 M 级证据。**
仅有 S 级证据的判定一律降到 `unattributed` 或封顶 0.45，并进"待人工复核"队列。

这条规则的由来是实测教训（§12.1）：`牛轧糖味` 的出库量 2208.18 与盘亏量 2208.06 相差 0.005%，
若按数值匹配判"重复计量"就完全判错 —— 真实机制是**先盘盈 2208.35、次日盘亏 2208.06 的冲销对**，
出库那 2208.06 只是盘亏的副产物。**数值巧合不能当因果。**

### 10.2 原因码字典

**人为操作（human）**

| 码 | 名称 | 机制证据 |
|----|------|---------|
| `H-REASON-BLANK` | 出库未填原因 | `reason IS NULL OR TRIM(reason)=''` |
| `H-SPIKE-ENTRY` | 单条数量异常 | 单条盘亏 > max(该品项窗口出库量 × 3, 该仓单条 P99) |
| `H-REVERSAL-PAIR` | 盘盈盘亏冲销对 | 同品项、相邻批次、量级差 <1%、净额≈0 |
| `H-ASYNC-ROLLBACK` | 回退不对称 | run 已回退但 outbound 未回退（或反之） |
| `H-OPERATOR-CLUSTER` | 集中操作员 | 窗口内 ≥70% 误差由 ≤2 个 `created_by` 产生 |
| `H-OVERRIDE-PLAN` | 手工改写实际用量 | `planned_default=0` 且偏差 > 阈值 |

**配方问题（recipe）**

| 码 | 名称 | 机制证据 |
|----|------|---------|
| `R-BOM-ZERO` | 配方系数为 0 | `planned_qty = 0` 且 `actual_qty > 0` |
| `R-BOM-MISSING` | 配方漏行 | 品项出现在 `production_run_items` 但不在该产品 `product_bom` |
| `R-SYSTEMATIC` | 系统性同向偏差 | 同品项同向偏差 ≥80% 且样本 ≥3 且 CV < 0.15 |
| `R-UNIT-FACTOR` | 换算倍数错 | 偏差率 ≈ 10/100/1000 的整数倍（含倒数） |
| `R-GRAM-CONFIG` | 克重配置错 | `gram_per_unit > 0` 但计划量级与库存单位明显不匹配 |

**流程设计问题（process）**

| 码 | 名称 | 机制证据 |
|----|------|---------|
| `P-NO-CHANNEL` | 无消耗登记渠道 | 品项不属任何 BOM、`category` 为生产消耗品/包材，却有盘亏 |
| `P-MULTI-PRODUCT` | 多产品共用无法归集 | `product_bom` 中该品项被 ≥5 个产品引用且偏差无法按产品拆 |
| `P-ZERO-COST` | 无成本无法核算 | `unit_cost = 0` |
| `P-STOCKTAKE-GAP` | 盘点周期过长 | 该品项相邻盘点间隔 > `STOCKTAKE_MAX_GAP` |
| `P-UNIT-UNCONVERT` | 单位换算缺失 | 出库用辅单位但 `aux_rate=0`；或 `unit` 与 BOM 用量单位不一致 |
| `P-ORPHAN-MOVEMENT` | 流水无业务单据 | `stock_movements` 有该品项流水但无对应业务表行 |

### 10.3 判定优先级

按"排他性从强到弱"排序，先命中先定，命中即停（不继续往下走）：

```
1. 若存在回退不对称                     → H-ASYNC-ROLLBACK     (confidence 0.95)
2. 若命中盘盈盘亏冲销对                 → H-REVERSAL-PAIR      (0.90，且 self_reversed=1)
3. 若单条数量异常                       → H-SPIKE-ENTRY         (0.85)
4. 若 BOM 系数为 0 / 配方漏行            → R-BOM-ZERO / R-BOM-MISSING (0.90)
5. 若同向 ≥80% 且 CV<0.15 且样本≥3      → R-SYSTEMATIC         (0.80)
6. 若偏差率呈整数倍                     → R-UNIT-FACTOR        (0.80)
7. 若 unit_cost=0 且无成本快照          → P-ZERO-COST          (0.85，value_state=unknown)
8. 若品项不属任何 BOM 且有盘亏           → P-NO-CHANNEL         (0.75)
9. 若被 ≥5 产品共用                     → P-MULTI-PRODUCT      (0.65)
10. 若出库 reason 空占比 ≥50%           → H-REASON-BLANK       (0.60)
11. 其余                                → unattributed         (0.00，进人工队列)
```

**冲突处理**：主因取第一条命中的；若后续还命中**不同大类**的规则，写入 `cause_secondary`。
不合并、不加权求和 —— 例如"配方漏配 + 出库不选原因"是两条独立事实，
合成一个分数会同时丢掉两个可执行动作。

### 10.4 置信度

**§10.3 表中每个原因码后的数值即该规则的置信度**，不再按证据条数二次加权。
理由：每条规则本身已经编码了它需要的证据组合（例：`R-BOM-ZERO` 天然自带一条 M 级机制证据），
再叠加计数会让"证据多但成因弱"的规则（如 `P-MULTI-PRODUCT`）反超强规则。

仅保留三条修正：

```
1. 主因规则本身只依赖 S 级证据（H-OPERATOR-CLUSTER / P-MULTI-PRODUCT / 单独出现的 R-SYSTEMATIC）
   → 封顶 0.65
2. 同一误差项命中 ≥2 条不同原因码的 M 级证据（如 R-BOM-ZERO + P-ZERO-COST）
   → +0.05，封顶 0.95
3. 无任何规则命中 → unattributed，conf = 0.00
```

封顶 0.95 而非 1.0：所有判定都基于历史数据推断，留 5% 给未观测因素。
封顶 0.65 的意义：这些规则只能说明"值得查"，不能说明"就是这样"，页面必须显示在"待复核"区。

## 11. 判定理由生成

要求是"可供判定的归因依据"，因此每个误差项的 `reason_text` 必须是
**带具体数字、可被人工复核推翻**的陈述，不能只给标签。

模板：

```
{品项名}（{sku}）在 {起始日}~{截止日} 出现 {n} 次{层描述}，累计 {qty}{unit} / ¥{value}。
判定为「{成因中文名}」（置信度 {conf}）。
依据：{evidence 1}；{evidence 2}；{evidence 3}。
建议动作：{action}
```

实例（真实数据，见 §13）：

> 海盐（PP072）在 2026-06-29~2026-09-14 出现 7 次配方偏差，累计 0.07 瓶 / ¥0.07。
> 判定为「配方系数为 0」（置信度 0.90）。
> 依据：涉及 2 个产品的 BOM 中 `qty_per_unit` 均为 0；7 次生产实际投料均为 0.01 瓶，
> 偏差方向 100% 同向、标准差为 0；该品项窗口内无任何其他消耗渠道。
> 建议动作：修正 `product_bom.qty_per_unit`（当前 0 → 建议 0.01），修正后重算历史 L1。

`evidence_json` 存结构化版本，便于前端展开与后续审计：

```json
{
  "signals": ["R-BOM-ZERO", "R-SYSTEMATIC"],
  "evidence_level": {"M": 2, "S": 1},
  "facts": {
    "bom_qty_per_unit": 0,
    "products": ["焦糖海盐", "黑胡椒蜂蜜海盐"],
    "occurrences": 7,
    "actual_values": [0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01],
    "direction_consistency": 1.0,
    "stddev": 0.0,
    "other_consumption_channels": 0
  }
}
```

## 12. 误差概率

用户要求"追踪各品项的发生情况及误差概率高低"，需要分母。

```
exposure_events   = 该品项窗口内 (出库次数 + 生产投料次数)       ← 操作暴露
inspection_events = 该品项窗口内 盘点次数                        ← 检验机会
err_events        = 台账条目数（self_reversed=0 且 cause≠unattributed）
err_rate          = err_events / exposure_events                  ← 操作差错率
loss_rate         = 盘亏次数 / inspection_events                 ← 检验失败率（盘亏率）
value_at_risk     = Σ err_value / 该品项窗口消耗金额              ← 金额风险敞口
```

三个指标并列，不合成。理由：`err_rate` 高的品项可能是操作频繁（分母大）导致的假象，
`loss_rate` 高的可能是盘点次数少，`value_at_risk` 高但次数少的才是真正该先修的。

实测基线（wh_003，盘点次数 ≥10 的品项）：

| SKU | 品项 | 出库事件 | 盘点次数 | 盘亏次数 | 盘亏率 |
|-----|------|---------|---------|---------|--------|
| PP094 | 小动物积木 | 8 | 15 | 12 | **80%** |
| PP048 | 安德鲁杏子酱 | 10 | 17 | 12 | 71% |
| PP056 | 迷你冰糕 | 10 | 14 | 10 | 71% |
| PP050 | 华夫蛋筒 | 48 | 17 | 10 | 59% |
| PP034 | 纯牛奶 | 88 | 18 | 10 | 56% |
| PP027 | 方巾-白 | 13 | 16 | 6 | 38% |
| PP051 | 原味奶浆JYX001 | 151 | 17 | 2 | **12%** |

`PP051`（151 次出库、13 个产品共用、盘亏率仅 12%）是事实上的**健康对照组** ——
它证明"高频 + 多产品共用"本身不必然导致高损耗，可用来校验判定规则有没有过度归因到 process。

### 12.1 冲销对识别（`self_reversed`）

实测 PP091 `牛轧糖味` 的完整时间线：

| 时间 | 事件 | 账面 | 实盘 | 差异 |
|------|------|------|------|------|
| 09-07 16:10 | 盘点批次#15 | 0.45 | **2208.8** | **+2208.35** |
| 09-08 13:48 | 盘点批次#16 | 2208.8 | 0.74 | **−2208.06** |
| 09-08 13:48 | 出库（`盘点审核#16盘亏`） | — | — | 2208.06 |

两次盘点相隔 21 小时，中间无任何出库或入库。**净影响 = 0.45 → 0.74，仅 +0.29 桶。**
但按月度盘亏汇总，这条贡献了 wh_003 2026-09 盘亏总额 2387.69 中的 2208.06（**92%**）。

配对判据（全部满足才算冲销对）：

```
1. 同 warehouse + 同 item_sku
2. 两条盘点 diff 符号相反
3. |正| 与 |负| 的相对差 < 1%
4. 两批次 created_at 间隔 <= REVERSAL_PAIR_HOURS（默认 48h）
5. 两条之间该品项无出库/入库流水（防止把真实的"错了又真少了"当冲销）
```

命中后：
- 两条都标 `self_reversed=1`，`cause_primary='human'`、`cause_code='H-REVERSAL-PAIR'`；
- **不计入 L3 损耗率分子**，但**保留在台账里**（这是"人为操作"的宝贵样本，
  且是改进录入校验的直接依据）；
- 页面单独一栏"已自愈误差（不计入损耗率）"，不要把 216 万挂在首页 KPI 上。

⚠️ 若不做配对识别，wh_003 九月损耗率会被单条脏数据拉爆，整个指标失去参考价值。

## 13. 实测判定结果（wh_003 真实数据）

用 §10 规则跑一遍，五个典型案例覆盖三类成因 + 一个对照组：

| SKU | 品项 | 层 | 误差量 | 金额 | 主因 | 原因码 | 置信度 |
|-----|------|----|--------|------|------|--------|--------|
| PP072 | 海盐 | L1 | 0.07 瓶 | ¥0.07 | **recipe** | R-BOM-ZERO | 0.90 |
| PP116 | 稳定剂 | L1 | 0.02 罐 | — | **recipe** | R-BOM-ZERO + P-ZERO-COST | 0.95 |
| PP091 | 牛轧糖味 | L3 | 2208.06 桶 | ¥216.4 万 | **human** | H-REVERSAL-PAIR | 0.90 |
| PP094 | 小动物积木 | L3 | 168 件 | ¥436.80 | **process** | P-NO-CHANNEL | 0.75 |
| PP027 | 方巾-白 | L2 | 16 条 | ¥21.44 | **human** | H-REASON-BLANK | 0.60 |
| PP034 | 纯牛奶 | L3 | 97.18 盒 | ¥971.80 | **process** | P-MULTI-PRODUCT | 0.65 |
| PP051 | 原味奶浆 | — | 9 袋 | ¥990.00 | — | 对照组，盘亏率仅 12% | — |

关键观察：

1. **PP072 海盐 / PP116 稳定剂 → 配方问题**，而且是**同一根因**（BOM 系数填 0）。
   这两条一旦修 BOM，L1 归零、对应盘亏也会消失。**修复成本最低、收益最直接。**
2. **PP091 → 人为操作，但已自愈**。它是月度盘亏的最大贡献者，却是最不该修的
   —— 该修的是"盘点录入缺少量级校验"（2208 桶 vs 账面 0.45 桶相差 4900 倍，无任何提示）。
3. **PP094 小动物积木 → 流程问题**。它不在任何 BOM，类别是"生产消耗品"（赠品玩具），
   15 次盘点 12 次盘亏、盘亏量(168) 反而大于出库量(97)。
   不是有人偷，是**这个品项根本没有日常消耗登记渠道**，只能靠盘点兜底。
   → 动作：给非原料类生产消耗品开出库/领用登记，或干脆不纳入损耗考核。
4. **PP027 方巾-白 → 人为操作**。13 次出库 8 次不填原因（62%），涉及 16 条 / ¥21.44。
   注意两个反向信号：**记录缺失率极高（62%）但金额极小（¥21）**。
   这条是 L2（旁路消耗），**不是损耗** —— 问题在"记录不全"，不在"料少了"。
   若把 L2 和 L3 混在一张损耗榜里，这类"管理问题"会挤掉真正有钱的项。
   → 动作：出库原因设为必填（前端），而不是去查库存。
5. **PP034 纯牛奶**：12 个产品共用、盘亏率 56%，但 `PP051` 同样 13 产品共用却只有 12%。
   → 说明"多产品共用"不足以单独定罪，置信度只能给 0.65（S 级证据主导）。
   这条要落到"待人工复核"队列，由 Eric 判断是配方设计还是操作问题。

   ⚠️ 对照组只能用单一维度：`PP051` 在**盘亏率**上是对照组（12%），
   但它的 L2 无原因出库有 49 次 / ¥5,390，并不健康。
   引用它时须限定"仅盘亏率维度"，否则会变成挑数据支持结论。

## 14. 展示方案

**区块 ⑬ 品项误差台账**（插在 §4.2 的 ⑩ 生产明细之后）

主表一行一个误差条目，按 `err_value` 降序：

| SKU | 品项 | 层 | 发生日 | 误差量 | 金额 | 主因 | 判定码 | 置信度 | 理由 |
|-----|------|----|--------|--------|------|------|--------|--------|------|
| PP091 | 牛轧糖味 | L3 | 09-08 | 2208.06 桶 | ¥216.4万 | 人为 | H-REVERSAL-PAIR | 0.90 | 展开 ▸ |

- 「理由」列点开显示 `reason_text` 全文 + `evidence_json` 的 facts 表；
- **「主因」列用颜色**区分配方(amber)/人为(blue)/流程(gray)/未判定(虚线框)，**不用红绿**
  （红绿留给盈亏方向，两者不能混用一套色）；「层」列只显示 L0~L3 文字，不着色；
- `self_reversed=1` 的行整行淡化 + 标「已自愈」角标；
- `value_state=unknown` 的金额显示「—」并附 tooltip「该品项无成本，仅计量」；
- 每行右侧给「溯源」链接，直跳 `/production/runs?run_id=` 或 `/stocktake/batch/<id>`。

**区块 ⑭ 归因分布矩阵**（同一 section 下半）

品项 × 成因的计数热力（纯 CSS grid，零 JS）：

```
品项           配方    人为    流程    未判定
海盐           7       -       -       -
牛轧糖味       -       1(自愈) -       -
小动物积木     -       -       12      -
方巾-白        -       8       5       -
纯牛奶         -       -       10      1       ← 未判定项高亮提示人工
```

**区块 ⑮ 品项风险排行**（可选，空间不足时并入 ⑬ 的排序选项）

按 `err_rate` / `loss_rate` / `value_at_risk` 三口径切换，白名单排序，照 `SKU_SORT_WHITELIST` 写法。
每行显示「误差次数 / 暴露次数 = 概率」，让"概率高低"可直接读。

**CSV 段 8**

```
SKU, 品项, 品类, 层, 发生日, 误差量, 单位, 金额, 主因, 原因码, 置信度, 已自愈, 判定理由, 溯源表, 溯源ID
```

导出时必须带 `evidence_json` 的压缩版（单行分号分隔），否则外部拿到"0.90 置信度"无法复核。

## 15. 实现顺序（并入 §5 的 P0/P1）

| 步 | 内容 | 依赖 | 验收 |
|----|------|------|------|
| 1 | `item_loss_ledger` 建表 + 幂等重算函数 `rebuild_item_loss_ledger(db, since)` | §2.1 迁移约束 | 连跑两次行数不变 |
| 2 | L1 归因（`R-BOM-ZERO` / `R-BOM-MISSING`）—— 信号最干净，先做 | 步骤 1 + `planned_default` 列 | PP072 判定出 recipe/0.90 |
| 3 | L3 归因 + **冲销对识别**（`H-REVERSAL-PAIR`） | 步骤 1 | PP091 判出 self_reversed=1，且 L3 损耗率不被它污染 |
| 4 | L2 归因（`H-REASON-BLANK`） | `source_type` 列（P0） | PP027 判出 human/0.60 |
| 5 | process 类（`P-NO-CHANNEL` / `P-ZERO-COST` / `P-MULTI-PRODUCT`） | 步骤 1 | PP094 / PP116 / PP034 判定正确 |
| 6 | 概率三口径 + 矩阵展示 | 步骤 2~5 | 与控制台手算一致 |

**步骤 3 是整条链的成败点**：不做冲销对，P0 上线第一天就会给 Eric 报"本月损耗 216 万"，
然后被判定为误报，指标信用一次性归零。

## 16. 归因判定的已知局限

1. **只能判定"合同类"，不能判定"具体是谁"**。`H-OPERATOR-CLUSTER` 给出的是操作员分布信号，
   不是责任认定。页面措辞必须是"集中于 N 个账号"，不得写"某某操作失误"。
2. **`unattributed` 会长期存在**。规则覆盖不到的组合应如实留白并进人工队列，
   不允许为了"每行都有结论"而降阈值硬判 —— 那会让整套归因失去可信度。
3. **配方类判定的置信度依赖 BOM 历史**。`product_bom` 无版本表，当前 BOM 配置 ≠ 历史 run 时的配置。
   若 BOM 已被人改过（如系数已从 0 改成 0.02），`R-BOM-ZERO` 会误判历史行。
   → 缓解：判定时以 `production_run_items.planned_qty` 为准（它是 run 时快照），
   BOM 现值只用于生成"建议动作"，不用于定性。
4. **不追溯建账前的存量**。`item_loss_ledger` 只从启用日起算，历史盘亏不补录
   —— 补录会引入大量不可验证的推定，反而污染概率基线。

---

# 附录 A · 实现记录（2026-10-20）

## A.1 已落地

| 文件 | 改动 |
|------|------|
| `db/__init__.py` | `item_loss_ledger` 表 + 3 索引；`production_run_items.planned_default` / `outbound_req_id`；`outbound_requests.source_type` + 三条回填 UPDATE；`WAREHOUSE_SCHEMA_VERSION` 2→3 |
| `reports_loss_pure.py`（新增） | 归因引擎：`compute_item_loss_ledger` / `rebuild_item_loss_ledger` / `summarize` / `build_ledger_context` / `find_reversal_pairs` |
| `blueprints/production.py` | 写 `planned_default`（表单留空 = 1）、`outbound_req_id`、`source_type='production'` |
| `blueprints/outbound.py` | 写 `source_type='manual'` |
| `blueprints/stocktake.py` | 写 `source_type='stocktake_loss'` |
| `blueprints/reports.py` | `_build_health_context` 接入 `loss`；CSV 段 6「品项误差台账」+ 概率表 |
| `templates/report_health.html` | 归因区块：汇总三卡 + 成因堆叠条 + 台账主表（`<details>` 展开理由）+ 自愈区 + 品项风险排行 |
| `tests/test_reports_loss_pure.py`（新增） | 30 条：冲销对五判据、L1/L2/L3 全分支、金额盲区、概率口径、幂等落库、三态、路由渲染、CSV、表缺失降级 |

## A.2 与设计的偏差（4 处，均已在代码注释说明理由）

1. **`outbound_req_id` 历史行不回填**（原 §2.6 要求回填）。
   理由：`migrate_warehouse_db_columns` 在**每个请求**都会跑，塞一个大表 UPDATE 风险过高；
   且回填 SQL 一旦有 bug 会让迁移本身失败，拖垮全站。
   → 改为生产时写入 + 历史查询按 `reason` 解析兜底。等回归稳定后可单开一次性 CLI 补。

2. **`err_rate` 分母加上盘点次数**（原 §12 只写出库 + 生产投料）。
   理由：实测 PP056 算出 120% 误差率 —— L3 盘亏事件是盘点产生的，分母漏掉盘点就会超 100%。

3. **`_FLOW_ACTIONS` 不含 `库存调整`，且查询排除 `note LIKE '盘点%'`**（原 §12.1 第 5 条只写"无出入库流水"）。
   理由：盘盈写 `action='库存调整'`、盘亏写 `action='出库'`，都是盘点自身的副产物。
   不排除则每对盘盈盘亏之间都"存在流水"，冲销对永远配不上 —— 已加回归用例并做变异验证。

4. **台账表增加 `value_state` 字段**（`deployed` / `unknown`）。
   理由：`unit_cost=0` 的品项金额不可核算，不能让它的 0 元混进金额合计。
   页面显示「—」，`summarize` 单列 `unknown_count` 与 `blind_spots`。

## A.3 实测结果（wh_003 副本，回看 180 天）

台账 584 行：L1 9 / L2 324 / L3 251。

| 原因码 | 条数 | 说明 |
|--------|------|------|
| `H-REASON-BLANK` | 324 | 出库未填原因，占 55%（记录完整性问题，不计损耗率） |
| `P-NO-CHANNEL` | 152 | 不在任何配方却有盘亏（生产消耗品/成品） |
| `unattributed` | 70 | 规则覆盖不到，进人工队列 |
| `P-MULTI-PRODUCT` | 25 | 多产品共用，置信度 0.65 |
| `R-BOM-ZERO` | 9 | 全部是 PP072 海盐 7 + PP116 稳定剂 2 |
| `H-REVERSAL-PAIR` | 4 | **已自愈，不计入损耗率** |

**冲销对识别到 4 对**（规则具备泛化能力，不只命中 PP091）：

| SKU | 品项 | 金额（已冲销） |
|-----|------|---------------|
| PP091 | 牛轧糖味 | ¥2,163,898.80 |
| PP109 | 杯子-银 | ¥430.00 |
| PP045 | 蓝钻杏仁片 | ¥20.50 |
| PP046 | 黄金椰子粒 | ¥17.80 |

可计价误差 ¥42,345.57 + 无法计价 29 条。
**若不做冲销对，首页损耗额会是 ¥2,206,244（虚高 52 倍）。**

## A.4 未落地（按 §5 分期留待后续）

- **口味消耗速度**（P0 另一半）：`compute_flavor_velocity` 未实现。
- **`production_loss_events` 表**（P2）：损耗事件仍未前移到生产当期，L3 仍靠盘点兜底。
- **`stocktake_batches.loss_cause`**（P3）：盘亏归因仍由规则推断，无人工确认入口。
- **`source_type` 存量 4 处 `LIKE '生产领料(run=#%'` 未下线**：新列已落地，但
  `reports_health_pure` / `core` / `consumption` 里的旧判断仍在用，需单独一轮替换。

## A.5 验证记录

- `ruff check` 改动文件全集：All checks passed。
- `pytest tests/ --ignore=tests/mcp_server`：失败集与 `tests/known_failures.txt` 完全一致（零净增）。
- 变异验证两处：
  1. 去掉 `note NOT LIKE '盘点%'` → `test_reversal_pair_ignores_stocktake_own_movements` 变红（`P-NO-CHANNEL` 替代 `H-REVERSAL-PAIR`）；
  2. 去掉 `s["exposure"] += s["inspections"]` → 2 条概率用例变红。
- 迁移幂等：同一库连跑两次，`user_version` 稳定在 3，`source_type` 无 NULL 残留。
