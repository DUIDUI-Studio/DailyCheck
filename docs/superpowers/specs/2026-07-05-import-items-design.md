# 品项导入（xlsx → 仓库 items）设计

> 范围:**只做品项导入**。`items.quantity` 一律按 0 创建,不导入库存数,不写
> `stock_movements`。后续盘点 / 入库走现有页面。
>
> 落地仓:**泰柯店**(`泰柯盘点表.xlsx`,81 行物料,5 个分组)。但本次设计通用化——
> 任何 admin 已建好的仓库都可作为目标。

---

## 0. 设计依据 & 业务背景

### 0.1 Excel 文件事实

- 文件:`docs/superpowers/泰柯盘点表.xlsx`
- Sheet:`Sheet1`,A1:I86(标题 + 表头 + 81 行数据 + 1 行合计)
- 表头(第 2 行):分类 | 物料名称 | SKU | 规格 | 单价/元 | 现有库存 | 单位 | 隐藏栏/盘点单位单价 | 库存金额
- 5 个分组(分类列首行出现,后续行向下合并风格):
  - **常温物料**(20 行)
  - **水果**(4 行)
  - **包装材料**(32 行)
  - **冷冻&冷藏食品**(14 行)
  - **定制周边**(6 行)
- 合计行:第 86 行,库存金额列 = 13888.106

### 0.2 系统现状

- 多仓库架构:master.db + 每个仓库独立 SQLite(`db/warehouses/<code>.db`)
- 仓库有 `code`(如 wh_001/wh_002)+ `name`(主仓库/泰柯店);创建通过
  `init_warehouse_db(db_path)` 自动种 `FIXED_CATEGORIES`(9 个)
- `blueprints/items.py` 3 处 SQL 用 `fixed_categories_in_clause()` 过滤品类
- `blueprints/_helpers.py::fixed_categories_in_clause()` 从 `config.FIXED_CATEGORIES`
  读全局常量
- `/items` chip bar 从 `categories` 表(`SELECT name FROM categories`)读;`/inventory` chip
  bar 从 items 实际出现的 `category_name` 动态生成;`/summary` `_compute_category_stats()`
  `FROM categories c LEFT JOIN ...` 遍历仓库自身 categories
- 已有 `db/import_items.py` CLI 工具,但期望 xlsx 是另一种 5 列格式(品项编码/品项
  名称/品项类别/订货单位/单价),与本 xlsx 不兼容。本次 web 导入是**新功能**。

### 0.3 用户决策(已确认)

| # | 问题 | 答复 |
|---|---|---|
| Q1 | 目标仓库 | **新建**泰柯店(仓库创建不在本次 spec 范围;admin 须先通过 `cli.py create-warehouse` 或用户管理页建好) |
| Q2 | 分组映射 | **独立分组**:xlsx 的 5 个分组直接成为泰柯店的 5 个新品类 |
| Q3 | 触发方式 | Web UI |
| Q4 | SKU 处理 | **不存 xlsx SKU**;统一 `gen_sku()` |
| Q5 | 单价口径 | `unit_cost = 列 8 隐藏栏/盘点单位单价` |
| Q6 | 单位字段 | **直接存 xlsx 的 11 种原始字符串**(箱/包/个/g/卷/条/袋/罐/盒/瓶/双/件) |
| Q7 | None 处理 | `现有库存=None` → `quantity=0`(本 spec 不导入库存数,所有 quantity 都为 0) |
| Q8 | 重复物料 | **全量覆盖**:commit 时 DELETE 该仓库 5 品类下 items 再 INSERT |
| Q9 | 库存数 | **不导入**;不写 `stock_movements` |
| OP1 | `/summary` 硬编码 | **不做硬编码**;本次确认现有 SQL 已天然读仓库 categories,0 改动 |
| OP2 | 旧 CLI 去留 | **保留**,仅改其报错文案 |
| OP3 | 缺品类引导 | **不给链接**,flash 列出缺哪些品类名 |
| OP4 | `/items` 入口位置 | **标题行右侧**(admin 可见) |

