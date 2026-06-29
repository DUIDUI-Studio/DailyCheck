"""Agent MPC blueprint: HTTP/JSON interface for external Agents (PRD §2.3).

This blueprint is **completely independent of session-based auth**:
- It uses `Authorization: Bearer <token>` exclusively.
- It MUST NOT call `g.user` or `require_login`.
- The session-based `before_request` hook in `blueprints/auth.py` is
  bypassed by short-circuiting the redirect logic when the request
  path starts with /api/v1/mpc (or by registering this blueprint's
  URLs in a way that auth's PUBLIC_ENDPOINTS already includes them).
  We use a per-blueprint `before_request` shim to make the load_user
  hook a no-op for /api/v1/* paths.

Public surface (PRD §2.3.2):

  Read (any valid token, if path whitelisted):
    GET  /api/v1/items
    GET  /api/v1/items/<id>
    GET  /api/v1/movements
    GET  /api/v1/forecast/item/<id>
    GET  /api/v1/procurement/store
    GET  /api/v1/procurement/hub
    GET  /api/v1/categories
    GET  /api/v1/templates
    GET  /api/v1/notifications/feed

  Write (path must be in write whitelist):
    POST /api/v1/restock
    POST /api/v1/procurement/recompute
    POST /api/v1/forecast/recompute

  Admin (session-based, no token):
    GET  /admin/mpc-usage
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import (
    Blueprint, abort, current_app, g, jsonify, render_template, request,
)
from werkzeug.security import check_password_hash

import config
from db import init_master_db
from .agent_mpc_pure import path_matches

# -----------------------------------------------------------------------------
# Blueprint
# -----------------------------------------------------------------------------

bp = Blueprint("agent_mpc", __name__)
# /api/v1/* paths must NOT trigger session-based redirect. The session
# before_request hook in blueprints/auth.py checks the endpoint name,
# so we register an extra PUBLIC_ENDPOINT for every route we expose
# below. The set is exposed publicly so the auth blueprint can adopt
# it without coupling (see app.py / blueprints/auth.py note).

# Access-log path. Spec §4.1 says append to the existing access.log
# at the project root (same file the request logger writes to). Tests
# monkeypatch this constant.
from config import BASE_DIR as _BASE_DIR
_ACCESS_LOG_PATH: Path = _BASE_DIR / "access.log"


def _write_mpc_access_log(
    token_id: int | None,
    method: str,
    path: str,
    status: int,
    duration_ms: int,
) -> None:
    """Append one JSON line to the access.log (spec §4.1).

    The JSON shape is fixed:
        {ts, agent_token_id, path, method, status, duration_ms}

    The helper must never raise — observability failures must not break
    the user-facing route. Errors are swallowed silently (operators can
    detect them via the per-request access.log gap, not via 500s).
    """
    try:
        rec = {
            "ts": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "agent_token_id": token_id,
            "path": path,
            "method": method,
            "status": int(status),
            "duration_ms": int(duration_ms),
        }
        line = json.dumps(rec, ensure_ascii=False)
        with open(_ACCESS_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001 — see docstring
        return


# -----------------------------------------------------------------------------
# Auth helpers (T3)
# -----------------------------------------------------------------------------


def verify_token() -> dict | None:
    """Return the agent_tokens row for the Bearer token, or None.

    Returns None when:
    - no Authorization header
    - not a Bearer scheme
    - token doesn't match any row
    - the row is revoked
    """
    h = request.headers.get("Authorization", "")
    if not h.startswith("Bearer "):
        return None
    raw = h[len("Bearer "):].strip()
    if not raw:
        return None
    init_master_db()
    with closing(sqlite3.connect(config.MASTER_DB)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM agent_tokens WHERE token_hash IS NOT NULL"
        ).fetchall()
    # Linear scan + check_password_hash — token count is tiny (handful
    # of platform admin tokens), and we never store plaintext so we
    # can't do an indexed lookup. werkzeug's verify is the right call.
    for r in row:
        if r["revoked_at"] is not None:
            continue
        if check_password_hash(r["token_hash"], raw):
            return dict(r)
    return None


def check_path_allowed(row: dict, method: str, path: str) -> bool:
    """Return True if the token row authorizes (method, path)."""
    try:
        allowed = json.loads(row["allowed_write_paths_json" if method != "GET" else "allowed_read_paths_json"])
    except (ValueError, TypeError):
        return False
    if not isinstance(allowed, list):
        return False
    for pat in allowed:
        if not isinstance(pat, str):
            continue
        if path_matches(pat, path):
            return True
    return False


def check_warehouse_allowed(row: dict, warehouse_code: str | None) -> bool:
    """Return True if warehouse_code is in the row's whitelist (or whitelist is empty)."""
    if not warehouse_code:
        return False
    try:
        codes = json.loads(row["allowed_warehouse_codes_json"])
    except (ValueError, TypeError):
        return False
    if not isinstance(codes, list):
        return False
    if len(codes) == 0:
        return True  # empty list = all warehouses
    return warehouse_code in codes
