# DailyCheck MCP Server — Integration Guide

External systems can consume DailyCheck inventory data via the **Model Context Protocol (MCP)** over streamable-HTTP. The server speaks JSON-RPC 2.0; **no LLM is required** — any HTTP client (curl, `requests`, `fetch`, etc.) can call it.

## 1. Server identity

| Field | Value |
|---|---|
| Server name | `dailycheck-mcp` |
| Protocol version | `2025-03-26` (MCP standard) |
| Capabilities | `tools` (no `resources`, no `prompts`) |
| Transport | streamable-HTTP (POST a single endpoint, no SSE session required) |
| Server version | see `serverInfo.version` returned by `initialize` |

## 2. Endpoint

```
POST  http://<host>:5100/api/mcp/
GET   http://<host>:5100/health
```

`/health` returns `{"status":"ok","checks":{...}}` when the server can reach `master.db`. It is **not** load-balanced by MCP clients and exists for monitoring.

For legacy SSE (not recommended for new integrations):

```
GET   http://<host>:5100/sse              # open long-lived event stream → server returns session_id
POST  http://<host>:5100/messages/?session_id=<id>   # send tool calls back
```

SSE mode requires a two-step handshake. Use streamable-HTTP unless you already have an SSE client.

### Required headers

| Header | Value | Required |
|---|---|---|
| `Authorization` | `Bearer <TOKEN>` | ✅ always |
| `Content-Type` | `application/json` | ✅ on POST |
| `Accept` | must contain `application/json` | ✅ on POST |

Both `Accept` and `Content-Type` are mandatory on POST. Missing `Accept` returns **HTTP 406** with a JSON-RPC `-32600` error.

### Quick connectivity check

```bash
curl -s http://<host>:5100/health \
  -H "Authorization: Bearer $DAILYCHECK_MCP_TOKEN"
# → {"status":"ok","checks":{"mcp_server":"ok","transport":"ok","db":"ok"}}
```

## 3. Authentication

DailyCheck uses **two-layer auth**, both required:

1. **Transport middleware** reads `Bearer <TOKEN>` from the `Authorization` header and compares it byte-for-byte against the `DAILYCHECK_MCP_TOKEN` environment variable on the server. Mismatch → **HTTP 401** `{"error":"unauthorized"}` — no further processing.
2. **Tool-layer auth** (for `tools/call`) looks up the same token in the `agent_tokens` table (PBKDF2-SHA256 hash). If the hash is not found → `isError=true` with `{"error":"unauthorized","message":"invalid token"}`.

In practice this means **the token is one value**, configured identically on both sides. The server admin must:

```bash
# Generate a token once (UI: Admin → Agent Tokens)
flask create-agent-token external-reader \
  --warehouses wh_001 \
  --read-paths consumption,inventory

# Set the env var so the middleware accepts it
export DAILYCHECK_MCP_TOKEN="<raw-token-from-create>"
```

| Failure | HTTP | response shape |
|---|---|---|
| Missing `Authorization` | 401 | `{"error":"unauthorized","message":"Invalid or missing token"}` |
| Wrong token (middleware) | 401 | same as above |
| Right token, not in `agent_tokens` | 200 | `result.content` is JSON `{"error":"unauthorized","message":"invalid token"}` with `isError:false` |

