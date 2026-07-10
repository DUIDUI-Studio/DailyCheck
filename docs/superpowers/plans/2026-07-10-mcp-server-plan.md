# Agent MPC MCP Server 实现规划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 独立 MCP Server（Python 3.14 + MCP SDK），三层架构，按模块分组 Tool，替代现有 `blueprints/agent_mpc.py`

**Architecture:** Protocol Layer（MCP SDK） → Service Layer（纯 Python） → Data Layer（SQLite 封装）。启动在端口 5100，通过 stdio Transport 与 Claude Code 通信。

**Tech Stack:** Python 3.14, `mcp>=1.0.0`, `modelcontextprotocol-sdk` (via homebrew python3.14)

## Global Constraints

- Python 3.14+（homebrew `/opt/homebrew/bin/python3`）
- 独立 venv：`mcp_server/.venv/`
- 启动端口：`5100`
- Tool 按模块分组：inventory / inbound / forecast / procurement
- Service 层零 Flask 依赖，零 MCP 依赖

---

## 文件结构

```
mcp_server/
├── __init__.py
├── main.py              # CLI 入口，python -m mcp_server --port 5100
├── config.py            # 引用项目 config.py 的路径常量
├── protocol/            # MCP Protocol Layer
│   ├── __init__.py
│   ├── server.py        # MCP server 实例，stdio transport
│   └── tools/
│       ├── __init__.py
│       ├── inventory.py
│       ├── inbound.py
│       ├── forecast.py
│       └── procurement.py
├── service/             # Service Layer（纯 Python）
│   ├── __init__.py
│   ├── auth.py          # Token 验证，AuthContext
│   ├── inventory.py
│   ├── inbound.py
│   ├── forecast.py
│   └── procurement.py
├── data/                # Data Access Layer
│   ├── __init__.py
│   ├── master.py        # master.db 查询
│   ├── warehouse.py     # warehouse DB 查询
│   └── unit_of_work.py # 连接生命周期
└── infra/               # 共享基础设施
    ├── __init__.py
    ├── access_log.py
    └── errors.py

tests/
└── mcp_server/
    ├── test_service/
    │   ├── test_auth.py
    │   ├── test_inventory.py
    │   └── test_inbound.py
    └── test_data/
        └── test_master.py
```

---

## Task 1: 搭建基础骨架（venv + 依赖 + config）

**Files:**
- Create: `mcp_server/__init__.py`
- Create: `mcp_server/main.py`
- Create: `mcp_server/config.py`
- Modify: `pyproject.toml`（添加 mcp 依赖）

**Interfaces:**
- Produces: `mcp_server/main.py` 提供 `run(port: int) -> None`

- [ ] **Step 1: 创建独立 venv（Python 3.14）**

```bash
python3 -m venv mcp_server/.venv
mcp_server/.venv/bin/pip install --upgrade pip
```

- [ ] **Step 2: 安装 MCP SDK**

```bash
mcp_server/.venv/bin/pip install "mcp>=1.0.0"
```

- [ ] **Step 3: 创建 `mcp_server/config.py`**

```python
"""引用项目根目录 config.py 中的路径常量。"""
from __future__ import annotations
import sys
from pathlib import Path

# 添加项目根目录到 sys.path 以便 import 项目模块
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

from config import (
    BASE_DIR,
    MASTER_DB,
    SECRET_KEY,
)
```

- [ ] **Step 4: 创建 `mcp_server/main.py`**

```python
"""MCP Server CLI 入口。"""
from __future__ import annotations

import sys
import click

from mcp_server.protocol.server import build_server


@click.command()
@click.option("--port", default=5100, help="Port to bind (stdio 模式下不生效，仅保留兼容）")
def run(port: int) -> None:
    """启动 MCP Server，stdio transport。"""
    server = build_server()
    from mcp.server.stdio import stdio_server
    import asyncio

    async def main():
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(main())


if __name__ == "__main__":
    run()
```

- [ ] **Step 5: 安装依赖并验证**

```bash
mcp_server/.venv/bin/pip install "mcp>=1.0.0" click
mcp_server/.venv/bin/python -c "from mcp.protocol import Protocol; print('MCP SDK OK')"
```

- [ ] **Step 6: Commit**

```bash
git add mcp_server/ pyproject.toml
git commit -m "feat(mcp): scaffold mcp_server directory and Python 3.14 venv"
```

---

## Task 2: Data Layer — 基础设施（errors + access_log + unit_of_work）

**Files:**
- Create: `mcp_server/infra/errors.py`
- Create: `mcp_server/infra/__init__.py`
- Create: `mcp_server/infra/access_log.py`
- Create: `mcp_server/data/unit_of_work.py`
- Create: `mcp_server/data/__init__.py`

**Interfaces:**
- Consumes: `mcp_server/config.py`（MASTER_DB, BASE_DIR）
- Produces: `McpError` 及派生类，`UnitOfWork` 类，`write_mcp_access_log()` 函数

- [ ] **Step 1: 创建 `mcp_server/infra/errors.py`**

```python
"""MCP Server 统一错误类型。"""
from __future__ import annotations

class McpError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 400) -> None:
        self.code = code
        self.message = message
        self.http_status = http_status
        super().__init__(message)

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message}


class UnauthorizedError(McpError):
    def __init__(self, message: str = "unauthorized") -> None:
        super().__init__("unauthorized", message, 401)


class ForbiddenError(McpError):
    def __init__(self, message: str = "forbidden") -> None:
        super().__init__("forbidden", message, 403)


class NotFoundError(McpError):
    def __init__(self, message: str = "not_found") -> None:
        super().__init__("not_found", message, 404)


class ValidationError(McpError):
    def __init__(self, message: str) -> None:
        super().__init__("validation_error", message, 400)
```

