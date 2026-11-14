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

## Strict read-only policy (opt-in, Issue #16)

The server can be started with an explicit, finite read-only policy. Without the
flag the server keeps its existing behaviour. The policy is loaded once at
process start; if it is missing, malformed, expired, or inconsistent with the
configured master, the process stops instead of falling back to standard mode.
HTTP and stdio use the same policy object.

```bash
python -m mcp_server --transport http \
  --host 127.0.0.1 --port 5100 \
  --readonly-policy /etc/dailycheck/mcp-readonly.json
```

The policy file contains only non-secret control fields (no bearer token):

```json
{
  "schema_version": 1,
  "token_id": 42,
  "expires_at": "2026-11-15T00:00:00Z",
  "master_path": "/var/lib/dailycheck/db/master.db",
  "warehouse_root": "/var/lib/dailycheck/db/warehouses",
  "warehouses": {
    "<approved-warehouse-code>": "/var/lib/dailycheck/db/warehouses/<approved-file>.db"
  }
}
```

Rules enforced by the server:

- `token_id` must be one **existing** `agent_tokens` row; the row's plaintext
  bearer token is still presented by the caller and checked against the table
  (`prefix` + `pbkdf2` + `revoked_at IS NULL`). No raw token is stored in the
  policy file or printed.
- The token must have an explicit, non-empty `allowed_warehouses` list that is a
  subset of the policy's `warehouses` map, and an empty `allowed_write_paths`.
  A policy never widens a token's ACL.
- `expires_at` is an explicit UTC deadline. It is checked at load and on every
  request/tool call; revocation is re-checked for each new operation. There is no
  auto-renewal.
- Only `warehouse_list`, `items_list`, and `items_detail` are listed and
  dispatchable. Every other tool — including writes, canonical/order/WMS reads,
  and future/unknown names — is rejected before any handler or business database
  open. Arguments must match the exact declared key set and types; SQL, paths,
  data roots, `profile`, and `read_only` overrides are not accepted.
- Reads use `mode=ro` plus `PRAGMA query_only` plus a SQLite authorizer that
  denies writes, DDL, `ATTACH`/`DETACH`, and extension loading. The warehouse
  metadata path must resolve to the exact approved file for that code; missing
  databases or incompatible schemas fail closed and are never migrated or created.
- `items_list` / `items_detail` keep the existing wire fields, including
  `unit_cost`.
- Audit reuses the metadata-only `access.log`. A tool rejected by the strict
  allowlist is recorded there with the fixed marker `restricted`, and the MCP wire
  response uses fixed safe text (`Unknown tool`); caller-supplied names and
  argument details are not copied into either channel. Note: MCP SDK 1.30.0
  itself may write a rejected tool name (`Tool '<name>' not listed...`) and
  pydantic `input_value` details for malformed arguments to the operator-side
  stderr log. That stderr channel is not scrubbed by the metadata audit. Keep
  operator diagnostics exposure under the normal production log policy; this
  repository does not claim an implemented log filter, zero logging, or a
  verified production logging configuration. Audit remains best-effort, not a
  fail-closed durable receipt.
- Bearer secrets belong only to the protected authentication transport/env and
  are not accepted in tool arguments or names, nor written to audit records or
  reports.
- A read-only WAL read may create or update the `-shm` sidecar; the main database
  and `-wal` remain unchanged. `immutable=1` is intentionally not used.
- The trust boundary is the operator's configuration, code, and filesystem. The
  policy does not claim protection from an administrator who can rewrite those.

This policy is **off by default and does not activate production access**. Secure
provisioning of the bearer token, network entry, deployment, and real receiver
acceptance remain Owner/Prod decisions under `docs/agent-framework/workflow.md`.

## One-shot Lucy read runner (Issue #16, not activated)

`scripts/mcp_lucy_read_once.py` is the finite receiving client that consumes the
strict policy above. It has exactly two subcommands and is not a gateway,
query client, credential service, or job queue.

- `consume` runs one operator-owned, hash-pinned plan on the host. The plan
  binds the repository and full git SHA, deployment anchor, one warehouse and
  its exact file, an explicit trusted deployment state directory, the
  active-business PID/start and master file binding, one item, and explicit `field_consent` for the complete
  single-item shape **including `unit_cost`**. Missing/unknown fields, a stale
  UTC deadline, a changed hash/correlation, unsafe paths, or denied consent fail
  before any identity or read.
- One durable non-secret claim receipt per correlation is created atomically and
  fsynced before any credential exists. The same plan can never run twice; a
  claimed attempt is never reset. An interrupted attempt stays
  `reconcile_required` for the operator and is not retried automatically.
