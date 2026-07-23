# DailyCheck MCP — Integration Checklist

Run through this before going live. Each section ends with a self-test command; mark them off as you go.

---

## 0. Confirm server is reachable

```bash
curl -sS -m 5 http://<host>:5100/health \
  -H "Authorization: Bearer $DAILYCHECK_MCP_TOKEN"
```

Expected:
```json
{"status":"ok","checks":{"mcp_server":"ok","transport":"ok","db":"ok"}}
```

If you see `401` instead, the server is up but the env var `DAILYCHECK_MCP_TOKEN` on the server side does not match the one you're sending. **Talk to the server admin before continuing.**

---

## 1. Token scope is minimal

The token must allow **only** what this integration needs. Required flags at token creation:

```bash
flask create-agent-token <name> \
  --warehouses wh_001,wh_002        # only the warehouses this app reads
  --read-paths consumption,inventory # only the tool namespaces it calls
```

What each flag means:

| Flag | Effect of over-permissioning | Recommended default |
|---|---|---|
| `--warehouses` | Reads/writes data for any warehouse the user has access to | explicit list |
| `--read-paths` | Can call any tool | explicit list of namespace prefixes |
| `--write-paths` | Can mutate state | **omit unless required** |

Self-test:
```bash
DAILYCHECK_MCP_URL=http://<host>:5100 \
DAILYCHECK_MCP_TOKEN=$TOKEN \
./examples/python.py
```

Look at the error-section output. **Every call you don't expect to succeed must fail with `forbidden_path` or `forbidden_warehouse`.**

---

## 2. TLS termination in front of port 5100

The token travels in plaintext over HTTP. **In production, the only acceptable setup is HTTPS in front of 5100.**

Minimal Nginx config:

```nginx
server {
    listen 443 ssl http2;
    server_name mcp.example.com;

    ssl_certificate     /etc/letsencrypt/live/mcp.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/mcp.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:5100;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
        proxy_buffering off;  # required for streaming responses
    }
}
```

`proxy_buffering off` matters: some MCP responses can be SSE-style streams and Nginx will buffer them otherwise.

Self-test:
```bash
curl -sS https://mcp.example.com/health \
  -H "Authorization: Bearer $TOKEN"
# Same JSON as §0, but now TLS-wrapped.
```

---

## 3. Idempotency for write tools

Read tools (`items_list`, `warehouse_consumption`, `item_consumption`, …) are inherently idempotent. **Write tools are not.**

| Tool | Idempotent? | Risk if retried |
|---|---|---|
| `restock_create` | ❌ | duplicates inbound records → double-incremented stock |
| `outbound_create` | ❌ | duplicate outbound → double-decremented stock + possible negative inventory |
| `outbound_rollback` | partial — same `request_id` rolled back twice is a no-op the second time | OK to retry once |

For `restock_create` and `outbound_create`, the contract today offers **no idempotency-key field**. Until one is added, the only safe pattern is:

1. Track every outbound write by `(external_idempotency_key, dailycheck_request_id)` in your own DB.
2. Before sending, check your table for the key.
3. After the call lands, store the key + DailyCheck's response id.
4. Retry only the network layer, not the application layer.

Or, simpler: serialize all writes from this integration through a single-connection queue so retries don't fork.

Self-test: send the same `outbound_create` twice with `idempotency_key=ABC`:

```bash
PAYLOAD='{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"outbound_create","arguments":{"warehouse_code":"wh_001","item_id":42,"quantity":1,"reason":"smoke-test-ABC"}}}'

curl -sS -X POST "$DAILYCHECK_MCP_URL/api/mcp/" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/json" -H "Content-Type: application/json" \
  -d "$PAYLOAD"

# Send it again — observe that two rows are created.
```

Until idempotency keys exist, **do not retry application-level writes**.

---

## 4. Concurrency: one writer per warehouse

SQLite has a single-writer lock. Parallel writes from multiple integrations (or your own multi-threaded code) will serialize. Symptoms:

- `outbound_create` p99 rises from ~50 ms to seconds.
- Reads continue to work but contend for the writer.

Recommendations:

- **One process per warehouse** if you have multiple integrations writing concurrently.
- If you must write from many workers, batch them in your integration and flush serially through one HTTP client.
- Reads are mostly unaffected but add a retry-with-backoff if you see `sqlite3.OperationalError: database is locked`.

---

## 5. Health monitoring

Add this to your monitoring stack — DailyCheck does not push health; you poll.