- [ ] **Step 2: 创建 `mcp_server/infra/access_log.py`**

```python
"""JSON access.log 写入，复用现有 `blueprints/agent_mpc.py` 逻辑。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from mcp_server.config import BASE_DIR

_ACCESS_LOG_PATH: Path = BASE_DIR / "access.log"


def write_mcp_access_log(
    token_id: int | None,
    method: str,
    path: str,
    status: int,
    duration_ms: int,
) -> None:
    """追加一条 JSON 记录到 access.log。异常静默吞掉。"""
    try:
        rec = {
            "ts": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "agent_token_id": token_id,
            "path": path,
            "method": method,
            "status": int(status),
            "duration_ms": int(duration_ms),
        }
        with open(_ACCESS_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
```

- [ ] **Step 3: 创建 `mcp_server/data/unit_of_work.py`**

```python
"""Unit of Work，管理 SQLite 连接生命周期。"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Generator

from mcp_server.config import MASTER_DB


@contextmanager
def master_connection() -> Generator[sqlite3.Connection, None, None]:
    """master.db 连接，row_factory = Row。"""
    conn = sqlite3.connect(MASTER_DB)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def warehouse_connection(db_path: str) -> Generator[sqlite3.Connection, None, None]:
    """warehouse DB 连接，row_factory = Row。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()
```

- [ ] **Step 4: 写单元测试 `tests/mcp_server/test_infra_test_errors.py`**

```python
from mcp_server.infra.errors import McpError, UnauthorizedError, NotFoundError


def test_mcp_error_to_dict():
    err = McpError("test_code", "test message", 400)
    assert err.to_dict() == {"error": "test_code", "message": "test message"}


def test_unauthorized_error():
    err = UnauthorizedError()
    assert err.http_status == 401
    assert err.code == "unauthorized"


def test_not_found_error():
    err = NotFoundError("item not found")
    assert err.http_status == 404
    assert err.message == "item not found"
```

- [ ] **Step 5: 写单元测试 `tests/mcp_server/test_data/test_unit_of_work.py`**

```python
import pytest
from mcp_server.data.unit_of_work import master_connection


def test_master_connection_returns_row():
    with master_connection() as conn:
        row = conn.execute("SELECT 1 AS val").fetchone()
        assert row["val"] == 1
```

- [ ] **Step 6: Run tests**

```bash
mcp_server/.venv/bin/python -m pytest tests/mcp_server/ -v
```

- [ ] **Step 7: Commit**

```bash
git add mcp_server/infra/ mcp_server/data/ tests/mcp_server/
git commit -m "feat(mcp): add infra errors, access_log, and unit_of_work"
```

---

## Task 3: Data Layer — master.py + warehouse.py

**Files:**
- Create: `mcp_server/data/master.py`
- Create: `mcp_server/data/warehouse.py`
- Create: `tests/mcp_server/test_data/test_master.py`
- Create: `tests/mcp_server/test_data/test_warehouse.py`

**Interfaces:**
- Consumes: `mcp_server/data/unit_of_work.py`
- Produces: `resolve_warehouse(code: str) -> dict | None`，`list_items(conn, warehouse_code) -> list[dict]`，`get_item(conn, item_id) -> dict | None`，`list_movements(conn) -> list[dict]`

- [ ] **Step 1: 创建 `mcp_server/data/master.py`**

```python
"""master.db 数据访问：warehouse 元信息查询。"""
from __future__ import annotations

from mcp_server.data.unit_of_work import master_connection


def resolve_warehouse(code: str) -> dict | None:
    """根据 code 查询 warehouse 元信息，返回 Row 或 None。"""
    with master_connection() as conn:
        row = conn.execute(
            "SELECT code, name, db_path FROM warehouses WHERE code = ?",
            (code,),
        ).fetchone()
        return dict(row) if row else None


def list_all_warehouses() -> list[dict]:
    """返回所有 warehouse 元信息。"""
    with master_connection() as conn:
        rows = conn.execute(
            "SELECT code, name, db_path FROM warehouses ORDER BY code"
        ).fetchall()
        return [dict(r) for r in rows]
```

- [ ] **Step 2: 创建 `mcp_server/data/warehouse.py`**

