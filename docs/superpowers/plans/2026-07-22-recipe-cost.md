# 配方成本计算模块 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在指定仓库（如 `wh_003` 研发中心）新增"配方成本计算"模块：每个品项增加销售单价；维护冰激凌配方与出品配方（出品可引冰激凌）；实时计算两套成本与毛利率；支持临时调价找合适毛利率并一键保存为新价。

**Architecture:** 沿用现有"按仓库隔离" SQLite + Flask Blueprint + Jinja + 原生 CSS 模式。4 张新表（`ic_recipes` / `ic_recipe_items` / `recipes` / `recipe_items`）走 `CREATE TABLE IF NOT EXISTS` 自动迁移；`items` 表加 `selling_price` / `selling_price_updated_at` 两列（幂等 ALTER）。前端 JS 镜像纯函数公式做实时重算，不存额外快照；后端只读 API 做 sanity check。

**Tech Stack:** Flask 3 / SQLite 3 / Jinja2 / 原生 CSS（移动端优先）。无新依赖。

---

## File Structure

新增（按职责拆分）：

| 文件 | 职责 |
|---|---|
| `blueprints/recipe_cost_pure.py` | 纯函数（成本/毛利计算、单位换算、多态展开） |
| `blueprints/recipe_cost.py` | 所有路由（CRUD + 滑块保存接口 + 只读 API） |
| `templates/recipe_cost/base_recipe.html` | 配方模块底版（继承 base.html, no_sidebar=True, 顶部 tabs） |
| `templates/recipe_cost/landing.html` | `/recipe-cost/` 入口卡片 |
| `templates/recipe_cost/ic_recipes.html` | 冰激凌配方列表 |
| `templates/recipe_cost/ic_recipe_edit.html` | 冰激凌配方编辑（含滑块 + JS 实时重算） |
| `templates/recipe_cost/recipes.html` | 出品配方列表 |
| `templates/recipe_cost/recipe_edit.html` | 出品配方编辑（含多态选择器 + 滑块 + JS 实时重算） |
| `tests/test_recipe_cost_pure.py` | 纯函数单元测试（≥12 用例） |
| `tests/test_recipe_cost_route.py` | 路由集成测试（≥10 用例） |

修改：

| 文件 | 改动 |
|---|---|
| `db/__init__.py` | `WAREHOUSE_SCHEMA` 末尾追加 4 张表 + `items` 表加 `selling_price` / `selling_price_updated_at` 列；`migrate_warehouse_db_columns()` 加幂等 ALTER |
| `blueprints/items.py` | `items_list` POST/GET 与 `edit_item` POST/GET 增加 `selling_price` 表单字段 |
| `blueprints/_helpers.py` | 新增 `qty_to_stock_units(qty, item)` helper（复用 `grams_to_stock`） |
| `templates/items.html` | "进货单价"字段后追加"销售单价（¥）"输入框 |
| `templates/edit_item.html` | 同上 |
| `templates/base.html` | sidebar + mobile-nav 追加"配方成本"链接（仅 admin 可见，复用 `g.user['is_admin']`） |
| `app.py` | `register_blueprint(recipe_cost_bp)` |

---

## Global Constraints

- **Python 3.10+** target（PEP 604 `int | None`、`tuple[...]` 等不可用，使用 `Optional`/`Union`）
- **行长度 100**（Ruff E501 已忽略 B008，仍按习惯保持）
- **Ruff 规则 E/F/W/I/B/UP**（沿用项目 `pyproject.toml`）
- **覆盖 ≥80%**（`pytest --cov`）
- **频率 commit**：每个 Task 一个 commit，commit message 遵循 conventional commits
- **decimal 精度**：所有 qty/cost 用 `_helpers.parse_qty` 处理（2 dp）
- **复刻现有模式**：BOM 平行数组表单（`bom_row_id[]` / `bom_item_id[]` / `bom_qty[]` / `bom_delete[]`）、`no_sidebar=True`、`audit()` 调用
- **不动 production 模块**：现有 `product_bom` / `products` / `production_run*` 全部不改（spec §0.2 YAGNI）

---

## Task 1: items 表加 `selling_price` 列 + 4 张配方表 schema

**Files:**
- Modify: `db/__init__.py`（`WAREHOUSE_SCHEMA` 末尾 + `migrate_warehouse_db_columns()` 内加幂等 ALTER）

- [ ] **Step 1: 在 `WAREHOUSE_SCHEMA` 末尾追加新表**

打开 `db/__init__.py`，定位到 `CREATE INDEX IF NOT EXISTS idx_pruni_run ON production_run_items(run_id);` 这一行（`WAREHOUSE_SCHEMA` 三引号结束前），在其后追加：

```sql
CREATE TABLE IF NOT EXISTS ic_recipes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    note TEXT,
    output_unit TEXT NOT NULL DEFAULT 'g',
    output_qty REAL NOT NULL DEFAULT 100,
    sale_price REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ic_recipe_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ic_recipe_id INTEGER NOT NULL,
    item_id INTEGER NOT NULL,
    qty_per_unit REAL NOT NULL,
    UNIQUE(ic_recipe_id, item_id),
    FOREIGN KEY (ic_recipe_id) REFERENCES ic_recipes(id) ON DELETE CASCADE,
    FOREIGN KEY (item_id) REFERENCES items(id)
);

CREATE TABLE IF NOT EXISTS recipes (
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

CREATE TABLE IF NOT EXISTS recipe_items (
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

CREATE INDEX IF NOT EXISTS idx_recipe_items_recipe ON recipe_items(recipe_id);
CREATE INDEX IF NOT EXISTS idx_ic_recipe_items_ic_recipe ON ic_recipe_items(ic_recipe_id);
```

- [ ] **Step 2: 在 `migrate_warehouse_db_columns()` 内加 `items.selling_price` 幂等 ALTER**

打开 `db/__init__.py`，定位到 `migrate_warehouse_db_columns` 函数末尾的 `conn.commit()` 之前。在 `item_cols = ...` 块附近追加：

```python
        item_cols = {r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()}
        if "selling_price" not in item_cols:
            conn.execute(
                "ALTER TABLE items ADD COLUMN selling_price REAL NOT NULL DEFAULT 0"
            )
        if "selling_price_updated_at" not in item_cols:
            conn.execute(
                "ALTER TABLE items ADD COLUMN selling_price_updated_at TEXT"
            )
```

注意：`item_cols` 在函数中已存在（用于 `gram_per_unit` / `aux_unit` / `aux_rate` 检查），**复用同一定义**，不要重复定义。

- [ ] **Step 3: 启动应用验证 schema 自动建立**

```bash
cd /Users/ericmr/Documents/GitHub/DailyCheck
flask --app app run --host 0.0.0.0 --port 5001 &
sleep 2
sqlite3 db/warehouses/*.db ".schema ic_recipes" 2>/dev/null | head -10
sqlite3 db/warehouses/*.db ".schema ic_recipe_items" 2>/dev/null | head -5
sqlite3 db/warehouses/*.db ".schema recipes" 2>/dev/null | head -10
sqlite3 db/warehouses/*.db ".schema recipe_items" 2>/dev/null | head -10
sqlite3 db/warehouses/*.db "PRAGMA table_info(items)" 2>/dev/null | grep selling
kill %1
```

Expected: 4 个 `.schema` 命令都打出对应表头；`PRAGMA table_info(items)` 输出含 `selling_price` 与 `selling_price_updated_at` 两行。任一失败 → 检查 SQL 语法。

- [ ] **Step 4: Commit**

```bash
git add db/__init__.py
git commit -m "feat(recipe-cost): add 4 recipe tables + items.selling_price"
```

---

## Task 2: 纯函数 helper — `qty_to_stock_units`

**Files:**
- Modify: `blueprints/_helpers.py`（末尾追加 helper）

- [ ] **Step 1: 写失败测试**

创建 `tests/test_recipe_cost_pure.py`：

```python
"""recipe_cost 纯函数测试：单位换算与基础成本行。"""
from decimal import Decimal


def test_qty_to_stock_units_grams_converts_to_stock():
    """启用克的品项：qty 视为克 → 除 gram_per_unit。"""
    from blueprints._helpers import qty_to_stock_units
    item = {"gram_per_unit": 50.0, "aux_rate": 50.0, "unit": "件"}
    # 100 克 = 2 件
    assert qty_to_stock_units(100, item) == Decimal("2.00")


def test_qty_to_stock_units_no_grams_returns_qty_unchanged():
    """未启用克的品项：qty 即库存单位。"""
    from blueprints._helpers import qty_to_stock_units
    item = {"gram_per_unit": 0.0, "aux_rate": 0.0, "unit": "件"}
    assert qty_to_stock_units(7.5, item) == Decimal("7.50")


def test_qty_to_stock_units_zero_grams_per_unit():
    """gram_per_unit=0 但 aux_rate>0 不应触发克换算（防御）。"""
    from blueprints._helpers import qty_to_stock_units
    item = {"gram_per_unit": 0.0, "aux_rate": 10.0, "unit": "件"}
    assert qty_to_stock_units(50, item) == Decimal("50.00")


def test_qty_to_stock_units_2dp_quantize():
    """结果量化到 2 位小数。"""
    from blueprints._helpers import qty_to_stock_units
    item = {"gram_per_unit": 3.0, "aux_rate": 3.0, "unit": "件"}
    # 10 / 3 = 3.333... → 3.33
    assert qty_to_stock_units(10, item) == Decimal("3.33")
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /Users/ericmr/Documents/GitHub/DailyCheck
pytest tests/test_recipe_cost_pure.py -v
```

Expected: 4 个测试都 ImportError 或 AttributeError（`qty_to_stock_units` 不存在）。

- [ ] **Step 3: 在 `blueprints/_helpers.py` 实现 helper**

打开 `blueprints/_helpers.py`，在 `base_to_aux` 函数之后追加：

```python
def qty_to_stock_units(qty, item: dict):
    """配方用量 (克 或 库存单位) → 库存单位（Decimal, 2dp）。

    item 必须含 gram_per_unit / aux_rate / unit。复用 grams_to_stock 的口径：
    gram_per_unit > 0 → qty 视为克，除以 gram_per_unit；否则 qty 即库存单位。
    返回 Decimal（不转 float，保持下游 cost 计算精度）。
    """
    qty_d = Decimal(str(qty)).quantize(Decimal("0.01"))
    gpu = float(item.get("gram_per_unit") or 0)
    if gpu > 0:
        return (qty_d / Decimal(str(gpu))).quantize(Decimal("0.01"))
    return qty_d
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest tests/test_recipe_cost_pure.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add blueprints/_helpers.py tests/test_recipe_cost_pure.py
git commit -m "feat(recipe-cost): qty_to_stock_units helper"
```

---

## Task 3: 纯函数 — `line_cost` / `ic_recipe_cost` / `recipe_cost`

**Files:**
- Create: `blueprints/recipe_cost_pure.py`

- [ ] **Step 1: 写失败测试（追加到 `tests/test_recipe_cost_pure.py`）**

打开 `tests/test_recipe_cost_pure.py`，在文件末尾追加：

