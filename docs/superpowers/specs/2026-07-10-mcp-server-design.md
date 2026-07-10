# Agent MPC MCP Server 重构设计

**日期**: 2026-07-10
**状态**: Approved
**分支**: feat/agent-mpc

## 背景

当前 `blueprints/agent_mpc.py` 以 Flask Blueprint 形式实现 HTTP/JSON 接口，存在以下生产级问题：

- **架构混乱**: Route、SQL、权限逻辑全混在同一个文件
- **无服务层**: 业务逻辑直接嵌入 route，无法独立测试
- **无数据访问层**: `sqlite3.connect` + 裸 SQL 散落各处，无事务管理
- **Auth 重复检查**: Token 验证在每条 route 重复调用

本次重构为独立 MCP Server（Mode A），使用官方 MCP Python SDK，通过 stdio Transport 与 Claude Code 对接。

---

## 目标

1. 独立 MCP Server 进程，端口 5100
2. 三层架构：Protocol → Service → Data
3. 按功能模块分类 Tool（inventory / inbound / forecast / procurement）
4. Service 层完全无 Flask、无 MCP 依赖，可单元测试
5. 逐步迁移，新老并存，最终废弃旧 Blueprint

---

## 架构

```
mcp_server/
├── __init__.py
├── main.py              # CLI 入口
├── config.py            # 引用项目 config.py
│
├── protocol/            # MCP Protocol Layer
│   ├── __init__.py
│   ├── server.py        # MCP server 实例，stdio transport
│   └── tools/
│       ├── __init__.py
│       ├── inventory.py    # 库存：items_list, items_detail, movements_list
│       ├── inbound.py      # 入库：restock_create, inbound_list
│       ├── forecast.py     # Forecast：item_forecast, recompute
│       └── procurement.py  # Procurement：store, hub, recompute
│
├── service/             # Service Layer（纯 Python，无协议依赖）
│   ├── __init__.py
│   ├── auth.py          # Token 验证，warehouse 白名单
│   ├── inventory.py     # 库存业务逻辑
│   ├── inbound.py       # 入库业务逻辑
│   ├── forecast.py      # Forecast（复用 forecast_pure）
│   └── procurement.py   # Procurement（复用 procurement_pure）
│
├── data/                # Data Access Layer
│   ├── __init__.py
│   ├── master.py        # master.db 查询（warehouse resolve）
│   ├── warehouse.py      # warehouse DB 查询
│   └── unit_of_work.py  # 事务管理，连接生命周期
│
└── infra/               # 共享基础设施
    ├── __init__.py
    ├── access_log.py    # JSON access.log 写入
    └── errors.py        # 统一错误类型（McpError）
```

---

## 三层职责

### Data Layer (`data/`)

- 封装所有 `sqlite3.connect` 调用
- 两套 DB：`master.db`（元信息）和 warehouse DB（业务数据）
- `UnitOfWork` 管理连接生命周期，支持 context manager
- **不包含任何业务判断**，只负责查询和写入

```python
# data/master.py
def resolve_warehouse(code: str) -> WarehouseRow | None:
    ...

# data/warehouse.py
def list_items(conn: sqlite3.Connection) -> list[ItemRow]:
    ...

# data/unit_of_work.py
class UnitOfWork:
    def __enter__(self) -> sqlite3.Connection: ...
    def __exit__(self, *args): ...
    def commit(self): ...
```

### Service Layer (`service/`)

- 接收 Python 对象（ dataclass），返回 Python 对象
- **完全无 Flask / 无 MCP 依赖**
- 包含所有业务逻辑（权限判断、参数验证、跨表聚合）
- 权限在入口统一检查，不重复

```python
# service/inventory.py
@dataclass
class ListItemsRequest:
    warehouse_code: str
    token_row: dict

def list_items(req: ListItemsRequest) -> ListItemsResponse:
    # 1. auth.check_warehouse_allowed
    # 2. data.warehouse.list_items
    # 3. return response
```

### Protocol Layer (`protocol/`)

- MCP Tool definition → Service method 调用 → JSON response
- `server.py` 初始化 MCP server，注册所有 Tool
- 每个 `tools/*.py` 按模块聚合，负责 JSON Schema 构造和响应转换

