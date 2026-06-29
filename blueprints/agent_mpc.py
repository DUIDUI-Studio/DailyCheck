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


# -----------------------------------------------------------------------------
# Common request guard — runs at the start of every MPC route
# -----------------------------------------------------------------------------


# Routes that DO NOT require warehouse_code (platform-level data).
_NO_WAREHOUSE_REQUIRED = {
    "/api/v1/templates",
    "/api/v1/notifications/feed",
}


def _guard_mpc() -> tuple[dict | None, tuple | None]:
    """Verify token + path/warehouse whitelist. Returns (row, error_response).

    If error_response is not None, the caller MUST return it (it is
    already a (body, status) tuple). The status is also stashed on
    `g.mpc_status` so the teardown access-log writer can record it.
    """
    path = request.path
    method = request.method
    row = verify_token()
    if row is None:
        g.mpc_status = 401
        return None, (jsonify({"error": "unauthorized"}), 401)
    if not check_path_allowed(row, method, path):
        g.mpc_status = 403
        return row, (jsonify({"error": "forbidden_path"}), 403)

    # warehouse_code: from query string for GET, from JSON body for POST
    if method == "GET":
        wh = request.args.get("warehouse_code")
    else:
        payload = request.get_json(silent=True) or request.form
        wh = payload.get("warehouse_code") if payload else None
        if wh is None:
            wh = request.args.get("warehouse_code")

    if path not in _NO_WAREHOUSE_REQUIRED and not wh:
        g.mpc_status = 400
        return row, (jsonify({"error": "warehouse_code_required"}), 400)
    if wh and not check_warehouse_allowed(row, wh):
        g.mpc_status = 403
        return row, (jsonify({"error": "forbidden_warehouse"}), 403)
    g.mpc_row = row
    g.mpc_warehouse_code = wh
    return row, None


def _resolve_warehouse(code: str) -> dict | None:
    """Look up a warehouses row from master.db. Returns sqlite Row or None."""
    init_master_db()
    with closing(sqlite3.connect(config.MASTER_DB)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM warehouses WHERE code=?", (code,)
        ).fetchone()


