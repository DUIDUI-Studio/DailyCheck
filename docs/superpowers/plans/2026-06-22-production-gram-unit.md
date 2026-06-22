# 生产录入克单位换算 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让生产录入时按克录入/显示配方用量，提交时自动换算回库存单位扣减，出入库与库存查阅仍用现有库存单位。

**Architecture:** 在 `items` 表加一列 `gram_per_unit`（1 库存单位=多少克，0=不启用克）。克只作录入/显示单位；换算只在生产提交那一刻发生（克→库存单位），写入数据库的全是库存单位，因此出库/回退/删除/库存页面零改动。

**Tech Stack:** Python 3.9 + Flask 3 + SQLite（多仓库，每仓一个 db 文件）；pytest 8 测试；前端原生 JS。

## Global Constraints

- 数量精度统一 2 位小数，用 `Decimal(...).quantize(Decimal('0.01'))`，禁止裸 float 运算（见 `blueprints/_helpers.py:52` 的 `parse_qty`）。
- 新列迁移走现有 `migrate_warehouse_db_columns()` 模式：`PRAGMA table_info` 检查缺列才 `ALTER`，每请求自动补列，不写独立迁移脚本。
- 写入 `production_run_items.actual_qty` / `outbound_requests` / `stock_movements` 的数量**永远是库存单位**，绝不是克。
- 测试从仓库根目录运行：`/usr/bin/python3 -m pytest tests/ -v`（用 `/usr/bin/python3`，因 `.venv` 在预览沙箱有权限问题；系统 Python 已装 Flask 3.0 + pytest 8.4）。
- 代码由 haiku 模型逐任务实施；第三层「实际操作验证」（Task 7）派**独立 agent** 用 preview 工具真实点击，与写代码上下文隔离。
- 提交信息用中文，遵循 `<type>: <desc>` 格式，不加 attribution。

---

## File Structure

| 文件 | 职责 | 改动 |
|------|------|------|
| `db/__init__.py` | schema + 迁移 | `WAREHOUSE_SCHEMA` 的 items 加列；`migrate_warehouse_db_columns()` 加 items 补列检查 |
| `blueprints/_helpers.py` | 共享纯函数 | 新增 `grams_to_stock()` |
| `blueprints/items.py` | 物品 CRUD | create + edit 读写 `gram_per_unit` |
| `templates/edit_item.html` | 物品编辑表单 | 加「每单位克重」字段 |
| `blueprints/production.py` | 生产录入/提交 | `_load_items_for_bom()` 带 `gram_per_unit`；`session()` bom 查询带 `gram_per_unit`；`submit()` 用 `grams_to_stock` 换算 |
| `templates/production/product_edit.html` | 配方编辑 | BOM 行单位标签随原料动态切换 |
| `templates/production/session.html` | 生产录入 | 原料卡显示克，hidden 传库存单位 |
| `tests/conftest.py` | 测试夹具 | 新增：临时仓库 db + 直设 session 登录 |
| `tests/test_grams_to_stock.py` | 单元测试 | 新增：Layer 1 |
| `tests/test_production_grams.py` | 集成测试 | 新增：Layer 2 |

零改动（靠测试保证）：出库、回退、删除、库存查阅、CSV 导出。

---

### Task 1: 加 `gram_per_unit` 列与迁移

**Files:**
- Modify: `db/__init__.py:101-112`（`WAREHOUSE_SCHEMA` 的 items 表）
- Modify: `db/__init__.py:278-300`（`migrate_warehouse_db_columns`）

**Interfaces:**
- Produces: `items` 表多一列 `gram_per_unit REAL NOT NULL DEFAULT 0`，所有已存在的仓库 db 在下次请求时自动补列。

- [ ] **Step 1: 在 `WAREHOUSE_SCHEMA` 的 items 表加列**

打开 `db/__init__.py`，找到 items 建表语句（约 101-112 行），把 `unit_cost REAL NOT NULL DEFAULT 0,` 那一行后面加一行 `gram_per_unit`。改成：

```python
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    category_id INTEGER NOT NULL,
    quantity REAL NOT NULL DEFAULT 0,
    safety_stock REAL NOT NULL DEFAULT 0,
    unit TEXT NOT NULL DEFAULT '件',
    unit_cost REAL NOT NULL DEFAULT 0,
    gram_per_unit REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (category_id) REFERENCES categories(id)
);
```

- [ ] **Step 2: 在 `migrate_warehouse_db_columns` 加 items 补列检查**

在 `db/__init__.py` 的 `migrate_warehouse_db_columns()` 里，`stocktake_batches` 的检查之后、`conn.commit()` 之前，加 items 列检查：

```python
        item_cols = {r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()}
        if "gram_per_unit" not in item_cols:
            conn.execute(
                "ALTER TABLE items ADD COLUMN gram_per_unit REAL NOT NULL DEFAULT 0"
            )
        conn.commit()
```

