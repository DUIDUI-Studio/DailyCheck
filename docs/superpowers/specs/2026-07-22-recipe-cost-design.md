# 配方成本计算模块 — spec

**版本**：v1（2026-07-22）
**状态**：设计中。
**目标读者**：实施 agent。
**范围限定**：本期仅"研发中心"等指定仓库启用；不实现推送/发布。

---

## 0. 概述

新增 **配方成本计算** 模块，按用户描述的需求："原料有采购成本和销售价格两套价格，配方基于此计算两套成本 + 出品价格算毛利率；配方两种（冰激凌配方、出品配方，其中出品可引冰激凌配方）；CRUD + 实时成本变化 + 临时调价找毛利率 + 一键保存为新价"。

### 0.0 用户故事（按 user persona 拆解）

1. **采购 / 库存管理员**：在 `/items` 给每个品项维护两个价格（采购单价 / 销售单价）。
2. **研发 / 配方设计员**：新建一份冰激凌配方（如"香草冰淇淋"），逐项选原料填克数；想算毛利率但不知道售价合不合适，**用滑块临时改某原料的销售单价**，看总采购成本 / 毛利率实时变化；调好后点"保存为新价"一次性写回 items。
3. **产品 / 成本核算员**：新建一份出品配方（如"经典柠檬茶"），原料既可以直接选品项（如柠檬汁 30g），也可以引用已有冰激凌配方（如"香草冰淇淋 50g"）；设好出品售价后页面直接显示毛利率；同样可临时调原料销售单价看不同假设下的毛利。
4. **管理层**：在入口与列表页看到所有配方 + 毛利率概览，快速定位低毛利 / 负毛利出品。

### 0.0.1 一个易误解的关键决策（务必 review）

**出品配方引用冰激凌配方时，采购成本按 ic_recipe 的"每份输出采购成本"折算，销售价值按 ic_recipe 的 `sale_price` 折算，不展开到底层 items。**

理由：ic_recipe 本身是有"成品价"的中间品；如果展开到底层 items，原料价 × qty 会双重计入；用户案例（柠檬茶引 50g 香草冰淇淋）已经验证这种语义（详见 §3.1 末尾示例）。

### 0.1 关键决策（已与用户确认）

| 决策点 | 选择 | 备注 |
|---|---|---|
| 原料双价存储 | `items` 加 `selling_price REAL` + `selling_price_updated_at TEXT` | 复用现有 `unit_cost` 做采购成本 |
| 冰激凌配方表 | 独立 `ic_recipes` + `ic_recipe_items`，**结构跟 `product_bom` 同型**（便于未来推送） | 不接 `production.run` 领料 |
| 冰激凌实体 | 冰激凌就是 `items` 里"冰激凌成品"品类下的品项 | 入库/出库/报表跟其他品项一致 |
| 出品配方成分 | 多态：可引 `items` 也可引 `ic_recipes`（一张 `recipe_items` 表 + `source_type` 列） | 出品不能引出品（防递归） |
| 仓库范围 | DB schema 全表都建（与现有模式一致，不按仓库拆表）；nav 仅 admin 可见 | 实际部署在 `wh_003` 研发中心 |
| 推送/发布 | **本期不实现** | 留 future，schema 跟 `product_bom` 同型以便迁移 |
| 出品价格 | `recipes.sale_price` 自带列 | |
| 实时重算 | **前端 JS**（不存额外快照） | 也提供 `/api/cost/...` 只读 sanity check |
| 临时调价 | 编辑页内每行加滑块（前端 state，不写 DB），旁边 "保存为新价" 按钮 POST 写回 `items.selling_price` | 全局开关 "使用临时价" |

### 0.2 YAGNI（明确不做）

- 推送/发布到门店（`recipe_publish_events` 等本期不实现）
- 历史价格快照表
- 配方版本化（`product_bom_versions` / `current_version_id`）
- 与 `production.run` / 出入库联动
- MCP 工具（本期纯 UI + 路由；MCP 留 future）
- 配方审批流

---

## 1. 架构

### 1.1 文件清单

