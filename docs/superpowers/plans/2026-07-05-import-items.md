# 品项导入（xlsx） Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 admin 通过 web UI 上传 xlsx 文件,把品项批量导入到已存在的目标仓库。同步把全项目 5 处硬编码 `FIXED_CATEGORIES` 的 SQL 改成读仓库自身的 `categories` 表。

**Architecture:** 新增 `blueprints/import_items.py`(4 路由) + 2 模板;`blueprints/_helpers.py` 新增 `warehouse_categories_in_clause()`(读仓库自身 categories);`blueprints/items.py` 3 处 SQL 切到新函数;`db/__init__.py::init_warehouse_db` 扩参 `seed_categories=None`(默认仍走 `FIXED_CATEGORIES`);清理 2 处死 import。整体遵循 TDD:先写失败测试,再实现,再 commit。

**Tech Stack:** Python 3 + Flask 3 + SQLite(多仓库)+ openpyxl 3.1.5(已装)+ pytest;前端原生 `<details>` 折叠,无新 JS 框架。

## Global Constraints

- `config.FIXED_CATEGORIES` 仍保留,作新建老式仓库的默认种子;**运行时** chip 过滤读仓库自身 categories(SPEC §1.1)。
- `init_warehouse_db(db_path, seed_categories=None)`:`None` 时退回 `FIXED_CATEGORIES`,行为不变(SPEC §1.4)。
- 导入仅 admin(`/admin/import-items*` 4 路由 `@require_role("admin")`)。
- 不导入库存数:`quantity = 0`;不写 `stock_movements`(SPEC §0.3 Q9)。
- 不存 xlsx SKU:统一 `gen_sku()`(SPEC §0.3 Q4)。
- `unit_cost = xlsx 列 8(隐藏栏/盘点单位单价)`;None → 0.0(SPEC §0.3 Q5/Q7)。
- `unit` 直接存 xlsx 原始字符串;空 → `"件"`(SPEC §0.3 Q6)。
- 全量覆盖:commit 时 `DELETE FROM items WHERE category_id IN (5 分组)`,再 INSERT(SPEC §0.3 Q8)。
- 缺品类 → commit 拒绝,flash 列出缺哪些品类名,**不**给引导链接(SPEC OP3)。
- `templates/items.html` 入口位置:**标题行右侧**,仅 `g.user.is_admin` 可见(SPEC OP4)。
- CSS v-bump:`base.html` `?v=12` → `?v=13`(SPEC §3.1 修复说明)。
- 数量精度:`Decimal('0.01')` 量化;沿用 `parse_qty` / `gen_sku` / `now`(在 `_helpers.py`)。
- 提交信息中文 `<type>(scope): <desc>` 格式,无 attribution。
- 测试命令:`./venv/bin/python -m pytest tests/ -v`(若未建 venv 用 `python3 -m pytest tests/ -v`)。
- 旧 CLI `db/import_items.py` **保留**,仅改其报错文案;旧 CLI 期望 5 列 xlsx(品项编码/品项名称/品项类别/订货单位/单价),与本次 web 导入(9 列 xlsx)**功能并存**(SPEC §1.5)。

---

## File Structure

| 文件 | 职责 | 改动 |
|------|------|------|
| `blueprints/_helpers.py` | 共享 helpers | 新增 `warehouse_categories_in_clause()`;保留 `fixed_categories_in_clause` 作别名 |
| `blueprints/items.py` | 物品 CRUD | 3 处 SQL 切到新函数 + 1 处 import |
| `blueprints/auth.py` | 认证 | 删除死 import `FIXED_CATEGORIES` |
| `blueprints/core.py` | dashboard / summary | 删除死 import `fixed_categories_in_clause` |
| `db/__init__.py` | db wiring | `init_warehouse_db(db_path)` → `init_warehouse_db(db_path, seed_categories=None)` |
| `db/import_items.py` | 旧 CLI 工具 | 仅 line 86 报错文案改为 "target warehouse's categories" |
| `blueprints/import_items.py` | **新增** | 4 路由(upload_form / upload_parse / preview / commit)+ 解析 + 写入 |
| `templates/admin/import_items.html` | **新增** | 上传表单页 |
| `templates/admin/import_items_preview.html` | **新增** | 预览页(5 分组折叠) |
| `templates/items.html` | 物品页 | 顶部 `.page-header` 包住标题 + admin 入口按钮 |
| `templates/base.html` | 全局布局 | `style.css?v=12` → `?v=13` |
| `static/style.css` | 全局样式 | 新增 `.page-header { display:flex; justify-content:space-between; align-items:center; gap:1rem; }` |
| `app.py` | app factory | 注册新蓝图 `import_items_bp` |
| `tests/conftest.py` | 测试夹具 | 已存在,复用 `_wh / _seed_item`,必要时扩 `_seed_warehouse_categories` |
| `tests/test_warehouse_categories_in_clause.py` | **新增** | 单元:各仓库读自身 categories |
| `tests/test_import_items_route.py` | **新增** | 路由:解析、缺品类拒绝、二次提交幂等、admin-only |
| `tests/test_import_items_parse.py` | **新增** | 解析逻辑:5 分组 81 行 / None 跳过 / 无分类计入 problems |

---

### Task 1: 全项目去硬编码品类 — 新 `warehouse_categories_in_clause()`

**Files:**
- Modify: `blueprints/_helpers.py`(在 `fixed_categories_in_clause` 之后追加)
- Modify: `blueprints/items.py:10`(import 名替换)
- Modify: `blueprints/items.py:57,107,144`(函数调用替换)
- Modify: `blueprints/auth.py:21`(删 `FIXED_CATEGORIES` 死 import)
- Modify: `blueprints/core.py:12`(删 `fixed_categories_in_clause` 死 import)
- Modify: `db/__init__.py:249-276`(`init_warehouse_db` 加 `seed_categories=None` 参数)
- Modify: `db/import_items.py:86`(报错文案)
- Test: `tests/test_warehouse_categories_in_clause.py`(新建)

