"""Declaration and handler for the policy-only production read."""
from __future__ import annotations

from mcp_server.service.policy import MAX_SQLITE_ID
from mcp_server.service.production_consumption import (
    DEFAULT_LIMIT,
    MAX_CURSOR_LENGTH,
    MAX_LIMIT,
    VIEW_FIELDS,
    read_production_consumption,
)

_DATE = {"type": "string", "format": "date", "maxLength": 10}
_ID = {"type": "integer", "minimum": 1, "maximum": MAX_SQLITE_ID}
INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "start_date": {**_DATE, "description": "Inclusive Beijing date (YYYY-MM-DD)."},
        "end_date": {**_DATE, "description": "Exclusive Beijing date (YYYY-MM-DD)."},
        "view": {"type": "string", "enum": list(VIEW_FIELDS)},
        "warehouse_code": {
            "type": "string", "maxLength": 32,
            "description": "One granted storefront. Omit to read all granted storefronts separately.",
        },
        "item_id": _ID,
        "run_id": {**_ID, "description": "Local production batch ID, lines view only."},
        "line_id": {**_ID, "description": "Local production line ID, lines view only."},
        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": DEFAULT_LIMIT},
        "cursor": {"type": "string", "maxLength": MAX_CURSOR_LENGTH},
    },
    "required": ["start_date", "end_date", "view"],
    "additionalProperties": False,
}


def production_consumption_impl(policy, fields, **arguments):
    return read_production_consumption(policy, fields, **arguments)
