# 生产录入模块 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 DailyCheck 中新增"生产录入"模块：店员选产品、输入产出量，系统按 BOM 自动扣减原料（允许实际消耗偏离 BOM），支持回退/删除/CSV 导出；选完仓库后从 `/land` 落地页分流到库存管理与生产录入。

**Architecture:** 沿用现有"按仓库隔离"的 SQLite + Flask Blueprint + Jinja 模式。4 张新表（products / product_bom / production_runs / production_run_items）走 `CREATE TABLE IF NOT EXISTS` 自动迁移；提交/回退/删除的库存与流水对称与 `outbound` 模块同构；汇总口径在 `summary` 端扩展。

**Tech Stack:** Flask 3 / SQLite 3 / Jinja2 / 原生 CSS（移动端优先）。无新依赖。

---

## File Structure

新增（按职责拆分）：

| 文件 | 职责 |
|---|---|
| `blueprints/production.py` | 生产模块所有路由（list / new / edit / delete / session / submit / runs / rollback / delete_run / csv） |
| `templates/land.html` | 仓库选择后落地页（库存管理 / 生产录入 二选一） |
| `templates/production/products.html` | 产品列表（含 BOM 项数、"编辑配方"/"录入"/"删除"按钮） |
| `templates/production/product_edit.html` | 产品编辑 + BOM 行增删改（同一页） |
| `templates/production/session.html` | 录入页（选产品 → 输入产出量 → 展示 BOM 行 actual 可改） |
| `templates/production/runs.html` | 历史记录列表（含"回退/删除"按钮） |

修改：

| 文件 | 改动 |
|---|---|
| `app.py` | `register_blueprint(production_bp)` |
| `blueprints/core.py` | 新增 `land()`；把 `dashboard` 路径从 `/` 改 `/dashboard`；新增 `GET /` → `land`；`summary` 口径加入生产消耗 |
| `blueprints/auth.py` | `warehouse_select` redirect 目标 → `core.land` |
| `blueprints/items.py::inventory_view` | 7 日消耗 action 集合 `('出库','生产消耗')` |
| `db/__init__.py` | `WAREHOUSE_SCHEMA` 末尾追加 4 张新表 + 2 索引 |
| `templates/base.html` | 侧栏 + mobile-nav 追加"生产录入"链接 |
| `AGENT.md` | 目录结构 / 数据表说明 / 关键业务约束 3 处追加 |
| `README.md` | 功能列表追加"生产录入" |

---

## Task 1: 仓库 schema 追加 4 张新表

**Files:**
- Modify: `db/__init__.py`（在 `WAREHOUSE_SCHEMA` 末尾追加；2 个索引）

- [ ] **Step 1: 在 `WAREHOUSE_SCHEMA` 末尾追加新表 + 索引**

打开 `db/__init__.py`，定位到 `idx_audit_action ON audit_log(created_at);` 这行（`WAREHOUSE_SCHEMA` 三引号结束前），在其后追加：

```sql
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    unit TEXT NOT NULL DEFAULT '件',
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS product_bom (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    item_id INTEGER NOT NULL,
    qty_per_unit REAL NOT NULL,
    UNIQUE(product_id, item_id),
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE,
    FOREIGN KEY (item_id) REFERENCES items(id)
);

CREATE TABLE IF NOT EXISTS production_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    output_qty REAL NOT NULL,
    note TEXT,
    rolled_back INTEGER NOT NULL DEFAULT 0,
    created_by TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (product_id) REFERENCES products(id)
);

CREATE TABLE IF NOT EXISTS production_run_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    item_id INTEGER NOT NULL,
    planned_qty REAL NOT NULL,
    actual_qty REAL NOT NULL,
    FOREIGN KEY (run_id) REFERENCES production_runs(id) ON DELETE CASCADE,
    FOREIGN KEY (item_id) REFERENCES items(id)
);

CREATE INDEX IF NOT EXISTS idx_prun_created ON production_runs(created_at);
CREATE INDEX IF NOT EXISTS idx_pruni_run ON production_run_items(run_id);
```

- [ ] **Step 2: 启动一次应用，确认表自动建立**

```bash
flask --app app run --host 0.0.0.0 --port 5001 &
sleep 2
sqlite3 db/warehouses/*.db ".schema products" 2>/dev/null | head -10
sqlite3 db/warehouses/*.db ".schema product_bom" 2>/dev/null | head -10
sqlite3 db/warehouses/*.db ".schema production_runs" 2>/dev/null | head -10
sqlite3 db/warehouses/*.db ".schema production_run_items" 2>/dev/null | head -10
kill %1
```

Expected: 4 个 `.schema` 命令都打出对应表头。任一失败 → 检查 SQL 语法。

- [ ] **Step 3: Commit**

```bash
git add db/__init__.py
git commit -m "feat(production): add 4 production tables to WAREHOUSE_SCHEMA"
```

---

## Task 2: 选完仓库落地页 `/land`

**Files:**
- Create: `templates/land.html`
- Modify: `blueprints/core.py`（新增 `land()`；将 `dashboard` 路径从 `/` 改到 `/dashboard`，新增 `GET /` redirect → `land`）
- Modify: `blueprints/auth.py`（`warehouse_select` redirect 目标 → `core.land`）

- [ ] **Step 1: 在 `core.py` 顶部 import 增加 `redirect, url_for`（已存在则跳过），新增 `land()` 路由，并改 `dashboard` 路径**

打开 `blueprints/core.py`。在 `dashboard()` 函数上方的 `@bp.route("/")` 改成 `@bp.route("/dashboard")`。然后在 `@bp.route("/summary")` 之前新增：

```python
@bp.route("/")
@bp.route("/land")
@require_login
def land():
    """Post-warehouse-pick landing page: choose between 库存管理 and 生产录入."""
    return render("land.html")
```

> `render()` 是 `blueprints/_helpers.py` 里的 helper，会自动注入 `g.warehouse` / `g.user` 到模板上下文。

- [ ] **Step 2: 改 `auth.warehouse_select` 的 redirect 目标**