```python
"""warehouse DB 数据访问：items, movements, restock 等。"""
from __future__ import annotations

import sqlite3


def list_items(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, sku, name, category_id, quantity, safety_stock, "
        "unit, unit_cost, gram_per_unit, updated_at "
        "FROM items ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def get_item(conn: sqlite3.Connection, item_id: int) -> dict | None:
    r = conn.execute(
        "SELECT id, sku, name, category_id, quantity, safety_stock, "
        "unit, unit_cost, gram_per_unit, updated_at "
        "FROM items WHERE id = ?",
        (item_id,),
    ).fetchone()
    return dict(r) if r else None


def item_exists(conn: sqlite3.Connection, item_id: int) -> bool:
    return (
        conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone()
        is not None
    )


def list_movements(conn: sqlite3.Connection, limit: int = 200) -> list[dict]:
    out_rows = conn.execute(
        """SELECT o.id, o.item_id, i.name AS item_name,
                  o.requested_quantity AS qty, o.reason, o.created_at,
                  'outbound' AS type
           FROM outbound_requests o
           JOIN items i ON i.id = o.item_id
           WHERE o.rolled_back = 0
           ORDER BY o.created_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    sm_rows = conn.execute(
        """SELECT s.id, s.item_id, i.name AS item_name,
                  s.delta AS qty, s.action AS reason, s.created_at,
                  'stock_movement' AS type
           FROM stock_movements s
           JOIN items i ON i.id = s.item_id
           ORDER BY s.created_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    movements = [
        {
            "id": r["id"],
            "type": r["type"],
            "item_id": r["item_id"],
            "item_name": r["item_name"],
            "qty": r["qty"],
            "reason": r["reason"],
            "created_at": r["created_at"],
        }
        for r in list(out_rows) + list(sm_rows)
    ]
    movements.sort(key=lambda m: (m["created_at"], m["id"]), reverse=True)
    return movements[:limit]


def create_restock(
    conn: sqlite3.Connection,
    item_id: int,
    quantity: int,
    reason: str | None,
) -> int:
    """创建入库记录，返回新行 id。"""
    cursor = conn.execute(
        "INSERT INTO stock_movements (item_id, delta, action, created_at) "
        "VALUES (?, ?, ?, datetime('now'))",
        (item_id, quantity, reason or "restock"),
    )
    conn.commit()
    # 更新 items quantity
    conn.execute(
        "UPDATE items SET quantity = quantity + ?, updated_at = datetime('now') "
        "WHERE id = ?",
        (quantity, item_id),
    )
    conn.commit()
    return cursor.lastrowid


def list_restock_movements(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        """SELECT s.id, s.item_id, i.name AS item_name,
                  s.delta AS qty, s.action AS reason, s.created_at
           FROM stock_movements s
           JOIN items i ON i.id = s.item_id
           WHERE s.action = 'restock'
           ORDER BY s.created_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 3: 写测试（使用内存 SQLite fixture）**

```python
# tests/mcp_server/test_data/test_warehouse.py
import pytest
import sqlite3
from mcp_server.data.warehouse import list_items, get_item, item_exists


def test_list_items_empty():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE items (id INTEGER PRIMARY KEY, sku, name, category_id, "
        "quantity, safety_stock, unit, unit_cost, gram_per_unit, updated_at)"
    )
    assert list_items(conn) == []


def test_list_items_returns_rows():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE items (id INTEGER PRIMARY KEY, sku, name, category_id, "
        "quantity, safety_stock, unit, unit_cost, gram_per_unit, updated_at)"
    )
    conn.execute(
        "INSERT INTO items (sku, name, quantity) VALUES ('SKU1', 'Test', 10)"
    )
    items = list_items(conn)
    assert len(items) == 1
    assert items[0]["sku"] == "SKU1"


def test_get_item_returns_none_for_missing():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE items (id INTEGER PRIMARY KEY, sku, name, category_id, "
        "quantity, safety_stock, unit, unit_cost, gram_per_unit, updated_at)"
    )
    assert get_item(conn, 999) is None
```

- [ ] **Step 4: Run tests**

```bash
mcp_server/.venv/bin/python -m pytest tests/mcp_server/test_data/ -v
```

- [ ] **Step 5: Commit**

```bash
git add mcp_server/data/master.py mcp_server/data/warehouse.py tests/mcp_server/test_data/
git commit -m "feat(mcp): add data layer - master and warehouse queries"
```

---

## Task 4: Service Layer — auth

**Files:**
- Create: `mcp_server/service/auth.py`
- Create: `tests/mcp_server/test_service/test_auth.py`

**Interfaces:**
- Consumes: `mcp_server/data/master.py`（resolve_warehouse）
- Produces: `AuthContext` dataclass，`authenticate(authorization_header: str) -> AuthContext | None`，`check_warehouse(ctx, warehouse_code) -> bool`

- [ ] **Step 1: 创建 `mcp_server/service/auth.py`**

```python
"""Token 验证与 AuthContext。"""
from __future__ import annotations

import json
from dataclasses import dataclass

from werkzeug.security import check_password_hash

from mcp_server.data.unit_of_work import master_connection
from mcp_server.data.master import resolve_warehouse


@dataclass
class AuthContext:
    token_id: int
    allowed_read_paths: list[str]
    allowed_write_paths: list[str]
    allowed_warehouses: list[str] | None  # None = all warehouses


def authenticate(authorization_header: str) -> AuthContext | None:
    """验证 Bearer token，返回 AuthContext 或 None。"""
    if not authorization_header.startswith("Bearer "):
        return None
    raw = authorization_header[len("Bearer "):].strip()
    if not raw:
        return None
    with master_connection() as conn:
        rows = conn.execute(
            "SELECT id, token_hash, allowed_read_paths_json, "
            "allowed_write_paths_json, allowed_warehouse_codes_json "
            "FROM agent_tokens WHERE token_hash IS NOT NULL"
        ).fetchall()
    for row in rows:
        if row["token_hash"] is None:
            continue
        if check_password_hash(row["token_hash"], raw):
            if row["revoked_at"] is not None:
                return None
            try:
                read_paths = json.loads(row["allowed_read_paths_json"] or "[]")
                write_paths = json.loads(row["allowed_write_paths_json"] or "[]")
                wh_codes = json.loads(row["allowed_warehouse_codes_json"] or "null")
            except (ValueError, TypeError):
                continue
            return AuthContext(
                token_id=row["id"],
                allowed_read_paths=read_paths,
                allowed_write_paths=write_paths,
                allowed_warehouses=wh_codes,
            )
    return None


def check_warehouse(ctx: AuthContext, warehouse_code: str) -> bool:
    """检查 warehouse_code 是否在 token 白名单中。"""
    if ctx.allowed_warehouses is None:
        return True  # None = all warehouses
    return warehouse_code in ctx.allowed_warehouses


