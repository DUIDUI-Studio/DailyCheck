# 生产录入模块设计文档

## 概述

在 DailyCheck 库存管理系统中新增"生产录入"模块：店员在生产时点选产品、输入产出量，系统按 BOM（配方）自动扣减原料库存，并允许在录入页逐行调整实际消耗量（可偏离 BOM）。

模块设计目标：
- 复用现有"按仓库隔离"的数据模型（`get_warehouse_db` / `WAREHOUSE_SCHEMA`）
- 与现有"出库/盘点/补货"流程同构：list / submit / rollback / delete
- 录入页为手机端优先，参考 `outbound_session.html` 的"cat-bar + 卡片"模式
- 主入口分流：选完仓库后落地到新页面 `/land`，提供"库存管理"和"生产录入"两个入口

非目标（首期不做）：
- 多级 BOM（嵌套半成品）
- 工时/损耗/分摊成本等非库存字段
- 跨仓库调拨
- 定时生产计划

## 主入口分流（修改现有）

### 登录后落地路径变更
| 路由 | 变更前 | 变更后 |
|---|---|---|
| `auth.warehouse_select` redirect 目标 | `core.dashboard` | `core.land` |
| `GET /` | `core.dashboard` | `core.land`（`core.dashboard` 仍可通过 `/dashboard` 访问，侧栏链接保留） |

### 新增 `core.land`（`GET /land`）
- 模板：`templates/land.html`
- 内容：两个大卡片按钮
  - **库存管理** → `url_for("items.items_list")`
  - **生产录入** → `url_for("production.products_list")`
- 顶部显示当前仓库名 + 切换链接 + 注销链接（沿用 `ctx-bar`）
- 移动端：两个按钮堆叠为全宽卡片；桌面端：并排两列
- 权限：`@require_login`（无角色要求，店员/管理员都能进）

### `base.html` 改动
- 侧栏新增 `<a href="{{ url_for('production.products_list') }}">生产录入</a>`（位于"出库记录"之后）
- mobile-nav 同步新增
- 侧栏"总览"链接保留指向 `core.dashboard`（库存管理的总览入口不变）

## 数据模型变更

### 新增 4 张表（追加到 `db/__init__.py` 的 `WAREHOUSE_SCHEMA`）

```sql
-- 产品定义（不进入 items 库存表；产品本身不入库、不占库存）
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    unit TEXT NOT NULL DEFAULT '件',           -- 产出量单位
    note TEXT,                                  -- 描述/口味/备注
    created_at TEXT NOT NULL
);

-- 产品-原料配方 (BOM)
CREATE TABLE IF NOT EXISTS product_bom (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    item_id INTEGER NOT NULL,                   -- 关联 items.id
    qty_per_unit REAL NOT NULL,                -- 每单位产品所需该原料量
    UNIQUE(product_id, item_id),
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE,
    FOREIGN KEY (item_id) REFERENCES items(id)
);

-- 生产批次
CREATE TABLE IF NOT EXISTS production_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    output_qty REAL NOT NULL,                   -- 本批产出量
    note TEXT,                                  -- 备注/操作员
    rolled_back INTEGER NOT NULL DEFAULT 0,
    created_by TEXT,                            -- 用户名(沿用 audit 口径)
    created_at TEXT NOT NULL,
    FOREIGN KEY (product_id) REFERENCES products(id)
);

-- 批次下各原料实际消耗(允许偏离 BOM)
CREATE TABLE IF NOT EXISTS production_run_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    item_id INTEGER NOT NULL,
    planned_qty REAL NOT NULL,                  -- 配方算出 (展示用)
    actual_qty REAL NOT NULL,                   -- 实际扣减量 (扣库存)
    FOREIGN KEY (run_id) REFERENCES production_runs(id) ON DELETE CASCADE,
    FOREIGN KEY (item_id) REFERENCES items(id)
);

CREATE INDEX IF NOT EXISTS idx_prun_created ON production_runs(created_at);
CREATE INDEX IF NOT EXISTS idx_pruni_run ON production_run_items(run_id);
```

### 迁移策略
- 全部走 `CREATE TABLE IF NOT EXISTS`，老仓库 db 文件首次连接即自动建表（`init_warehouse_db` 已经在每次启动调用）
- 不需要 `migrate_warehouse_db_columns` 补丁

## 新增/修改页面

### `land.html`（新增）
- 继承 `base.html`，覆盖 `sidebar` block 为空
- 顶卡：当前仓库名 + 用户名
- 两个大按钮卡片，URL 见上