---

## 1. 核心架构改动:全项目去硬编码品类

`config.FIXED_CATEGORIES` 仍保留(作为新建老式仓库的默认种子),但**运行时 SQL
过滤改为读仓库自身 categories 表**。这是本次 spec 的最大改动面——影响 7 处
引用,其中 5 处是改动,2 处是死代码清理。

### 1.1 `blueprints/_helpers.py`

```python
def warehouse_categories_in_clause() -> tuple[str, list]:
    """按当前仓库自身的 categories 表生成 IN 子句。

    替代旧的 fixed_categories_in_clause(),让每家店用自己的品类集合。
    极端情况(仓库无 categories)返回 (1, [0]) 防御性 0 行。
    """
    db = get_warehouse_db()
    names = [r["name"] for r in db.execute(
        "SELECT name FROM categories ORDER BY id"
    ).fetchall()]
    if not names:
        return "1", [0]
    return ",".join("?" for _ in names), names


# 向后兼容别名(本次 spec 内全部调用方替换为新名,旧名可保留作过渡)
fixed_categories_in_clause = warehouse_categories_in_clause
```

### 1.2 `blueprints/items.py` 3 处替换

| 行 | 旧 | 新 |
|---|---|---|
| 10 | `from ._helpers import fixed_categories_in_clause, ...` | `from ._helpers import warehouse_categories_in_clause, ...` |
| 57 | `placeholders, params = fixed_categories_in_clause()` | `placeholders, params = warehouse_categories_in_clause()` |
| 107 | 同上 | 同上 |
| 144 | 同上 | 同上 |

### 1.3 死代码清理

| 文件 | 行 | 改动 |
|---|---|---|
| `blueprints/auth.py` | 21 | 删除 `FIXED_CATEGORIES`(只 import 没真用) |
| `blueprints/core.py` | 12 | 删除 `fixed_categories_in_clause`(只 import 没真用) |

### 1.4 `db/__init__.py::init_warehouse_db()` 加可选参数

```python
def init_warehouse_db(db_path: Path, seed_categories=None) -> None:
    """... 同前,但 seed_categories 缺省时用 config.FIXED_CATEGORIES。

    默认行为不变(老仓库继续种 9 品类),为将来支持新仓库自定义种子留口子。
    本次 spec 不改任何调用方,只把签名扩成可选。
    """
    if seed_categories is None:
        from config import FIXED_CATEGORIES
        seed_categories = FIXED_CATEGORIES
    # ... 其余不变
```

### 1.5 `db/import_items.py` 旧 CLI 报错文案

仅修改 line 86:

```python
# 旧
"Unknown categories in xlsx (not in FIXED_CATEGORIES, refusing to insert): "
# 新
"Unknown categories in xlsx (not in target warehouse's categories, refusing to insert): "
```

行为不变;旧 CLI 与本 spec 的 web 导入是**两种不同 xlsx 格式**的工具,功能并存。

---

## 2. 新增 web 导入功能

### 2.1 路由总览

| 路径 | 方法 | 权限 | 作用 |
|---|---|---|---|
| `/admin/import-items` | GET | admin | 上传表单 |
| `/admin/import-items` | POST | admin | 解析 → session 缓存预览 → 重定向预览页 |
| `/admin/import-items/preview` | GET | admin | 预览页 |
| `/admin/import-items/commit` | POST | admin | 写入(事务) |

### 2.2 端点 1:`GET /admin/import-items`

- 渲染 `templates/admin/import_items.html`
- 上下文:
  - `warehouses`:所有 master.db 已注册的仓库(SELECT code, name)
- 模板:单文件上传表单 + 仓库下拉 + 解析按钮

### 2.3 端点 2:`POST /admin/import-items`

#### 输入校验

