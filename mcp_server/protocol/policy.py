"""Request-scoped policy view over the existing MCP SDK server."""
from __future__ import annotations

import json
import time

from mcp import types
from mcp.server import Server
from starlette.concurrency import run_in_threadpool

from mcp_server.infra.errors import ForbiddenError, McpError, ValidationError
from mcp_server.protocol.registry import get_policy_actions
from mcp_server.service.auth import _current_auth
from mcp_server.service.policy import MAX_SQLITE_ID, BoundPolicy
from mcp_server.service.policy_reads import get_inventory, list_orders
from mcp_server.service.store_order import DEFAULT_LIMIT, MAX_LIMIT

_INPUT_SCHEMAS = {
    "items_detail": {
        "type": "object",
        "properties": {
            "item_id": {"type": "integer", "minimum": 1, "maximum": MAX_SQLITE_ID},
            "warehouse_code": {"type": "string", "minLength": 1},
        },
        "required": ["item_id", "warehouse_code"],
        "additionalProperties": False,
    },
    "store_order_list": {
        "type": "object",
        "properties": {
            "warehouse_code": {"type": "string", "minLength": 1},
            "status": {"type": "string", "enum": [
                "pending", "approved", "rejected", "shipped", "delivered", "cancelled",
            ]},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": DEFAULT_LIMIT},
            "offset": {"type": "integer", "minimum": 0, "maximum": MAX_SQLITE_ID, "default": 0},
        },
        "required": [],
        "additionalProperties": False,
    },
}
_HANDLERS = {"items_detail": get_inventory, "store_order_list": list_orders}


def _validate_arguments(schema: dict, arguments: dict) -> None:
    properties = schema["properties"]
    if not set(schema["required"]) <= arguments.keys() <= properties.keys():
        raise ValidationError("invalid_arguments")
    for key, value in arguments.items():
        spec = properties[key]
        if spec["type"] == "integer":
            if type(value) is not int or not spec["minimum"] <= value <= spec["maximum"]:
                raise ValidationError("invalid_arguments")
        elif not isinstance(value, str) or not value or ("enum" in spec and value not in spec["enum"]):
            raise ValidationError("invalid_arguments")


def _input_schema(name: str, fields: tuple[str, ...]) -> dict:
    schema = _INPUT_SCHEMAS[name]
    if name == "store_order_list" and "status" not in fields:
        return {**schema, "properties": {key: value for key, value in schema["properties"].items() if key != "status"}}
    return schema


def _policy_tools(policy: BoundPolicy, actions: dict[str, str]) -> list[types.Tool]:
    tools = []
    for name, action in actions.items():
        if fields := policy.fields(action):
            tools.append(types.Tool(
                name=name, inputSchema=_input_schema(name, fields),
                description=f"Read authorized storefront data. Returned record fields: {', '.join(fields)}.",
            ))
    return tools


def bind_policy_dispatch(server: Server, audit) -> None:
    actions = get_policy_actions()
    if actions.keys() != _INPUT_SCHEMAS.keys() or actions.keys() != _HANDLERS.keys():
        raise RuntimeError("Scoped policy adapters must match registered read tools")
    # Scoped declarations never get written to SDK's shared legacy tool cache.
    legacy_list = server.request_handlers[types.ListToolsRequest]
    legacy_call = server.request_handlers[types.CallToolRequest]

    async def list_tools(request):
        ctx = _current_auth.get()
        if ctx is None or ctx.policy is None:
            return await legacy_list(request)
        return types.ServerResult(types.ListToolsResult(tools=_policy_tools(ctx.policy, actions)))

    async def call_tool(request: types.CallToolRequest):
        ctx = _current_auth.get()
        if ctx is None or ctx.policy is None:
            return await legacy_call(request)

        started_ns = time.perf_counter_ns()
        name = request.params.name
        fields = ctx.policy.fields(actions.get(name))
        status = 500
        try:
            if not fields:
                raise ForbiddenError("forbidden_tool")
            arguments = request.params.arguments or {}
            if request.params.model_extra:
                raise ValidationError("invalid_arguments")
            _validate_arguments(_input_schema(name, fields), arguments)
            payload = await run_in_threadpool(_HANDLERS[name], ctx.policy, fields, **arguments)
            status = 200
        except McpError as exc:
            payload = exc.to_dict()
            status = exc.http_status
        except Exception:
            payload = {"error": "internal_error", "message": "internal_error"}
        finally:
            audit(name if fields else "restricted", status, started_ns)
        text = types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))
        return types.ServerResult(types.CallToolResult(content=[text], isError=status != 200))

    server.request_handlers[types.ListToolsRequest] = list_tools
    server.request_handlers[types.CallToolRequest] = call_tool
