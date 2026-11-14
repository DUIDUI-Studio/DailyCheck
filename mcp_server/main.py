"""MCP Server CLI — supports stdio and HTTP/SSE transports."""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import click
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from mcp_server.protocol.server import build_server
from mcp_server.readonly import ReadonlyPolicy

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Config constants
MESSAGES_PATH = "/messages/"
SSE_PATH = "/sse"
HEALTH_PATH = "/health"
STREAMABLE_HTTP_PATH = "/api/mcp/"


# ---------------------------------------------------------------------------
# Lifespan — manages transport & server lifecycle properly
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: Starlette) -> AsyncIterator[None]:
    """Create transport on startup, clean up on shutdown.

    Starlette guarantees this runs before first request and after
    the server stops accepting connections.
    """
    policy = getattr(app.state, "readonly_policy", None)
    transport = SseServerTransport(MESSAGES_PATH)
    app.state.sse_transport = transport
    app.state.mcp_server = build_server(policy)
    app.state.streamable_manager = StreamableHTTPSessionManager(app.state.mcp_server, json_response=True, stateless=True)
    logger.info("MCP server initialized")
    try:
        async with app.state.streamable_manager.run():
            yield
    finally:
        logger.info("MCP server shutting down")


# ---------------------------------------------------------------------------
# Auth middleware — validates Bearer token via DAILYCHECK_MCP_TOKEN env var
# OR queries agent_tokens table (production main path).
# Fallback chain: env → agent_tokens table (standard mode only).
# Strict policy mode is table-only and never takes the env shortcut.
# ---------------------------------------------------------------------------

class AuthMiddleware:
    """ASGI middleware that authenticates Bearer tokens.

    Standard mode fallback chain:
      1. env DAILYCHECK_MCP_TOKEN (legacy dev/test; backward-compat)
      2. agent_tokens table (production main path) — checked via werkzeug
         pbkdf2:sha256 hash, supports per-token revocation + future ACL.

    Strict mode (an explicit ReadonlyPolicy was passed): only the pinned
    agent_tokens row authenticated over a read-only table connection; the env
    shortcut is not available, even if the env value equals a revoked token.

    Both branches verify the same header shape; either success short-circuits
    to next middleware. 401 if neither matches.
    """

    def __init__(self, app, policy: ReadonlyPolicy | None = None) -> None:
        self.app = app
        self.policy = policy
        # P0-4 per-token 配额：key=token_id，每 token 独立滑动窗口。
        # 单 uvicorn 进程内共享；重启清零（v1 约定，见 rate_limit_pure）。
        self._limiters: dict[int, object] = {}

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode(): v.decode() for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")

        if self.policy is None:
            # Branch 1: legacy env-based shared secret (dev only, but kept for
            # zero-friction local testing without provisioning a token).
            expected = os.environ.get("DAILYCHECK_MCP_TOKEN", "")
            if expected and auth.startswith("Bearer ") and auth[7:].strip() == expected:
                await self.app(scope, receive, send)
                return

        # Branch 2: agent_tokens table lookup (production / strict mode).
        # Lazy import to avoid loading SQLite connection machinery for the
        # env-only dev path (and to keep cold-start minimal).
        try:
            from mcp_server.service.auth import (
                authenticate,
                reset_current_auth,
                set_current_auth,
            )
            ctx = authenticate(auth, policy=self.policy) if auth.startswith("Bearer ") else None
            if ctx is not None:
                # P0-4: per-token 滑动窗口配额（默认 MCP_DEFAULT_RATE_LIMIT/分钟）。
                from rate_limit_pure import SlidingWindowLimiter, mcp_quota_limit

                limiter = self._limiters.get(ctx.token_id)
                if limiter is None:
                    limiter = SlidingWindowLimiter(
                        max_events=mcp_quota_limit(ctx.rate_limit),
                        window_seconds=60.0,
                    )
                    self._limiters[ctx.token_id] = limiter
                if not limiter.hit(f"token-{ctx.token_id}"):
                    resp = JSONResponse(
                        {"error": "rate_limited", "message": "Token quota exceeded"},
                        status_code=429,
                    )
                    await resp(scope, receive, send)
                    return
                # Publish the context two ways: in the ASGI scope (transport
                # level) and in a ContextVar — tool handlers are sync functions
                # with no access to scope, so the ContextVar is what actually
                # reaches them. Always restore it so a sequential or failed
                # request cannot borrow this caller's identity.
                scope.setdefault("state", {})["auth"] = ctx
                token = set_current_auth(ctx)
                try:
                    await self.app(scope, receive, send)
                finally:
                    reset_current_auth(token)
                return
        except Exception as e:
            if self.policy is None:
                logger.warning("agent_tokens lookup failed: %s", e)
            else:
                # Never echo a raw exception/path in strict mode.
                logger.warning("strict authentication failed")

        resp = JSONResponse(
            {"error": "unauthorized", "message": "Invalid or missing token"},
            status_code=401,
        )
        await resp(scope, receive, send)
        return


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


