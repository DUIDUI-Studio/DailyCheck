# 汇总页面 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a summary page (`/summary`) showing total inbound, remaining stock, consumption rate, total inbound value, and total stock value.

**Architecture:** A new Flask route + Jinja2 template + DB migration for `unit_cost` on `items` table. Existing item create/edit forms gain a unit_cost input. Existing `.cols-4` grid style gets a `.cols-5` companion with mobile 2-2-1 breakpoint.

**Tech Stack:** Flask, SQLite, Jinja2, CSS grid

---

### Task 1: DB migration — add unit_cost to items

**Files:**
- Modify: `app.py:114-134` (init_db migration section)

- [ ] **Add unit_cost column migration in init_db()**

After the existing column migration blocks (line 116-125), add:

```python
items_cols = [r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()]
if "unit_cost" not in items_cols:
    conn.execute("ALTER TABLE items ADD COLUMN unit_cost REAL NOT NULL DEFAULT 0")
```

- [ ] **Run app to verify migration works**

```bash
cd /Users/ericmr/Documents/GitHub/DailyCheck && python3 -c "
import app; app.init_db()
import sqlite3
conn = sqlite3.connect('inventory.db')
cols = [r[1] for r in conn.execute('PRAGMA table_info(items)').fetchall()]
assert 'unit_cost' in cols, 'unit_cost missing'
print('OK: unit_cost column exists')
conn.close()
"
```

Expected output: `OK: unit_cost column exists`

- [ ] **Commit**

```bash
cd /Users/ericmr/Documents/GitHub/DailyCheck
git add app.py
git commit -m "feat(db): add unit_cost column to items table"
```

---

### Task 2: Add unit_cost to item creation form

**Files:**
- Modify: `templates/items.html:5-15`

- [ ] **Add unit_cost input field**

Insert between the safety_stock input and unit input (line 13-14):

```html
  <input name="unit_cost" type="number" min="0" step="0.01" placeholder="进货单价（¥）" />
```

Result block (lines 5-16):

```html
<form method="post" class="grid cols-3">
  <input name="name" placeholder="品项名称" required />
  <select name="category_id" required>
    <option value="">选择品类</option>
    {% for c in categories %}
    <option value="{{ c.id }}">{{ c.name }}</option>
    {% endfor %}
  </select>
  <input name="quantity" type="number" min="0" placeholder="初始库存" />
  <input name="safety_stock" type="number" min="0" placeholder="安全库存" />
  <input name="unit_cost" type="number" min="0" step="0.01" placeholder="进货单价（¥）" />
  <input name="unit" placeholder="单位（默认件）" />
  <button type="submit">新增库存品</button>
</form>
```

- [ ] **Update POST handler to save unit_cost**

In `app.py` line 200-222, read `unit_cost` from the form. After `quantity = ...` line:

```python
quantity = int(request.form.get("quantity", "0") or 0)
safety_stock = int(request.form.get("safety_stock", "0") or 0)
unit_cost = float(request.form.get("unit_cost", "0") or 0)
unit = request.form.get("unit", "件").strip() or "件"
```

Then in the INSERT (line 213), add `unit_cost` to both the column list and values:

```python
db.execute(
    """
    INSERT INTO items (sku, name, category_id, quantity, safety_stock, unit_cost, unit, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """,
    (gen_sku(), name, int(category_id), quantity, safety_stock, unit_cost, unit, now()),
)
```

- [ ] **Commit**

```bash
cd /Users/ericmr/Documents/GitHub/DailyCheck
git add app.py templates/items.html
git commit -m "feat(items): add unit_cost field to item creation form"
```

---

### Task 3: Add unit_cost to item edit form

**Files:**
- Modify: `templates/edit_item.html:5-17`

- [ ] **Add unit_cost input field to edit form**

Insert after safety_stock input (line 14 after change):

```html
  <input name="unit_cost" type="number" min="0" step="0.01" placeholder="进货单价（¥）" value="{{ item.unit_cost or 0 }}" />
```

Result block (lines 5-18):

```html
<form method="post" class="grid cols-3">
  <input name="name" placeholder="品项名称" value="{{ item.name }}" required />
  <select name="category_id" required>
    <option value="">选择品类</option>
    {% for c in categories %}
    <option value="{{ c.id }}" {% if c.id == item.category_id %}selected{% endif %}>{{ c.name }}</option>
    {% endfor %}
  </select>
  <input name="quantity" type="number" min="0" placeholder="库存数量" value="{{ item.quantity }}" />
  <input name="safety_stock" type="number" min="0" placeholder="安全库存" value="{{ item.safety_stock }}" />
  <input name="unit_cost" type="number" min="0" step="0.01" placeholder="进货单价（¥）" value="{{ item.unit_cost or 0 }}" />
  <input name="unit" placeholder="单位（默认件）" value="{{ item.unit }}" />
  <a href="{{ url_for('items') }}" class="btn-cancel">取消</a>
  <button type="submit">保存修改</button>
</form>
```

- [ ] **Update edit POST handler to save unit_cost**