**新增**：
- `blueprints/recipe_cost.py` — routes（CRUD + 临时价保存接口）
- `blueprints/recipe_cost_pure.py` — 纯函数（成本/毛利计算、单位换算）
- `templates/recipe_cost/`
  - `base_recipe.html` — 继承 `base.html`，`no_sidebar=True`，顶部 tabs（冰激凌 / 出品 / 列表）
  - `landing.html` — 入口卡片网格
  - `ic_recipes.html` — 冰激凌配方列表
  - `ic_recipe_edit.html` — 冰激凌配方编辑（含滑块 + JS）
  - `recipes.html` — 出品配方列表
  - `recipe_edit.html` — 出品配方编辑（含多态选择器 + 滑块 + JS）
- `tests/test_recipe_cost_pure.py`
- `tests/test_recipe_cost_route.py`

**修改**：
- `db/__init__.py` — `WAREHOUSE_SCHEMA` 加 4 张表；`migrate_warehouse_db_columns()` 加 `items.selling_price` / `selling_price_updated_at` 的幂等 ALTER
- `blueprints/items.py` — `/items` POST/GET 与 `/items/<id>/edit` POST/GET 增加 `selling_price` 表单字段与写入
- `blueprints/_helpers.py` — `qty_to_stock_units(qty, item)` 复用 `grams_to_stock` / `aux_to_base` 统一换算
- `templates/items.html` + `templates/edit_item.html` — 加 selling_price 输入框（"销售单价 ¥"，默认 0）
- `templates/base.html` — sidebar + mobile nav 加 "配方成本" 链接（仅 admin 可见，复用 `g.user['is_admin']` 模式）
- `app.py` — 注册 `recipe_cost_bp`

### 1.2 模块边界

- **不**修改现有 `production` / `product_bom` 任何代码（保持生产侧稳定）
- **不**改动现有 14 个 blueprints 之间的依赖
- **不**创建跨仓推送接口

---

## 2. 数据契约

### 2.1 新表（warehouse.db）

```sql
-- 冰激凌配方（structure mirrors product_bom; not wired to production.run）
CREATE TABLE ic_recipes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    note TEXT,
    output_unit TEXT NOT NULL DEFAULT 'g',
    output_qty REAL NOT NULL DEFAULT 100,
    sale_price REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE ic_recipe_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ic_recipe_id INTEGER NOT NULL,
    item_id INTEGER NOT NULL,
    qty_per_unit REAL NOT NULL,
    UNIQUE(ic_recipe_id, item_id),
    FOREIGN KEY (ic_recipe_id) REFERENCES ic_recipes(id) ON DELETE CASCADE,
    FOREIGN KEY (item_id) REFERENCES items(id)
);

-- 出品配方
CREATE TABLE recipes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    note TEXT,
    output_unit TEXT NOT NULL DEFAULT '件',
    output_qty REAL NOT NULL DEFAULT 1,
    sale_price REAL NOT NULL DEFAULT 0,
    sale_price_updated_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 出品配方原料：多态（item | ic_recipe）
CREATE TABLE recipe_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id INTEGER NOT NULL,
    source_type TEXT NOT NULL,
    item_id INTEGER,
    ic_recipe_id INTEGER,
    qty_per_unit REAL NOT NULL,
    CHECK (source_type IN ('item', 'ic_recipe')),
    CHECK (
        (source_type = 'item' AND item_id IS NOT NULL AND ic_recipe_id IS NULL)
     OR (source_type = 'ic_recipe' AND ic_recipe_id IS NOT NULL AND item_id IS NULL)
    ),
    FOREIGN KEY (recipe_id) REFERENCES recipes(id) ON DELETE CASCADE
);
```

**不**给 `recipe_items.item_id` / `ic_recipe_id` 加 SQLite 外键（多态外键不支持）；删除品项/ic_recipe 时显式检查引用，命中即拦截。

### 2.2 既有 items 表迁移

```sql
ALTER TABLE items ADD COLUMN selling_price REAL NOT NULL DEFAULT 0;
ALTER TABLE items ADD COLUMN selling_price_updated_at TEXT;
```

放在 `db/__init__.py:migrate_warehouse_db_columns()` 内，幂等（跟现有 `gram_per_unit` / `aux_unit` / `aux_rate` 同模式 PRAGMA 检查后再 ALTER）。

### 2.3 关键约束

- `qty_per_unit` 精度：`REAL`，`_helpers.parse_qty` 2-dp
- 删除保护：
  - `ic_recipes` 被 `recipe_items` 引用 → 拦截（"该冰激凌配方被 X 个出品配方引用，无法删除"）
  - `items` 被 `ic_recipe_items` 或 `recipe_items(source_type='item')` 引用 → 拦截
