# Foundation Updates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 收紧 `/items` 可见权限、增加品类芯片筛选与每单位克重 UI;为 `/summary` 增加时间维度筛选、库存周转率、CSV 导出三块内容。

**Architecture:** 复用 `/inventory` 现有的 `.cat-bar/.cat-chip` CSS 与 JS 模式(纯前端切换)。周转率用"消耗金额 ÷ 起止两点平均库存金额",平均库存从 `stock_movements` 反推窗口起点的 quantity。CSV 导出走 UTF-8 BOM。系统日志不在本 PR(已记 v2)。

**Tech Stack:** Python 3 + Flask 3 + SQLite(多仓库)+ pytest;前端原生 JS,移动端优先 CSS。

## Global Constraints

- 数量精度统一 2 位小数,沿用 `parse_qty` / `Decimal('0.01')` 量化。
- 时间筛选 SQL 起点:`7d` 用 `datetime('now','-7 days')`;`month` 用 `created_at LIKE 'YYYY-MM%'`;`all` 不加 WHERE。
- 库存金额**不受时间筛选**(`SUM(items.quantity * items.unit_cost)`)。
- 周转率:`turnover = consumed_value / avg_stock_value`,`turnover_days = avg_stock_value / (consumed_value / window_days)`。
- 反推 `qty_start = items.quantity - SUM(stock_movements.delta WHERE created_at >= window_start)` — 依赖代码纪律(`stock_movements` 必须与 `items.quantity` 同步)。
- 提交信息中文 `<type>(scope): <desc>` 格式,无 attribution。
- 测试命令:`cd /Users/ericmr/Documents/GitHub/DailyCheck && ./venv/bin/python -m pytest tests/ -v`(.venv 已在本次预览启动时创建)。

---

## File Structure

| 文件 | 职责 | 改动 |
|------|------|------|
| `blueprints/items.py` | 物品 CRUD | `items_list` GET/POST 与 `edit_item` 升级权限;`POST` 加 gram_per_unit 负值校验;SELECT 加 `fixed_categories_in_clause` |
| `templates/items.html` | 物品列表/新增页 | 顶部 chip bar;新增表单补 gram_per_unit 输入;列表加克重列 + data-cat 属性 |
| `templates/edit_item.html` | 物品编辑页 | 不动(已有 gram_per_unit 输入) |
| `templates/base.html` | 全局布局 | 侧栏与移动 nav 的"品类与品项"链接包 `{% if current_role != 'staff' %}`;CSS 版本 `?v=10` → `?v=11` |
| `blueprints/core.py` | dashboard / summary | `summary()` 增加 `range` 参数、时间筛选 SQL、`turnover` / `turnover_days` 计算、按品类周转率;`turnover_days` 边界返回 `None`(模板渲染为 `—`) |
| `templates/summary.html` | 汇总页 | 时间范围芯片(URL 提交);卡片区"营业额"替换为"库存周转率 + 可售天数";按品类表加周转率列;导出按钮链接 `/summary/export` |
| `blueprints/reports.py` | 报表 / 导出 | 新增 `GET /summary/export` 路由,三段 CSV 输出 |
| `static/style.css` | 全局样式 | 新增 `.summary-range` / `.turnover-card` 样式;版本 bump 与 base.html 一致 |
| `tests/conftest.py` | 测试夹具 | 已存在(直设 session 登录 + 临时仓库 db),复用 |
| `tests/test_items_route.py` | 物品路由测试 | 新增:权限、gram_per_unit 校验、芯片切换 |
| `tests/test_summary_route.py` | 汇总测试 | 新增:range 默认 / month / all、周转率公式、反推库存、空表边界 |
| `tests/test_summary_export.py` | 导出测试 | 新增:UTF-8 BOM、文件名、三段内容 |

---

### Task 1: `/items` 权限升级 + gram_per_unit 负值校验

**Files:**
- Modify: `blueprints/items.py:17-20`(items_list 装饰器)
- Modify: `blueprints/items.py:67-89`(edit_item 装饰器)
- Modify: `blueprints/items.py:22-48`(POST 增加 gram_per_unit / unit_cost 校验)

**Interfaces:**
- Produces:`/items` 和 `/items/<id>/edit` 都拒绝 staff;`POST /items` 拒绝 `gram_per_unit<0` 或 `unit_cost<0`,flash 报错后 redirect。

- [ ] **Step 1: 写失败测试**

在 `tests/` 下新建 `test_items_route.py`:

```python
"""物品路由测试:权限收紧 + gram_per_unit / unit_cost 校验。"""
import sqlite3
from datetime import datetime

import pytest


def _seed_master_user(master_path, *, is_admin=True, role="manager"):
    """在临时 master.db 插入用户 + 仓库绑定。is_admin=True 时跳过 role 检查。"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    m = sqlite3.connect(master_path)
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (1, 'admin', 'x', ?, ?)", (1 if is_admin else 0, ts))
    if not is_admin:
        # role 决定访问权限;role='staff' 看不到 /items
        m.execute(
            "INSERT INTO warehouses (id, code, name, db_path, created_at) "
            "VALUES (1, 'wh_t', 'T', 'unused', ?)", (ts,))
        m.execute(
            "INSERT INTO warehouse_users (user_id, warehouse_id, role) "
            "VALUES (1, 1, ?)", (role,))
    m.commit()
    m.close()


def test_staff_gets_403_on_items(tmp_path, monkeypatch):
    import db as db_module
    from db import init_master_db, init_warehouse_db
    master_path = tmp_path / "master.db"
    wh_path = tmp_path / "wh.db"
    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", tmp_path)
    init_master_db()
    init_warehouse_db(wh_path)
    _seed_master_user(master_path, is_admin=False, role="staff")

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = 1
        s["warehouse_id"] = 1

    resp = client.get("/items")
    assert resp.status_code == 403


def test_manager_can_view_items(tmp_path, monkeypatch):
    import db as db_module
    from db import init_master_db, init_warehouse_db
    master_path = tmp_path / "master.db"
    wh_path = tmp_path / "wh.db"
    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", tmp_path)
    init_master_db()
    init_warehouse_db(wh_path)
    _seed_master_user(master_path, is_admin=False, role="manager")

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = 1
        s["warehouse_id"] = 1

    resp = client.get("/items")
    assert resp.status_code == 200


def test_negative_gram_per_unit_rejected(tmp_path, monkeypatch):
    import db as db_module
    from db import init_master_db, init_warehouse_db
    master_path = tmp_path / "master.db"
    wh_path = tmp_path / "wh.db"
    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", tmp_path)
    init_master_db()
    init_warehouse_db(wh_path)
    _seed_master_user(master_path, is_admin=False, role="manager")

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = 1
        s["warehouse_id"] = 1

    # 取一个有效 category_id
    conn = sqlite3.connect(wh_path)
    cat_id = conn.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()[0]
    conn.close()

    resp = client.post("/items", data={
        "name": "test",
        "category_id": str(cat_id),
        "quantity": "0",
        "safety_stock": "0",
        "unit_cost": "0",
        "unit": "件",
        "gram_per_unit": "-5",
    }, follow_redirects=False)
    assert resp.status_code in (302, 303)

    conn = sqlite3.connect(wh_path)
    cnt = conn.execute("SELECT COUNT(*) FROM items WHERE name='test'").fetchone()[0]
    conn.close()
    assert cnt == 0  # 负值被拒,未写入
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /Users/ericmr/Documents/GitHub/DailyCheck && ./venv/bin/python -m pytest tests/test_items_route.py -v`

