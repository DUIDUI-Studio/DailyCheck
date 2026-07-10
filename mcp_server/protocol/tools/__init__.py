"""Tool exports."""
from __future__ import annotations

from mcp.types import Tool


def get_tools() -> list[Tool]:
    """Return the list of Tool definitions for the list_tools callback."""
    return [
        Tool(
            name="items_list",
            title="List Warehouse Items",
            description="List all items in a warehouse, including quantity and safety stock.",
            inputSchema={
                "type": "object",
                "properties": {
                    "warehouse_code": {
                        "type": "string",
                        "description": "Warehouse code (e.g. WH001)",
                    }
                },
                "required": ["warehouse_code"],
            },
        ),
        Tool(
            name="items_detail",
            title="Get Item Detail",
            description="Get full details for a single inventory item by ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "item_id": {
                        "type": "integer",
                        "description": "Item ID",
                    },
                    "warehouse_code": {
                        "type": "string",
                        "description": "Warehouse code (e.g. WH001)",
                    },
                },
                "required": ["item_id", "warehouse_code"],
            },
        ),
        Tool(
            name="movements_list",
            title="List Stock Movements",
            description="List recent stock movements (outbound requests and stock adjustments) for a warehouse.",
            inputSchema={
                "type": "object",
                "properties": {
                    "warehouse_code": {
                        "type": "string",
                        "description": "Warehouse code (e.g. WH001)",
                    }
                },
                "required": ["warehouse_code"],
            },
        ),
    ]