- Preflight takes a **shared** lock on the existing deployment fence (the file
  must already exist; the runner never creates, truncates, or clears a lock),
  then verifies the source HEAD and every tracked Python file against its Git blob
  (including the runner and blueprint import side effects, even if marked
  assume-unchanged), refuses untracked Python source before fresh imports, and
  checks the active-business PID/start/master-fd binding. Ignored Python files
  are also refused outside the trusted interpreter's `.venv`/`node_modules`.
  `deploy_anchor` now contains exactly `state_dir` and `source_sha`; no separate
  `fence` locator is accepted. Fixed `state.json` and `.attempt.lock` filenames
  bind both artifacts to that same canonical, trusted owner/mode directory,
  independent of the source and DB roots. The runner checks lock fstat/lstat
  inode equality and private owner/mode after opening; it never creates or
  chmods the deployment lock. It directly consumes unchanged `deploy_state.py`
  releases: version 1, settled lease/native state, no failed last attempt,
  `deployed.sha/pr/at`, and validates the referenced successful outcome's
  SHA/PR/generation/deployment identity when present. An init-only baseline
  without a release PR, malformed/missing/unknown state, or an authored
  `source_sha/release_id/deployment_root` substitute fails closed. These are
  deployment facts, never proof of Owner approval or a live process identity.
  The first pilot requires an explicitly approved regular-file warehouse;
  external symlink mappings such as `wh_002` remain fail-closed.
  Production consume is Linux-only; a non-Linux synthetic plan uses a clearly
  labeled surrogate and Linux CI covers the real `/proc` path.
- Only after preflight does the runner create one temporary `agent_tokens` row:
  correlation-derived name, random secret held in memory, exactly
  `GET /api/v1/warehouses` and `GET /api/v1/items`, no write paths, one warehouse,
  and a rate limit. Existing identities are untouched; no plaintext is stored
  or returned. The non-secret policy file is 0600 in a 0700 temp directory, and
  the child environment receives only this job's token plus a fresh per-job
  `DAILYCHECK_SECRET_KEY` (no existing web/MCP secret is read).
- The child is the real MCP SDK stdio client. Calls are fixed:
  `initialize -> tools/list (exactly 3) -> warehouse_list (exactly 1) ->
  items_detail (exactly 1)`. The result carries one item, the MCP tool/argument
  metadata, actual child pid/start/returncode/wait evidence, and cleanup proof.
  Consume custody is held on a real `finally` path, including KeyboardInterrupt,
  SDK cancellation, unexpected exceptions and ledger failure after issuance.
  Revocation (commit + re-read) precedes positive child settlement, own policy
  temp removal and fence release last; the producer's exclusive flock remains
  blocked through this cleanup. No cancellation or incomplete cleanup returns
  business success or grants another read. Any cleanup failure returns a non-zero
  `reconcile_required` result with no business success.
- The existing token schema has no universal expiry. This job bounds its
  identity with the strict policy deadline and verified revocation;
  uncatchable death (SIGKILL/host crash) cannot run `finally` and still needs
  later explicitly authorized operator reconciliation of any surviving row,
  including a receipt-recorded token id. There is no guaranteed global TTL,
  background sweeper, third command or automatic retry.
- `relay` (Mac) reads a protected relay request, requires the supplied SSH
  public key to match the trusted fingerprint, fixes the host alias to
  `wdg_vps`, resolves the alias destination/port locally and refuses a mismatch,
  writes only a task-local 0600 known_hosts, and runs one fixed remote `consume`
  command with `StrictHostKeyChecking=yes`, `UpdateHostKeys=no`,
  `BatchMode=yes`, no forwarding/X11/PTY/ControlMaster, and no fallback after a
  host-key denial, EPERM, or timeout. Key provenance remains an Owner/Prod
  prerequisite; the runner never runs `ssh-keyscan` or changes global SSH config.
- A plan or request is a protected operator artifact, not self-authenticating
  Owner approval. The concrete production fence/anchor/data/identity values are
  still unknown and must be supplied and revalidated by the future authorized
  Prod operator. Business results go only to the authorized receiver/owner,
  never to a public PR or chat channel.

## Available Tools

The live server registers **32 tools**: 14 inventory/document/analysis tools,
13 canonical tools, two store-order reads and three external WMS reads. The
external integration [tools.json](integrations/dailycheck-mcp/tools.json)
intentionally contains a **19-tool subset**; it is not the full live list and
does not grant token permissions.

The generated [complete contracts](integrations/dailycheck-mcp/registry.md) and
[machine-readable ledger](integrations/dailycheck-mcp/registry.json) cover all 32
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
