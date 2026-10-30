"""MCP Server implementation."""
from __future__ import annotations

import json
import logging
import time

from mcp.server import Server
from mcp.types import TextContent, Tool

from mcp_server.infra.access_log import write_mcp_access_log
from mcp_server.infra.errors import McpError
from mcp_server.protocol.tools import get_tools
from mcp_server.protocol.tools.canonical import (
    canonical_categories_impl,
    canonical_claim_impl,
    canonical_conflicts_impl,
    canonical_create_impl,
    canonical_detail_impl,
    canonical_fanout_event_impl,
    canonical_fanout_impl,
    canonical_fanout_targets_impl,
    canonical_list_impl,
    canonical_resolve_conflict_impl,
    canonical_set_binding_active_impl,
    canonical_unclaim_impl,
    canonical_update_impl,
)
from mcp_server.protocol.tools.consumption import (
    item_consumption_impl,
    warehouse_consumption_impl,
)
from mcp_server.protocol.tools.forecast import item_forecast_impl
from mcp_server.protocol.tools.inbound import (
    restock_create_impl,
    restock_list_impl,
)
from mcp_server.protocol.tools.inventory import (
    items_detail_impl,
    items_list_impl,
    movements_list_impl,
    warehouse_list_impl,
)
from mcp_server.protocol.tools.outbound import (
    outbound_create_impl,
    outbound_list_impl,
    outbound_rollback_impl,
)
from mcp_server.protocol.tools.procurement import (
    procurement_hub_impl,
    procurement_store_impl,
)
from mcp_server.protocol.tools.store_order import (
    store_order_detail_impl,
    store_order_list_impl,
)

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


_tool_map = {
    "warehouse_list": warehouse_list_impl,
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
    "canonical_list": canonical_list_impl,
    "canonical_detail": canonical_detail_impl,
    "canonical_categories": canonical_categories_impl,
    "canonical_fanout_targets": canonical_fanout_targets_impl,
    "canonical_conflicts": canonical_conflicts_impl,
    "canonical_fanout_event": canonical_fanout_event_impl,
    "canonical_create": canonical_create_impl,
    "canonical_update": canonical_update_impl,
    "canonical_claim": canonical_claim_impl,
    "canonical_unclaim": canonical_unclaim_impl,
    "canonical_fanout": canonical_fanout_impl,
    "canonical_resolve_conflict": canonical_resolve_conflict_impl,
    "canonical_set_binding_active": canonical_set_binding_active_impl,
    "store_order_list": store_order_list_impl,
    "store_order_detail": store_order_detail_impl,
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
        """Route incoming tool calls to handlers; serialize results as JSON.

        Every call entering this route writes exactly one access-log record in
        `finally`. `status` is the business result (200 / McpError.http_status /
        400 unknown tool / 500 unexpected error), never the HTTP transport
        status. Logging failures are swallowed so they cannot change the
        handler's response or make it run twice.
        """
        started_ns = time.perf_counter_ns()
        status = 500

        try:
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
            duration_ms = max(0, (time.perf_counter_ns() - started_ns) // 1_000_000)
            try:
                write_mcp_access_log(
                    token_id=_access_log_token_id(),
                    method=_ACCESS_LOG_METHOD,
                    path=_ACCESS_LOG_PATH,
                    status=status,
                    duration_ms=duration_ms,
                    tool_name=_bounded_tool_name(name),
                )
            except Exception:
                logger.debug("call_tool: access log write failed", exc_info=True)

    return server