（即在现有 `loss_req_ids` 检查块之后追加这段，保留原有的 `conn.commit()` 或合并为一个。）

- [ ] **Step 3: 验证迁移在真实仓库 db 上生效**

Run:
```bash
cd /Users/ericmr/Documents/GitHub/DailyCheck
DAILYCHECK_SECRET_KEY=dev-key-change-me /usr/bin/python3 -c "
from db import migrate_warehouse_db_columns
from pathlib import Path
migrate_warehouse_db_columns(Path('db/warehouses/wh_001.db'))
import sqlite3
cols = [r[1] for r in sqlite3.connect('db/warehouses/wh_001.db').execute('PRAGMA table_info(items)')]
print('gram_per_unit' in cols)
"
```
Expected: `True`

- [ ] **Step 4: Commit**

```bash
git add db/__init__.py
git commit -m "feat(db): items 表加 gram_per_unit 列与自动迁移"
```

---

### Task 2: `grams_to_stock` 纯函数（Layer 1 单元测试）

**Files:**
- Create: `tests/test_grams_to_stock.py`
- Modify: `blueprints/_helpers.py`（在 `parse_qty` 之后加新函数）

**Interfaces:**
- Produces: `grams_to_stock(grams: float, gram_per_unit: float) -> float` — 克转库存单位；`gram_per_unit<=0` 时原样返回 grams；否则 `grams/gram_per_unit` 量化 2 位小数。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_grams_to_stock.py`：

```python
"""Layer 1: grams_to_stock 纯函数单元测试。"""
from blueprints._helpers import grams_to_stock


def test_basic_conversion():
    # 1440 克，1 袋=1000 克 → 1.44 袋
    assert grams_to_stock(1440, 1000) == 1.44


def test_disabled_returns_grams_unchanged():
    # gram_per_unit=0 表示未启用克，原样返回（此时入参其实是库存单位量）
    assert grams_to_stock(6, 0) == 6


def test_disabled_negative_guard():
    # 负的 gram_per_unit 也按未启用处理
    assert grams_to_stock(5, -1) == 5


def test_another_rate():
    # 2880 克，1 罐=2000 克 → 1.44 罐
    assert grams_to_stock(2880, 2000) == 1.44


def test_tiny_quantity():
    # 10 克，1 瓶=500 克 → 0.02 瓶
    assert grams_to_stock(10, 500) == 0.02


def test_rounding_to_two_dp():
    # 1000 克，1 袋=3 克 → 333.33（量化 2 位）
    assert grams_to_stock(1000, 3) == 333.33


def test_zero_grams():
    assert grams_to_stock(0, 1000) == 0.0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /Users/ericmr/Documents/GitHub/DailyCheck && /usr/bin/python3 -m pytest tests/test_grams_to_stock.py -v`
Expected: FAIL with `ImportError: cannot import name 'grams_to_stock'`

- [ ] **Step 3: 实现 `grams_to_stock`**

在 `blueprints/_helpers.py` 的 `parse_qty` 函数之后（约 79 行后）加：

```python
def grams_to_stock(grams: float, gram_per_unit: float) -> float:
    """克 → 库存单位。

    gram_per_unit<=0 表示该物品未启用克换算，原样返回入参
    （此时调用方传入的本就是库存单位量）。否则做 grams/gram_per_unit
    的除法并量化到 2 位小数（与系统其余数量精度一致）。
    """
    if gram_per_unit <= 0:
        return float(Decimal(str(grams)).quantize(Decimal('0.01')))
    return float(
        (Decimal(str(grams)) / Decimal(str(gram_per_unit)))
        .quantize(Decimal('0.01'))
    )
```

（`Decimal` 已在文件顶部 `from decimal import Decimal, InvalidOperation` 导入，无需新增 import。）

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /Users/ericmr/Documents/GitHub/DailyCheck && /usr/bin/python3 -m pytest tests/test_grams_to_stock.py -v`
Expected: PASS（7 passed）

- [ ] **Step 5: Commit**

```bash
git add blueprints/_helpers.py tests/test_grams_to_stock.py
git commit -m "feat(helpers): 新增 grams_to_stock 克换算纯函数 + 单元测试"
```

---

### Task 3: 物品 CRUD 读写 `gram_per_unit`

**Files:**
- Modify: `blueprints/items.py:22-47`（create 分支）
- Modify: `blueprints/items.py:70-87`（edit 分支）
- Modify: `templates/edit_item.html:30-33`（加字段）

**Interfaces:**
- Consumes: Task 1 的 `gram_per_unit` 列。
- Produces: 物品新建/编辑可设置 `gram_per_unit`；`edit_item.html` 渲染该字段。

