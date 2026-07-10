# MCP Server Configuration Guide

This guide explains how to configure Claude Code to connect to the DailyCheck MCP server.

## Prerequisites

- Claude Code installed and configured
- DailyCheck project cloned locally
- Agent token from the DailyCheck system

## Configuration Steps

### 1. Set Up the Token

The MCP server requires authentication via the `DAILYCHECK_MCP_TOKEN` environment variable. This token is your agent's bearer token for authentication.

In your Claude Code settings (`~/.claude/settings.json`), update the `mcpServers.dailycheck.env.DAILYCHECK_MCP_TOKEN` field with your actual agent token:

```json
{
  "mcpServers": {
    "dailycheck": {
      "command": "/Users/ericmr/Documents/GitHub/DailyCheck/mcp_server/.venv/bin/python",
      "args": ["-m", "mcp_server"],
      "env": {
        "DAILYCHECK_MCP_TOKEN": "your_actual_agent_token_here"
      }
    }
  }
}
```

### 2. Restart Claude Code

After updating the settings, restart your Claude Code session to pick up the new MCP server configuration. The server will automatically connect on next session start.

### 3. Verify Connection

You can verify the MCP server is connected by checking if the tools are available in your Claude Code session.

## Available Tools

The DailyCheck MCP server provides 9 tools:

### Inventory Tools

| Tool | Description |
|------|-------------|
| `items_list` | List all items in a warehouse, including quantity and safety stock |
| `items_detail` | Get full details for a single inventory item by ID |
| `movements_list` | List recent stock movements (outbound requests and stock adjustments) for a warehouse |

### Inbound Tools

| Tool | Description |
|------|-------------|
| `restock_create` | Create a restock (inbound) record |
| `restock_list` | List restock records for a warehouse |

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
- The `DAILYCHECK_MCP_TOKEN` is set correctly in settings.json
- The token has not expired
- The token matches the expected format (bearer token)

### Connection Issues

If the MCP server doesn't connect:
- Verify the Python path is correct: `/Users/ericmr/Documents/GitHub/DailyCheck/mcp_server/.venv/bin/python`
- Ensure the virtual environment exists and has the required packages installed
- Check Claude Code logs for any connection errors