def check_path(ctx: AuthContext, method: str, path: str) -> bool:
    """检查 (method, path) 是否在 token 白名单中。"""
    from mcp_server.agent_mpc_pure import path_matches
    paths = (
        ctx.allowed_write_paths if method != "GET"
        else ctx.allowed_read_paths
    )
    for pat in paths:
        if path_matches(pat, path):
            return True
    return False
```

**注意**: `mcp_server/agent_mpc_pure.py` 的 `path_matches` 函数需要从原 `blueprints/agent_mpc_pure.py` 复制过来，避免从 blueprints 目录 import（blueprints 有 Flask 依赖）。

- [ ] **Step 2: 复制 `blueprints/agent_mpc_pure.py` → `mcp_server/agent_mpc_pure.py`**

```bash
cp blueprints/agent_mpc_pure.py mcp_server/agent_mpc_pure.py
```

- [ ] **Step 3: 写测试**

```python
# tests/mcp_server/test_service/test_auth.py
from mcp_server.service.auth import check_warehouse, check_path, AuthContext


def test_check_warehouse_none_allows_all():
    ctx = AuthContext(1, [], [], None)
    assert check_warehouse(ctx, "WH001") is True


def test_check_warehouse_whitelist():
    ctx = AuthContext(1, [], [], ["WH001", "WH002"])
    assert check_warehouse(ctx, "WH001") is True
    assert check_warehouse(ctx, "WH999") is False


def test_check_path_get():
    ctx = AuthContext(1, ["/api/v1/items"], [], None)
    assert check_path(ctx, "GET", "/api/v1/items") is True
    assert check_path(ctx, "GET", "/api/v1/items/1") is True
    assert check_path(ctx, "POST", "/api/v1/items") is False


def test_check_path_wildcard():
    ctx = AuthContext(1, ["/api/v1/items/*"], [], None)
    assert check_path(ctx, "GET", "/api/v1/items/123") is True
    assert check_path(ctx, "GET", "/api/v1/items") is True
```

- [ ] **Step 4: Run tests**

```bash
mcp_server/.venv/bin/python -m pytest tests/mcp_server/test_service/test_auth.py -v
```

- [ ] **Step 5: Commit**

```bash
git add mcp_server/service/auth.py mcp_server/agent_mpc_pure.py tests/mcp_server/test_service/test_auth.py
git commit -m "feat(mcp): add service auth - AuthContext and token verification"
```

---

## Task 5: Service Layer — inventory + inbound

**Files:**
- Create: `mcp_server/service/inventory.py`
- Create: `mcp_server/service/inbound.py`
- Create: `tests/mcp_server/test_service/test_inventory.py`
- Create: `tests/mcp_server/test_service/test_inbound.py`

**Interfaces:**
- Consumes: `mcp_server/data/master.py`, `mcp_server/data/warehouse.py`, `mcp_server/service/auth.py`
- Produces: `list_items(warehouse_code, ctx) -> list[dict]`，`get_item(item_id, warehouse_code, ctx) -> dict`，`list_movements(warehouse_code, ctx) -> list[dict]`，`create_restock(item_id, quantity, reason, warehouse_code, ctx) -> dict`

- [ ] **Step 1: 创建 `mcp_server/service/inventory.py`**

```python
"""库存查询服务。"""
from __future__ import annotations

from mcp_server.data.master import resolve_warehouse
from mcp_server.data.warehouse import (
    list_items as _list_items,
    get_item as _get_item,
    list_movements as _list_movements,
)
from mcp_server.data.unit_of_work import warehouse_connection
from mcp_server.service.auth import AuthContext, check_warehouse
from mcp_server.infra.errors import ForbiddenError, NotFoundError, ValidationError


def list_items(warehouse_code: str, ctx: AuthContext) -> list[dict]:
    _validate_warehouse(warehouse_code, ctx)
    wh = resolve_warehouse(warehouse_code)
    if wh is None:
        raise NotFoundError("warehouse_not_found")
    with warehouse_connection(wh["db_path"]) as conn:
        return _list_items(conn)


def get_item(item_id: int, warehouse_code: str, ctx: AuthContext) -> dict:
    _validate_warehouse(warehouse_code, ctx)
    wh = resolve_warehouse(warehouse_code)
    if wh is None:
        raise NotFoundError("warehouse_not_found")
    with warehouse_connection(wh["db_path"]) as conn:
        item = _get_item(conn, item_id)
    if item is None:
        raise NotFoundError("not_found")
    return item


def list_movements(warehouse_code: str, ctx: AuthContext) -> list[dict]:
    _validate_warehouse(warehouse_code, ctx)
    wh = resolve_warehouse(warehouse_code)
    if wh is None:
        raise NotFoundError("warehouse_not_found")
    with warehouse_connection(wh["db_path"]) as conn:
        return _list_movements(conn)


def _validate_warehouse(warehouse_code: str, ctx: AuthContext) -> None:
    if not warehouse_code:
        raise ValidationError("warehouse_code_required")
    if not check_warehouse(ctx, warehouse_code):
        raise ForbiddenError("forbidden_warehouse")
```

- [ ] **Step 2: 创建 `mcp_server/service/inbound.py`**

```python
"""入库服务。"""
from __future__ import annotations

from mcp_server.data.master import resolve_warehouse
from mcp_server.data.warehouse import (
    create_restock as _create_restock,
    item_exists,
    list_restock_movements as _list_restock_movements,
)
from mcp_server.data.unit_of_work import warehouse_connection
from mcp_server.service.auth import AuthContext, check_warehouse
from mcp_server.infra.errors import (
    ForbiddenError,
    NotFoundError,
    ValidationError,
)