```python
# protocol/server.py
server = Server("dailycheck-mcp")
for tool in [*inventory_tools, *inbound_tools, ...]:
    server.add_tool(tool)

if __name__ == "__main__":
    stdio_server.run(server)
```

---

## Tool 分组

### inventory（库存查询）

| Tool | 说明 |
|------|------|
| `items_list` | 列出仓库所有物品 |
| `items_detail` | 单个物品详情 |
| `movements_list` | 出库记录 + 库存变动流水 |

### inbound（入库 / restock）

| Tool | 说明 |
|------|------|
| `restock_create` | 创建入库单 |
| `inbound_list` | 入库记录列表 |

### forecast（预测）

| Tool | 说明 |
|------|------|
| `item_forecast` | 单品预测（消费趋势 + 补货建议） |
| `forecast_recompute` | 触发重新计算 |

### procurement（采购）

| Tool | 说明 |
|------|------|
| `procurement_store` | 门店采购建议 |
| `procurement_hub` | 中心仓汇总 |
| `procurement_recompute` | 触发重新计算 |

---

## Auth 设计

Token 验证在 MCP Protocol 层一次性完成，结果通过 `Context` 传递：

```python
# service/auth.py
@dataclass
class AuthContext:
    token_id: int
    allowed_read_paths: list[str]
    allowed_write_paths: list[str]
    allowed_warehouses: list[str] | None  # None = all

def authenticate(authorization_header: str) -> AuthContext | None:
    ...

def check_warehouse(ctx: AuthContext, warehouse_code: str) -> bool:
    ...
```

MCP Protocol 层在每个 Tool call 入口做一次验证，Service 层不再重复。

---

## 错误处理

统一 `McpError` 类型，Protocol 层捕获并转为 MCP error response：

```python
# infra/errors.py
class McpError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 400): ...

# 派生类型
class UnauthorizedError(McpError): ...
class ForbiddenError(McpError): ...
class NotFoundError(McpError): ...
class ValidationError(McpError): ...
```

---

## 启动方式

```bash
# 方式 1：直接运行
python -m mcp_server --port 5100

# 方式 2：通过项目 CLI（方案B）
python cli.py mcp start --port 5100
```

注册到 `cli.py`：

```python
# cli.py
@cli.command("mcp")
@click.option("--port", default=5100)
def mcp(start, port):
    from mcp_server.main import run
    run(port=port)
```

Claude Code `settings.json` 配置：

```json
{
  "mcpServers": {
    "dailycheck": {
      "command": "python",
      "args": ["-m", "mcp_server"],
      "env": {
        "PORT": "5100"
      }
    }
  }
}
```

---

## 迁移策略

```
Phase 1: 新 MCP Server 开发完成，独立启动（端口 5100）
Phase 2: Claude Code 切换到新 MCP，验证功能一致
Phase 3: 删除 blueprints/agent_mpc.py + blueprints/agent_mpc_pure.py
Phase 4: 清理 auth.py 中 MPC 相关的 PUBLIC_ENDPOINTS 条目
```

---

## 依赖

```toml
# 新增依赖（pyproject.toml）
mcp >= 0.1.0
```

现有依赖复用：`werkzeug`, `sqlite3`（标准库），`blueprints/forecast_pure.py`（复用逻辑）。

---

## 测试策略

| 层级 | 覆盖要求 |
|------|---------|
| Data Layer | pytest + sqlite 内存 fixture，SQL 正确性 |
| Service Layer | pytest，纯 Python mock，无 DB |
| Protocol Layer | 集成测试，验证 MCP JSON-RPC round-trip |

---

## 风险与注意事项

- **Token 验证性能**: 当前线性扫描所有 token（handful 量级，可接受），未来 token 增多时需加索引或换哈希存储
- **仓库 DB 路径**: 从 master.db 读取，安全点需验证路径在允许目录下（防止 path traversal）
- **并行写入**: 当前 SQLite 无并发控制，并行写入场景下可能锁竞争；未来迁移 PostgreSQL 时统一处理
