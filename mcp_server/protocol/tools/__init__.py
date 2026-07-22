"""Tool exports."""
from __future__ import annotations

from mcp.types import Tool


def get_tools() -> list[Tool]:
    """Return the list of Tool definitions for the list_tools callback."""
    tools = [
        # Warehouse meta
        Tool(
            name="warehouse_list",
            title="List Warehouses",
            description="List all warehouses accessible to this token, returning code and display name. Returns: array of {code, name}.",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        # Inventory tools
        Tool(
            name="items_list",
            title="List Warehouse Items",
            description="List all items in a warehouse. Returns: array of {id, sku, name, category_id, category_name, current_stock, safety_stock, unit, unit_cost, gram_per_unit, updated_at}.",
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
            description="Get full details for a single inventory item by ID. Returns: {id, sku, name, category_id, category_name, current_stock, safety_stock, unit, unit_cost, gram_per_unit, updated_at}.",
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
            description="List recent stock movements (outbound requests and stocktake adjustments) for a warehouse. Returns: array of {id, type, item_id, item_name, qty, reason, created_at}.",
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
            description="Create a restock (inbound) record. Returns: {id, item_id, quantity, warehouse_code}.",
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
            description="List restock records for a warehouse. Returns: array of {id, item_id, item_name, qty, reason, created_at}.",
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
            description="Get consumption forecast for an item. Returns: {item_id, warehouse_code, horizon_days, daily_avg, forecast_total, confidence, computed_at, data_status}.",
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
            description="Get procurement recommendations for a store. Returns: {warehouse_code, computed_at, items: [{item_id, item_name, current_qty, in_transit_qty, daily_avg, forecast_total_horizon, safety_stock, suggested_qty}]}.",
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
            description="Get procurement recommendations aggregated across all warehouses. Returns: {computed_at, items: [{item_id, item_name, total_suggested_qty, stores_needing, stores_detail}]}.",
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
        # Outbound tools
        Tool(
            name="outbound_create",
            title="Create Outbound",
            description="Create an outbound request: deduct stock and write movement record. Mirrors the Flask outbound_submit endpoint. Returns: {id, item_id, quantity, warehouse_code}.",
            inputSchema={
                "type": "object",
                "properties": {
                    "item_id": {
                        "type": "integer",
                        "description": "Item ID",
                    },
                    "quantity": {
                        "type": "number",
                        "description": "Quantity to outbound (must be <= current stock)",
                    },
                    "warehouse_code": {
                        "type": "string",
                        "description": "Warehouse code (e.g. WH001)",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Reason for outbound (optional)",
                    },
                },
                "required": ["item_id", "quantity", "warehouse_code"],
            },
        ),
        Tool(
            name="outbound_list",
            title="List Outbounds",
            description="List outbound requests for a warehouse. Returns: array of {id, item_id, item_name, quantity, reason, status, rolled_back, created_at}.",
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
            name="outbound_rollback",
            title="Rollback Outbound",
            description="Roll back an outbound request: return stock to warehouse. Returns: {id, item_id, quantity}.",
            inputSchema={
                "type": "object",
                "properties": {
                    "request_id": {
                        "type": "integer",
                        "description": "Outbound request ID to roll back",
                    },
                    "warehouse_code": {
                        "type": "string",
                        "description": "Warehouse code (e.g. WH001)",
                    },
                },
                "required": ["request_id", "warehouse_code"],
            },
        ),
        # Consumption tools
        Tool(
            name="warehouse_consumption",
            title="Warehouse Consumption Summary",
            description=(
                "Return per-item consumption summary for a warehouse with rank, qty, daily avg, turnover rate, and percentage. "
                "Returns: {items: [{rank, item_id, sku, name, category_name, unit, current_stock, safety_stock, "
                "consume_qty, active_days, daily_avg, turnover_rate, consume_pct, first_date, last_date}], "
                "warehouse_turnover: {window_days, warehouse_cogs_value, warehouse_avg_inventory_value, "
                "turnover_value, items_with_turnover, items_total, data_quality, method}}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "warehouse_code": {
                        "type": "string",
                        "description": "Warehouse code (e.g. WH001)",
                    },
                    "days": {
                        "type": "integer",
                        "enum": [7, 14, 30],
                        "description": "Time window: 7, 14, or 30 days (default 7)",
                    },
                    "sort_by": {
                        "type": "string",
                        "enum": ["qty", "value", "turnover", "name"],
                        "description": "Sort by: qty (default), value, turnover, name",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max items to return (default 100, max 200)",
                    },
                },
                "required": ["warehouse_code"],
            },
        ),
        Tool(
            name="item_consumption",
            title="Item Consumption Detail",
            description=(
                "Return consumption stats for a single item: 7d / 14d / 30d / monthly totals, weekly breakdown. "
                "Opt-in include_turnover adds a stocktake-anchored inventory turnover estimate. "
                "Returns: {item_id, sku, name, category_name, unit, current_stock, safety_stock, "
                "consume_7d{qty,window_days,daily_avg}, consume_14d{...}, consume_30d{...}, consume_monthly{...}, "
                "weekly[{...}], "
                "[opt-in when include_turnover=true: inventory_turnover]}."
            ),
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
                    "include_turnover": {
                        "type": "boolean",
                        "description": "If true, also return inventory_turnover (stocktake-anchored average-inventory estimate). Default false.",
                        "default": False,
                    },
                    "turnover_days": {
                        "type": "integer",
                        "enum": [7, 14, 30],
                        "description": "Window for the inventory_turnover calculation (default 30).",
                        "default": 30,
                    },
                },
                "required": ["item_id", "warehouse_code"],
            },
        ),
    ]
    return tools
