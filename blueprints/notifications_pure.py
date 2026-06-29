"""Pure functions for the notifications event bus.

Spec: docs/superpowers/specs/2026-06-29-notifications-design.md
PRD : §2.5.4 (event bus) + §2.5.5 (data contract)
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any


# PRD §2.5.4: first-cut event types. Only recipe_published is wired up in
# this subproject; the others are reserved for subprojects 4/5/6.
ALLOWED_EVENT_TYPES = frozenset({"recipe_published"})

SUMMARY_MAX_LEN = 200

_LIST_LIMIT = 100


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _iso_z() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def emit_event(
    db: sqlite3.Connection,
    event_type: str,
    summary: str,
    target_url: str | None,
    user_ids: list[int],
) -> int:
    """Fan out one event to N users by writing N rows to notifications.

    Returns the number of rows inserted. Validation raises ValueError on
    bad input (does NOT write a row) so callers can pre-check.
    """
    if event_type not in ALLOWED_EVENT_TYPES:
        raise ValueError(f"event_type {event_type!r} not allowed")
    if not user_ids:
        raise ValueError("user_ids must be non-empty")
    if len(summary) > SUMMARY_MAX_LEN:
        raise ValueError(f"summary exceeds {SUMMARY_MAX_LEN} chars")
    now = _now()
    rows = [
        (uid, event_type, summary, target_url, now) for uid in user_ids
    ]
    cur = db.executemany(
        """INSERT INTO notifications
           (user_id, event_type, summary, target_url, created_at, read_at)
           VALUES (?, ?, ?, ?, ?, NULL)""",
        rows,
    )
    db.commit()
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(rows)


def mark_read(db: sqlite3.Connection, user_id: int, event_id: int) -> bool:
    """Mark a single notification as read. Idempotent.

    Returns True iff the row was found in unread state and successfully
    flipped to read. False for: missing event, already read, or event
    belongs to another user.
    """
    cur = db.execute(
        """UPDATE notifications
           SET read_at = ?
           WHERE id=? AND user_id=? AND read_at IS NULL""",
        (_now(), event_id, user_id),
    )
    db.commit()
    return cur.rowcount > 0


def list_for_user(
    db: sqlite3.Connection,
    user_id: int,
    unread_only: bool = False,
) -> list[dict[str, Any]]:
    """Return notifications for a user, newest first, capped at _LIST_LIMIT.

    Shape (PRD §2.5.5):
      { event_id, event_type, summary, created_at, target_url, read }
    """
    where = "WHERE user_id=?"
    params: list[Any] = [user_id]
    if unread_only:
        where += " AND read_at IS NULL"
    rows = db.execute(
        f"""SELECT id, event_type, summary, target_url, created_at, read_at
            FROM notifications
            {where}
            ORDER BY created_at DESC, id DESC
            LIMIT ?""",
        (*params, _LIST_LIMIT),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append({
            "event_id": r["id"],
            "event_type": r["event_type"],
            "summary": r["summary"],
            "created_at": r["created_at"],
            "target_url": r["target_url"],
            "read": r["read_at"] is not None,
        })
    return out