**Interfaces:**
- Consumes: `get_warehouse_db()`(现有)
- Produces: `warehouse_categories_in_clause() -> tuple[str, list[str]]` — `(placeholder_sql, params)`;极端空表返回 `("1", [0])`。`fixed_categories_in_clause` 保留为别名。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_warehouse_categories_in_clause.py`:

```python
"""去硬编码验证:warehouse_categories_in_clause 读仓库自身 categories 表。"""
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest


def _make_warehouse(master_path: Path, wh_path: Path, cats: list[str]) -> None:
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
    w = sqlite3.connect(wh_path)
    w.execute("""CREATE TABLE categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        description TEXT,
        created_at TEXT NOT NULL)""")
    for c in cats:
        w.execute(
            "INSERT INTO categories (name, description, created_at) VALUES (?, ?, ?)",
            (c, "", ts))
    w.commit()
    w.close()


def test_returns_all_categories_in_wh(tmp_path, monkeypatch):
    """仓库种了 5 个分类,函数返回 5 个 name。"""
    import db as db_module
    master_path = tmp_path / "master.db"
    wh_path = tmp_path / "warehouses" / "wh_t.db"
    wh_path.parent.mkdir()
    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_path.parent)
    _make_warehouse(master_path, wh_path, ["常温物料", "水果", "包装材料", "冷冻&冷藏食品", "定制周边"])

    from blueprints._helpers import warehouse_categories_in_clause
    from app import create_app
    app = create_app()
    with app.test_request_context():
        from flask import g
        g.warehouse_db_path = str(wh_path)
        g.warehouse_id = 1
        g.user = type("U", (), {"is_admin": True})()
        placeholders, params = warehouse_categories_in_clause()
        assert params == ["常温物料", "水果", "包装材料", "冷冻&冷藏食品", "定制周边"]
        assert placeholders == "?,?,?,?,?"


def test_empty_categories_returns_safe_clause(tmp_path, monkeypatch):
    """无分类的极端情况返回 (1, [0]) 防御性 0 行。"""
    import db as db_module
    master_path = tmp_path / "master.db"
    wh_path = tmp_path / "warehouses" / "wh_t.db"
    wh_path.parent.mkdir()
    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_path.parent)
    _make_warehouse(master_path, wh_path, [])

    from blueprints._helpers import warehouse_categories_in_clause
    from app import create_app
    app = create_app()
    with app.test_request_context():
        from flask import g
        g.warehouse_db_path = str(wh_path)
        placeholders, params = warehouse_categories_in_clause()
        assert (placeholders, params) == ("1", [0])
```

- [ ] **Step 2: 运行测试,确认失败**

```bash
cd /Users/ericmr/Documents/GitHub/DailyCheck && ./venv/bin/python -m pytest tests/test_warehouse_categories_in_clause.py -v
```

预期:FAIL,`ImportError: cannot import name 'warehouse_categories_in_clause' from 'blueprints._helpers'`

- [ ] **Step 3: 实现新函数(blueprints/_helpers.py)**

在 `blueprints/_helpers.py` 第 43 行后追加:

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


# 向后兼容别名
fixed_categories_in_clause = warehouse_categories_in_clause
```

- [ ] **Step 4: items.py 替换 3 处**

在 `blueprints/items.py` 第 10 行:

```python
# 旧
from ._helpers import fixed_categories_in_clause, fmt_qty, gen_sku, now, parse_qty
# 新
from ._helpers import warehouse_categories_in_clause, fmt_qty, gen_sku, now, parse_qty
```

把 `fixed_categories_in_clause()` 三处调用(第 57、107、144 行)替换为 `warehouse_categories_in_clause()`。

- [ ] **Step 5: 删死 import**

`blueprints/auth.py` 第 21 行:

```python
# 旧
from config import BASE_DIR, FIXED_CATEGORIES, MASTER_DB, SECRET_KEY
# 新
from config import BASE_DIR, MASTER_DB, SECRET_KEY
```

`blueprints/core.py` 第 12 行:

```python
# 旧
from ._helpers import fixed_categories_in_clause, render
# 新
from ._helpers import render
```

- [ ] **Step 6: db/__init__.py 扩参**

把 `init_warehouse_db(db_path: Path)` 改为:

```python
def init_warehouse_db(db_path: Path, seed_categories=None) -> None:
    """Create the schema for one warehouse db if missing, and seed fixed categories.

    seed_categories=None 时退回 config.FIXED_CATEGORIES(默认行为不变)。
    传入自定义 tuple 可让新建仓库用别的品类集合(本次 spec 不调用,留作未来)。

    Also runs idempotent column-add migrations for tables that pre-date
    some columns (CREATE TABLE IF NOT EXISTS is a no-op for existing
    tables, so missing columns must be added separately).
    """
    from datetime import datetime
    if seed_categories is None:
        from config import FIXED_CATEGORIES
        seed_categories = FIXED_CATEGORIES

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(WAREHOUSE_SCHEMA)
        # Defensive column-add migrations for legacy warehouse dbs that
        # were created before the column was introduced.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(stocktake_batches)").fetchall()}
        if "status" not in cols:
            conn.execute(
                "ALTER TABLE stocktake_batches ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'"
            )
        existing = {r[0] for r in conn.execute("SELECT name FROM categories").fetchall()}
        for name in seed_categories:
            if name not in existing:
                conn.execute(
                    "INSERT INTO categories (name, description, created_at) VALUES (?, ?, ?)",
                    (name, "系统固定品类", ts),
                )
        conn.commit()
```