In `app.py` `edit_item()` function, after the `safety_stock = ...` line:

```python
safety_stock = int(request.form.get("safety_stock", "0") or 0)
unit_cost = float(request.form.get("unit_cost", "0") or 0)
```

Then in the UPDATE (line 258-264), add `unit_cost = ?` to SET clause and the value:

```python
db.execute(
    """
    UPDATE items
    SET name = ?, category_id = ?, quantity = ?, safety_stock = ?, unit_cost = ?, unit = ?, updated_at = ?
    WHERE id = ?
    """,
    (name, int(category_id), quantity, safety_stock, unit_cost, unit, now(), item_id),
)
```

- [ ] **Commit**

```bash
cd /Users/ericmr/Documents/GitHub/DailyCheck
git add app.py templates/edit_item.html
git commit -m "feat(items): add unit_cost field to item edit form"
```

---

### Task 4: Add summary route in app.py

**Files:**
- Modify: `app.py` (insert new route after line 178, before `@app.route("/categories"...)`)

- [ ] **Add `GET /summary` route**

Insert after the `dashboard()` function (before the blank line before `@app.route("/categories")`):

```python

@app.route("/summary")
def summary():
    db = get_db()

    # Total inbound quantity
    total_inbound = db.execute(
        "SELECT COALESCE(SUM(delta), 0) AS c FROM stock_movements WHERE action = '补货入库'"
    ).fetchone()["c"]

    # Total current stock quantity
    total_stock = db.execute(
        "SELECT COALESCE(SUM(quantity), 0) AS c FROM items"
    ).fetchone()["c"]

    # Consumption rate
    consumption_rate = round(
        (total_inbound - total_stock) / total_inbound * 100, 1
    ) if total_inbound > 0 else 0

    # Total inbound value (unit_cost × inbound qty per item)
    total_inbound_value = db.execute(
        """
        SELECT COALESCE(SUM(i.unit_cost * sub.inbound_qty), 0) AS c
        FROM (
            SELECT m.item_id, SUM(m.delta) AS inbound_qty
            FROM stock_movements m
            WHERE m.action = '补货入库'
            GROUP BY m.item_id
        ) sub
        JOIN items i ON i.id = sub.item_id
        """
    ).fetchone()["c"]

    # Total stock value
    total_stock_value = db.execute(
        "SELECT COALESCE(SUM(quantity * unit_cost), 0) AS c FROM items"
    ).fetchone()["c"]

    # Per-category stats
    category_stats = db.execute(
        """
        SELECT c.name AS category_name,
               COALESCE(SUM(i.quantity), 0) AS stock_qty,
               COALESCE(SUM(i.quantity * i.unit_cost), 0) AS stock_value
        FROM categories c
        LEFT JOIN items i ON i.category_id = c.id
        GROUP BY c.id, c.name
        ORDER BY c.id
        """
    ).fetchall()

    # Category inbound subtotals (separate query for clarity)
    cat_inbound = {
        r["category_name"]: r["total_inbound"]
        for r in db.execute(
            """
            SELECT c.name AS category_name,
                   COALESCE(SUM(m.delta), 0) AS total_inbound
            FROM categories c
            LEFT JOIN items i ON i.category_id = c.id
            LEFT JOIN stock_movements m ON m.item_id = i.id AND m.action = '补货入库'
            GROUP BY c.id, c.name
            ORDER BY c.id
            """
        ).fetchall()
    }

    # Merge inbound qty into category_stats and compute consumption rate per category
    enriched_stats = []
    for row in category_stats:
        ib = cat_inbound.get(row["category_name"], 0)
        consumed = ib - row["stock_qty"]
        rate = round(consumed / ib * 100, 1) if ib > 0 else 0
        enriched_stats.append({
            "category_name": row["category_name"],
            "total_inbound": ib,
            "stock_qty": row["stock_qty"],
            "consumed": consumed,
            "consumption_rate": rate,
            "stock_value": row["stock_value"],
        })

    # Top 10 most consumed items
    top_consumed = db.execute(
        """
        SELECT i.name AS item_name, c.name AS category_name,
               ABS(SUM(m.delta)) AS consumed_qty,
               i.unit,
               ROUND(ABS(SUM(m.delta)) * i.unit_cost, 2) AS consumed_value
        FROM stock_movements m
        JOIN items i ON i.id = m.item_id
        JOIN categories c ON c.id = i.category_id
        WHERE m.action = '出库'
        GROUP BY m.item_id
        ORDER BY consumed_qty DESC
        LIMIT 10
        """
    ).fetchall()

    return render_template(
        "summary.html",
        total_inbound=total_inbound,
        total_stock=total_stock,
        consumption_rate=consumption_rate,
        total_inbound_value=round(total_inbound_value, 2),
        total_stock_value=round(total_stock_value, 2),
        category_stats=enriched_stats,
        top_consumed=top_consumed,
    )
```

- [ ] **Commit**

```bash
cd /Users/ericmr/Documents/GitHub/DailyCheck
git add app.py
git commit -m "feat(api): add /summary route with aggregation queries"
```

---

### Task 5: Create summary.html template