```python
def test_line_cost_basic_uses_unit_cost_and_selling_price():
    """单行成本：qty × unit_cost 与 qty × selling_price。"""
    from blueprints.recipe_cost_pure import line_cost
    item = {"unit_cost": 5.0, "selling_price": 10.0, "gram_per_unit": 0.0}
    r = line_cost(2.0, item)
    assert r["cost_purchase"] == Decimal("10.00")
    assert r["cost_selling"] == Decimal("20.00")


def test_line_cost_with_grams_converts_first():
    """启用克的品项：qty=克 → 先转库存单位再乘。"""
    from blueprints.recipe_cost_pure import line_cost
    item = {"unit_cost": 5.0, "selling_price": 10.0, "gram_per_unit": 50.0}
    r = line_cost(100.0, item)
    # 100/50 = 2 件；2*5=10；2*10=20
    assert r["cost_purchase"] == Decimal("10.00")
    assert r["cost_selling"] == Decimal("20.00")


def test_line_cost_temp_selling_overrides_selling_price():
    """temp_selling_price 覆盖 selling_price。"""
    from blueprints.recipe_cost_pure import line_cost
    item = {"unit_cost": 5.0, "selling_price": 10.0, "gram_per_unit": 0.0}
    r = line_cost(2.0, item, temp_selling_price=Decimal("7.00"))
    assert r["cost_purchase"] == Decimal("10.00")
    assert r["cost_selling"] == Decimal("14.00")


def test_ic_recipe_cost_empty_recipe_returns_zero():
    """空冰激凌配方：cost 全 0、margin None。"""
    import sqlite3
    from blueprints.recipe_cost_pure import ic_recipe_cost
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE ic_recipes (id INTEGER PRIMARY KEY, sale_price REAL, output_qty REAL)"
    )
    conn.execute(
        "CREATE TABLE ic_recipe_items ("
        "id INTEGER PRIMARY KEY, ic_recipe_id INTEGER, item_id INTEGER, qty_per_unit REAL)"
    )
    conn.execute("INSERT INTO ic_recipes VALUES (1, 25.0, 100.0)")
    conn.commit()
    result = ic_recipe_cost(conn, 1)
    assert result["cost_purchase"] == Decimal("0.00")
    assert result["cost_selling"] == Decimal("0.00")
    assert result["sale_price"] == 25.0
    assert result["margin_purchase"] is None  # 0/0 不算
    conn.close()


def test_ic_recipe_cost_sums_3_items_with_grams():
    """3 原料含克重 → cost_purchase = sum(qty/gram_per_unit × unit_cost)。"""
    import sqlite3
    from blueprints.recipe_cost_pure import ic_recipe_cost
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE ic_recipes (id INTEGER PRIMARY KEY, sale_price REAL, output_qty REAL);
        CREATE TABLE ic_recipe_items (
            id INTEGER PRIMARY KEY, ic_recipe_id INTEGER, item_id INTEGER, qty_per_unit REAL);
        CREATE TABLE items (
            id INTEGER PRIMARY KEY, unit_cost REAL, selling_price REAL,
            gram_per_unit REAL, aux_rate REAL, unit TEXT);
        INSERT INTO ic_recipes VALUES (1, 25.0, 100.0);
        INSERT INTO items VALUES (1, 5.0, 10.0, 50.0, 50.0, '件');
        INSERT INTO items VALUES (2, 3.0, 6.0, 30.0, 30.0, '件');
        INSERT INTO items VALUES (3, 2.0, 4.0, 0.0, 0.0, '件');
        INSERT INTO ic_recipe_items VALUES (1, 1, 1, 100.0);  -- 100g item1=2件
        INSERT INTO ic_recipe_items VALUES (2, 1, 2, 60.0);   -- 60g item2=2件
        INSERT INTO ic_recipe_items VALUES (3, 1, 3, 1.5);    -- 1.5 件 item3
        """
    )
    conn.commit()
    r = ic_recipe_cost(conn, 1)
    # cost_purchase = 2*5 + 2*3 + 1.5*2 = 10+6+3 = 19
    assert r["cost_purchase"] == Decimal("19.00")
    # cost_selling = 2*10 + 2*6 + 1.5*4 = 20+12+6 = 38
    assert r["cost_selling"] == Decimal("38.00")
    # margin = (25 - 19) / 25 = 0.24
    assert abs(r["margin_purchase"] - 0.24) < 0.001
    assert len(r["lines"]) == 3
    conn.close()


def test_ic_recipe_cost_temp_prices_override():
    """temp_prices 字典按 item_id 覆盖。"""
    import sqlite3
    from decimal import Decimal
    from blueprints.recipe_cost_pure import ic_recipe_cost
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE ic_recipes (id INTEGER PRIMARY KEY, sale_price REAL, output_qty REAL);
        CREATE TABLE ic_recipe_items (
            id INTEGER PRIMARY KEY, ic_recipe_id INTEGER, item_id INTEGER, qty_per_unit REAL);
        CREATE TABLE items (
            id INTEGER PRIMARY KEY, unit_cost REAL, selling_price REAL,
            gram_per_unit REAL, aux_rate REAL, unit TEXT);
        INSERT INTO ic_recipes VALUES (1, 0, 100.0);
        INSERT INTO items VALUES (1, 5.0, 10.0, 0.0, 0.0, '件');
        INSERT INTO ic_recipe_items VALUES (1, 1, 1, 2.0);
        """
    )
    conn.commit()
    r = ic_recipe_cost(conn, 1, temp_prices={1: Decimal("7.00")})
    # cost_purchase = 2*5 = 10 (unit_cost 不变)
    assert r["cost_purchase"] == Decimal("10.00")
    # cost_selling = 2*7 = 14 (selling_price 被临时覆盖)
    assert r["cost_selling"] == Decimal("14.00")
    conn.close()


def test_recipe_cost_all_items():
    """出品配方：原料全是 item。"""
    import sqlite3
    from decimal import Decimal
    from blueprints.recipe_cost_pure import recipe_cost
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE recipes (
            id INTEGER PRIMARY KEY, sale_price REAL, output_qty REAL);
        CREATE TABLE recipe_items (
            id INTEGER PRIMARY KEY, recipe_id INTEGER, source_type TEXT,
            item_id INTEGER, ic_recipe_id INTEGER, qty_per_unit REAL);
        CREATE TABLE items (
            id INTEGER PRIMARY KEY, unit_cost REAL, selling_price REAL,
            gram_per_unit REAL, aux_rate REAL, unit TEXT);
        CREATE TABLE ic_recipes (
            id INTEGER PRIMARY KEY, sale_price REAL, output_qty REAL);
        CREATE TABLE ic_recipe_items (
            id INTEGER PRIMARY KEY, ic_recipe_id INTEGER, item_id INTEGER, qty_per_unit REAL);
        INSERT INTO recipes VALUES (10, 15.0, 1.0);
        INSERT INTO items VALUES (1, 2.0, 4.0, 30.0, 30.0, '件');
        INSERT INTO items VALUES (2, 1.0, 2.0, 0.0, 0.0, '件');
        INSERT INTO recipe_items VALUES (1, 10, 'item', 1, NULL, 30.0);  -- 30g item1=1件
        INSERT INTO recipe_items VALUES (2, 10, 'item', 2, NULL, 3.0);   -- 3 件 item2
        """
    )
    conn.commit()
    r = recipe_cost(conn, 10)
    # cost_purchase = 1*2 + 3*1 = 5
    assert r["cost_purchase"] == Decimal("5.00")
    # cost_selling = 1*4 + 3*2 = 10
    assert r["cost_selling"] == Decimal("10.00")
    # margin = (15-5)/15 = 0.6667
    assert abs(r["margin_purchase"] - 0.6667) < 0.001
    conn.close()


def test_recipe_cost_references_ic_recipe_by_sale_price():
    """出品引用冰激凌配方：按 ic_recipe.cost_purchase_per_unit × qty 计入，
    不展开到底层 items（避免双重计算）。"""
    import sqlite3
    from decimal import Decimal
    from blueprints.recipe_cost_pure import recipe_cost
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE recipes (
            id INTEGER PRIMARY KEY, sale_price REAL, output_qty REAL);
        CREATE TABLE recipe_items (
            id INTEGER PRIMARY KEY, recipe_id INTEGER, source_type TEXT,
            item_id INTEGER, ic_recipe_id INTEGER, qty_per_unit REAL);
        CREATE TABLE items (
            id INTEGER PRIMARY KEY, unit_cost REAL, selling_price REAL,
            gram_per_unit REAL, aux_rate REAL, unit TEXT);
        CREATE TABLE ic_recipes (
            id INTEGER PRIMARY KEY, sale_price REAL, output_qty REAL);
        CREATE TABLE ic_recipe_items (
            id INTEGER PRIMARY KEY, ic_recipe_id INTEGER, item_id INTEGER, qty_per_unit REAL);
        -- ic_recipe 1 = 香草冰淇淋：每份（output_qty=100g）成本 ¥8.50
        INSERT INTO ic_recipes VALUES (1, 25.0, 100.0);
        INSERT INTO items VALUES (100, 5.0, 10.0, 50.0, 50.0, '件');
        INSERT INTO ic_recipe_items VALUES (1, 1, 100, 50.0);
        -- recipe 10 = 柠檬茶：售价 15，引 ic_recipe 1 用量 50g
        INSERT INTO recipes VALUES (10, 15.0, 1.0);
        INSERT INTO recipe_items VALUES (1, 10, 'ic_recipe', NULL, 1, 50.0);
        """
    )
    conn.commit()
    r = recipe_cost(conn, 10)
    # ic_recipe.cost_purchase_per_unit = 8.50/100g = 0.085/g
    # recipe.cost_purchase = 50 * 0.085 = 4.25
    # recipe.cost_selling = 50 * (25/100) = 12.50
    assert r["cost_purchase"] == Decimal("4.25")
    assert r["cost_selling"] == Decimal("12.50")
    # margin = (15-4.25)/15 ≈ 0.7167
    assert abs(r["margin_purchase"] - 0.7167) < 0.001
    conn.close()


def test_recipe_cost_empty_returns_zero():
    """空出品配方。"""
    import sqlite3
    from blueprints.recipe_cost_pure import recipe_cost
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE recipes (id INTEGER PRIMARY KEY, sale_price REAL, output_qty REAL);
        CREATE TABLE recipe_items (
            id INTEGER PRIMARY KEY, recipe_id INTEGER, source_type TEXT,
            item_id INTEGER, ic_recipe_id INTEGER, qty_per_unit REAL);
        CREATE TABLE ic_recipes (id INTEGER PRIMARY KEY, sale_price REAL, output_qty REAL);
        INSERT INTO recipes VALUES (10, 0.0, 1.0);
        """
    )
    conn.commit()
    r = recipe_cost(conn, 10)
    assert r["cost_purchase"] == Decimal("0.00")
    assert r["cost_selling"] == Decimal("0.00")
    assert r["margin_purchase"] is None
    conn.close()


def test_recipe_cost_with_temp_prices_in_mixed_lines():
    """出品含 ic_recipe 引用 + item + temp_prices 混合。"""
    import sqlite3
    from decimal import Decimal
    from blueprints.recipe_cost_pure import recipe_cost
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE recipes (id INTEGER PRIMARY KEY, sale_price REAL, output_qty REAL);
        CREATE TABLE recipe_items (
            id INTEGER PRIMARY KEY, recipe_id INTEGER, source_type TEXT,
            item_id INTEGER, ic_recipe_id INTEGER, qty_per_unit REAL);
        CREATE TABLE items (
            id INTEGER PRIMARY KEY, unit_cost REAL, selling_price REAL,
            gram_per_unit REAL, aux_rate REAL, unit TEXT);
        CREATE TABLE ic_recipes (id INTEGER PRIMARY KEY, sale_price REAL, output_qty REAL);
        CREATE TABLE ic_recipe_items (
            id INTEGER PRIMARY KEY, ic_recipe_id INTEGER, item_id INTEGER, qty_per_unit REAL);
        INSERT INTO ic_recipes VALUES (1, 25.0, 100.0);
        INSERT INTO items VALUES (1, 5.0, 10.0, 50.0, 50.0, '件');
        INSERT INTO ic_recipe_items VALUES (1, 1, 1, 100.0);
        INSERT INTO recipes VALUES (10, 15.0, 1.0);
        INSERT INTO items VALUES (2, 2.0, 4.0, 0.0, 0.0, '件');
        INSERT INTO recipe_items VALUES (1, 10, 'ic_recipe', NULL, 1, 50.0);
        INSERT INTO recipe_items VALUES (2, 10, 'item', 2, NULL, 2.0);
        """
    )
    conn.commit()
    # temp_prices 覆盖 item 2 的 selling_price
    r = recipe_cost(conn, 10, temp_prices={2: Decimal("3.00")})
    # cost_purchase: ic_recipe 部分 50 * (8.50/100) = 4.25；item 部分 2*2 = 4；合计 8.25
    assert r["cost_purchase"] == Decimal("8.25")
    # cost_selling: ic_recipe 50 * (25/100) = 12.50；item 2*3 = 6；合计 18.50
    assert r["cost_selling"] == Decimal("18.50")
    conn.close()
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest tests/test_recipe_cost_pure.py -v
```

Expected: 前 4 个 line_cost / qty_to_stock_units 通过；后 8 个 ImportError（`recipe_cost_pure` 模块不存在）。

- [ ] **Step 3: 创建 `blueprints/recipe_cost_pure.py`**

```python
"""配方成本计算纯函数：单位换算、行成本、冰激凌/出品配方总成本。

不做 DB 写。所有数据从调用方传入（conn + ids），无副作用，便于单测。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional


def _safe_decimal(value) -> Decimal:
    """None / 数字 / 字符串 → Decimal(2dp)。None 视作 0。"""
    if value is None:
        return Decimal("0.00")
    return Decimal(str(value)).quantize(Decimal("0.01"))


def qty_to_stock_units(qty, item: dict) -> Decimal:
    """配方用量 → 库存单位（Decimal, 2dp）。

    item 必须含 gram_per_unit。gram_per_unit > 0 → qty 视为克，除以它；
    否则 qty 即库存单位。
    与 blueprints._helpers.qty_to_stock_units 等价；本函数独立避免 blueprint
    之间的循环依赖（pure 模块不依赖任何 blueprint）。
    """
    qty_d = _safe_decimal(qty)
    gpu = float(item.get("gram_per_unit") or 0)
    if gpu > 0:
        return (qty_d / Decimal(str(gpu))).quantize(Decimal("0.01"))
    return qty_d


def line_cost(
    qty,
    item: dict,
    temp_selling_price: Optional[Decimal] = None,
) -> dict:
    """单条原料行：含克重换算的采购/销售成本。

    Returns: {qty_stock, cost_purchase, cost_selling} (all Decimal)
    """
    qty_stock = qty_to_stock_units(qty, item)
    unit_cost = _safe_decimal(item.get("unit_cost"))
    selling = (
        temp_selling_price
        if temp_selling_price is not None
        else _safe_decimal(item.get("selling_price"))
    )
    return {
        "qty_stock": qty_stock,
        "cost_purchase": (qty_stock * unit_cost).quantize(Decimal("0.01")),
        "cost_selling": (qty_stock * selling).quantize(Decimal("0.01")),
    }


def _margin(sale_price, cost_purchase) -> Optional[float]:
    if sale_price is None or float(sale_price) <= 0:
        return None
    sp = float(sale_price)
    cp = float(cost_purchase)
    return round((sp - cp) / sp, 4)


def _lines_for_recipe(conn, table: str, recipe_id: int, recipe_id_col: str):
    """提取配方的所有 BOM 行（含 items JOIN）。"""
    return conn.execute(
        f"""
        SELECT t.id AS line_id, t.qty_per_unit,
               t.item_id AS item_id,
               i.unit_cost, i.selling_price, i.gram_per_unit,
               i.aux_rate, i.unit
        FROM {table} t
        JOIN items i ON i.id = t.item_id
        WHERE t.{recipe_id_col} = ?
        ORDER BY t.id
        """,
        (recipe_id,),
    ).fetchall()


def ic_recipe_cost(
    conn,
    ic_recipe_id: int,
    temp_prices: Optional[dict] = None,
) -> dict:
    """冰激凌配方总成本（采购 + 销售价值）+ 毛利率 + 每行小计。

    temp_prices: {item_id: Decimal}，覆盖对应 item 的 selling_price。
    """
    temp_prices = temp_prices or {}
    recipe = conn.execute(
        "SELECT id, sale_price, output_qty FROM ic_recipes WHERE id = ?",
        (ic_recipe_id,),
    ).fetchone()
    if recipe is None:
        return None  # 不存在

    rows = _lines_for_recipe(conn, "ic_recipe_items", ic_recipe_id, "ic_recipe_id")
    cost_purchase = Decimal("0.00")
    cost_selling = Decimal("0.00")
    lines = []
    for r in rows:
        item = {
            "unit_cost": r["unit_cost"],
            "selling_price": r["selling_price"],
            "gram_per_unit": r["gram_per_unit"],
            "aux_rate": r["aux_rate"],
            "unit": r["unit"],
        }
        tmp = temp_prices.get(int(r["item_id"]))
        lc = line_cost(r["qty_per_unit"], item, temp_selling_price=tmp)
        cost_purchase += lc["cost_purchase"]
        cost_selling += lc["cost_selling"]
        lines.append({
            "item_id": int(r["item_id"]),
            "qty_per_unit": float(r["qty_per_unit"]),
            **lc,
        })

    sale_price = float(recipe["sale_price"] or 0)
    return {
        "cost_purchase": cost_purchase,
        "cost_selling": cost_selling,
        "sale_price": sale_price,
        "margin_purchase": _margin(sale_price, cost_purchase),
        "lines": lines,
    }


def recipe_cost(
    conn,
    recipe_id: int,
    temp_prices: Optional[dict] = None,
) -> dict:
    """出品配方总成本 + 毛利率。多态原料：
    - source_type='item' → line_cost（克重换算）
    - source_type='ic_recipe' → 按 ic_recipe.cost_purchase_per_unit × qty 计入
      （ic_recipe 引用不展开到底层 items，避免双重计算）
    """
    temp_prices = temp_prices or {}
    recipe = conn.execute(
        "SELECT id, sale_price, output_qty FROM recipes WHERE id = ?",
        (recipe_id,),
    ).fetchone()
    if recipe is None:
        return None

    rows = conn.execute(
        """
        SELECT ri.id AS line_id, ri.source_type, ri.qty_per_unit,
               ri.item_id, ri.ic_recipe_id,
               i.unit_cost, i.selling_price, i.gram_per_unit,
               i.aux_rate, i.unit
        FROM recipe_items ri
        LEFT JOIN items i ON i.id = ri.item_id
        WHERE ri.recipe_id = ?
        ORDER BY ri.id
        """,
        (recipe_id,),
    ).fetchall()

    cost_purchase = Decimal("0.00")
    cost_selling = Decimal("0.00")
    lines = []
    for r in rows:
        if r["source_type"] == "item":
            item = {
                "unit_cost": r["unit_cost"],
                "selling_price": r["selling_price"],
                "gram_per_unit": r["gram_per_unit"],
                "aux_rate": r["aux_rate"],
                "unit": r["unit"],
            }
            tmp = temp_prices.get(int(r["item_id"]))
            lc = line_cost(r["qty_per_unit"], item, temp_selling_price=tmp)
            cost_purchase += lc["cost_purchase"]
            cost_selling += lc["cost_selling"]
            lines.append({
                "line_type": "item",
                "item_id": int(r["item_id"]),
                "qty_per_unit": float(r["qty_per_unit"]),
                **lc,
            })
        elif r["source_type"] == "ic_recipe":
            ic_id = int(r["ic_recipe_id"])
            ic = conn.execute(
                "SELECT id, sale_price, output_qty FROM ic_recipes WHERE id = ?",
                (ic_id,),
            ).fetchone()
            if ic is None:
                continue  # ic_recipe 被删 → 跳过；UI 层会校验
            ic_cost = ic_recipe_cost(conn, ic_id, temp_prices=temp_prices)
            out_qty = float(ic["output_qty"] or 0)
            qty = _safe_decimal(r["qty_per_unit"])
            # per-unit cost = cost_purchase / output_qty
            if out_qty > 0:
                per_unit_purchase = (
                    ic_cost["cost_purchase"] / Decimal(str(out_qty))
                ).quantize(Decimal("0.0001"))
                per_unit_selling = (
                    ic_cost["cost_selling"] / Decimal(str(out_qty))
                ).quantize(Decimal("0.0001"))
            else:
                per_unit_purchase = Decimal("0.0000")
                per_unit_selling = Decimal("0.0000")
            line_purchase = (qty * per_unit_purchase).quantize(Decimal("0.01"))
            line_selling = (qty * per_unit_selling).quantize(Decimal("0.01"))
            cost_purchase += line_purchase
            cost_selling += line_selling
            lines.append({
                "line_type": "ic_recipe",
                "ic_recipe_id": ic_id,
                "qty_per_unit": float(r["qty_per_unit"]),
                "cost_purchase": line_purchase,
                "cost_selling": line_selling,
            })

    sale_price = float(recipe["sale_price"] or 0)
    return {
        "cost_purchase": cost_purchase,
        "cost_selling": cost_selling,
        "sale_price": sale_price,
        "margin_purchase": _margin(sale_price, cost_purchase),
        "lines": lines,
    }
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest tests/test_recipe_cost_pure.py -v
```

