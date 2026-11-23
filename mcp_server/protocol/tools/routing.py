"""Registry-bound adapters for exact routing policy tools."""
from __future__ import annotations

from mcp_server.service import routing
from mcp_server.service.policy import MAX_SQLITE_ID

ID = {"type": "integer", "minimum": 1, "maximum": MAX_SQLITE_ID}


def _string(limit):
    return {"type": "string", "minLength": 1, "maxLength": limit}


def _object(**properties):
    return {
        "type": "object", "properties": properties,
        "required": list(properties), "additionalProperties": False,
    }


_TARGET = {"canonical_id": ID, "warehouse_code": _string(128)}
READ_INPUT = _object(**_TARGET)


def _write_input(operation):
    pause = {"pause_id": _string(32)} if operation == "restore" else {}
    return _object(
        **_TARGET, **pause, expected_version=_string(32), idempotency_key=_string(128),
        evidence=_object(
            **_TARGET, **pause,
            kind={"type": "string", "enum": ["manual_fulfillment_confirmation"]},
            reference=_string(256), observed_at=_string(40), quantity=_string(15), unit=_string(32),
        ),
    )


PAUSE_INPUT = _write_input("pause")
RESTORE_INPUT = _write_input("restore")

def routing_item_status_impl(policy, fields, **arguments):
    return routing.read_routing(policy, fields, **arguments)


def routing_pause_item_impl(policy, fields, **arguments):
    return routing.change_routing(policy, "pause", arguments)


def routing_restore_item_impl(policy, fields, **arguments):
    return routing.change_routing(policy, "restore", arguments)
