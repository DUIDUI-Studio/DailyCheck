"""/notifications blueprint: Web-channel notification feed and
mark-read API. Pure logic lives in blueprints.notifications_pure.

PRD §2.5.4 (event bus design) + §2.5.5 (data contract).
First-cut supported event_type: 'recipe_published' (see plan §0).

A dev-only /admin/notifications/test-emit route is exposed to drive
end-to-end testing without going through subproject 5. In production
it should be removed or gated by an env var (not done in this PR).
"""
from __future__ import annotations

import sqlite3
from contextlib import closing

from flask import Blueprint, abort, g, jsonify, request

from db import get_master_db, init_master_db
from permissions import require_login, require_role
from .notifications_pure import (
    ALLOWED_EVENT_TYPES,
    SUMMARY_MAX_LEN,
    emit_event,
    list_for_user,
    mark_read,
)

bp = Blueprint("notifications", __name__)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@bp.route("/notifications", methods=["GET"])
@require_login
def notifications_feed():
    """Return the user's notification feed as JSON.

    Query params:
      unread=true  → only unread events
      (default)    → all events
    """
    if request.args.get("format") == "html" or request.headers.get("Accept", "").startswith("text/html"):
        # Lazy import: only needed for HTML rendering, keeps JSON path light.
        from flask import render_template
        from db import get_master_db as _gmdb
        with _gmdb() as conn:
            conn.row_factory = sqlite3.Row
            events = list_for_user(conn, g.user["id"], unread_only=False)
        return render_template("notifications.html", events=events)

    unread_only = request.args.get("unread", "false").lower() in ("true", "1", "yes")
    with get_master_db() as conn:
        events = list_for_user(conn, g.user["id"], unread_only=unread_only)
    unread_count = sum(1 for e in events if not e["read"]) if not unread_only else len(events)
    return jsonify({"unread_count": unread_count, "events": events})


@bp.route("/notifications/<int:event_id>/read", methods=["POST"])
@require_login
def mark_notification_read(event_id: int):
    """Mark a single notification as read. Idempotent.

    404 if the event does not exist OR belongs to another user.
    200 (with {"ok": true}) if the event is now read (whether it was
    read before or not — repeated POSTs are no-ops).
    """
    with get_master_db() as conn:
        # First, verify the event belongs to the current user.
        row = conn.execute(
            "SELECT read_at FROM notifications WHERE id=? AND user_id=?",
            (event_id, g.user["id"]),
        ).fetchone()
        if row is None:
            abort(404)
        if row["read_at"] is None:
            mark_read(conn, g.user["id"], event_id)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Dev-only test emitter
# ---------------------------------------------------------------------------


@bp.route("/admin/notifications/test-emit", methods=["POST"])
@require_role("manager")
def test_emit():
    """Emit one event for fanout testing. Dev-only — see spec §1.3."""
    import config  # call-time lookup so conftest monkeypatch works
    payload = request.get_json(silent=True) or request.form
    event_type = payload.get("event_type", "recipe_published")
    summary = payload.get("summary", "")
    target_url = payload.get("target_url")

    if event_type not in ALLOWED_EVENT_TYPES:
        return jsonify({"error": "unsupported_event_type"}), 400
    if len(summary) > SUMMARY_MAX_LEN:
        return jsonify({"error": "summary_too_long"}), 400

    user_ids = payload.get("user_ids")
    if user_ids is None:
        # Default: all users (dev convenience for fixtures with 1 user)
        init_master_db()
        with closing(sqlite3.connect(config.MASTER_DB)) as m:
            m.row_factory = sqlite3.Row
            user_ids = [r["id"] for r in m.execute("SELECT id FROM users").fetchall()]
    else:
        user_ids = [int(u) for u in user_ids]

    with get_master_db() as conn:
        n = emit_event(conn, event_type, summary, target_url, user_ids)
    return jsonify({"ok": True, "emitted": n})