打开 `blueprints/auth.py`，定位到 `return redirect(url_for("core.dashboard"))` 这一行（文件末尾，函数体最后一行），改为：

```python
    return redirect(url_for("core.land"))
```

- [ ] **Step 3: 创建 `templates/land.html`**

```html
{% extends "base.html" %}
{% block sidebar %}{% endblock %}
{% block title %}选择功能{% endblock %}
{% block content %}
<h2>请选择功能</h2>
{% if g.warehouse %}
<p class="muted">当前仓库：<strong>{{ g.warehouse['name'] }}</strong></p>
{% endif %}

<div class="land-grid">
  <a class="land-card" href="{{ url_for('items.items_list') }}">
    <h3>库存管理</h3>
    <p>品项 / 盘点 / 入库 / 出库 / 调整 / 库存查阅</p>
  </a>
  <a class="land-card" href="{{ url_for('production.products_list') }}">
    <h3>生产录入</h3>
    <p>产品配方 / 按产出量自动领料</p>
  </a>
</div>
{% endblock %}
```

- [ ] **Step 4: 手工验证**

启动应用，登录 → 选仓库 → 应该看到 `/land` 两个卡片。点"库存管理"进 `/items`；"生产录入"目前点开会是 404（蓝图还没注册，预期行为）。

- [ ] **Step 5: Commit**

```bash
git add blueprints/core.py blueprints/auth.py templates/land.html
git commit -m "feat(production): add /land post-warehouse-pick landing page"
```

---

## Task 3: 注册 `production` Blueprint（占位路由 + 侧栏入口）

**Files:**
- Create: `blueprints/production.py`（含 products_list 占位）
- Modify: `app.py`（register_blueprint）
- Modify: `templates/base.html`（侧栏 + mobile-nav 加链接）

- [ ] **Step 1: 创建 `blueprints/production.py`**

```python
"""Production: products, BOM, runs, rollback, delete, CSV export."""
from __future__ import annotations

from flask import Blueprint

from ._helpers import render


bp = Blueprint("production", __name__)


@bp.route("/production", methods=["GET"])
def products_list():
    return render("production/products.html", products=[])
```

> 暂时空渲染，Task 4 替换为真查询。

- [ ] **Step 2: 在 `app.py` 注册蓝图**

打开 `app.py`，在 `from blueprints.reports import bp as reports_bp` 后追加：

```python
    from blueprints.production import bp as production_bp
```

在 `app.register_blueprint(reports_bp)` 后追加：

```python
    app.register_blueprint(production_bp)
```

- [ ] **Step 3: `templates/base.html` 侧栏追加"生产录入"**

打开 `templates/base.html`，在侧栏"出库记录"链接 `<a href="{{ url_for('outbound.outbound_list') }}">出库记录</a>` 后追加：

```html
      <a href="{{ url_for('production.products_list') }}">生产录入</a>
```

在 mobile-nav 同一位置同步追加：

```html
      <a href="{{ url_for('production.products_list') }}">生产</a>
```

- [ ] **Step 4: 创建 `templates/production/products.html`（空列表占位）**

> 注：暂不放 `+ 新建产品` 按钮（`production.product_new` 路由在 Task 4 才注册，放进去会在 Task 3 阶段导致 `/production` 渲染 500）。Task 4 替换为真查询时一起加回按钮。

```html
{% extends "base.html" %}
{% block content %}
<h2>产品</h2>
<p class="muted">暂无产品，点击右上"新建产品"开始。</p>
{% endblock %}
```

- [ ] **Step 5: 启动应用验证路由通**

访问 `/land` → 点"生产录入" → `/production` 出现空列表页（"暂无产品"）。侧栏"生产录入"链接可点。

- [ ] **Step 6: Commit**

```bash
git add blueprints/production.py app.py templates/base.html templates/production/products.html
git commit -m "feat(production): register blueprint + sidebar entry + products list stub"
```

---

## Task 4: 新建 / 编辑 / 删除 产品（含 BOM 维护）

**Files:**
- Modify: `blueprints/production.py`（追加 `product_new` / `product_edit` / `product_delete` 三个路由；扩 `products_list` 真实查询）
- Create: `templates/production/product_edit.html`

- [ ] **Step 1: 替换 `products_list` 路由（真查询）**

在 `blueprints/production.py` 顶部 imports 加：

```python
from db import get_warehouse_db
from flask import flash, redirect, request, url_for
from permissions import require_login, require_role
from ._helpers import now, parse_qty
from .auth import audit
```

替换原 `products_list`：

```python
@bp.route("/production", methods=["GET"])
@require_login
def products_list():
    db = get_warehouse_db()
    products_data = db.execute(
        """SELECT p.*, COUNT(b.id) AS bom_count
           FROM products p
           LEFT JOIN product_bom b ON b.product_id = p.id
           GROUP BY p.id ORDER BY p.id DESC"""
    ).fetchall()
    return render("production/products.html", products=products_data)
```

- [ ] **Step 2: 新建产品路由**

```python
@bp.route("/production/products/new", methods=["GET", "POST"])
@require_role("manager")
def product_new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        unit = request.form.get("unit", "件").strip() or "件"
        note = request.form.get("note", "").strip()
        if not name:
            flash("产品名称为必填")
            return redirect(url_for("production.product_new"))
        import sqlite3
        db = get_warehouse_db()
        try:
            db.execute(
                "INSERT INTO products (name, unit, note, created_at) VALUES (?, ?, ?, ?)",
                (name, unit, note, now()),
            )
            new_id = int(db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
            db.commit()
            audit("production.product.create", "product", new_id, {"name": name})
            flash("产品已创建，请补充配方")
            return redirect(url_for("production.product_edit", product_id=new_id))
        except sqlite3.IntegrityError:
            flash("产品名称已存在")
            return redirect(url_for("production.product_new"))
    return render("production/product_edit.html", product=None, bom_rows=[], items=[])


def _load_items_for_bom():
    """Return items for the BOM item picker: id, name, unit, category_name."""
    db = get_warehouse_db()
    return db.execute(
        """SELECT i.id, i.name, i.unit, c.name AS category_name
           FROM items i JOIN categories c ON c.id = i.category_id
           ORDER BY c.name, i.name"""
    ).fetchall()
```

