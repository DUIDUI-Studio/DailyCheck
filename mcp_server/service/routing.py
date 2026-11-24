"""Scoped stockout reads and transitions on the same master boundary as orders."""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import nullcontext
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import config
from blueprints.fulfillment_pure import (
    FulfillmentError,
    FulfillmentNotOrderableError,
    resolve_orderable_fulfillment_item,
)
from blueprints.item_routing_pure import get_routing, stockout_state
from mcp_server.data.unit_of_work import (
    policy_master_connection,
    policy_read_connection,
    routing_write_connection,
)
from mcp_server.infra.errors import ForbiddenError, ValidationError, WriteError
from mcp_server.service.policy import BoundPolicy, load_binding
from routing_mode import routing_modes
from routing_writer_fence import routing_writer_fence
from warehouse_paths import resolve_warehouse_path


def _target(conn, policy, action, cid, code):
    live = load_binding(conn, policy.token_id)
    if live != policy or not live.routing or not live.fields(action):
        raise ForbiddenError("routing_authorization_required")
    scope = live.routing
    if (cid, code) not in scope.pairs:
        raise ForbiddenError("routing_pair_not_authorized")
    canonical = conn.execute(
        """SELECT id, canonical_sku, name, status, unit, gram_per_unit, aux_unit, aux_rate
           FROM canonical_items WHERE tenant_id=? AND id=?""", (scope.tenant_id, cid),
    ).fetchone()
    warehouse = conn.execute(
        """SELECT db_path FROM warehouses WHERE tenant_id=? AND code=?
           AND warehouse_type='distribution_center'""", (scope.tenant_id, code),
    ).fetchone()
    if not canonical or not warehouse:
        raise ForbiddenError("routing_pair_not_authorized")
    return dict(canonical), warehouse["db_path"]


def _binding(conn, cid, code, path):
    path = resolve_warehouse_path(path, config.BASE_DIR, db_dir=config.DB_DIR).resolve()
    with policy_read_connection(path) as warehouse:
        warehouse.execute("BEGIN")
        rows = [dict(row) for row in warehouse.execute(
            """SELECT id, sku, unit, is_active, is_alias, is_orderable
               FROM items WHERE canonical_id=? ORDER BY id""", (cid,),
        )]
        try:
            resolve_orderable_fulfillment_item(conn, warehouse, canonical_id=cid, warehouse_code=code)
        except FulfillmentNotOrderableError:
            return rows, "warehouse_not_orderable"
        except FulfillmentError:
            return rows, "binding_unavailable"
        return rows, "eligible"


def read_routing(policy: BoundPolicy, fields, *, canonical_id, warehouse_code):
    with policy_master_connection() as conn:
        conn.execute("BEGIN")
        canonical, path = _target(conn, policy, "routing.read", canonical_id, warehouse_code)
        result = {}
        if "canonical" in fields:
            result["canonical"] = canonical
        reason = None
        if "binding" in fields or "routing" in fields:
            rows, reason = _binding(conn, canonical_id, warehouse_code, path)
            if "binding" in fields:
                result["binding"] = {"warehouse_code": warehouse_code, "items": rows}
        modes = None
        if "mode" in fields or "routing" in fields:
            modes = routing_modes(config.MASTER_DB, schema_version=conn.execute("PRAGMA user_version").fetchone()[0])
        if "routing" in fields:
            source = get_routing(conn, canonical_id)
            version, pause_id = stockout_state(conn, canonical_id)
            if source != warehouse_code:
                reason = "not_configured_source"
            elif pause_id:
                reason = "stockout_paused"
            elif canonical["status"] != "active":
                reason = "canonical_not_active"
            elif modes["web_multi_warehouse"] is not True:
                reason = "web_routed_mode_unconfirmed"
            history = conn.execute(
                """SELECT COUNT(DISTINCT o.id) FROM store_order_items i
                   JOIN store_orders o ON o.id=i.order_id
                   WHERE i.canonical_id=? AND (i.source_warehouse_code=?
                     OR (i.source_warehouse_code IS NULL AND o.dc_warehouse_code=?))
                     AND o.flow_type='store_order' AND o.status IN ('pending', 'approved', 'shipped')""",
                (canonical_id, warehouse_code, warehouse_code),
            ).fetchone()[0]
            result["routing"] = {
                "configured_source": warehouse_code if source == warehouse_code else None,
                "effective_source": warehouse_code if reason == "eligible" else None,
                "orderable": reason == "eligible", "reason": reason,
                "version": version, "pause_id": pause_id if source == warehouse_code else None,
                "availability": "manual_recovery_confirmation_required" if pause_id and source == warehouse_code
                else "not_measured",
                "existing_open_orders": history,
            }
        if "mode" in fields:
            result["mode"] = modes
        return result


def _timestamp(raw):
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if "T" not in raw or value.utcoffset() is None:
            raise ValueError
    except ValueError as exc:
        raise ValidationError("evidence_requires_timestamp_with_timezone") from exc
    return value.astimezone(timezone.utc)