Expected: 3 个测试均 FAIL(`test_staff_gets_403_on_items` → 200 而非 403;其他因 /items 路由尚未升级,POST 仍接受负值)。

- [ ] **Step 3: 升级权限装饰器**

打开 `blueprints/items.py`,把:

```python
@bp.route("/items", methods=["GET", "POST"])
@require_login
@require_role("staff")
def items_list():
```

改为:

```python
@bp.route("/items", methods=["GET", "POST"])
@require_login
@require_role("manager")
def items_list():
```

把 `edit_item` 的 `@require_login` 改为 `@require_role("manager")`(在第 67 行附近):

```python
@bp.route("/items/<int:item_id>/edit", methods=["GET", "POST"])
@require_login
@require_role("manager")
def edit_item(item_id: int):
```

`delete_item` 的 `@require_role("staff")` **不动**(staff 通过 URL 也无法访问,但保持代码层兜底)。

- [ ] **Step 4: 增加 `gram_per_unit` / `unit_cost` 负值校验**

在 `items_list()` 的 POST 分支(约 22-33 行),`quantity = ...` 那行之后加:

```python
        if gram_per_unit < 0:
            flash("每单位克重不能为负数")
            return redirect(url_for("items.items_list"))
        if unit_cost < 0:
            flash("进货单价不能为负数")
            return redirect(url_for("items.items_list"))
```

(`gram_per_unit` 与 `unit_cost` 在原代码已经从 request.form 读取,引用这两行前要先确认上文有 `gram_per_unit = parse_qty(...)` — 检查原代码:见 `blueprints/items.py:29`,已存在。)

同样的校验块也加到 `edit_item()` 的 POST 分支(约 70-77 行之后):

```python
        if gram_per_unit < 0:
            flash("每单位克重不能为负数")
            return redirect(url_for("items.edit_item", item_id=item_id))
        if unit_cost < 0:
            flash("进货单价不能为负数")
            return redirect(url_for("items.edit_item", item_id=item_id))
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd /Users/ericmr/Documents/GitHub/DailyCheck && ./venv/bin/python -m pytest tests/test_items_route.py -v`

Expected: 3 passed。

- [ ] **Step 6: Commit**

```bash
git add blueprints/items.py tests/test_items_route.py
git commit -m "feat(items): 权限收紧到 manager，校验 gram_per_unit/unit_cost 负值"
```

---

### Task 2: `/items` SELECT 加 `fixed_categories_in_clause`

**Files:**
- Modify: `blueprints/items.py:50-59`(GET 分支查询)

**Interfaces:**
- Produces:`GET /items` 的 items 查询与 `/inventory` 同源 — 只显示 9 个固定品类的品项。

- [ ] **Step 1: 改 SELECT**

在 `items_list()` 的 GET 分支(约 50-59 行),把:

```python
    placeholders, params = fixed_categories_in_clause()
    categories_data = db.execute(
        f"SELECT id, name, description FROM categories WHERE name IN ({placeholders}) ORDER BY name",
        params,
    ).fetchall()
    rows = db.execute(
        """SELECT i.*, c.name AS category_name
           FROM items i JOIN categories c ON c.id = i.category_id
           ORDER BY i.id DESC"""
    ).fetchall()
```

改为:

```python
    placeholders, params = fixed_categories_in_clause()
    categories_data = db.execute(
        f"SELECT id, name, description FROM categories WHERE name IN ({placeholders}) ORDER BY name",
        params,
    ).fetchall()
    rows = db.execute(
        f"""SELECT i.*, c.name AS category_name
            FROM items i JOIN categories c ON c.id = i.category_id
            WHERE c.name IN ({placeholders})
            ORDER BY i.id DESC""",
        params,
    ).fetchall()
```

- [ ] **Step 2: 验证不报错**

Run: `cd /Users/ericmr/Documents/GitHub/DailyCheck && ./venv/bin/python -c "
from app import create_app
app = create_app()
print('items route OK:', any(r.endpoint=='items.items_list' for r in app.url_map.iter_rules()))
"`

Expected:`items route OK: True`。

- [ ] **Step 3: 跑既有测试**

Run: `cd /Users/ericmr/Documents/GitHub/DailyCheck && ./venv/bin/python -m pytest tests/test_items_route.py -v`

Expected: 3 passed(无回归)。

- [ ] **Step 4: Commit**

```bash
git add blueprints/items.py
git commit -m "refactor(items): GET /items 限定固定品类(与 /inventory 同源)"
```

---

### Task 3: `items.html` 加品类 chip bar + 克重输入 + 克重列

**Files:**
- Modify: `templates/items.html`(顶部加 chip bar;新增表单补 gram_per_unit 输入;列表加克重列 + data-cat;底部加 JS)

**Interfaces:**
- Produces:`/items` 页面顶部一排 chip 切换品类;新增表单有 gram_per_unit 输入;列表展示克重列;JS 切换不重发请求。

- [ ] **Step 1: 加 chip bar**

打开 `templates/items.html`,在 `<h2>品类与品项</h2>` 之后、`<section id="categories">` 之前插入:

```html
<div class="cat-bar" role="tablist" aria-label="按品类筛选">
  <button type="button" class="cat-chip is-active" data-cat="__all" role="tab" aria-selected="true">全部</button>
  {% for c in categories %}
  <button type="button" class="cat-chip" data-cat="{{ c.name }}" role="tab" aria-selected="false">{{ c.name }}</button>
  {% endfor %}
</div>
```

- [ ] **Step 2: 新增表单补 gram_per_unit 输入**

把现有"新增库存品"表单(原 17-48 行)的 6 个 label 后面、单位 label 之前/之后,新增一个 label:

```html
  <label>
    <span>每单位克重（克，0=不启用）</span>
    <input name="gram_per_unit" type="number" min="0" step="0.01" placeholder="0" />
  </label>
```

(插入位置:`<label><span>单位</span><input name="unit" placeholder="件" /></label>` 之后,`<button type="submit">新增库存品</button>` 之前。)

- [ ] **Step 3: 列表加克重列 + data-cat 属性**

把表头(原 52 行):

```html
<tr><th>品项</th><th>品类</th><th>库存</th><th>安全库存</th><th>单价（¥）</th><th>更新时间</th><th>操作</th></tr>
```

改为:

```html
<tr><th>品项</th><th>品类</th><th>库存</th><th>安全库存</th><th>单价（¥）</th><th>克重</th><th>更新时间</th><th>操作</th></tr>
```

把循环里的 `<tr>`(原 54 行):

```html
  <tr>
```

改为:

```html
  <tr data-cat="{{ i.category_name }}">
```

在 `<td>{{ i.updated_at }}</td>` 之后、`<td class="inline">` 之前,加一列:

```html
    <td>{% if i.gram_per_unit and i.gram_per_unit > 0 %}{{ i.gram_per_unit | fmt_qty }} g/件{% else %}—{% endif %}</td>
```

- [ ] **Step 4: 加切换 JS**

在 `{% endblock %}` 之前(原文件末尾)插入:

