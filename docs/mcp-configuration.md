# MCP Server Configuration Guide

This guide explains how to configure Claude Code and other agents to connect to the DailyCheck MCP server.

## Transports

The CLI defaults to **stdio** (`python -m mcp_server` or `flask mcp`). TCP serving
requires explicit `--transport http`; the shipped systemd unit passes that flag,
`--host 0.0.0.0` and `--port 5100`. The CLI's HTTP bind defaults are
`127.0.0.1:5100` when host/port are omitted. MCP code changes need a process restart.

The MCP server supports two transport modes:

| Transport | Use Case | Connection |
|-----------|----------|------------|
| `stdio` | Local Claude Code | Child process, stdin/stdout |
| `http` | Remote agents on other machines | Stateless JSON StreamableHTTP `/api/mcp/` (recommended); legacy SSE `/sse` + `/messages/` also mounted |

---

## Local: Claude Code (stdio)

### 1. Create an Agent Token

Tokens are stored in `master.db`. You can create them via the web UI or the CLI.

**Web UI (recommended):**

1. Log in as admin at `http://your-host/login`
2. Navigate to **Agent Token** in the sidebar
3. Fill in the form and click **Create Token**
4. Copy the token from the prompt — shown only once

**CLI:**

```bash
# Replace --read-paths, --warehouses as needed
flask --app app create-agent-token my-agent \
  --read-paths "*" \
  --warehouses wh_001
```

The command prints the raw secret — copy it before closing the terminal.

**Options:**
- `--read-paths` — allowed read paths (comma-separated, `*` = all, default: `*`)
- `--write-paths` — allowed write paths (comma-separated, `*` = all, default: empty)
- `--warehouses` — allowed warehouse codes (comma-separated, empty = all)

### 2. Configure Claude Code

In Claude Code settings (`~/.claude/settings.json`), add the MCP server config with your token:

```json
{
  "mcpServers": {
    "dailycheck": {
      "command": "/Users/ericmr/Documents/GitHub/DailyCheck/mcp_server/.venv/bin/python",
      "args": ["-m", "mcp_server"],
      "env": {
        "DAILYCHECK_MCP_TOKEN": "your_token"
      }
    }
  }
}
```

### 3. Restart Claude Code

After updating the settings, restart your Claude Code session to pick up the new MCP server configuration. The server will automatically connect on next session start.

### 3. Verify Connection

You can verify the MCP server is connected by checking if the tools are available in your Claude Code session.

---

## Remote: Other Agents (HTTP/SSE)

For agents running on other machines, start the MCP server in HTTP mode and configure the agent to connect via HTTP/SSE.

### 1. Start the HTTP Server

On the machine where DailyCheck is installed:

```bash
DAILYCHECK_MCP_TOKEN=your_token \
  /Users/ericmr/Documents/GitHub/DailyCheck/mcp_server/.venv/bin/python \
  -m mcp_server \
  --transport http \
  --host 0.0.0.0 \
  --port 5100
```

- `--transport http` — enables HTTP/SSE mode
- `--host 0.0.0.0` — binds to all network interfaces (remote access)
- `--port 5100` — TCP port
- `DAILYCHECK_MCP_TOKEN` — shared secret for authentication

For production, run behind a reverse proxy (nginx) with HTTPS.

### 2. Configure Remote Agent