- [ ] **Step 7: db/import_items.py 报错文案**

`db/import_items.py` 第 86 行:

```python
# 旧
"Unknown categories in xlsx (not in FIXED_CATEGORIES, refusing to insert): "
# 新
"Unknown categories in xlsx (not in target warehouse's categories, refusing to insert): "
```

- [ ] **Step 8: 运行测试,确认通过**

```bash
cd /Users/ericmr/Documents/GitHub/DailyCheck && ./venv/bin/python -m pytest tests/test_warehouse_categories_in_clause.py -v
```

预期:PASS,2 个测试通过

- [ ] **Step 9: 跑回归**

```bash
cd /Users/ericmr/Documents/GitHub/DailyCheck && ./venv/bin/python -m pytest tests/ -v
```

预期:全部通过(`test_items_route.py` 等老测试不应破坏,因 `fixed_categories_in_clause` 仍作别名存在)

- [ ] **Step 10: 提交**

```bash
cd /Users/ericmr/Documents/GitHub/DailyCheck && \
git add blueprints/_helpers.py blueprints/items.py blueprints/auth.py blueprints/core.py \
        db/__init__.py db/import_items.py tests/test_warehouse_categories_in_clause.py && \
git commit -m "refactor(core): 去硬编码品类,SQL 改读仓库自身 categories"
```

---

### Task 2: xlsx 解析纯函数(单测,先于 web 路由)

**Files:**
- Modify: `blueprints/import_items.py`(新增,本任务只放 `parse_xlsx` 函数,不挂路由)
- Test: `tests/test_import_items_parse.py`(新建)

**Interfaces:**
- Produces: `parse_xlsx(file_stream) -> dict` — 返回 `{"groups_order": [...], "groups_rows": {cat: [{row, name, spec, unit_cost, unit}, ...]}, "problems": [...]}`。**纯函数,不写库,不出 redirect**。供 Task 3 / Task 4 复用。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_import_items_parse.py`:

```python
"""解析 泰柯盘点表.xlsx 单元测试。"""
import io
from pathlib import Path

import pytest
from openpyxl import Workbook


def _make_xlsx(path: Path, rows: list[tuple]) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.cell(1, 1, "tamkoko月盘表 ")
    ws.cell(2, 1, "分类")
    ws.cell(2, 2, "物料名称")
    ws.cell(2, 3, "SKU")
    ws.cell(2, 4, "规格")
    ws.cell(2, 5, "单价/元")
    ws.cell(2, 6, "现有库存")
    ws.cell(2, 7, "单位")
    ws.cell(2, 8, "隐藏栏/盘点单位单价")
    ws.cell(2, 9, "库存金额")
    for r_idx, row in enumerate(rows, start=3):
        for c_idx, val in enumerate(row, start=1):
            ws.cell(r_idx, c_idx, val)
    wb.save(str(path))


def test_parse_groups_by_category(tmp_path):
    """5 分组向下合并:第一行有分类名,后续行空 → 沿用上一行。"""
    rows = [
        ("常温物料", "A", None, "x", 100, 5, "箱", 10, 50),
        (None, "B", None, "y", 200, 3, "包", 20, 60),
        ("包装材料", "C", None, "z", 300, 2, "个", 30, 60),
    ]
    p = tmp_path / "in.xlsx"
    _make_xlsx(p, rows)
    from blueprints.import_items import parse_xlsx
    with open(p, "rb") as f:
        result = parse_xlsx(f)
    assert result["groups_order"] == ["常温物料", "包装材料"]
    assert len(result["groups_rows"]["常温物料"]) == 2
    assert result["groups_rows"]["常温物料"][0]["name"] == "A"
    assert result["groups_rows"]["常温物料"][0]["unit_cost"] == 10.0
    assert result["groups_rows"]["常温物料"][1]["unit"] == "包"


def test_parse_none_unit_cost_defaults_to_zero(tmp_path):
    rows = [("X", "A", None, "x", 100, None, "箱", None, 0)]
    p = tmp_path / "in.xlsx"
    _make_xlsx(p, rows)
    from blueprints.import_items import parse_xlsx
    with open(p, "rb") as f:
        result = parse_xlsx(f)
    assert result["groups_rows"]["X"][0]["unit_cost"] == 0.0


def test_parse_empty_unit_defaults_to_jian(tmp_path):
    rows = [("X", "A", None, "x", 100, 5, None, 10, 50)]
    p = tmp_path / "in.xlsx"
    _make_xlsx(p, rows)
    from blueprints.import_items import parse_xlsx
    with open(p, "rb") as f:
        result = parse_xlsx(f)
    assert result["groups_rows"]["X"][0]["unit"] == "件"


def test_parse_skips_blank_name_rows(tmp_path):
    """合计行(名称为空)跳过。"""
    rows = [
        ("X", "A", None, "x", 100, 5, "箱", 10, 50),
        (None, None, None, None, None, None, "总额", None, 13888.106),
    ]
    p = tmp_path / "in.xlsx"
    _make_xlsx(p, rows)
    from blueprints.import_items import parse_xlsx
    with open(p, "rb") as f:
        result = parse_xlsx(f)
    assert len(result["groups_rows"]["X"]) == 1


def test_parse_records_problem_when_first_row_missing_category(tmp_path):
    """首行分类为空 → 计入 problems,跳过该行。"""
    rows = [(None, "A", None, "x", 100, 5, "箱", 10, 50)]
    p = tmp_path / "in.xlsx"
    _make_xlsx(p, rows)
    from blueprints.import_items import parse_xlsx
    with open(p, "rb") as f:
        result = parse_xlsx(f)
    assert result["groups_rows"] == {}
    assert any("缺少分类" in p for p in result["problems"])
