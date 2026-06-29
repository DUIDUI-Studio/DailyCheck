"""Unit tests for the notifications event-bus pure functions.

The pure fns take a sqlite3.Connection explicitly (no module-level g /
current_app) so they can be tested with an in-memory db. The blueprint
(blueprints/notifications.py) opens the connection in the request
context and passes it in.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from blueprints.notifications_pure import (
    ALLOWED_EVENT_TYPES,
    SUMMARY_MAX_LEN,
    emit_event,
    list_for_user,
    mark_read,
)


@pytest.fixture
def db():
    """In-memory sqlite3 with the notifications + prefs schema."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript("""
        CREATE TABLE notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            summary TEXT NOT NULL,
            target_url TEXT,
            created_at TEXT NOT NULL,
            read_at TEXT
        );
        CREATE TABLE notification_prefs (
            user_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            channel TEXT NOT NULL,
            muted INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, event_type, channel)
        );
        CREATE INDEX idx_notif_user_read ON notifications(user_id, read_at, created_at);
    """)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# emit_event
# ---------------------------------------------------------------------------


def test_emit_event_writes_one_row_per_user(db):
    emit_event(db, "recipe_published", "经典柠檬茶 v3 已发布", "/products/12/versions/3", [1, 2, 3])
    n = db.execute("SELECT COUNT(*) AS c FROM notifications").fetchone()["c"]
    assert n == 3


def test_emit_event_uses_passed_connection(db):
    """Same conn should see the new rows immediately (no separate flush)."""
    emit_event(db, "recipe_published", "x", "/x", [1])
    row = db.execute("SELECT user_id, summary FROM notifications WHERE user_id=1").fetchone()
    assert row["user_id"] == 1
    assert row["summary"] == "x"


def test_emit_event_empty_user_ids_raises(db):
    with pytest.raises(ValueError):
        emit_event(db, "recipe_published", "x", "/x", [])


def test_emit_event_disallowed_event_type_raises(db):
    with pytest.raises(ValueError):
        emit_event(db, "item_published", "x", "/x", [1])  # not in first-cut list


def test_emit_event_summary_too_long_raises(db):
    long = "a" * (SUMMARY_MAX_LEN + 1)
    with pytest.raises(ValueError):
        emit_event(db, "recipe_published", long, "/x", [1])


def test_emit_event_sets_created_at_to_now(db):
    emit_event(db, "recipe_published", "x", "/x", [1])
    row = db.execute("SELECT created_at FROM notifications WHERE user_id=1").fetchone()
    # ISO-ish format
    assert row["created_at"] is not None


def test_emit_event_target_url_optional(db):
    emit_event(db, "recipe_published", "x", None, [1])
    row = db.execute("SELECT target_url FROM notifications WHERE user_id=1").fetchone()
    assert row["target_url"] is None


def test_emit_event_allowed_types_includes_recipe_published():
    assert "recipe_published" in ALLOWED_EVENT_TYPES


# ---------------------------------------------------------------------------
# mark_read
# ---------------------------------------------------------------------------


def test_mark_read_unread_becomes_read(db):
    emit_event(db, "recipe_published", "x", "/x", [1])
    event_id = db.execute("SELECT id FROM notifications WHERE user_id=1").fetchone()["id"]
    assert mark_read(db, user_id=1, event_id=event_id) is True
    row = db.execute("SELECT read_at FROM notifications WHERE id=?", (event_id,)).fetchone()
    assert row["read_at"] is not None


def test_mark_read_already_read_returns_false(db):
    emit_event(db, "recipe_published", "x", "/x", [1])
    event_id = db.execute("SELECT id FROM notifications WHERE user_id=1").fetchone()["id"]
    mark_read(db, user_id=1, event_id=event_id)
    assert mark_read(db, user_id=1, event_id=event_id) is False  # idempotent


def test_mark_read_missing_event_returns_false(db):
    assert mark_read(db, user_id=1, event_id=999) is False


def test_mark_read_other_user_event_returns_false(db):
    """mark_read must be per-user — a notification belonging to user 2
    cannot be marked read by user 1."""
    emit_event(db, "recipe_published", "x", "/x", [2])
    event_id = db.execute("SELECT id FROM notifications WHERE user_id=2").fetchone()["id"]
    assert mark_read(db, user_id=1, event_id=event_id) is False


# ---------------------------------------------------------------------------
# list_for_user
# ---------------------------------------------------------------------------


def test_list_for_user_returns_unread_when_flag_set(db):
    emit_event(db, "recipe_published", "x", "/x", [1])
    event_id = db.execute("SELECT id FROM notifications WHERE user_id=1").fetchone()["id"]
    mark_read(db, user_id=1, event_id=event_id)
    out = list_for_user(db, user_id=1, unread_only=True)
    assert out == []


def test_list_for_user_returns_all_when_flag_false(db):
    emit_event(db, "recipe_published", "a", "/a", [1])
    event_id = db.execute("SELECT id FROM notifications WHERE user_id=1").fetchone()["id"]
    mark_read(db, user_id=1, event_id=event_id)
    out = list_for_user(db, user_id=1, unread_only=False)
    assert len(out) == 1
    assert out[0]["event_id"] == event_id
    assert out[0]["read"] is True


def test_list_for_user_descending_by_created_at(db):
    """Most recent event appears first."""
    emit_event(db, "recipe_published", "first", "/1", [1])
    # Force a different created_at by manually updating
    db.execute(
        "UPDATE notifications SET created_at='2026-06-29 10:00:00' WHERE user_id=1"
    )
    emit_event(db, "recipe_published", "second", "/2", [1])
    db.execute(
        "UPDATE notifications SET created_at='2026-06-29 11:00:00' WHERE id=last_insert_rowid()"
    )
    out = list_for_user(db, user_id=1, unread_only=False)
    assert [o["summary"] for o in out] == ["second", "first"]


def test_list_for_user_limit_100(db):
    """101 events → only 100 returned."""
    for i in range(101):
        emit_event(db, "recipe_published", f"e{i}", "/x", [1])
    out = list_for_user(db, user_id=1, unread_only=False)
    assert len(out) == 100


def test_list_for_user_only_returns_user_own(db):
    emit_event(db, "recipe_published", "a", "/a", [1, 2, 3])
    out = list_for_user(db, user_id=1, unread_only=False)
    # Only 1 row (the one for user 1). The user_id field is not in
    # the response shape (spec §1.1) — implicit from the filter.
    assert len(out) == 1
    # And user 2 sees only their own
    out2 = list_for_user(db, user_id=2, unread_only=False)
    assert len(out2) == 1
    # And user 1 does NOT see user 2's event
    out1_ids = [o["event_id"] for o in out]
    out2_ids = [o["event_id"] for o in out2]
    assert out1_ids != out2_ids


def test_list_for_user_shape_stable(db):
    """Lock the response shape so subproject 5/6 consumers can rely on it."""
    emit_event(db, "recipe_published", "测试事件", "/products/12/versions/3", [1])
    out = list_for_user(db, user_id=1, unread_only=False)
    assert len(out) == 1
    e = out[0]
    expected_keys = {"event_id", "event_type", "summary", "created_at", "target_url", "read"}
    assert expected_keys.issubset(set(e.keys()))