Expected: 全部 12+ passed. 任一失败 → 检查 Decimal 量化与空值处理。

- [ ] **Step 5: Commit**

```bash
git add blueprints/recipe_cost_pure.py tests/test_recipe_cost_pure.py
git commit -m "feat(recipe-cost): pure cost/margin calculations"
```

---

## Task 4: items 表 CRUD 加 `selling_price` 字段

**Files:**
- Modify: `blueprints/items.py`（POST/GET 与表单字段）
- Modify: `templates/items.html`（"销售单价"输入框）
- Modify: `templates/edit_item.html`（"销售单价"输入框）

- [ ] **Step 1: 写失败测试（追加到 `tests/test_recipe_cost_route.py`，先建空文件）**

创建 `tests/test_recipe_cost_route.py`：

```python
"""items.selling_price 字段端到端测试。"""
from datetime import datetime


def test_create_item_stores_selling_price(tmp_path, monkeypatch):
    """POST /items 含 selling_price=12.50 → DB 写入。"""
    import db as db_module
    import config as config_module
    from db import init_master_db, init_warehouse_db
    master_path = tmp_path / "master.db"
    wh_path = tmp_path / "wh.db"
    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", tmp_path)
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", tmp_path)
    init_master_db()
    init_warehouse_db(wh_path)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    import sqlite3
    m = sqlite3.connect(master_path)
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (1, 'admin', 'x', 1, ?)", (ts,))
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, created_at) "
        "VALUES (1, 'wh_t', 'T', ?, ?)", (str(wh_path), ts))
    m.execute(
        "INSERT INTO warehouse_users (user_id, warehouse_id, role) "
        "VALUES (1, 1, 'admin')")
    m.commit()
    m.close()

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = 1
        s["warehouse_id"] = 1

    cat_id = sqlite3.connect(wh_path).execute(
        "SELECT id FROM categories ORDER BY id LIMIT 1"
    ).fetchone()["id"]

    resp = client.post("/items", data={
        "name": "糖",
        "category_id": str(cat_id),
        "quantity": "10",
        "safety_stock": "0",
        "unit_cost": "5",
        "selling_price": "12.50",
        "unit": "件",
        "aux_unit": "",
        "aux_rate": "0",
    }, follow_redirects=True)
    assert resp.status_code == 200

    row = sqlite3.connect(wh_path).execute(
        "SELECT selling_price, selling_price_updated_at FROM items WHERE name='糖'"
    ).fetchone()
    assert float(row["selling_price"]) == 12.50
    assert row["selling_price_updated_at"] is not None  # 写入时戳


def test_edit_item_updates_selling_price(tmp_path, monkeypatch):
    """POST /items/<id>/edit 改 selling_price → DB 更新 + 时间戳。"""
    import db as db_module
    import config as config_module
    from db import init_master_db, init_warehouse_db
    master_path = tmp_path / "master.db"
    wh_path = tmp_path / "wh.db"
    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", tmp_path)
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", tmp_path)
    init_master_db()
    init_warehouse_db(wh_path)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    import sqlite3
    m = sqlite3.connect(master_path)
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (1, 'admin', 'x', 1, ?)", (ts,))
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, created_at) "
        "VALUES (1, 'wh_t', 'T', ?, ?)", (str(wh_path), ts))
    m.execute(
        "INSERT INTO warehouse_users (user_id, warehouse_id, role) "
        "VALUES (1, 1, 'admin')")
    m.commit()
    m.close()

    # Seed an item
    conn = sqlite3.connect(wh_path)
    conn.row_factory = sqlite3.Row
    cat_id = conn.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()["id"]
    conn.execute(
        "INSERT INTO items (sku, name, category_id, quantity, unit_cost, "
        "selling_price, selling_price_updated_at, unit, gram_per_unit, aux_rate, "
        "aux_unit, updated_at) "
        "VALUES ('X-1', '糖', ?, 10, 5, 10, ?, '件', 0, 0, NULL, ?)",
        (cat_id, ts, ts))
    item_id = conn.execute("SELECT id FROM items WHERE name='糖'").fetchone()["id"]
    conn.commit()
    conn.close()

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = 1
        s["warehouse_id"] = 1

    resp = client.post(f"/items/{item_id}/edit", data={
        "name": "糖",
        "category_id": str(cat_id),
        "safety_stock": "0",
        "unit_cost": "5",
        "selling_price": "20.00",
        "unit": "件",
        "aux_unit": "",
        "aux_rate": "0",
    }, follow_redirects=True)
    assert resp.status_code == 200

    row = sqlite3.connect(wh_path).execute(
        "SELECT selling_price, selling_price_updated_at FROM items WHERE id=?",
        (item_id,),
    ).fetchone()
    assert float(row["selling_price"]) == 20.00
    assert row["selling_price_updated_at"] is not None
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest tests/test_recipe_cost_route.py -v
```

Expected: 2 failed（selling_price 字段未读取）。

- [ ] **Step 3: 改 `blueprints/items.py` — POST/GET 增加 selling_price**

打开 `blueprints/items.py`，在 `items_list` 路由的 POST 处理（`if request.method == "POST":`）中，在 `unit_cost = ...` 一行后追加：

```python
        selling_price = float(request.form.get("selling_price", "0") or 0)
```

修改 `INSERT INTO items` 的 SQL 和参数列表：

替换原 SQL：

```python
            db.execute(
                """INSERT INTO items
                   (sku, name, category_id, quantity, safety_stock, unit_cost,
                    unit, gram_per_unit, aux_unit, aux_rate, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (gen_sku(), name, int(category_id), quantity, safety_stock,
                 unit_cost, unit, gram_per_unit, aux_unit, aux_rate, now()),
            )
```

为：

```python
            db.execute(
                """INSERT INTO items
                   (sku, name, category_id, quantity, safety_stock, unit_cost,
                    selling_price, selling_price_updated_at,
                    unit, gram_per_unit, aux_unit, aux_rate, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (gen_sku(), name, int(category_id), quantity, safety_stock,
                 unit_cost, selling_price, now() if selling_price > 0 else None,
                 unit, gram_per_unit, aux_unit, aux_rate, now()),
            )
```

把负数校验（`if unit_cost < 0:` 块）后追加：

```python
        if selling_price < 0:
            flash("销售单价不能为负数")
            return redirect(url_for("items.items_list"))
```

- [ ] **Step 4: 改 `blueprints/items.py` — `edit_item` 路由的 POST 处理**

在 `edit_item` 函数 POST 块中 `unit_cost = float(...)` 一行后追加：

```python
        selling_price = float(request.form.get("selling_price", "0") or 0)
```

替换原 UPDATE SQL：

```python
        db.execute(
            """UPDATE items SET name=?, category_id=?, safety_stock=?,
               unit_cost=?, unit=?, gram_per_unit=?,
               aux_unit=?, aux_rate=?, updated_at=? WHERE id=?""",
            (name, int(category_id), safety_stock, unit_cost, unit,
             gram_per_unit, aux_unit, aux_rate, now(), item_id),
        )
```

为：

```python
        old = db.execute(
            "SELECT selling_price FROM items WHERE id=?", (item_id,)
        ).fetchone()
        old_sp = float((old or {}).get("selling_price") or 0)
        sp_updated_at = now() if selling_price != old_sp else None
        db.execute(
            """UPDATE items SET name=?, category_id=?, safety_stock=?,
               unit_cost=?, selling_price=?, selling_price_updated_at=?,
               unit=?, gram_per_unit=?, aux_unit=?, aux_rate=?,
               updated_at=? WHERE id=?""",
            (name, int(category_id), safety_stock, unit_cost,
             selling_price, sp_updated_at,
             unit, gram_per_unit, aux_unit, aux_rate,
             now(), item_id),
        )
```

把负数校验块后追加：

```python
        if selling_price < 0:
            flash("销售单价不能为负数")
            return redirect(url_for("items.edit_item", item_id=item_id))
```

- [ ] **Step 5: 改 `templates/items.html` 加 "销售单价" 输入框**

打开 `templates/items.html`，定位到 `进货单价（¥）` 那个 `<label>` 块（"cols-3" grid 内），在其后追加：

```html
  <label>
    <span>销售单价（¥）</span>
    <input name="selling_price" type="number" min="0" step="0.001" value="0" />
  </label>
```

并在表格 header（`<th>单价（¥）</th>`）后追加一列：

```html
      <th>销售单价（¥）</th>
```

在 `{% for i in items %}` 的 `<td>` 块（`"%.3f"|format(i.unit_cost)` 那个 `<td>`）后追加：

```html
      <td>{{ "%.3f"|format(i.selling_price) if i.selling_price else "-" }}</td>
```

- [ ] **Step 6: 改 `templates/edit_item.html` 加 "销售单价" 输入框**

打开 `templates/edit_item.html`，定位到 `unit_cost` 输入框那个 `<label>` 块，在其后追加：

```html
  <label>销售单价（¥）
    <input name="selling_price" type="number" min="0" step="0.001"
           value="{{ "%.3f"|format(item['selling_price']) if item['selling_price'] else '0' }}" />
  </label>
```

- [ ] **Step 7: 跑测试确认通过**

```bash
pytest tests/test_recipe_cost_route.py -v
```

Expected: 2 passed.

- [ ] **Step 8: 跑全部测试确保无回归**

```bash
pytest -q
```

Expected: 全绿（旧的 items 测试也应该通过，因为新字段 default 0；老测试不读 selling_price 不影响）。

- [ ] **Step 9: Commit**

```bash
git add blueprints/items.py templates/items.html templates/edit_item.html tests/test_recipe_cost_route.py
git commit -m "feat(recipe-cost): items.selling_price CRUD + forms"
```

---

## Task 5: 配方模块 Blueprint 占位 + nav 入口

**Files:**
- Create: `blueprints/recipe_cost.py`（仅占位路由）
- Modify: `app.py`（注册蓝图）
- Modify: `templates/base.html`（sidebar + mobile nav 加 "配方成本" 链接）

- [ ] **Step 1: 创建 `blueprints/recipe_cost.py`（仅入口路由）**

```python
"""Recipe cost: ice-cream recipes + serving recipes with cost/margin."""
from __future__ import annotations

from flask import Blueprint, redirect, url_for

from permissions import require_login


bp = Blueprint("recipe_cost", __name__)


@bp.route("/recipe-cost/")
@require_login
def landing():
    """入口：跳到冰激凌配方列表。"""
    return redirect(url_for("recipe_cost.ic_recipes_list"))
```

- [ ] **Step 2: 在 `app.py` 注册蓝图**

打开 `app.py`，在 `from blueprints.production import bp as production_bp` 后追加：

```python
    from blueprints.recipe_cost import bp as recipe_cost_bp
```

在 `app.register_blueprint(production_bp)` 后追加：

```python
    app.register_blueprint(recipe_cost_bp)
```

- [ ] **Step 3: 在 `templates/base.html` sidebar 加链接**

打开 `templates/base.html`，定位到 sidebar 中"生产录入"链接 `<a href="{{ url_for('production.products_list') }}">生产录入</a>` 后追加：

```html
      <a href="{{ url_for('recipe_cost.landing') }}">配方成本</a>
```

- [ ] **Step 4: 在 `templates/base.html` mobile nav 加链接**

定位到 mobile nav 中"生产"链接 `<a href="{{ url_for('production.products_list') }}">生产</a>` 后追加：

```html
      <a href="{{ url_for('recipe_cost.landing') }}">配方</a>
```

- [ ] **Step 5: 启动验证路由通**

```bash
flask --app app run --host 0.0.0.0 --port 5001 &
sleep 2
curl -sL -o /dev/null -w "%{http_code}\n" http://localhost:5001/recipe-cost/
kill %1
```

Expected: 302（redirect 到 `/recipe-cost/ic-recipes`，该路由还没注册 → 404，OK；sidebar 链接可点）。如需更细，可加 print debug 但非必要。

- [ ] **Step 6: Commit**

```bash
git add blueprints/recipe_cost.py app.py templates/base.html
git commit -m "feat(recipe-cost): blueprint skeleton + nav entry"
```

---

## Task 6: 冰激凌配方 CRUD（list / new / edit / delete）

**Files:**
- Modify: `blueprints/recipe_cost.py`（追加 4 个路由）
- Create: `templates/recipe_cost/base_recipe.html`
- Create: `templates/recipe_cost/ic_recipes.html`
- Create: `templates/recipe_cost/ic_recipe_edit.html`

- [ ] **Step 1: 写失败测试（追加到 `tests/test_recipe_cost_route.py`）**

打开 `tests/test_recipe_cost_route.py`，在末尾追加：