- [ ] **Step 1: create 分支读取并写入 `gram_per_unit`**

在 `blueprints/items.py` 的 `items_list()` POST 分支，`unit = ...` 那行之后加读取：

```python
        unit = request.form.get("unit", "件").strip() or "件"
        gram_per_unit = parse_qty(request.form.get("gram_per_unit", "0"))
```

并把 INSERT 语句改为包含该列：

```python
            db.execute(
                """INSERT INTO items
                   (sku, name, category_id, quantity, safety_stock, unit_cost, unit, gram_per_unit, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (gen_sku(), name, int(category_id), quantity, safety_stock, unit_cost, unit, gram_per_unit, now()),
            )
```

- [ ] **Step 2: edit 分支读取并更新 `gram_per_unit`**

在 `edit_item()` 的 POST 分支，`unit = ...` 那行之后加：

```python
        unit = request.form.get("unit", "件").strip() or "件"
        gram_per_unit = parse_qty(request.form.get("gram_per_unit", "0"))
```

并把 UPDATE 改为：

```python
        db.execute(
            """UPDATE items SET name=?, category_id=?, safety_stock=?,
               unit_cost=?, unit=?, gram_per_unit=?, updated_at=? WHERE id=?""",
            (name, int(category_id), safety_stock, unit_cost, unit, gram_per_unit, now(), item_id),
        )
```

- [ ] **Step 3: 模板加「每单位克重」字段**

在 `templates/edit_item.html` 的「单位」label（30-33 行）之后、`<a href...取消>` 之前插入：

```html
  <label>
    <span>每单位克重（克，0=不启用克）</span>
    <input name="gram_per_unit" type="number" min="0" step="0.01" value="{{ item.gram_per_unit or 0 }}" />
  </label>
```

- [ ] **Step 4: 手工冒烟验证（启动应用→编辑物品→存克重→重开确认留存）**

Run:
```bash
cd /Users/ericmr/Documents/GitHub/DailyCheck
DAILYCHECK_SECRET_KEY=dev-key-change-me /usr/bin/python3 -c "
from app import create_app
app = create_app()
print('items.py imports OK, edit_item route registered:',
      any(r.endpoint=='items.edit_item' for r in app.url_map.iter_rules()))
"
```
Expected: `... True`（确认无语法/导入错误，路由存在；UI 留存验证在 Task 7 实操）

- [ ] **Step 5: Commit**

```bash
git add blueprints/items.py templates/edit_item.html
git commit -m "feat(items): 物品支持设置每单位克重 gram_per_unit"
```

---

### Task 4: 配方 BOM 携带 `gram_per_unit` 并动态切换单位标签

**Files:**
- Modify: `blueprints/production.py:45-52`（`_load_items_for_bom`）
- Modify: `templates/production/product_edit.html:27-71`（表头、行、新增行 JS）

**Interfaces:**
- Consumes: Task 1 的列；Task 3 写入的克重。
- Produces: `_load_items_for_bom()` 返回的每个 item 多带 `gram_per_unit`；配方编辑页每行用量输入框旁显示「克」或原单位（随所选原料）。

- [ ] **Step 1: `_load_items_for_bom` 查询带上 `gram_per_unit`**

在 `blueprints/production.py` 的 `_load_items_for_bom()`，SELECT 加 `i.gram_per_unit`：

```python
    return db.execute(
        """SELECT i.id, i.name, i.unit, i.gram_per_unit, c.name AS category_name
           FROM items i JOIN categories c ON c.id = i.category_id
           ORDER BY c.name, i.name"""
    ).fetchall()
```

- [ ] **Step 2: 配方表 option 带 data 属性，加单位标签列**

在 `templates/production/product_edit.html`，把表头（28 行）改为带单位列：

```html
    <thead><tr><th>原料</th><th>每单位用量</th><th>单位</th><th>操作</th></tr></thead>
```

把现有 BOM 行（30-47 行）的 `<select>` 的 option 加 `data-gram` 属性，并新增一个单位标签单元格。整行改为：

```html
      {% for b in bom_rows %}
      <tr>
        <td>
          <input type="hidden" name="bom_row_id" value="{{ b['id'] }}" />
          <select name="bom_item_id" class="bom-item-sel">
            {% for it in items %}
            <option value="{{ it['id'] }}"
                    data-gram="{{ it['gram_per_unit'] or 0 }}"
                    data-unit="{{ it['unit'] }}"
                    {% if it['id']==b['item_id'] %}selected{% endif %}>
              {{ it['category_name'] }} / {{ it['name'] }} ({{ it['unit'] }})
            </option>
            {% endfor %}
          </select>
        </td>
        <td><input type="number" step="0.01" min="0" name="bom_qty" value="{{ b['qty_per_unit'] }}" /></td>
        <td><span class="bom-unit-label"></span></td>
        <td>
          <label class="del-flag"><input type="checkbox" name="bom_delete" value="1" /> 删除</label>
        </td>
      </tr>
      {% endfor %}
```