- `request.files['file'].filename` 必须以 `.xlsx` 结尾 → 否则 flash 报错并 redirect 回表单
- `warehouse_code` 必须存在于 master.db → 否则 flash 报错并 redirect 回表单

#### 解析流程

```python
import openpyxl

wb = openpyxl.load_workbook(file_stream, data_only=True)
ws = wb["Sheet1"]  # 强制取 Sheet1
# 跳过前 2 行(标题 + 表头)
groups_order: list[str] = []      # 顺序保留的分组名
groups_rows: dict[str, list[dict]] = {}
problems: list[str] = []
prev_cat: str | None = None

for r in range(3, ws.max_row + 1):
    name = ws.cell(r, 2).value
    # 合计行:第 9 列("库存金额"列)存在且名称为空
    if not name:
        # 跳过合计/空行
        continue
    cat = ws.cell(r, 1).value or prev_cat
    if cat is None:
        problems.append(f"行 {r}: 缺少分类")
        continue
    prev_cat = cat
    if cat not in groups_rows:
        groups_order.append(cat)
        groups_rows[cat] = []
    unit_cost = ws.cell(r, 8).value
    unit = ws.cell(r, 7).value or "件"
    groups_rows[cat].append({
        "row": r,
        "name": str(name).strip(),
        "spec": ws.cell(r, 4).value,           # 仅展示用,不写入
        "unit_cost": float(unit_cost) if unit_cost is not None else 0.0,
        "unit": str(unit).strip() or "件",
    })

session["import_preview"] = {
    "warehouse_code": warehouse_code,
    "groups_order": groups_order,
    "groups_rows": groups_rows,
    "problems": problems,
    "filename": file.filename,
}
return redirect(url_for("import_items.preview"))
```

### 2.4 端点 3:`GET /admin/import-items/preview`

- 渲染 `templates/admin/import_items_preview.html`
- 上下文:`session['import_preview']` 全部字段 + 目标仓库名
- 模板:5 个 `<details>` 分组(默认展开第一个);底部三按钮(取消 / 返回上传 / 确认导入)

#### 错误处理

- session 中无 `import_preview` → flash "预览已过期,请重新上传" → redirect `/admin/import-items`

### 2.5 端点 4:`POST /admin/import-items/commit`

#### 前置校验

1. `session['import_preview']` 存在 → 否则 400
2. 目标仓库 code 在 master.db 存在 → 否则 400
3. **目标仓库 categories 表包含 xlsx 全部 5 个分组名** → 缺则
   `flash(f"目标仓库缺少分类:{', '.join(missing)}")` 并 redirect 回 `/items`

#### 写入流程(单一 SQLite 连接 + 事务)

```python
import sqlite3
from pathlib import Path
from config import BASE_DIR, WAREHOUSE_DB_DIR

preview = session.pop("import_preview")
wh = preview["warehouse_code"]

# 1. 取 db_path(从 master.db)
master = get_master_db()
row = master.execute(
    "SELECT db_path, name FROM warehouses WHERE code=?", (wh,)
).fetchone()
db_path = Path(BASE_DIR) / row["db_path"]

# 2. 连接 + 事务
with closing(sqlite3.connect(db_path)) as conn:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        # 3. DELETE 该仓库 5 品类下所有 items
        #    用 (SELECT id FROM categories WHERE name IN (5分组)) 子查询
        placeholders = ",".join("?" for _ in preview["groups_order"])
        conn.execute(
            f"DELETE FROM items WHERE category_id IN "
            f"(SELECT id FROM categories WHERE name IN ({placeholders}))",
            preview["groups_order"],
        )
        # 4. cat_by_name 缓存
        cat_by_name = {
            r["name"]: r["id"]
            for r in conn.execute("SELECT id, name FROM categories").fetchall()
        }
        # 5. INSERT 逐行
        ts = now()
        for cat_name in preview["groups_order"]:
            cid = cat_by_name[cat_name]
            for item in preview["groups_rows"][cat_name]:
                conn.execute(
                    """INSERT INTO items
                       (sku, name, category_id, quantity, safety_stock,
                        unit_cost, unit, gram_per_unit, updated_at)
                       VALUES (?, ?, ?, 0, 0, ?, ?, 0, ?)""",
                    (gen_sku(), item["name"], cid,
                     item["unit_cost"], item["unit"], ts),
                )
        conn.commit()
        audit("import_items.import", "warehouse", wh, {
            "count": sum(len(v) for v in preview["groups_rows"].values()),
            "filename": preview["filename"],
        })
        flash(f"导入成功:{sum(len(v) for v in preview['groups_rows'].values())} 条品项")
    except sqlite3.IntegrityError:
        conn.rollback()
        flash("导入失败,请重试")

return redirect(url_for("items.items_list"))
```