```python
def test_ic_recipe_crud(tmp_path, monkeypatch):
    """冰激凌配方 CRUD 完整链路。"""
    import db as db_module
    import config as config_module
    from db import init_master_db, init_warehouse_db
    master_path = tmp_path / "master.db"
    wh_path = tmp_path / "wh.db"
    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", tmp_path)
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", tmp_path)
    init_master_db()
    init_warehouse_db(wh_path)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    import sqlite3
    m = sqlite3.connect(master_path)
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (1, 'admin', 'x', 1, ?)", (ts,))
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, created_at) "
        "VALUES (1, 'wh_t', 'T', ?, ?)", (str(wh_path), ts))
    m.execute(
        "INSERT INTO warehouse_users (user_id, warehouse_id, role) "
        "VALUES (1, 1, 'admin')")
    m.commit()
    m.close()

    # Seed one item
    conn = sqlite3.connect(wh_path)
    conn.row_factory = sqlite3.Row
    cat_id = conn.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()["id"]
    conn.execute(
        "INSERT INTO items (sku, name, category_id, quantity, unit_cost, "
        "selling_price, unit, gram_per_unit, aux_rate, aux_unit, updated_at) "
        "VALUES ('X-1', '糖', ?, 10, 5, 10, '件', 50, 50, '克', ?)",
        (cat_id, ts))
    item_id = conn.execute("SELECT id FROM items WHERE name='糖'").fetchone()["id"]
    conn.commit()
    conn.close()

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = 1
        s["warehouse_id"] = 1

    # 1. GET /recipe-cost/ic-recipes 渲染空列表
    resp = client.get("/recipe-cost/ic-recipes")
    assert resp.status_code == 200
    assert b"\xe6\x98\x87\xe5\x87\xba\xe5\x88\xb6\xe6\x96\xb9" in resp.data or b"ic_recipes" in resp.data

    # 2. POST /recipe-cost/ic-recipes/new → 创建
    resp = client.post("/recipe-cost/ic-recipes/new", data={
        "name": "香草冰淇淋",
        "note": "测试",
        "output_unit": "g",
        "output_qty": "100",
        "sale_price": "25",
    }, follow_redirects=True)
    assert resp.status_code == 200
    ic_id = sqlite3.connect(wh_path).execute(
        "SELECT id FROM ic_recipes WHERE name='香草冰淇淋'"
    ).fetchone()["id"]

    # 3. POST /recipe-cost/ic-recipes/<id>/edit 加 1 行原料
    resp = client.post(f"/recipe-cost/ic-recipes/{ic_id}/edit", data={
        "name": "香草冰淇淋",
        "note": "测试",
        "output_unit": "g",
        "output_qty": "100",
        "sale_price": "25",
        "bom_row_id": [""],
        "bom_item_id": [str(item_id)],
        "bom_qty": ["60"],
        "bom_delete": [""],
    }, follow_redirects=True)
    assert resp.status_code == 200

    row = sqlite3.connect(wh_path).execute(
        "SELECT qty_per_unit FROM ic_recipe_items WHERE ic_recipe_id=?",
        (ic_id,),
    ).fetchone()
    assert float(row["qty_per_unit"]) == 60.0

    # 4. POST /recipe-cost/ic-recipes/<id>/delete → 删除
    resp = client.post(f"/recipe-cost/ic-recipes/{ic_id}/delete", follow_redirects=True)
    assert resp.status_code == 200
    cnt = sqlite3.connect(wh_path).execute(
        "SELECT COUNT(*) AS c FROM ic_recipes WHERE id=?", (ic_id,)
    ).fetchone()["c"]
    assert cnt == 0


def test_ic_recipe_delete_blocked_when_referenced(tmp_path, monkeypatch):
    """冰激凌配方被 recipe_items 引用时禁止删除。"""
    import db as db_module
    import config as config_module
    from db import init_master_db, init_warehouse_db
    master_path = tmp_path / "master.db"
    wh_path = tmp_path / "wh.db"
    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", tmp_path)
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", tmp_path)
    init_master_db()
    init_warehouse_db(wh_path)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    import sqlite3
    m = sqlite3.connect(master_path)
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (1, 'admin', 'x', 1, ?)", (ts,))
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, created_at) "
        "VALUES (1, 'wh_t', 'T', ?, ?)", (str(wh_path), ts))
    m.execute(
        "INSERT INTO warehouse_users (user_id, warehouse_id, role) "
        "VALUES (1, 1, 'admin')")
    m.commit()
    m.close()

    conn = sqlite3.connect(wh_path)
    conn.execute(
        "INSERT INTO ic_recipes (name, output_unit, output_qty, sale_price, "
        "created_at, updated_at) VALUES ('香草冰淇淋', 'g', 100, 25, ?, ?)",
        (ts, ts))
    ic_id = conn.execute("SELECT id FROM ic_recipes WHERE name='香草冰淇淋'").fetchone()["id"]
    conn.execute(
        "INSERT INTO recipes (name, output_unit, output_qty, sale_price, "
        "created_at, updated_at) VALUES ('柠檬茶', '杯', 1, 15, ?, ?)",
        (ts, ts))
    recipe_id = conn.execute("SELECT id FROM recipes WHERE name='柠檬茶'").fetchone()["id"]
    conn.execute(
        "INSERT INTO recipe_items (recipe_id, source_type, ic_recipe_id, qty_per_unit) "
        "VALUES (?, 'ic_recipe', ?, 50)",
        (recipe_id, ic_id),
    )
    conn.commit()
    conn.close()

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = 1
        s["warehouse_id"] = 1

    resp = client.post(f"/recipe-cost/ic-recipes/{ic_id}/delete", follow_redirects=True)
    assert resp.status_code == 200
    # 应仍在
    cnt = sqlite3.connect(wh_path).execute(
        "SELECT COUNT(*) AS c FROM ic_recipes WHERE id=?", (ic_id,)
    ).fetchone()["c"]
    assert cnt == 1
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest tests/test_recipe_cost_route.py -v
```

Expected: test_ic_recipe_crud 与 test_ic_recipe_delete_blocked_when_referenced 都 404。

- [ ] **Step 3: 扩 `blueprints/recipe_cost.py` — 加 4 个路由**

替换 `blueprints/recipe_cost.py`：

```python
"""Recipe cost: ice-cream recipes + serving recipes with cost/margin."""
from __future__ import annotations

import sqlite3
from typing import Optional

from flask import Blueprint, flash, redirect, render_template, request, url_for

from db import get_warehouse_db
from permissions import require_login, require_platform_admin
from blueprints._helpers import now, parse_qty
from blueprints.auth import audit


bp = Blueprint("recipe_cost", __name__)


@bp.route("/recipe-cost/")
@require_login
def landing():
    """入口：跳到冰激凌配方列表。"""
    return redirect(url_for("recipe_cost.ic_recipes_list"))


# ---------------------------------------------------------------------------
# 冰激凌配方 (ic_recipes)
# ---------------------------------------------------------------------------

def _load_items_for_bom() -> list:
    """Return items for the BOM item picker."""
    db = get_warehouse_db()
    return db.execute(
        """SELECT i.id, i.name, i.unit, i.gram_per_unit, i.aux_rate, i.aux_unit,
                  i.unit_cost, i.selling_price, c.name AS category_name
           FROM items i JOIN categories c ON c.id = i.category_id
           ORDER BY c.name, i.name"""
    ).fetchall()


@bp.route("/recipe-cost/ic-recipes", methods=["GET"])
@require_login
def ic_recipes_list():
    db = get_warehouse_db()
    from blueprints.recipe_cost_pure import ic_recipe_cost
    rows = db.execute(
        "SELECT id, name, output_unit, output_qty, sale_price, created_at "
        "FROM ic_recipes ORDER BY id DESC"
    ).fetchall()
    enriched = []
    for r in rows:
        c = ic_recipe_cost(db, int(r["id"]))
        enriched.append({
            **dict(r),
            "cost_purchase": float(c["cost_purchase"]),
            "cost_selling": float(c["cost_selling"]),
            "margin_purchase": c["margin_purchase"],
            "item_count": len(c["lines"]),
        })
    return render_template(
        "recipe_cost/ic_recipes.html",
        ic_recipes=enriched,
    )


@bp.route("/recipe-cost/ic-recipes/new", methods=["GET", "POST"])
@require_platform_admin
def ic_recipe_new():
    if request.method == "POST":
        return _save_ic_recipe(None)
    return render_template(
        "recipe_cost/ic_recipe_edit.html",
        recipe=None,
        bom_rows=[],
        items=_load_items_for_bom(),
    )


@bp.route("/recipe-cost/ic-recipes/<int:ic_recipe_id>/edit", methods=["GET", "POST"])
@require_platform_admin
def ic_recipe_edit(ic_recipe_id: int):
    if request.method == "POST":
        return _save_ic_recipe(ic_recipe_id)
    db = get_warehouse_db()
    recipe = db.execute(
        "SELECT * FROM ic_recipes WHERE id = ?", (ic_recipe_id,)
    ).fetchone()
    if recipe is None:
        flash("冰激凌配方不存在")
        return redirect(url_for("recipe_cost.ic_recipes_list"))
    bom_rows = db.execute(
        """SELECT ri.*, i.name AS item_name, i.unit AS item_unit,
                  i.gram_per_unit, i.aux_rate, i.aux_unit,
                  i.unit_cost, i.selling_price
           FROM ic_recipe_items ri JOIN items i ON i.id = ri.item_id
           WHERE ri.ic_recipe_id = ? ORDER BY ri.id""",
        (ic_recipe_id,),
    ).fetchall()
    return render_template(
        "recipe_cost/ic_recipe_edit.html",
        recipe=recipe,
        bom_rows=bom_rows,
        items=_load_items_for_bom(),
    )


@bp.route("/recipe-cost/ic-recipes/<int:ic_recipe_id>/delete", methods=["POST"])
@require_platform_admin
def ic_recipe_delete(ic_recipe_id: int):
    db = get_warehouse_db()
    used = db.execute(
        "SELECT COUNT(*) AS c FROM recipe_items "
        "WHERE source_type='ic_recipe' AND ic_recipe_id=?",
        (ic_recipe_id,),
    ).fetchone()["c"]
    if used > 0:
        flash(f"该冰激凌配方被 {used} 个出品配方引用，无法删除")
        return redirect(url_for("recipe_cost.ic_recipes_list"))
    db.execute("DELETE FROM ic_recipes WHERE id = ?", (ic_recipe_id,))
    db.commit()
    audit("recipe_cost.ic_recipe.delete", "ic_recipe", ic_recipe_id)
    flash("冰激凌配方已删除")
    return redirect(url_for("recipe_cost.ic_recipes_list"))


def _save_ic_recipe(ic_recipe_id: Optional[int]):
    """Create or update an ic_recipe + its BOM rows.

    Form fields: name / note / output_unit / output_qty / sale_price /
                 bom_row_id[] / bom_item_id[] / bom_qty[] / bom_delete[]
    """
    name = request.form.get("name", "").strip()
    note = request.form.get("note", "").strip()
    output_unit = request.form.get("output_unit", "g").strip() or "g"
    output_qty = parse_qty(request.form.get("output_qty", "100"))
    sale_price = float(request.form.get("sale_price", "0") or 0)

    if not name:
        flash("配方名称为必填")
        if ic_recipe_id:
            return redirect(url_for("recipe_cost.ic_recipe_edit", ic_recipe_id=ic_recipe_id))
        return redirect(url_for("recipe_cost.ic_recipe_new"))
    if sale_price < 0:
        flash("售价不能为负")

    db = get_warehouse_db()
    if ic_recipe_id is None:
        try:
            db.execute(
                """INSERT INTO ic_recipes
                   (name, note, output_unit, output_qty, sale_price,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (name, note, output_unit, output_qty, sale_price, now(), now()),
            )
            new_id = int(db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
            db.commit()
            audit("recipe_cost.ic_recipe.create", "ic_recipe", new_id, {"name": name})
            return redirect(url_for("recipe_cost.ic_recipe_edit", ic_recipe_id=new_id))
        except sqlite3.IntegrityError:
            flash("配方名称已存在")
            return redirect(url_for("recipe_cost.ic_recipe_new"))
    else:
        db.execute(
            """UPDATE ic_recipes SET name=?, note=?, output_unit=?,
               output_qty=?, sale_price=?, updated_at=? WHERE id=?""",
            (name, note, output_unit, output_qty, sale_price, now(), ic_recipe_id),
        )

    # BOM rows
    bom_ids = request.form.getlist("bom_row_id")
    item_ids = request.form.getlist("bom_item_id")
    qtys = request.form.getlist("bom_qty")
    deletes = request.form.getlist("bom_delete")
    added = removed = updated = 0
    for i in range(len(bom_ids)):
        row_id = bom_ids[i].strip()
        if i < len(deletes) and deletes[i] == "1":
            if row_id:
                db.execute("DELETE FROM ic_recipe_items WHERE id = ?", (int(row_id),))
                removed += 1
            continue
        item_id = item_ids[i].strip() if i < len(item_ids) else ""
        qty = parse_qty(qtys[i]) if i < len(qtys) else 0.0
        if not item_id or qty <= 0:
            continue
        if row_id:
            db.execute(
                "UPDATE ic_recipe_items SET item_id=?, qty_per_unit=? WHERE id=?",
                (int(item_id), qty, int(row_id)),
            )
            updated += 1
        else:
            try:
                db.execute(
                    """INSERT INTO ic_recipe_items
                       (ic_recipe_id, item_id, qty_per_unit) VALUES (?, ?, ?)""",
                    (ic_recipe_id, int(item_id), qty),
                )
                added += 1
            except sqlite3.IntegrityError:
                flash(f"第 {i+1} 行原料重复")

    db.commit()
    audit("recipe_cost.ic_recipe.update", "ic_recipe", ic_recipe_id, {
        "added": added, "removed": removed, "updated": updated,
    })
    flash("冰激凌配方已保存")
    return redirect(url_for("recipe_cost.ic_recipe_edit", ic_recipe_id=ic_recipe_id))
```

- [ ] **Step 4: 创建 `templates/recipe_cost/base_recipe.html`**

```html
{% extends "base.html" %}
{% block sidebar %}{% endblock %}
{% block content %}
{% block recipe_content %}{% endblock %}
{% endblock %}
```

- [ ] **Step 5: 创建 `templates/recipe_cost/ic_recipes.html`**

```html
{% extends "recipe_cost/base_recipe.html" %}
{% block recipe_content %}
<div class="list-head">
  <h2>冰激凌配方</h2>
  <a class="btn" href="{{ url_for('core.land') }}">← 返回落地页</a>
  <a class="btn" href="{{ url_for('recipe_cost.recipes_list') }}">出品配方 →</a>
  {% if g.user['is_admin'] %}
  <a class="btn btn-primary" href="{{ url_for('recipe_cost.ic_recipe_new') }}">+ 新建冰激凌配方</a>
  {% endif %}
</div>

{% if ic_recipes %}
  <div class="card-list card-list-3">
    {% for r in ic_recipes %}
    <div class="card">
      <div class="card-main">
        <h3>{{ r['name'] }}</h3>
        <p class="muted">{{ r['output_qty'] | fmt_qty }} {{ r['output_unit'] }} / 份 · 配方项数 {{ r['item_count'] }}</p>
        <p>采购成本：<strong>{{ r['cost_purchase'] | fmt_money }}</strong></p>
        <p>销售价值：<strong>{{ r['cost_selling'] | fmt_money }}</strong></p>
        <p>售价 {{ r['sale_price'] | fmt_money }} · 毛利率
          {% if r['margin_purchase'] is not none %}
            <strong>{{ '%.1f' | format(r['margin_purchase'] * 100) }}%</strong>
          {% else %}—{% endif %}
        </p>
      </div>
      {% if g.user['is_admin'] %}
      <div class="card-actions">
        <a class="btn-sm" href="{{ url_for('recipe_cost.ic_recipe_edit', ic_recipe_id=r['id']) }}">编辑</a>
        <form method="post" action="{{ url_for('recipe_cost.ic_recipe_delete', ic_recipe_id=r['id']) }}" onsubmit="return confirm('确认删除该冰激凌配方？');">
          <button class="btn-sm btn-delete" type="submit">删除</button>
        </form>
      </div>
      {% endif %}
    </div>
    {% endfor %}
  </div>
{% else %}
  <p class="muted">暂无冰激凌配方{% if g.user['is_admin'] %}，点击右上"新建冰激凌配方"开始{% endif %}。</p>
{% endif %}
{% endblock %}
```