- 不支持 `recipes` 引用 `recipes`（防递归），CHECK 不写但 UI 层不暴露此选项

---

## 3. 纯函数（`blueprints/recipe_cost_pure.py`）

```python
def qty_to_stock_units(qty: float, item: dict) -> Decimal:
    """配方用量 (克 或 库存单位) → 库存单位。

    Reuses _helpers.parse_qty + grams_to_stock.
    gram_per_unit > 0 时 qty 视为克；否则 qty 即库存单位。
    """

def line_cost(qty: float, item: dict, use_temp_selling: Decimal | None = None) -> dict:
    """Returns {qty_stock, cost_purchase, cost_selling} for one line.

    use_temp_selling: 非 None 时用临时售价覆盖 item.selling_price（前端滑块场景）。
    """

def ic_recipe_cost(conn, ic_recipe_id: int, temp_prices: dict[int, Decimal] | None = None) -> dict:
    """Returns {cost_purchase, cost_selling, sale_price, margin_purchase, lines}."""
    # temp_prices: {item_id: temp_selling_price}，前端滑块覆盖用

def recipe_cost(conn, recipe_id: int, temp_prices: dict | None = None) -> dict:
    """多态原料展开：item 直接计算；ic_recipe 引用递归 1 层（取出底层 items 的 cost_purchase/cost_selling 合计）。

    关键决策：ic_recipe 引用按 ic_recipe.sale_price 计入（不展开为原料合计）。
    这是为了避免"双重计算"——冰激凌作为成品有自身售价，引用方按该售价折算；
    出品的"采购成本"=原料的采购成本合计（用底层 items.unit_cost）；"销售成本"=Σ(原料的 selling_price 折算)。
    详见 §3.1。
    """

def qty_to_stock_units_batch(...)  # 内部辅助，避免重复
```

### 3.1 关键公式（与前端 JS 镜像）

```
# 单条原料行
qty_stock = qty_to_stock_units(qty, item)  # 含克重换算
line_cost_purchase = qty_stock × item.unit_cost
line_cost_selling  = qty_stock × item.selling_price (或 temp_selling)

# 冰激凌配方总成本
ic_recipe.cost_purchase = Σ line_cost_purchase (展开所有 ic_recipe_items)
ic_recipe.cost_selling  = Σ line_cost_selling  (展开所有 ic_recipe_items)

# 出品配方（多态原料）
recipe.cost_purchase = Σ
  - 若 source_type='item' 且启用克：qty_stock × item.unit_cost
  - 若 source_type='item' 且未启用克：qty × item.unit_cost
  - 若 source_type='ic_recipe'：qty × ic_recipe.cost_purchase_per_output_unit
    （即 ic_recipe 的每份输出单位的采购成本；qty 仍按 output_unit 同型换算）

recipe.cost_selling = Σ（同样的逻辑，把 unit_cost 换成 selling_price / temp_selling）

# 毛利率
recipe.margin_purchase = (recipe.sale_price − recipe.cost_purchase) / recipe.sale_price  # sale_price > 0
ic_recipe.margin_purchase = (ic_recipe.sale_price − ic_recipe.cost_purchase) / ic_recipe.sale_price
```

> **关键**：ic_recipe 引用作为"成品价"按 ic_recipe.sale_price × 折算系数进入 recipe 的 cost_purchase/cost_selling。这避免了双重展开（ic_recipe 内的原料价 × recipe 内 qty 会重复计入）。  
> 用户场景验证：出品"柠檬茶"含 50g 香草冰淇淋 → 香草冰淇淋 ic_recipe 内部 3 个 item 原料，sale_price ¥25，cost_purchase ¥8.50/份（100g）→ 50g = 0.5 份 → recipe.cost_purchase 计入 0.5 × 8.50 = ¥4.25（按采购成本）+ 0.5 × 25 = ¥12.50（按销售价值）。

### 3.2 边界
- `sale_price <= 0` 时 `margin_purchase = None`，UI 显示 "—"
- `qty <= 0` 的行不参与计算（保存时被 form 跳过）
- `gram_per_unit <= 0` 时 qty 视为库存单位（不除）

---

## 4. 路由（`blueprints/recipe_cost.py`）