**The token is a shared secret.** It must be passed via TLS in production and never logged. Rotate via [integration-checklist.md](./integration-checklist.md#rotating-the-token).

## 4. Tool registry (16 tools)

Fetch the full live registry: `tools/list`. Snapshot included in [tools.json](./tools.json).

Reading / analysis:

| Tool | Purpose |
|---|---|
| `warehouse_list` | List all warehouses (code + name) |
| `items_list` | Per-warehouse item inventory |
| `items_detail` | Single item info |
| `movements_list` | Recent inventory movements |
| `item_forecast` | Single-item forecast (next N days) |
| `procurement_store` | Per-store procurement suggestions |
| `procurement_hub` | Hub-level procurement rollup |
| `warehouse_consumption` | **Per-item consumption summary** (rank, qty, daily avg, **turnover rate**) + warehouse-level 30d financial turnover |
| `item_consumption` | Single-item consumption detail (7d / 14d / 30d / monthly) — opt-in `include_turnover` adds stocktake-anchored COGS turnover |
| `store_order_list` | Store orders (store ordering / DC transfer) visible to this token by **participating warehouse** — filter by `warehouse_code` (store side or any participating warehouse) / `status`, paginated with `limit` / `offset` |
| `store_order_detail` | One store order: header + line items (`canonical_name`, `quantity`, `fulfilled_quantity`, `dc_item_id`, `source_warehouse_code`) + deliveries (`warehouse_code`) + receipts + status history |

**Store-order ACL expansion (explicit).** An order is visible when the token is
allowed on its store warehouse or on **any participating warehouse** (a line's
source warehouse, header DC as legacy fallback). A token allowed on any
participating warehouse can read the **full order** — including line items
fulfilled by other warehouses, their amounts and the receipt history. This is
intentional: one cross-warehouse order is one document for both sides.

Write:

| Tool | Purpose |
|---|---|
| `restock_create` | Create a restock (inbound) request |
| `restock_list` | List recent restocks |
| `outbound_create` | Create an outbound request (decrements stock) |
| `outbound_list` | List recent outbounds |
| `outbound_rollback` | Reverse a previous outbound |

For warehouse-level dashboards (this guide's motivating use case), `warehouse_consumption` is the primary entry point. See [examples/python.py](./examples/python.py) for the canonical call sequence.

### Which "list" tool to use?

Three tools expose a list-style view of stock activity. They overlap but answer different questions:

| Question | Tool | Why |
|---|---|---|
| "Show me recent activity in this warehouse" (any kind) | `movements_list` | **Union view** — merges outbound + stock_movements, with `type` field |
| "List outbound requests, including rolled-back ones" | `outbound_list` | Has `status` / `rolled_back` fields that `movements_list` strips |
| "List restock (inbound) records" | `restock_list` | Reads `restock_requests` (inbound orders), newest first — carries `quantity` / `status` |
| "Show me what items exist + their stock" | `items_list` | Snapshot, no activity |
| "Show me what this item's recent activity is" | `items_detail` | Per-item lookup, no activity |
| "Show me what categories consume the most" | `warehouse_consumption` | Has `consume_pct` + `daily_avg` + `turnover_rate` + warehouse-level turnover |
| "Deep-dive on a single item's consumption" | `item_consumption` | 7/14/30/monthly windows + weekly breakdown + opt-in inventory turnover |

Field name conventions across all tools:

- **Stock quantity field**: always `current_stock` (in `items_list`, `items_detail`, `warehouse_consumption.items[]`, `item_consumption`)
- **Category field**: always `category_name`
- ⚠️ **Exception**: `procurement_store` / `procurement_hub` use `current_qty` for historical reasons (their outputs come from the procurement blueprint which also drives Flask templates + a DB cache column). Don't try to share a single accessor between procurement and consumption tools.

## 5. Call sequence

Every streamable-HTTP integration follows three steps:

```text
1. initialize  (recommended, but not strictly required for tools/list & tools/call in this server)
2. tools/list   (optional — if you already have the schema snapshot, skip)
3. tools/call   (your real workload)
```

Step 1 exists for protocol conformance; **all three methods** work without a session in streamable-HTTP mode (unlike SSE).

### Initialize

```bash
curl -s -X POST http://<host>:5100/api/mcp/ \
  -H "Authorization: Bearer $DAILYCHECK_MCP_TOKEN" \
  -H "Accept: application/json" -H "Content-Type: application/json" \
  -d '{
    "jsonrpc":"2.0","id":1,"method":"initialize",
    "params":{
      "protocolVersion":"2025-03-26",
      "capabilities":{},
      "clientInfo":{"name":"my-app","version":"1.0.0"}
    }
  }'
```

Response:

```json
{"jsonrpc":"2.0","id":1,"result":{
  "protocolVersion":"2025-03-26",
  "capabilities":{"experimental":{},"tools":{"listChanged":false}},
  "serverInfo":{"name":"dailycheck-mcp","version":"1.28.1"}
}}
```

### tools/list

```bash
curl -s -X POST http://<host>:5100/api/mcp/ \
  -H "Authorization: Bearer $DAILYCHECK_MCP_TOKEN" \
  -H "Accept: application/json" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
```

Returns the full schema registry. Cache it locally — tool definitions change only with server releases.

### tools/call — `warehouse_consumption`

```bash
curl -s -X POST http://<host>:5100/api/mcp/ \
  -H "Authorization: Bearer $DAILYCHECK_MCP_TOKEN" \
  -H "Accept: application/json" -H "Content-Type: application/json" \
  -d '{
    "jsonrpc":"2.0","id":3,"method":"tools/call",
    "params":{
      "name":"warehouse_consumption",
      "arguments":{"warehouse_code":"wh_001","days":30,"sort_by":"turnover","limit":20}
    }
  }'
```

**Input schema** (from [tools.json](./tools.json)):

| Argument | Type | Required | Default | Constraint |
|---|---|---|---|---|
| `warehouse_code` | string | ✅ | — | must exist; must be in token's warehouse allow-list |
| `days` | integer | — | `7` | enum: `7`, `14`, or `30` |
| `sort_by` | string | — | `"qty"` | enum: `qty`, `value`, `turnover`, `name` |
| `limit` | integer | — | `100` | max `200` |

**Output**: a single object with two top-level fields:

| Field | Type | Meaning |
|---|---|---|
| `items` | array | per-item rows (see below) |
| `warehouse_turnover` | object | warehouse-level financial turnover (see below) |

`items[]` fields:

| Field | Type | Meaning |
|---|---|---|
| `rank` | int | sort order (1-indexed) |
| `item_id` | int | DailyCheck internal ID |
| `sku` | string | SKU code |
| `name` | string | item display name |
| `category_name` | string | category name (fixed enum, see below) |
| `unit` | string | e.g. `g`, `瓶`, `包` |
| `current_stock` | float | current inventory |
| `safety_stock` | float | configured safety threshold |
| `consume_qty` | float | total consumed in the last N days |
| `consume_days` | int | distinct days with consumption in window |
| `daily_avg` | float | `consume_qty / consume_days` |
| `turnover_rate` | float | **`consume_qty / current_stock`** — how many times stock has been consumed |
| `consume_pct` | float | percent share of total warehouse consumption |
| `first_date` / `last_date` | string? | earliest / latest consumption timestamp; `null` if no consumption |

`warehouse_turnover` fields (always 30-day window, computed regardless of `days`/`sort_by`):

| Field | Type | Meaning |
|---|---|---|
| `window_days` | int | always `30` |
| `warehouse_cogs_value` | float | Σ per-item `consume_qty × unit_cost` (window 30d) |
| `warehouse_avg_inventory_value` | float | Σ per-item `avg_inventory × unit_cost` (window 30d) |
| `turnover_value` | float \| null | `warehouse_cogs_value / warehouse_avg_inventory_value` |
| `items_with_turnover` | int | items that contributed (have ≥2 stocktake anchors in window) |
| `items_total` | int | total items in warehouse |
| `data_quality` | string | `none` (no items contributed) / `medium` (partial) / `high` (all contributed) |
| `method` | string | always `"stocktake_weighted_sum"` |

> ⚠️ **Breaking change** (vs prior version): this tool used to return a flat
> array. It now returns `{items: [...], warehouse_turnover: {...}}`. Update
> callers from `result[0]["sku"]` to `result["items"][0]["sku"]`.

**Turnover-rate semantics (important)**:

- `turnover_rate = 0` when `current_stock = 0` — item not stocked; do **not** interpret as "zero turnover"
- high `turnover_rate` + low `current_stock` ⇒ restock soon
- low `turnover_rate` + high `current_stock` ⇒ overstock risk
- For "what's about to stock-out" → `sort_by=turnover, limit=20, days=7`
- For "what's slow-moving" → `sort_by=turnover, limit=20, days=30` then read **from the bottom of the sorted list** or sort ascending by re-computing

**Warehouse-level turnover semantics**:

- `turnover_value` is the financial-style aggregate (`Σ COGS / Σ avg_inventory_value` over 30d)
- Different scale from per-item `turnover_rate`: warehouse value is "how many turns per 30d", per-item is "window qty / current stock"
- `unit_cost` is current, not historical → price changes within the window distort COGS
- `data_quality="none"` means no item has stocktake anchors → number not meaningful

**Categories are fixed** (DailyCheck's 9-category system, immutable): 包材, 辅料, 调味酱, 调味酱分, 风味奶浆, 乳制品, 生产消耗品, 生产工具, 冰激凌成品.

## 6. Error handling

See [errors.md](./errors.md) for the full catalog. Two distinct envelopes exist:

- **JSON-RPC envelope errors** (protocol-level: bad JSON, missing Accept, wrong method): appear at the **top level** of the response with `error.code` ∈ {-32700, -32600, -32601, -32602, -32603}.
- **Tool errors** (validation failures, unauthorized, not_found, etc.): appear inside `result.content[0].text` as a **JSON string** with `result.isError=true`. HTTP is still 200.

Always check **both**: HTTP status, JSON-RPC top-level error, AND `result.isError`.

## 7. Operational guarantees

- **No caching layer**: every call hits SQLite on disk. p50 ~50 ms, p99 ~500 ms on the reference dataset (~3 warehouses × 67 items × ~200 movements each).
- **No write-then-read consistency gap**: all inventory writes commit to `master.db` or `db/warehouses/<code>.db` **before** the call returns.
- **Idempotency**: read tools (`items_list`, `warehouse_consumption`, …) are safe to retry. **Write tools** (`restock_create`, `outbound_create`) are idempotent only with the paired `doc_no`/`line_no` contract described below — see [integration-checklist.md](./integration-checklist.md#idempotency) before issuing them.
- **Concurrency**: the SQLite connection pool is single-writer; high-frequency parallel writes from multiple integrations can serialize. Use one process per warehouse and avoid burst writes.
- **Time zone**: all `*_date` fields and `created_at` strings are in the server's local time (Asia/Shanghai). No timezone suffix; treat as naive datetimes in client code.

## 8. Examples

| File | Use it for |
|---|---|
| [examples/curl.sh](./examples/curl.sh) | One-off `curl` smoke tests, no dependencies |
| [examples/python.py](./examples/python.py) | Reference client; usable copy-paste in any Python project |
| [examples/node.ts](./examples/node.ts) | TypeScript reference using only `fetch` (no SDK) |

## 9. Production checklist

Before going live, work through [integration-checklist.md](./integration-checklist.md). The two non-negotiables are:

1. **TLS** in front of port 5100 (Nginx/Caddy reverse proxy). Tokens in plaintext headers are not acceptable over public networks.
2. **Token scoping** (`--warehouses` + `--read-paths`) granted per integration. Never reuse a single admin-wide token.

---

**Last verified**: tested against commit `fd8e87f` on `main` against the local Docker dev container. See [integration-checklist.md § Verification](./integration-checklist.md#verification) for how to re-run.

## Document line writes (Issue #87 / warehouse v7)

`restock_create` and `outbound_create` retain required `item_id`, `quantity`,
`warehouse_code` and optional nullable `reason`. Both quantities accept numbers,
including `0.25`. New optional arguments are `doc_no`, `line_no`, and `factor`
(default `1`). New service arguments are keyword-only after `reason`, so existing
positional Python callers continue to work.

| Argument | Contract |
|---|---|
| `item_id` | Positive integer; bool, float and string rejected |
| `quantity`, `factor` | Finite positive JSON numbers; bool, string, explicit null, NaN/Infinity rejected |
| `doc_no` | Opaque case-sensitive string, 1–128 characters / at most 512 UTF-8 bytes |
| `line_no` | Opaque case-sensitive string, 1–64 characters / at most 256 UTF-8 bytes; `01` differs from `1` |
| Identity pair | Omit both or provide both; explicit null, controls and edge whitespace rejected; no automatic trim/casefold |

Structured callers use `<source>:<external_doc_no>` (for example
`juleng:JL-20261030-001`); source convention is `[a-z][a-z0-9_-]{0,31}` and the
external portion may contain more colons. This is a caller convention: the server
does **not** reject a doc_no without a prefix. The complete opaque string is the
identity, independent of token rotation and item ID. Warehouses and directions
have separate key spaces.

```json
{"name":"outbound_create","arguments":{"item_id":43,"quantity":1,"factor":10,"warehouse_code":"wh_000","doc_no":"juleng:JL-20261030-001","line_no":"01","reason":"source mirror"}}
```

Effective base quantity is `quantity * factor`, rounded once to two decimal places
with Decimal HALF_EVEN. `1 * 10` deducts 10, `.125 * 10` deducts 1.25, `1.005 * 1`
becomes 1.00, and `.001 * 1` is rejected as zero. Nonfinite/overflowed results and
values that lose cents on SQLite REAL round-trip are rejected. Send original
source quantity with its factor, or an already converted base quantity with
factor=1. Web auxiliary-unit division is a different contract.

Create returns existing `id` (integer), `item_id`, effective `quantity` and
`warehouse_code`, plus `doc_no`, `line_no`, numeric `input_quantity`/`factor`,
`status`, `rolled_back` (null for restock), `replayed`, `applied`. First write is
`replayed=false, applied=true`; exact replay returns the original ID with
`replayed=true, applied=false` and performs no stock/movement writes. Numeric
representations `1` and `1.0` are equivalent. Different item, original quantity or
factor conflicts even if products match; changed reason does not overwrite the
first reason. Without a key, every call creates a new row, even with equal reason.
Factor can be supplied without a key, but such calls cannot safely auto-retry.

Rollback returns stored effective quantity once; replay after rollback stays
rolled back. Web deletion permanently consumes the key and subsequent create
returns `document_line_deleted`, never recreates the row. List envelopes, total,
LEFT JOIN orphan rows and `created_at DESC,id DESC` order are unchanged (#72/#74).
Each list row gains `doc_no`, `line_no`, `input_quantity` (canonical decimal string
or null), `factor` (number); `quantity` remains effective base units. Historical
and Web-created rows keep NULL identity/input and factor=1; reasons are never
backfilled as identities.

### Warehouse migration and sync cutover

Web/init and the MCP first warehouse connection share a validated v7 migration;
no Web warm-up is required. Existing files open in mode=rw. Dedicated single-target
entry: `python -m db.warehouse_migrate --db /absolute/path/warehouse.db status|up`
(or `down --target 6`, which deliberately refuses destructive downgrade).
`--db` is mandatory; missing files, master DB, wrong schemas and future versions
are rejected. Do **not** use `flask migrate --db <warehouse> up`: that uses the
master registry (still v6). `db/migrate.py` is the legacy inventory-copy path.

Realpath-based flock covers backup, baseline through v6, and registered v7;
symlink aliases share the lock, green copies have their own. Every actual stage
gets a SQLite backup API hot backup, checked with integrity_check, under adjacent
`backups/migrations/`; errors block migration. v7 adds columns, partial unique
indexes, permanent `document_write_keys` and version stamp in one transaction.
Legacy v0–5 baseline may retain partial DDL and can be retried; it never stamps v7.
Current v7 is a read-only version fast path without repeated backups/ALTERs.

Prod owns cutover and production verification. Pause old writers, save a stable
source cursor/ID set and reconcile historical set H against new set N: H never
calls new create; only verified N gets structured keys. Cover overlap, same-second
rows, multiple pages and late arrivals; reason alone is insufficient proof.
Quarantine unknown outcomes and do not advance the cursor across them. Keep H
and checkpoints durable; conflicts/rollback/deleted results require reconciliation,
not new identities. No client synchronizer or production scripts are introduced.

Preferred rollback stops new sync calls and rolls back code while preserving v7
columns/indexes/journal. A verified pre-v7 backup restore requires stopped writers,
closed connections and no unaccounted new business writes, with matching restored
source checkpoint. Never overwrite an online DB or delete journal keys to retry.
Prod independently exercises green-copy migration, WAL backup/restore and its
standard observation window; green migration does not upgrade the real database.