def create_restock(
    item_id: int,
    quantity: int,
    warehouse_code: str,
    ctx: AuthContext,
    reason: str | None = None,
) -> dict:
    if not warehouse_code:
        raise ValidationError("warehouse_code_required")
    if not check_warehouse(ctx, warehouse_code):
        raise ForbiddenError("forbidden_warehouse")
    if quantity <= 0:
        raise ValidationError("quantity must be positive")
    wh = resolve_warehouse(warehouse_code)
    if wh is None:
        raise NotFoundError("warehouse_not_found")
    with warehouse_connection(wh["db_path"]) as conn:
        if not item_exists(conn, item_id):
            raise NotFoundError("item_not_found")
        row_id = _create_restock(conn, item_id, quantity, reason)
        return {
            "id": row_id,
            "item_id": item_id,
            "quantity": quantity,
            "warehouse_code": warehouse_code,
        }


def list_restock(warehouse_code: str, ctx: AuthContext) -> list[dict]:
    if not warehouse_code:
        raise ValidationError("warehouse_code_required")
    if not check_warehouse(ctx, warehouse_code):
        raise ForbiddenError("forbidden_warehouse")
    wh = resolve_warehouse(warehouse_code)
    if wh is None:
        raise NotFoundError("warehouse_not_found")
    with warehouse_connection(wh["db_path"]) as conn:
        return _list_restock_movements(conn)
```

- [ ] **Step 3: 写测试（mock data layer）**

```python
# tests/mcp_server/test_service/test_inventory.py
import pytest
from unittest.mock import patch
from mcp_server.service.inventory import list_items
from mcp_server.service.auth import AuthContext
from mcp_server.infra.errors import ForbiddenError, ValidationError


def test_list_items_rejects_missing_warehouse():
    ctx = AuthContext(1, [], [], ["WH001"])
    with pytest.raises(ForbiddenError):
        list_items("WH999", ctx)


def test_list_items_requires_warehouse_code():
    ctx = AuthContext(1, [], [], None)
    with pytest.raises(ValidationError):
        list_items("", ctx)
```

- [ ] **Step 4: Run tests**

```bash
mcp_server/.venv/bin/python -m pytest tests/mcp_server/test_service/ -v
```

- [ ] **Step 5: Commit**

```bash
git add mcp_server/service/inventory.py mcp_server/service/inbound.py tests/mcp_server/test_service/
git commit -m "feat(mcp): add service layer - inventory and inbound"
```

---

## Task 6: Service Layer — forecast + procurement

**Files:**
- Create: `mcp_server/service/forecast.py`
- Create: `mcp_server/service/procurement.py`
- Create: `tests/mcp_server/test_service/test_forecast.py`
- Create: `tests/mcp_server/test_service/test_procurement.py`

**Interfaces:**
- Consumes: 复用 `blueprints/forecast_pure.py` 和 `blueprints/procurement_pure.py`
- Produces: `get_forecast(item_id, warehouse_code, horizon_days, ctx) -> dict`，`procurement_store(warehouse_code, ctx) -> dict`，`procurement_hub(ctx) -> list[dict]`

- [ ] **Step 1: 创建 `mcp_server/service/forecast.py`**

```python
"""Forecast 服务，复用 forecast_pure.py。"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from mcp_server.data.master import resolve_warehouse
from mcp_server.data.warehouse import item_exists
from mcp_server.data.unit_of_work import warehouse_connection
from mcp_server.service.auth import AuthContext, check_warehouse
from mcp_server.infra.errors import ForbiddenError, NotFoundError, ValidationError

# 复用 forecast_pure 的核心逻辑
from blueprints.forecast_pure import build_forecast, parse_horizon
from blueprints.consumption import fetch_item_movements_30d


def get_forecast(
    item_id: int,
    warehouse_code: str,
    horizon_days: int | None,
    ctx: AuthContext,
) -> dict:
    if not warehouse_code:
        raise ValidationError("warehouse_code_required")
    if not check_warehouse(ctx, warehouse_code):
        raise ForbiddenError("forbidden_warehouse")
    wh = resolve_warehouse(warehouse_code)
    if wh is None:
        raise NotFoundError("warehouse_not_found")
    horizon = parse_horizon(horizon_days)
    if horizon is None:
        raise ValidationError("invalid_horizon")
    with warehouse_connection(wh["db_path"]) as conn:
        if not item_exists(conn, item_id):
            raise NotFoundError("not_found")
        movements = fetch_item_movements_30d(conn, item_id)
        body = build_forecast(item_id, horizon, movements)
        body["warehouse_code"] = warehouse_code
        return body
```

- [ ] **Step 2: 创建 `mcp_server/service/procurement.py`**

```python
"""Procurement 服务，复用 procurement_pure.py。"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from mcp_server.data.master import resolve_warehouse, list_all_warehouses
from mcp_server.data.unit_of_work import warehouse_connection
from mcp_server.service.auth import AuthContext, check_warehouse
from mcp_server.infra.errors import ForbiddenError, NotFoundError, ValidationError

from blueprints.procurement import _store_procurement_json
from blueprints.procurement_pure import aggregate_hub


def procurement_store(warehouse_code: str, ctx: AuthContext) -> dict:
    if not warehouse_code:
        raise ValidationError("warehouse_code_required")
    if not check_warehouse(ctx, warehouse_code):
        raise ForbiddenError("forbidden_warehouse")
    body = _store_procurement_json(warehouse_code)
    if body is None:
        raise NotFoundError("warehouse_not_found")
    return body