| 路由 | 方法 | 权限 | 功能 |
|------|------|------|------|
| `/recipe-cost/` | GET | `@require_login` | 入口（跳到 `ic_recipes`） |
| `/recipe-cost/ic-recipes` | GET | `@require_login` | 冰激凌配方列表（成本/毛利率摘要） |
| `/recipe-cost/ic-recipes/new` | GET/POST | `@require_platform_admin` | 新建 |
| `/recipe-cost/ic-recipes/<int:id>/edit` | GET/POST | `@require_platform_admin` | 编辑 |
| `/recipe-cost/ic-recipes/<int:id>/delete` | POST | `@require_platform_admin` | 删除（被引用 → 拦截） |
| `/recipe-cost/recipes` | GET | `@require_login` | 出品配方列表 |
| `/recipe-cost/recipes/new` | GET/POST | `@require_platform_admin` | 新建 |
| `/recipe-cost/recipes/<int:id>/edit` | GET/POST | `@require_platform_admin` | 编辑（多态原料） |
| `/recipe-cost/recipes/<int:id>/delete` | POST | `@require_platform_admin` | 删除（被引用 → 拦截） |
| `/recipe-cost/api/cost/<kind>/<int:id>` | GET | `@require_login` | 只读 JSON `{cost_purchase, cost_selling, sale_price, margin}` |
| `/recipe-cost/items/<int:item_id>/update-selling-price` | POST | `@require_platform_admin` | 滑块"保存为新价"：更新 `items.selling_price` + `selling_price_updated_at`，写 audit_log |

### 4.1 实时重算（前端 JS）

`ic_recipe_edit.html` 与 `recipe_edit.html` 内嵌 JS：

- 服务端渲染时把每个原料行的 `data-unit-cost` / `data-selling-price` / `data-gram-per-unit` / `data-aux-rate` 写到 DOM
- **编辑出品配方时**，服务端把所有 ic_recipes 的 `{id, sale_price, cost_purchase_per_unit, cost_selling_per_unit}` 序列化成 JSON 注入 `data-ic-recipes` 属性（避免前端发请求查 ic_recipe 成本）；切换"类型" select 到 ic_recipe 时下拉从该 JSON 渲染
- 监听每行 `qty` 输入、原料 select、sale_price 输入 → 立即重算该行小计 + 底部总成本/毛利率（**不发请求**）
- 全局开关 "使用临时价"（默认 off）：开启时每行滑块生效，滑块的 value 进入重算；关闭时滑块隐藏、用真实价格
- 滑块旁 "保存为新价" 按钮：POST 到 `/recipe-cost/items/<id>/update-selling-price`，body=`{selling_price: <value>}`，刷新页面（让所有行重渲染）

### 4.2 编辑表单提交

仿 `production/product_edit.py`：用 `bom_row_id[]` / `bom_item_id[]` / `bom_qty[]` / `bom_delete[]` 平行数组提交。出品编辑页额外加 `bom_source_type[]` 列。

### 4.3 audit_log
- `recipe_cost.ic_recipe.create` / `.update` / `.delete`
- `recipe_cost.recipe.create` / `.update` / `.delete`
- `recipe_cost.items.update_selling_price`（detail 记 `{old, new, from_recipe_id}`）

---

## 5. UI（移动端优先，复用 `static/style.css`）

### 5.1 入口 `/recipe-cost/`
两个大卡片（冰激凌配方 / 出品配方），显示配方数 + 平均毛利率。

### 5.2 冰激凌配方列表 `/recipe-cost/ic-recipes`
3-列卡片网格。每张卡：名称 / output_qty+output_unit / 配方项数 / 采购成本 / 销售价值 / 售价 / 毛利率 / 售价 ¥X.XX / 编辑/删除按钮。

### 5.3 冰激凌配方编辑 `/recipe-cost/ic-recipes/<id>/edit`
上方基础信息表单（名称/output_unit/output_qty/sale_price/备注）。下方 BOM 表（仿 `production/product_edit.html`）：
- 列：原料 select（按品类分组）/ qty / 显示单位（克 or 件）/ 临时价滑块 / 临时价显示 / "保存为新价"按钮 / 采购小计 / 销售小计 / 删除 checkbox
- 全局开关："使用临时价"（default off）
- 底部 footer：总采购成本 / 总销售价值 / 售价 / 毛利率 / 毛利额
- 滑块：min=0, step=0.01, max=item.selling_price×3（防止 UI 出极端值）；默认 value = item.selling_price