- [ ] **Step 6: 创建 `templates/recipe_cost/ic_recipe_edit.html`**

（基础信息 + BOM 表 + 滑块 + JS 实时重算。Step 7 加最小版即可，JS 滑块在 Task 8 完善。）

```html
{% extends "recipe_cost/base_recipe.html" %}
{% block recipe_content %}
<div class="list-head">
  <h2>{% if recipe %}编辑冰激凌配方{% else %}新建冰激凌配方{% endif %}</h2>
  <a class="btn" href="{{ url_for('recipe_cost.ic_recipes_list') }}">← 返回列表</a>
</div>

<form method="post" class="form-block" id="ic-recipe-form">
  <div class="grid cols-3">
    <label>配方名称 <input name="name" required value="{{ recipe['name'] if recipe else '' }}" /></label>
    <label>输出单位
      <select name="output_unit">
        <option value="g" {% if recipe and recipe['output_unit']=='g' %}selected{% endif %}>g（克）</option>
        <option value="ml" {% if recipe and recipe['output_unit']=='ml' %}selected{% endif %}>ml（毫升）</option>
        <option value="件" {% if recipe and recipe['output_unit']=='件' %}selected{% endif %}>件</option>
      </select>
    </label>
    <label>每份输出量 <input name="output_qty" type="number" min="0" step="0.01"
      value="{{ recipe['output_qty'] if recipe else 100 }}" /></label>
    <label>售价（¥） <input name="sale_price" id="sale-price" type="number" min="0" step="0.01"
      value="{{ recipe['sale_price'] if recipe else 0 }}" /></label>
    <label>备注 <input name="note" value="{{ recipe['note'] or '' if recipe else '' }}" /></label>
  </div>
  <button type="submit" class="btn-primary">保存配方</button>
</form>

<hr/>
<h3>原料配方</h3>
<p class="muted">每份冰激凌的原料用量（单位与品项一致：克或库存单位）。</p>

<form method="post" id="bom-form">
  <input type="hidden" name="name" value="{{ recipe['name'] if recipe else '' }}" />
  <input type="hidden" name="note" value="{{ recipe['note'] or '' if recipe else '' }}" />
  <input type="hidden" name="output_unit" value="{{ recipe['output_unit'] if recipe else 'g' }}" />
  <input type="hidden" name="output_qty" value="{{ recipe['output_qty'] if recipe else 100 }}" />
  <input type="hidden" name="sale_price" id="bom-sale-price"
         value="{{ recipe['sale_price'] if recipe else 0 }}" />

  <div class="form-block" style="margin: 0.5em 0;">
    <label>
      <input type="checkbox" id="use-temp-prices" />
      使用临时售价（用于探索毛利率）
    </label>
  </div>

  <table class="bom-table">
    <thead>
      <tr>
        <th>原料</th><th>每份用量</th><th>单位</th>
        <th>采购单价</th><th>采购小计</th>
        <th>销售单价</th><th>销售小计</th>
        <th>临时售价</th><th>操作</th>
      </tr>
    </thead>
    <tbody id="bom-body">
      {% for b in bom_rows %}
      <tr class="bom-row"
          data-item-id="{{ b['item_id'] }}"
          data-unit-cost="{{ b['unit_cost'] }}"
          data-selling-price="{{ b['selling_price'] or 0 }}"
          data-gram-per-unit="{{ b['gram_per_unit'] or 0 }}"
          data-aux-rate="{{ b['aux_rate'] or 0 }}">
        <td>
          <input type="hidden" name="bom_row_id" value="{{ b['id'] }}" />
          <select name="bom_item_id" class="bom-item-sel">
            {% for it in items %}
            <option value="{{ it['id'] }}"
                    data-gram="{{ it['gram_per_unit'] or 0 }}"
                    data-unit="{{ it['unit'] }}"
                    data-uc="{{ it['unit_cost'] or 0 }}"
                    data-sp="{{ it['selling_price'] or 0 }}"
                    {% if it['id']==b['item_id'] %}selected{% endif %}>
              {{ it['category_name'] }} / {{ it['name'] }}
            </option>
            {% endfor %}
          </select>
        </td>
        <td><input type="number" step="0.01" min="0" name="bom_qty" class="bom-qty"
                   value="{{ b['qty_per_unit'] }}" /></td>
        <td><span class="bom-unit-label">{% if b['gram_per_unit'] %}克{% else %}{{ b['item_unit'] }}{% endif %}</span></td>
        <td><span class="bom-uc">{{ '%.3f' | format(b['unit_cost']) }}</span></td>
        <td><span class="bom-cost-purchase">0.00</span></td>
        <td><span class="bom-sp">{{ '%.3f' | format(b['selling_price'] or 0) }}</span></td>
        <td><span class="bom-cost-selling">0.00</span></td>
        <td>
          <input type="range" class="bom-temp-slider" min="0" step="0.01"
                 value="{{ b['selling_price'] or 0 }}" disabled />
          <span class="bom-temp-display">{{ '%.2f' | format(b['selling_price'] or 0) }}</span>
          <button type="button" class="btn-sm bom-save-temp"
                  data-item-id="{{ b['item_id'] }}" disabled>保存为新价</button>
        </td>
        <td>
          <label class="del-flag"><input type="checkbox" name="bom_delete" value="1" /> 删除</label>
        </td>
      </tr>
      {% endfor %}
    </tbody>
    <tfoot>
      <tr>
        <th colspan="4" style="text-align:right;">合计</th>
        <th id="total-purchase">0.00</th>
        <th></th>
        <th id="total-selling">0.00</th>
        <th colspan="2"></th>
      </tr>
      <tr>
        <th colspan="4" style="text-align:right;">售价</th>
        <th colspan="2" id="footer-sale-price">0.00</th>
        <th colspan="3">毛利率：<strong id="footer-margin">—</strong></th>
      </tr>
    </tfoot>
  </table>

  <button type="button" id="bom-add" class="btn-sm">+ 新增一行</button>
  <button type="submit" class="btn-primary">保存配方</button>
</form>

<script src="{{ url_for('static', filename='recipe_cost.js') }}?v=1"></script>
{% endblock %}
```

- [ ] **Step 7: 跑测试确认通过**

```bash
pytest tests/test_recipe_cost_route.py -v
```

Expected: 4 passed（test_create_item_stores_selling_price + test_edit_item_updates_selling_price + test_ic_recipe_crud + test_ic_recipe_delete_blocked_when_referenced）。

- [ ] **Step 8: Commit**

```bash
git add blueprints/recipe_cost.py templates/recipe_cost/ tests/test_recipe_cost_route.py
git commit -m "feat(recipe-cost): ic_recipe CRUD routes + list/edit templates"
```

---

## Task 7: 出品配方 CRUD（list / new / edit / delete，含多态原料）

**Files:**
- Modify: `blueprints/recipe_cost.py`（追加 4 个路由 + 保存函数）
- Create: `templates/recipe_cost/recipes.html`
- Create: `templates/recipe_cost/recipe_edit.html`

- [ ] **Step 1: 写失败测试（追加到 `tests/test_recipe_cost_route.py`）**

打开 `tests/test_recipe_cost_route.py`，在末尾追加：

```python
def test_recipe_crud_with_mixed_sources(tmp_path, monkeypatch):
    """出品配方 CRUD：含 item + ic_recipe 引用。"""
    import db as db_module
    import config as config_module
    from db import init_master_db, init_warehouse_db
    master_path = tmp_path / "master.db"
    wh_path = tmp_path / "wh.db"
    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", tmp_path)
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", tmp_path)
    init_master_db()
    init_warehouse_db(wh_path)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    import sqlite3
    m = sqlite3.connect(master_path)
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (1, 'admin', 'x', 1, ?)", (ts,))
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, created_at) "
        "VALUES (1, 'wh_t', 'T', ?, ?)", (str(wh_path), ts))
    m.execute(
        "INSERT INTO warehouse_users (user_id, warehouse_id, role) "
        "VALUES (1, 1, 'admin')")
    m.commit()
    m.close()

    # Seed item + ic_recipe
    conn = sqlite3.connect(wh_path)
    conn.row_factory = sqlite3.Row
    cat_id = conn.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()["id"]
    conn.execute(
        "INSERT INTO items (sku, name, category_id, quantity, unit_cost, "
        "selling_price, unit, gram_per_unit, aux_rate, aux_unit, updated_at) "
        "VALUES ('X-1', '糖', ?, 10, 5, 10, '件', 50, 50, '克', ?)",
        (cat_id, ts))
    item_id = conn.execute("SELECT id FROM items WHERE name='糖'").fetchone()["id"]
    conn.execute(
        "INSERT INTO ic_recipes (name, output_unit, output_qty, sale_price, "
        "created_at, updated_at) VALUES ('香草冰淇淋', 'g', 100, 25, ?, ?)",
        (ts, ts))
    ic_id = conn.execute("SELECT id FROM ic_recipes WHERE name='香草冰淇淋'").fetchone()["id"]
    conn.commit()
    conn.close()

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = 1
        s["warehouse_id"] = 1

    # 1. POST /recipe-cost/recipes/new → 创建
    resp = client.post("/recipe-cost/recipes/new", data={
        "name": "柠檬茶",
        "note": "test",
        "output_unit": "杯",
        "output_qty": "1",
        "sale_price": "15",
    }, follow_redirects=True)
    assert resp.status_code == 200
    rid = sqlite3.connect(wh_path).execute(
        "SELECT id FROM recipes WHERE name='柠檬茶'"
    ).fetchone()["id"]

    # 2. POST /recipe-cost/recipes/<id>/edit 加 2 行（一 item 一 ic_recipe）
    resp = client.post(f"/recipe-cost/recipes/{rid}/edit", data={
        "name": "柠檬茶",
        "note": "test",
        "output_unit": "杯",
        "output_qty": "1",
        "sale_price": "15",
        "bom_row_id": ["", ""],
        "bom_source_type": ["item", "ic_recipe"],
        "bom_item_id": [str(item_id), ""],
        "bom_ic_recipe_id": ["", str(ic_id)],
        "bom_qty": ["60", "50"],
        "bom_delete": ["", ""],
    }, follow_redirects=True)
    assert resp.status_code == 200

    rows = sqlite3.connect(wh_path).execute(
        "SELECT source_type, item_id, ic_recipe_id, qty_per_unit FROM recipe_items "
        "WHERE recipe_id=? ORDER BY id",
        (rid,),
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["source_type"] == "item"
    assert rows[0]["item_id"] == item_id
    assert float(rows[0]["qty_per_unit"]) == 60.0
    assert rows[1]["source_type"] == "ic_recipe"
    assert rows[1]["ic_recipe_id"] == ic_id
    assert float(rows[1]["qty_per_unit"]) == 50.0

    # 3. POST /recipe-cost/recipes/<id>/delete → 删除（无引用，OK）
    resp = client.post(f"/recipe-cost/recipes/{rid}/delete", follow_redirects=True)
    assert resp.status_code == 200
    cnt = sqlite3.connect(wh_path).execute(
        "SELECT COUNT(*) AS c FROM recipes WHERE id=?", (rid,)
    ).fetchone()["c"]
    assert cnt == 0
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest tests/test_recipe_cost_route.py::test_recipe_crud_with_mixed_sources -v
```

Expected: 404.

- [ ] **Step 3: 扩 `blueprints/recipe_cost.py` — 加 4 个出品路由**

在文件末尾（`_save_ic_recipe` 函数后）追加：