- [ ] **Step 3: 编辑产品 + BOM 路由**

```python
@bp.route("/production/products/<int:product_id>/edit", methods=["GET", "POST"])
@require_role("manager")
def product_edit(product_id: int):
    db = get_warehouse_db()
    product = db.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if product is None:
        flash("产品不存在")
        return redirect(url_for("production.products_list"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        unit = request.form.get("unit", "件").strip() or "件"
        note = request.form.get("note", "").strip()
        if not name:
            flash("产品名称为必填")
            return redirect(url_for("production.product_edit", product_id=product_id))
        db.execute(
            "UPDATE products SET name=?, unit=?, note=? WHERE id=?",
            (name, unit, note, product_id),
        )
        # BOM rows
        bom_ids = request.form.getlist("bom_row_id")
        item_ids = request.form.getlist("bom_item_id")
        qtys = request.form.getlist("bom_qty")
        deletes = request.form.getlist("bom_delete")
        added, removed, updated = 0, 0, 0
        for i in range(len(bom_ids)):
            row_id = bom_ids[i].strip()
            if i < len(deletes) and deletes[i] == "1":
                if row_id:
                    db.execute("DELETE FROM product_bom WHERE id = ?", (int(row_id),))
                    removed += 1
                continue
            item_id = item_ids[i].strip() if i < len(item_ids) else ""
            qty = parse_qty(qtys[i]) if i < len(qtys) else 0.0
            if not item_id or qty <= 0:
                continue
            if row_id:
                db.execute(
                    "UPDATE product_bom SET item_id=?, qty_per_unit=? WHERE id=?",
                    (int(item_id), qty, int(row_id)),
                )
                updated += 1
            else:
                try:
                    db.execute(
                        "INSERT INTO product_bom (product_id, item_id, qty_per_unit) VALUES (?, ?, ?)",
                        (product_id, int(item_id), qty),
                    )
                    added += 1
                except Exception:
                    flash(f"第 {i+1} 行原料重复或无效")
        db.commit()
        audit("production.product.update", "product", product_id, {
            "added": added, "removed": removed, "updated": updated,
        })
        flash("产品已保存")
        return redirect(url_for("production.product_edit", product_id=product_id))

    bom_rows = db.execute(
        """SELECT b.*, i.name AS item_name, i.unit AS item_unit
           FROM product_bom b JOIN items i ON i.id = b.item_id
           WHERE b.product_id = ? ORDER BY b.id""",
        (product_id,),
    ).fetchall()
    items = _load_items_for_bom()
    return render("production/product_edit.html", product=product, bom_rows=bom_rows, items=items)
```

- [ ] **Step 4: 删除产品路由**

```python
@bp.route("/production/products/<int:product_id>/delete", methods=["POST"])
@require_role("manager")
def product_delete(product_id: int):
    db = get_warehouse_db()
    used = db.execute(
        "SELECT COUNT(*) AS c FROM production_runs WHERE product_id = ?",
        (product_id,),
    ).fetchone()["c"]
    if used > 0:
        flash("该产品存在生产记录，无法删除")
        return redirect(url_for("production.products_list"))
    db.execute("DELETE FROM products WHERE id = ?", (product_id,))
    db.commit()
    audit("production.product.delete", "product", product_id)
    flash("产品已删除")
    return redirect(url_for("production.products_list"))
```

- [ ] **Step 5: 创建 `templates/production/product_edit.html`**

```html
{% extends "base.html" %}
{% block content %}
<div class="list-head">
  <h2>{% if product %}编辑产品{% else %}新建产品{% endif %}</h2>
  <a class="btn" href="{{ url_for('production.products_list') }}">← 返回列表</a>
</div>

<form method="post" class="form-block">
  <label>产品名称 <input name="name" required value="{{ product['name'] if product else '' }}" /></label>
  <label>产出单位 <input name="unit" value="{{ product['unit'] if product else '件' }}" /></label>
  <label>备注 <input name="note" value="{{ product['note'] if product else '' }}" /></label>
  <button type="submit" class="btn-primary">保存产品</button>
</form>

{% if product %}
<hr/>
<h3>配方 (BOM)</h3>
<p class="muted">每生产 1 单位产品，消耗多少该原料。</p>

<form method="post" id="bom-form">
  <input type="hidden" name="name" value="{{ product['name'] }}" />
  <input type="hidden" name="unit" value="{{ product['unit'] }}" />
  <input type="hidden" name="note" value="{{ product['note'] or '' }}" />

  <table class="bom-table">
    <thead><tr><th>原料</th><th>每单位用量</th><th>操作</th></tr></thead>
    <tbody id="bom-body">
      {% for b in bom_rows %}
      <tr>
        <td>
          <input type="hidden" name="bom_row_id" value="{{ b['id'] }}" />
          <select name="bom_item_id">
            {% for it in items %}
            <option value="{{ it['id'] }}" {% if it['id']==b['item_id'] %}selected{% endif %}>
              {{ it['category_name'] }} / {{ it['name'] }} ({{ it['unit'] }})
            </option>
            {% endfor %}
          </select>
        </td>
        <td><input type="number" step="0.01" min="0" name="bom_qty" value="{{ b['qty_per_unit'] }}" /></td>
        <td>
          <label class="del-flag"><input type="checkbox" name="bom_delete" value="1" /> 删除</label>
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>

  <button type="button" id="bom-add" class="btn-sm">+ 新增一行</button>
  <button type="submit" class="btn-primary">保存配方</button>
</form>

<script>
document.getElementById('bom-add').addEventListener('click', function() {
  var tbody = document.getElementById('bom-body');
  var tr = document.createElement('tr');
  tr.innerHTML = `
    <td>
      <input type="hidden" name="bom_row_id" value="" />
      <select name="bom_item_id">
        {% for it in items %}<option value="{{ it['id'] }}">{{ it['category_name'] }} / {{ it['name'] }} ({{ it['unit'] }})</option>{% endfor %}
      </select>
    </td>
    <td><input type="number" step="0.01" min="0" name="bom_qty" value="0" /></td>
    <td><label class="del-flag"><input type="checkbox" name="bom_delete" value="1" /> 删除</label></td>
  `;
  tbody.appendChild(tr);
});
</script>
{% endif %}
{% endblock %}
```