```html
<script>
(function() {
  var bar = document.querySelector('.cat-bar');
  if (!bar) return;
  var chips = bar.querySelectorAll('.cat-chip');
  var rows = document.querySelectorAll('table tbody tr[data-cat]');
  function apply(cat) {
    chips.forEach(function(c) {
      var on = c.getAttribute('data-cat') === cat;
      c.classList.toggle('is-active', on);
      c.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    rows.forEach(function(row) {
      var match = (cat === '__all') || (row.getAttribute('data-cat') === cat);
      row.classList.toggle('is-hidden', !match);
    });
  }
  chips.forEach(function(c) {
    c.addEventListener('click', function() { apply(c.getAttribute('data-cat')); });
  });
})();
</script>
```

- [ ] **Step 5: 手工验证模板渲染**

Run: `cd /Users/ericmr/Documents/GitHub/DailyCheck && ./venv/bin/python -c "
from app import create_app
app = create_app()
print('OK')
" 2>&1 | tail -5`

Expected:`OK`(无 Jinja 语法错误)。

(完整 UI 行为在 Task 8 实操验证。)

- [ ] **Step 6: Commit**

```bash
git add templates/items.html
git commit -m "feat(items): 品类 chip 切换 + 每单位克重 UI"
```

---

### Task 4: `base.html` 隐藏 staff 入口 + CSS 版本 bump

**Files:**
- Modify: `templates/base.html:23`(侧栏"品类与品项"链接)
- Modify: `templates/base.html:75`(移动 nav"品项"链接)
- Modify: `templates/base.html:13`(CSS 版本 `?v=10` → `?v=11`)

- [ ] **Step 1: 侧栏入口包条件**

把:

```html
      <a href="{{ url_for('items.items_list') }}">品类与品项</a>
```

改为:

```html
      {% if current_role != 'staff' %}
      <a href="{{ url_for('items.items_list') }}">品类与品项</a>
      {% endif %}
```

- [ ] **Step 2: 移动 nav 入口包条件**

把:

```html
      <a href="{{ url_for('items.items_list') }}">品项</a>
```

改为:

```html
      {% if current_role != 'staff' %}
      <a href="{{ url_for('items.items_list') }}">品项</a>
      {% endif %}
```

- [ ] **Step 3: CSS 版本 bump**

把:

```html
  <link rel="stylesheet" href="{{ url_for('static', filename='style.css') }}?v=10" />
```

改为:

```html
  <link rel="stylesheet" href="{{ url_for('static', filename='style.css') }}?v=11" />
```

- [ ] **Step 4: 验证模板渲染**

Run: `cd /Users/ericmr/Documents/GitHub/DailyCheck && ./venv/bin/python -c "
from app import create_app
app = create_app()
print('OK')
" 2>&1 | tail -5`

Expected:`OK`。

- [ ] **Step 5: Commit**

```bash
git add templates/base.html
git commit -m "feat(nav): 隐藏 staff 角色的品类与品项入口，CSS v11"
```

---

### Task 5: `summary()` 增加 range 参数与时间筛选 SQL(不动周转率)

**Files:**
- Modify: `blueprints/core.py:64-175`(summary 函数)

**Interfaces:**
- Produces:`GET /summary?range=7d|month|all`(默认 7d);模板上下文多 `range`,`total_inbound_value` / `total_consumed_value` / `category_stats[].consumed_value` / `category_stats[].inbound_value` / `top_consumed` 受时间筛选;`total_stock_value` 不变。**本任务不实现 turnover**,仅做时间筛选 SQL 改造 — turnover 在 Task 6 加。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_summary_route.py`:

```python
"""汇总路由测试:range 参数 + 时间筛选。"""
import sqlite3
from datetime import datetime

import pytest


def _login_as_admin(tmp_path, monkeypatch):
    import db as db_module
    from db import init_master_db, init_warehouse_db
    master_path = tmp_path / "master.db"
    wh_path = tmp_path / "wh.db"
    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", tmp_path)
    init_master_db()
    init_warehouse_db(wh_path)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
    return client, wh_path


def test_default_range_is_7d(tmp_path, monkeypatch):
    client, _ = _login_as_admin(tmp_path, monkeypatch)
    resp = client.get("/summary")
    assert resp.status_code == 200
    assert b"7 \xe6\x97\xa5" in resp.data or b"7日" in resp.data  # chip 含 "7 日" / "7日"


def test_range_param_passes_through(tmp_path, monkeypatch):
    client, _ = _login_as_admin(tmp_path, monkeypatch)
    resp = client.get("/summary?range=month")
    assert resp.status_code == 200
    # month 芯片应该有 is-active 类
    assert b"is-active" in resp.data


def test_invalid_range_falls_back_to_7d(tmp_path, monkeypatch):
    client, _ = _login_as_admin(tmp_path, monkeypatch)
    resp = client.get("/summary?range=bogus")
    assert resp.status_code == 200


def test_total_inbound_value_unchanged_by_range(tmp_path, monkeypatch):
    """库存金额字段必须不受 range 影响(账面永远是当下)。"""
    client, wh_path = _login_as_admin(tmp_path, monkeypatch)
    conn = sqlite3.connect(wh_path)
    cat_id = conn.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()[0]
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO items (sku, name, category_id, quantity, safety_stock, unit_cost, unit, gram_per_unit, updated_at) "
        "VALUES ('X', 'test', ?, 100, 0, 5, '件', 0, ?)", (cat_id, ts))
    conn.commit()
    conn.close()

    resp = client.get("/summary")
    # 库存金额 ¥500 在 HTML 中存在(无论 range)
    assert b"500" in resp.data
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /Users/ericmr/Documents/GitHub/DailyCheck && ./venv/bin/python -m pytest tests/test_summary_route.py -v`

Expected: `test_default_range_is_7d` 与 `test_range_param_passes_through` FAIL(模板还没渲染 range 芯片)。

- [ ] **Step 3: 在 `summary()` 加 range 解析**

打开 `blueprints/core.py`,把 `summary()` 函数开头改为:

```python
@bp.route("/summary")
@require_login
def summary():
    db = get_warehouse_db()

    # range 参数:7d(默认)/ month / all
    range_param = request.args.get("range", "7d")
    if range_param not in ("7d", "month", "all"):
        range_param = "7d"

    # 时间筛选 SQL 起点表达式(SQLite 字符串)
    if range_param == "7d":
        time_clause_outbound = "o.created_at >= datetime('now','-7 days')"
        time_clause_production = "pr.created_at >= datetime('now','-7 days')"
        time_clause_restock = "r.created_at >= datetime('now','-7 days')"
        window_days = 7
    elif range_param == "month":
        import datetime as _dt
        ym = _dt.datetime.now().strftime("%Y-%m")
        time_clause_outbound = f"o.created_at LIKE '{ym}%'"
        time_clause_production = f"pr.created_at LIKE '{ym}%'"
        time_clause_restock = f"r.created_at LIKE '{ym}%'"
        _, last_day = _dt.datetime.now(), None
        import calendar
        year = _dt.datetime.now().year
        month = _dt.datetime.now().month
        window_days = calendar.monthrange(year, month)[1]
    else:  # all
        time_clause_outbound = "1=1"
        time_clause_production = "1=1"
        time_clause_restock = "1=1"
        # window_days 用 stock_movements 实际跨度
        first = db.execute(
            "SELECT MIN(created_at) AS d FROM stock_movements"
        ).fetchone()["d"]
        if first:
            window_days = max(
                1, (_dt.datetime.now() - _dt.datetime.strptime(first[:10], "%Y-%m-%d")).days
            )
        else:
            window_days = 1  # 空数据时给 1,避免除 0