async def post_message(scope, receive, send) -> None:
    # Mount passes an ASGI 3-callable, not a Request — use scope["app"] to reach state.
    transport: SseServerTransport = scope["app"].state.sse_transport
    await transport.handle_post_message(scope, receive, send)


async def streamable_http_asgi(scope, receive, send) -> None:
    """ASGI entrypoint for streamableHttp transport — delegates to manager which
    sends its own ASGI messages. Must NOT be wrapped in a starlette typed endpoint
    (Request→Response), since that would trigger an extra http.response.start."""
    manager: StreamableHTTPSessionManager = scope["app"].state.streamable_manager
    await manager.handle_request(scope, receive, send)


# ---------------------------------------------------------------------------
# Health check — more thorough than just "ok"
# ---------------------------------------------------------------------------

def _build_app(policy: ReadonlyPolicy | None = None) -> Starlette:
    """Construct the Starlette app (exposed for programmatic use).

    ``policy`` is threaded explicitly into both the middleware and the lifespan
    server construction so one transport cannot end up unguarded.
    """
    app = Starlette(
        lifespan=lifespan,
        middleware=[Middleware(AuthMiddleware, policy=policy)],
        routes=[
            Route(SSE_PATH, endpoint=sse_endpoint, methods=["GET"]),
            Mount(MESSAGES_PATH, app=post_message),
            Mount(STREAMABLE_HTTP_PATH, app=streamable_http_asgi),
            Route(HEALTH_PATH, endpoint=health, methods=["GET"]),
        ],
    )
    app.state.readonly_policy = policy
    return app


def health(request) -> JSONResponse:
    """Return health status including MCP server and DB connectivity."""
    checks = {
        "mcp_server": "ok",
        "transport": "ok",
        "db": "unknown",
    }
    policy = getattr(request.app.state, "readonly_policy", None)
    try:
        if policy is not None:
            from mcp_server.readonly import readonly_connection

            with readonly_connection(policy.master_path) as conn:
                conn.execute("SELECT 1").fetchone()
        else:
            from mcp_server.data.unit_of_work import master_connection
            with master_connection() as conn:
                conn.execute("SELECT 1").fetchone()
        checks["db"] = "ok"
    except Exception as e:
        checks["db"] = "error" if policy is not None else f"error: {e}"

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
@click.option(
    "--readonly-policy",
    "readonly_policy_path",
    default=None,
    type=click.Path(dir_okay=False, path_type=str),
    help="Explicit read-only policy JSON (default: off)",
)
def run(transport: str, port: int, host: str, readonly_policy_path: str | None) -> None:
    """Start the DailyCheck MCP server.

    stdio  — local process via stdin/stdout (default, for Claude Code)
    http   — HTTP/SSE on TCP port (for remote agents)

    --readonly-policy enables the strict, server-enforced read-only pilot.
    A requested policy that cannot be loaded stops the process; it never falls
    back to standard mode.
    """
    policy = None
    if readonly_policy_path is not None:
        from mcp_server.readonly import ReadonlyPolicyError

        try:
            policy = ReadonlyPolicy.load(readonly_policy_path)
        except ReadonlyPolicyError as exc:
            raise click.ClickException(f"readonly policy rejected: {exc}") from exc

    if transport == "stdio":
        if policy is None:
            _run_stdio()
        else:
            _run_stdio(policy)
    else:
        if policy is None:
            _run_http(port, host)
        else:
            _run_http(port, host, policy)


def _run_stdio(policy: ReadonlyPolicy | None = None) -> None:
    from mcp.server.stdio import stdio_server

    async def main() -> None:
        server = build_server(policy)
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(main())


def _run_http(port: int, host: str, policy: ReadonlyPolicy | None = None) -> None:
    import uvicorn

    app = _build_app(policy)
    logger.info("Starting HTTP/SSE MCP server on %s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run()
