"""MCP Server implementation."""
from __future__ import annotations

from mcp import ServerCapabilities, Tool


def build_server() -> "Server":
    """Build and return the MCP Server instance."""
    from mcp.server import Server as MCPServer

    server = MCPServer("dailycheck-mcp")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        """List available tools."""
        return [
            Tool(
                name="hello",
                description="A simple hello world tool",
                inputSchema={"type": "object", "properties": {"name": {"type": "string"}}}
            )
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict | None) -> list:
        """Handle tool calls."""
        if name == "hello":
            name = arguments.get("name", "world") if arguments else "world"
            return [f"Hello, {name}!"]
        return []

    return server


class Server:
    """Stub for type hints."""
    pass