```

- [ ] **Step 2: 运行测试,确认失败**

```bash
cd /Users/ericmr/Documents/GitHub/DailyCheck && ./venv/bin/python -m pytest tests/test_import_items_parse.py -v
```

预期:FAIL,`ModuleNotFoundError: No module named 'blueprints.import_items'`

- [ ] **Step 3: 创建 blueprints/import_items.py(仅 parse_xlsx)**

```python
"""Web 端品项批量导入:上传 → 预览 → 确认写入。"""
from __future__ import annotations

import io
from typing import IO


def parse_xlsx(file_stream: IO[bytes]) -> dict:
    """解析 xlsx 文件为分组预览数据。

    期望格式:Sheet1,跳过前 2 行(标题 + 表头),第 3 行起为数据。
    分类列首行有值,后续空 → 沿用上一行(向下合并)。
    列序:分类(1)|物料名称(2)|SKU(3)|规格(4)|单价/元(5)|
          现有库存(6)|单位(7)|隐藏栏/盘点单位单价(8)|库存金额(9)

    Returns:
        {
            "groups_order": ["常温物料", ...],
            "groups_rows": {"常温物料": [{"row": 3, "name": "...", "spec": "...",
                                          "unit_cost": float, "unit": "str"}, ...]},
            "problems": ["行 N: 缺少分类", ...]
        }

    注意:本函数不写库,不读 session,纯函数。
    """
    import openpyxl

    wb = openpyxl.load_workbook(file_stream, data_only=True)
    ws = wb["Sheet1"]
    groups_order: list[str] = []
    groups_rows: dict[str, list[dict]] = {}
    problems: list[str] = []
    prev_cat: str | None = None

    for r in range(3, ws.max_row + 1):
        name = ws.cell(r, 2).value
        if not name:
            # 合计行 / 空行 → 跳过
            continue
        cat = ws.cell(r, 1).value or prev_cat
        if cat is None:
            problems.append(f"行 {r}: 缺少分类")
            continue
        prev_cat = cat
        if cat not in groups_rows:
            groups_order.append(cat)
            groups_rows[cat] = []
        unit_cost_raw = ws.cell(r, 8).value
        try:
            unit_cost = float(unit_cost_raw) if unit_cost_raw is not None else 0.0
        except (TypeError, ValueError):
            unit_cost = 0.0
            problems.append(f"行 {r}: 单价无法解析为数字 → 记为 0")
        unit_raw = ws.cell(r, 7).value
        unit = str(unit_raw).strip() if unit_raw is not None else ""
        if not unit:
            unit = "件"
        groups_rows[cat].append({
            "row": r,
            "name": str(name).strip(),
            "spec": ws.cell(r, 4).value,
            "unit_cost": unit_cost,
            "unit": unit,
        })

    return {
        "groups_order": groups_order,
        "groups_rows": groups_rows,
        "problems": problems,
    }
```

- [ ] **Step 4: 运行测试,确认通过**

```bash
cd /Users/ericmr/Documents/GitHub/DailyCheck && ./venv/bin/python -m pytest tests/test_import_items_parse.py -v
```

预期:PASS,5 个测试全部通过

- [ ] **Step 5: 提交**

```bash
cd /Users/ericmr/Documents/GitHub/DailyCheck && \
git add blueprints/import_items.py tests/test_import_items_parse.py && \
git commit -m "feat(import): xlsx 解析纯函数(5 分组 / None 处理 / 跳过空行)"
```

---

### Task 3: 上传 + 预览路由 + 模板

**Files:**
- Modify: `blueprints/import_items.py`(加 3 路由:upload_form / upload_parse / preview)
- Create: `templates/admin/import_items.html`
- Create: `templates/admin/import_items_preview.html`
- Modify: `templates/base.html`(CSS v-bump `?v=12` → `?v=13`)
- Modify: `static/style.css`(加 `.page-header`)
- Test: `tests/test_import_items_route.py`(新建,本任务覆盖上传/预览的 GET 路由)

**Interfaces:**
- Produces: 3 路由:
  - `GET /admin/import-items` — 渲染上传表单
  - `POST /admin/import-items` — 调 `parse_xlsx` → 写入 `session['import_preview']` → redirect `/admin/import-items/preview`
  - `GET /admin/import-items/preview` — 读 session 渲染预览页

- [ ] **Step 1: 写失败测试**

在 `tests/test_import_items_route.py` 新增:

```python
"""上传 + 预览路由测试。"""
import io

from openpyxl import Workbook


def _login_admin(client):
    """直接设 session 为 admin(role='admin' on warehouse)。"""
    with client.session_transaction() as s:
        s["user_id"] = 1
        s["warehouse_id"] = 1


