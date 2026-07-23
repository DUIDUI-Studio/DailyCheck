# DailyCheck MCP — Error Reference

Every response from the MCP server is **either** a top-level JSON-RPC error **or** an HTTP 200 success whose `result` may carry a tool error. Clients must check both layers.

```text
                  ┌─────────────────────────────────────┐
                  │ response.ok && response.status == 200 │
                  └─────────────────────────────────────┘
                                    │
                                    ▼
                       ┌────────────────────────┐
                       │ top-level "error" key? │
                       └────────────────────────┘
                          │                │
                         yes               no
                          │                │
                          ▼                ▼
              ┌─────────────────────┐    ┌──────────────────────────┐
              │ JSON-RPC envelope   │    │ result.isError === true ? │
              │ error (this file §1)│    └──────────────────────────┘
              └─────────────────────┘       │                │
                                          yes               no
                                           │                │
                                           ▼                ▼
                                  ┌─────────────────────┐  ┌──────────────┐
                                  │ Tool business error │  │ success      │
                                  │ (this file §2)      │  │ parse .text  │
                                  └─────────────────────┘  └──────────────┘
```

Two distinct envelopes; never assume one covers the other.

---

## §1 — JSON-RPC envelope errors

These appear at the **top level** of the response (`response["error"]`) and **never carry a `result`**. HTTP status may be 200, 400, or 406 depending on which constraint failed.

| HTTP | `error.code` | name | Typical `error.message` | When it happens | Client action |
|---:|---:|---|---|---|---|
| 400 | `-32700` | Parse error | `"Parse error: Expecting value: line 1 column 1 (char 0)"` | Body is not valid JSON | Do not retry without fixing the body |
| 406 | `-32600` | Invalid Request | `"Not Acceptable: Client must accept application/json"` | `Accept` header lacks `application/json` | Add `Accept: application/json` |
| 400 | `-32602` | Invalid params | `"Validation error: <pydantic detail>"` (long) | Missing `jsonrpc:"2.0"` field, or wrong type/structure | Fix request shape; do not retry as-is |
| 200 | `-32602` | Invalid params | `"Invalid request parameters"` (short) | Unknown method name (e.g. `tools/call` typo) | Check method spelling |
| 202 | — | (notification accepted) | empty body | Sent `method:"notifications/..."` | None, idempotent |
| 401 | — | (envelope — not JSON-RPC) | `{"error":"unauthorized","message":"Invalid or missing token"}` | Missing or wrong `Authorization` header | Add/correct token |

The 401 case **is not JSON-RPC** — it is the DailyCheck auth middleware responding before MCP even sees the request. `response["error"]` is **not** present; the body shape is the raw middleware response.

### Example: missing Accept

```http
POST /api/mcp/ HTTP/1.1
Authorization: Bearer <TOK>
Content-Type: application/json

{"jsonrpc":"2.0","id":1,"method":"tools/list"}
```

```http
HTTP/1.1 406 Not Acceptable
```
```json
{"jsonrpc":"2.0","id":"server-error","error":{"code":-32600,"message":"Not Acceptable: Client must accept application/json"}}
```

### Example: malformed JSON

```http
POST /api/mcp/ HTTP/1.1
Content-Type: application/json
Authorization: Bearer <TOK>
Accept: application/json

not json at all
```

```http
HTTP/1.1 400 Bad Request
```
```json
{"jsonrpc":"2.0","id":"server-error","error":{"code":-32700,"message":"Parse error: Expecting value: line 1 column 1 (char 0)"}}
```

### Example: missing `jsonrpc:2.0`

```json
{"id":1,"method":"tools/list"}
```

```http
HTTP/1.1 400 Bad Request
```
```json
{"jsonrpc":"2.0","id":"server-error","error":{"code":-32602,"message":"Validation error: 6 validation errors for JSONRPCMessage\nJSONRPCRequest.jsonrpc\n  Field required [type=missing, ...]\n..."}}
```

---

## §2 — Tool business errors

A tool call that reaches the handler but fails for business reasons returns **HTTP 200** with:

```json
{
  "jsonrpc":"2.0",
  "id":<your-id>,
  "result":{
    "content":[{"type":"text","text":"<error-json-as-string>"}],
    "isError":true
  }
}
```

