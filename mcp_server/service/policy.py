"""Policy vocabulary and the read-only decision for a bound token."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

POLICY_TOKEN_PREFIX = "mp."
MAX_SQLITE_ID = 2**63 - 1
WMS_LINES_ACTION = "wms.document_lines.read"
WMS_LINES_FIELDS = ("goods_code", "goods_name", "qty", "unit")
PUYU_WAREHOUSE_ID = 3

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
    WMS_LINES_ACTION: WMS_LINES_FIELDS,
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
    if WMS_LINES_ACTION in result and result != {WMS_LINES_ACTION: list(WMS_LINES_FIELDS)}:
        raise ValueError("WMS lines require a standalone preset with the fixed projection")
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
    wms_warehouse_id: int | None = None

    def fields(self, action: str | None) -> tuple[str, ...]:
        return next((fields for permitted, fields in self.grants if permitted == action), ())


def binding_scope(conn: sqlite3.Connection, grants: dict, selection: object) -> list[str] | dict:
    """Storefront scopes keep their original list. External WMS uses a distinct snapshot."""
    if WMS_LINES_ACTION not in grants:
        return list(storefront_scope(conn, selection))
    if (
        not isinstance(selection, dict) or selection.keys() != {"wms_warehouse_id"}
        or type(selection["wms_warehouse_id"]) is not int
        or selection["wms_warehouse_id"] != PUYU_WAREHOUSE_ID
    ):
        raise ValueError("Invalid external WMS warehouse selection")
    return {"wms_warehouse_id": PUYU_WAREHOUSE_ID}


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
        scope = binding_scope(conn, grants, json.loads(row["warehouse_codes_json"]))
    except (TypeError, ValueError):
        return None
    return BoundPolicy(
        token_id, tuple(scope) if isinstance(scope, list) else (),
        tuple((action, tuple(fields)) for action, fields in grants.items()),
        scope["wms_warehouse_id"] if isinstance(scope, dict) else None,
    )