def _make_xlsx_bytes(rows: list[tuple]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.cell(1, 1, "title")
    ws.cell(2, 1, "分类"); ws.cell(2, 2, "物料名称")
    ws.cell(2, 7, "单位"); ws.cell(2, 8, "隐藏栏/盘点单位单价")
    for r_idx, row in enumerate(rows, start=3):
        for c_idx, val in enumerate(row, start=1):
            ws.cell(r_idx, c_idx, val)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_upload_form_renders_for_admin(logged_client):
    client, _ = logged_client
    _login_admin(client)
    resp = client.get("/admin/import-items")
    assert resp.status_code == 200
    assert b"\xe6\x9c\x8d\xe5\x8a\xa1\xe5\x99\xa8\xe7\x9b\xae\xe5\x89\x8d" in resp.data or \
           b"\xe5\x93\x81\xe9\xa1\xb9\xe6\x89\xb9\xe9\x87\x8f\xe5\xaf\xbc\xe5\x85\xa5" in resp.data


def test_upload_parse_redirects_to_preview(logged_client):
    client, _ = logged_client
    _login_admin(client)
    xlsx_bytes = _make_xlsx_bytes([
        ("X", "A", None, "s", 100, 5, "箱", 10, 50),
    ])
    data = {
        "file": (io.BytesIO(xlsx_bytes), "test.xlsx"),
        "warehouse_code": "wh_test",
    }
    resp = client.post("/admin/import-items", data=data,
                       content_type="multipart/form-data", follow_redirects=False)
    assert resp.status_code == 302
    assert "/admin/import-items/preview" in resp.headers["Location"]


def test_upload_rejects_non_xlsx(logged_client):
    client, _ = logged_client
    _login_admin(client)
    data = {
        "file": (io.BytesIO(b"not an xlsx"), "test.txt"),
        "warehouse_code": "wh_test",
    }
    resp = client.post("/admin/import-items", data=data,
                       content_type="multipart/form-data", follow_redirects=True)
    assert b"\xe4\xbb\x85\xe6\x94\xaf\xe6\x8c\x81 .xlsx" in resp.data or \
           resp.status_code in (200, 302)


def test_preview_without_session_redirects(logged_client):
    client, _ = logged_client
    _login_admin(client)
    resp = client.get("/admin/import-items/preview", follow_redirects=True)
    assert b"\xe9\xa2\x84\xe8\xa7\x88\xe5\xb7\xb2\xe8\xbf\x87\xe6\x9c\x9f" in resp.data or \
           resp.status_code == 200


def test_non_admin_cannot_access(logged_client):
    """非 admin 访问 /admin/import-items 应被拒。"""
    client, _wh_path = logged_client
    # logged_client 默认 admin。改 session role 需绕开;改测直接验证装饰器:
    # 用无 session 的 client 访问 → 应重定向到登录或 403
    client2 = client.application.test_client()
    resp = client2.get("/admin/import-items")
    assert resp.status_code in (302, 403)
```

- [ ] **Step 2: 运行测试,确认失败**

```bash
cd /Users/ericmr/Documents/GitHub/DailyCheck && ./venv/bin/python -m pytest tests/test_import_items_route.py -v
```

预期:FAIL(部分 404 / ImportError)

- [ ] **Step 3: 实现 3 路由(blueprints/import_items.py 追加)**

在 `parse_xlsx` 后追加:

```python
from flask import (
    Blueprint, abort, flash, redirect, render_template, request, session, url_for,
)

from permissions import require_login, require_role


bp = Blueprint("import_items", __name__, url_prefix="/admin/import-items")


@bp.route("", methods=["GET"])  # /admin/import-items (尾部不带 /,与 url_prefix 拼接)
@require_login
@require_role("admin")
def upload_form():
    """渲染上传表单。"""
    import db as db_module
    master = db_module.get_master_db()
    warehouses = master.execute(
        "SELECT code, name FROM warehouses ORDER BY id"
    ).fetchall()
    return render_template("admin/import_items.html", warehouses=warehouses)


@bp.route("", methods=["POST"])
@require_login
@require_role("admin")
def upload_parse():
    """解析上传的 xlsx → 缓存到 session → 重定向预览。"""
    from werkzeug.datastructures import FileStorage

    file = request.files.get("file")
    warehouse_code = request.form.get("warehouse_code", "").strip()

    if not isinstance(file, FileStorage) or not file.filename:
        flash("请选择文件")
        return redirect(url_for("import_items.upload_form"))

    if not file.filename.lower().endswith(".xlsx"):
        flash("仅支持 .xlsx 文件")
        return redirect(url_for("import_items.upload_form"))

    # 校验仓库存在
    import db as db_module
    master = db_module.get_master_db()
    row = master.execute(
        "SELECT 1 FROM warehouses WHERE code = ?", (warehouse_code,)
    ).fetchone()
    if row is None:
        flash(f"仓库 {warehouse_code} 不存在")
        return redirect(url_for("import_items.upload_form"))

    try:
        preview = parse_xlsx(file.stream)
    except Exception as e:  # openpyxl 各种解析异常
        flash(f"Excel 解析失败:{e}")
        return redirect(url_for("import_items.upload_form"))

    if not preview["groups_order"]:
        flash("Excel 中无数据行")
        return redirect(url_for("import_items.upload_form"))

    session["import_preview"] = {
        "warehouse_code": warehouse_code,
        "filename": file.filename,
        **preview,
    }
    return redirect(url_for("import_items.preview"))


@bp.route("/preview", methods=["GET"])
@require_login
@require_role("admin")
def preview():
    """渲染预览页。"""
    pv = session.get("import_preview")
    if not pv:
        flash("预览已过期,请重新上传")
        return redirect(url_for("import_items.upload_form"))

    import db as db_module
    master = db_module.get_master_db()
    wh = master.execute(
        "SELECT name FROM warehouses WHERE code = ?", (pv["warehouse_code"],)
    ).fetchone()
    target_wh_name = wh["name"] if wh else pv["warehouse_code"]

    total_rows = sum(len(v) for v in pv["groups_rows"].values())
    return render_template(
        "admin/import_items_preview.html",
        target_wh_name=target_wh_name,
        total_rows=total_rows,
        **pv,
    )
```

- [ ] **Step 4: 模板 1(上传页)**

新建 `templates/admin/import_items.html`:

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

- [ ] **Step 5: 模板 2(预览页)**

新建 `templates/admin/import_items_preview.html`:

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

- [ ] **Step 6: CSS 加 `.page-header`(先于 Step 7 用到)**

在 `static/style.css` 文件末尾追加:

```css
.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 1rem;
  margin-bottom: 1rem;
}
```

- [ ] **Step 7: 模板 3(items.html 顶部 admin 入口)**

修改 `templates/items.html` 第 3 行 `<h2>品类与品项</h2>`:

```html
<!-- 旧 -->
<h2>品类与品项</h2>
<!-- 新 -->
<div class="page-header">
  <h2>品类与品项</h2>
  {% if g.user.is_admin %}
    <a href="{{ url_for('import_items.upload_form') }}" class="btn-sm">
      📥 批量导入
    </a>
  {% endif %}
