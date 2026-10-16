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
        # ────────────────────────────────────────────────────────────
        #  品项主数据（canonical）工具 — 见 mcp_server/protocol/tools/canonical.py
        # ────────────────────────────────────────────────────────────
        Tool(
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
        Tool(
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
        Tool(
            name="canonical_categories",
            title="List Canonical Categories",
            description=(
                "List canonical categories with per-category item counts (for filter dropdowns). "
                "Returns: array of {code, name, item_count}."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="canonical_fanout_targets",
            title="List Fanout Target Warehouses",
            description=(
                "List warehouses eligible as fanout targets: storefront + rd + distribution_center. "
                "Returns: array of {code, name, warehouse_type}."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
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
        Tool(
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
        Tool(
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
        Tool(
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
        Tool(
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
        Tool(
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
        Tool(
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
        Tool(
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
        Tool(
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
    ]
    return tools