In the remote agent's `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "dailycheck": {
      "url": "http://YOUR_HOST_IP:5100/sse",
      "headers": {
        "Authorization": "Bearer your_token"
      }
    }
  }
}
```

Replace `YOUR_HOST_IP` with the IP or hostname of the machine running the HTTP server.

### 3. Health Check

`AuthMiddleware` protects **all HTTP routes, including `/health`**. Missing/invalid
credentials return HTTP 401. Authenticated checks return HTTP 200 when all checks
are `ok`, otherwise HTTP 503; the legacy top-level `status` stays `ok` even on
503, so monitoring must inspect the HTTP status and `checks.db`. The DB check runs
`SELECT 1` and does not prove migrations, warehouse integrity or a successful tool call.


```bash
curl -H "Authorization: Bearer your_token" http://YOUR_HOST_IP:5100/health
# Expected HTTP 200: {"status":"ok","checks":{"mcp_server":"ok","transport":"ok","db":"ok"}}
```

---

## Storefront read policy presets

Policy tokens use the same StreamableHTTP endpoint, `/api/mcp/`. On
**Agent Token 管理**, a platform administrator publishes a named preset by
selecting return fields for `inventory.read`, `orders.read`, or both.
Publishing the same name creates another immutable version. Each action
requires at least one safe field.

In **创建受限策略令牌**, select the exact preset version and each storefront.
The preview and binding details show the registered MCP tools, actions,
returned fields, explicit storefronts, and ownership scope. The preview
identifies the issuing administrator and shows the three empty legacy ACLs
and their effect if the application is rolled back. After revocation the
binding details show its administrator and recorded time.
Confirm within ten minutes from the same signed-in session. The confirmation
binds the exact operator, version, fields and storefronts. The new token is
shown once on a private, no-store page. The database stores its hash,
lookup prefix, immutable policy binding, and the issuing administrator and
timestamp. The bearer is kept out of the Flask session and the recoverable
legacy token field.

| Action | Existing MCP tool | Scope |
|---|---|---|
| `inventory.read` | `items_detail` | Exact authorized `warehouse_code` and positive `item_id`. Possible fields are `sku`, book quantity `current_stock`, `unit`, and source `updated_at`. |
| `orders.read` | `store_order_list` | Ordinary orders whose initiating `store_warehouse_code` is authorized. Optional exact warehouse, status, limit and offset filters. The policy controls order number, initiating store, status, order dates and `fulfillment`. |
| `production_consumption.read` | `production_consumption` | Separate storefront production reports and traceable source lines. Grant `summary` (day/week), `lines`, or both. The bound storefronts must be a nonempty subset of `wh_002`, `wh_003`, `wh_004`, `wh_006`. |

An omitted order warehouse filters to all explicitly bound storefronts.
Requests can use only declared input arguments. Disallowed actions, extra
arguments and foreign warehouse filters return tool errors. The order status
filter is declared and accepted only when the policy includes the status
field. Amounts, costs,
line details, tracking, people and notes are outside the policy vocabulary.

When selected, `fulfillment` returns authorized warehouse names and source
timestamps. Shipments use the batch's recorded shipment time. Receipts use
an applied receipt's recorded time and its same-order line's explicit
source. An applied receipt has no direct delivery-batch link. Known
out-of-scope facts are suppressed. A corroborated legacy single-source
outside warehouse suppresses otherwise unassigned facts on that order.
Mixed orders retain genuinely unassigned timestamps at order level, even
when the primary warehouse is outside the policy. Empty event arrays mean
no confirmed fact is available in this scope.

Publishing a replacement preset version leaves existing bindings intact.
To rotate a token, preview and issue a new binding, confirm its scope with
the client, then revoke the old row through **Agent Token 管理**. A replayed
confirmation or a second active policy token with the same name is refused.
The next request using a revoked token receives HTTP 401. In dry-run mode,
the policy revoke route returns HTTP 409 and leaves the token in its existing
state.

The reserved bearer namespace is accepted through StreamableHTTP only.
All three legacy ACL columns are empty, so earlier versions may advertise
their static catalog but cannot use the bearer to read business fields or
warehouse names. Revoke policy tokens before rolling back across the new
behavior and verify rejection on both versions. Master schema v9 is additive
and retains policy and revoke provenance during code rollback.

Existing source databases use rollback-journal mode for scoped access.
SQLite read-only WAL connections can update sidecars, so the guard checks
the database header before connecting. Keep the journal mode and registered
paths stable during requests. The pre-open check cannot synchronize a
concurrent operator change. Routine tool metadata may be appended to the
existing access log.

### Production consumption

Publish a policy preset with `production_consumption.read` on the existing
admin Token page. Preview the selected storefronts and view grants, then
confirm issuance or revoke through the existing page. Use the resulting
identity on `/api/mcp/`. The tool is available exclusively through a bound
policy. Existing credentials retain their previous tool access.

```json
{
  "start_date": "2026-11-16",
  "end_date": "2026-11-23",
  "view": "week",
  "warehouse_code": "wh_002",
  "limit": 50
}
```

- Dates are Beijing calendar dates, inclusive start and exclusive end.
  An explicit range spans 1–92 days. For `week`, both dates must be Mondays.
  The source writer stores server-local `YYYY-MM-DD HH:MM:SS`. This tool
  interprets those strings as Beijing time, regardless of the reader's
  process time zone. Verify the actual source time zone and timestamp format
  before enabling a production preset. UTC dev fixtures must seed explicit
  Beijing wall times. Unsupported timestamps selected by the lexical date
  window produce `source_unavailable`.
- Omit `warehouse_code` for all bound stores, still returned separately.
  Optional `item_id` selects the local item ID. A material identity is
  `warehouse_code` + `item_id`. `cross_store_rollup` remains
  `historical_mapping_unavailable`. Range `unit_status` is `snapshotted`,
  `partially_snapshotted`, or `historical_unit_unavailable`, based on the
  entire filtered range before pagination.
- `summary` rows contain the store, `period_start`, material, four line
  counts and four nullable quantities. `planned_count` counts all eligible
  lines. `manual_count`, `system_default_count`, and `unknown_count`
  partition the booked source counts. Each group has one deterministic
  `sample` with `run_id`, `line_id`, and `created_at`. Query `view=lines`
  with its warehouse and originating date range to retrieve the batch
  (`run_id`) or exact line (`run_id` + `line_id`).
- `unit_groups` lists comparable quantities within each summary row. A
  comparable group shares the snapshotted unit, SKU, optional canonical
  identity, conversion basis and item measurement revision. A change in
  the measurement basis can split two groups with the same display unit.
  `unknown_unit_count` and `unknown_unit_fraction` describe the lines that
  could not enter a comparable group. The old `unit` and four quantity
  fields are populated only for a fully known, homogeneous row. A source
  with no lines has a null quantity, while a booked zero remains zero.
- `lines` returns per-line `planned_qty`, `booked_qty` and `booked_source`.
  These are original numeric database values in the historical stock unit
  of that line, with no conversion. Explicit `planned_default=0` identifies
  manual input, including zero. `planned_default=1` is classified as
  `system_default` when its unique outbound link corroborates the item,
  batch, completed production outbound status and absence of rollback.
  The planned, booked and linked outbound quantities must also agree
  with the writer's defaulting invariant. Quantity equality alone does
  not establish the source. Remaining sources are `unknown`. Summing
  outbound or stock-movement quantities would duplicate these production facts.
- Snapshotted lines add `output_unit`, `unit`, `unit_status`, `snapshot_source`,
  `item_sku`, `canonical_item_id`, `bom_row_id`, `bom_qty_per_unit`,
  `conversion_mode`, `grams_per_unit`, `calculation_version` and
  `measurement_revision`. `grams_per_unit` is null for `stock_unit` mode.
  `snapshot_source=production_write` identifies a v1 writer snapshot. An
  older line has null snapshot metadata and `historical_unit_unavailable`.
- Legacy `outbound_list` exposes `unit`, `unit_status` and `snapshot_source`
  on automatic production rows. A unique verified association supplies the
  same snapshot. Legacy/ambiguous rows retain a null unit. The existing
  `factor` remains the document-line field. Reverse production stock from
  the production run. The standalone `outbound_rollback` returns
  `production_requires_run_reversal` for these rows.
- Summary sorts by warehouse, period start and local item ID. Lines sort by
  warehouse, created time, batch ID and line ID. Each call opens one read
  snapshot per warehouse in sequence. The page size is 1–200 (default 50)
  and the range may match at most 20,000 input lines across the warehouses.
  Complete groups are formed before pagination. `range_too_dense` asks the
  client to submit a smaller date or store range.
- `range_empty` refers to the effective filtered range and `page_empty`
  refers to the returned array. A true recorded zero remains numeric in a
  line. `truncated=true` supplies `next_cursor`; replay with the same
  parameters and identity to continue. The last page has a null cursor.
  Any change to the measured facts gives `stale_cursor`, and another server
  process or a restart gives `invalid_cursor`. In both cases restart at
  page one. No database snapshot is kept open between requests.
- An invalid or unauthorized request returns a protocol tool error.
  Missing, unreadable, incompatible-schema and WAL sources return
  `source_unavailable` for the entire request. Before any warehouse opens,
  the server uses file metadata to compare the targets of every registered
  warehouse. It rejects a requested store that shares a physical file with
  another, including hard links. External symlinks to distinct files work,
  and an absent file for a different warehouse does not block the request.
  Reads use existing-only SQLite policy connections. MCP calls append to
  the normal access log.

## Available Tools

The live server registers **29 tools**: 14 inventory/document/analysis tools,
13 canonical tools and two store-order reads. The external integration
[tools.json](integrations/dailycheck-mcp/tools.json) intentionally contains a
**16-tool subset** (the 14 base tools plus two store-order reads); it is not the
full live list and does not grant token permissions.

The generated [complete contracts](integrations/dailycheck-mcp/registry.md) and
[machine-readable ledger](integrations/dailycheck-mcp/registry.json) cover all 29
names, handlers, input declarations, actual minimum output shapes, ACL enforcement,
warehouse scope, compatibility and regression references. `get_tools()` and dispatch
are derived from `mcp_server/protocol/registry.py`. Bot execution remains independently
approved for 22 tools; seven canonical writes stay excluded.

After editing the authoritative registration, run from the repository root:

```bash
python -m scripts.mcp_contracts --write
python -m scripts.mcp_contracts --check
python -m pytest tests/test_mcp_registry.py
```

`--check` is read-only and exits nonzero on artifact/implementation/Bot metadata drift;
it is suitable for CI. Output contracts are internal verification metadata: responses
remain JSON text inside MCP `TextContent`, without SDK `outputSchema` or a new wrapper.

## Troubleshooting

### Token Issues

If you see authentication errors, verify:
- The `DAILYCHECK_MCP_TOKEN` is set correctly in settings.json or as an env var
- The token has not expired
- The token matches exactly on both server and client

### Connection Issues

If the MCP server doesn't connect:
- Verify the Python path is correct: `/Users/ericmr/Documents/GitHub/DailyCheck/mcp_server/.venv/bin/python`
- Ensure the virtual environment exists and has the required packages installed
- Check Claude Code logs for any connection errors

### HTTP Mode Only Accessible Locally

If remote agents cannot reach the HTTP server:
- Ensure the server is bound to `0.0.0.0` (not `127.0.0.1`)
- Check firewall rules allow inbound TCP on the configured port
- For internet access, put behind HTTPS via nginx/cloudflare