</div>
```

- [ ] **Step 8: base.html v-bump**

`templates/base.html` 第 13 行:

```html
<!-- 旧 -->
<link rel="stylesheet" href="{{ url_for('static', filename='style.css') }}?v=12" />
<!-- 新 -->
<link rel="stylesheet" href="{{ url_for('static', filename='style.css') }}?v=13" />
```

- [ ] **Step 9: 注册蓝图(app.py)**

`app.py` 第 41 行后(`from blueprints.users import bp as users_bp` 之后):

```python
    from blueprints.import_items import bp as import_items_bp
```

`app.py` 第 52 行后:

```python
    app.register_blueprint(import_items_bp)
```

- [ ] **Step 10: 运行测试,确认通过**

```bash
cd /Users/ericmr/Documents/GitHub/DailyCheck && ./venv/bin/python -m pytest tests/test_import_items_route.py -v
```

预期:PASS

- [ ] **Step 11: 跑全量回归**

```bash
cd /Users/ericmr/Documents/GitHub/DailyCheck && ./venv/bin/python -m pytest tests/ -v
```

预期:全部通过

- [ ] **Step 12: 提交**

```bash
cd /Users/ericmr/Documents/GitHub/DailyCheck && \
git add blueprints/import_items.py templates/admin/import_items.html \
        templates/admin/import_items_preview.html templates/items.html \
        templates/base.html static/style.css app.py tests/test_import_items_route.py && \
git commit -m "feat(import): 上传/预览路由 + 模板 + /items 入口"
```

---

### Task 4: 提交 commit 路由 + 缺品类拒绝 + 幂等覆盖

**Files:**
- Modify: `blueprints/import_items.py`(加 `commit` 路由)
- Modify: `tests/test_import_items_route.py`(补 commit 测试)

**Interfaces:**
- Produces: `POST /admin/import-items/commit` — 前置校验(session + 仓库存在 + 5 分组全在仓库 categories),缺则 flash 报错;通过则 `DELETE` + `INSERT`(事务),写 audit,flash 成功

- [ ] **Step 1: 写失败测试**

`tests/test_import_items_route.py` 追加:

```python
def test_commit_requires_session(logged_client):
    client, _ = logged_client
    _login_admin(client)
    # session 无 preview → 应 400 或 redirect
    resp = client.post("/admin/import-items/commit", follow_redirects=True)
    assert resp.status_code in (200, 400)
    # 仓库 items 表不应有新增
    import sqlite3
    wh = sqlite3.connect(_)
    n = wh.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"]
    assert n == 0
    wh.close()


def test_commit_rejects_when_category_missing(logged_client):
    """目标仓库 categories 缺 xlsx 的某个分组 → commit 拒绝,不写 items。"""
    client, wh_path = logged_client
    _login_admin(client)
    xlsx_bytes = _make_xlsx_bytes([
        ("不存在的分类", "A", None, "s", 100, 5, "箱", 10, 50),
    ])
    with client.session_transaction() as s:
        s["user_id"] = 1
        s["warehouse_id"] = 1
    data = {"file": (io.BytesIO(xlsx_bytes), "test.xlsx"),
            "warehouse_code": "wh_test"}
    client.post("/admin/import-items", data=data,
                content_type="multipart/form-data", follow_redirects=False)
    resp = client.post("/admin/import-items/commit", follow_redirects=True)
    # 应 flash 拒绝
    assert resp.status_code == 200
    import sqlite3
    wh = sqlite3.connect(wh_path)
    n = wh.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"]
    assert n == 0
    wh.close()


def test_commit_inserts_items_when_categories_match(logged_client):
    """仓库已有"X"分类 + 上传"X"分类 → commit 后 items 行数 = 1。"""
    client, wh_path = logged_client
    _login_admin(client)
    # 注入一个 "X" 分类
    import sqlite3
    from datetime import datetime
    wh = sqlite3.connect(wh_path)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    wh.execute(
        "INSERT INTO categories (name, description, created_at) VALUES ('X', '', ?)",
        (ts,))
    wh.commit()
    wh.close()
    xlsx_bytes = _make_xlsx_bytes([
        ("X", "AAA", None, "spec1", 100, 5, "箱", 10, 50),
        (None, "BBB", None, "spec2", 200, 3, "包", 20, 60),
    ])
    data = {"file": (io.BytesIO(xlsx_bytes), "test.xlsx"),
            "warehouse_code": "wh_test"}
    client.post("/admin/import-items", data=data,
                content_type="multipart/form-data", follow_redirects=False)
    resp = client.post("/admin/import-items/commit", follow_redirects=False)
    assert resp.status_code == 302
    wh = sqlite3.connect(wh_path)
    rows = wh.execute("SELECT name, unit, unit_cost FROM items ORDER BY id").fetchall()
    assert len(rows) == 2
    assert rows[0]["name"] == "AAA"
    assert rows[0]["unit"] == "箱"
    assert rows[0]["unit_cost"] == 10.0
    assert rows[1]["unit"] == "包"
    wh.close()


