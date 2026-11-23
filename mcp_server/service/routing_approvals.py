"""One source-referenced authorization decision per confirmed host command."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4

import config
from blueprints.fulfillment_pure import resolve_orderable_fulfillment_item
from blueprints.item_routing_pure import get_routing, stockout_state
from mcp_server.data.unit_of_work import policy_read_connection
from mcp_server.service.policy import ROUTING_FIELDS, encode_grants
from warehouse_paths import resolve_warehouse_path

# Reserved from the Web management page's legacy preset-name vocabulary.
FIXED_PRESET = "dailycheck.gene-routing"


def _next_preset(conn):
    row = conn.execute(
        "SELECT MAX(version), COUNT(CASE WHEN disabled_at IS NULL THEN 1 END) "
        "FROM mcp_policy_presets WHERE name=?", (FIXED_PRESET,),
    ).fetchone()
    if row[1]:
        raise ValueError("An active fixed routing preset already exists")
    return {"preset_name": FIXED_PRESET, "preset_version": (row[0] or 0) + 1}


def _source_version(conn, db_path, actor, cid, code):
    user = conn.execute(
        "SELECT 1 FROM users WHERE id=? AND tenant_id=1 AND is_active=1", (actor,),
    ).fetchone()
    tenant = conn.execute("SELECT 1 FROM tenants WHERE id=1 AND status='active'").fetchone()
    if user is None or tenant is None:
        raise ValueError("An active Gene subject in tenant 1 is required")
    canonical = conn.execute(
        "SELECT 1 FROM canonical_items WHERE id=? AND tenant_id=1", (cid,),
    ).fetchone()
    if canonical is None:
        raise ValueError("Canonical item must belong to the Gene principal's tenant")
    source = conn.execute(
        "SELECT db_path FROM warehouses WHERE code=? AND tenant_id=1 AND warehouse_type='distribution_center'",
        (code,),
    ).fetchone()
    if source is None or get_routing(conn, cid) != code:
        raise ValueError("The nominated warehouse must be the current original source")
    path = resolve_warehouse_path(source[0], config.BASE_DIR, db_dir=db_path.resolve().parent).resolve()
    with policy_read_connection(path) as warehouse:
        warehouse.execute("BEGIN")
        resolve_orderable_fulfillment_item(conn, warehouse, canonical_id=cid, warehouse_code=code)
    return stockout_state(conn, cid)[0]


def preview(conn, db_path, *, operation, actor, pair, reference):
    """Review the live generation and exact scope, leaving the caller's DB unchanged."""
    if reference != reference.strip() or not 1 <= len(reference) <= 256 or not reference.isprintable():
        raise ValueError("An independent business decision reference is required")
    if operation == "install-preset":
        if actor is not None or pair is not None:
            raise ValueError("Preset registration does not assign a subject or scope")
        return _next_preset(conn)
    if operation not in {"approve", "revoke"} or actor is None or pair is None:
        raise ValueError("One exact Gene subject and warehouse/item pair is required")
    cid, code = pair
    principal = conn.execute(
        "SELECT id, actor_id FROM mcp_policy_routing_principals WHERE tenant_id=1 AND revoked_at IS NULL",
    ).fetchone()
    if principal and principal["actor_id"] != actor:
        raise ValueError("Another Gene subject is already registered")
    principal_id = principal["id"] if principal else None
    history = [tuple(row) for row in conn.execute(
        "SELECT id, revoked_at FROM mcp_policy_routing_approvals "
        "WHERE principal_id=? AND canonical_id=? AND warehouse_code=? ORDER BY id",
        (principal_id, cid, code),
    )]
    approval_id = next((identity for identity, revoked_at in history if revoked_at is None), None)
    if operation == "revoke":
        if approval_id is None:
            raise ValueError("No live approval matches the subject and exact pair")
        return {"approval_id": approval_id}
    if approval_id is not None:
        raise ValueError("The exact pair already has a live approval")
    if conn.execute(
        """SELECT 1 FROM mcp_policy_routing_admin_events e
           JOIN mcp_policy_routing_approvals a ON a.id=e.approval_id
           JOIN mcp_policy_routing_principals p ON p.id=a.principal_id
           WHERE e.operation='approve' AND e.decision_ref=? AND p.tenant_id=1 AND p.actor_id=?
             AND a.canonical_id=? AND a.warehouse_code=?""", (reference, actor, cid, code),
    ).fetchone():
        raise ValueError("A new approval generation requires a new business decision reference")
    # A grant followed by revocation returns to "no active approval", but
    # must change the signed version before a different grant can proceed.
    history_version = hashlib.sha256(json.dumps(history, separators=(",", ":")).encode()).hexdigest()
    return {
        "principal_id": principal_id,
        "history_version": history_version,
        "route_version": _source_version(conn, db_path, actor, cid, code),
    }


def apply(conn, db_path, *, issuer, operation, actor, pair, reference, state, confirmation_id):
    """Apply inside the caller's master write transaction."""
    if conn.execute(
        "SELECT 1 FROM mcp_policy_routing_admin_events WHERE confirmation_id=?", (confirmation_id,),
    ).fetchone():
        raise ValueError("Confirmation already consumed")
    if preview(conn, db_path, operation=operation, actor=actor, pair=pair, reference=reference) != state:
        raise ValueError("Routing authorization changed since preview")

    when = datetime.now(timezone.utc).isoformat()
    approval_id = preset_name = preset_version = None
    if operation == "install-preset":
        preset_name, preset_version = state["preset_name"], state["preset_version"]
        grants = encode_grants({action: list(fields) for action, fields in ROUTING_FIELDS.items()}, routing=True)
        conn.execute(
            "INSERT INTO mcp_policy_presets (name, version, grants_json, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
            (preset_name, preset_version, grants, issuer, when),
        )
        result = state
    elif operation == "approve":
        principal_id = state["principal_id"] or uuid4().hex
        if state["principal_id"] is None:
            conn.execute(
                "INSERT INTO mcp_policy_routing_principals (id, tenant_id, actor_id) VALUES (?, 1, ?)",
                (principal_id, actor),
            )
        approval_id = uuid4().hex
        conn.execute(
            "INSERT INTO mcp_policy_routing_approvals (id, principal_id, canonical_id, warehouse_code) VALUES (?, ?, ?, ?)",
            (approval_id, principal_id, *pair),
        )
        result = {"approval_id": approval_id}
    else:
        approval_id = state["approval_id"]
        conn.execute("UPDATE mcp_policy_routing_approvals SET revoked_at=? WHERE id=?", (when, approval_id))
        result = {"approval_id": approval_id}
    conn.execute(
        """INSERT INTO mcp_policy_routing_admin_events
           (confirmation_id, operation, issuer_id, decision_ref, decided_at, approval_id, preset_name, preset_version)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (confirmation_id, operation, issuer, reference, when, approval_id, preset_name, preset_version),
    )
    return {"operation": operation, **result}