def _open_warehouse_db(path: str) -> sqlite3.Connection:
    """Open a per-warehouse db connection. Caller closes it."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


# -----------------------------------------------------------------------------
# Read routes
# -----------------------------------------------------------------------------


@bp.route("/api/v1/items", methods=["GET"])
def items_list():
    row, err = _guard_mpc()
    if err:
        return err
    wh = request.args["warehouse_code"]
    wh_row = _resolve_warehouse(wh)
    if wh_row is None:
        return jsonify({"error": "warehouse_not_found"}), 404
    conn = _open_warehouse_db(wh_row["db_path"])
    with closing(conn) as c:
        rows = c.execute(
            "SELECT id, sku, name, category_id, quantity, safety_stock, "
            "unit, unit_cost, gram_per_unit, updated_at "
            "FROM items ORDER BY id"
        ).fetchall()
    items = [dict(r) for r in rows]
    return jsonify({"warehouse_code": wh, "items": items})


@bp.route("/api/v1/items/<int:item_id>", methods=["GET"])
def items_detail(item_id: int):
    row, err = _guard_mpc()
    if err:
        return err
    wh = request.args["warehouse_code"]
    wh_row = _resolve_warehouse(wh)
    if wh_row is None:
        return jsonify({"error": "warehouse_not_found"}), 404
    conn = _open_warehouse_db(wh_row["db_path"])
    with closing(conn) as c:
        r = c.execute(
            "SELECT id, sku, name, category_id, quantity, safety_stock, "
            "unit, unit_cost, gram_per_unit, updated_at "
            "FROM items WHERE id=?", (item_id,),
        ).fetchone()
    if r is None:
        return jsonify({"error": "not_found"}), 404
    return jsonify(dict(r))


@bp.route("/api/v1/movements", methods=["GET"])
def movements_list():
    row, err = _guard_mpc()
    if err:
        return err
    wh = request.args["warehouse_code"]
    wh_row = _resolve_warehouse(wh)
    if wh_row is None:
        return jsonify({"error": "warehouse_not_found"}), 404
    conn = _open_warehouse_db(wh_row["db_path"])
    with closing(conn) as c:
        # Outbound requests (active only, exclude rolled-back)
        out_rows = c.execute(
            """SELECT o.id, o.item_id, i.name AS item_name,
                      o.requested_quantity AS qty, o.reason, o.created_at,
                      'outbound' AS type
               FROM outbound_requests o
               JOIN items i ON i.id = o.item_id
               WHERE o.rolled_back = 0
               ORDER BY o.created_at DESC LIMIT 200"""
        ).fetchall()
        # Stock movements (audit trail of stock changes)
        sm_rows = c.execute(
            """SELECT s.id, s.item_id, i.name AS item_name,
                      s.delta AS qty, s.action AS reason, s.created_at,
                      'stock_movement' AS type
               FROM stock_movements s
               JOIN items i ON i.id = s.item_id
               ORDER BY s.created_at DESC LIMIT 200"""
        ).fetchall()
    movements: list[dict] = []
    for r in out_rows:
        movements.append({
            "id": r["id"],
            "type": r["type"],
            "item_id": r["item_id"],
            "item_name": r["item_name"],
            "qty": r["qty"],
            "reason": r["reason"],
            "created_at": r["created_at"],
        })
    for r in sm_rows:
        movements.append({
            "id": r["id"],
            "type": r["type"],
            "item_id": r["item_id"],
            "item_name": r["item_name"],
            "qty": r["qty"],
            "reason": r["reason"],
            "created_at": r["created_at"],
        })
    # Sort by created_at desc; stable on id desc as tiebreaker
    movements.sort(key=lambda m: (m["created_at"], m["id"]), reverse=True)
    return jsonify({"warehouse_code": wh, "movements": movements[:200]})


@bp.route("/api/v1/forecast/item/<int:item_id>", methods=["GET"])
def forecast_item(item_id: int):
    row, err = _guard_mpc()
    if err:
        return err
    wh = request.args["warehouse_code"]
    wh_row = _resolve_warehouse(wh)
    if wh_row is None:
        return jsonify({"error": "warehouse_not_found"}), 404
    # Re-use subproject 1's helpers directly (no need to re-implement).
    # We import inside the route to avoid a hard dep at module load.
    from blueprints.forecast import _fetch_outbound_rows, _build_response
    conn = _open_warehouse_db(wh_row["db_path"])
    with closing(conn) as c:
        if c.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone() is None:
            return jsonify({"error": "not_found"}), 404
    # Parse horizon
    from blueprints.forecast import _parse_horizon
    horizon = _parse_horizon(request.args.get("horizon_days"))
    if horizon is None:
        return jsonify({"error": "invalid_horizon"}), 400
    # _fetch_outbound_rows uses get_warehouse_db() (flask 'g'). Open a
    # fresh connection on the target db and reuse the same query.
    from datetime import datetime
    conn = _open_warehouse_db(wh_row["db_path"])
    with closing(conn) as c:
        rows = c.execute(
            """SELECT requested_quantity, created_at
               FROM outbound_requests
               WHERE item_id=? AND rolled_back=0
                 AND created_at >= datetime('now', '-30 days')""",
            (item_id,),
        ).fetchall()
    parsed: list[tuple[datetime, float]] = []
    for r in rows:
        try:
            ts = datetime.strptime(r["created_at"], "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue
        parsed.append((ts, float(r["requested_quantity"])))
    # Build response with a custom warehouse_code (the helper uses g).
    body = _build_response(item_id, horizon, parsed)
    body["warehouse_code"] = wh
    return jsonify(body)


@bp.route("/api/v1/procurement/store", methods=["GET"])
def procurement_store():
    row, err = _guard_mpc()
    if err:
        return err
    wh = request.args["warehouse_code"]
    wh_row = _resolve_warehouse(wh)
    if wh_row is None:
        return jsonify({"error": "warehouse_not_found"}), 404
    from blueprints.procurement import _store_procurement_json
    body = _store_procurement_json(wh)
    if body is None:
        return jsonify({"error": "not_found"}), 404
    return jsonify(body)


@bp.route("/api/v1/procurement/hub", methods=["GET"])
def procurement_hub():
    row, err = _guard_mpc()
    if err:
        return err
    # hub iterates ALL warehouses; warehouse_code query is optional and
    # only used to filter (the route's primary purpose is a platform
    # roll-up). We accept the parameter and use it to filter, defaulting
    # to "all" if absent.
    init_master_db()
    with closing(sqlite3.connect(config.MASTER_DB)) as m:
        m.row_factory = sqlite3.Row
        codes = [r["code"] for r in m.execute(
            "SELECT code FROM warehouses ORDER BY code"
        ).fetchall()]
    wh_filter = request.args.get("warehouse_code")
    if wh_filter:
        codes = [c for c in codes if c == wh_filter]
    if not codes:
        return jsonify({
            "computed_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "items": [],
        })
    from blueprints.procurement import _store_procurement_json
    from blueprints.procurement_pure import aggregate_hub
    reports = []
    for c in codes:
        body = _store_procurement_json(c)
        if body is None:
            continue
        reports.append({
            "warehouse_code": body["warehouse_code"],
            "items": [
                {
                    "item_id": it["item_id"],
                    "item_name": it["item_name"],
                    "suggested_qty": it["suggested_qty"],
                }
                for it in body["items"]
            ],
        })
    hub_items = aggregate_hub(reports)
    return jsonify({
        "computed_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "items": hub_items,
    })


@bp.route("/api/v1/categories", methods=["GET"])
def categories_list():
    row, err = _guard_mpc()
    if err:
        return err
    wh = request.args["warehouse_code"]
    wh_row = _resolve_warehouse(wh)
    if wh_row is None:
        return jsonify({"error": "warehouse_not_found"}), 404
    conn = _open_warehouse_db(wh_row["db_path"])
    with closing(conn) as c:
        rows = c.execute(
            "SELECT id, name, description, created_at "
            "FROM categories ORDER BY name"
        ).fetchall()
    return jsonify({"warehouse_code": wh, "categories": [dict(r) for r in rows]})


@bp.route("/api/v1/templates", methods=["GET"])
def templates_list():
    """List publish_templates (subproject 4). Table may not exist yet →
    return empty list with a stable shape. Master.db, no warehouse_code."""
    row, err = _guard_mpc()
    if err:
        return err
    init_master_db()
    templates: list[dict] = []
    with closing(sqlite3.connect(config.MASTER_DB)) as conn:
        # Check if table exists; if not, return empty (subproject 4 not
        # merged yet — graceful degradation per spec §1).
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='publish_templates'"
        ).fetchone()
        if exists is not None:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM publish_templates ORDER BY id"
            ).fetchall()
            templates = [dict(r) for r in rows]
    return jsonify({"templates": templates})


@bp.route("/api/v1/notifications/feed", methods=["GET"])
def notifications_feed():
    """Spec: Agents do not consume notifications yet. Always return empty.

    We still verify the token + path, but the body is empty. This is
    a placeholder for future fan-out (PRD §2.3.6 deferred).
    """
    row, err = _guard_mpc()
    if err:
        return err
    return jsonify({"events": [], "unread_count": 0})


# -----------------------------------------------------------------------------
# Read-route timing/logging wrapper (T6) — applied via before_request on
# the bp. Centralises the access.log append so individual routes don't
# need to remember to log.
# -----------------------------------------------------------------------------


@bp.before_request
def _log_mpc_request():
    """Record start time; the teardown handler writes the access log."""
    g._mpc_start_ms = int(time.time() * 1000)


@bp.after_request
def _capture_mpc_status(resp):
    """Stash the final response status on g so the teardown can log it."""
    g.mpc_status = resp.status_code
    return resp


@bp.teardown_request
def _log_mpc_teardown(exc):
    try:
        start = g.get("_mpc_start_ms")
        if start is None:
            return
        duration = int(time.time() * 1000) - start
        token_id: int | None = None
        try:
            r = g.get("mpc_row")
            if r is not None:
                token_id = int(r["id"])
        except Exception:  # noqa: BLE001
            token_id = None
        status = int(g.get("mpc_status", 200))
        _write_mpc_access_log(
            token_id=token_id,
            method=request.method,
            path=request.path,
            status=status,
            duration_ms=duration,
        )
    except Exception:  # noqa: BLE001 — never break the request from teardown
        return
