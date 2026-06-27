# Foundation Updates — 品类与品项 / 汇总页 设计

## 概述

本 spec 覆盖两块 UI 与功能的迭代,**系统报错 log 功能不在本次范围**(已在 memory 中记录为 v2,落库方案 = error_logs 表 + 平台管理员后台查看页)。

### 块 A:品类与品项页(`/items`)

1. **可见权限收紧**:staff 角色不可见;`/items` 路由从 `@require_role("staff")` 升级到 `@require_role("manager")`,侧栏和移动 nav 入口用 `{% if current_role != 'staff' %}` 包住。
2. **品类筛选(芯片)**:复用 `/inventory` 的 `.cat-bar / .cat-chip` 设计,顶部一排按钮切换,纯前端隐藏/显示,无需重新请求。
3. **每单位克重 UI**:新增/编辑表单补"每单位克重(g/单位)"输入框,品项列表新增"克重"列显示(0/空 → `—`)。数据库列 `items.gram_per_unit` 已存在,**无需迁移**。
4. **后端校验**:`POST /items` 对 `gram_per_unit` 显式校验 `>= 0`(目前依赖 `parse_qty` 接受负值,补防御)。

### 块 B:汇总页(`/summary`)

1. **时间维度筛选**(URL `?range=7d|month|all`,默认 `7d`):影响进货金额、消耗金额、按品类表、消耗 Top、周转率;**库存金额不受影响**(它表示"此刻")。
2. **库存周转率**:分子 = 窗口内消耗金额;分母 = 窗口内平均库存金额(起止两个时刻的算术平均,从 stock_movements 反推)。公式详见下文。
3. **顶部卡片重构**:原"营业额"卡片被"库存周转率 + 可售天数"取代。其余 3 张卡片(进货金额/消耗金额/当前库存金额)保留。
4. **CSV 导出**:`/summary/export?range=...` 输出三段(总体/按品类/消耗 Top),UTF-8 BOM。

---

## 数据模型变更

**无 schema 变更**。`items.gram_per_unit` 列已存在(`2026-06-22 production-gram-unit` PR 引入),本次只是把 UI 接上。

## 接口设计

### A. `GET /items`

- 权限:`@require_role("manager")`(`require_login` 保持)。
- 查询参数:`?cat=<category_name>`(可选,目前未用;前端芯片切换不依赖该参数,纯前端过滤)。建议**后端不实现**该参数(芯片切换不重新请求),保持 URL 干净。
- 返回模板 `items.html`,带 `items`(所有品项)、`categories`(固定品类)、`current_role`。

### A. `POST /items`(新增库存品)

- 权限:`@require_role("manager")`。
- 字段:`name`(必填)、`category_id`(必填)、`quantity`、`safety_stock`、`unit_cost`、`unit`、`gram_per_unit`。
- **新增校验**:
  - `gram_per_unit < 0` → flash 报错,不写入,redirect 回列表。
  - `unit_cost < 0` → 同上(目前未校验,补)。
- 行为不变的部分:SKU 自动生成、`audit()` 调用、redirect 回 `items.items_list`。

### A. `GET /items/<id>/edit`、`POST /items/<id>/edit`

- 权限:`@require_role("manager")`(从 `@require_login` 升级)。
- 新增校验:同 POST /items。

### A. `POST /items/<id>/delete`

- 权限保持 `@require_role("staff")`(经理仍可删;staff 看不到入口但代码层兜底)。

### B. `GET /summary`

- 权限:保持 `@require_login`。
- 查询参数:`range ∈ {7d, month, all}`,默认 `7d`。
- 计算口径见下文。

### B. `GET /summary/export`

- 权限:`@require_login`(所有登录用户)。
- 查询参数:同 `/summary`。
- 返回 `text/csv` 响应,UTF-8 BOM,`Content-Disposition: attachment; filename=summary-YYYY-MM-DD-<range>.csv`。
- 三段内容(详见"导出内容"小节),段间空行。

## 业务口径

### 固定品类过滤

`/items` 和 `/summary` 的查询都基于 `fixed_categories_in_clause()`(从 `config.FIXED_CATEGORIES` 派生 9 个固定品类:包材、辅料、调味酱、调味酱 分、风味奶浆、乳制品、生产消耗品、生产工具、冰激凌成品)。