### 5.4 出品配方列表与编辑
列表同 §5.2（多算冰激凌引用数）。编辑：
- BOM 表多一列"类型" select（item / ic_recipe），切换时联动 select 内容
- 冰激凌引用行的"采购/销售小计" = qty × ic_recipe.{cost_purchase_per_unit / cost_selling_per_unit}
- item 引用行同冰激凌编辑页

### 5.5 templates/base.html 修改
Sidebar + mobile nav 在 "生产录入" 后插入 "配方成本" 链接（仅 `g.user['is_admin']` 可见）。

---

## 6. 测试矩阵（pytest，≥80% 覆盖）

### 6.1 单元（`test_recipe_cost_pure.py`，≥12 用例）
- `qty_to_stock_units`：gram_per_unit=50, qty=100 → 2；gram_per_unit=0, qty=100 → 100；边界 0 / 负数
- `line_cost`：基础 + temp_selling 覆盖
- `ic_recipe_cost`：空配方 → 全 0；3 原料含克重 → sum 正确；带 selling_price → cost_selling 正确；temp_prices 覆盖
- `recipe_cost`：全 item；混合 ic_recipe 引用（验证 ic_recipe 引用按 sale_price 折算）；ic_recipe 引用 + temp_prices；空配方；ic_recipe 引用为 ic_recipe.id=不存在 → 抛错
- `recipe_cost` 不递归：ic_recipe 引 ic_recipe 场景（DB 层不允许，测试用 raw INSERT 制造 → 抛错或 limit depth）

### 6.2 集成（`test_recipe_cost_route.py`，≥10 用例）
- items 加 selling_price：POST `/items/<id>/edit` 含 selling_price → DB 写入；GET `/items` 渲染含 selling_price
- 新建/编辑/删除 ic_recipe
- 新建/编辑/删除 recipe（含多态原料）
- 删除保护：ic_recipe 被 recipe 引用 → POST delete 拦截
- 滑块保存：POST `/recipe-cost/items/<id>/update-selling-price` → DB 写入 + audit_log
- 只读 API：`GET /recipe-cost/api/cost/recipe/<id>` 返回正确 JSON（不依赖前端）

### 6.3 端到端（手工）
- 在 `/items` 给某品项设 selling_price=10、unit_cost=5
- 新建冰激凌配方含该品项 100g（gram_per_unit=50）→ 列表与编辑页底部显示 cost_purchase = 5/50×100 = ¥10.00、cost_selling = ¥20.00
- 出品配方引该冰激凌 50g + 直接引 1 个辅料 200g → 验证 ic_recipe 引用按 sale_price 折算
- 改某原料 selling_price 不刷新页面 → JS 立即重算
- 临时价滑块开启 → 调到 ¥8.00 → 毛利率变化；点 "保存为新价" → 刷新页面，DB 中 items.selling_price 变成 ¥8.00

---

## 7. 异常表

| 场景 | 行为 |
|---|---|
| 重复配方名称（ic_recipes/recipes） | UNIQUE 冲突 → flash "配方名已存在" |
| 删除被引用的 ic_recipe | flash "该冰激凌配方被 N 个出品引用，无法删除" |
| 删除被引用的 items | flash "该品项已被 X 个配方引用，无法删除"（与现有 `delete_item` 同样扩展） |
| selling_price < 0 | form 校验 → flash "销售单价不能为负" |
| sale_price = 0 | 毛利率显示 "—"，不报错 |
| 编辑页 sale_price 输入负数 | flash 拦截 |
| `recipes.<id>` 不存在 | 404 / redirect + flash |
| 临时价滑块值 < 0 | min=0 限制 |
| POST update-selling-price 无权限 | 403 |

---

## 8. 验收门

1. `pytest -q` 全绿（含新 `test_recipe_cost_*`），覆盖率 ≥ 80%
2. `ruff check .` 0 警告
3. 仓库 `wh_003` 打开 `/recipe-cost/` 能看到入口
4. §6.3 全部手工用例通过
5. 既有 production 模块功能未回归（`pytest tests/test_production_grams.py -v` 全绿）
6. 既有 inventory / outbound / restock 路由未变（仅 items 模板加输入框；items 列表/编辑页功能不破）
7. SPEC §0.2 YAGNI 项全部确认不实现