```

(注意:上面的 `_dt` 引用在 `else` 分支里没有 `import`,需要把 `import datetime as _dt` 移到 `summary()` 顶部 — 见 Step 4。)

- [ ] **Step 4: 顶部加 `import datetime`**

在 `blueprints/core.py` 顶部,把 `from datetime import datetime` 改为:

```python
import datetime as _dt
from datetime import datetime
```

(为简化,后续代码统一用 `_dt`。)

- [ ] **Step 5: 把现有 4 个聚合查询改为应用 time_clause**

把 `total_inbound_value` 查询(73-77 行)改为:

```python
    total_inbound_value = db.execute(
        f"""SELECT COALESCE(SUM(r.requested_quantity * i.unit_cost), 0) AS c
            FROM restock_requests r
            JOIN items i ON i.id = r.item_id
            WHERE {time_clause_restock}"""
    ).fetchone()["c"]
```

把 `consumed_outbound` 查询(82-88 行)改为:

```python
    consumed_outbound = db.execute(
        f"""SELECT COALESCE(SUM(o.requested_quantity * i.unit_cost), 0) AS c
            FROM outbound_requests o
            JOIN items i ON i.id = o.item_id
            WHERE o.rolled_back = 0
              AND (o.reason IS NULL OR o.reason NOT LIKE '生产领料(run=#%')
              AND {time_clause_outbound}"""
    ).fetchone()["c"]
```

把 `consumed_production` 查询(89-95 行)改为:

```python
    consumed_production = db.execute(
        f"""SELECT COALESCE(SUM(pri.actual_qty * i.unit_cost), 0) AS c
            FROM production_run_items pri
            JOIN production_runs pr ON pr.id = pri.run_id
            JOIN items i ON i.id = pri.item_id
            WHERE pr.rolled_back = 0
              AND {time_clause_production}"""
    ).fetchone()["c"]
```

把 `category_stats` 子查询里的 `o.total_outbound` 与 `r.total_restock` 加 WHERE:

```python
    cat_data = db.execute(
        f"""SELECT
              c.name AS category_name,
              COALESCE(SUM(item_vals.restock_value), 0) AS restock_value,
              COALESCE(SUM(item_vals.consumed_value), 0) AS consumed_value,
              COALESCE(SUM(item_vals.current_stock_value), 0) AS stock_value
           FROM categories c
           LEFT JOIN (
              SELECT
                  i.category_id,
                  COALESCE(r.total_restock, 0) * i.unit_cost AS restock_value,
                  COALESCE(o.total_outbound, 0) * i.unit_cost AS consumed_value,
                  i.quantity * i.unit_cost AS current_stock_value
              FROM items i
              LEFT JOIN (
                  SELECT item_id, SUM(requested_quantity) AS total_restock
                  FROM restock_requests
                  WHERE {time_clause_restock}
                  GROUP BY item_id
              ) r ON r.item_id = i.id
              LEFT JOIN (
                  SELECT item_id, SUM(requested_quantity) AS total_outbound
                  FROM outbound_requests
                  WHERE rolled_back = 0
                    AND (reason IS NULL OR reason NOT LIKE '生产领料(run=#%')
                    AND {time_clause_outbound}
                  GROUP BY item_id
              ) o ON o.item_id = i.id
           ) item_vals ON item_vals.category_id = c.id
           GROUP BY c.id, c.name ORDER BY c.id"""
    ).fetchall()
```

把 `top_consumed` 子查询加 WHERE:

```python
    top_consumed = db.execute(
        f"""SELECT i.name AS item_name, c.name AS category_name,
                  o.total_qty AS consumed_qty, i.unit,
                  ROUND(o.total_qty * i.unit_cost, 2) AS consumed_value
           FROM (
               SELECT item_id, SUM(requested_quantity) AS total_qty
               FROM outbound_requests
               WHERE rolled_back = 0
                 AND (reason IS NULL OR reason NOT LIKE '生产领料(run=#%')
                 AND {time_clause_outbound}
               GROUP BY item_id
           ) o
           JOIN items i ON i.id = o.item_id
           JOIN categories c ON c.id = i.category_id
           ORDER BY o.total_qty DESC LIMIT 10"""
    ).fetchall()
```

- [ ] **Step 6: render_template 加 range**

把最后的 `return render_template(...)`(166-174 行)改为:

```python
    return render_template(
        "summary.html",
        total_inbound_value=round(total_inbound_value, 2),
        total_consumed_value=round(total_consumed_value, 2),
        total_stock_value=round(total_stock_value, 2),
        total_revenue=round(total_revenue, 2),
        category_stats=enriched_stats,
        top_consumed=top_consumed,
        range=range_param,
    )
```

- [ ] **Step 7: 运行测试**

Run: `cd /Users/ericmr/Documents/GitHub/DailyCheck && ./venv/bin/python -m pytest tests/test_summary_route.py -v`

Expected:4 个测试仍 FAIL(模板还没渲染 range 芯片),但模板渲染本身不报错。

- [ ] **Step 8: 验证模板能渲染**

Run: `cd /Users/ericmr/Documents/GitHub/DailyCheck && ./venv/bin/python -c "
from app import create_app
app = create_app()
with app.test_client() as c:
    with c.session_transaction() as s: s['user_id']=1; s['warehouse_id']=1
    r = c.get('/summary?range=month')
    print(r.status_code)
"

Expected:`200`(模板渲染不报错;range 上下文传到 summary.html 了,即使模板还没用)。

- [ ] **Step 9: Commit**

```bash
git add blueprints/core.py tests/test_summary_route.py
git commit -m "feat(summary): range=7d|month|all 时间筛选 SQL 改造"
```

---

### Task 6: `summary()` 增加 turnover / turnover_days 计算

**Files:**
- Modify: `blueprints/core.py:summary()`(增加 avg_stock_value 计算、turnover 字段)

**Interfaces:**
- Produces:模板上下文多 `turnover` (float, 2 位小数)与 `turnover_days` (float 或 None — None 时模板渲染 `—`)。按品类表每行多 `turnover` 字段(可选 None)。`window_days` 已通过 Task 5 算好,本任务用之。

- [ ] **Step 1: 加反推起点的 SQL 函数**

在 `summary()` 函数顶部(紧接 `db = get_warehouse_db()` 之后),加 helper 函数(在函数体内定义,不导出):

```python
    # 起止两点平均库存金额
    end_value = db.execute(
        "SELECT COALESCE(SUM(quantity * unit_cost), 0) AS c FROM items"
    ).fetchone()["c"]
    end_value = float(end_value)

    # 反推窗口起始库存金额
    if range_param == "7d":
        start_filter = "m.created_at >= datetime('now','-7 days')"
    elif range_param == "month":
        ym = _dt.datetime.now().strftime("%Y-%m")
        start_filter = f"m.created_at LIKE '{ym}%'"
    else:  # all:反推到第一条 stock_movements
        first = db.execute(
            "SELECT MIN(created_at) AS d FROM stock_movements"
        ).fetchone()["d"]
        if not first:
            start_filter = None
        else:
            start_filter = f"m.created_at >= '{first}'"

    if start_filter is None:
        start_value = end_value  # 无 stock_movements 历史,起点=当前
    else:
        rows = db.execute(
            f"""SELECT i.id, i.quantity, i.unit_cost,
                       COALESCE(SUM(m.delta), 0) AS d
                FROM items i
                LEFT JOIN stock_movements m
                  ON m.item_id = i.id AND {start_filter}
                GROUP BY i.id"""
        ).fetchall()
        start_value = 0.0
        for r in rows:
            qty_start = float(r["quantity"]) - float(r["d"])
            if qty_start < 0:
                qty_start = 0  # 防御:反推得负数视为 0
            start_value += qty_start * float(r["unit_cost"])

    avg_stock_value = (start_value + end_value) / 2

    # 周转率 + 可售天数
    if avg_stock_value > 0 and window_days > 0:
        turnover = round(float(total_consumed_value) / avg_stock_value, 2)
        daily_consume = float(total_consumed_value) / window_days
        if daily_consume > 0:
            turnover_days = round(avg_stock_value / daily_consume, 1)
        else:
            turnover_days = None  # 消耗为 0,无意义
    else:
        turnover = 0.0
        turnover_days = None
```