- [ ] **Step 3: 新增行模板同步加单位列，并写单位标签 JS**

把「新增一行」的 JS（55-71 行）整段替换为（新行模板加单位列 + 统一的标签刷新逻辑）：

```html
<script>
function refreshBomUnitLabel(sel) {
  var opt = sel.options[sel.selectedIndex];
  var gram = parseFloat(opt.getAttribute('data-gram') || '0');
  var unit = opt.getAttribute('data-unit') || '';
  var label = sel.closest('tr').querySelector('.bom-unit-label');
  label.textContent = gram > 0 ? '克' : unit;
}

document.getElementById('bom-add').addEventListener('click', function() {
  var tbody = document.getElementById('bom-body');
  var tr = document.createElement('tr');
  tr.innerHTML = `
    <td>
      <input type="hidden" name="bom_row_id" value="" />
      <select name="bom_item_id" class="bom-item-sel">
        {% for it in items %}<option value="{{ it['id'] }}" data-gram="{{ it['gram_per_unit'] or 0 }}" data-unit="{{ it['unit'] }}">{{ it['category_name'] }} / {{ it['name'] }} ({{ it['unit'] }})</option>{% endfor %}
      </select>
    </td>
    <td><input type="number" step="0.01" min="0" name="bom_qty" value="0" /></td>
    <td><span class="bom-unit-label"></span></td>
    <td><label class="del-flag"><input type="checkbox" name="bom_delete" value="1" /> 删除</label></td>
  `;
  tbody.appendChild(tr);
  var sel = tr.querySelector('.bom-item-sel');
  sel.addEventListener('change', function() { refreshBomUnitLabel(sel); });
  refreshBomUnitLabel(sel);
});

// 初始化已有行
document.querySelectorAll('.bom-item-sel').forEach(function(sel) {
  sel.addEventListener('change', function() { refreshBomUnitLabel(sel); });
  refreshBomUnitLabel(sel);
});
</script>
```

- [ ] **Step 4: 验证导入与模板渲染无误**

Run:
```bash
cd /Users/ericmr/Documents/GitHub/DailyCheck
DAILYCHECK_SECRET_KEY=dev-key-change-me /usr/bin/python3 -c "
from app import create_app
app = create_app()
print('production blueprint OK:',
      any(r.endpoint=='production.product_edit' for r in app.url_map.iter_rules()))
"
```
Expected: `production blueprint OK: True`（UI 行为在 Task 7 实操验证）

- [ ] **Step 5: Commit**

```bash
git add blueprints/production.py templates/production/product_edit.html
git commit -m "feat(production): 配方录入按原料动态显示克/原单位标签"
```

---

### Task 5: 生产提交克→库存单位换算 + 生产录入页显示克

**Files:**
- Modify: `blueprints/production.py:14`（import `grams_to_stock`）
- Modify: `blueprints/production.py:187-193`（`session()` 的 bom 查询）
- Modify: `blueprints/production.py:224-249`（`submit()` 的 bom 查询 + planned 计算）
- Modify: `templates/production/session.html`（配方卡、原料卡、JS）

**Interfaces:**
- Consumes: Task 2 的 `grams_to_stock`；Task 1 的 `gram_per_unit` 列。
- Produces: 生产提交时启用克的原料按 `grams_to_stock(qty_per_unit*output_qty, gram_per_unit)` 折算库存单位扣减；生产录入页给启用克的原料显示克数，hidden 字段传库存单位量。

- [ ] **Step 1: import `grams_to_stock`**

把 `blueprints/production.py:14` 的 import 行改为：

```python
from ._helpers import now, parse_qty, render, grams_to_stock
```

- [ ] **Step 2: `session()` 的 bom 查询带 `gram_per_unit`**

在 `session()` 路由里，bom 查询（约 187-193 行）加 `i.gram_per_unit`：

```python
            bom = db.execute(
                """SELECT b.id AS bom_id, b.qty_per_unit, b.item_id,
                          i.name AS item_name, i.unit, i.quantity AS stock,
                          i.gram_per_unit
                   FROM product_bom b JOIN items i ON i.id = b.item_id
                   WHERE b.product_id = ? ORDER BY b.id""",
                (product_id,),
            ).fetchall()
```

- [ ] **Step 3: `submit()` 的 bom 查询带 `gram_per_unit`，planned 用 `grams_to_stock`**

在 `submit()` 里，bom_rows 查询（约 224-230 行）加 `i.gram_per_unit`：