- [ ] **Step 6: 扩展 `templates/production/products.html`，列出产品卡片**

覆盖原 `templates/production/products.html`：

```html
{% extends "base.html" %}
{% block content %}
<div class="list-head">
  <h2>产品</h2>
  <a class="btn" href="{{ url_for('production.product_new') }}">+ 新建产品</a>
</div>

{% if products %}
  <div class="card-list">
    {% for p in products %}
    <div class="card">
      <div class="card-main">
        <h3>{{ p['name'] }}</h3>
        <p class="muted">单位：{{ p['unit'] }}{% if p['note'] %} · {{ p['note'] }}{% endif %}</p>
        <p>配方项数：<strong>{{ p['bom_count'] }}</strong></p>
      </div>
      <div class="card-actions">
        <a class="btn-sm" href="{{ url_for('production.session') }}?product_id={{ p['id'] }}">录入</a>
        <a class="btn-sm" href="{{ url_for('production.product_edit', product_id=p['id']) }}">编辑配方</a>
        <form method="post" action="{{ url_for('production.product_delete', product_id=p['id']) }}" onsubmit="return confirm('确认删除该产品？');">
          <button class="btn-sm btn-delete" type="submit">删除</button>
        </form>
      </div>
    </div>
    {% endfor %}
  </div>
{% else %}
  <p class="muted">暂无产品，点击右上"新建产品"开始。</p>
{% endif %}
{% endblock %}
```

- [ ] **Step 7: 手工冒烟**

1. 访问 `/production` → 看到"暂无产品"
2. 点"新建产品" → 输入名称 → 保存 → 跳到编辑页
3. 点"+ 新增一行" → 选原料 → 输入用量 → 保存配方
4. 返回列表 → 卡片显示"配方项数: 1"
5. 点击该卡片的"删除" → 卡片消失

- [ ] **Step 8: Commit**

```bash
git add blueprints/production.py templates/production/product_edit.html templates/production/products.html
git commit -m "feat(production): product CRUD with BOM editor"
```

---

## Task 5: 录入页（`/production/session` + `/production/submit`）

**Files:**
- Modify: `blueprints/production.py`（追加 `session` / `submit`）
- Create: `templates/production/session.html`

- [ ] **Step 1: 录入页 `GET` 路由**

在 `blueprints/production.py` 末尾追加：

```python
@bp.route("/production/session", methods=["GET"])
@require_login
def session():
    db = get_warehouse_db()
    products_data = db.execute("SELECT id, name, unit FROM products ORDER BY name").fetchall()
    product_id = request.args.get("product_id", type=int)
    bom = []
    chosen = None
    if product_id:
        chosen = db.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
        if chosen is not None:
            bom = db.execute(
                """SELECT b.id AS bom_id, b.qty_per_unit, b.item_id,
                          i.name AS item_name, i.unit, i.quantity AS stock
                   FROM product_bom b JOIN items i ON i.id = b.item_id
                   WHERE b.product_id = ? ORDER BY b.id""",
                (product_id,),
            ).fetchall()
    return render(
        "production/session.html",
        products=products_data,
        chosen=chosen,
        bom=bom,
    )
```

- [ ] **Step 2: 提交路由（核心事务）**

```python
@bp.route("/production/submit", methods=["POST"])
@require_login
def submit():
    db = get_warehouse_db()
    product_id = request.form.get("product_id", type=int)
    output_qty = parse_qty(request.form.get("output_qty", "0"))
    note = request.form.get("note", "").strip()

    if not product_id or output_qty <= 0:
        flash("请选择产品并填写大于 0 的产出量")
        return redirect(url_for("production.session"))

    product = db.execute("SELECT id, name, unit FROM products WHERE id = ?", (product_id,)).fetchone()
    if product is None:
        flash("产品不存在")
        return redirect(url_for("production.session"))

    bom_rows = db.execute(
        """SELECT b.id AS bom_id, b.item_id, b.qty_per_unit,
                  i.name AS item_name, i.quantity AS stock
           FROM product_bom b JOIN items i ON i.id = b.item_id
           WHERE b.product_id = ? ORDER BY b.id""",
        (product_id,),
    ).fetchall()
    if not bom_rows:
        flash("该产品尚未配置配方")
        return redirect(url_for("production.session", product_id=product_id))

    # Build planned & actual
    plan = []
    for b in bom_rows:
        planned = round(b["qty_per_unit"] * output_qty, 2)
        raw = request.form.get(f"actual_{b['item_id']}", "").strip()
        actual = parse_qty(raw) if raw != "" else planned
        if actual < 0:
            flash(f"原料 {b['item_name']} 实际消耗不能为负")
            return redirect(url_for("production.session", product_id=product_id))
        plan.append((int(b["item_id"]), b["item_name"], float(b["stock"]), planned, actual))

    # 硬性拦截: 库存不足
    for item_id, name, stock, planned, actual in plan:
        if actual > stock:
            flash(f"原料 {name} 库存不足（需 {actual}，现有 {stock}）")
            return redirect(url_for("production.session", product_id=product_id))

    from flask import g
    created_by = g.user["username"] if g.get("user") else None
    cur = db.execute(
        """INSERT INTO production_runs
           (product_id, output_qty, note, rolled_back, created_by, created_at)
           VALUES (?, ?, ?, 0, ?, ?)""",
        (product_id, output_qty, note, created_by, now()),
    )
    run_id = int(cur.lastrowid)

    for item_id, name, stock, planned, actual in plan:
        db.execute(
            """INSERT INTO production_run_items
               (run_id, item_id, planned_qty, actual_qty) VALUES (?, ?, ?, ?)""",
            (run_id, item_id, planned, actual),
        )
        db.execute(
            "UPDATE items SET quantity = quantity - ?, updated_at = ? WHERE id = ?",
            (actual, now(), item_id),
        )
        db.execute(
            """INSERT INTO stock_movements (item_id, action, delta, note, created_at)
               VALUES (?, '生产消耗', ?, ?, ?)""",
            (item_id, -actual, f"生产记录#{run_id}领料", now()),
        )

    db.commit()
    audit("production.run.submit", "run", run_id, {
        "product_id": product_id, "output_qty": output_qty, "rows": len(plan),
    })
    flash("生产已记录")
    return redirect(url_for("production.runs_list"))
```