### 时间窗口 SQL 起点

| range | 起点 SQL | 终点 |
|-------|---------|------|
| `7d`(默认) | `created_at >= datetime('now','-7 days')` | `now()` |
| `month` | `created_at LIKE 'YYYY-MM%'`(当年当月前缀) | `now()` |
| `all` | 不加时间约束;若需 `MIN(created_at)` 反推起点则取 `MIN(stock_movements.created_at)` | `now()` |

### 进货金额(`total_inbound_value`)

**口径不变**:已用 `restock_requests` 而非 `stock_movements`,被删除的不算。

```sql
SELECT COALESCE(SUM(r.requested_quantity * i.unit_cost), 0)
FROM restock_requests r JOIN items i ON i.id = r.item_id
[WHERE r.created_at >= :start_ts]  -- 7d/all 不需要时省略
```

`month` 范围:

```sql
WHERE r.created_at LIKE :year_month || '%'
```

`range=all` 时:无 WHERE。

### 消耗金额(`total_consumed_value`)

**口径不变**:出库(rolled_back=0,排除生产领料 reason)+ 生产消耗(rolled_back=0)。

```sql
-- 完整版(7d)
SELECT COALESCE(SUM(qty * unit_cost), 0) FROM (
  SELECT o.requested_quantity AS qty, i.unit_cost
  FROM outbound_requests o JOIN items i ON i.id = o.item_id
  WHERE o.rolled_back = 0
    AND (o.reason IS NULL OR o.reason NOT LIKE '生产领料(run=#%')
    AND o.created_at >= datetime('now','-7 days')
  UNION ALL
  SELECT pri.actual_qty, i.unit_cost
  FROM production_run_items pri
  JOIN production_runs pr ON pr.id = pri.run_id
  JOIN items i ON i.id = pri.item_id
  WHERE pr.rolled_back = 0
    AND pr.created_at >= datetime('now','-7 days')
)
```

`month` 把 `>= datetime('now','-7 days')` 换成 `LIKE :ym || '%'`;`all` 直接去掉 WHERE 时间部分。

### 库存金额(`total_stock_value`)

**不受时间筛选影响**:`SUM(items.quantity * items.unit_cost)`(账面)。

### 周转率(`turnover` + `turnover_days`)

```
turnover = consumed_value / avg_stock_value
turnover_days = avg_stock_value / (consumed_value / window_days)
```

**`avg_stock_value` 计算**(起止两点平均):

- `end_value` = 当前库存金额 = `SUM(items.quantity * items.unit_cost)`
- `start_value` = 窗口起始时刻的库存金额,通过 stock_movements 反推:
  ```sql
  -- 7d 起点的每个品项 quantity_start
  SELECT i.id, i.unit_cost,
         i.quantity - COALESCE(SUM(m.delta), 0) AS qty_start
  FROM items i
  LEFT JOIN stock_movements m
    ON m.item_id = i.id AND m.created_at >= datetime('now','-7 days')
  GROUP BY i.id
  ```
  然后 `start_value = SUM(qty_start * unit_cost)`。
- `avg_stock_value = (start_value + end_value) / 2`

`month` 起点:`created_at LIKE 'YYYY-MM%'`(字符串前缀匹配,等价于"月初到月末",不严格等于月初零点的整点边界,可接受)。
`all` 起点:`MIN(stock_movements.created_at)`(数据起点;若为空表则 `avg_stock_value = end_value`)。

**`window_days`**:

- `7d` → 7
- `month` → 当月天数(由 `calendar.monthrange(year, month)[1]` 算)
- `all` → `(end_date - MIN(stock_movements.created_at)).days`,**至少 1**(避免除 0)

**边界处理**:

| 条件 | turnover | turnover_days |
|------|----------|---------------|
| `consumed_value = 0` | `0.00` | `—` |
| `avg_stock_value = 0 且 consumed_value > 0` | 实际值(可能很大) | 实际值(可能很小) |
| `window_days = 0`(防御性) | `0.00` | `—` |

### 按品类周转率(`category_stats[].turnover`)

