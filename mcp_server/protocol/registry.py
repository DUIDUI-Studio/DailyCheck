"""Authoritative MCP declarations, dispatch bindings and maintenance contracts.

Output shapes are internal regression metadata, never SDK outputSchema: the
existing TextContent(JSON) wire format and all handler behavior stay unchanged.
ACL metadata describes current enforcement, including legacy gaps; it grants
neither Bot execution nor external token permissions.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from mcp.types import Tool

from mcp_server.protocol.tools import (
    canonical,
    consumption,
    forecast,
    inbound,
    inventory,
    outbound,
    procurement,
    store_order,
    wms,
)


def obj(**properties: dict) -> dict:
    """A verifiable minimum shape; extra business fields remain compatible."""
    return {"type": "object", "properties": properties, "required": list(properties)}


def array(row: dict) -> dict:
    return {"type": "array", "items": row}


INTEGER = {"type": "integer"}
NUMBER = {"type": "number"}
STRING = {"type": "string"}
BOOLEAN = {"type": "boolean"}
NULLABLE_STRING = {"type": ["string", "null"]}
ROW = {"type": "object"}
ITEM = obj(id=INTEGER, name=STRING, current_stock=NUMBER)
CANONICAL = obj(id=INTEGER, canonical_sku=STRING, name=STRING, unit=STRING, status=STRING)
DOCUMENT = obj(
    id=INTEGER, item_id=INTEGER, quantity=NUMBER, warehouse_code=STRING,
    doc_no=NULLABLE_STRING, line_no=NULLABLE_STRING,
    input_quantity={"type": ["number", "null"]}, factor=NUMBER,
    status=STRING, rolled_back={"type": ["integer", "null"]}, replayed=BOOLEAN, applied=BOOLEAN,
)


def page(key: str, row: dict) -> dict:
    return obj(**{key: array(row)}, total=INTEGER, limit=INTEGER, offset=INTEGER, truncated=BOOLEAN)


def canonical_page() -> dict:
    shape = page("items", CANONICAL)
    shape["properties"]["categories"] = array(ROW)
    shape["required"].append("categories")
    return shape


@dataclass(frozen=True)
class AccessContract:
    method: str
    path: str
    path_enforcement: str
    warehouse_scope: str


@dataclass(frozen=True)
class ToolRegistration:
    tool: Tool
    domain: str
    handler: Callable
    access: AccessContract
    output_shape: dict
    external_subset: bool
    bot_approved: bool
    tests: tuple[str, ...]
    compatibility: str = "preserved-input-description-and-TextContent-JSON"

    @property
    def name(self) -> str:
        return self.tool.name

    @property
    def mode(self) -> str:
        return "read" if self.access.method == "GET" else "write"


REGISTRY = (
    ToolRegistration(
        tool=Tool(
            name="warehouse_list",
            title="List Warehouses",
            description="List all warehouses accessible to this token, returning code and display name. Returns: array of {code, name}.",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        domain='inventory', handler=inventory.warehouse_list_impl,
        access=AccessContract('GET', '/api/v1/warehouses', 'none; Bot adapter checks the documented path', 'filter warehouses by token'),
        output_shape=array(obj(code=STRING, name=STRING)),
        external_subset=True, bot_approved=True,
        tests=("tests/test_mcp_registry.py::test_registered_tool_real_serialized_shape[warehouse_list]",),
    ),
    ToolRegistration(
        tool=Tool(
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
        domain='inventory', handler=inventory.items_list_impl,
        access=AccessContract('GET', '/api/v1/items', 'handler', 'warehouse_code checked in handler/service'),
        output_shape=array(ITEM),
        external_subset=True, bot_approved=True,
        tests=("tests/test_mcp_registry.py::test_registered_tool_real_serialized_shape[items_list]",),
    ),
    ToolRegistration(
        tool=Tool(
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
        domain='inventory', handler=inventory.items_detail_impl,
        access=AccessContract('GET', '/api/v1/items/<id>', 'handler', 'warehouse_code checked in handler/service'),
        output_shape=array(ITEM),
        external_subset=True, bot_approved=True,
        tests=("tests/test_mcp_registry.py::test_registered_tool_real_serialized_shape[items_detail]",),
    ),
    ToolRegistration(
        tool=Tool(
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
        domain='inventory', handler=inventory.movements_list_impl,
        access=AccessContract('GET', '/api/v1/movements', 'handler', 'warehouse_code checked in handler/service'),
        output_shape=array(obj(id=INTEGER, type=STRING, item_id=INTEGER, qty=NUMBER)),
        external_subset=True, bot_approved=True,
        tests=("tests/test_mcp_registry.py::test_registered_tool_real_serialized_shape[movements_list]",),
    ),
    ToolRegistration(
        tool=Tool(
            name="restock_create",
            title="Create Restock",
            description='Create a restock (inbound) record. Returns: {id, item_id, quantity, warehouse_code, doc_no, line_no, input_quantity, factor, status, rolled_back, replayed, applied}. quantity is effective base units; replay does not apply stock again; deleted keys remain consumed.',
            inputSchema={
                "type": "object",
                "properties": {
                    "item_id": {
                        "type": "integer",
                        "description": "Item ID",
                    },
                    "quantity": {
                        "type": "number",
                        "description": "Quantity to restock",
                    },
                    "warehouse_code": {
                        "type": "string",
                        "description": "Warehouse code (e.g. WH001)",
                    },
                    "reason": {
                        "type": ["string", "null"],
                        "description": "Reason for restock (optional)",
                    },
                    'doc_no': {'type': 'string', 'minLength': 1, 'maxLength': 128, 'description': 'Opaque, case-sensitive identity; caller convention <source>:<external_doc_no> (e.g. juleng:JL-001); UTF-8 <=512 bytes, no controls or edge whitespace. Supply together with line_no; explicit null invalid.'},
                    'line_no': {'type': 'string', 'minLength': 1, 'maxLength': 64, 'description': 'Opaque line identity; preserves leading zeros; UTF-8 <=256 bytes, no controls or edge whitespace. Supply together with doc_no; explicit null invalid.'},
                    'factor': {'type': 'number', 'exclusiveMinimum': 0, 'default': 1, 'description': 'Finite positive source-to-base multiplier, applied once before 2dp HALF_EVEN rounding; may be used without a document key.'},
                },
                "required": ["item_id", "quantity", "warehouse_code"],
                "dependentRequired": {"doc_no": ["line_no"], "line_no": ["doc_no"]},
            },
        ),
        domain='inbound', handler=inbound.restock_create_impl,
        access=AccessContract('POST', '/api/v1/restock', 'handler', 'warehouse_code checked in handler/service'),
        output_shape=DOCUMENT,
        external_subset=True, bot_approved=True,
        tests=("tests/test_mcp_registry.py::test_registered_tool_real_serialized_shape[restock_create]",),
    ),
    ToolRegistration(
        tool=Tool(
            name="restock_list",
            title="List Restocks",
            description=(
                "List inbound restock requests for a warehouse with pagination. "
                "Returns: {items: [{id, item_id, item_name, quantity, status, reason, "
                "created_at, doc_no, line_no, input_quantity, factor}], total, limit, offset, truncated}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "warehouse_code": {
                        "type": "string",
                        "description": "Warehouse code (e.g. WH001)",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 500,
                        "default": 100,
                        "description": "Page size (1-500, default 100).",
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 0,
                        "description": "Page offset (default 0; negative values are treated as 0).",
                    },
                },
                "required": ["warehouse_code"],
            },
        ),
        domain='inbound', handler=inbound.restock_list_impl,
        access=AccessContract('GET', '/api/v1/restock', 'handler', 'warehouse_code checked in handler/service'),
        output_shape=page("items", obj(id=INTEGER, item_id=INTEGER, quantity=NUMBER, status=STRING)),
        external_subset=True, bot_approved=True,
        tests=("tests/test_mcp_registry.py::test_registered_tool_real_serialized_shape[restock_list]",),
    ),
    ToolRegistration(
        tool=Tool(
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
        domain='forecast', handler=forecast.item_forecast_impl,
        access=AccessContract('GET', '/api/v1/forecast/item/<id>', 'handler', 'warehouse_code checked in handler/service'),
        output_shape=obj(item_id=INTEGER, warehouse_code=STRING, horizon_days=INTEGER, daily_avg=NUMBER, forecast_total=NUMBER, confidence=STRING, computed_at=STRING, data_status=STRING),
        external_subset=True, bot_approved=True,
        tests=("tests/test_mcp_registry.py::test_registered_tool_real_serialized_shape[item_forecast]",),
    ),
    ToolRegistration(
        tool=Tool(
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
        domain='procurement', handler=procurement.procurement_store_impl,
        access=AccessContract('GET', '/api/v1/procurement/store', 'handler', 'warehouse_code checked in handler/service'),
        output_shape=obj(warehouse_code=STRING, computed_at=STRING, items=array(obj(item_id=INTEGER, suggested_qty=NUMBER))),
        external_subset=True, bot_approved=True,
        tests=("tests/test_mcp_registry.py::test_registered_tool_real_serialized_shape[procurement_store]",),
    ),
    ToolRegistration(
        tool=Tool(
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
        domain='procurement', handler=procurement.procurement_hub_impl,
        access=AccessContract('GET', '/api/v1/procurement/hub', 'handler', 'filter authorized warehouses before aggregation'),
        output_shape=array(obj(item_id=INTEGER, total_suggested_qty=NUMBER, stores_needing=INTEGER, stores_detail=array(ROW))),
        external_subset=True, bot_approved=True,
        tests=("tests/test_mcp_registry.py::test_registered_tool_real_serialized_shape[procurement_hub]",),
    ),
    ToolRegistration(
        tool=Tool(
            name="outbound_create",
            title="Create Outbound",
            description='Create an outbound record. Returns: {id, item_id, quantity, warehouse_code, doc_no, line_no, input_quantity, factor, status, rolled_back, replayed, applied}. quantity is effective base units; replay does not apply stock again; deleted keys remain consumed.',
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
                        "type": ["string", "null"],
                        "description": "Reason for outbound (optional)",
                    },
                    'doc_no': {'type': 'string', 'minLength': 1, 'maxLength': 128, 'description': 'Opaque, case-sensitive identity; caller convention <source>:<external_doc_no> (e.g. juleng:JL-001); UTF-8 <=512 bytes, no controls or edge whitespace. Supply together with line_no; explicit null invalid.'},
                    'line_no': {'type': 'string', 'minLength': 1, 'maxLength': 64, 'description': 'Opaque line identity; preserves leading zeros; UTF-8 <=256 bytes, no controls or edge whitespace. Supply together with doc_no; explicit null invalid.'},
                    'factor': {'type': 'number', 'exclusiveMinimum': 0, 'default': 1, 'description': 'Finite positive source-to-base multiplier, applied once before 2dp HALF_EVEN rounding; may be used without a document key.'},
                },
                "required": ["item_id", "quantity", "warehouse_code"],
                "dependentRequired": {"doc_no": ["line_no"], "line_no": ["doc_no"]},
            },
        ),
        domain='outbound', handler=outbound.outbound_create_impl,
        access=AccessContract('POST', '/api/v1/outbound', 'handler', 'warehouse_code checked in service'),
        output_shape=DOCUMENT,
        external_subset=True, bot_approved=True,
        tests=("tests/test_mcp_registry.py::test_registered_tool_real_serialized_shape[outbound_create]",),
    ),
    ToolRegistration(
        tool=Tool(
            name="outbound_list",
            title="List Outbounds",
            description=(
                "List outbound requests for a warehouse with pagination. "
                "Returns: {items: [{id, item_id, item_name, quantity, reason, status, "
                "rolled_back, created_at}], total, limit, offset, truncated}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "warehouse_code": {
                        "type": "string",
                        "description": "Warehouse code (e.g. WH001)",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 500,
                        "default": 100,
                        "description": "Page size (1-500, default 100).",
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 0,
                        "description": "Page offset (default 0; negative values are treated as 0).",
                    },
                },
                "required": ["warehouse_code"],
            },
        ),
        domain='outbound', handler=outbound.outbound_list_impl,
        access=AccessContract('GET', '/api/v1/outbound', 'handler', 'warehouse_code checked in service'),
        output_shape=page("items", obj(id=INTEGER, item_id=INTEGER, quantity=NUMBER, rolled_back=INTEGER)),
        external_subset=True, bot_approved=True,
        tests=("tests/test_mcp_registry.py::test_registered_tool_real_serialized_shape[outbound_list]",),
    ),
    ToolRegistration(
        tool=Tool(
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
        domain='outbound', handler=outbound.outbound_rollback_impl,
        access=AccessContract('POST', '/api/v1/outbound/rollback', 'handler', 'warehouse_code checked in service'),
        output_shape=obj(id=INTEGER, item_id=INTEGER, quantity=NUMBER),
        external_subset=True, bot_approved=True,
        tests=("tests/test_mcp_registry.py::test_registered_tool_real_serialized_shape[outbound_rollback]",),
    ),
    ToolRegistration(
        tool=Tool(
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
        domain='consumption', handler=consumption.warehouse_consumption_impl,
        access=AccessContract('GET', '/api/v1/consumption/warehouse', 'handler', 'warehouse_code checked in service'),
        output_shape=obj(items=array(obj(item_id=INTEGER, consume_qty=NUMBER, rank=INTEGER)), warehouse_turnover=ROW),
        external_subset=True, bot_approved=True,
        tests=("tests/test_mcp_registry.py::test_registered_tool_real_serialized_shape[warehouse_consumption]",),
    ),
    ToolRegistration(
        tool=Tool(
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
        domain='consumption', handler=consumption.item_consumption_impl,
        access=AccessContract('GET', '/api/v1/consumption/item/<id>', 'handler', 'warehouse_code checked in service'),
        output_shape=obj(item_id=INTEGER, consume_7d=obj(qty=NUMBER, window_days=INTEGER, daily_avg=NUMBER), consume_14d=ROW, consume_30d=ROW, consume_monthly=ROW, weekly=array(ROW)),
        external_subset=True, bot_approved=True,
        tests=("tests/test_mcp_registry.py::test_registered_tool_real_serialized_shape[item_consumption]",),
    ),
    ToolRegistration(
        tool=Tool(
            name="canonical_list",
            title="List Canonical Items",
            description=(
                "List canonical master items with optional status/category/q filters. "
                "Returns: {items: [{id, canonical_sku, name, category_code, unit, "
                "gram_per_unit, aux_unit, aux_rate, selling_price, unit_cost, "
                "barcode, status, created_from, created_at, updated_at}], total, "
                "limit, offset, truncated, categories: [{code, name, item_count}]}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["active", "disabled"],
                        "description": "Optional status filter.",
                    },
                    "category_code": {
                        "type": "string",
                        "description": "Optional canonical category code filter (e.g. PRODUCE_CONSUMABLE).",
                    },
                    "q": {
                        "type": "string",
                        "description": "Optional keyword search across name / canonical_sku / barcode.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max items to return (default 200, max 500).",
                        "default": 200,
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Offset for pagination (default 0).",
                        "default": 0,
                    },
                },
                "required": [],
            },
        ),
        domain='canonical', handler=canonical.canonical_list_impl,
        access=AccessContract('GET', '/api/v1/canonical', 'handler', 'global; no token warehouse filtering'),
        output_shape=canonical_page(),
        external_subset=False, bot_approved=True,
        tests=("tests/test_mcp_registry.py::test_registered_tool_real_serialized_shape[canonical_list]",),
    ),
    ToolRegistration(
        tool=Tool(
            name="canonical_detail",
            title="Get Canonical Item",
            description=(
                "Get one canonical item with aliases and per-warehouse bindings. "
                "Returns: {canonical: {id, canonical_sku, name, ...}, "
                "bindings: [{warehouse_code, warehouse_type, id, sku, name, unit, "
                "canonical_id, is_alias, canonical_status, is_store_exclusive, "
                "is_active}], bound_warehouse_count}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "canonical_id": {
                        "type": "integer",
                        "description": "Canonical master item id.",
                    },
                },
                "required": ["canonical_id"],
            },
        ),
        domain='canonical', handler=canonical.canonical_detail_impl,
        access=AccessContract('GET', '/api/v1/canonical/<id>', 'handler', 'global bindings; no token warehouse filtering'),
        output_shape=obj(canonical=CANONICAL, bindings=array(ROW), bound_warehouse_count=INTEGER),
        external_subset=False, bot_approved=True,
        tests=("tests/test_mcp_registry.py::test_registered_tool_real_serialized_shape[canonical_detail]",),
    ),
    ToolRegistration(
        tool=Tool(
            name="canonical_categories",
            title="List Canonical Categories",
            description=(
                "List canonical categories with per-category item counts (for filter dropdowns). "
                "Returns: array of {code, name, item_count}."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        domain='canonical', handler=canonical.canonical_categories_impl,
        access=AccessContract('GET', '/api/v1/canonical/categories', 'handler', 'global; no token warehouse filtering'),
        output_shape=array(obj(code=STRING, name=STRING, item_count=INTEGER)),
        external_subset=False, bot_approved=True,
        tests=("tests/test_mcp_registry.py::test_registered_tool_real_serialized_shape[canonical_categories]",),
    ),
    ToolRegistration(
        tool=Tool(
            name="canonical_fanout_targets",
            title="List Fanout Target Warehouses",
            description=(
                "List warehouses eligible as fanout targets: storefront + rd + distribution_center. "
                "Returns: array of {code, name, warehouse_type}."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        domain='canonical', handler=canonical.canonical_fanout_targets_impl,
        access=AccessContract('GET', '/api/v1/canonical/fanout-targets', 'handler', 'global; no token warehouse filtering'),
        output_shape=array(obj(code=STRING, name=STRING, warehouse_type=STRING)),
        external_subset=False, bot_approved=True,
        tests=("tests/test_mcp_registry.py::test_registered_tool_real_serialized_shape[canonical_fanout_targets]",),
    ),
    ToolRegistration(
        tool=Tool(
            name="canonical_conflicts",
            title="List Open Canonical Conflicts",
            description=(
                "List status='open' canonical_conflicts optionally filtered by canonical_id. "
                "Returns: array of {id, canonical_id, warehouse_code, local_item_id, "
                "field, conflict_type, canonical_value, local_value, last_synced_value, "
                "status, canonical_sku, canonical_name, created_at}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "canonical_id": {
                        "type": "integer",
                        "description": "Optional canonical_id filter.",
                    },
                },
                "required": [],
            },
        ),
        domain='canonical', handler=canonical.canonical_conflicts_impl,
        access=AccessContract('GET', '/api/v1/canonical/conflicts', 'handler', 'global; no token warehouse filtering'),
        output_shape=array(obj(id=INTEGER, canonical_id=INTEGER, warehouse_code=STRING, status=STRING)),
        external_subset=False, bot_approved=True,
        tests=("tests/test_mcp_registry.py::test_registered_tool_real_serialized_shape[canonical_conflicts]",),
    ),
    ToolRegistration(
        tool=Tool(
            name="canonical_fanout_event",
            title="Get Fanout Event",
            description=(
                "Get one fanout/publish event with per-warehouse and per-item results. "
                "Returns: {event: {id, summary, status, started_by, started_at, "
                "completed_at, item_count, ...}, warehouses: [...], items: [...]}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "event_id": {"type": "integer", "description": "Fanout event id."},
                },
                "required": ["event_id"],
            },
        ),
        domain='canonical', handler=canonical.canonical_fanout_event_impl,
        access=AccessContract('GET', '/api/v1/canonical/fanout/event/<id>', 'handler', 'global; no token warehouse filtering'),
        output_shape={"anyOf": [obj(event=ROW, warehouses=array(ROW), items=array(ROW)), {"type": "null"}]},
        external_subset=False, bot_approved=True,
        tests=("tests/test_mcp_registry.py::test_registered_tool_real_serialized_shape[canonical_fanout_event]",),
    ),
    ToolRegistration(
        tool=Tool(
            name="canonical_create",
            title="Create Canonical Item",
            description=(
                "Create one canonical master item. Two-phase insert returns {id, canonical_sku, ...}. "
                "Requires the token's allowed_write_paths includes POST /api/v1/canonical."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Standard name (required)."},
                    "unit": {"type": "string", "description": "Unit (required)."},
                    "category_code": {"type": "string", "description": "Optional category code."},
                    "gram_per_unit": {"type": "number", "description": "Grams per unit (default 0)."},
                    "aux_unit": {"type": "string", "description": "Optional auxiliary unit (e.g. 克)."},
                    "aux_rate": {"type": "number", "description": "Aux unit conversion rate (default 0)."},
                    "barcode": {"type": "string", "description": "Optional barcode."},
                    "status": {
                        "type": "string",
                        "enum": ["active", "disabled"],
                        "description": "Status (default 'active'). Q3=deactivate_only — 'deleted' is rejected.",
                    },
                    "selling_price": {
                        "type": "number",
                        "description": "Optional selling price (only when Q6=canonical_managed; negative rejected).",
                    },
                    "unit_cost": {
                        "type": "number",
                        "description": "Optional unit cost (only when Q6=canonical_managed).",
                    },
                },
                "required": ["name", "unit"],
            },
        ),
        domain='canonical', handler=canonical.canonical_create_impl,
        access=AccessContract('POST', '/api/v1/canonical', 'handler', 'global; no token warehouse filtering'),
        output_shape=CANONICAL,
        external_subset=False, bot_approved=False,
        tests=("tests/test_mcp_registry.py::test_registered_tool_real_serialized_shape[canonical_create]",),
    ),
    ToolRegistration(
        tool=Tool(
            name="canonical_update",
            title="Update Canonical Item",
            description=(
                "Update editable fields on a canonical item. Only the listed properties "
                "are accepted; status='deleted' is rejected by Q3=deactivate_only. "
                "Returns the updated canonical row."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "canonical_id": {"type": "integer", "description": "Canonical master item id."},
                    "name": {"type": "string"},
                    "unit": {"type": "string"},
                    "category_code": {"type": "string"},
                    "gram_per_unit": {"type": "number"},
                    "aux_unit": {"type": "string"},
                    "aux_rate": {"type": "number"},
                    "barcode": {"type": "string"},
                    "status": {"type": "string", "enum": ["active", "disabled"]},
                    "selling_price": {"type": "number"},
                    "unit_cost": {"type": "number"},
                },
                "required": ["canonical_id"],
            },
        ),
        domain='canonical', handler=canonical.canonical_update_impl,
        access=AccessContract('POST', '/api/v1/canonical/<id>/update', 'handler', 'global; no token warehouse filtering'),
        output_shape=CANONICAL,
        external_subset=False, bot_approved=False,
        tests=("tests/test_mcp_registry.py::test_registered_tool_real_serialized_shape[canonical_update]",),
    ),
    ToolRegistration(
        tool=Tool(
            name="canonical_claim",
            title="Claim Item to Canonical",
            description=(
                "Claim one items row to a canonical_items row. Strict 4-column write "
                "(canonical_id / is_alias / canonical_status / updated_at); inventory / "
                "business fields untouched (Q7 §7.7.2). Returns the claim receipt."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "warehouse_code": {"type": "string", "description": "Warehouse code (e.g. WH001)."},
                    "local_item_id": {"type": "integer", "description": "items.id in the warehouse db."},
                    "canonical_id": {"type": "integer", "description": "canonical_items.id."},
                    "is_alias": {
                        "type": "boolean",
                        "default": False,
                        "description": "If true, keep the local name as an alias on the canonical item.",
                    },
                    "local_keep_name": {"type": "string", "description": "Optional alias name to record."},
                    "reason": {"type": "string", "description": "Optional reason / audit note."},
                },
                "required": ["warehouse_code", "local_item_id", "canonical_id"],
            },
        ),
        domain='canonical', handler=canonical.canonical_claim_impl,
        access=AccessContract('POST', '/api/v1/canonical/claim', 'handler', 'warehouse_code checked in service'),
        output_shape=obj(warehouse_code=STRING, local_item_id=INTEGER, canonical_id=INTEGER, request_id=INTEGER),
        external_subset=False, bot_approved=False,
        tests=("tests/test_mcp_registry.py::test_registered_tool_real_serialized_shape[canonical_claim]",),
    ),
    ToolRegistration(
        tool=Tool(
            name="canonical_unclaim",
            title="Unclaim Item from Canonical",
            description=(
                "Unbind one items row from its canonical item. Symmetric to claim_item. "
                "Returns the unclaim receipt."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "warehouse_code": {"type": "string"},
                    "local_item_id": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["warehouse_code", "local_item_id"],
            },
        ),
        domain='canonical', handler=canonical.canonical_unclaim_impl,
        access=AccessContract('POST', '/api/v1/canonical/unclaim', 'handler', 'warehouse_code checked in service'),
        output_shape=obj(warehouse_code=STRING, local_item_id=INTEGER, canonical_id=INTEGER, action=STRING),
        external_subset=False, bot_approved=False,
        tests=("tests/test_mcp_registry.py::test_registered_tool_real_serialized_shape[canonical_unclaim]",),
    ),
    ToolRegistration(
        tool=Tool(
            name="canonical_fanout",
            title="Fanout Canonical Items",
            description=(
                "Fanout canonical item fields to target warehouses (M2 关键路径). "
                "Q4=freeze: if local field diverges from last_synced and not force, "
                "the field is frozen. Returns {event_id, status, total_written, "
                "total_frozen, warehouses: [{code, status, error_message}], "
                "items: [...]}. warehouse_codes MUST include at least one "
                "distribution_center, otherwise new items cannot be ordered (P0-10)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "canonical_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Canonical item ids to publish.",
                    },
                    "warehouse_codes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Target warehouse codes. Must include at least one distribution_center.",
                    },
                    "action": {
                        "type": "string",
                        "enum": ["overwrite", "skip"],
                        "default": "overwrite",
                    },
                    "force": {
                        "type": "boolean",
                        "default": False,
                        "description": "Force override frozen fields (Q4=freeze bypass).",
                    },
                    "summary": {"type": "string", "description": "Optional event summary."},
                },
                "required": ["canonical_ids", "warehouse_codes"],
            },
        ),
        domain='canonical', handler=canonical.canonical_fanout_impl,
        access=AccessContract('POST', '/api/v1/canonical/fanout', 'handler', 'explicit warehouse_codes; no token warehouse filtering; distribution center required'),
        output_shape=obj(event_id=INTEGER, per_canonical=array(ROW), total_written=INTEGER, total_frozen=INTEGER, status=STRING),
        external_subset=False, bot_approved=False,
        tests=("tests/test_mcp_registry.py::test_registered_tool_real_serialized_shape[canonical_fanout]",),
    ),
    ToolRegistration(
        tool=Tool(
            name="canonical_resolve_conflict",
            title="Resolve Canonical Conflict",
            description=(
                "Resolve one open canonical_conflict. decision ∈ "
                "{'keep_local', 'accept_canonical', 'waive'}. Returns "
                "{conflict_id, status}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "conflict_id": {"type": "integer"},
                    "decision": {
                        "type": "string",
                        "enum": ["keep_local", "accept_canonical", "waive"],
                    },
                    "note": {"type": "string", "description": "Optional resolution note."},
                },
                "required": ["conflict_id", "decision"],
            },
        ),
        domain='canonical', handler=canonical.canonical_resolve_conflict_impl,
        access=AccessContract('POST', '/api/v1/canonical/conflicts/<id>/resolve', 'handler', 'global; no token warehouse filtering'),
        output_shape=obj(conflict_id=INTEGER, status=STRING),
        external_subset=False, bot_approved=False,
        tests=("tests/test_mcp_registry.py::test_registered_tool_real_serialized_shape[canonical_resolve_conflict]",),
    ),
    ToolRegistration(
        tool=Tool(
            name="canonical_set_binding_active",
            title="Set Warehouse Binding Active",
            description=(
                "P0-10 — Toggle a per-warehouse binding row's is_active flag. "
                "Only writes items.is_active + updated_at; inventory, history "
                "and master data fields are untouched. Returns "
                "{item_id, warehouse_code, canonical_id, active, quantity}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "warehouse_code": {"type": "string"},
                    "canonical_id": {"type": "integer"},
                    "active": {"type": "boolean"},
                },
                "required": ["warehouse_code", "canonical_id", "active"],
            },
        ),
        domain='canonical', handler=canonical.canonical_set_binding_active_impl,
        access=AccessContract('POST', '/api/v1/canonical/binding-active', 'handler', 'warehouse_code checked in service'),
        output_shape=obj(item_id=INTEGER, warehouse_code=STRING, canonical_id=INTEGER, active=BOOLEAN, quantity=NUMBER),
        external_subset=False, bot_approved=False,
        tests=("tests/test_mcp_registry.py::test_registered_tool_real_serialized_shape[canonical_set_binding_active]",),
    ),
    ToolRegistration(
        tool=Tool(
            name="store_order_list",
            title="List Store Orders",
            description=(
                "List store orders (store ordering / DC transfer documents) visible to "
                "this token. Read-only. Visibility is by participating warehouse: an "
                "order is visible when the token is allowed on its store warehouse or "
                "on ANY participating warehouse (a line's source warehouse, with the "
                "header DC as legacy fallback). ACL expansion (explicit): a token "
                "allowed on any participating warehouse can read the full order, "
                "including line items fulfilled by other warehouses and their amounts. "
                "Optional filters: warehouse_code (matches the store side or any "
                "participating warehouse of an order), status, limit, offset. Returns: "
                "{orders: [{id, order_no, store_warehouse_code, "
                "dc_warehouse_code, flow_type, status, requested_by, expected_delivery_date, "
                "created_at, updated_at, ...}], total, limit, offset, truncated}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "warehouse_code": {
                        "type": "string",
                        "description": (
                            "Optional warehouse code filter; matches an order whose "
                            "store warehouse or any participating warehouse is this "
                            "warehouse (e.g. wh_000)."
                        ),
                    },
                    "status": {
                        "type": "string",
                        "enum": [
                            "pending",
                            "approved",
                            "rejected",
                            "shipped",
                            "delivered",
                            "cancelled",
                        ],
                        "description": "Optional order status filter.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 500,
                        "default": 200,
                        "description": "Page size (1-500, default 200).",
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 0,
                        "description": "Page offset (default 0).",
                    },
                },
                "required": [],
            },
        ),
        domain='store_order', handler=store_order.store_order_list_impl,
        access=AccessContract('GET', '/api/v1/store-orders', 'handler; exact path or path plus slash', 'any participating warehouse grants full order visibility, including other warehouses; optional warehouse filter'),
        output_shape=page("orders", obj(id=INTEGER, order_no=STRING, store_warehouse_code=STRING, dc_warehouse_code=STRING, status=STRING)),
        external_subset=True, bot_approved=True,
        tests=("tests/test_mcp_registry.py::test_registered_tool_real_serialized_shape[store_order_list]",),
    ),
    ToolRegistration(
        tool=Tool(
            name="store_order_detail",
            title="Get Store Order Detail",
            description=(
                "Get one store order with its header, line items, deliveries, receipts "
                "and status history. Read-only. Visible when the token is allowed on the "
                "store warehouse or on ANY participating warehouse; ACL expansion "
                "(explicit): such a token can read the full order, including line items "
                "fulfilled by other warehouses, their amounts and the receipt history. "
                "Returns: {order: {id, order_no, "
                "store_warehouse_code, dc_warehouse_code, flow_type, status, ...}, "
                "items: [{id, canonical_id, canonical_name, dc_item_id, dc_item_bound, "
                "source_warehouse_code, quantity, fulfilled_quantity, shipped_quantity, "
                "unit, status}], deliveries: [{..., warehouse_code}], receipts: [...], "
                "status_history: [...]}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "integer",
                        "description": "Store order id (store_orders.id).",
                    },
                },
                "required": ["order_id"],
            },
        ),
        domain='store_order', handler=store_order.store_order_detail_impl,
        access=AccessContract('GET', '/api/v1/store-orders/<id>', 'handler; exact path or path plus slash', 'any participating warehouse grants full order visibility, including other warehouses'),
        output_shape=obj(order=obj(id=INTEGER, order_no=STRING, status=STRING), items=array(ROW), deliveries=array(ROW), receipts=array(ROW), status_history=array(ROW)),
        external_subset=True, bot_approved=True,
        tests=("tests/test_mcp_registry.py::test_registered_tool_real_serialized_shape[store_order_detail]",),
    ),
    ToolRegistration(
        tool=Tool(
            name="wms_stock_list",
            title="List WMS Stock (external)",
            description="Read-only: current stock of this tenant in the external WMS (Puyu cold store, SQL Server). Returns: array of {goods_code, goods_name, unit, qty, warehouse, zone, position, lot_no}.",
            inputSchema={
                "type": "object",
                "properties": {
                    "goods_code": {
                        "type": "string",
                        "description": "Optional goods code filter, e.g. SC0001",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max rows (default 100, max 500)",
                    },
                },
                "required": [],
            },
        ),
        domain='wms', handler=wms.wms_stock_list_impl,
        access=AccessContract('GET', '/api/v1/wms/stock', 'handler', 'tenant fixed by server config; caller cannot widen it'),
        output_shape=array(obj(goods_code=STRING, goods_name=STRING, unit=STRING, qty=NUMBER)),
        external_subset=True, bot_approved=False,
        tests=("tests/test_mcp_registry.py::test_registered_tool_real_serialized_shape[wms_stock_list]",),
    ),
    ToolRegistration(
        tool=Tool(
            name="wms_stock_summary",
            title="Summarize WMS Stock (external)",
            description="Read-only: stock totals per warehouse for this tenant in the external WMS. Returns: array of {warehouse, sku_count, total_qty}.",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        domain='wms', handler=wms.wms_stock_summary_impl,
        access=AccessContract('GET', '/api/v1/wms/stock/summary', 'handler', 'tenant fixed by server config; caller cannot widen it'),
        output_shape=array(obj(warehouse=STRING, sku_count=INTEGER, total_qty=NUMBER)),
        external_subset=True, bot_approved=False,
        tests=("tests/test_mcp_registry.py::test_registered_tool_real_serialized_shape[wms_stock_summary]",),
    ),
    ToolRegistration(
        tool=Tool(
            name="wms_documents_list",
            title="List WMS Documents (external)",
            description="Read-only: inbound/outbound documents of this tenant in the external WMS. Returns: array of {doc_no, biz_date, warehouse, qty, supplier|receiver, issued_by}.",
            inputSchema={
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "enum": ["in", "out"],
                        "description": "in = inbound (biz_storeIn), out = outbound (biz_storeOut)",
                    },
                    "date_from": {
                        "type": "string",
                        "description": "Optional lower bound, YYYY-MM-DD",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max rows (default 100, max 500)",
                    },
                },
                "required": ["direction"],
            },
        ),
        domain='wms', handler=wms.wms_documents_list_impl,
        access=AccessContract('GET', '/api/v1/wms/documents', 'handler', 'tenant fixed by server config; caller cannot widen it'),
        output_shape=array(obj(doc_no=STRING, biz_date=STRING, qty=NUMBER)),
        external_subset=True, bot_approved=False,
        tests=("tests/test_mcp_registry.py::test_registered_tool_real_serialized_shape[wms_documents_list]",),
    ),
)


def validate_registry(registrations: tuple[ToolRegistration, ...] | None = None) -> None:
    """Reject duplicate names, unbound handlers and incomplete metadata early."""
    if registrations is None:
        registrations = REGISTRY
    names = set()
    handlers = set()
    for entry in registrations:
        if entry.name in names:
            raise ValueError(f"duplicate tool name: {entry.name}")
        names.add(entry.name)
        if (
            not callable(entry.handler)
            or entry.handler.__name__ != entry.name + "_impl"
            or entry.handler.__module__ != "mcp_server.protocol.tools." + entry.domain
        ):
            raise ValueError(f"invalid handler binding: {entry.name}")
        if entry.handler in handlers:
            raise ValueError(f"duplicate handler: {entry.name}")
        handlers.add(entry.handler)
        if not entry.tests or not entry.compatibility or not entry.output_shape:
            raise ValueError(f"incomplete contract: {entry.name}")
        if entry.access.method not in {"GET", "POST"} or not all((
            entry.access.path.startswith("/api/v1/"),
            entry.access.path_enforcement, entry.access.warehouse_scope,
        )):
            raise ValueError(f"incomplete access contract: {entry.name}")


def get_tools() -> list[Tool]:
    """Keep declarations independent between callers, as before the registry."""
    return [entry.tool.model_copy(deep=True) for entry in REGISTRY]


def get_handlers() -> dict[str, Callable]:
    return {entry.name: entry.handler for entry in REGISTRY}


validate_registry()