def _confirmed_evidence(evidence, operation, cid, code, unit, pause, when):
    if (evidence["canonical_id"], evidence["warehouse_code"], evidence["unit"]) != (cid, code, unit):
        raise ValidationError("evidence_target_mismatch")
    if evidence.get("pause_id") != (pause["id"] if pause else None):
        raise ValidationError("evidence_pause_mismatch")
    raw = evidence["quantity"]
    if not re.fullmatch(r"[0-9]{1,12}(?:\.[0-9]{1,2})?", raw):
        raise ValidationError("evidence_requires_base_unit_quantity")
    qty = Decimal(raw)
    if (operation == "pause" and qty != 0) or (operation == "restore" and qty <= 0):
        raise ValidationError("evidence_quantity_does_not_support_decision")
    observed = _timestamp(evidence["observed_at"])
    if observed > when or (pause is not None and observed <= _timestamp(pause["decided_at"])):
        raise ValidationError("evidence_time_does_not_support_decision")
    reference = evidence["reference"].strip()
    if not reference:
        raise ValidationError("evidence_reference_required")
    return observed.isoformat(), str(qty), reference


def _receipt(event):
    op = event["operation"]
    return {
        "event_id": event["id"], "pause_id": event["pause_id"] or event["id"],
        "canonical_id": event["canonical_id"], "warehouse_code": event["warehouse_code"],
        "actor_id": event["actor_id"], "decided_at": event["decided_at"],
        "operation": op, "before_state": "open" if op == "pause" else "paused",
        "after_state": "paused" if op == "pause" else "open",
        "before_version": event["before_version"], "version": event["id"],
        "evidence": {
            "kind": event["evidence_kind"], "reference": event["evidence_ref"],
            "observed_at": event["evidence_at"], "quantity": event["evidence_qty"],
            "unit": event["evidence_unit"],
        },
    }


def change_routing(policy: BoundPolicy, operation: str, arguments: dict):
    cid, code = arguments["canonical_id"], arguments["warehouse_code"]
    evidence = arguments["evidence"]
    idem = arguments["idempotency_key"]
    request_json = json.dumps({"operation": operation, **arguments}, sort_keys=True, separators=(",", ":"))
    try:
        fence = routing_writer_fence() if config.IS_PRODUCTION else nullcontext()
        with fence, routing_write_connection() as conn:
            canonical, path = _target(conn, policy, f"routing.{operation}", cid, code)
            modes = routing_modes(config.MASTER_DB, schema_version=conn.execute("PRAGMA user_version").fetchone()[0])
            if not modes["writes_enabled"]:
                raise WriteError("routing_mode_unavailable", "Web and MCP must confirm matching enabled live mode")
            prior = conn.execute(
                "SELECT * FROM item_routing_stockout_events WHERE token_id=? AND idempotency_key=?",
                (policy.token_id, idem),
            ).fetchone()
            if prior:
                if prior["request_json"] != request_json:
                    raise WriteError("idempotency_conflict", "Key already bound to a different request")
                return _receipt(prior)
            if get_routing(conn, cid) != code:
                raise WriteError("original_source_required", "Operation requires the original configured warehouse")
            version, pause_id = stockout_state(conn, cid)
            if arguments["expected_version"] != version:
                raise WriteError("routing_version_conflict", "Read the latest route before retrying")
            if operation == "pause" and pause_id:
                raise WriteError("already_paused", "A stockout pause is already active")
            if operation == "restore" and (not pause_id or arguments["pause_id"] != pause_id):
                raise WriteError("active_pause_required", "Restore requires the current pause record")
            pause = conn.execute(
                "SELECT * FROM item_routing_stockout_events WHERE id=? AND tenant_id=? "
                "AND canonical_id=? AND warehouse_code=? AND operation='pause'",
                (pause_id, policy.routing.tenant_id, cid, code),
            ).fetchone() if pause_id else None
            if pause_id and pause is None:
                raise WriteError("active_pause_required", "Stored pause record does not match")
            when = datetime.now(timezone.utc)
            observed, qty, reference = _confirmed_evidence(
                evidence, operation, cid, code, canonical["unit"], pause, when,
            )
            if conn.execute(
                "SELECT 1 FROM item_routing_stockout_events WHERE tenant_id=? AND canonical_id=? "
                "AND warehouse_code=? AND evidence_kind=? AND evidence_ref=?",
                (policy.routing.tenant_id, cid, code, evidence["kind"], reference),
            ).fetchone():
                raise WriteError("evidence_already_used", "Provide evidence for this decision")
            _, reason = _binding(conn, cid, code, path)
            if canonical["status"] != "active" or reason != "eligible":
                raise WriteError("routing_binding_unavailable", "Current source binding cannot fulfill the item")

            event_id = uuid4().hex
            conn.execute(
                """INSERT INTO item_routing_stockout_events
                   (id, tenant_id, canonical_id, warehouse_code, token_id, actor_id,
                    idempotency_key, operation, pause_id, before_version, decided_at,
                    evidence_kind, evidence_ref, evidence_at, evidence_qty, evidence_unit, request_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (event_id, policy.routing.tenant_id, cid, code, policy.token_id, policy.routing.actor_id,
                 idem, operation, pause_id, version, when.isoformat(), evidence["kind"],
                 reference, observed, qty, evidence["unit"], request_json),
            )
            conn.execute(
                """INSERT INTO item_routing_stockout_state (tenant_id, canonical_id, version, pause_id)
                   VALUES (?, ?, ?, ?) ON CONFLICT (tenant_id, canonical_id) DO UPDATE
                   SET version=excluded.version, pause_id=excluded.pause_id""",
                (policy.routing.tenant_id, cid, event_id, event_id if operation == "pause" else None),
            )
            event = conn.execute(
                "SELECT * FROM item_routing_stockout_events WHERE id=?", (event_id,),
            ).fetchone()
            return _receipt(event)
    except (OSError, sqlite3.OperationalError) as exc:
        raise WriteError("routing_storage_unavailable", "Routing state temporarily unavailable", 503) from exc
