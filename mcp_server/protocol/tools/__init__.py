"""Tool exports."""
from __future__ import annotations

from mcp.types import Tool


def get_tools() -> list[Tool]:
    """Return the list of Tool definitions for the list_tools callback."""
    tools = [
        # Inventory tools
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
        # Inbound tools
        Tool(
            name="restock_create",
            title="Create Restock",
            description="Create a restock (inbound) record",
            inputSchema={
                "type": "object",
                "properties": {
                    "item_id": {
                        "type": "integer",
                        "description": "Item ID",
                    },
                    "quantity": {
                        "type": "integer",
                        "description": "Quantity to restock",
                    },
                    "warehouse_code": {
                        "type": "string",
                        "description": "Warehouse code (e.g. WH001)",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Reason for restock (optional)",
                    },
                },
                "required": ["item_id", "quantity", "warehouse_code"],
            },
        ),
        Tool(
            name="restock_list",
            title="List Restocks",
            description="List restock records for a warehouse",
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
        # Forecast tools
        Tool(
            name="item_forecast",
            title="Get Item Forecast",
            description="Get consumption forecast for an item",
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
                    "horizon_days": {
                        "type": "integer",
                        "description": "Forecast horizon in days (1-90, default 14)",
                    },
                },
                "required": ["item_id", "warehouse_code"],
            },
        ),
        # Procurement tools
        Tool(
            name="procurement_store",
            title="Get Store Procurement",
            description="Get procurement recommendations for a store",
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
            name="procurement_hub",
            title="Get Hub Procurement",
            description="Get procurement recommendations aggregated across all warehouses",
            inputSchema={
                "type": "object",
                "properties": {
                    "warehouse_code": {
                        "type": "string",
                        "description": "Warehouse code (optional, for specific warehouse)",
                    },
                },
                "required": [],
            },
        ),
    ]
    return tools