- [ ] **Step 3: 创建 `templates/production/session.html`**

```html
{% extends "base.html" %}
{% block content %}
<div class="list-head">
  <h2>生产录入</h2>
  <a class="btn" href="{{ url_for('production.runs_list') }}">历史记录 →</a>
</div>

<form method="get" action="{{ url_for('production.session') }}" class="form-inline">
  <label>产品
    <select name="product_id" onchange="this.form.submit()">
      <option value="">-- 请选择产品 --</option>
      {% for p in products %}
      <option value="{{ p['id'] }}" {% if chosen and chosen['id']==p['id'] %}selected{% endif %}>
        {{ p['name'] }} ({{ p['unit'] }})
      </option>
      {% endfor %}
    </select>
  </label>
</form>

{% if chosen and bom %}
<form method="post" action="{{ url_for('production.submit') }}" class="session-form" id="prod-form">
  <input type="hidden" name="product_id" value="{{ chosen['id'] }}" />

  <div class="session-head">
    <div class="session-actions">
      <label>产出量（{{ chosen['unit'] }}）
        <input type="number" step="0.01" min="0" name="output_qty" id="output_qty" required />
      </label>
      <label>备注 <input name="note" placeholder="可选" /></label>
      <button type="submit" id="submit-btn">提交</button>
    </div>
    <p class="hint">实际消耗可偏离配方；原料不足将无法提交。</p>
  </div>

  <div class="entry-grid" id="bom-grid">
    {% for b in bom %}
    <div class="entry-row" data-stock="{{ b['stock'] }}">
      <div class="er-meta">
        <span class="er-name">{{ b['item_name'] }}</span>
        <span class="er-cat muted">当前库存 {{ b['stock'] | fmt_qty }} {{ b['unit'] }}</span>
      </div>
      <div class="er-stock">
        <span>配方 / 单位 <strong>{{ b['qty_per_unit'] | fmt_qty }}</strong> {{ b['unit'] }}</span>
        <span>本批计划 <strong class="planned" data-qpu="{{ b['qty_per_unit'] }}">0.00</strong> {{ b['unit'] }}</span>
      </div>
      <div class="er-input">
        <input
          type="number" step="0.01" min="0" inputmode="decimal"
          name="actual_{{ b['item_id'] }}" data-stock="{{ b['stock'] }}"
          class="actual" placeholder="0" autocomplete="off" />
        <span class="er-unit">{{ b['unit'] }}</span>
      </div>
    </div>
    {% endfor %}
  </div>
</form>

<script>
(function() {
  var out = document.getElementById('output_qty');
  var rows = document.querySelectorAll('#bom-grid .entry-row');
  var btn = document.getElementById('submit-btn');

  function recompute() {
    var oq = parseFloat(out.value || '0');
    var anyShort = false;
    rows.forEach(function(r) {
      var qpu = parseFloat(r.querySelector('.planned').getAttribute('data-qpu'));
      var planned = Math.round(qpu * oq * 100) / 100;
      r.querySelector('.planned').textContent = planned.toFixed(2);
      var act = r.querySelector('.actual');
      var stock = parseFloat(act.getAttribute('data-stock'));
      var actVal = parseFloat(act.value);
      var show = (actVal > stock);
      r.classList.toggle('is-short', show);
      if (show) anyShort = true;
    });
    btn.disabled = (oq <= 0) || anyShort;
  }
  out.addEventListener('input', recompute);
  rows.forEach(function(r) { r.querySelector('.actual').addEventListener('input', recompute); });
  recompute();
})();
</script>
{% elif chosen %}
  <p class="muted">该产品还没有配方，请先 <a href="{{ url_for('production.product_edit', product_id=chosen['id']) }}">编辑配方</a>。</p>
{% endif %}
{% endblock %}
```

- [ ] **Step 4: 手工冒烟**

1. 访问 `/production/session` → 选产品 → 列表展示 BOM 行
2. 输入产出量 → 计划量随公式更新
3. 修改某行 `actual` → 超过库存时该行标红、提交按钮 disabled
4. 把 `actual` 改回 ≤ 库存 → 提交按钮恢复
5. 点"提交" → 跳到 `/production/runs`（Task 6 还没做，预期 404；先看 flash 提示"生产已记录"是否落库）

SQL 验证（`sqlite3 db/warehouses/<code>.db`）：
```sql
SELECT * FROM production_runs ORDER BY id DESC LIMIT 1;
SELECT * FROM production_run_items ORDER BY id DESC LIMIT 5;
SELECT * FROM stock_movements WHERE action='生产消耗' ORDER BY id DESC LIMIT 5;
SELECT id, name, quantity FROM items;
```

期望：`production_runs` 多 1 行，`production_run_items` 多 N 行（N=BOM 行数），`stock_movements` 多 N 行 `action='生产消耗'` 且 `delta` 为负，`items.quantity` 减扣。

- [ ] **Step 5: Commit**

```bash
git add blueprints/production.py templates/production/session.html
git commit -m "feat(production): entry session + submit with stock pre-check"
```

---

## Task 6: 历史记录 + 回退 + 删除

**Files:**
- Modify: `blueprints/production.py`（追加 `runs_list` / `rollback` / `delete_run`）
- Create: `templates/production/runs.html`