```python
    bom_rows = db.execute(
        """SELECT b.id AS bom_id, b.item_id, b.qty_per_unit,
                  i.name AS item_name, i.quantity AS stock, i.gram_per_unit
           FROM product_bom b JOIN items i ON i.id = b.item_id
           WHERE b.product_id = ? ORDER BY b.id""",
        (product_id,),
    ).fetchall()
```

把 planned 计算块（约 238-249 行）改为：先算「本批配方量」（克或库存单位），再用 `grams_to_stock` 折算成库存单位：

```python
    plan = []
    for b in bom_rows:
        gpu = float(b["gram_per_unit"] or 0)
        # qty_per_unit: 启用克时是克/件，否则是库存单位/件。
        # 先乘产出量得本批配方量，再一次性折算成库存单位（避免逐件累积误差）。
        batch_recipe_qty = float(
            (Decimal(str(b["qty_per_unit"])) * Decimal(str(output_qty)))
            .quantize(Decimal("0.01"))
        )
        planned = grams_to_stock(batch_recipe_qty, gpu)  # 库存单位
        raw = request.form.get(f"actual_{b['item_id']}", "").strip()
        actual = parse_qty(raw) if raw != "" else planned  # 表单已传库存单位
        if actual < 0:
            flash(f"原料 {b['item_name']} 实际消耗不能为负")
            return redirect(url_for("production.session", product_id=product_id))
        plan.append((int(b["item_id"]), b["item_name"], float(b["stock"]), planned, actual))
```

（其余 submit 逻辑——库存不足校验、写 production_runs/run_items/outbound_requests/stock_movements——保持不变，因为 planned/actual 现在已是库存单位。）

- [ ] **Step 4: `session.html` 配方卡显示克**

在 `templates/production/session.html`，把配方卡列表（24-31 行）改为按 `gram_per_unit` 显示克或原单位：

```html
      <ul class="recipe-list">
        {% for b in bom %}
        <li>
          <span class="recipe-name">{{ b['item_name'] }}</span>
          {% if b['gram_per_unit'] and b['gram_per_unit'] > 0 %}
          <span class="recipe-qty"><strong>{{ b['qty_per_unit'] | fmt_qty }}</strong> 克</span>
          {% else %}
          <span class="recipe-qty"><strong>{{ b['qty_per_unit'] | fmt_qty }}</strong> {{ b['unit'] }}</span>
          {% endif %}
        </li>
        {% endfor %}
      </ul>
```

- [ ] **Step 5: `session.html` 原料卡带 `data-gram-per-unit`，本批用量单位随之变**

把原料卡列表（54-75 行）改为：每行带 `data-gram-per-unit`，「本批用量」单位标签按是否启用克显示「克」或原单位：

```html
      <ul class="materials-list" id="bom-grid">
        {% for b in bom %}
        <li class="entry-row"
            data-stock="{{ b['stock'] }}"
            data-min-qty="{{ b['qty_per_unit'] }}"
            data-gram-per-unit="{{ b['gram_per_unit'] or 0 }}">
          <div class="er-name">{{ b['item_name'] }}</div>
          <div class="er-cell er-stock-cell" title="当前库存">
            <span class="er-cell-label">库存</span>
            <span class="er-cell-num er-stock-num">{{ b['stock'] | fmt_qty }}</span>
            <span class="er-cell-unit">{{ b['unit'] }}</span>
          </div>
          <div class="er-cell er-planned-cell er-planned-pill" title="按当前产出量本批共需" data-qpu="{{ b['qty_per_unit'] }}">
            <span class="er-cell-label">本批用量</span>
            <span class="er-cell-num er-planned-num planned">0.00</span>
            {% if b['gram_per_unit'] and b['gram_per_unit'] > 0 %}
            <span class="er-cell-unit">克</span>
            {% else %}
            <span class="er-cell-unit">{{ b['unit'] }}</span>
            {% endif %}
          </div>
          <input type="hidden" name="actual_{{ b['item_id'] }}" data-stock="{{ b['stock'] }}" value="0" class="actual" />
        </li>
        {% endfor %}
      </ul>
```

- [ ] **Step 6: `session.html` JS：显示克数，hidden 传库存单位**

把 `recompute()` 函数（101-126 行）整段替换为：