### 2.6 数据映射总表

| Excel 列 | items 字段 | 转换 |
|---|---|---|
| 物料名称(2) | `name` | `str.strip()`;空 → 跳过该行 |
| 分类(1,首行 + 向下合并) | `category_id` | 沿用上一行;首行为空 → 计入 problems 并跳过 |
| 隐藏栏/盘点单位单价(8) | `unit_cost` | `float()`;None → 0.0 |
| 单位(7) | `unit` | `str.strip()`;空 → "件" |
| 单价/元(5) | — | **忽略** |
| 现有库存(6) | — | **忽略**(quantity 一律 0) |
| SKU(3) | — | **忽略** |
| 规格(4) | — | **忽略**(仅预览展示) |
| 库存金额(9) | — | **忽略** |
| — | `sku` | `gen_sku()` |
| — | `quantity` | 0 |
| — | `safety_stock` | 0 |
| — | `gram_per_unit` | 0 |

---

## 3. UI 设计

### 3.1 `templates/items.html` 顶部入口(admin 可见)

在 `<h2>品类与品项</h2>` 同行右侧新增(在 chip bar 之前):

```html
<div class="page-header">
  <h2>品类与品项</h2>
  {% if g.user.is_admin %}
    <a href="{{ url_for('import_items.upload_form') }}" class="btn-sm">
      📥 批量导入
    </a>
  {% endif %}
</div>
```

`.page-header` 复用现有 `.flex-between` 样式(若存在),否则新增简单一行布局。

### 3.2 `templates/admin/import_items.html` 上传页

```html
{% extends "base.html" %}
{% block title %}品项批量导入{% endblock %}
{% block content %}
<h2>品项批量导入</h2>
<p class="muted">仅管理员可操作;目标仓库的分类必须已存在。</p>

<form method="post" enctype="multipart/form-data" class="grid cols-2">
  <label>
    <span>目标仓库</span>
    <select name="warehouse_code" required>
      {% for wh in warehouses %}
        <option value="{{ wh.code }}">{{ wh.name }} ({{ wh.code }})</option>
      {% endfor %}
    </select>
  </label>
  <label>
    <span>Excel 文件(.xlsx)</span>
    <input type="file" name="file" accept=".xlsx" required>
  </label>
  <div class="form-actions">
    <a href="{{ url_for('items.items_list') }}" class="btn-sm">取消</a>
    <button type="submit" class="btn-primary">解析预览</button>
  </div>
</form>
{% endblock %}
```

### 3.3 `templates/admin/import_items_preview.html` 预览页