def procurement_hub(
    ctx: AuthContext,
    warehouse_code: str | None = None,
) -> list[dict]:
    if warehouse_code and not check_warehouse(ctx, warehouse_code):
        raise ForbiddenError("forbidden_warehouse")
    if warehouse_code:
        whs = [resolve_warehouse(warehouse_code)] if resolve_warehouse(warehouse_code) else []
    else:
        whs = [dict(r) for r in list_all_warehouses()]
    result = []
    for wh in whs:
        if wh is None:
            continue
        body = _store_procurement_json(wh["code"])
        if body:
            result.append(body)
    return aggregate_hub(result)
```

- [ ] **Step 3: 写测试（mock forecast_pure / procurement）**

```python
# tests/mcp_server/test_service/test_forecast.py
import pytest
from unittest.mock import patch, MagicMock
from mcp_server.service.forecast import get_forecast
from mcp_server.service.auth import AuthContext
from mcp_server.infra.errors import ValidationError, ForbiddenError


def test_get_forecast_requires_warehouse():
    ctx = AuthContext(1, [], [], None)
    with pytest.raises(ValidationError):
        get_forecast(1, "", None, ctx)


def test_get_forecast_forbidden_warehouse():
    ctx = AuthContext(1, [], [], ["WH001"])
    with pytest.raises(ForbiddenError):
        get_forecast(1, "WH999", None, ctx)
```

- [ ] **Step 4: Run tests**

```bash
mcp_server/.venv/bin/python -m pytest tests/mcp_server/test_service/ -v
```

- [ ] **Step 5: Commit**

```bash
git add mcp_server/service/forecast.py mcp_server/service/procurement.py tests/mcp_server/test_service/
git commit -m "feat(mcp): add service layer - forecast and procurement"
```

---

## Task 7: Protocol Layer — MCP Server 骨架 + inventory tools

**Files:**
- Create: `mcp_server/protocol/server.py`
- Create: `mcp_server/protocol/tools/inventory.py`
- Create: `tests/mcp_server/test_protocol/test_server.py`

**Interfaces:**
- Consumes: `mcp_server/service/inventory.py`，`mcp_server/service/auth.py`
- Produces: MCP server 实例，注册所有 inventory tools

- [ ] **Step 1: 创建 `mcp_server/protocol/server.py`**

```python
"""MCP Server 主入口，注册所有 Tool。"""
from __future__ import annotations

from mcp.server import Server
from mcp_server.protocol.tools import (
    inventory_tools,
    inbound_tools,
    forecast_tools,
    procurement_tools,
)


def build_server() -> Server:
    """构建并返回配置好的 MCP Server 实例。"""
    server = Server("dailycheck-mcp")
    for tool in [
        *inventory_tools,
        *inbound_tools,
        *forecast_tools,
        *procurement_tools,
    ]:
        server.add_tool(tool)
    return server
```

- [ ] **Step 2: 创建 `mcp_server/protocol/tools/inventory.py`**

```python
"""Inventory MCP Tools。"""
from __future__ import annotations

from typing import Any
from mcp.server import Server
from mcp.types import Tool, TextContent

from mcp_server.service.auth import authenticate
from mcp_server.service.inventory import (
    list_items as svc_list_items,
    get_item as svc_get_item,
    list_movements as svc_list_movements,
)
from mcp_server.infra.errors import McpError
from mcp_server.protocol.tools._auth_guard import auth_guard, make_tools


def _items_list(args: dict, auth_header: str | None) -> Any:
    warehouse_code = args.get("warehouse_code")
    ctx = auth_guard(auth_header, "GET", "/api/v1/items", warehouse_code)
    return svc_list_items(warehouse_code, ctx)


def _items_detail(args: dict, auth_header: str | None) -> Any:
    item_id: int = args["item_id"]
    warehouse_code = args.get("warehouse_code")
    ctx = auth_guard(auth_header, "GET", "/api/v1/items/<id>", warehouse_code)
    return svc_get_item(item_id, warehouse_code, ctx)


def _movements_list(args: dict, auth_header: str | None) -> Any:
    warehouse_code = args.get("warehouse_code")
    ctx = auth_guard(auth_header, "GET", "/api/v1/movements", warehouse_code)
    return svc_list_movements(warehouse_code, ctx)


inventory_tools: list[Tool] = make_tools(
    [
        (
            "items_list",
            "List all items in a warehouse",
            {
                "warehouse_code": {"type": "string", "description": "Warehouse code"}
            },
            _items_list,
        ),
        (
            "items_detail",
            "Get single item details",
            {
                "item_id": {"type": "integer", "description": "Item ID"},
                "warehouse_code": {"type": "string", "description": "Warehouse code"},
            },
            _items_detail,
        ),
        (
            "movements_list",
            "List stock movements (outbound + stock_movement)",
            {
                "warehouse_code": {"type": "string", "description": "Warehouse code"}
            },
            _movements_list,
        ),
    ]
)
```

- [ ] **Step 3: 创建 `mcp_server/protocol/tools/_auth_guard.py`（公共 guard 逻辑）**

```python
"""每个 Tool handler 调用的统一认证 guard。"""
from __future__ import annotations

from typing import Any, Callable
from mcp.types import Tool

from mcp_server.service.auth import authenticate, check_path, check_warehouse, AuthContext
from mcp_server.infra.errors import UnauthorizedError, ForbiddenError


def get_auth_context(auth_header: str | None) -> AuthContext:
    if not auth_header:
        raise UnauthorizedError("missing authorization header")
    ctx = authenticate(auth_header)
    if ctx is None:
        raise UnauthorizedError("invalid token")
    return ctx