- [ ] **Step 1: 历史列表路由**

在 `blueprints/production.py` 末尾追加：

```python
@bp.route("/production/runs", methods=["GET"])
@require_login
def runs_list():
    db = get_warehouse_db()
    runs = db.execute(
        """SELECT r.*, p.name AS product_name, p.unit AS product_unit
           FROM production_runs r JOIN products p ON p.id = r.product_id
           ORDER BY r.id DESC LIMIT 200"""
    ).fetchall()
    # attach per-run items for the expanded view
    enriched = []
    for r in runs:
        items = db.execute(
            """SELECT pri.*, i.name AS item_name, i.unit
               FROM production_run_items pri JOIN items i ON i.id = pri.item_id
               WHERE pri.run_id = ? ORDER BY pri.id""",
            (r["id"],),
        ).fetchall()
        enriched.append({**dict(r), "items": items})
    return render("production/runs.html", runs=enriched)
```

- [ ] **Step 2: 回退路由**

```python
@bp.route("/production/runs/<int:run_id>/rollback", methods=["POST"])
@require_role("manager")
def rollback(run_id: int):
    db = get_warehouse_db()
    run = db.execute(
        "SELECT id, rolled_back FROM production_runs WHERE id = ?", (run_id,),
    ).fetchone()
    if run is None:
        flash("生产记录不存在")
        return redirect(url_for("production.runs_list"))
    if int(run["rolled_back"]) == 1:
        flash("该记录已回退")
        return redirect(url_for("production.runs_list"))

    items = db.execute(
        "SELECT item_id, actual_qty FROM production_run_items WHERE run_id = ?",
        (run_id,),
    ).fetchall()
    for it in items:
        qty = parse_qty(it["actual_qty"])
        db.execute(
            "UPDATE items SET quantity = quantity + ?, updated_at = ? WHERE id = ?",
            (qty, now(), int(it["item_id"])),
        )
        db.execute(
            """INSERT INTO stock_movements (item_id, action, delta, note, created_at)
               VALUES (?, '生产消耗回退', ?, ?, ?)""",
            (int(it["item_id"]), qty, f"回退生产记录#{run_id}", now()),
        )
    db.execute("UPDATE production_runs SET rolled_back = 1 WHERE id = ?", (run_id,))
    db.commit()
    audit("production.run.rollback", "run", run_id)
    flash("生产记录已回退")
    return redirect(url_for("production.runs_list"))
```

- [ ] **Step 3: 删除路由**

```python
@bp.route("/production/runs/<int:run_id>/delete", methods=["POST"])
@require_role("manager")
def delete_run(run_id: int):
    db = get_warehouse_db()
    run = db.execute(
        "SELECT id, rolled_back FROM production_runs WHERE id = ?", (run_id,),
    ).fetchone()
    if run is None:
        flash("生产记录不存在")
        return redirect(url_for("production.runs_list"))

    qty_total = 0.0
    if int(run["rolled_back"]) == 0:
        items = db.execute(
            "SELECT item_id, actual_qty FROM production_run_items WHERE run_id = ?",
            (run_id,),
        ).fetchall()
        for it in items:
            qty = parse_qty(it["actual_qty"])
            qty_total += qty
            db.execute(
                "UPDATE items SET quantity = quantity + ?, updated_at = ? WHERE id = ?",
                (qty, now(), int(it["item_id"])),
            )
            db.execute(
                """INSERT INTO stock_movements (item_id, action, delta, note, created_at)
                   VALUES (?, '生产消耗回退', ?, ?, ?)""",
                (int(it["item_id"]), qty, f"删除生产记录#{run_id}回滚", now()),
            )

    db.execute("DELETE FROM production_runs WHERE id = ?", (run_id,))
    db.commit()
    audit("production.run.delete", "run", run_id, {
        "rolled_back": int(run["rolled_back"]),
        "qty_total": qty_total,
    })
    flash("生产记录已删除" + ("，库存已归还" if int(run["rolled_back"]) == 0 else ""))
    return redirect(url_for("production.runs_list"))
```

- [ ] **Step 4: 创建 `templates/production/runs.html`**

```html
{% extends "base.html" %}
{% block content %}
<div class="list-head">
  <h2>生产记录</h2>
  <a class="btn" href="{{ url_for('production.session') }}">+ 新建生产</a>
</div>

{% if runs %}
<div class="card-list">
  {% for r in runs %}
  <div class="card">
    <div class="card-main">
      <h3>{{ r['product_name'] }} <small class="muted">产出 {{ r['output_qty'] | fmt_qty }} {{ r['product_unit'] }}</small></h3>
      <p class="muted">
        {{ r['created_at'] }}{% if r['created_by'] %} · {{ r['created_by'] }}{% endif %}
        {% if r['note'] %} · {{ r['note'] }}{% endif %}
        {% if r['rolled_back'] == 1 %} · <span class="status-rolled">已回退</span>{% endif %}
      </p>
      <ul class="bom-mini">
        {% for it in r['items'] %}
        <li>
          {{ it['item_name'] }}：
          计划 {{ it['planned_qty'] | fmt_qty }} /
          实际 <strong>{{ it['actual_qty'] | fmt_qty }}</strong>
          {{ it['unit'] }}
          {% if it['planned_qty'] != it['actual_qty'] %}<span class="muted">（偏离配方）</span>{% endif %}
        </li>
        {% endfor %}
      </ul>
    </div>
    <div class="card-actions">
      {% if r['rolled_back'] == 0 %}
      <form method="post" action="{{ url_for('production.rollback', run_id=r['id']) }}" onsubmit="return confirm('确认回退该生产记录？库存将归还。');">
        <button class="btn-sm" type="submit">回退</button>
      </form>
      <form method="post" action="{{ url_for('production.delete_run', run_id=r['id']) }}" onsubmit="return confirm('确认删除？未回退的记录会先回滚库存。');">
        <button class="btn-sm btn-delete" type="submit">删除</button>
      </form>
      {% else %}
      <form method="post" action="{{ url_for('production.delete_run', run_id=r['id']) }}" onsubmit="return confirm('确认删除该记录？');">
        <button class="btn-sm btn-delete" type="submit">删除</button>
      </form>
      {% endif %}
    </div>
  </div>
  {% endfor %}
</div>
{% else %}
  <p class="muted">暂无生产记录。</p>
{% endif %}
{% endblock %}
```

