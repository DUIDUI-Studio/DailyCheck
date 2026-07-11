# MCP Server Configuration Guide

This guide explains how to configure Claude Code and other agents to connect to the DailyCheck MCP server.

## Transports

The MCP server supports two transport modes:

| Transport | Use Case | Connection |
|-----------|----------|------------|
| `stdio` | Local Claude Code | Child process, stdin/stdout |
| `http` | Remote agents on other machines | HTTP/SSE on TCP port |

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
        "DAILYCHECK_MCP_TOKEN": "REDACTED_UNRESOLVED_MCP_TOKEN"
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

```bash
curl -H "Authorization: Bearer your_token" http://YOUR_HOST_IP:5100/health
# Expected: {"status":"ok"}
```

---

## Available Tools

The DailyCheck MCP server provides 13 tools:

### Inventory Tools

| Tool | Description |
|------|-------------|
| `items_list` | List all items in a warehouse, including quantity and safety stock |
| `items_detail` | Get full details for a single inventory item by ID |
| `movements_list` | List recent stock movements (outbound requests and stocktake adjustments) for a warehouse |

### Inbound Tools

| Tool | Description |
|------|-------------|
| `restock_create` | Create a restock (inbound) record |
| `restock_list` | List restock records for a warehouse |

### Outbound Tools

| Tool | Description |
|------|-------------|
| `outbound_create` | Create an outbound request: deduct stock + write movement (mirrors Flask `/outbound/submit`) |
| `outbound_list` | List outbound requests for a warehouse |
| `outbound_rollback` | Roll back an outbound request: return stock to warehouse |

### Consumption Tools

| Tool | Description |
|------|-------------|
| `warehouse_consumption` | Per-item consumption summary with rank, qty, daily avg, turnover rate, consume pct |
| `item_consumption` | Single item: 7d/30d/monthly totals + weekly breakdown + daily avg |

### Forecast Tools

| Tool | Description |
|------|-------------|
| `item_forecast` | Get consumption forecast for an item |

### Procurement Tools

| Tool | Description |
|------|-------------|
| `procurement_store` | Get procurement recommendations for a specific store |
| `procurement_hub` | Get procurement recommendations aggregated across all warehouses |

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