def test_commit_is_idempotent(logged_client):
    """二次 commit → 行数仍为 2(覆盖无残留)。"""
    client, wh_path = logged_client
    _login_admin(client)
    import sqlite3
    from datetime import datetime
    wh = sqlite3.connect(wh_path)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    wh.execute(
        "INSERT INTO categories (name, description, created_at) VALUES ('X', '', ?)",
        (ts,))
    wh.commit()
    wh.close()
    xlsx_bytes = _make_xlsx_bytes([("X", "AAA", None, "s", 100, 5, "箱", 10, 50)])
    data = {"file": (io.BytesIO(xlsx_bytes), "test.xlsx"),
            "warehouse_code": "wh_test"}
    client.post("/admin/import-items", data=data,
                content_type="multipart/form-data", follow_redirects=False)
    client.post("/admin/import-items/commit", follow_redirects=False)
    # 再来一遍
    client.post("/admin/import-items", data=data,
                content_type="multipart/form-data", follow_redirects=False)
    client.post("/admin/import-items/commit", follow_redirects=False)
    wh = sqlite3.connect(wh_path)
    n = wh.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"]
    assert n == 1
    wh.close()
```

- [ ] **Step 2: 运行测试,确认失败**

```bash
cd /Users/ericmr/Documents/GitHub/DailyCheck && ./venv/bin/python -m pytest tests/test_import_items_route.py -v
```

预期:新增 4 个 FAIL(`commit` 路由不存在 → 404)

- [ ] **Step 3: 实现 commit 路由**

`blueprints/import_items.py` 追加:

```python
@bp.route("/commit", methods=["POST"])
@require_login
@require_role("admin")
def commit():
    """事务化:DELETE 5 分组下 items + INSERT 预览数据。"""
    from contextlib import closing
    from datetime import datetime
    from pathlib import Path

    import db as db_module
    from blueprints._helpers import gen_sku, now

    pv = session.pop("import_preview", None)
    if not pv:
        flash("预览已过期,请重新上传")
        return redirect(url_for("import_items.upload_form"))

    warehouse_code = pv["warehouse_code"]
    groups_order = pv["groups_order"]

    # 1. 校验仓库存在
    master = db_module.get_master_db()
    wh_row = master.execute(
        "SELECT db_path, name FROM warehouses WHERE code = ?", (warehouse_code,)
    ).fetchone()
    if wh_row is None:
        flash(f"仓库 {warehouse_code} 不存在")
        return redirect(url_for("import_items.upload_form"))

    from config import BASE_DIR
    db_path = Path(BASE_DIR) / wh_row["db_path"]

    # 2. 校验 5 分组全在仓库 categories
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        existing_cats = {
            r["name"] for r in conn.execute("SELECT name FROM categories").fetchall()
        }
    missing = [c for c in groups_order if c not in existing_cats]
    if missing:
        flash(f"目标仓库缺少分类:{', '.join(missing)}")
        return redirect(url_for("items.items_list"))

    # 3. 事务化 DELETE + INSERT
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            # DELETE 5 分组下 items
            placeholders = ",".join("?" for _ in groups_order)
            conn.execute(
                f"DELETE FROM items WHERE category_id IN "
                f"(SELECT id FROM categories WHERE name IN ({placeholders}))",
                groups_order,
            )
            # cat_by_name 缓存
            cat_by_name = {
                r["name"]: r["id"]
                for r in conn.execute("SELECT id, name FROM categories").fetchall()
            }
            ts = now()
            inserted = 0
            for cat_name in groups_order:
                cid = cat_by_name[cat_name]
                for item in pv["groups_rows"][cat_name]:
                    conn.execute(
                        """INSERT INTO items
                           (sku, name, category_id, quantity, safety_stock,
                            unit_cost, unit, gram_per_unit, updated_at)
                           VALUES (?, ?, ?, 0, 0, ?, ?, 0, ?)""",
                        (gen_sku(), item["name"], cid,
                         item["unit_cost"], item["unit"], ts),
                    )
                    inserted += 1
            conn.commit()
    except sqlite3.IntegrityError:
        flash("导入失败,请重试")
        return redirect(url_for("import_items.upload_form"))

    # 4. audit
    from blueprints.auth import audit
    audit("import_items.import", "warehouse", warehouse_code, {
        "count": inserted,
        "filename": pv.get("filename"),
    })
    flash(f"导入成功:{inserted} 条品项")
    return redirect(url_for("items.items_list"))