- [ ] **Step 5: 手工冒烟**

1. `/production/runs` → 看到上一步提交的生产记录，卡片展开原料计划/实际
2. 点"回退" → 卡片状态变"已回退"，对应原料 `items.quantity` 加回
3. 找另一条未回退记录点"删除" → 库存归还且记录消失
4. 对已回退的记录点"删除" → 直接消失（不写流水）

- [ ] **Step 6: Commit**

```bash
git add blueprints/production.py templates/production/runs.html
git commit -m "feat(production): run list + rollback + delete"
```

---

## Task 7: 库存查阅 + 汇总 接入生产消耗

**Files:**
- Modify: `blueprints/items.py::inventory_view`（7 日消耗 action 集合）
- Modify: `blueprints/core.py::summary`（总消耗金额 + 顶部"按生产产品的消耗"卡片）
- Modify: `templates/inventory.html`（无文本改动，但验证）

- [ ] **Step 1: 改 `inventory_view` 7 日消耗 action 集合**

打开 `blueprints/items.py`，在 `inventory_view` 内定位到 `WHERE m.action = '出库'` 这两处（**共 2 处**：第 149 行和第 150 行附近），都改为 `WHERE m.action IN ('出库', '生产消耗')`。

完整 SQL 改动片段（替换原有 cte 中 WHERE 子句）：

```python
           ) c7 ON c7.item_id = i.id
           WHERE c.name IN ({placeholders})
             AND (? = '' OR i.name LIKE '%' || ? || '%' OR i.sku LIKE '%' || ? || '%')
             AND (? = '' OR c.name = ?)
           ORDER BY (i.quantity <= i.safety_stock) DESC, i.name""",
            ("""
           SELECT m.item_id,
                  ABS(SUM(m.delta)) AS qty,
                  ROUND(ABS(SUM(m.delta)) * i2.unit_cost, 2) AS value,
                  COUNT(DISTINCT substr(m.created_at, 1, 10)) AS days
           FROM stock_movements m
           JOIN items i2 ON i2.id = m.item_id
           WHERE m.action IN ('出库', '生产消耗')
             AND m.created_at >= datetime('now', '-7 days')
           GROUP BY m.item_id
           """,) + params + [q, q, q, cat, cat],
        ).fetchall()
```

**注意：上面仅为说明**。实际修改时只动 SQL 字符串里的 `WHERE m.action = '出库'` → `WHERE m.action IN ('出库', '生产消耗')`，不要重写整个函数。

- [ ] **Step 2: 改 `summary` 总消耗金额**

打开 `blueprints/core.py`，定位到 `total_consumed_value` 的查询（第 75 行附近）。在原查询后追加生产消耗查询并求和：

```python
    # 口径:消耗金额 = 出库(rolled_back=0) + 生产消耗(run.rolled_back=0)
    consumed_outbound = db.execute(
        """SELECT COALESCE(SUM(o.requested_quantity * i.unit_cost), 0) AS c
           FROM outbound_requests o JOIN items i ON i.id = o.item_id
           WHERE o.rolled_back = 0"""
    ).fetchone()["c"]
    consumed_production = db.execute(
        """SELECT COALESCE(SUM(pri.actual_qty * i.unit_cost), 0) AS c
           FROM production_run_items pri
           JOIN production_runs pr ON pr.id = pri.run_id
           JOIN items i ON i.id = pri.item_id
           WHERE pr.rolled_back = 0"""
    ).fetchone()["c"]
    total_consumed_value = float(consumed_outbound) + float(consumed_production)
```

把原 `total_consumed_value = db.execute(...).fetchone()["c"]` 这一行替换为上面三行。然后 `return render_template(...)` 里 `total_consumed_value=round(total_consumed_value, 2)` 不变。

- [ ] **Step 3: 验证**

启动应用 → 访问 `/inventory` → 7 日消耗应包含生产领料数量（至少会有 1 个原料的 `consume_7d_qty > 0`，因为我们在 Task 5/6 已经触发过生产）。
访问 `/summary` → `total_consumed_value` 应大于"仅看 outbound_requests"的口径。

- [ ] **Step 4: Commit**

```bash
git add blueprints/items.py blueprints/core.py
git commit -m "feat(production): include production consumption in 7d/inventory and summary"
```

---

## Task 8: CSV 导出 + AGENT.md / README

**Files:**
- Modify: `blueprints/production.py`（追加 `runs_csv`）
- Modify: `AGENT.md`（目录结构 / 数据表说明 / 关键业务约束）
- Modify: `README.md`（功能列表）
- Modify: `templates/production/runs.html`（顶部加"导出 CSV"按钮）

- [ ] **Step 1: CSV 导出路由**

在 `blueprints/production.py` 顶部 imports 加：

```python
import csv
import io
from datetime import datetime
from flask import current_app
```

在文件末尾追加：

