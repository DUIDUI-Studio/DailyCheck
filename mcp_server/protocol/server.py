"""MCP Server implementation."""
from __future__ import annotations

import json
import logging

from mcp.server import Server
from mcp.types import Tool, TextContent
from mcp_server.protocol.tools import get_tools
from mcp_server.protocol.tools.inventory import (
    items_list_impl,
    items_detail_impl,
    movements_list_impl,
)
from mcp_server.protocol.tools.inbound import (
    restock_create_impl,
    restock_list_impl,
)
from mcp_server.protocol.tools.forecast import item_forecast_impl
from mcp_server.protocol.tools.procurement import (
    procurement_store_impl,
    procurement_hub_impl,
)
from mcp_server.protocol.tools.outbound import (
    outbound_create_impl,
    outbound_list_impl,
    outbound_rollback_impl,
)
from mcp_server.protocol.tools.consumption import (
    warehouse_consumption_impl,
    item_consumption_impl,
)
from mcp_server.infra.errors import McpError

logger = logging.getLogger(__name__)

_tool_map = {
    "items_list": items_list_impl,
    "items_detail": items_detail_impl,
    "movements_list": movements_list_impl,
    "restock_create": restock_create_impl,
    "restock_list": restock_list_impl,
    "item_forecast": item_forecast_impl,
    "procurement_store": procurement_store_impl,
    "procurement_hub": procurement_hub_impl,
    "outbound_create": outbound_create_impl,
    "outbound_list": outbound_list_impl,
    "outbound_rollback": outbound_rollback_impl,
    "warehouse_consumption": warehouse_consumption_impl,
    "item_consumption": item_consumption_impl,
}


def build_server() -> Server:
    """Build and return a configured MCP Server."""
    server = Server("dailycheck-mcp")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        """Serve the tool registry."""
        return get_tools()

    @server.call_tool()
    async def call_tool(
        name: str, arguments: dict | None
    ) -> list[TextContent]:
        """Route incoming tool calls to handlers; serialize results as JSON."""
        handler = _tool_map.get(name)
        if handler is None:
            logger.warning("call_tool: unknown tool %r", name)
            raise ValueError(f"Unknown tool: {name}")

        try:
            result = handler(arguments or {})
        except McpError as e:
            logger.info("call_tool %r raised McpError %s: %s", name, e.code, e.message)
            payload = json.dumps(e.to_dict(), ensure_ascii=False)
            return [TextContent(type="text", text=payload)]

        # Return the result as a JSON string so the caller can deserialize it
        payload = json.dumps(result, ensure_ascii=False, default=str)
        return [TextContent(type="text", text=payload)]

    return server