```python
# ---------------------------------------------------------------------------
# 出品配方 (recipes)
# ---------------------------------------------------------------------------

@bp.route("/recipe-cost/recipes", methods=["GET"])
@require_login
def recipes_list():
    db = get_warehouse_db()
    from blueprints.recipe_cost_pure import recipe_cost
    rows = db.execute(
        "SELECT id, name, output_unit, output_qty, sale_price, created_at "
        "FROM recipes ORDER BY id DESC"
    ).fetchall()
    enriched = []
    for r in rows:
        c = recipe_cost(db, int(r["id"]))
        enriched.append({
            **dict(r),
            "cost_purchase": float(c["cost_purchase"]),
            "cost_selling": float(c["cost_selling"]),
            "margin_purchase": c["margin_purchase"],
            "item_count": len(c["lines"]),
        })
    return render_template("recipe_cost/recipes.html", recipes=enriched)


def _load_ic_recipes_for_picker() -> list:
    db = get_warehouse_db()
    return db.execute(
        "SELECT id, name, sale_price, output_unit, output_qty FROM ic_recipes ORDER BY name"
    ).fetchall()


@bp.route("/recipe-cost/recipes/new", methods=["GET", "POST"])
@require_platform_admin
def recipe_new():
    if request.method == "POST":
        return _save_recipe(None)
    return render_template(
        "recipe_cost/recipe_edit.html",
        recipe=None,
        bom_rows=[],
        items=_load_items_for_bom(),
        ic_recipes=_load_ic_recipes_for_picker(),
    )


@bp.route("/recipe-cost/recipes/<int:recipe_id>/edit", methods=["GET", "POST"])
@require_platform_admin
def recipe_edit(recipe_id: int):
    if request.method == "POST":
        return _save_recipe(recipe_id)
    db = get_warehouse_db()
    recipe = db.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    if recipe is None:
        flash("出品配方不存在")
        return redirect(url_for("recipe_cost.recipes_list"))
    bom_rows = db.execute(
        """SELECT ri.*, i.name AS item_name, i.unit AS item_unit,
                  i.gram_per_unit, i.aux_rate, i.aux_unit,
                  i.unit_cost, i.selling_price,
                  ic.name AS ic_recipe_name
           FROM recipe_items ri
           LEFT JOIN items i ON i.id = ri.item_id
           LEFT JOIN ic_recipes ic ON ic.id = ri.ic_recipe_id
           WHERE ri.recipe_id = ? ORDER BY ri.id""",
        (recipe_id,),
    ).fetchall()
    return render_template(
        "recipe_cost/recipe_edit.html",
        recipe=recipe,
        bom_rows=bom_rows,
        items=_load_items_for_bom(),
        ic_recipes=_load_ic_recipes_for_picker(),
    )


@bp.route("/recipe-cost/recipes/<int:recipe_id>/delete", methods=["POST"])
@require_platform_admin
def recipe_delete(recipe_id: int):
    db = get_warehouse_db()
    db.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
    db.commit()
    audit("recipe_cost.recipe.delete", "recipe", recipe_id)
    flash("出品配方已删除")
    return redirect(url_for("recipe_cost.recipes_list"))


def _save_recipe(recipe_id):
    name = request.form.get("name", "").strip()
    note = request.form.get("note", "").strip()
    output_unit = request.form.get("output_unit", "件").strip() or "件"
    output_qty = parse_qty(request.form.get("output_qty", "1"))
    sale_price = float(request.form.get("sale_price", "0") or 0)

    if not name:
        flash("配方名称为必填")
        if recipe_id:
            return redirect(url_for("recipe_cost.recipe_edit", recipe_id=recipe_id))
        return redirect(url_for("recipe_cost.recipe_new"))
    if sale_price < 0:
        flash("售价不能为负")

    db = get_warehouse_db()
    if recipe_id is None:
        try:
            db.execute(
                """INSERT INTO recipes
                   (name, note, output_unit, output_qty, sale_price,
                    sale_price_updated_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (name, note, output_unit, output_qty, sale_price, now(), now(), now()),
            )
            new_id = int(db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
            db.commit()
            audit("recipe_cost.recipe.create", "recipe", new_id, {"name": name})
            return redirect(url_for("recipe_cost.recipe_edit", recipe_id=new_id))
        except sqlite3.IntegrityError:
            flash("配方名称已存在")
            return redirect(url_for("recipe_cost.recipe_new"))
    else:
        old = db.execute(
            "SELECT sale_price FROM recipes WHERE id=?", (recipe_id,)
        ).fetchone()
        old_sp = float((old or {}).get("sale_price") or 0)
        sp_updated_at = now() if sale_price != old_sp else None
        db.execute(
            """UPDATE recipes SET name=?, note=?, output_unit=?,
               output_qty=?, sale_price=?, sale_price_updated_at=?,
               updated_at=? WHERE id=?""",
            (name, note, output_unit, output_qty, sale_price, sp_updated_at,
             now(), recipe_id),
        )

    # BOM rows (多态)
    bom_ids = request.form.getlist("bom_row_id")
    source_types = request.form.getlist("bom_source_type")
    item_ids = request.form.getlist("bom_item_id")
    ic_ids = request.form.getlist("bom_ic_recipe_id")
    qtys = request.form.getlist("bom_qty")
    deletes = request.form.getlist("bom_delete")
    added = removed = updated = 0
    for i in range(len(bom_ids)):
        row_id = bom_ids[i].strip()
        if i < len(deletes) and deletes[i] == "1":
            if row_id:
                db.execute("DELETE FROM recipe_items WHERE id = ?", (int(row_id),))
                removed += 1
            continue
        st = source_types[i].strip() if i < len(source_types) else "item"
        qty = parse_qty(qtys[i]) if i < len(qtys) else 0.0
        if qty <= 0:
            continue
        item_id = item_ids[i].strip() if i < len(item_ids) else ""
        ic_id = ic_ids[i].strip() if i < len(ic_ids) else ""
        if st == "item":
            if not item_id:
                continue
            if row_id:
                db.execute(
                    """UPDATE recipe_items SET source_type='item',
                       item_id=?, ic_recipe_id=NULL, qty_per_unit=? WHERE id=?""",
                    (int(item_id), qty, int(row_id)),
                )
                updated += 1
            else:
                db.execute(
                    """INSERT INTO recipe_items
                       (recipe_id, source_type, item_id, ic_recipe_id, qty_per_unit)
                       VALUES (?, 'item', ?, NULL, ?)""",
                    (recipe_id, int(item_id), qty),
                )
                added += 1
        elif st == "ic_recipe":
            if not ic_id:
                continue
            if row_id:
                db.execute(
                    """UPDATE recipe_items SET source_type='ic_recipe',
                       item_id=NULL, ic_recipe_id=?, qty_per_unit=? WHERE id=?""",
                    (int(ic_id), qty, int(row_id)),
                )
                updated += 1
            else:
                db.execute(
                    """INSERT INTO recipe_items
                       (recipe_id, source_type, item_id, ic_recipe_id, qty_per_unit)
                       VALUES (?, 'ic_recipe', NULL, ?, ?)""",
                    (recipe_id, int(ic_id), qty),
                )
                added += 1

    db.commit()
    audit("recipe_cost.recipe.update", "recipe", recipe_id, {
        "added": added, "removed": removed, "updated": updated,
    })
    flash("出品配方已保存")
    return redirect(url_for("recipe_cost.recipe_edit", recipe_id=recipe_id))
```

- [ ] **Step 4: 创建 `templates/recipe_cost/recipes.html`**

```html
{% extends "recipe_cost/base_recipe.html" %}
{% block recipe_content %}
<div class="list-head">
  <h2>出品配方</h2>
  <a class="btn" href="{{ url_for('recipe_cost.ic_recipes_list') }}">← 冰激凌配方</a>
  {% if g.user['is_admin'] %}
  <a class="btn btn-primary" href="{{ url_for('recipe_cost.recipe_new') }}">+ 新建出品配方</a>
  {% endif %}
</div>

{% if recipes %}
  <div class="card-list card-list-3">
    {% for r in recipes %}
    <div class="card">
      <div class="card-main">
        <h3>{{ r['name'] }}</h3>
        <p class="muted">{{ r['output_qty'] | fmt_qty }} {{ r['output_unit'] }} / 份 · 配方项数 {{ r['item_count'] }}</p>
        <p>采购成本：<strong>{{ r['cost_purchase'] | fmt_money }}</strong></p>
        <p>销售价值：<strong>{{ r['cost_selling'] | fmt_money }}</strong></p>
        <p>售价 {{ r['sale_price'] | fmt_money }} · 毛利率
          {% if r['margin_purchase'] is not none %}
            <strong>{{ '%.1f' | format(r['margin_purchase'] * 100) }}%</strong>
          {% else %}—{% endif %}
        </p>
      </div>
      {% if g.user['is_admin'] %}
      <div class="card-actions">
        <a class="btn-sm" href="{{ url_for('recipe_cost.recipe_edit', recipe_id=r['id']) }}">编辑</a>
        <form method="post" action="{{ url_for('recipe_cost.recipe_delete', recipe_id=r['id']) }}" onsubmit="return confirm('确认删除该出品配方？');">
          <button class="btn-sm btn-delete" type="submit">删除</button>
        </form>
      </div>
      {% endif %}
    </div>
    {% endfor %}
  </div>
{% else %}
  <p class="muted">暂无出品配方{% if g.user['is_admin'] %}，点击右上"新建出品配方"开始{% endif %}。</p>
{% endif %}
{% endblock %}
```

- [ ] **Step 5: 创建 `templates/recipe_cost/recipe_edit.html`**

（多态原料：每行 source_type select 切换联动 item / ic_recipe 选择器）

```html
{% extends "recipe_cost/base_recipe.html" %}
{% block recipe_content %}
<div class="list-head">
  <h2>{% if recipe %}编辑出品配方{% else %}新建出品配方{% endif %}</h2>
  <a class="btn" href="{{ url_for('recipe_cost.recipes_list') }}">← 返回列表</a>
</div>

<form method="post" class="form-block">
  <div class="grid cols-3">
    <label>配方名称 <input name="name" required value="{{ recipe['name'] if recipe else '' }}" /></label>
    <label>输出单位 <input name="output_unit" value="{{ recipe['output_unit'] if recipe else '件' }}" /></label>
    <label>每份输出量 <input name="output_qty" type="number" min="0" step="0.01"
      value="{{ recipe['output_qty'] if recipe else 1 }}" /></label>
    <label>售价（¥） <input name="sale_price" id="sale-price" type="number" min="0" step="0.01"
      value="{{ recipe['sale_price'] if recipe else 0 }}" /></label>
    <label>备注 <input name="note" value="{{ recipe['note'] or '' if recipe else '' }}" /></label>
  </div>
  <button type="submit" class="btn-primary">保存配方</button>
</form>

<hr/>
<h3>原料配方</h3>
<p class="muted">可引用 品项（item）或 冰激凌配方（ic_recipe）。</p>

<form method="post" id="bom-form">
  <input type="hidden" name="name" value="{{ recipe['name'] if recipe else '' }}" />
  <input type="hidden" name="note" value="{{ recipe['note'] or '' if recipe else '' }}" />
  <input type="hidden" name="output_unit" value="{{ recipe['output_unit'] if recipe else '件' }}" />
  <input type="hidden" name="output_qty" value="{{ recipe['output_qty'] if recipe else 1 }}" />
  <input type="hidden" name="sale_price" id="bom-sale-price"
         value="{{ recipe['sale_price'] if recipe else 0 }}" />

  <div class="form-block" style="margin: 0.5em 0;">
    <label>
      <input type="checkbox" id="use-temp-prices" />
      使用临时售价（用于探索毛利率）
    </label>
  </div>

  <table class="bom-table">
    <thead>
      <tr>
        <th>类型</th><th>成分</th><th>每份用量</th><th>单位</th>
        <th>采购小计</th><th>销售小计</th>
        <th>临时售价</th><th>操作</th>
      </tr>
    </thead>
    <tbody id="bom-body">
      {% for b in bom_rows %}
      <tr class="bom-row"
          data-source-type="{{ b['source_type'] }}"
          {% if b['source_type']=='item' %}data-item-id="{{ b['item_id'] }}"
          data-unit-cost="{{ b['unit_cost'] or 0 }}"
          data-selling-price="{{ b['selling_price'] or 0 }}"
          data-gram-per-unit="{{ b['gram_per_unit'] or 0 }}"{% endif %}>
        <td>
          <input type="hidden" name="bom_row_id" value="{{ b['id'] }}" />
          <select name="bom_source_type" class="bom-source-type">
            <option value="item" {% if b['source_type']=='item' %}selected{% endif %}>品项</option>
            <option value="ic_recipe" {% if b['source_type']=='ic_recipe' %}selected{% endif %}>冰激凌配方</option>
          </select>
        </td>
        <td class="bom-target-cell">
          {% if b['source_type']=='item' %}
            <select name="bom_item_id" class="bom-item-sel">
              {% for it in items %}
              <option value="{{ it['id'] }}"
                      data-gram="{{ it['gram_per_unit'] or 0 }}"
                      data-uc="{{ it['unit_cost'] or 0 }}"
                      data-sp="{{ it['selling_price'] or 0 }}"
                      {% if it['id']==b['item_id'] %}selected{% endif %}>
                {{ it['category_name'] }} / {{ it['name'] }}
              </option>
              {% endfor %}
            </select>
          {% else %}
            <select name="bom_ic_recipe_id" class="bom-ic-sel">
              {% for ic in ic_recipes %}
              <option value="{{ ic['id'] }}"
                      data-sp="{{ ic['sale_price'] or 0 }}"
                      data-output-qty="{{ ic['output_qty'] or 1 }}"
                      {% if ic['id']==b['ic_recipe_id'] %}selected{% endif %}>
                {{ ic['name'] }}
              </option>
              {% endfor %}
            </select>
          {% endif %}
        </td>
        <td><input type="number" step="0.01" min="0" name="bom_qty" class="bom-qty"
                   value="{{ b['qty_per_unit'] }}" /></td>
        <td><span class="bom-unit-label">—</span></td>
        <td><span class="bom-cost-purchase">0.00</span></td>
        <td><span class="bom-cost-selling">0.00</span></td>
        <td>
          <input type="range" class="bom-temp-slider" min="0" step="0.01"
                 value="0" disabled />
          <span class="bom-temp-display">0.00</span>
          <button type="button" class="btn-sm bom-save-temp"
                  data-item-id="" disabled>保存为新价</button>
        </td>
        <td>
          <label class="del-flag"><input type="checkbox" name="bom_delete" value="1" /> 删除</label>
        </td>
      </tr>
      {% endfor %}
    </tbody>
    <tfoot>
      <tr>
        <th colspan="4" style="text-align:right;">合计</th>
        <th id="total-purchase">0.00</th>
        <th id="total-selling">0.00</th>
        <th colspan="2"></th>
      </tr>
      <tr>
        <th colspan="4" style="text-align:right;">售价</th>
        <th colspan="2" id="footer-sale-price">0.00</th>
        <th colspan="2">毛利率：<strong id="footer-margin">—</strong></th>
      </tr>
    </tfoot>
  </table>

  <button type="button" id="bom-add" class="btn-sm">+ 新增一行</button>
  <button type="submit" class="btn-primary">保存配方</button>
</form>

<script src="{{ url_for('static', filename='recipe_cost.js') }}?v=1"></script>
{% endblock %}
```

- [ ] **Step 6: 跑测试确认通过**

```bash
pytest tests/test_recipe_cost_route.py -v
```

Expected: 5 passed.

- [ ] **Step 7: 跑全部测试**

```bash
pytest -q
```

Expected: 全绿（老测试 + 新测试）。

- [ ] **Step 8: Commit**

```bash
git add blueprints/recipe_cost.py templates/recipe_cost/recipes.html templates/recipe_cost/recipe_edit.html tests/test_recipe_cost_route.py
git commit -m "feat(recipe-cost): recipe CRUD with polymorphic ingredients"
```

---

## Task 8: 前端 JS 实时重算 + 滑块保存为新价

**Files:**
- Create: `static/recipe_cost.js`

- [ ] **Step 1: 写失败测试（端到端：模拟表单提交 → 验证 DB 更新）**

追加到 `tests/test_recipe_cost_route.py`：

```python
def test_update_selling_price_endpoint(tmp_path, monkeypatch):
    """POST /recipe-cost/items/<id>/update-selling-price 写 DB + audit_log。"""
    import db as db_module
    import config as config_module
    from db import init_master_db, init_warehouse_db
    master_path = tmp_path / "master.db"
    wh_path = tmp_path / "wh.db"
    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", tmp_path)
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", tmp_path)
    init_master_db()
    init_warehouse_db(wh_path)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    import sqlite3
    m = sqlite3.connect(master_path)
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (1, 'admin', 'x', 1, ?)", (ts,))
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, created_at) "
        "VALUES (1, 'wh_t', 'T', ?, ?)", (str(wh_path), ts))
    m.execute(
        "INSERT INTO warehouse_users (user_id, warehouse_id, role) "
        "VALUES (1, 1, 'admin')")
    m.commit()
    m.close()

    conn = sqlite3.connect(wh_path)
    conn.row_factory = sqlite3.Row
    cat_id = conn.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()["id"]
    conn.execute(
        "INSERT INTO items (sku, name, category_id, quantity, unit_cost, "
        "selling_price, unit, gram_per_unit, aux_rate, aux_unit, updated_at) "
        "VALUES ('X-1', '糖', ?, 10, 5, 10, '件', 0, 0, NULL, ?)",
        (cat_id, ts))
    item_id = conn.execute("SELECT id FROM items WHERE name='糖'").fetchone()["id"]
    conn.commit()
    conn.close()

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = 1
        s["warehouse_id"] = 1

    resp = client.post(f"/recipe-cost/items/{item_id}/update-selling-price",
                       data={"selling_price": "15.00"},
                       follow_redirects=True)
    assert resp.status_code == 200

    row = sqlite3.connect(wh_path).execute(
        "SELECT selling_price, selling_price_updated_at FROM items WHERE id=?",
        (item_id,),
    ).fetchone()
    assert float(row["selling_price"]) == 15.00
    assert row["selling_price_updated_at"] is not None


def test_update_selling_price_rejects_negative(tmp_path, monkeypatch):
    """负数 → flash 拦截 + DB 不变。"""
    import db as db_module
    import config as config_module
    from db import init_master_db, init_warehouse_db
    master_path = tmp_path / "master.db"
    wh_path = tmp_path / "wh.db"
    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", tmp_path)
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", tmp_path)
    init_master_db()
    init_warehouse_db(wh_path)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    import sqlite3
    m = sqlite3.connect(master_path)
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (1, 'admin', 'x', 1, ?)", (ts,))
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, created_at) "
        "VALUES (1, 'wh_t', 'T', ?, ?)", (str(wh_path), ts))
    m.execute(
        "INSERT INTO warehouse_users (user_id, warehouse_id, role) "
        "VALUES (1, 1, 'admin')")
    m.commit()
    m.close()

    conn = sqlite3.connect(wh_path)
    conn.row_factory = sqlite3.Row
    cat_id = conn.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()["id"]
    conn.execute(
        "INSERT INTO items (sku, name, category_id, quantity, unit_cost, "
        "selling_price, unit, gram_per_unit, aux_rate, aux_unit, updated_at) "
        "VALUES ('X-1', '糖', ?, 10, 5, 10, '件', 0, 0, NULL, ?)",
        (cat_id, ts))
    item_id = conn.execute("SELECT id FROM items WHERE name='糖'").fetchone()["id"]
    conn.commit()
    conn.close()

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = 1
        s["warehouse_id"] = 1

    resp = client.post(f"/recipe-cost/items/{item_id}/update-selling-price",
                       data={"selling_price": "-1"},
                       follow_redirects=True)
    assert resp.status_code == 200

    row = sqlite3.connect(wh_path).execute(
        "SELECT selling_price FROM items WHERE id=?", (item_id,)
    ).fetchone()
    assert float(row["selling_price"]) == 10.0  # 未变
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest tests/test_recipe_cost_route.py::test_update_selling_price_endpoint tests/test_recipe_cost_route.py::test_update_selling_price_rejects_negative -v
```