```html
{% extends "base.html" %}
{% block title %}导入预览 — {{ target_wh_name }}{% endblock %}
{% block content %}
<h2>导入预览 — {{ target_wh_name }} ({{ warehouse_code }})</h2>
<p class="muted">
  共 {{ total_rows }} 行 · {{ groups_order|length }} 个分组
  {% if problems %} · <span class="warn">{{ problems|length }} 个数据问题</span>{% endif %}
</p>

{% for cat_name in groups_order %}
<details {% if loop.first %}open{% endif %}>
  <summary>{{ cat_name }} ({{ groups_rows[cat_name]|length }} 行)</summary>
  <table>
    <tr><th>物料名称</th><th>规格</th><th>单位</th><th>单价(¥)</th></tr>
    {% for item in groups_rows[cat_name] %}
    <tr>
      <td>{{ item.name }}</td>
      <td>{{ item.spec or '—' }}</td>
      <td>{{ item.unit }}</td>
      <td>{{ "%.3f"|format(item.unit_cost) }}</td>
    </tr>
    {% endfor %}
  </table>
</details>
{% endfor %}

{% if problems %}
<details>
  <summary>数据问题({{ problems|length }} 个,导入时跳过)</summary>
  <ul>{% for p in problems %}<li>{{ p }}</li>{% endfor %}</ul>
</details>
{% endif %}

<form method="post" action="{{ url_for('import_items.commit') }}"
      onsubmit="return confirm('确认导入 {{ total_rows }} 条品项到 {{ target_wh_name }}?\n该仓库以上分类下的现有品项将被全部删除。');">
  <div class="form-actions">
    <a href="{{ url_for('items.items_list') }}" class="btn-sm">取消</a>
    <a href="{{ url_for('import_items.upload_form') }}" class="btn-sm">返回修改</a>
    <button type="submit" class="btn-danger">⚠️ 确认导入</button>
  </div>
</form>
{% endblock %}
```

### 3.4 CSS

- 复用 `.btn-sm / .btn-primary / .btn-danger / .form-actions / .grid / .cols-2`
  (均已存在于 `static/style.css`)
- `<details>` 用浏览器原生样式,不引入额外交互

---

## 4. 错误处理

| 场景 | 行为 |
|---|---|
| 非 .xlsx 文件 | `flash("仅支持 .xlsx 文件")` → redirect 回 `/admin/import-items` |
| 文件无 Sheet1 | `flash("Excel 中无 Sheet1")` → redirect |
| 第 3 行起无名称 | `flash("Excel 中无数据行")` → redirect |
| 目标分类不在仓库 categories | **commit 时拒绝** + flash 列出缺失的品类名 → redirect `/items` |
| 单位为空 | 默认 "件"(与现有 `/items` POST 一致) |
| 单价为 None | 0.0 |
| 名称重复 | 允许(系统 name 无 UNIQUE 约束);全部 INSERT,id 自增顺序 |
| 预览过期(session 丢) | flash "预览已过期" → redirect `/admin/import-items` |
| commit 时 session 丢 | flash "预览已过期" → redirect |
| SQL IntegrityError | `conn.rollback()` + flash "导入失败,请重试" |

**幂等保证**:二次导入相同 xlsx → DELETE 5 品类下 items + 重新 INSERT,行数与首次
相同(全量覆盖)。

---

## 5. 测试策略

### 5.1 单元 / 路由测试(`tests/`)

| 测试文件 | 覆盖 |
|---|---|
| `test_warehouse_categories_in_clause.py` | 各仓库 chip 过滤正确:wh_001=4,wh_002=9,wh_003=9,wh_004=5(fixture 手工种) |
| `test_import_items_upload.py` | 上传解析:5 分组 81 行正确分桶;None 名称跳过;无分类计入 problems |
| `test_import_items_commit.py` | commit 后 items 表行数 = 81;audit log 含条目;session 清空;二次提交无 preview 时拒绝 |
| `test_import_items_missing_category.py` | 目标仓库缺分类 → 400 + flash 列出 |
| `test_import_items_idempotent.py` | 二次 commit → items 仍为 81 行(覆盖无残留) |
| `test_items_list_no_hardcoded.py`(回归) | 临时把 `FIXED_CATEGORIES` 改成空 tuple → `/items` chip 仍读仓库自身(验证去硬编码生效) |
| `test_inventory_chip_warehouse_scoped.py`(回归) | wh_002 chip 9 个;wh_004 chip 5 个 |