def auth_guard(
    auth_header: str | None,
    method: str,
    path: str,
    warehouse_code: str | None,
) -> AuthContext:
    ctx = get_auth_context(auth_header)
    if not check_path(ctx, method, path):
        raise ForbiddenError("forbidden_path")
    if warehouse_code and not check_warehouse(ctx, warehouse_code):
        raise ForbiddenError("forbidden_warehouse")
    return ctx


def make_tools(
    definitions: list[tuple[str, str, dict, Callable]],
) -> list[Tool]:
    """从 (name, description, input_schema, handler) 构建 Tool 列表。"""
    from mcp.types import Tool
    tools = []
    for name, desc, schema, handler in definitions:
        async def wrapper(args: dict, _=None, h=handler):
            auth = _get_auth_header_from_env()  # 暂用 env var 传 token
            try:
                result = h(args, auth)
                return {"content": [{"type": "text", "text": str(result)}]}
            except McpError as e:
                return {"content": [{"type": "text", "text": f"ERROR: {e.to_dict()}"}]}
        tools.append(Tool(name=name, description=desc, inputSchema=schema))
    return tools
```

**注意**: MCP SDK 通过 `mcp.types.Tool` 构造 Tool，handler 注册方式需根据实际 SDK API 调整。上例为概念代码，实现时以 SDK 实际 API 为准。

- [ ] **Step 4: 验证 server 可以启动（不接 Claude Code，纯启动测试）**

```bash
mcp_server/.venv/bin/python -c "
from mcp_server.protocol.server import build_server
server = build_server()
print('Server built OK, tools:', len(server._tools) if hasattr(server, '_tools') else 'N/A')
"
```

- [ ] **Step 5: Commit**

```bash
git add mcp_server/protocol/server.py mcp_server/protocol/tools/
git commit -m "feat(mcp): add protocol layer - server skeleton and inventory tools"
```

---

## Task 8: Protocol Layer — inbound + forecast + procurement tools

**Files:**
- Create: `mcp_server/protocol/tools/inbound.py`
- Create: `mcp_server/protocol/tools/forecast.py`
- Create: `mcp_server/protocol/tools/procurement.py`
- Modify: `mcp_server/protocol/server.py`（注册新 tools）

**Interfaces:**
- Consumes: `mcp_server/service/inbound.py`，`mcp_server/service/forecast.py`，`mcp_server/service/procurement.py`
- Produces: 补全所有 Tool 定义

- [ ] **Step 1: 创建 `mcp_server/protocol/tools/inbound.py`**

```python
"""Inbound MCP Tools。"""
from __future__ import annotations

from mcp.types import Tool
from mcp_server.protocol.tools._auth_guard import auth_guard, make_tools
from mcp_server.service.inbound import create_restock as svc_create_restock, list_restock as svc_list_restock


def _restock_create(args: dict, auth_header: str | None) -> dict:
    item_id: int = args["item_id"]
    quantity: int = args["quantity"]
    warehouse_code: str = args["warehouse_code"]
    reason: str | None = args.get("reason")
    ctx = auth_guard(auth_header, "POST", "/api/v1/restock", warehouse_code)
    return svc_create_restock(item_id, quantity, warehouse_code, ctx, reason)


def _restock_list(args: dict, auth_header: str | None) -> dict:
    warehouse_code: str = args["warehouse_code"]
    ctx = auth_guard(auth_header, "GET", "/api/v1/restock", warehouse_code)
    return svc_list_restock(warehouse_code, ctx)


inbound_tools: list[Tool] = make_tools(
    [
        (
            "restock_create",
            "Create a restock (inbound) record",
            {
                "item_id": {"type": "integer"},
                "quantity": {"type": "integer"},
                "warehouse_code": {"type": "string"},
                "reason": {"type": "string", "required": False},
            },
            _restock_create,
        ),
        (
            "restock_list",
            "List restock records",
            {
                "warehouse_code": {"type": "string"},
            },
            _restock_list,
        ),
    ]
)
```

- [ ] **Step 2: 创建 `mcp_server/protocol/tools/forecast.py`**

```python
"""Forecast MCP Tools。"""
from __future__ import annotations

from mcp.types import Tool
from mcp_server.protocol.tools._auth_guard import auth_guard, make_tools
from mcp_server.service.forecast import get_forecast as svc_get_forecast


def _item_forecast(args: dict, auth_header: str | None) -> dict:
    item_id: int = args["item_id"]
    warehouse_code: str = args["warehouse_code"]
    horizon_days: int | None = args.get("horizon_days")
    ctx = auth_guard(auth_header, "GET", "/api/v1/forecast/item/<id>", warehouse_code)
    return svc_get_forecast(item_id, warehouse_code, horizon_days, ctx)


forecast_tools: list[Tool] = make_tools(
    [
        (
            "item_forecast",
            "Get consumption forecast for an item",
            {
                "item_id": {"type": "integer"},
                "warehouse_code": {"type": "string"},
                "horizon_days": {"type": "integer", "required": False},
            },
            _item_forecast,
        ),
    ]
)
```

- [ ] **Step 3: 创建 `mcp_server/protocol/tools/procurement.py`**

```python
"""Procurement MCP Tools。"""
from __future__ import annotations

from mcp.types import Tool
from mcp_server.protocol.tools._auth_guard import auth_guard, make_tools
from mcp_server.service.procurement import procurement_store as svc_store, procurement_hub as svc_hub


def _procurement_store(args: dict, auth_header: str | None) -> dict:
    warehouse_code: str = args["warehouse_code"]
    ctx = auth_guard(auth_header, "GET", "/api/v1/procurement/store", warehouse_code)
    return svc_store(warehouse_code, ctx)