```js
  function recompute() {
    highlightPreset();
    var oq = parseFloat(out.value || '0');
    var anyShort = false;
    rows.forEach(function(r) {
      var qpu = parseFloat(r.querySelector('.er-planned-cell').getAttribute('data-qpu'));
      var gpu = parseFloat(r.getAttribute('data-gram-per-unit') || '0');
      // 本批配方量：启用克时是克，否则是库存单位
      var batchRecipe = Math.round(qpu * oq * 100) / 100;
      // 折算成库存单位（提交给后端 / 库存比较）
      var stockUnits = gpu > 0 ? (Math.round(batchRecipe / gpu * 100) / 100) : batchRecipe;

      // 显示：启用克的行展示克数，否则展示库存单位
      r.querySelector('.er-planned-cell .planned').textContent = batchRecipe.toFixed(2);
      // hidden actual 永远是库存单位
      var act = r.querySelector('.actual');
      act.value = stockUnits.toFixed(2);

      var stock = parseFloat(r.getAttribute('data-stock'));          // 库存单位
      var minRecipe = parseFloat(r.getAttribute('data-min-qty'));    // 每件配方量(克或库存单位)
      var minStock = gpu > 0 ? (minRecipe / gpu) : minRecipe;        // 每件折算库存单位

      // 红底：库存 < 生产 1 件所需（库存单位口径）
      r.classList.toggle('is-low', stock < minStock);
      // 红框：本批需求 > 库存（库存单位口径）
      var showShort = (stockUnits > stock);
      r.classList.toggle('is-short', showShort);
      if (showShort) anyShort = true;
    });
    btn.disabled = (oq <= 0) || anyShort;
  }
```

- [ ] **Step 7: 验证导入无误**

Run:
```bash
cd /Users/ericmr/Documents/GitHub/DailyCheck
DAILYCHECK_SECRET_KEY=dev-key-change-me /usr/bin/python3 -c "
from app import create_app
from blueprints.production import grams_to_stock
print('submit conversion wired, grams_to_stock(1440,1000)=', grams_to_stock(1440,1000))
"
```
Expected: `submit conversion wired, grams_to_stock(1440,1000)= 1.44`

- [ ] **Step 8: Commit**

```bash
git add blueprints/production.py templates/production/session.html
git commit -m "feat(production): 生产提交克→库存单位换算，录入页按克显示用量"
```

---

### Task 6: 集成测试（Layer 2，pytest + Flask test client）

**Files:**
- Create: `tests/conftest.py`
- Create: `tests/test_production_grams.py`

**Interfaces:**
- Consumes: Task 1-5 全部成果。
- Produces: `logged_client` 夹具（临时仓库 db + 直设 session 登录，返回 `(client, wh_db_path)`）；跨边界断言测试。

**测试夹具设计说明：** 用 `monkeypatch` 把 `db.MASTER_DB` / `db.WAREHOUSE_DB_DIR` 指向临时目录，建临时 master.db + 仓库 db，插入 admin 用户（is_admin=1）+ 仓库（db_path 存绝对路径，`BASE_DIR / 绝对路径` 仍得绝对路径）+ 绑定，再用 `client.session_transaction()` 直接写 session（绕过密码校验，规避 Python 3.9 无 scrypt 的问题）。

- [ ] **Step 1: 写 conftest 夹具**

创建 `tests/conftest.py`：

```python
"""集成测试夹具：临时仓库 db + 直设 session 登录。

绕过密码校验（直接写 session），规避 Python 3.9 无 hashlib.scrypt
导致 werkzeug 默认 hash 不可用的问题。
"""
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest


@pytest.fixture
def logged_client(tmp_path, monkeypatch):
    import db as db_module
    from db import init_master_db, init_warehouse_db

    master_path = tmp_path / "master.db"
    wh_dir = tmp_path / "warehouses"
    wh_dir.mkdir()
    wh_path = wh_dir / "wh_test.db"

    # 让 db 模块用临时路径
    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_dir)

    init_master_db()
    init_warehouse_db(wh_path)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    m = sqlite3.connect(master_path)
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (1, 'admin', 'x', 1, ?)", (ts,))
    # db_path 存绝对路径：auth.py 用 BASE_DIR / db_path，绝对路径右值会覆盖
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, created_at) "
        "VALUES (1, 'wh_test', '测试仓', ?, ?)", (str(wh_path), ts))
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

    return client, wh_path


def _wh(wh_path):
    conn = sqlite3.connect(wh_path)
    conn.row_factory = sqlite3.Row
    return conn
```

- [ ] **Step 2: 写失败的集成测试**

创建 `tests/test_production_grams.py`：

