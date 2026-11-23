"""Policy vocabulary and the read-only decision for a bound token."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

POLICY_TOKEN_PREFIX = "mp."
MAX_SQLITE_ID = 2**63 - 1

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


ROUTING_FIELDS = {
    "routing.read": ("canonical", "binding", "routing", "mode"),
    "routing.pause": ("receipt",),
    "routing.restore": ("receipt",),
}


def canonical_grants(grants: object, *, routing: bool = False) -> dict[str, list[str]]:
    """Validate a nonempty subset of actions and a nonempty mask per action."""
    vocabulary = ROUTING_FIELDS if routing else SAFE_FIELDS
    if not isinstance(grants, dict) or not grants or grants.keys() - vocabulary.keys():
        raise ValueError("Invalid policy actions")
    result = {}
    for action, fields in grants.items():
        allowed = vocabulary[action]
        if (
            not isinstance(fields, list) or not fields
            or not all(isinstance(field, str) and field in allowed for field in fields)
            or len(fields) != len(set(fields))
        ):
            raise ValueError("Invalid policy fields")
        result[action] = [field for field in allowed if field in fields]
    return result


def encode_grants(grants: object, *, routing: bool = False) -> str:
    return json.dumps(canonical_grants(grants, routing=routing), sort_keys=True, separators=(",", ":"))


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
class RoutingScope:
    tenant_id: int
    actor_id: int
    pairs: frozenset[tuple[int, str]]


def approved_routing_pairs(conn, tenant_id, actor_id) -> dict[tuple[int, str], str]:
    return {(row[0], row[1]): row[2] for row in conn.execute(
        """SELECT a.canonical_id, a.warehouse_code, a.id FROM mcp_policy_routing_approvals a
           JOIN mcp_policy_routing_principals p ON p.id=a.principal_id AND p.revoked_at IS NULL
           JOIN users u ON u.id=p.actor_id AND u.tenant_id=p.tenant_id AND u.is_active=1
           JOIN tenants t ON t.id=p.tenant_id AND t.status='active'
           WHERE p.tenant_id=? AND p.actor_id=? AND a.revoked_at IS NULL""",
        (tenant_id, actor_id),
    )}

def _routing_scope(conn: sqlite3.Connection, token_id: int) -> RoutingScope | None:
    row = conn.execute(
        """SELECT s.tenant_id, s.actor_id, s.pairs_json FROM mcp_policy_routing_scopes s
           WHERE s.token_id=? AND s.tenant_id=1""", (token_id,),
    ).fetchone()
    if row is None:
        return None
    pairs = json.loads(row["pairs_json"])
    if not isinstance(pairs, list) or not pairs:
        return None
    scope = set()
    approved = approved_routing_pairs(conn, row["tenant_id"], row["actor_id"])
    for pair in pairs:
        if not isinstance(pair, list) or len(pair) != 3:
            return None
        item, code, approval = pair
        if (type(item) is not int or not 0 < item <= MAX_SQLITE_ID
                or not isinstance(code, str) or not code or code.strip() != code or "*" in code
                or (item, code) in scope or not isinstance(approval, str)
                or approved.get((item, code)) != approval):
            return None
        scope.add((item, code))
    return RoutingScope(row["tenant_id"], row["actor_id"], frozenset(scope))


@dataclass(frozen=True)
class BoundPolicy:
    token_id: int
    warehouses: tuple[str, ...]
    grants: tuple[tuple[str, tuple[str, ...]], ...]
    routing: RoutingScope | None = None

    def fields(self, action: str | None) -> tuple[str, ...]:
        return next((fields for permitted, fields in self.grants if permitted == action), ())


def load_binding(conn: sqlite3.Connection, token_id: int) -> BoundPolicy | None:
    row = conn.execute(
        "SELECT b.preset_version, b.grants_json, b.warehouse_codes_json, p.grants_json AS preset_grants "
        "FROM mcp_policy_bindings b JOIN mcp_policy_presets p "
        "ON p.name = b.preset_name AND p.version = b.preset_version "
        "JOIN agent_tokens a ON a.id=b.token_id "
        "WHERE b.token_id = ? AND b.revoked_at IS NULL AND p.disabled_at IS NULL AND a.revoked_at IS NULL",
        (token_id,),
    ).fetchone()
    if row is None or type(row["preset_version"]) is not int or row["preset_version"] <= 0:
        return None
    try:
        raw = json.loads(row["grants_json"])
        routing = isinstance(raw, dict) and bool(raw.keys() & ROUTING_FIELDS.keys())
        grants = canonical_grants(raw, routing=routing)
        if row["grants_json"] != encode_grants(grants, routing=routing) or row["preset_grants"] != row["grants_json"]:
            return None
        scope = _routing_scope(conn, token_id) if routing else None
        if routing and (scope is None or row["warehouse_codes_json"] != "[]"):
            return None
        warehouses = () if routing else storefront_scope(conn, json.loads(row["warehouse_codes_json"]))
    except (TypeError, ValueError):
        return None
    return BoundPolicy(token_id, warehouses, tuple((action, tuple(fields)) for action, fields in grants.items()), scope)