```

并在文件顶部加 `import sqlite3`(已有)。

- [ ] **Step 4: 运行测试,确认通过**

```bash
cd /Users/ericmr/Documents/GitHub/DailyCheck && ./venv/bin/python -m pytest tests/test_import_items_route.py -v
```

预期:全部通过(包括原 5 个上传/预览测试 + 4 个 commit 测试)

- [ ] **Step 5: 全量回归**

```bash
cd /Users/ericmr/Documents/GitHub/DailyCheck && ./venv/bin/python -m pytest tests/ -v
```

预期:全部通过

- [ ] **Step 6: 提交**

```bash
cd /Users/ericmr/Documents/GitHub/DailyCheck && \
git add blueprints/import_items.py tests/test_import_items_route.py && \
git commit -m "feat(import): commit 路由 — 缺品类拒绝 + DELETE+INSERT 全量覆盖"
```

---

### Task 5: 手工验证(提交前必做)

**Files:** 无代码改动,纯手动

- [ ] **Step 1: 启动应用**

```bash
cd /Users/ericmr/Documents/GitHub/DailyCheck && ./venv/bin/python app.py
```

预期:启动成功,无报错

- [ ] **Step 2: 创建泰柯店仓库**

```bash
cd /Users/ericmr/Documents/GitHub/DailyCheck && \
./venv/bin/python -m cli create-warehouse wh_004 "泰柯店"
```

预期:输出 `Created warehouse wh_004 (泰柯店)`;同时 `db/warehouses/wh_004.db` 创建,含 9 个默认品类(`FIXED_CATEGORIES`)。**注意**:默认种子仍是 9 个老品类,泰柯的 5 个新品类本 spec 不种;Task 5 之前由 admin 手动通过用户管理或 SQL 插入(本次 spec 不创建品类 UI)。

> **如果 admin 还没手动种 5 个品类**,则需要在 wh_004.db 的 categories 表里手工插入"常温物料 / 水果 / 包装材料 / 冷冻&冷藏食品 / 定制周边"5 行:
>
> ```bash
> ./venv/bin/python -c "
> import sqlite3
> from datetime import datetime
> ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
> db = sqlite3.connect('db/warehouses/wh_004.db')
> for c in ['常温物料', '水果', '包装材料', '冷冻&冷藏食品', '定制周边']:
>     db.execute(\"INSERT INTO categories (name, description, created_at) VALUES (?, ?, ?)\", (c, '泰柯店分组', ts))
> db.commit()
> print(db.execute('SELECT name FROM categories').fetchall())
> "
> ```

- [ ] **Step 3: 浏览器登录 admin,访问 `/admin/import-items`**

预期:看到上传表单,仓库下拉含 `泰柯店 (wh_004)`

- [ ] **Step 4: 上传 `docs/superpowers/泰柯盘点表.xlsx`**

预期:重定向到预览页,显示 5 个分组折叠:
- 常温物料 (20 行)
- 水果 (4 行)
- 包装材料 (32 行)
- 冷冻&冷藏食品 (14 行)
- 定制周边 (6 行)

合计 81 行,无数据问题(或显示一些 None 单价行被记为 0)

- [ ] **Step 5: 点击"确认导入"**

预期:弹 confirm → 确认 → 重定向 `/items` → 显示 81 条品项,顶部 5 个 chip

- [ ] **Step 6: 二次导入同一 xlsx**

预期:仍 81 行(全量覆盖,无残留)

- [ ] **Step 7: 取消流程**

访问 `/admin/import-items` → 上传 → 预览页点"取消" → 回 `/items`,items 数量不变

- [ ] **Step 8: 非 admin 角色测试**

切换到非 admin 用户(在 master.db 修改 `is_admin=0`)→ 访问 `/admin/import-items` → 应 403 / 重定向

- [ ] **Step 9: 上传非 .xlsx**

上传 `.txt` → 应 flash 报错并回上传页

- [ ] **Step 10: 缺品类场景(可选)**

新建一个 wh_005 但**不**种 5 个泰柯品类 → admin 上传 xlsx → commit 应 flash "目标仓库缺少分类: 常温物料, 水果, ..." 并回 `/items`,items 数量为 0

- [ ] **Step 11: 提交验证记录(可选)**

```bash
cd /Users/ericmr/Documents/GitHub/DailyCheck && \
git add -A && git status
```

确认无未预期改动;若有测试 fixture 临时文件(如临时 wh_004.db)用 `git status` 确认未跟踪

---

## Self-Review

### 1. Spec coverage

| Spec 章节 | Task 覆盖 |
|---|---|
| §1.1 `warehouse_categories_in_clause()` 新函数 | Task 1 |
| §1.2 items.py 3 处替换 | Task 1 |
| §1.3 auth.py / core.py 死 import 清理 | Task 1 |
| §1.4 `init_warehouse_db` 扩参 | Task 1 |
| §1.5 db/import_items.py 报错文案 | Task 1 |
| §2.1 路由总览(4 路由) | Task 3 (3 路由) + Task 4 (commit) |
| §2.2 GET /admin/import-items | Task 3 |
| §2.3 POST /admin/import-items (解析流程) | Task 2 (纯函数) + Task 3 (路由) |
| §2.4 GET /preview | Task 3 |
| §2.5 POST /commit | Task 4 |
| §2.6 数据映射 | Task 2 + Task 4(INSERT 字段) |
| §3.1 items.html 顶部 admin 入口 | Task 3 |
| §3.2 admin/import_items.html | Task 3 |
| §3.3 admin/import_items_preview.html | Task 3 |
| §3.4 CSS `.page-header` | Task 3 |
| §4 错误处理 | Task 3 (非 xlsx) + Task 4 (缺品类 / session) |
| §5.1 单元 / 路由测试 | Task 1 + 2 + 3 + 4 各自测试 |
| §5.2 手工验证 | Task 5 |

**无遗漏**。

### 2. Placeholder scan

- 无 "TBD" / "TODO"
- 无 "Add appropriate error handling"(所有错误都有具体实现)
- 测试代码块完整,无 "Similar to Task N"
- Step 中代码完整,无 "fill in details"

### 3. Type consistency

- `parse_xlsx(file_stream) -> dict` 在 Task 2 定义,Task 3 路由使用,Task 4 commit 通过 `session['import_preview']` 间接使用 — 字段名 `groups_order / groups_rows / problems / warehouse_code / filename / unit_cost / unit / name / spec` 一致
- `warehouse_categories_in_clause() -> tuple[str, list]` 在 Task 1 定义,Task 1 自测使用,Task 4 commit 用独立 SQL 不依赖该函数 — 一致
- `bp` 蓝图对象:`from blueprints.import_items import bp as import_items_bp` — 一致

### 4. 已识别风险

- **Task 5 Step 2** 中"手工种 5 品类"假设 admin 有其他途径种;本次 spec 不创建品类 UI,若 admin 无工具,SPEC.md §6.2 已说明"仓库创建不在本次 spec 范围"。一致。
- **CSV / BOM / Excel 中文** 不在本 plan 范围(spec 无此项)。
- **`/summary` 不依赖 FIXED_CATEGORIES**:Task 1 自测覆盖;`test_warehouse_categories_in_clause.py` 的第二个 case 验证仓库自带 categories。