每行独立算:

```sql
-- 品类 X 的消耗金额(7d)
SELECT COALESCE(SUM(qty * unit_cost), 0) FROM (
  SELECT o.requested_quantity, i.unit_cost
  FROM outbound_requests o JOIN items i ON i.id = o.item_id
  JOIN categories c ON c.id = i.category_id
  WHERE o.rolled_back = 0
    AND (o.reason IS NULL OR o.reason NOT LIKE '生产领料(run=#%')
    AND o.created_at >= datetime('now','-7 days')
    AND c.name = :cat_name
  UNION ALL
  SELECT pri.actual_qty, i.unit_cost
  FROM production_run_items pri
  JOIN production_runs pr ON pr.id = pri.run_id
  JOIN items i ON i.id = pri.item_id
  JOIN categories c ON c.id = i.category_id
  WHERE pr.rolled_back = 0
    AND pr.created_at >= datetime('now','-7 days')
    AND c.name = :cat_name
)
```

品类级 `start_value` / `end_value`:同样口径,但 `WHERE c.name = :cat_name`。

**按品类不显示 turnover_days**(信息密度太大),只显示 turnover。

### 消耗 Top(`top_consumed`)

**口径不变**,加时间筛选(同消耗金额)。保持 `LIMIT 10`(目前 SQL 没有 LIMIT,补)。

## UI 改动

### `templates/items.html`

- **顶部 chip bar**:`全部 / 包材 / 辅料 / 调味酱 / 调味酱 分 / 风味奶浆 / 乳制品 / 生产消耗品 / 生产工具 / 冰激凌成品`(纯前端切换)。
- **新增表单**:补 `<label><span>每单位克重(g/单位)</span><input name="gram_per_unit" type="number" min="0" step="0.01"></label>`(注:目前 `items.py` 已经接收 `gram_per_unit`,但模板没渲染输入框,这是补漏)。
- **品项列表**:新增 `<th>克重</th>`,渲染 `{{ i.gram_per_unit | fmt_qty }} g/件` 或 `—`(0/空)。
- **JS**:从 `inventory.html` 抽 `<script>` 片段(或新写一份)。实现方式:列表的 `<tr>` 加 `data-cat="{{ i.category_name }}"`,点击 `.cat-chip` 时给不匹配的 `<tr>` 加 `.is-hidden` 类(复用 `static/style.css:1202` 已定义的 `.inv-card.is-hidden` 样式,逻辑上对 `<tr>` 也生效)。

### `templates/edit_item.html`

- 已有 `gram_per_unit` 输入框,本次**不动**。

### `templates/base.html`

- 侧栏"品类与品项"链接用 `{% if current_role != 'staff' %}` 包住。
- 移动 nav"品项"链接同样处理。
- 汇总链接无变化。

### `templates/summary.html`

- **顶部**:增加时间范围芯片(纯前端或 URL 提交均可,选 URL 提交以保证刷新保持)。
- **卡片区**:原"营业额"卡片替换为"库存周转率 + 可售天数"。其余 3 张卡片保留,卡片右下角小字注明"7 日滚动"/"不受时间筛选"。
- **按品类表**:新增 `<th>周转率</th>`。
- **消耗 Top**:无变化。
- **导出按钮**:链接 `{{ url_for('reports.export_summary', range=range) }}`。

### CSS

- `.cat-bar / .cat-chip / .is-active`:**已存在**于 `static/style.css:1018-1046`(由 `/inventory` 引入)。本次复用,样式不动。
- `.is-hidden`:**已存在**于 `static/style.css:1202`(用于 `.inv-card.is-hidden`),但其样式为 `display: none`,对 `<tr>` 同样生效。
- `.summary-range / .turnover-card`:**新增**,仅样式化,不影响逻辑。
- 引入 CSS 缓存破坏:在 `templates/base.html` 第 13 行 `style.css?v=10` 改 `?v=11`(根据最近 commit 6796e3b 设计的 mtime + no-cache + sw v5 策略,本次依然 bump query string)。

## 导出内容(`/summary/export`)

UTF-8 BOM,文件名 `summary-YYYY-MM-DD-<range>.csv`,三段以空行分隔。