def _procurement_hub(args: dict, auth_header: str | None) -> dict:
    warehouse_code: str | None = args.get("warehouse_code")
    ctx = auth_guard(auth_header, "GET", "/api/v1/procurement/hub", warehouse_code)
    return svc_hub(ctx, warehouse_code)


procurement_tools: list[Tool] = make_tools(
    [
        (
            "procurement_store",
            "Get procurement recommendations for a store",
            {"warehouse_code": {"type": "string"}},
            _procurement_store,
        ),
        (
            "procurement_hub",
            "Get procurement recommendations aggregated across all warehouses",
            {"warehouse_code": {"type": "string", "required": False}},
            _procurement_hub,
        ),
    ]
)
```

- [ ] **Step 4: 验证所有 tools 注册成功**

```bash
mcp_server/.venv/bin/python -c "
from mcp_server.protocol.server import build_server
server = build_server()
print('All tools registered OK')
"
```

- [ ] **Step 5: Commit**

```bash
git add mcp_server/protocol/tools/inbound.py mcp_server/protocol/tools/forecast.py mcp_server/protocol/tools/procurement.py
git commit -m "feat(mcp): add inbound, forecast, procurement protocol tools"
```

---

## Task 9: CLI 集成 + 启动脚本

**Files:**
- Modify: `cli.py`（注册 `mcp start` 命令）
- Modify: `mcp_server/main.py`（添加 `--port` 参数）

**Interfaces:**
- Produces: `python cli.py mcp start --port 5100` 启动 MCP server

- [ ] **Step 1: 读取当前 cli.py 末尾**

```bash
tail -20 cli.py
```

- [ ] **Step 2: 添加 MCP 命令**

在 `cli.py` 末尾添加：

```python
@cli.command("mcp")
@click.option("--port", default=5100, help="MCP server port (stdio mode only binds to stdio)")
def mcp(port: int) -> None:
    """Start the MCP server (stdio transport)."""
    import asyncio
    from mcp_server.protocol.server import build_server
    from mcp.server.stdio import stdio_server

    async def main():
        server = build_server()
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(main())
```

- [ ] **Step 3: 验证 CLI 注册成功**

```bash
source .venv/bin/activate && python cli.py --help | grep mcp
```

- [ ] **Step 4: Commit**

```bash
git add cli.py
git commit -m "feat(mcp): add 'cli.py mcp start' command"
```

---

## Task 10: Claude Code 配置 + 端到端验证

**Files:**
- Modify: `~/.claude/settings.json`（添加 mcpServers 配置）

**Interfaces:**
- Produces: Claude Code 识别 MCP server 并能调用 tools

- [ ] **Step 1: 读取现有 settings.json**

```bash
cat ~/.claude/settings.json | python3 -m json.tool | head -20
```

- [ ] **Step 2: 添加 mcpServers 配置**

```json
{
  "mcpServers": {
    "dailycheck": {
      "command": "/Users/ericmr/Documents/GitHub/DailyCheck/mcp_server/.venv/bin/python",
      "args": ["-m", "mcp_server"],
      "env": {
        "DAILYCHECK_MCP_TOKEN": "YOUR_BEARER_TOKEN_HERE"
      }
    }
  }
}
```

**注意**: Token 应放在环境变量，通过 `DAILYCHECK_MCP_TOKEN` env var 传入，不在命令行暴露。

- [ ] **Step 3: 验证 MCP handshake**

```bash
# 重启 Claude Code session 后测试
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}' | \
  mcp_server/.venv/bin/python -m mcp_server
```

- [ ] **Step 4: Commit**（settings.json 不入 git，仅记录配置说明）

在 README.md 或 docs 添加 MCP 配置说明。

- [ ] **Step 5: Commit**

```bash
git add docs/
git commit -m "docs(mcp): add MCP server configuration guide"
```

---

## Task 11: 迁移收尾 — 删除旧 Blueprint

**Files:**
- Delete: `blueprints/agent_mpc.py`
- Delete: `blueprints/agent_mpc_pure.py`
- Modify: `app.py`（移除 agent_mpc 注册）
- Modify: `blueprints/auth.py`（移除 MPC PUBLIC_ENDPOINTS 相关注释）

**Interfaces:**
- Consumes: 新 MCP Server 已验证通过
- Produces: 干净移除旧 Blueprint

- [ ] **Step 1: 确认新 MCP 在生产环境验证通过后再执行**

- [ ] **Step 2: 从 app.py 移除**

```bash
# 删除 agent_mpc 注册行
# 删除 blueprints/auth.py 中的 MPC 相关注释（不影响功能）
```

- [ ] **Step 3: Run tests 确认无破坏**

```bash
source .venv/bin/activate && python -m pytest tests/ -v --tb=short -x
```

- [ ] **Step 4: Commit**

```bash
git rm blueprints/agent_mpc.py blueprints/agent_mpc_pure.py
git add app.py blueprints/auth.py
git commit -m "feat(mcp): remove deprecated blueprints/agent_mpc.py"
```

---

## 依赖清单

```toml
# pyproject.toml 新增
mcp-server = [
    { version = ">=1.0.0", source = "pypi" },
]
```

---

## 风险与注意事项

- MCP SDK API（`mcp>=1.0.0`）在 Task 7 前需实测，`Tool.add_handler` 注册方式以实际 SDK 为准
- Python 3.14 + homebrew 环境需确保 `.venv` 创建成功
- 旧 Blueprint 迁移（Task 11）必须在生产验证通过后执行