### 生产模块模板（新增）
| 模板 | 说明 |
|---|---|
| `production/products.html` | 产品列表（卡片：名称、单位、BOM 项数、"编辑配方"/"录入"两个按钮、"删除"） |
| `production/product_edit.html` | 产品编辑：名称、单位、备注 + BOM 行（增/删/改） |
| `production/session.html` | 录入页：选产品 → 输入产出量 → 展示 BOM 行（actual_qty 可编辑） |
| `production/runs.html` | 历史记录（按时间倒序，"回退"/"删除"按钮） |

### UI 模式（沿用 `outbound_session.html`）
- 顶部 cat-bar 三个 tab：产品 / 录入 / 历史
- 移动端：单列卡片；桌面端：双列
- 录入页：
  - `<select name="product_id">` 选产品
  - `<input type="number" name="output_qty" step="0.01" min="0">` 产出量
  - BOM 行展示：原料名 / 当前库存 / 计划量（planned）/ 实际量（input）
  - 缺料行：边框 + 标签标红（planned_qty 或 actual_qty > current_quantity）
  - 提交按钮：缺料时 disabled；点击弹二次确认"将扣减 X 种原料，总量 N"

## 新增/修改路由

### 修改
| 文件 | 改动 |
|---|---|
| `app.py` | `register_blueprint(production_bp)` |
| `blueprints/core.py` | 新增 `land()`；把 `dashboard` 路径从 `/` 改为 `/dashboard`；新增 `GET /` → redirect `land` |
| `blueprints/auth.py` | `warehouse_select` 的 redirect 改为 `core.land` |
| `templates/base.html` | 侧栏 + mobile-nav 增加"生产录入"链接 |
| `db/__init__.py` | `WAREHOUSE_SCHEMA` 末尾追加 4 张新表 + 2 个索引 |
| `blueprints/core.py::summary` | 消耗金额 += `production_run_items.actual_qty × unit_cost`（仅 `rolled_back=0` 的 run） |
| `blueprints/items.py::inventory_view` | 7 日消耗子查询 `action='出库'` → `action IN ('出库','生产消耗')` |
| `AGENT.md` | "目录结构"/"数据表说明"/"关键业务约束"三处追加 |
| `README.md` | 功能列表追加"生产录入（配方驱动自动扣减原料）" |

### 新增 `blueprints/production.py`

| 路由 | 方法 | 权限 | 说明 |
|---|---|---|---|
| `/production` | GET | login | 产品列表（同 `products.html`） |
| `/production/products/new` | GET/POST | manager | 新建产品 |
| `/production/products/<id>/edit` | GET/POST | manager | 编辑产品 + BOM（同一页） |
| `/production/products/<id>/delete` | POST | manager | 删除产品（需无 `production_runs` 记录） |
| `/production/session` | GET | login | 录入页 |
| `/production/submit` | POST | login | 提交：预检库存 → 写 run + run_items + 流水 + audit |
| `/production/runs` | GET | login | 历史记录列表 |
| `/production/runs/<id>/rollback` | POST | manager | 回退 |
| `/production/runs/<id>/delete` | POST | manager | 删除（未回退的删除会还原库存） |
| `/production/runs.csv` | GET | login | CSV 导出（每行 = 一条 run_item） |

## 业务规则

### 提交时校验（`submit`）
1. `product_id` 必须存在
2. `output_qty` 必须 `> 0`（用 `parse_qty`，2 位小数）
3. BOM 必须非空；任一 `actual_qty < 0` 视为非法
4. **硬性拦截**（已与用户确认）：遍历 `actual_qty`，若任一 `actual_qty > items.quantity`，flash "原料 X 库存不足（需 N，现有 M）" 并拒绝提交
5. 同事务内：
   - 插入 `production_runs`（`created_by = g.user["username"]`）
   - 遍历 BOM 行插入 `production_run_items`
   - `UPDATE items SET quantity = quantity - actual_qty`
   - 插入 `stock_movements`（`action='生产消耗'`, `delta=-actual_qty`, `note="生产记录#<run_id>领料"`）
6. `audit("production.run.submit", "run", run_id, {...})`
7. flash "生产已记录" → redirect `production.runs_list`

### 回退（`rollback`）
- 仅 `rolled_back=0` 的 run 可回退（参照 `outbound.rollback`）
- 同事务内：
  - `UPDATE items SET quantity = quantity + actual_qty`
  - 插入 `stock_movements`（`action='生产消耗回退'`, `delta=+actual_qty`）
  - `UPDATE production_runs SET rolled_back=1`
- `audit("production.run.rollback", "run", run_id)`
- flash "已回退" → redirect `production.runs_list`

