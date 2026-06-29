# Agent MPC 接口 — 子项目 6 spec

**版本**：v1（2026-06-29）
**状态**：实施中。引用总览 PRD §2.3。
**前置依赖**：subproject 1（/forecast）、subproject 2（/procurement/store、/procurement/hub）。
**目标读者**：实施 agent。

---

## 0. 引用与本 spec 自决

引用 PRD：
- §2.3.2 接口清单
- §2.3.3 鉴权与跨仓库
- §2.3.4 可观测性
- §2.3.5 异常表
- §2.3.6 测试要点
- §3.2/3.3/3.5/3.6
- §8 锁定项

本 spec 自决项：

1. **Token 存储**：`master.db` 表 `agent_tokens`（spec §0 锁定）。**Hash 存储** token（不存明文），使用 werkzeug `generate_password_hash`。
2. **路径匹配**：精确前缀 + 通配符 `*`（spec §0 锁定）。实现一个 `path_matches(pattern, path) -> bool` 纯函数。
3. **限流 / 自动封禁**：PRD §2.3.4 末段写"不在 PRD 范围"。**不**实现。
4. **access.log 格式**：每条调用写一条 JSON 行（PRD 锁定）：`{ts, agent_token_id, path, status, duration_ms}`。**追加**到现有 `access.log` 文件（不新建）。
5. **/admin/mpc-usage 页面**：admin 页面列出 token 统计。**实现**（PRD 锁定 §2.3.4）。
6. **错误响应 request_id**：PRD §2.3.5 写"数据库错误 → 500 + access.log + 唯一 request_id"。**实现**。

---

## 1. 接口清单（PRD §2.3.2 锁定）

### 读（所有 token 可调）

| 路径 | 方法 | 实现来源 |
|---|---|---|
| `/api/v1/items` | GET | subproject 4 发布的新 items 列表（如果 subproject 4 已合）；否则直接查 master.db warehouses → 列出所有 items |
| `/api/v1/items/<id>` | GET | subproject 1 路径下落到 warehouse db |
| `/api/v1/movements` | GET | warehouse db `outbound_requests` + `stock_movements` 合并查询 |
| `/api/v1/forecast/item/<id>` | GET | **复用 subproject 1 的逻辑** |
| `/api/v1/procurement/store` | GET | **复用 subproject 2 的逻辑** |
| `/api/v1/procurement/hub` | GET | **复用 subproject 2 的逻辑** |
| `/api/v1/categories` | GET | warehouse db `categories` |
| `/api/v1/templates` | GET | master.db `publish_templates` (subproject 4 表) |
| `/api/v1/notifications/feed` | GET | subproject 3 `list_for_user` 但 **不限制 user_id**（spec 写"Agent 暂不消费，仅预留"）→ 返回空 |

### 写（必须在 token 白名单中显式列出路径前缀）

| 路径 | 方法 | 实现 |
|---|---|---|
| `/api/v1/restock` | POST | 调 `blueprints/restock.py::restock_submit` 逻辑 |
| `/api/v1/procurement/recompute` | POST | 调 `blueprints/procurement.py::mark_procurement_invalid` |
| `/api/v1/forecast/recompute` | POST | 调 `blueprints/forecast.py::forecast_recompute` |

---

## 2. 鉴权

### 2.1 Token 表 schema

```sql
CREATE TABLE agent_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    allowed_read_paths_json TEXT NOT NULL DEFAULT '[]',
    allowed_write_paths_json TEXT NOT NULL DEFAULT '[]',
    allowed_warehouse_codes_json TEXT NOT NULL DEFAULT '[]'  -- [] 表示全平台
);
```

### 2.2 鉴权逻辑

1. 解析 `Authorization: Bearer <token>` 头
2. 查表 + verify hash
3. 检查 `revoked_at IS NULL`
4. 检查 `path` 在 `allowed_read_paths` / `allowed_write_paths` 中（按 method 类型）
5. 检查 `warehouse_code`（来自 query string 或 body）在 `allowed_warehouse_codes` 中（[] 表示全过）

### 2.3 路径匹配

- 精确匹配：`/api/v1/items` 仅匹配 `/api/v1/items`
- 前缀匹配：`/api/v1/items` 匹配 `/api/v1/items/123`
- 通配符 `*`：`/api/v1/items/*` 匹配 `/api/v1/items/123` 和 `/api/v1/items/foo/bar`

### 2.4 跨仓库

- 每个 Agent 调用必须显式 `warehouse_code` 参数（query string 或 body）
- 缺失 → 400 + `{"error": "warehouse_code_required"}`
- 不在白名单 → 403

---

## 3. 异常（PRD §2.3.5）

| 场景 | 行为 |
|---|---|
| 缺 Authorization 头 | 401 |
| Token 不存在或 revoked | 401 + access.log |
| 路径越权 | 403 + access.log |
| 仓库越权 | 403 + access.log |
| warehouse_code 缺失 | 400 + `{"error": "warehouse_code_required"}` |
| 写动作参数不合法 | 400 + `{"error": "...", "field": "..."}` |
| 数据库错误 | 500 + access.log + `request_id` 返回 |

---

## 4. 可观测性

### 4.1 access.log

每条 MPC 调用写 JSON 行（追加到 `access.log`）：
```json
{"ts": "2026-06-29T...", "agent_token_id": 5, "path": "/api/v1/items", "method": "GET", "status": 200, "duration_ms": 12}
```

### 4.2 /admin/mpc-usage

列出：
- 每个 token 的累计调用次数、错误率、最近一次调用时间
- 路径 Top N
- 异常事件（连续 401/403、token 突然高频调用）— 简化：只显示错误率 > 50% 的 token

---

## 5. 测试矩阵

### 5.1 单元

- `path_matches(pattern, path)`:
  - 精确匹配
  - 前缀匹配
  - 通配符 `*`
  - 不匹配

### 5.2 集成

- 创建 token + 调用读路径 → 200
- token 撤销后调用 → 401
- 路径越权 → 403
- 仓库越权 → 403
- 缺 warehouse_code → 400
- 写路径 POST /api/v1/restock → 200 + 创建 restock_request
- /admin/mpc-usage 显示 token 统计

### 5.3 安全测试

- 篡改 Authorization 头 → 401
- SQL 注入（参数化查询必须）
- CSRF（API 不依赖 session cookie — 必须）

---

## 6. 文件清单

- 新增 `blueprints/agent_mpc.py`（blueprint + 路由 + 鉴权）
- 新增 `blueprints/agent_mpc_pure.py`（path_matches 纯函数）
- 修改 `db/__init__.py`（agent_tokens 表）
- 修改 `app.py`
- 新增 `templates/admin_mpc_usage.html`
- 新增 `tests/test_agent_mpc_pure.py`
- 新增 `tests/test_agent_mpc_route.py`
- 新增 `tests/test_agent_mpc_security.py`
- 修改 `templates/base.html`（admin nav 加"MPC 用量"）
- 修改 `templates/land.html`（admin 卡）

---

## 7. 验收门

1. `pytest -q` 全绿
2. access.log 验证有 MPC JSON 行
3. /admin/mpc-usage 页面正常
4. PRD §2.3.6 安全测试（SQL 注入、CSRF、token 篡改）通过