```bash
# Watch for 401 (token revoked) or non-200 (server down)
while true; do
  status=$(curl -sS -o /dev/null -w '%{http_code}' \
    -m 5 http://<host>:5100/health \
    -H "Authorization: Bearer $TOKEN")
  echo "$(date -Iseconds)  $status"
  [ "$status" = "200" ] || alert
  sleep 60
done
```

Also periodically run `tools/list`. If the returned registry changes shape (a tool renamed, a field removed), that's a server upgrade — bump your client's schema version.

---

## 6. Rotating the token

When the token leaks, expires, or an integration is decommissioned:

```bash
# On the DailyCheck server
flask revoke-agent-token <name>           # sets revoked_at
flask create-agent-token <name>-v2 ...    # creates a new one, prints raw token once
```

Pass the new raw token to the integration **out-of-band** (1Password, Vault, etc.). Update the integration's secret store. Confirm with §0 self-test.

> The previous token cannot be re-enabled. Once revoked, only a new token (with new hash) can authenticate.

---

## 7. Time zone handling

DailyCheck stores `created_at` and `*_date` as **naive local strings** (no timezone suffix). The server runs in `Asia/Shanghai` by default. **Treat them as UTC+8 in your code** — do not assume the client/server agree.

For `warehouse_consumption` outputs:

```python
from datetime import datetime, timedelta, timezone

# Wrong: assumes UTC
recent = datetime.utcnow() - timedelta(days=7)

# Right: matches DailyCheck's reference clock
recent = datetime.now(timezone(timedelta(hours=8))) - timedelta(days=7)
```

---

## 8. Logging discipline

**Never log the token.** It must not appear in:

- Application logs (stdout, file logs, structured JSON).
- Error reports sent to Sentry / Bugsnag.
- HTTP access logs (use `Authorization: Bearer <REDACTED>` filter at the proxy).

If you write an MCP reverse proxy in front of the server, **strip the `Authorization` header before logging**. Nginx:

```nginx
log_format mcp_safe '$remote_addr [$time_iso8601] '
                    '"$request" $status $body_bytes_sent '
                    '"$http_x_forwarded_for"';

# Reference log_format WITHOUT $http_authorization anywhere.
```

---

## 9. Common pitfalls

| Symptom | Cause | Fix |
|---|---|---|
| HTTP 406 on every POST | `Accept` header missing `application/json` | Always set `Accept: application/json` |
| HTTP 401 on `/health` | Token not sent or wrong | Confirm `$DAILYCHECK_MCP_TOKEN` env on both sides |
| Tool call returns `{"error":"unauthorized","message":"invalid token"}` | Token in env doesn't match `agent_tokens` table hash | Regenerate via `flask create-agent-token`, copy raw, set env var to it |
| `result.isError=false` but body is still an error JSON | Tool implementation forgot to set the flag | Always parse `result.content[0].text` and check for `{"error":...}` |
| Empty response with HTTP 202 | You sent a `notifications/...` (correct behavior) | None — accept and move on |
| Server returns `field required: jsonrpc` | You forgot `"jsonrpc":"2.0"` | Always include it in every request |
| `mcpFetch` keeps timing out | Nginx `proxy_buffering on` is buffering SSE streams | Set `proxy_buffering off` in the location block |

---

## 10. Verification (re-run before any release)

After integrating, run the **full smoke matrix** in this order:

```bash
# 1. Health
curl -fsS http://<host>:5100/health -H "Authorization: Bearer $TOKEN"

# 2. Curl reference (zero-deps smoke)
DAILYCHECK_MCP_URL=http://<host>:5100 \
DAILYCHECK_MCP_TOKEN=$TOKEN \
./examples/curl.sh

# 3. Python reference
DAILYCHECK_MCP_URL=http://<host>:5100 \
DAILYCHECK_MCP_TOKEN=$TOKEN \
python3 examples/python.py

# 4. Node reference (Node ≥ 18, or Bun/Deno)
DAILYCHECK_MCP_URL=http://<host>:5100 \
DAILYCHECK_MCP_TOKEN=$TOKEN \
npx ts-node --transpile-only examples/node.ts
```

All three must produce the same `serverInfo.name` and end with `Done.` (Python) / `Done.` (Node) / `## Done.` (curl). If any fails, do not ship.

---

**Last verified**: tested against commit `fd8e87f` on `main`. The auth flow assumed here is the current **two-layer** model: middleware checks `Bearer == $DAILYCHECK_MCP_TOKEN` AND tool layer checks the same token's hash in `agent_tokens`. If the server changes this, all tests under §1 will fail loudly.