Expected: 404.

- [ ] **Step 3: 在 `blueprints/recipe_cost.py` 加 `/items/<id>/update-selling-price` 路由**

在 `_save_recipe` 函数后追加：

```python
@bp.route("/recipe-cost/items/<int:item_id>/update-selling-price", methods=["POST"])
@require_platform_admin
def update_selling_price(item_id: int):
    """滑块"保存为新价"按钮：POST 写回 items.selling_price。"""
    db = get_warehouse_db()
    new_sp_raw = request.form.get("selling_price", "0") or "0"
    try:
        new_sp = float(new_sp_raw)
    except ValueError:
        flash("销售单价格式错误")
        return redirect(request.referrer or url_for("core.land"))
    if new_sp < 0:
        flash("销售单价不能为负")
        return redirect(request.referrer or url_for("core.land"))

    old_row = db.execute(
        "SELECT selling_price FROM items WHERE id=?", (item_id,)
    ).fetchone()
    if old_row is None:
        flash("品项不存在")
        return redirect(request.referrer or url_for("core.land"))
    old_sp = float(old_row["selling_price"] or 0)

    db.execute(
        "UPDATE items SET selling_price=?, selling_price_updated_at=?, "
        "updated_at=? WHERE id=?",
        (new_sp, now(), now(), item_id),
    )
    db.commit()
    audit("recipe_cost.items.update_selling_price", "item", item_id, {
        "old": old_sp, "new": new_sp,
    })
    flash(f"已保存新售价 ¥{new_sp:.2f}")
    return redirect(request.referrer or url_for("core.land"))
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest tests/test_recipe_cost_route.py -v
```

Expected: 7 passed.

- [ ] **Step 5: 创建 `static/recipe_cost.js`（实时重算 + 滑块 + 保存）**

```javascript
// 配方成本实时重算（含临时售价滑块）。
// 公式镜像 blueprints/recipe_cost_pure.py::line_cost。

(function() {
  'use strict';

  function round2(n) { return Math.round(n * 100) / 100; }

  function fmtPct(x) {
    if (x === null || x === undefined || isNaN(x)) return '—';
    return (x * 100).toFixed(1) + '%';
  }

  // 单行成本（item 引用）。
  function lineItemCost(row, useTemp) {
    var qty = parseFloat(row.querySelector('.bom-qty').value) || 0;
    var itemSel = row.querySelector('.bom-item-sel');
    if (!itemSel) return {purchase: 0, selling: 0};
    var opt = itemSel.options[itemSel.selectedIndex];
    var uc = parseFloat(opt.getAttribute('data-uc') || '0');
    var sp = parseFloat(opt.getAttribute('data-sp') || '0');
    var gpu = parseFloat(opt.getAttribute('data-gram') || '0');
    var qtyStock = gpu > 0 ? qty / gpu : qty;
    var tempSp = useTemp
      ? parseFloat(row.querySelector('.bom-temp-slider').value) || 0
      : sp;
    return {
      purchase: round2(qtyStock * uc),
      selling: round2(qtyStock * tempSp),
    };
  }

  // 单行成本（ic_recipe 引用）。需要后端算出 ic_recipe 的 per-unit 成本。
  // 简化：让 JS 用一个 ic_recipes 字典（服务端序列化到 dataset）。
  function lineIcCost(row, useTemp, icRecipes) {
    var qty = parseFloat(row.querySelector('.bom-qty').value) || 0;
    var sel = row.querySelector('.bom-ic-sel');
    if (!sel) return {purchase: 0, selling: 0};
    var icId = parseInt(sel.value);
    var ic = icRecipes[icId];
    if (!ic) return {purchase: 0, selling: 0};
    var outQty = parseFloat(ic.output_qty) || 1;
    // 服务端已注入 icRecipes[icId].cost_purchase_per_unit
    // 见 ic_recipe_cost() 调用方：
    // icRecipes 数据结构：{ id: {name, sale_price, output_qty, cost_purchase_per_unit, cost_selling_per_unit} }
    return {
      purchase: round2(qty * ic.cost_purchase_per_unit),
      selling: round2(qty * ic.cost_selling_per_unit),
    };
  }

  function recomputeAll() {
    var useTemp = document.getElementById('use-temp-prices').checked;
    var rows = document.querySelectorAll('#bom-body .bom-row');
    var totalP = 0, totalS = 0;
    rows.forEach(function(row) {
      // Skip rows marked for delete.
      var delCb = row.querySelector('input[name="bom_delete"]');
      if (delCb && delCb.checked) {
        row.querySelector('.bom-cost-purchase').textContent = '0.00';
        row.querySelector('.bom-cost-selling').textContent = '0.00';
        return;
      }
      var st = row.getAttribute('data-source-type') || 'item';
      var cost;
      if (st === 'ic_recipe') {
        cost = lineIcCost(row, useTemp, window.__icRecipes || {});
      } else {
        cost = lineItemCost(row, useTemp);
      }
      totalP += cost.purchase;
      totalS += cost.selling;
      row.querySelector('.bom-cost-purchase').textContent = cost.purchase.toFixed(2);
      row.querySelector('.bom-cost-selling').textContent = cost.selling.toFixed(2);

      // Slider/display sync (item 引用且 useTemp 启用)。
      var slider = row.querySelector('.bom-temp-slider');
      if (slider) {
        var sp = row.getAttribute('data-selling-price') || '0';
        var display = row.querySelector('.bom-temp-display');
        if (useTemp && st === 'item') {
          slider.disabled = false;
          display.textContent = parseFloat(slider.value).toFixed(2);
        } else {
          slider.disabled = true;
          if (st === 'item') {
            slider.value = sp;
            display.textContent = parseFloat(sp).toFixed(2);
          } else {
            slider.value = 0;
            display.textContent = '0.00';
          }
        }
      }
    });
    var salePrice = parseFloat(
      document.getElementById('bom-sale-price').value || '0'
    );
    var margin = salePrice > 0 ? (salePrice - totalP) / salePrice : null;
    document.getElementById('total-purchase').textContent = totalP.toFixed(2);
    document.getElementById('total-selling').textContent = totalS.toFixed(2);
    document.getElementById('footer-sale-price').textContent = salePrice.toFixed(2);
    document.getElementById('footer-margin').textContent = fmtPct(margin);
  }

  // 滑块 "保存为新价"。
  function bindSaveButtons() {
    document.querySelectorAll('.bom-save-temp').forEach(function(btn) {
      btn.addEventListener('click', function() {
        var row = btn.closest('.bom-row');
        var itemId = btn.getAttribute('data-item-id');
        if (!itemId) return;
        var sp = row.querySelector('.bom-temp-slider').value;
        var form = document.createElement('form');
        form.method = 'POST';
        form.action = '/recipe-cost/items/' + itemId + '/update-selling-price';
        var input = document.createElement('input');
        input.type = 'hidden';
        input.name = 'selling_price';
        input.value = sp;
        form.appendChild(input);
        document.body.appendChild(form);
        form.submit();
      });
    });
  }

  // 切换 source_type → 替换 target cell 的 select。
  function bindSourceTypeSwitches() {
    document.querySelectorAll('.bom-source-type').forEach(function(sel) {
      sel.addEventListener('change', function() {
        var row = sel.closest('.bom-row');
        var cell = row.querySelector('.bom-target-cell');
        var st = sel.value;
        if (st === 'ic_recipe') {
          cell.innerHTML = buildIcRecipeSelect();
        } else {
          cell.innerHTML = buildItemSelect();
        }
        recomputeAll();
      });
    });
  }

  function buildItemSelect() {
    // 简单粗暴：用 dataset 里嵌入的 items JSON（如果有）。否则给空。
    var items = window.__items || [];
    var opts = items.map(function(it) {
      return '<option value="' + it.id + '" data-gram="' + (it.gram_per_unit || 0) +
             '" data-uc="' + (it.unit_cost || 0) + '" data-sp="' + (it.selling_price || 0) +
             '">' + it.label + '</option>';
    }).join('');
    return '<select name="bom_item_id" class="bom-item-sel">' + opts + '</select>';
  }

  function buildIcRecipeSelect() {
    var ics = window.__icRecipes || {};
    var opts = Object.keys(ics).map(function(id) {
      var ic = ics[id];
      return '<option value="' + id + '">' + ic.name + '</option>';
    }).join('');
    return '<select name="bom_ic_recipe_id" class="bom-ic-sel">' + opts + '</select>';
  }

  // 新增一行
  function bindAddRow() {
    var btn = document.getElementById('bom-add');
    if (!btn) return;
    btn.addEventListener('click', function() {
      var tbody = document.getElementById('bom-body');
      var tr = document.createElement('tr');
      tr.className = 'bom-row';
      tr.setAttribute('data-source-type', 'item');
      tr.innerHTML = `
        <td>
          <input type="hidden" name="bom_row_id" value="" />
          <select name="bom_source_type" class="bom-source-type">
            <option value="item" selected>品项</option>
            <option value="ic_recipe">冰激凌配方</option>
          </select>
        </td>
        <td class="bom-target-cell">${buildItemSelect()}</td>
        <td><input type="number" step="0.01" min="0" name="bom_qty" class="bom-qty" value="0" /></td>
        <td><span class="bom-unit-label">—</span></td>
        <td><span class="bom-cost-purchase">0.00</span></td>
        <td><span class="bom-cost-selling">0.00</span></td>
        <td>
          <input type="range" class="bom-temp-slider" min="0" step="0.01" value="0" disabled />
          <span class="bom-temp-display">0.00</span>
          <button type="button" class="btn-sm bom-save-temp" data-item-id="" disabled>保存为新价</button>
        </td>
        <td><label class="del-flag"><input type="checkbox" name="bom_delete" value="1" /> 删除</label></td>
      `;
      tbody.appendChild(tr);
      bindSourceTypeSwitches();
      bindSliderFor(tr);
      bindItemSelChange(tr);
      recomputeAll();
    });
  }

  function bindSliderFor(row) {
    var slider = row.querySelector('.bom-temp-slider');
    var display = row.querySelector('.bom-temp-display');
    if (!slider) return;
    slider.addEventListener('input', function() {
      display.textContent = parseFloat(slider.value).toFixed(2);
      recomputeAll();
    });
  }

  function bindItemSelChange(row) {
    var sel = row.querySelector('.bom-item-sel');
    if (!sel) return;
    sel.addEventListener('change', function() {
      var opt = sel.options[sel.selectedIndex];
      var sp = parseFloat(opt.getAttribute('data-sp') || '0');
      row.setAttribute('data-selling-price', sp);
      var slider = row.querySelector('.bom-temp-slider');
      if (slider) slider.value = sp;
      var saveBtn = row.querySelector('.bom-save-temp');
      if (saveBtn) saveBtn.setAttribute('data-item-id', sel.value);
      recomputeAll();
    });
  }

  function bindAll() {
    var toggle = document.getElementById('use-temp-prices');
    if (toggle) toggle.addEventListener('change', recomputeAll);

    var salePrice = document.getElementById('bom-sale-price');
    if (salePrice) salePrice.addEventListener('input', recomputeAll);

    document.querySelectorAll('#bom-body .bom-row').forEach(function(row) {
      bindSliderFor(row);
      bindItemSelChange(row);
    });

    bindSourceTypeSwitches();
    bindSaveButtons();
    bindAddRow();
    recomputeAll();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bindAll);
  } else {
    bindAll();
  }
})();
```

- [ ] **Step 6: 把 ic_recipes + items 字典从服务端注入到模板**

打开 `templates/recipe_cost/ic_recipe_edit.html`，在 `<script src=...>` 前追加：

```html
<script>
  window.__items = [
    {% for it in items %}
    {"id": {{ it['id'] }}, "label": "{{ it['category_name'] }} / {{ it['name'] }}",
     "gram_per_unit": {{ it['gram_per_unit'] or 0 }},
     "unit_cost": {{ it['unit_cost'] or 0 }},
     "selling_price": {{ it['selling_price'] or 0 }}}{% if not loop.last %},{% endif %}
    {% endfor %}
  ];
</script>
```

打开 `templates/recipe_cost/recipe_edit.html`，在 `<script src=...>` 前追加：

```html
<script>
  window.__items = [
    {% for it in items %}
    {"id": {{ it['id'] }}, "label": "{{ it['category_name'] }} / {{ it['name'] }}",
     "gram_per_unit": {{ it['gram_per_unit'] or 0 }},
     "unit_cost": {{ it['unit_cost'] or 0 }},
     "selling_price": {{ it['selling_price'] or 0 }}}{% if not loop.last %},{% endif %}
    {% endfor %}
  ];
  window.__icRecipes = {
    {% for ic in ic_recipes %}
    "{{ ic['id'] }}": {
      "name": "{{ ic['name'] }}",
      "sale_price": {{ ic['sale_price'] or 0 }},
      "output_qty": {{ ic['output_qty'] or 1 }},
      "cost_purchase_per_unit": {{ ic['cost_purchase_per_unit'] }},
      "cost_selling_per_unit": {{ ic['cost_selling_per_unit'] }}
    }{% if not loop.last %},{% endif %}
    {% endfor %}
  };
</script>
```

- [ ] **Step 7: 在 `recipe_cost.py` 的 `_load_ic_recipes_for_picker()` 中附 per-unit 成本**

替换 `_load_ic_recipes_for_picker`：