```python
"""Layer 2: 生产录入克换算集成测试（跨边界）。"""
import sqlite3
from datetime import datetime

from tests.conftest import _wh


def _seed_item(wh_path, name, unit, qty, gram_per_unit):
    """直接在仓库 db 建一个物品，返回其 id。category_id 取第一个固定品类。"""
    conn = _wh(wh_path)
    cat_id = conn.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()["id"]
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sku = f"T-{name}"
    cur = conn.execute(
        "INSERT INTO items (sku, name, category_id, quantity, safety_stock, unit_cost, unit, gram_per_unit, updated_at) "
        "VALUES (?, ?, ?, ?, 0, 0, ?, ?, ?)",
        (sku, name, cat_id, qty, unit, gram_per_unit, ts))
    item_id = cur.lastrowid
    conn.commit()
    conn.close()
    return item_id


def _seed_product_with_bom(wh_path, pname, bom):
    """建产品 + 配方。bom = [(item_id, qty_per_unit), ...]，返回 product_id。"""
    conn = _wh(wh_path)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "INSERT INTO products (name, unit, note, created_at) VALUES (?, '件', '', ?)",
        (pname, ts))
    pid = cur.lastrowid
    for item_id, qpu in bom:
        conn.execute(
            "INSERT INTO product_bom (product_id, item_id, qty_per_unit) VALUES (?, ?, ?)",
            (pid, item_id, qpu))
    conn.commit()
    conn.close()
    return pid


def test_gram_item_deducts_stock_units(logged_client):
    """启用克的原料：配方 480 克/件，产出 3 件 → 扣 1.44 袋（1 袋=1000 克）。"""
    client, wh_path = logged_client
    milk = _seed_item(wh_path, "牛乳", "袋", qty=50, gram_per_unit=1000)
    pid = _seed_product_with_bom(wh_path, "焦糖海盐", [(milk, 480)])

    resp = client.post("/production/submit", data={
        "product_id": str(pid),
        "output_qty": "3",
        "note": "",
        f"actual_{milk}": "",  # 留空 → 服务端用 planned
    }, follow_redirects=False)
    assert resp.status_code in (302, 303)

    conn = _wh(wh_path)
    # 库存：50 - 1.44 = 48.56 袋
    stock = conn.execute("SELECT quantity FROM items WHERE id=?", (milk,)).fetchone()["quantity"]
    assert round(stock, 2) == 48.56
    # production_run_items.actual_qty 是库存单位
    pri = conn.execute("SELECT actual_qty FROM production_run_items WHERE item_id=?", (milk,)).fetchone()
    assert round(pri["actual_qty"], 2) == 1.44
    # outbound_requests 写库存单位
    ob = conn.execute(
        "SELECT requested_quantity FROM outbound_requests WHERE item_id=? AND reason LIKE '生产领料%'",
        (milk,)).fetchone()
    assert round(ob["requested_quantity"], 2) == 1.44
    # stock_movements 写库存单位（负）
    sm = conn.execute(
        "SELECT delta FROM stock_movements WHERE item_id=? AND action='生产消耗'",
        (milk,)).fetchone()
    assert round(sm["delta"], 2) == -1.44
    conn.close()


def test_non_gram_item_unchanged(logged_client):
    """未启用克：配方 2 条/件，产出 3 件 → 扣 6 条（原逻辑不变）。"""
    client, wh_path = logged_client
    cone = _seed_item(wh_path, "甜筒", "条", qty=100, gram_per_unit=0)
    pid = _seed_product_with_bom(wh_path, "甜筒产品", [(cone, 2)])

    resp = client.post("/production/submit", data={
        "product_id": str(pid), "output_qty": "3", "note": "", f"actual_{cone}": "",
    })
    assert resp.status_code in (302, 303)

    conn = _wh(wh_path)
    stock = conn.execute("SELECT quantity FROM items WHERE id=?", (cone,)).fetchone()["quantity"]
    assert round(stock, 2) == 94.0  # 100 - 6
    conn.close()


def test_mixed_recipe(logged_client):
    """混合配方：克原料 + 非克原料各扣各的。"""
    client, wh_path = logged_client
    milk = _seed_item(wh_path, "牛乳2", "袋", qty=10, gram_per_unit=1000)
    cone = _seed_item(wh_path, "甜筒2", "条", qty=20, gram_per_unit=0)
    pid = _seed_product_with_bom(wh_path, "混合品", [(milk, 500), (cone, 1)])

    resp = client.post("/production/submit", data={
        "product_id": str(pid), "output_qty": "2", "note": "",
        f"actual_{milk}": "", f"actual_{cone}": "",
    })
    assert resp.status_code in (302, 303)

    conn = _wh(wh_path)
    milk_stock = conn.execute("SELECT quantity FROM items WHERE id=?", (milk,)).fetchone()["quantity"]
    cone_stock = conn.execute("SELECT quantity FROM items WHERE id=?", (cone,)).fetchone()["quantity"]
    assert round(milk_stock, 2) == 9.0   # 10 - (500*2/1000=1.0)
    assert round(cone_stock, 2) == 18.0  # 20 - 2
    conn.close()


def test_rollback_restores_stock(logged_client):
    """生产后回退：库存精确还原（验证零改动回退路径正确）。"""
    client, wh_path = logged_client
    milk = _seed_item(wh_path, "牛乳3", "袋", qty=5, gram_per_unit=1000)
    pid = _seed_product_with_bom(wh_path, "回退品", [(milk, 480)])

    client.post("/production/submit", data={
        "product_id": str(pid), "output_qty": "3", "note": "", f"actual_{milk}": "",
    })
    conn = _wh(wh_path)
    run_id = conn.execute("SELECT id FROM production_runs ORDER BY id DESC LIMIT 1").fetchone()["id"]
    after_submit = conn.execute("SELECT quantity FROM items WHERE id=?", (milk,)).fetchone()["quantity"]
    conn.close()
    assert round(after_submit, 2) == 3.56  # 5 - 1.44

    resp = client.post(f"/production/runs/{run_id}/rollback")
    assert resp.status_code in (302, 303)

    conn = _wh(wh_path)
    restored = conn.execute("SELECT quantity FROM items WHERE id=?", (milk,)).fetchone()["quantity"]
    assert round(restored, 2) == 5.0  # 精确还原
    conn.close()
```

