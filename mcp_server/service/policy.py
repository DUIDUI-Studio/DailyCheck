"""Policy vocabulary and the read-only decision for a bound token."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

POLICY_TOKEN_PREFIX = "mp."
MAX_SQLITE_ID = 2**63 - 1
TOOL_ACTIONS = {"items_detail": "inventory.read", "store_order_list": "orders.read"}

# Values are trusted SQL fragments. An administrator selects keys only.
INVENTORY_COLUMNS = {"sku": "sku", "current_stock": "quantity AS current_stock",
                     "unit": "unit", "updated_at": "updated_at"}
ORDER_COLUMNS = {
    key: key for key in ("order_no", "store_warehouse_code", "status", "created_at", "updated_at",
                         "expected_delivery_date", "approved_at", "delivered_at", "cancelled_at")
}
SAFE_FIELDS = {
    "inventory.read": tuple(INVENTORY_COLUMNS),
    "orders.read": (*ORDER_COLUMNS, "fulfillment"),
}


def canonical_grants(grants: object) -> dict[str, list[str]]:
    """Validate a nonempty subset of actions and a nonempty mask per action."""
    if not isinstance(grants, dict) or not grants or grants.keys() - SAFE_FIELDS.keys():
        raise ValueError("Invalid policy actions")
    result = {}
    for action, fields in grants.items():
        allowed = SAFE_FIELDS[action]
        if (
            not isinstance(fields, list) or not fields
            or not all(isinstance(field, str) and field in allowed for field in fields)
            or len(fields) != len(set(fields))
        ):
            raise ValueError("Invalid policy fields")
        result[action] = [field for field in allowed if field in fields]
    return result


def encode_grants(grants: object) -> str:
    return json.dumps(canonical_grants(grants), sort_keys=True, separators=(",", ":"))


def storefront_scope(conn: sqlite3.Connection, codes: object) -> tuple[str, ...]:
    if (
        not isinstance(codes, list) or not codes
        or not all(isinstance(code, str) and code and code.strip() == code and "*" not in code
                   for code in codes)
        or len(codes) != len(set(codes))
    ):
        raise ValueError("Invalid storefront selection")
    placeholders = ", ".join("?" for _ in codes)
    rows = conn.execute(
        f"SELECT code FROM warehouses WHERE code COLLATE BINARY IN ({placeholders}) "
        "AND warehouse_type = 'storefront'", codes,
    )
    if {row[0] for row in rows} != set(codes):
        raise ValueError("Invalid storefront selection")
    return tuple(sorted(codes))


@dataclass(frozen=True)
class BoundPolicy:
    token_id: int
    warehouses: tuple[str, ...]
    grants: tuple[tuple[str, tuple[str, ...]], ...]

    def fields(self, tool_name: str) -> tuple[str, ...]:
        action = TOOL_ACTIONS.get(tool_name)
        return next((fields for permitted, fields in self.grants if permitted == action), ())


def load_binding(conn: sqlite3.Connection, token_id: int) -> BoundPolicy | None:
    row = conn.execute(
        "SELECT b.preset_version, b.grants_json, b.warehouse_codes_json, p.grants_json AS preset_grants "
        "FROM mcp_policy_bindings b JOIN mcp_policy_presets p "
        "ON p.name = b.preset_name AND p.version = b.preset_version "
        "WHERE b.token_id = ? AND b.revoked_at IS NULL AND p.disabled_at IS NULL",
        (token_id,),
    ).fetchone()
    if row is None or type(row["preset_version"]) is not int or row["preset_version"] <= 0:
        return None
    try:
        grants = canonical_grants(json.loads(row["grants_json"]))
        if row["grants_json"] != encode_grants(grants) or row["preset_grants"] != row["grants_json"]:
            return None
        warehouses = storefront_scope(conn, json.loads(row["warehouse_codes_json"]))
    except (TypeError, ValueError):
        return None
    return BoundPolicy(token_id, warehouses, tuple((action, tuple(fields)) for action, fields in grants.items()))