- [ ] **Step 2: 按品类 turnover(独立反推每个品类的 avg)**

为支持品类级 turnover,需要让 `cat_data` 查询带上 `category_id`,并额外查一次"按品类分组的起点库存金额"。修改原 `cat_data` SQL(在 Task 5 Step 5 已改),把 `SELECT` 头改为:

```sql
              SELECT
                  c.id AS category_id,
                  c.name AS category_name,
                  COALESCE(SUM(item_vals.restock_value), 0) AS restock_value,
                  COALESCE(SUM(item_vals.consumed_value), 0) AS consumed_value,
                  COALESCE(SUM(item_vals.current_stock_value), 0) AS stock_value
              FROM categories c
              LEFT JOIN (...)
              ...
              GROUP BY c.id, c.name ORDER BY c.id
```

(注意:把 `c.id` 也 SELECT 出,`GROUP BY c.id, c.name`。)

然后在 `cat_data = db.execute(...)` 之后,加一次"按品类分组的反推起点金额"查询:

```python
    # 品类级反推起点库存金额
    if range_param == "7d":
        cat_start_filter = "sm.created_at >= datetime('now','-7 days')"
    elif range_param == "month":
        ym = _dt.datetime.now().strftime("%Y-%m")
        cat_start_filter = f"sm.created_at LIKE '{ym}%'"
    else:
        first = db.execute(
            "SELECT MIN(created_at) AS d FROM stock_movements"
        ).fetchone()["d"]
        if first:
            cat_start_filter = f"sm.created_at >= '{first}'"
        else:
            cat_start_filter = None

    if cat_start_filter:
        cat_start_rows = db.execute(
            f"""SELECT i.category_id AS cid,
                       COALESCE(SUM((i.quantity - sm.delta) * i.unit_cost), 0) AS start_value
                FROM items i
                LEFT JOIN stock_movements sm
                  ON sm.item_id = i.id AND {cat_start_filter}
                GROUP BY i.category_id"""
        ).fetchall()
        cat_start_map = {r["cid"]: float(r["start_value"]) for r in cat_start_rows}
    else:
        cat_start_map = {}
```

把 `enriched_stats.append({...})` 块替换为:

```python
    enriched_stats = []
    for row in cat_data:
        consumed_v = round(float(row["consumed_value"]), 2)
        stock_v = round(float(row["stock_value"]), 2)
        cid = row["category_id"]
        start_v = cat_start_map.get(cid, stock_v)
        if start_v < 0:
            start_v = 0
        avg = (start_v + stock_v) / 2
        if avg > 0 and consumed_v > 0:
            cat_turnover = round(consumed_v / avg, 2)
        else:
            cat_turnover = None
        enriched_stats.append({
            "category_name": row["category_name"],
            "inbound_value": round(float(row["restock_value"]), 2),
            "consumed_value": consumed_v,
            "stock_value": stock_v,
            "turnover": cat_turnover,
        })
```

- [ ] **Step 3: render_template 多传 2 个字段**

把最后的 `return render_template(...)` 块改为:

```python
    return render_template(
        "summary.html",
        total_inbound_value=round(total_inbound_value, 2),
        total_consumed_value=round(total_consumed_value, 2),
        total_stock_value=round(total_stock_value, 2),
        total_revenue=round(total_revenue, 2),
        category_stats=enriched_stats,
        top_consumed=top_consumed,
        range=range_param,
        turnover=turnover,
        turnover_days=turnover_days,
    )
```

- [ ] **Step 4: 补测试断言**

修改 `tests/test_summary_route.py`,在 `test_default_range_is_7d` 后加:

```python
def test_turnover_zero_when_no_consume(tmp_path, monkeypatch):
    """无消耗数据时 turnover=0.00,turnover_days=None(模板渲染为 '—')。"""
    client, _ = _login_as_admin(tmp_path, monkeypatch)
    resp = client.get("/summary")
    assert resp.status_code == 200
    # turnover 字段在响应体里能找到(模板渲染后);HTML 出现 "库存周转率" 字样
    assert "库存周转率" in resp.data.decode("utf-8")
```

- [ ] **Step 5: 运行测试**

Run: `cd /Users/ericmr/Documents/GitHub/DailyCheck && ./venv/bin/python -m pytest tests/test_summary_route.py -v`

Expected:仍 4 个 FAIL(模板还没渲染 turnover 卡片,但模板不报错)。

- [ ] **Step 6: 验证导入与计算函数无异常**

Run: `cd /Users/ericmr/Documents/GitHub/DailyCheck && ./venv/bin/python -c "
from app import create_app
from blueprints.core import summary
print('summary fn importable:', summary.__name__)
" 2>&1 | tail -5`

Expected:`summary fn importable: summary`。

- [ ] **Step 7: Commit**

```bash
git add blueprints/core.py tests/test_summary_route.py
git commit -m "feat(summary): turnover + turnover_days 计算 (avg=起止两点平均)"
```

---

### Task 7: `summary.html` 时间筛选芯片 + turnover 卡片 + 按品类 turnover 列 + 导出按钮

**Files:**
- Modify: `templates/summary.html`(顶部加 chip bar,替换营业额卡片,加导出按钮)

- [ ] **Step 1: 加时间范围 chip bar**

打开 `templates/summary.html`,把 `<h2>汇总</h2>` 行替换为:

```html
<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
  <h2 style="margin:0">汇总</h2>
  <div style="display:flex;gap:6px;align-items:center">
    <a class="btn" href="{{ url_for('reports.export_summary', range=range) }}">导出 CSV</a>
  </div>
</div>

<div class="cat-bar summary-range" role="tablist" aria-label="时间范围">
  <a href="{{ url_for('core.summary', range='7d') }}" class="cat-chip {% if range == '7d' %}is-active{% endif %}" role="tab" aria-selected="{{ 'true' if range == '7d' else 'false' }}">7 日</a>
  <a href="{{ url_for('core.summary', range='month') }}" class="cat-chip {% if range == 'month' %}is-active{% endif %}" role="tab" aria-selected="{{ 'true' if range == 'month' else 'false' }}">当月</a>
  <a href="{{ url_for('core.summary', range='all') }}" class="cat-chip {% if range == 'all' %}is-active{% endif %}" role="tab" aria-selected="{{ 'true' if range == 'all' else 'false' }}">全部</a>
</div>
```

- [ ] **Step 2: 替换营业额卡片为周转率**

把 4 张卡片的最后一张("库存总价值"卡片之后,实际上 spec 说"营业额被替换"):

原代码(8-25 行):