**Files:**
- Create: `templates/summary.html`

- [ ] **Write summary.html**

```html
{% extends 'base.html' %}
{% block content %}
<h2>汇总</h2>

<div class="grid cols-5">
  <section class="card">
    <p>总进货量</p>
    <strong>{{ total_inbound }} <small>件</small></strong>
  </section>
  <section class="card">
    <p>当前库存</p>
    <strong>{{ total_stock }} <small>件</small></strong>
  </section>
  <section class="card">
    <p>消耗率</p>
    <strong>{{ consumption_rate }}<small>%</small></strong>
  </section>
  <section class="card">
    <p>进货总金额</p>
    <strong><small>¥</small>{{ "{:,.2f}".format(total_inbound_value) }}</strong>
  </section>
  <section class="card">
    <p>库存总价值</p>
    <strong><small>¥</small>{{ "{:,.2f}".format(total_stock_value) }}</strong>
  </section>
</div>

<h3>按品类统计</h3>
<div class="table-scroll">
<table>
  <tr>
    <th>品类</th>
    <th>总进货</th>
    <th>当前库存</th>
    <th>已消耗</th>
    <th>消耗率</th>
    <th>品类价值</th>
  </tr>
  {% for cat in category_stats %}
  <tr>
    <td>{{ cat.category_name }}</td>
    <td>{{ cat.total_inbound }} 件</td>
    <td>{{ cat.stock_qty }} 件</td>
    <td>{{ cat.consumed }} 件</td>
    <td>{{ cat.consumption_rate }}%</td>
    <td>¥{{ "{:,.2f}".format(cat.stock_value) }}</td>
  </tr>
  {% endfor %}
</table>
</div>

<h3>消耗排行 Top 10</h3>
<div class="table-scroll">
<table>
  <tr>
    <th>排名</th>
    <th>品项</th>
    <th>品类</th>
    <th>消耗量</th>
    <th>消耗金额</th>
  </tr>
  {% for item in top_consumed %}
  <tr>
    <td>{{ loop.index }}</td>
    <td>{{ item.item_name }}</td>
    <td>{{ item.category_name }}</td>
    <td>{{ item.consumed_qty }} {{ item.unit }}</td>
    <td>¥{{ "{:,.2f}".format(item.consumed_value) }}</td>
  </tr>
  {% else %}
  <tr><td colspan="5" style="text-align:center;color:var(--muted)">暂无消耗记录</td></tr>
  {% endfor %}
</table>
</div>
{% endblock %}
```

- [ ] **Commit**

```bash
cd /Users/ericmr/Documents/GitHub/DailyCheck
git add templates/summary.html
git commit -m "feat(template): create summary.html template"
```

---

### Task 6: Add nav link and CSS styles

**Files:**
- Modify: `templates/base.html`
- Modify: `static/style.css`

- [ ] **Add "汇总" to sidebar nav**

In `templates/base.html`, after the categories link line:

```html
    <a href="{{ url_for('categories') }}">品类管理</a>
    <a href="{{ url_for('summary') }}">汇总</a>
```

- [ ] **Add "汇总" to mobile nav**

In `templates/base.html`, the mobile nav currently has 6 links. Add summary after inventory:

```html
      <a href="{{ url_for('inventory') }}">库存</a>
      <a href="{{ url_for('summary') }}">汇总</a>
```

Note: with 7 links the mobile nav will wrap to 2 rows (5+2). That's fine — each link is a grid cell.

- [ ] **Add cols-5 grid and table-scroll CSS**

Append to `static/style.css` before the `@media` block (around line 238):

```css
.cols-5 { grid-template-columns: 1fr 1fr; }

.card small { font-size: 14px; font-weight: 400; }

.table-scroll {
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
  margin-bottom: 14px;
}
.table-scroll table { display: table; min-width: 100%; }
```

- [ ] **Add desktop cols-5 and mobile 2-2-1 layout**

Inside the `@media (min-width: 961px)` block, add:

```css
  .cols-5 { grid-template-columns: repeat(5, minmax(0, 1fr)); }
```

After the media block (line 254), add mobile 2-2-1 layout for cols-5:

```css
@media (max-width: 960px) {
  .cols-5 { grid-template-columns: 1fr 1fr; }
  .cols-5 .card:last-child:nth-child(5) { grid-column: 1 / -1; }
}
```

- [ ] **Commit**

```bash
cd /Users/ericmr/Documents/GitHub/DailyCheck
git add templates/base.html static/style.css
git commit -m "feat(ui): add summary nav link and cols-5 grid with mobile 2-2-1 layout"
```

---

### Task 7: Verify everything works

- [ ] **Restart dev server and check the page**

The Flask dev server auto-reloads. Navigate to `http://localhost:56970/summary` in the preview.

- [ ] **Verify all sections render**

Check: 5 metric cards at top, category stats table, and top consumed table all render without errors.

- [ ] **Final commit if any fixes needed**

```bash
cd /Users/ericmr/Documents/GitHub/DailyCheck
git add -A
git commit -m "fix: adjustments after summary page verification"
```