The `text` field is itself a JSON string with `{"error": "<code>", "message": "<human>"}`. **Both layers must be parsed.**

### 2.1 — Tool error codes (validated against live server)

| `error.code` (string) | `error.message` | Triggered by | Client action |
|---|---|---|---|
| `unauthorized` | `invalid token` | Token reaches middleware, but its hash is not in `agent_tokens` | Rotate / re-grant token; do not retry |
| `unauthorized` | `DAILYCHECK_MCP_TOKEN not set` | Server admin misconfiguration | Server-side bug; not client-fixable |
| `not_found` | `not_found` (or item/warehouse-specific) | `item_id` does not exist in target warehouse, or `warehouse_code` not in registry | Verify IDs server-side via `items_list` / `warehouse_list` |
| `forbidden` | `forbidden_warehouse` | Token's warehouse allow-list excludes the requested `warehouse_code` | Use a token scoped to that warehouse, or remove the call |
| `forbidden` | `forbidden_path` | Token's read/write-path allow-list excludes this tool name (e.g., a read-only token calling `restock_create`) | Use a scoped read-only token for read tools |
| `validation_error` | `Input validation error: ...` — e.g. `'warehouse_code' is a required property`, `99 is not one of [7, 14, 30]`, `'foo' is not one of ['qty', 'value', 'turnover', 'name']`, `Item id 1 not found` | Fix arguments per detail; do not retry |
| `validation_error` | `days must be 7, 14, or 30` | Out-of-window days | Pass `7`, `14`, or `30` |
| `validation_error` | `sort_by must be one of: qty, value, turnover, name` | Bad sort key | One of the four enum values |
| `validation_error` | `warehouse_code required` | Empty warehouse_code string | Provide a value |
| `internal_error` | varies | Server-side bug or DB error | Retry with backoff; if persistent, server-side fix needed |

### 2.2 — "Unknown tool"

```json
{
  "jsonrpc":"2.0","id":1,
  "result":{
    "content":[{"type":"text","text":"Unknown tool: foo_bar"}],
    "isError":true
  }
}
```

The text is **not JSON here** — it is plain. Always check `isError` before JSON-parsing `text`.

### 2.3 — Real examples from the live server

**Validation error (missing required arg):**

```json
{"content":[{"type":"text","text":"Input validation error: 'warehouse_code' is a required property"}],"isError":true}
```

**Validation error (enum):**

```json
{"content":[{"type":"text","text":"Input validation error: 'foo' is not one of ['qty', 'value', 'turnover', 'name']"}],"isError":true}
```

**Forbidden path:**

```json
{"content":[{"type":"text","text":"{\"error\": \"forbidden\", \"message\": \"forbidden_path\"}"}],"isError":false}
```

> **Trap**: `isError` here is **false** even though the inner payload is an error. Always parse `text` and inspect its own `error` key.

**Not found (missing item):**

```json
{"content":[{"type":"text","text":"{\"error\": \"not_found\", \"message\": \"not_found\"}"}],"isError":false}
```

---

## §3 — Recommended client pattern

```pseudo
response = POST /api/mcp/...

if response.status == 401:
    raise Unauthorized("middleware rejected token")

body = response.json()

if "error" in body:
    # JSON-RPC envelope error (§1)
    raise JsonRpcError(body["error"])

if body.get("result", {}).get("isError"):
    payload = json.loads(body["result"]["content"][0]["text"])
    raise ToolError(payload["error"], payload["message"])

# success path: parse the data out of result.content[0].text
data = json.loads(body["result"]["content"][0]["text"])
```

Always treat **HTTP 200 with `isError=true`** as a failure. Treat **HTTP 401 as never-retry-without-fix**. Everything else (HTTP 400/406) is a request-shape bug, not a server problem.

---

## §4 — What this server does NOT return

- No rate-limit headers. The server has no explicit throttling; rely on upstream proxy / API gateway for that.
- No retry-after hints. Construct your own backoff for transport-level retries (5xx, timeouts).
- No pagination metadata. The `limit` argument caps results; paging is manual (`limit` + client-side offset filtering).
- No `request-id` echo. Pass your own `id` and the server does not log it back.