```html
<div class="grid cols-4">
  <section class="card">
    <p>营业额</p>
    <strong><small>¥</small>{{ "{:,.2f}".format(total_revenue) }}</strong>
  </section>
  ...
  <section class="card">
    <p>库存总价值</p>
    <strong><small>¥</small>{{ "{:,.2f}".format(total_stock_value) }}</strong>
  </section>
</div>
```

改为:

```html
<div class="grid cols-4">
  <section class="card">
    <p>进货总金额</p>
    <strong><small>¥</small>{{ "{:,.2f}".format(total_inbound_value) }}</strong>
    <small class="muted">{{ range_label }}</small>
  </section>
  <section class="card">
    <p>消耗金额</p>
    <strong><small>¥</small>{{ "{:,.2f}".format(total_consumed_value) }}</strong>
    <small class="muted">{{ range_label }}</small>
  </section>
  <section class="card">
    <p>库存总价值</p>
    <strong><small>¥</small>{{ "{:,.2f}".format(total_stock_value) }}</strong>
    <small class="muted">不受时间筛选</small>
  </section>
  <section class="card turnover-card">
    <p>库存周转率</p>
    <strong>{{ "{:.2f}".format(turnover) }}</strong>
    {% if turnover_days is not none %}
    <small class="muted">≈ 可售 {{ turnover_days }} 天</small>
    {% else %}
    <small class="muted">可售 — 天</small>
    {% endif %}
  </section>
</div>
```

(注意:原代码第二张卡片是"进货总金额",第三张是"总消耗金额" — spec 文档里只说"营业额被周转率替换",所以保留前两张,第三张换为周转率。但原代码顺序有差异 — 按现有 summary.html(8-25 行):营业额 / 进货 / 消耗 / 库存。本任务做最小改动:删除"营业额"卡片,新增"库存周转率"卡片到末尾。)

为简化,**实际替换**:把 8-25 行整段改为:

```html
<div class="grid cols-4">
  <section class="card">
    <p>进货总金额</p>
    <strong><small>¥</small>{{ "{:,.2f}".format(total_inbound_value) }}</strong>
  </section>
  <section class="card">
    <p>消耗金额</p>
    <strong><small>¥</small>{{ "{:,.2f}".format(total_consumed_value) }}</strong>
  </section>
  <section class="card">
    <p>库存总价值</p>
    <strong><small>¥</small>{{ "{:,.2f}".format(total_stock_value) }}</strong>
    <small class="muted">不受时间筛选</small>
  </section>
  <section class="card turnover-card">
    <p>库存周转率</p>
    <strong>{{ "{:.2f}".format(turnover) }}</strong>
    {% if turnover_days is not none %}
    <small class="muted">≈ 可售 {{ turnover_days }} 天</small>
    {% else %}
    <small class="muted">可售 — 天</small>
    {% endif %}
  </section>
</div>
```

(删除"营业额"卡片,前两张卡片顺序对调为"进货 / 消耗 / 库存 / 周转率"四块。这是 spec 文档"其余 3 张卡片保留"的最小实现。)

- [ ] **Step 3: 加 `range_label` 上下文**

回到 `blueprints/core.py`,在 `render_template(...)` 里多传一个:

```python
    range_label = {"7d": "7 日滚动", "month": "当月", "all": "全部"}[range_param]
```

然后 `render_template(..., range_label=range_label, ...)`。

- [ ] **Step 4: 按品类表加周转率列**

把按品类表(原 27-45 行)的表头改为:

```html
  <tr>
    <th>品类</th>
    <th>进货金额</th>
    <th>消耗金额</th>
    <th>库存金额</th>
    <th>周转率</th>
  </tr>
```

把循环里每个 `<tr>` 加一列(在最后一个 `<td>` 之后):

```html
    <td>
      {% if cat.turnover is not none %}{{ "{:.2f}".format(cat.turnover) }}{% else %}—{% endif %}
    </td>
```

(Task 6 的 Step 2 已说明品类 turnover 暂为 None,本期显示 `—`。)

- [ ] **Step 5: 验证模板**

Run: `cd /Users/ericmr/Documents/GitHub/DailyCheck && ./venv/bin/python -c "
from app import create_app
app = create_app()
print('OK')
" 2>&1 | tail -5`

Expected:`OK`(无 Jinja 错误)。

- [ ] **Step 6: 跑既有测试**

Run: `cd /Users/ericmr/Documents/GitHub/DailyCheck && ./venv/bin/python -m pytest tests/test_summary_route.py -v`

Expected:至少 `test_default_range_is_7d`、`test_range_param_passes_through`、`test_turnover_zero_when_no_consume` 通过(其他用例要看后续模板细节)。

- [ ] **Step 7: Commit**

```bash
git add templates/summary.html blueprints/core.py
git commit -m "feat(summary): 时间芯片 + 周转率卡片 + 按品类周转率列 + 导出按钮"
```

---

### Task 8: `static/style.css` 加 `.summary-range` / `.turnover-card` 样式

**Files:**
- Modify: `static/style.css`(在文件末尾追加)

- [ ] **Step 1: 加样式**

打开 `static/style.css`,在文件末尾追加:

```css
/* Summary time-range + turnover card */
.summary-range { margin: 8px 0 14px; }
.turnover-card { background: #ecfdf5; border-color: #0f766e; }
.turnover-card p { color: #0f766e; }
.turnover-card strong { color: #0f766e; }
.turnover-card .muted { color: #047857; }
```

- [ ] **Step 2: 验证 CSS 不破坏既有**

Run: `cd /Users/ericmr/Documents/GitHub/DailyCheck && ./venv/bin/python -c "
from app import create_app
app = create_app()
print('OK')
"`

Expected:`OK`。

- [ ] **Step 3: Commit**

```bash
git add static/style.css
git commit -m "feat(style): summary 时间芯片与周转率卡片样式"
```

---

### Task 9: `reports.export_summary` 路由(三段 CSV)

**Files:**
- Modify: `blueprints/reports.py`(在文件末尾追加新路由)

- [ ] **Step 1: 写失败测试**

新建 `tests/test_summary_export.py`:

```python
"""汇总 CSV 导出测试。"""
import csv
import io
from datetime import datetime

import pytest


def _login_as_admin(tmp_path, monkeypatch):
    import db as db_module
    from db import init_master_db, init_warehouse_db
    master_path = tmp_path / "master.db"
    wh_path = tmp_path / "wh.db"
    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", tmp_path)
    init_master_db()
    init_warehouse_db(wh_path)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
    return client


def test_export_has_utf8_bom(tmp_path, monkeypatch):
    client = _login_as_admin(tmp_path, monkeypatch)
    resp = client.get("/summary/export?range=7d")
    assert resp.status_code == 200
    assert resp.data.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM


def test_export_filename(tmp_path, monkeypatch):
    client = _login_as_admin(tmp_path, monkeypatch)
    resp = client.get("/summary/export?range=7d")
    cd = resp.headers.get("Content-Disposition", "")
    assert "summary-" in cd
    assert "-7d.csv" in cd


def test_export_three_sections(tmp_path, monkeypatch):
    client = _login_as_admin(tmp_path, monkeypatch)
    resp = client.get("/summary/export?range=7d")
    body = resp.data.decode("utf-8-sig")
    # 三段标题
    assert "范围" in body  # 段 1 表头
    assert "品类" in body  # 段 2 / 段 3 表头
    assert "进货金额" in body
    assert "消耗金额" in body
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /Users/ericmr/Documents/GitHub/DailyCheck && ./venv/bin/python -m pytest tests/test_summary_export.py -v`