### 删除（`delete`）
- 参照 `outbound.delete` 语义：删除 = "撤销这笔业务"
- 若 `rolled_back=0`：先回退（同上事务），再 `DELETE FROM production_runs`（CASCADE 删 run_items）
- 若 `rolled_back=1`：直接 `DELETE FROM production_runs`
- `audit("production.run.delete", "run", run_id, {"rolled_back": ..., "qty": ...})`

### 删除产品
- 先查 `production_runs` 中是否引用，引用则 flash "存在生产记录，无法删除" 并拒绝
- 无引用：`DELETE FROM products`（CASCADE 删 BOM）

### BOM 编辑（`product_edit` POST）
- 接收 `name/unit/note` 三个产品字段
- 接收 BOM 行数组（行 id 可为空，标识新增）：
  - `bom_item_id[]`、`bom_qty[]`、`bom_delete[]`（存在表示删除该行）
- 事务内：
  - 更新 product
  - 遍历：`bom_delete[i]=1` → DELETE；`bom_item_id[i]` 已存在 → UPDATE；空 id → INSERT
  - 不允许重复 `item_id`（UNIQUE 约束兜底）
  - 不允许 `qty_per_unit <= 0`
- `audit("production.product.update", "product", id, {"bom_added": N, "bom_removed": M})`

## 与现有模块的衔接

### 流水（`stock_movements`）
- 新增 action 字符串：`'生产消耗'`、`'生产消耗回退'`
- 与现有 `'出库'` / `'出库回退'` / `'补货入库'` 并列，由 `inventory_view` 7 日消耗子查询统一覆盖

### 汇总（`/summary`）
- `total_consumed_value` SQL 改为：
  ```sql
  COALESCE(SUM(o.requested_quantity * i.unit_cost), 0)        -- 出库
  + COALESCE(SUM(pri.actual_qty * i2.unit_cost), 0)           -- 生产消耗(JOIN)
  ```
  （分两条子查询合并即可，避免 JOIN 笛卡尔积）
- `top_consumed` 增加"生产产品消耗 Top N"，与"原料消耗 Top N"并排显示（同一卡样式）

### CSV 导出（`/production/runs.csv`）
- 字段顺序：`run_id, created_at, created_by, product_name, output_qty, output_unit, item_sku, item_name, planned_qty, actual_qty, rolled_back, note`
- 一行 = 一条 `production_run_items`（run_id 相同的多行）
- `rolled_back=1` 的行仍导出（标 1），便于事后审计
- 用 `Response` + `csv.writer` 直接返回，`Content-Disposition: attachment; filename=production_runs_YYYYMMDD.csv`
- 文件名带当天日期

## 测试策略

### 单元/集成层（建议 pytest + 现有 helper）
- 重点覆盖 `submit` 的事务逻辑：原料不足拦截、流水落库、汇总 SQL 影响
- `rollback` / `delete` 的库存与流水对称性
- BOM 编辑的增删改混合提交

### 手工冒烟（按 `AGENT.md` 风格）
- `/land` 两入口点击可进
- 创建一个产品 + 2 项 BOM
- `/production/session` 提交一次生产 → 库存减扣、流水可见、回退按钮可用
- 回退后库存与流水复原；删除回退过的记录直接消失
- `/summary` 总消耗金额变化符合预期
- `/production/runs.csv` 下载后用 Excel 打开无乱码、列顺序一致

## 文件清单

新增：
- `blueprints/production.py`
- `templates/land.html`
- `templates/production/products.html`
- `templates/production/product_edit.html`
- `templates/production/session.html`
- `templates/production/runs.html`

修改：
- `app.py`
- `blueprints/core.py`（`land` 新增；`dashboard` 路径变更；`summary` 口径）
- `blueprints/auth.py`（`warehouse_select` 落地页）
- `blueprints/items.py::inventory_view`（7 日消耗 action 集合）
- `db/__init__.py`（4 张新表 + 2 索引）
- `templates/base.html`（侧栏 + mobile-nav）
- `AGENT.md`、`README.md`

## 风险与权衡

- **CASCADE 删除 BOM** 在产品删除时触发；若未来需要保留历史产品但禁用，必须先加 `archived` 字段。当前 YAGNI。
- **不锁库存**：高并发下两次提交可能同时通过预检都成功，导致库存变负。沿用现有所有模块的"无锁"风格，不在首期引入；如发生，靠 flash 错误提示让操作员重做（事务回滚后 `items.quantity` 仍是原值）。
- **CSV 编码**：默认 UTF-8 + `﻿` BOM 头，Excel 打开不乱码；如用户报"Excel 还乱码"再调整。
- **`created_by` 取 `g.user['username']`** 而非 `g.user['id']`：沿用 `audit()` 的口径，便于人读；如未来要按人统计可改 id 字段，迁移成本低。
