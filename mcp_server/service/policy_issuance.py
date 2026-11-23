"""Shared atomic insert for the existing bound-token credential format."""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

from werkzeug.security import generate_password_hash

from mcp_server.service.auth import token_prefix
from mcp_server.service.policy import POLICY_TOKEN_PREFIX


def insert_bound_token(
    db, *, name, preset, version, grants_json, warehouses_json, confirmation_id, issuer,
):
    """The caller owns the transaction and has revalidated the confirmed scope."""
    if db.execute(
        "SELECT 1 FROM mcp_policy_bindings WHERE confirmation_id=?", (confirmation_id,),
    ).fetchone() or db.execute(
        "SELECT 1 FROM mcp_policy_bindings b JOIN agent_tokens a ON a.id=b.token_id "
        "WHERE a.name COLLATE BINARY=? AND a.revoked_at IS NULL AND b.revoked_at IS NULL",
        (name,),
    ).fetchone():
        raise ValueError("Confirmation consumed or active name")

    raw = POLICY_TOKEN_PREFIX + secrets.token_urlsafe(32)
    token_hash = generate_password_hash(raw, method="pbkdf2:sha256")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    token_id = db.execute(
        "INSERT INTO agent_tokens (name, token_hash, token_prefix, created_by, created_at, "
        "allowed_read_paths_json, allowed_write_paths_json, allowed_warehouse_codes_json) "
        "VALUES (?, ?, ?, ?, ?, '[]', '[]', '[]')",
        (name, token_hash, token_prefix(raw), issuer, now),
    ).lastrowid
    db.execute(
        "INSERT INTO mcp_policy_bindings (token_id, preset_name, preset_version, grants_json, "
        "warehouse_codes_json, confirmation_id, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (token_id, preset, version, grants_json, warehouses_json, confirmation_id, issuer, now),
    )
    return token_id, raw