Expected:3 个 FAIL(路由尚未注册 → 404)。

- [ ] **Step 3: 在 reports.py 末尾加 export_summary**

打开 `blueprints/reports.py`,在文件最末尾追加:

```python


# ---------------------------------------------------------------------------
# Summary export (CSV with three sections)
# ---------------------------------------------------------------------------

@bp.route("/summary/export")
@require_login
def export_summary():
    """CSV 三段导出:总体 / 按品类 / 消耗 Top。

    与 /summary 共享 range 参数(7d|month|all,默认 7d)。
    输出 UTF-8 BOM 兼容 Excel 中文。
    """
    import calendar
    import datetime as _dt

    db = get_warehouse_db()
    range_param = request.args.get("range", "7d")
    if range_param not in ("7d", "month", "all"):
        range_param = "7d"

    if range_param == "7d":
        time_clause_outbound = "o.created_at >= datetime('now','-7 days')"
        time_clause_production = "pr.created_at >= datetime('now','-7 days')"
        time_clause_restock = "r.created_at >= datetime('now','-7 days')"
        window_days = 7
        range_label = "7 日"
    elif range_param == "month":
        ym = _dt.datetime.now().strftime("%Y-%m")
        time_clause_outbound = f"o.created_at LIKE '{ym}%'"
        time_clause_production = f"pr.created_at LIKE '{ym}%'"
        time_clause_restock = f"r.created_at LIKE '{ym}%'"
        window_days = calendar.monthrange(_dt.datetime.now().year, _dt.datetime.now().month)[1]
        range_label = "当月"
    else:
        time_clause_outbound = "1=1"
        time_clause_production = "1=1"
        time_clause_restock = "1=1"
        first = db.execute("SELECT MIN(created_at) AS d FROM stock_movements").fetchone()["d"]
        if first:
            window_days = max(1, (_dt.datetime.now() - _dt.datetime.strptime(first[:10], "%Y-%m-%d")).days)
        else:
            window_days = 1
        range_label = "全部"

    # 段 1:总体
    total_inbound = db.execute(
        f"""SELECT COALESCE(SUM(r.requested_quantity * i.unit_cost), 0) AS c
            FROM restock_requests r
            JOIN items i ON i.id = r.item_id
            WHERE {time_clause_restock}"""
    ).fetchone()["c"]
    consumed_outbound = db.execute(
        f"""SELECT COALESCE(SUM(o.requested_quantity * i.unit_cost), 0) AS c
            FROM outbound_requests o
            JOIN items i ON i.id = o.item_id
            WHERE o.rolled_back = 0
              AND (o.reason IS NULL OR o.reason NOT LIKE '生产领料(run=#%')
              AND {time_clause_outbound}"""
    ).fetchone()["c"]
    consumed_production = db.execute(
        f"""SELECT COALESCE(SUM(pri.actual_qty * i.unit_cost), 0) AS c
            FROM production_run_items pri
            JOIN production_runs pr ON pr.id = pri.run_id
            JOIN items i ON i.id = pri.item_id
            WHERE pr.rolled_back = 0
              AND {time_clause_production}"""
    ).fetchone()["c"]
    total_consumed = float(consumed_outbound) + float(consumed_production)
    total_stock = db.execute(
        "SELECT COALESCE(SUM(quantity * unit_cost), 0) AS c FROM items"
    ).fetchone()["c"]
    if float(total_stock) > 0 and window_days > 0 and total_consumed > 0:
        turnover = total_consumed / float(total_stock)
        turnover_days = round(float(total_stock) / (total_consumed / window_days), 1)
        turnover_str = f"{turnover:.2f}"
        days_str = f"{turnover_days}"
    else:
        turnover_str = "0.00"
        days_str = "—"

    # 段 2:按品类
    cat_rows = db.execute(
        f"""SELECT c.name AS category_name,
                  COALESCE(SUM(r.total_restock * i.unit_cost), 0) AS inbound_value,
                  COALESCE(SUM(o.total_outbound * i.unit_cost), 0) AS consumed_value,
                  COALESCE(SUM(i.quantity * i.unit_cost), 0) AS stock_value
           FROM categories c
           LEFT JOIN items i ON i.category_id = c.id
           LEFT JOIN (
               SELECT item_id, SUM(requested_quantity) AS total_restock
               FROM restock_requests WHERE {time_clause_restock}
               GROUP BY item_id
           ) r ON r.item_id = i.id
           LEFT JOIN (
               SELECT item_id, SUM(requested_quantity) AS total_outbound
               FROM outbound_requests
               WHERE rolled_back = 0
                 AND (reason IS NULL OR reason NOT LIKE '生产领料(run=#%')
                 AND {time_clause_outbound}
               GROUP BY item_id
           ) o ON o.item_id = i.id
           GROUP BY c.id, c.name ORDER BY c.id"""
    ).fetchall()

    # 段 3:消耗 Top 10
    top_rows = db.execute(
        f"""SELECT i.name AS item_name, c.name AS category_name,
                  o.total_qty AS consumed_qty, i.unit,
                  ROUND(o.total_qty * i.unit_cost, 2) AS consumed_value
           FROM (
               SELECT item_id, SUM(requested_quantity) AS total_qty
               FROM outbound_requests
               WHERE rolled_back = 0
                 AND (reason IS NULL OR reason NOT LIKE '生产领料(run=#%')
                 AND {time_clause_outbound}
               GROUP BY item_id
           ) o
           JOIN items i ON i.id = o.item_id
           JOIN categories c ON c.id = i.category_id
           ORDER BY o.total_qty DESC LIMIT 10"""
    ).fetchall()

    # 写 CSV(三段以空行分隔)
    output = io.StringIO()
    w = csv.writer(output)

    w.writerow(["范围", "进货金额", "消耗金额", "当前库存金额", "周转率", "可售天数"])
    w.writerow([range_label, f"{float(total_inbound):.2f}", f"{total_consumed:.2f}",
                f"{float(total_stock):.2f}", turnover_str, days_str])
    w.writerow([])  # 空行

    w.writerow(["品类", "进货金额", "消耗金额", "库存金额", "周转率"])
    for cat in cat_rows:
        cat_consumed = float(cat["consumed_value"])
        cat_stock = float(cat["stock_value"])
        if cat_stock > 0 and cat_consumed > 0:
            cat_turnover = f"{cat_consumed / cat_stock:.2f}"
        else:
            cat_turnover = "—"
        w.writerow([
            cat["category_name"],
            f"{float(cat['inbound_value']):.2f}",
            f"{cat_consumed:.2f}",
            f"{cat_stock:.2f}",
            cat_turnover,
        ])
    w.writerow([])

    w.writerow(["品类", "品项", "消耗数量", "单位", "消耗金额"])
    for r in top_rows:
        w.writerow([
            r["category_name"], r["item_name"],
            fmt_qty(r["consumed_qty"]), r["unit"],
            f"{float(r['consumed_value']):.2f}",
        ])

    filename = f"summary-{_dt.datetime.now().strftime('%Y-%m-%d')}-{range_param}.csv"
    from flask import current_app
    return current_app.response_class(
        output.getvalue().encode("utf-8-sig"),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename={filename}"},
    )


def fmt_qty(value):
    """简化版 fmt_qty,避免循环导入。"""
    if value is None:
        return "0"
    s = f"{float(value):.2f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"
```