**段 1:总体**

```
范围,进货金额,消耗金额,当前库存金额,周转率,可售天数
7 日,18200.00,9840.00,15600.00,0.71,9.9
```

**段 2:按品类**

```
品类,进货金额,消耗金额,库存金额,周转率
包材,3200.00,2100.00,3000.00,0.66
辅料,8000.00,4400.00,7500.00,0.60
...
```

**段 3:消耗 Top**

```
品类,品项,消耗量,消耗金额
辅料,面粉,150,2400.00
辅料,白砂糖,80,1120.00
...
```

数字字段:`{:.2f}` 格式化;消耗量保留原单位(在第 3 段用 `品项,数量,单位,金额` 4 列更稳——见"开放问题")。

## 错误处理

- **模板渲染异常**:Jinja 模板用 `{% if consumed_value > 0 %}` 守卫,0 时显示 `0.00` 或 `—`。
- **SQL 异常**:`db.execute` 抛 `sqlite3.OperationalError`,由 Flask 统一 error handler 转 500。本次不引入专用错误日志表(v2 范围),但 SQL 注释里强调"`stock_movements` 必须与 `items.quantity` 同步写入"约定。
- **空数据**:`total_inbound_value=0` 等空表 → 渲染 `0.00`,周转率 `0.00`,可售天数 `—`。`range=all` 且 `stock_movements` 表为空 → `start_value = end_value = 0`,`avg = 0`,`turnover = 0.00`,`turnover_days = '—'`。

## 测试策略

- **`tests/test_items_route.py`**:
  - `staff` 用户访问 `/items` → 403。
  - `manager` 用户访问 → 200,渲染所有品项。
  - `POST /items` 提交 `gram_per_unit=-5` → 不写入,flash 报错。
- **`tests/test_summary_route.py`**:
  - 默认 `?range=`(空)→ 等同 `7d`,上下文 `range='7d'`。
  - `?range=month` → SQL 含 `LIKE '2026-06%'`。
  - `?range=all` → 无时间 WHERE。
  - 周转率:模拟 `consumed_value=0` → `turnover=0.00, turnover_days='—'`。
  - 反推库存:`quantity=100,delta 累加=30` → `qty_start=70`。
- **`tests/test_summary_export.py`**:
  - 文件头含 UTF-8 BOM (`﻿`)。
  - 文件名 `summary-2026-06-27-7d.csv`(用 `freeze_time` 锁定日期)。
  - 三段齐备。
- **手工验证**(提交前):
  - `/items` 在 staff 浏览器不可见
  - 芯片切换不重发请求
  - 克重输入框负值被拒
  - `/summary?range=month` 切换生效,顶部 4 张卡片数字变化
  - 导出 CSV 用 Excel 打开中文不乱码

## 依赖与已知约束

- **`items.gram_per_unit` 列已存在**,但目前只有生产录入页面有 UI。本次补 `/items` + `/items/<id>/edit` 入口。
- **不动 `config.FIXED_CATEGORIES`**:本次所有 SQL 通过 `fixed_categories_in_clause()` 派生,新增品类时自动跟上。
- **`stock_movements` 反推精度**:依赖"每次改 `items.quantity` 都同步写 stock_movements"的代码纪律。SQL 注释里强调。

## 开放问题(实现前确认)

1. **导出 CSV 第 3 段列顺序**:采用 `品类,品项,消耗量,单位,消耗金额`(5 列,带单位)还是 `品类,品项,消耗量,消耗金额`(4 列,不带单位)?前者更完整、后者更短。
2. **staff 完全无法看到 `/items` URL**(即使直接输入)?目前设计是 `@require_role("manager")` 直接 403,无重定向。
3. **周转率"全部"模式的时间范围**:用 `MIN(stock_movements.created_at)` 还是 `MAX - 30 天`(最近 30 天作为兜底)?前者是真实口径,后者更稳。

## 后续优化(不在本次)

- v2:系统报错日志(error_logs 表 + 平台管理员后台查看)
- v2:导出 XLSX 多 Sheet 替代 CSV(目前 CSV 够用)
- v2:周转率按品项展示(目前只到品类级)