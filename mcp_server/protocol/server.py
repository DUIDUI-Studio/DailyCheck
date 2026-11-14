"""MCP Server implementation."""
from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING

from mcp.server import Server
from mcp.types import TextContent, Tool

from mcp_server.infra.access_log import write_mcp_access_log
from mcp_server.infra.errors import McpError, UnauthorizedError
from mcp_server.protocol.registry import get_handlers
from mcp_server.protocol.tools import get_tools
from mcp_server.readonly import (
    STRICT_REJECTED_TOOL_LOG_NAME,
    STRICT_TOOL_NAMES,
    ReadonlyPolicyError,
    ensure_token_active,
    reset_current_policy,
    set_current_policy,
    validate_strict_arguments,
)

if TYPE_CHECKING:
    from mcp_server.readonly import ReadonlyPolicy

logger = logging.getLogger(__name__)

# Issue #85: every call that enters call_tool appends exactly one access-log
# record. MCP has no HTTP method/path at this layer, so method/path identify
# the JSON-RPC operation instead; tool_name carries the concrete tool.
_ACCESS_LOG_METHOD = "tools/call"
_ACCESS_LOG_PATH = "/api/mcp/"
_MAX_TOOL_NAME_LEN = 128


def _access_log_token_id() -> int | None:
    """Return the request-scoped token id without authenticating anything.

    resolve_ctx() falls back to the process-level env token on stdio; using it
    here would attribute calls to that shared identity and mix them with a
    request that never authenticated. An absent request context stays null.
    """
    from mcp_server.service.auth import _current_auth

    ctx = _current_auth.get()
    return ctx.token_id if ctx is not None else None


def _bounded_tool_name(name: str) -> str:
    """Cap the logged tool name so a caller cannot bloat or inject log lines."""
    if not isinstance(name, str):
        name = str(name)
    return name[:_MAX_TOOL_NAME_LEN]


_tool_map = get_handlers()


def _strict_rejection() -> ValueError:
    """A fixed error that never echoes a caller-controlled tool name."""
    return ValueError("Unknown tool")


def build_server(policy: ReadonlyPolicy | None = None) -> Server:
    """Build and return a configured MCP Server.

    ``policy`` is the explicit, opt-in strict read-only mode.  Passing None
    preserves the existing full-registry behaviour.  The policy is captured in
    this server's closures; no module-level state decides the transport.
    """
    strict = policy is not None
    server = Server("dailycheck-mcp")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        """Serve the tool registry (filtered to the pilot allowlist in strict mode)."""
        tools = get_tools()
        if strict:
            return [tool for tool in tools if tool.name in STRICT_TOOL_NAMES]
        return tools

    @server.call_tool()
    async def call_tool(
        name: str, arguments: dict | None
    ) -> list[TextContent]:
        """Route incoming tool calls to handlers; serialize results as JSON.

        Every call entering this route writes exactly one access-log record in
        `finally`. `status` is the business result (200 / McpError.http_status /
        400 unknown tool / 500 unexpected error), never the HTTP transport
        status. Logging failures are swallowed so they cannot change the
        handler's response or make it run twice.

        In strict mode the static allowlist, exact-argument check and policy
        deadline/revocation check all run before any handler or business
        database is opened.
        """
        started_ns = time.perf_counter_ns()
        status = 500
        log_name = _bounded_tool_name(name)
        policy_token = None

        try:
            if strict:
                if name not in STRICT_TOOL_NAMES:
                    log_name = STRICT_REJECTED_TOOL_LOG_NAME
                    status = 400
                    logger.warning("call_tool: restricted tool rejected")
                    raise _strict_rejection()
                policy_token = set_current_policy(policy)
                try:
                    validate_strict_arguments(name, arguments)
                except McpError as e:
                    logger.info("call_tool %r rejected strict arguments", name)
                    status = e.http_status
                    payload = json.dumps(e.to_dict(), ensure_ascii=False)
                    return [TextContent(type="text", text=payload)]
                try:
                    ensure_token_active(policy)
                except ReadonlyPolicyError:
                    logger.info("call_tool: readonly policy inactive")
                    status = 401
                    payload = json.dumps(
                        UnauthorizedError("readonly policy inactive").to_dict(),
                        ensure_ascii=False,
                    )
                    return [TextContent(type="text", text=payload)]

            handler = _tool_map.get(name)
            if handler is None:
                logger.warning("call_tool: unknown tool %r", name)
                status = 400
                raise ValueError(f"Unknown tool: {name}")

            try:
                result = handler(arguments or {})
            except McpError as e:
                logger.info("call_tool %r raised McpError %s: %s", name, e.code, e.message)
                payload = json.dumps(e.to_dict(), ensure_ascii=False)
                status = e.http_status
                return [TextContent(type="text", text=payload)]

            # Return the result as a JSON string so the caller can deserialize it
            payload = json.dumps(result, ensure_ascii=False, default=str)
            status = 200
            return [TextContent(type="text", text=payload)]
        finally:
            if policy_token is not None:
                reset_current_policy(policy_token)
            duration_ms = max(0, (time.perf_counter_ns() - started_ns) // 1_000_000)
            try:
                write_mcp_access_log(
                    token_id=_access_log_token_id(),
                    method=_ACCESS_LOG_METHOD,
                    path=_ACCESS_LOG_PATH,
                    status=status,
                    duration_ms=duration_ms,
                    tool_name=log_name,
                )
            except Exception:
                logger.debug("call_tool: access log write failed", exc_info=True)

    return server