```python
def _load_ic_recipes_for_picker() -> list:
    db = get_warehouse_db()
    from blueprints.recipe_cost_pure import ic_recipe_cost
    rows = db.execute(
        "SELECT id, name, sale_price, output_unit, output_qty FROM ic_recipes ORDER BY name"
    ).fetchall()
    enriched = []
    for r in rows:
        c = ic_recipe_cost(db, int(r["id"]))
        out_qty = float(r["output_qty"] or 1)
        enriched.append({
            **dict(r),
            "cost_purchase_per_unit": float(c["cost_purchase"]) / out_qty if out_qty > 0 else 0.0,
            "cost_selling_per_unit": float(c["cost_selling"]) / out_qty if out_qty > 0 else 0.0,
        })
    return enriched
```

- [ ] **Step 8: 跑全部测试**

```bash
pytest -q
ruff check .
```

Expected: 全绿、0 warning。

- [ ] **Step 9: 手工冒烟**

启动服务 → 登录 admin → 选仓库 → 走 `/recipe-cost/` → 进入冰激凌配方 → 新建 → 加 2 行原料（其中一个启用克）→ 编辑 sale_price → 看底部数字实时变。勾 "使用临时价" → 滑块可调 → 点 "保存为新价" → 页面刷新，DB 中 items.selling_price 改变。

- [ ] **Step 10: Commit**

```bash
git add static/recipe_cost.js templates/recipe_cost/ blueprints/recipe_cost.py tests/test_recipe_cost_route.py
git commit -m "feat(recipe-cost): live JS recalc + temp slider + save-as-new-price"
```

---

## Task 9: 只读成本 API + 删除 items 引用保护

**Files:**
- Modify: `blueprints/recipe_cost.py`（追加 `/api/cost/<kind>/<id>` 路由）
- Modify: `blueprints/items.py`（`delete_item` 扩引用检查）

- [ ] **Step 1: 写失败测试（追加到 `tests/test_recipe_cost_route.py`）**

```python
def test_api_cost_returns_json(tmp_path, monkeypatch):
    """GET /recipe-cost/api/cost/<kind>/<id> 返回成本 JSON。"""
    import db as db_module
    import config as config_module
    from db import init_master_db, init_warehouse_db
    master_path = tmp_path / "master.db"
    wh_path = tmp_path / "wh.db"
    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", tmp_path)
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", tmp_path)
    init_master_db()
    init_warehouse_db(wh_path)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    import sqlite3
    m = sqlite3.connect(master_path)
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (1, 'admin', 'x', 1, ?)", (ts,))
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, created_at) "
        "VALUES (1, 'wh_t', 'T', ?, ?)", (str(wh_path), ts))
    m.execute(
        "INSERT INTO warehouse_users (user_id, warehouse_id, role) "
        "VALUES (1, 1, 'admin')")
    m.commit()
    m.close()

    conn = sqlite3.connect(wh_path)
    conn.execute(
        "INSERT INTO ic_recipes (name, output_unit, output_qty, sale_price, "
        "created_at, updated_at) VALUES ('香草冰淇淋', 'g', 100, 25, ?, ?)",
        (ts, ts))
    ic_id = conn.execute("SELECT id FROM ic_recipes WHERE name='香草冰淇淋'").fetchone()["id"]
    conn.commit()
    conn.close()

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = 1
        s["warehouse_id"] = 1

    resp = client.get(f"/recipe-cost/api/cost/ic_recipe/{ic_id}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["cost_purchase"] == 0.0
    assert data["cost_selling"] == 0.0
    assert data["sale_price"] == 25.0
    assert data["margin_purchase"] is None  # 0/0


def test_items_delete_blocked_when_referenced_by_recipe(tmp_path, monkeypatch):
    """品项被 ic_recipe_items 引用时禁止删除。"""
    import db as db_module
    import config as config_module
    from db import init_master_db, init_warehouse_db
    master_path = tmp_path / "master.db"
    wh_path = tmp_path / "wh.db"
    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", tmp_path)
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", tmp_path)
    init_master_db()
    init_warehouse_db(wh_path)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    import sqlite3
    m = sqlite3.connect(master_path)
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (1, 'admin', 'x', 1, ?)", (ts,))
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, created_at) "
        "VALUES (1, 'wh_t', 'T', ?, ?)", (str(wh_path), ts))
    m.execute(
        "INSERT INTO warehouse_users (user_id, warehouse_id, role) "
        "VALUES (1, 1, 'admin')")
    m.commit()
    m.close()

    conn = sqlite3.connect(wh_path)
    conn.row_factory = sqlite3.Row
    cat_id = conn.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()["id"]
    conn.execute(
        "INSERT INTO items (sku, name, category_id, quantity, unit_cost, "
        "selling_price, unit, gram_per_unit, aux_rate, aux_unit, updated_at) "
        "VALUES ('X-1', '糖', ?, 10, 5, 10, '件', 0, 0, NULL, ?)",
        (cat_id, ts))
    item_id = conn.execute("SELECT id FROM items WHERE name='糖'").fetchone()["id"]
    conn.execute(
        "INSERT INTO ic_recipes (name, output_unit, output_qty, sale_price, "
        "created_at, updated_at) VALUES ('香草冰淇淋', 'g', 100, 25, ?, ?)",
        (ts, ts))
    ic_id = conn.execute("SELECT id FROM ic_recipes WHERE name='香草冰淇淋'").fetchone()["id"]
    conn.execute(
        "INSERT INTO ic_recipe_items (ic_recipe_id, item_id, qty_per_unit) "
        "VALUES (?, ?, 50)", (ic_id, item_id))
    conn.commit()
    conn.close()

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = 1
        s["warehouse_id"] = 1

    resp = client.post(f"/items/{item_id}/delete", follow_redirects=True)
    assert resp.status_code == 200
    cnt = sqlite3.connect(wh_path).execute(
        "SELECT COUNT(*) AS c FROM items WHERE id=?", (item_id,)
    ).fetchone()["c"]
    assert cnt == 1  # 未删
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest tests/test_recipe_cost_route.py::test_api_cost_returns_json tests/test_recipe_cost_route.py::test_items_delete_blocked_when_referenced_by_recipe -v
```

Expected: 都 404 / 200 (但物品被删).

- [ ] **Step 3: 在 `blueprints/recipe_cost.py` 加 `/api/cost/<kind>/<id>` 路由**

在 `update_selling_price` 函数后追加：

```python
@bp.route("/recipe-cost/api/cost/<kind>/<int:rid>", methods=["GET"])
@require_login
def api_cost(kind: str, rid: int):
    """只读 JSON：当前 ic_recipe / recipe 的成本与毛利率（前端 sanity check 用）。"""
    db = get_warehouse_db()
    from blueprints.recipe_cost_pure import ic_recipe_cost, recipe_cost
    if kind == "ic_recipe":
        c = ic_recipe_cost(db, rid)
    elif kind == "recipe":
        c = recipe_cost(db, rid)
    else:
        return {"error": "unknown_kind"}, 400
    if c is None:
        return {"error": "not_found"}, 404
    return {
        "cost_purchase": float(c["cost_purchase"]),
        "cost_selling": float(c["cost_selling"]),
        "sale_price": float(c["sale_price"]),
        "margin_purchase": c["margin_purchase"],
    }
```

- [ ] **Step 4: 改 `blueprints/items.py::delete_item` 加引用检查**

打开 `blueprints/items.py`，定位到 `delete_item` 函数内 `usage = db.execute(...)` 这一行。替换为：

```python
    usage = db.execute(
        """SELECT
              (SELECT COUNT(*) FROM stock_movements WHERE item_id=?) +
              (SELECT COUNT(*) FROM restock_requests WHERE item_id=?) +
              (SELECT COUNT(*) FROM outbound_requests WHERE item_id=?) +
              (SELECT COUNT(*) FROM stocktakes WHERE item_id=?) +
              (SELECT COUNT(*) FROM ic_recipe_items WHERE item_id=?) +
              (SELECT COUNT(*) FROM recipe_items WHERE item_id=? AND source_type='item') AS c""",
        (item_id, item_id, item_id, item_id, item_id, item_id),
    ).fetchone()["c"]
```

把 flash 文案改为更通用（如果引用计数为 0 才走删除流程；否则闪原有文案）。

- [ ] **Step 5: 跑全部测试**

```bash
pytest -q
```

Expected: 全绿。

- [ ] **Step 6: Commit**

```bash
git add blueprints/recipe_cost.py blueprints/items.py tests/test_recipe_cost_route.py
git commit -m "feat(recipe-cost): api cost endpoint + items.delete reference check"
```

---

## Task 10: 端到端冒烟 + AGENT.md / README 更新

**Files:**
- Modify: `AGENT.md`（如存在）
- Modify: `README.md`（如存在）

- [ ] **Step 1: 端到端冒烟**

```bash
cd /Users/ericmr/Documents/GitHub/DailyCheck
flask --app app run --host 0.0.0.0 --port 5001 &
sleep 2
```

- 登录 admin → 选仓库 → 落地到 `/land`
- `/items` 给 "糖" 设 `unit_cost=5`、`selling_price=10`
- `/items` 给 "牛奶" 设 `unit_cost=8`、`selling_price=15`，gram_per_unit=50
- `/recipe-cost/` → 跳到冰激凌配方 → 新建 "香草冰淇淋"，输出 100g，售价 25
  - 加 2 行：糖 20g、牛奶 60g → 底部应显示：
    - cost_purchase = 20/50*5 + 60/50*8 = 2 + 9.6 = 11.60（糖未启用克，按原值；若糖启用克则要换算）
    - 这里我们让"糖"不启用克，单位"件"，那 20g 视为 0.2 件 → 0.2 * 5 = 1.0；实际看界面
    - 实际正确口径：**所有 qty 在"启用克"的品项上视为克**，否则视为库存单位。这里糖 gram_per_unit=0，所以 20g = 20 件 → cost_purchase = 20*5 = 100。牛奶 60g / 50 * 8 = 9.6 → 总 109.6。检查 UI 显示与此一致
- 保存 → 列表卡显示毛利率
- `/recipe-cost/recipes` → 新建 "柠檬茶"，售价 15
  - 加 2 行：第 1 行类型=ic_recipe，选"香草冰淇淋" 50g；第 2 行类型=item，选"糖" 20g（无克重）
  - 底部应显示：cost_purchase = 50/100*11.60 + 20*5 = 5.80 + 100 = 105.80
  - cost_selling = 50/100*23.20 + 20*10 = 11.60 + 200 = 211.60
  - margin = (15 - 105.80) / 15 → 负数（说明数据示例只作演示）
- 改 "糖" 的 selling_price → 跳回冰激凌编辑页 → 数字应已变（页面重渲）
- 在柠檬茶编辑页勾 "使用临时价" → 调糖的滑块到 6 → 底部数字立即变（不发请求）
- 点糖的"保存为新价" → 跳回 `/items` → 糖的 selling_price 现在是 6
- 删除冰激凌配方 → 提示"被 1 个出品引用，无法删除"
- 删除出品配方 → 成功
- 再删冰激凌 → 成功

- [ ] **Step 2: 跑 lint + 全测试**

```bash
ruff check .
pytest -q --cov=blueprints/recipe_cost_pure.py --cov=blueprints/recipe_cost.py --cov=blueprints/items.py --cov=blueprints/_helpers.py
```

Expected: 0 warning；覆盖率 ≥ 80% on new files。

- [ ] **Step 3: 收尾**

```bash
kill %1
git status
git log --oneline -10
```

Expected: 10 个新 commit 落 main，无未提交新文件。

- [ ] **Step 4: 如果 AGENT.md / README 存在，更新**

打开 `AGENT.md`，在目录结构块追加：

```
- `blueprints/recipe_cost.py`：配方成本计算（冰激凌配方 / 出品配方 CRUD + 实时毛利率）
- `blueprints/recipe_cost_pure.py`：成本/毛利计算纯函数
```

如果 `README.md` 存在，在功能列表追加：

```
- 配方成本计算（冰激凌配方与出品配方 + 双价原料 + 实时毛利率 + 临时调价探索）
```

```bash
git add AGENT.md README.md  # 仅当文件存在
git commit -m "docs: AGENT.md / README add recipe-cost module"
```

---

## Self-Review

**Spec coverage** (对照 `2026-07-22-recipe-cost-design.md`):

- [x] `items.selling_price` 加列 + 4 张新表 → Task 1
- [x] 纯函数 helper `qty_to_stock_units` → Task 2
- [x] 纯函数 `line_cost` / `ic_recipe_cost` / `recipe_cost`（含 ic_recipe 引用按 sale_price 折算） → Task 3
- [x] items CRUD selling_price 字段 → Task 4
- [x] 蓝图骨架 + nav 入口 → Task 5
- [x] 冰激凌配方 CRUD（list / new / edit / delete + 删除保护） → Task 6
- [x] 出品配方 CRUD（多态原料） → Task 7
- [x] 前端 JS 实时重算 + 滑块 + 保存为新价 → Task 8
- [x] 只读 API + items.delete 引用保护 → Task 9
- [x] 端到端冒烟 + 文档 → Task 10

**Placeholder scan**: 无 TBD / "类似 Task N" / "implement later"；所有代码块完整。

**Type/name 一致性**:
- `qty_to_stock_units` 在 Task 2 测试与 Task 3 实现 + `recipe_cost_pure.py` 一致（pure 模块自带实现 + _helpers 也有，命名一致）
- `line_cost` / `ic_recipe_cost` / `recipe_cost` 在 Task 3 测试 + Task 6/7 路由引用一致
- 蓝图路由 `ic_recipes_list` / `ic_recipe_new` / `ic_recipe_edit` / `ic_recipe_delete` / `recipes_list` / `recipe_new` / `recipe_edit` / `recipe_delete` / `update_selling_price` / `api_cost` / `landing` 在 Task 5/6/7/9 一致
- 模板 `recipe_cost/ic_recipes.html` / `ic_recipe_edit.html` / `recipes.html` / `recipe_edit.html` / `base_recipe.html` 与路由 render 调用一致
- JS `window.__items` / `window.__icRecipes` 在 Task 8 Step 6 模板注入 + Task 8 Step 5 JS 读取一致
- 表单字段 `bom_row_id` / `bom_source_type` / `bom_item_id` / `bom_ic_recipe_id` / `bom_qty` / `bom_delete` 在 Task 7 Step 3 路由 + Task 7 Step 5 模板一致

**注意事项**:
- Task 1 Step 2 复用 `item_cols` 已存在定义，不要重复定义（spec 已有该变量）。
- Task 7 Step 5 `recipe_edit.html` 的 source_type 切换联动通过 JS Task 8 Step 5 的 `bindSourceTypeSwitches()` 完成。
- Task 8 Step 7 把 `_load_ic_recipes_for_picker` 升级为附 per-unit 成本 — 这是模板需要的新数据。
- Task 10 端到端中"糖"是否启用克：默认不启用 → 20g 视为 20 件 → cost_purchase = 100。验证前先检查 `/items` 编辑页确认 gram_per_unit 字段值。
- `audit()` 调用需要 `from blueprints.auth import audit`（Task 6/7 已 import）。
- coverage ≥ 80% 的检查需要新文件：recipe_cost_pure.py / recipe_cost.py / items.py 的 selling_price 部分 / _helpers.py 的 qty_to_stock_units。