- [ ] **Step 3: 运行测试确认失败（夹具未就绪时）→ 实则验证全流程**

Run: `cd /Users/ericmr/Documents/GitHub/DailyCheck && /usr/bin/python3 -m pytest tests/test_production_grams.py -v`
Expected: 若 Task 1-5 已实现，应全部 PASS（4 passed）。若有红，按报错定位到对应 Task 的实现修正（不要改测试期望值，期望值由设计推导：1440/1000=1.44 等）。

- [ ] **Step 4: 跑全量测试套件**

Run: `cd /Users/ericmr/Documents/GitHub/DailyCheck && /usr/bin/python3 -m pytest tests/ -v`
Expected: PASS（Layer 1 的 7 个 + Layer 2 的 4 个 = 11 passed）

- [ ] **Step 5: Commit**

```bash
git add tests/conftest.py tests/test_production_grams.py
git commit -m "test(production): 克换算集成测试（扣减/混合/回退跨边界）"
```

---

### Task 7: 实际操作验证（Layer 3，独立 agent + preview 工具）

**Files:** 无（纯验证，不改代码）

**Interfaces:**
- Consumes: Task 1-6 全部成果（应用可启动、测试全绿）。

**这一步必须派独立 agent 执行**（与写代码上下文隔离），用 preview 工具真实点击。主会话负责派发与汇总，不自己跑实操。

- [ ] **Step 1: 确认应用可启动、测试全绿（派发前的前置门槛）**

Run:
```bash
cd /Users/ericmr/Documents/GitHub/DailyCheck && /usr/bin/python3 -m pytest tests/ -v 2>&1 | tail -5
```
Expected: `11 passed`。未全绿则不派发，先回到对应 Task 修复。

- [ ] **Step 2: 派发独立验证 agent**

用 Agent 工具（subagent_type: `general-purpose` 或 `e2e-runner`）派发，prompt 至少包含：

```
任务：用 preview 工具对 DailyCheck 生产录入克换算特性做实际操作验证。应用启动方式见 README（DAILYCHECK_SECRET_KEY=dev-key-change-me RUNAPP=1 python3 app.py，端口 5001）。登录 admin/admin123，选「中央仓」。

验证步骤（每步截图留证）：
1. 进物品编辑页，给一个原料（如牛乳）设「每单位克重」=1000，保存。重开编辑页确认 1000 留存。
2. 进生产「产品」→编辑某产品配方，把该原料行用量改为 480，确认单位标签显示「克」。保存。
3. 进生产录入页，选该产品，产出量点「3」。
   - 截图验证「本批用量」对该原料显示 1440 克（480×3）。
4. 记录提交前该原料库存（库存单位，如 X 袋）。提交生产。
5. 回库存/物品页，验证库存变为 X - 1.44（袋）。截图。
6. 进生产历史，对刚才的记录点回退，验证库存还原为 X。截图。

交付：每步截图 + 一句话结论（通过/不通过）。发现不符立即报告具体现象，不要自行改代码。
```

- [ ] **Step 3: 汇总验证结论**

收到 agent 的截图与结论后，在主会话汇总：克显示正确 / 库存按库存单位扣减 / 回退精确还原 三项是否全部通过。任一不通过 → 定位对应 Task 修复后重跑 Task 6 + 重新派发 Task 7。

- [ ] **Step 4: 收尾提交（如验证中产生了 README 或 launch 配置等辅助改动）**

```bash
git add -A && git commit -m "chore(production): 克换算实操验证收尾" || echo "无额外改动"
```

---

## 成功标准（全部满足才算完成）

1. Layer 1（7）+ Layer 2（4）pytest 全绿：`/usr/bin/python3 -m pytest tests/ -v` → 11 passed
2. 独立 agent 实操截图证明：生产页给工人显示**克**、库存按**库存单位**扣减（48.56 袋而非克）、回退**精确还原**
3. 出库、库存查阅、回退、删除、CSV 导出**行为未变**（由 Task 6 回退测试 + Layer 2 断言间接保证）
4. 所有改动已分任务提交到 `newtool` 分支