(若 `reports.py` 已 `from ._helpers import fmt_qty`,把上面的本地 `fmt_qty` 删掉,用 helpers 里的。检查文件顶部 — 目前 reports.py 没 import fmt_qty,所以加本地版即可。)

- [ ] **Step 4: 运行测试**

Run: `cd /Users/ericmr/Documents/GitHub/DailyCheck && ./venv/bin/python -m pytest tests/test_summary_export.py -v`

Expected:3 passed。

- [ ] **Step 5: Commit**

```bash
git add blueprints/reports.py tests/test_summary_export.py
git commit -m "feat(reports): /summary/export 三段 CSV 导出(UTF-8 BOM)"
```

---

### Task 10: 反推库存单元测试

**Files:**
- Create: `tests/test_summary_reverse_stock.py`

**Interfaces:**
- Produces:独立验证 Task 6 的反推逻辑(quantity - sum(delta) 起点)— 用真实 SQLite 跑小脚本。

- [ ] **Step 1: 写测试**

```python
"""汇总页反推起点库存逻辑测试。"""
import sqlite3
from datetime import datetime, timedelta


def _seed_history(wh_path):
    """建一个有历史的临时仓库 db,验证 quantity - delta 累加 == 起点 quantity。"""
    conn = sqlite3.connect(wh_path)
    conn.row_factory = sqlite3.Row

    # 建表 + 类目(简化,不走 init_warehouse_db)
    conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT, quantity REAL, unit_cost REAL)")
    conn.execute("CREATE TABLE stock_movements (id INTEGER PRIMARY KEY, item_id INTEGER, delta REAL, created_at TEXT)")
    conn.execute("CREATE TABLE categories (id INTEGER PRIMARY KEY, name TEXT)")

    cat_id = 1
    conn.execute("INSERT INTO categories (id, name) VALUES (?, '包材')", (cat_id,))
    conn.execute(
        "INSERT INTO items (id, name, quantity, unit_cost) VALUES (1, '面粉', 100, 5)"
    )
    # 7 日窗口内的变动:+30 入库, -50 出库
    now = datetime.now()
    conn.execute(
        "INSERT INTO stock_movements (item_id, delta, created_at) VALUES (1, 30, ?)",
        ((now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),)
    )
    conn.execute(
        "INSERT INTO stock_movements (item_id, delta, created_at) VALUES (1, -50, ?)",
        ((now - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S"),)
    )
    # 窗口外的变动(应被排除)
    conn.execute(
        "INSERT INTO stock_movements (item_id, delta, created_at) VALUES (1, 1000, ?)",
        ((now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S"),)
    )
    conn.commit()
    conn.close()


def test_reverse_qty_start():
    """起点 quantity = 当前 quantity - 窗口内 delta 之和。"""
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        _seed_history(path)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        # 7 日窗口
        rows = conn.execute(
            """SELECT i.id, i.quantity, i.unit_cost,
                      COALESCE(SUM(m.delta), 0) AS d
               FROM items i
               LEFT JOIN stock_movements m
                 ON m.item_id = i.id AND m.created_at >= datetime('now','-7 days')
               GROUP BY i.id"""
        ).fetchall()
        # quantity=100, 窗口内 delta = +30 + -50 = -20,起点 = 100 - (-20) = 120
        assert len(rows) == 1
        qty_start = float(rows[0]["quantity"]) - float(rows[0]["d"])
        assert qty_start == 120.0
        conn.close()
    finally:
        os.unlink(path)


def test_all_uses_min_created_at():
    """range=all:起点用 MIN(stock_movements.created_at)。"""
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        _seed_history(path)
        conn = sqlite3.connect(path)
        first = conn.execute(
            "SELECT MIN(created_at) AS d FROM stock_movements"
        ).fetchone()["d"]
        # MIN 应该是 30 天前那条
        assert first is not None
        days_ago = (datetime.now() - datetime.strptime(first[:10], "%Y-%m-%d")).days
        assert days_ago >= 28  # 30 天左右
        conn.close()
    finally:
        os.unlink(path)
```

- [ ] **Step 2: 运行测试**

Run: `cd /Users/ericmr/Documents/GitHub/DailyCheck && ./venv/bin/python -m pytest tests/test_summary_reverse_stock.py -v`

Expected:2 passed(纯 SQL 逻辑,无依赖应用启动)。

- [ ] **Step 3: Commit**

```bash
git add tests/test_summary_reverse_stock.py
git commit -m "test(summary): 反推起点库存 SQL 单元测试"
```

---

### Task 11: 全量测试 + 手工冒烟验证

**Files:** 无

- [ ] **Step 1: 全量跑测试**

Run: `cd /Users/ericmr/Documents/GitHub/DailyCheck && ./venv/bin/python -m pytest tests/ -v 2>&1 | tail -25`

Expected:所有测试 PASS(原 11 个 + 本次新增 9 个 ≈ 20 个)。

- [ ] **Step 2: 启动应用,手工验证关键路径**

确认 Flask 仍在跑(任务开始时已启动,debug 模式自动 reload):

Run: `curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:5001/summary?range=7d`

Expected:`302`(重定向到登录)。

- [ ] **Step 3: 派发独立 agent 实操验证**

派一个 `general-purpose` subagent 用 Playwright 实操(`/items` 权限、芯片切换、`/summary` 切换 range、CSV 导出)。prompt 至少包含:

```
任务:用 Playwright 对 DailyCheck "foundation updates" 特性做实操验证。应用已在 http://127.0.0.1:5001 运行。

登录:admin 账号 / admin 密码(系统默认密码,如果没有用过可以从 master.db 查)。登录后选仓库。

验证步骤:
1. 切换到 staff 账号登录(若 master.db 没有 staff,先在 /users 建一个)→ 侧栏不应有"品类与品项",底部 nav 不应有"品项";直接访问 /items URL 应 403。
2. 切回 manager → /items 应可见,顶部 chip bar 一排按钮。点击"包材"chip,品项列表只剩包材品类。
3. 在 /items 新建一个品项,克重填 -5 → 应被拒绝(flash 错误)。改 250,提交 → 重开能看到 250 g/件。
4. /summary 默认 → 顶部"库存周转率"卡片显示数字,卡片含"可售 X 天"或"— 天"。
5. 点"当月" → URL 变 ?range=month,卡片数字变化。
6. 点"导出 CSV" → 下载 summary-YYYY-MM-DD-7d.csv,UTF-8 编码,Excel 打开中文不乱码,三段内容。

每步截图 + 一句话结论。发现不符立即报告,不要自行改代码。
```

- [ ] **Step 4: 收尾**

如果实操发现问题,定位到对应 Task 修复后重跑 Task 11 Step 1 + 重新派发 Step 3。

如果一切通过:

```bash
git log --oneline foundation-updates-20260626 ^main
```

确认所有 commit 都在新分支,无 main 上的改动。

---

## 成功标准

1. `./venv/bin/python -m pytest tests/ -v` 全绿(原 11 + 新增 9 = 20 个测试通过)
2. 实操截图证明:`/items` 在 staff 视角不可见 / 芯片切换不重发请求 / 克重负值被拒 / `/summary?range=month` 切换生效 / CSV 三段内容齐备且 UTF-8 BOM
3. 所有 commit 在 `foundation-updates-20260626` 分支
4. `master.db` / `inventory.db` / `.venv` / `.superpowers/` / `.playwright-mcp/` 未被提交