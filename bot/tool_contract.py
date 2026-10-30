"""Approved execution names, independent of dynamic MCP discovery (#78 D1–D7)."""
from __future__ import annotations

LEGACY_PARENTS = {
    "list_warehouses": "warehouse_list",
    "list_warehouse_items": "items_list",
    "get_item_detail": "items_detail",
    "get_item_consumption": "item_consumption",
}
# Explicit path ACLs also cover handlers which historically only checked warehouses.
READ_PATHS = {
    "warehouse_list": "/api/v1/warehouses",
    "items_list": "/api/v1/items",
    "items_detail": "/api/v1/items/<id>",
    "movements_list": "/api/v1/movements",
    "restock_list": "/api/v1/restock",
    "outbound_list": "/api/v1/outbound",
    "item_forecast": "/api/v1/forecast/item/<id>",
    "procurement_store": "/api/v1/procurement/store",
    "procurement_hub": "/api/v1/procurement/hub",
    "warehouse_consumption": "/api/v1/consumption/warehouse",
    "item_consumption": "/api/v1/consumption/item/<id>",
    "canonical_list": "/api/v1/canonical",
    "canonical_detail": "/api/v1/canonical/<id>",
    "canonical_categories": "/api/v1/canonical/categories",
    "canonical_fanout_targets": "/api/v1/canonical/fanout-targets",
    "canonical_conflicts": "/api/v1/canonical/conflicts",
    "canonical_fanout_event": "/api/v1/canonical/fanout/event/<id>",
    "store_order_list": "/api/v1/store-orders",
    "store_order_detail": "/api/v1/store-orders/<id>",
}
WRITE_PATHS = {
    "restock_create": "/api/v1/restock",
    "outbound_create": "/api/v1/outbound",
    "outbound_rollback": "/api/v1/outbound/rollback",
}
EXTRA_READS = frozenset(name for name in READ_PATHS if name.startswith(("canonical_", "store_order_")))
GLOBAL_READS = frozenset(name for name in READ_PATHS if name.startswith("canonical_"))
MCP_NAMES = frozenset(READ_PATHS) | frozenset(WRITE_PATHS)
LEGACY_PATHS = {
    **{name: READ_PATHS[parent] for name, parent in LEGACY_PARENTS.items()},
    "search_items": READ_PATHS["items_list"],
    "list_low_stock": READ_PATHS["items_list"],
    "list_store_overview": READ_PATHS["items_list"],
    "list_orders": READ_PATHS["store_order_list"],
}


def mcp_schemas() -> list[dict]:
    """Reuse registered schemas, but discovery never grants execution authority."""
    from mcp_server.protocol.tools import get_tools

    result = []
    for tool in get_tools():
        if tool.name not in MCP_NAMES:
            continue
        schema = dict(tool.inputSchema)
        schema["additionalProperties"] = False
        description = tool.description or tool.name
        if tool.name in GLOBAL_READS:
            description += " 全局主数据/跨仓信息；需要显式 global_read 授权，不按仓自动裁切。"
        if tool.name == "item_consumption":
            description += " MCP 固定窗口/未回滚业务单据口径；旧 get_item_consumption 为动作流水口径。"
        result.append({"type": "function", "function": {
            "name": tool.name, "description": description, "parameters": schema,
        }})
    return result