```python
@bp.route("/production/runs.csv", methods=["GET"])
@require_login
def runs_csv():
    """CSV download: one row per production_run_item.

    口径:包含 rolled_back=1 的行(审计需要),文件加 UTF-8 BOM 以兼容 Excel CN。
    """
    db = get_warehouse_db()
    rows = db.execute(
        """SELECT pr.id AS run_id, pr.created_at, pr.created_by,
                  pr.output_qty, pr.note AS run_note, pr.rolled_back,
                  p.name AS product_name, p.unit AS product_unit,
                  pri.planned_qty, pri.actual_qty,
                  i.sku AS item_sku, i.name AS item_name, i.unit AS item_unit
           FROM production_runs pr
           JOIN products p ON p.id = pr.product_id
           JOIN production_run_items pri ON pri.run_id = pr.id
           JOIN items i ON i.id = pri.item_id
           ORDER BY pr.id DESC, pri.id"""
    ).fetchall()
    audit("production.runs_export", "report", None, {"rows": len(rows)})

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "run_id", "created_at", "created_by", "product_name", "output_qty",
        "output_unit", "item_sku", "item_name", "planned_qty", "actual_qty",
        "item_unit", "rolled_back", "run_note",
    ])
    for r in rows:
        writer.writerow([
            r["run_id"], r["created_at"], r["created_by"] or "",
            r["product_name"], r["output_qty"], r["product_unit"],
            r["item_sku"], r["item_name"], r["planned_qty"], r["actual_qty"],
            r["item_unit"], r["rolled_back"], r["run_note"] or "",
        ])
    today = datetime.now().strftime("%Y%m%d")
    response = current_app.response_class(
        output.getvalue().encode("utf-8-sig"),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename=production_runs_{today}.csv"},
    )
    return response
```

- [ ] **Step 2: `templates/production/runs.html` 顶部加导出按钮**

打开 `templates/production/runs.html`，在 `<div class="list-head">` 里 `+ 新建生产` 链接前加：

```html
  <a class="btn" href="{{ url_for('production.runs_csv') }}">导出 CSV</a>
```

- [ ] **Step 3: 更新 `AGENT.md`**

打开 `AGENT.md`，在"目录结构"块里追加：
```
- `blueprints/production.py`：生产录入（产品/BOM/生产批次/回退/CSV）
```

在"数据表说明"块里追加：
```
- `products`：产品定义（独立于库存品）
- `product_bom`：产品-原料配方
- `production_runs`：生产批次
- `production_run_items`：批次原料消耗
```

在"关键业务约束"里追加：
```
- 产品定义独立于库存品（products 表），产品本身不入库、不产生库存
- 生产录入时若任一原料库存不足，提交被硬性拦截
```

- [ ] **Step 4: 更新 `README.md`**

打开 `README.md`，在功能列表追加：
```
- 生产录入（产品配方驱动，按产出量自动扣减原料）
```

- [ ] **Step 5: 手工冒烟**

1. 访问 `/production/runs` → 顶部多了"导出 CSV"按钮
2. 点击 → 浏览器下载 `production_runs_YYYYMMDD.csv`
3. 用 Excel 打开 → 无乱码、列顺序一致
4. 记录数 ≥ Task 5/6 创建的 run × BOM 项数

- [ ] **Step 6: Commit**

```bash
git add blueprints/production.py templates/production/runs.html AGENT.md README.md
git commit -m "feat(production): CSV export + AGENT.md/README updates"
```

---

## Task 9: 端到端冒烟

**Files:** (无)

- [ ] **Step 1: 重启应用，从空状态跑一遍完整流程**

```bash
flask --app app run --host 0.0.0.0 --port 5001 &
sleep 1
```

- 登录 → 选仓库 → 落地到 `/land`
- 走 `/production` → 新建产品"奶茶" → 编辑配方添加 2 项原料（如：茶叶 0.05 / 牛奶 0.2）
- 走 `/production/session` → 选"奶茶" → 产出 100 → 实际消耗默认 5/20 → 提交
- `/production/runs` → 看到记录 → 卡片展开显示计划/实际 → 状态正常
- `/inventory` → 茶叶、牛奶的 `consume_7d_qty` 各为 5/20
- `/summary` → `total_consumed_value` 大于 0 且包含"奶茶"的原料价值
- 走回退 → 卡片变"已回退"，茶叶、牛奶库存加回
- 走删除（已回退的）→ 记录消失
- 再走一遍"硬性拦截"：实际消耗改 > 库存 → 行变红、提交按钮 disabled
- `/production/runs.csv` → 下载 → Excel 打开 → 列/数据齐

- [ ] **Step 2: 检查最终状态**

```bash
git log --oneline -10
git status
```

期望：8 个新 commit（Task 1~8）落 main，无未提交文件（除 `.claude/` `.playwright-mcp/` 这些已存在的 untracked）。

- [ ] **Step 3: 杀掉服务并收尾**

```bash
kill %1
```

---

## Self-Review

**Spec coverage** (对照 `2026-06-20-production-entry-design.md`):

- [x] 4 张新表 → Task 1
- [x] `/land` 落地页 → Task 2
- [x] 侧栏入口 → Task 3
- [x] 产品增/改/删 + BOM 维护 → Task 4
- [x] 录入页 + submit + 硬性拦截 → Task 5
- [x] 历史列表 + 回退 + 删除 → Task 6
- [x] 库存查阅 7 日消耗 + 汇总口径 → Task 7
- [x] CSV 导出 → Task 8
- [x] AGENT.md / README → Task 8
- [x] 端到端冒烟 → Task 9

**Placeholder scan**: 无 TBD / "类似 Task N" / "implement later"；所有代码块完整。

**Type/name 一致性**:
- `products_list` / `product_new` / `product_edit` / `product_delete` / `session` / `submit` / `runs_list` / `rollback` / `delete_run` / `runs_csv` 全部唯一且在 Task 间一致
- 模板名 `production/products.html` / `production/product_edit.html` / `production/session.html` / `production/runs.html` 与路由 render 调用一致
- 流水 action 字符串 `'生产消耗'` / `'生产消耗回退'` 在 Task 1/5/6/7/8 一致
- 模板 jinja 过滤器 `fmt_qty` 在 Task 4/5/6/8 一致（来自 `blueprints/_helpers.py`）

**注意事项**:
- Task 7 Step 1 的"完整 SQL 改动片段"用了占位说明，**实际执行时只改 `WHERE m.action = '出库'` 这一个子句**，不要重写整个函数 — 已在该 Step 末附提醒。
- Task 6 在 spec 里命名是 `/production/runs/<id>/delete`，但 Python 函数名要避免与 `from flask import ...` 的 `delete` 冲突（restock / outbound 都是直接叫 `delete`），所以本计划用 `delete_run`。模板 `url_for('production.delete_run', ...)` 已在 Task 6 Step 4 中对齐。