### 5.2 手工验证(提交前必做)

1. 上传 `docs/superpowers/泰柯盘点表.xlsx`
2. 预览页 5 个分组正确分桶(常温 20 / 水果 4 / 包装 32 / 冷冻 14 / 周边 6)
3. 合计 81 行(无空行残留)
4. 取消按钮回到 `/items`
5. 确认导入 → `/items` 显示 81 条,5 个 chip 正确切换
6. 二次导入同一文件 → 仍 81 条(全量覆盖)
7. admin 以外角色访问 `/admin/import-items` → 403
8. 上传非 .xlsx 文件 → flash 报错
9. 上传针对缺品类的仓库 → commit 拒绝 + flash 列出缺哪些

---

## 6. 依赖与已知约束

### 6.1 新增依赖

- 已有 `openpyxl`(`requirements.txt`);无需新增

### 6.2 不在本次范围

- 仓库创建:admin 通过 `cli.py create-warehouse` 或用户管理页(`POST /admin/users`)
  自行创建
- 库存数导入:仅做品项导入;`quantity=0` 由后续盘点 / 入库页面补
- `stock_movements` 不写:新仓库无历史,无影响
- 规格解析启发式:暂不实现(将来可推 `gram_per_unit`)
- 多 Sheet xlsx:仅支持 Sheet1

### 6.3 风险点

1. **`/items` 入口按钮可见性**:若 admin 同时操作多仓库,默认 `g.warehouse`
   决定显示哪个仓库的 items;导入按钮始终跳上传页,不带预选仓库(用户自选)
2. **并发**:SQLite 文件锁兜底;不在本次引入显式锁
3. **大型 xlsx**:81 行量级无问题;若将来 xlsx 变大(>5000 行),session 缓存
   preview 可能膨胀;届时改 DB 临时表。本次不优化
4. **wh_001 老 4 品类**:`init_warehouse_db` 不会回填 wh_001 缺失的 9 品类;
   但本 spec 让 chip 过滤读仓库自身 categories,wh_001 仍显示其 4 品类,无影响

---

## 7. 后续优化(不在本次)

- v2:`init_warehouse_db` 创建新仓库时支持传自定义 `seed_categories`,Web UI 提供
  "新建仓库 + 种自定义分类"流程
- v2:`gram_per_unit` 从 xlsx "规格"列启发式推断(如 "1kg*12盒/箱" → 1000 g/件)
- v2:多 Sheet 支持(不同 sheet 映射不同仓库)
- v2:导入预览分页(>500 行时)
- v2:导入进度条 / 异步任务

---

## 8. 文件改动清单

| 类型 | 路径 | 改动 |
|---|---|---|
| 改 | `blueprints/_helpers.py` | `fixed_categories_in_clause` → `warehouse_categories_in_clause`(别名保留) |
| 改 | `blueprints/items.py` | 3 处 import + 函数调用替换 |
| 改 | `blueprints/auth.py` | 删除 `FIXED_CATEGORIES` 死 import |
| 改 | `blueprints/core.py` | 删除 `fixed_categories_in_clause` 死 import |
| 改 | `db/__init__.py` | `init_warehouse_db(db_path)` → `init_warehouse_db(db_path, seed_categories=None)` |
| 改 | `db/import_items.py` | line 86 报错文案改为 "target warehouse's categories" |
| 改 | `templates/items.html` | 顶部 admin 入口按钮 |
| 增 | `blueprints/import_items.py` | 新蓝图:4 路由 + session 缓存 + 解析 + 写入 |
| 增 | `templates/admin/import_items.html` | 上传表单 |
| 增 | `templates/admin/import_items_preview.html` | 预览页 |
| 增 | `app.py`(或 `__init__.py`) | 注册新蓝图 |
| 增 | `tests/test_*.py` | 见 §5.1 |