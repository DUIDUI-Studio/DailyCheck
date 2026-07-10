"""MCP Server CLI entry point."""
from __future__ import annotations

import click

from mcp_server.protocol.server import build_server


@click.command()
@click.option("--port", default=5100, help="Port to bind (not used in stdio mode, kept for compatibility)")
def run(port: int) -> None:
    """Start MCP Server with stdio transport."""
    server = build_server()
    from mcp.server.stdio import stdio_server
    import asyncio

    async def main():
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(main())


if __name__ == "__main__":
    run()
