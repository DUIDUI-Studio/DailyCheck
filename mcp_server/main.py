"""MCP Server CLI — supports stdio and HTTP/SSE transports."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

import click
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from mcp.server.sse import SseServerTransport
from mcp_server.protocol.server import build_server

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Config constants
MESSAGES_PATH = "/messages/"
SSE_PATH = "/sse"
HEALTH_PATH = "/health"


# ---------------------------------------------------------------------------
# Lifespan — manages transport & server lifecycle properly
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: Starlette) -> AsyncIterator[None]:
    """Create transport on startup, clean up on shutdown.

    Starlette guarantees this runs before first request and after
    the server stops accepting connections.
    """
    transport = SseServerTransport(MESSAGES_PATH)
    app.state.sse_transport = transport
    app.state.mcp_server = build_server()
    logger.info("MCP server initialized")
    try:
        yield
    finally:
        logger.info("MCP server shutting down")


# ---------------------------------------------------------------------------
# Auth middleware — validates Bearer token via DAILYCHECK_MCP_TOKEN env var
# ---------------------------------------------------------------------------

class AuthMiddleware:
    """ASGI middleware that requires DAILYCHECK_MCP_TOKEN if set."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        expected = os.environ.get("DAILYCHECK_MCP_TOKEN", "")
        if expected:
            headers = {k.decode(): v.decode() for k, v in scope.get("headers", [])}
            auth = headers.get("authorization", "")
            if not (auth.startswith("Bearer ") and auth[7:].strip() == expected):
                resp = JSONResponse(
                    {"error": "unauthorized", "message": "Invalid or missing token"},
                    status_code=401,
                )
                await resp(scope, receive, send)
                return

        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# SSE + POST endpoints — access transport via app.state, not globals
# ---------------------------------------------------------------------------

async def sse_endpoint(request: Request) -> Response:
    transport: SseServerTransport = request.app.state.sse_transport
    server = request.app.state.mcp_server
    async with transport.connect_sse(
        request.scope, request.receive, request._send
    ) as streams:
        read, write = streams
        await server.run(read, write, server.create_initialization_options())
    return Response()


async def post_message(request: Request) -> Response:
    transport: SseServerTransport = request.app.state.sse_transport
    await transport.handle_post_message(
        request.scope, request.receive, request._send
    )
    return Response()


# ---------------------------------------------------------------------------
# Health check — more thorough than just "ok"
# ---------------------------------------------------------------------------

def _build_app() -> Starlette:
    """Construct the Starlette app (exposed for programmatic use)."""
    app = Starlette(
        lifespan=lifespan,
        middleware=[Middleware(AuthMiddleware)],
        routes=[
            Route(SSE_PATH, endpoint=sse_endpoint, methods=["GET"]),
            Mount(MESSAGES_PATH, app=post_message),
            Route(HEALTH_PATH, endpoint=health, methods=["GET"]),
        ],
    )
    return app


def health(**_) -> JSONResponse:
    """Return health status including MCP server and DB connectivity."""
    checks = {
        "mcp_server": "ok",
        "transport": "ok",
        "db": "unknown",
    }
    try:
        from mcp_server.data.unit_of_work import master_connection
        with master_connection() as conn:
            conn.execute("SELECT 1").fetchone()
        checks["db"] = "ok"
    except Exception as e:
        checks["db"] = f"error: {e}"

    status_code = 200 if all(v == "ok" for v in checks.values()) else 503
    return JSONResponse({"status": "ok", "checks": checks}, status_code=status_code)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option(
    "--transport",
    type=click.Choice(["stdio", "http"]),
    default="stdio",
    show_default=True,
    help="stdio = local Claude Code; http = HTTP/SSE for remote agents",
)
@click.option("--port", default=5100, help="TCP port (http mode only)")
@click.option("--host", default="127.0.0.1", help="Bind address (http mode only)")
def run(transport: str, port: int, host: str) -> None:
    """Start the DailyCheck MCP server.

    stdio  — local process via stdin/stdout (default, for Claude Code)
    http   — HTTP/SSE on TCP port (for remote agents)
    """
    if transport == "stdio":
        _run_stdio()
    else:
        _run_http(port, host)


def _run_stdio() -> None:
    from mcp.server.stdio import stdio_server

    async def main() -> None:
        async with stdio_server() as (read, write):
            await _mcp_server.run(read, write, _mcp_server.create_initialization_options())

    asyncio.run(main())


def _run_http(port: int, host: str) -> None:
    import uvicorn

    app = _build_app()
    logger.info("Starting HTTP/SSE MCP server on %s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")


# Keep stdio path consistent — build server once at module load
_mcp_server = build_server()

if __name__ == "__main__":
    run()
