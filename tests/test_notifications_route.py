"""Integration tests for the /notifications blueprint.

Covers TASK 3 (feed + mark-read routes) and TASK 5 (dev-only test-emit).
"""
from __future__ import annotations

import pytest

from tests.conftest import _seed_item  # noqa: F401  (imported for fixture consistency)


# ---------------------------------------------------------------------------
# GET /notifications
# ---------------------------------------------------------------------------


def test_notifications_unauthenticated_redirects_to_login(logged_client):
    """No session → 302 to /login (require_login behavior)."""
    client, _ = logged_client
    with client.session_transaction() as s:
        s.pop("user_id", None)
    resp = client.get("/notifications")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_notifications_empty_returns_zero_count(logged_client):
    client, _ = logged_client
    resp = client.get("/notifications?unread=true")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["unread_count"] == 0
    assert body["events"] == []


def test_notifications_returns_emitted_events(logged_client):
    client, _ = logged_client
    # Emit a notification for the current logged-in user (id=1).
    client.post(
        "/admin/notifications/test-emit",
        json={
            "event_type": "recipe_published",
            "summary": "经典柠檬茶 v3 已发布",
            "target_url": "/products/12/versions/3",
            "user_ids": [1],
        },
    )
    resp = client.get("/notifications?unread=true")
    body = resp.get_json()
    assert body["unread_count"] == 1
    assert len(body["events"]) == 1
    e = body["events"][0]
    assert e["event_type"] == "recipe_published"
    assert e["summary"] == "经典柠檬茶 v3 已发布"
    assert e["target_url"] == "/products/12/versions/3"
    assert e["read"] is False
    assert "event_id" in e


def test_notifications_unread_false_returns_all(logged_client):
    client, _ = logged_client
    client.post(
        "/admin/notifications/test-emit",
        json={"event_type": "recipe_published", "summary": "x", "target_url": "/x", "user_ids": [1]},
    )
    client.post(
        "/admin/notifications/test-emit",
        json={"event_type": "recipe_published", "summary": "y", "target_url": "/y", "user_ids": [1]},
    )
    # Mark one as read
    body = client.get("/notifications?unread=true").get_json()
    eid = body["events"][0]["event_id"]
    client.post(f"/notifications/{eid}/read")

    out_all = client.get("/notifications").get_json()
    assert len(out_all["events"]) == 2
    out_unread = client.get("/notifications?unread=true").get_json()
    assert len(out_unread["events"]) == 1


# ---------------------------------------------------------------------------
# POST /notifications/<id>/read
# ---------------------------------------------------------------------------


def test_mark_own_notification_read(logged_client):
    client, _ = logged_client
    client.post(
        "/admin/notifications/test-emit",
        json={"event_type": "recipe_published", "summary": "x", "target_url": "/x", "user_ids": [1]},
    )
    eid = client.get("/notifications?unread=true").get_json()["events"][0]["event_id"]
    resp = client.post(f"/notifications/{eid}/read")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}
    # Verify read=true
    out = client.get("/notifications").get_json()
    assert all(e["read"] is True for e in out["events"])


def test_mark_other_users_notification_returns_404(logged_client):
    """Notification belongs to user 2; user 1 (logged in) cannot mark it."""
    client, _ = logged_client
    client.post(
        "/admin/notifications/test-emit",
        json={"event_type": "recipe_published", "summary": "x", "target_url": "/x", "user_ids": [2]},
    )
    # Find the event id from master.db directly (we can't fetch via /notifications
    # because user 1 doesn't see user 2's notifications)
    with client.application.app_context():
        from db import get_master_db
        db = get_master_db()
        eid = db.execute("SELECT id FROM notifications WHERE user_id=2").fetchone()["id"]
    resp = client.post(f"/notifications/{eid}/read")
    assert resp.status_code == 404


def test_mark_read_idempotent(logged_client):
    client, _ = logged_client
    client.post(
        "/admin/notifications/test-emit",
        json={"event_type": "recipe_published", "summary": "x", "target_url": "/x", "user_ids": [1]},
    )
    eid = client.get("/notifications?unread=true").get_json()["events"][0]["event_id"]
    r1 = client.post(f"/notifications/{eid}/read")
    r2 = client.post(f"/notifications/{eid}/read")
    assert r1.status_code == 200
    assert r2.status_code == 200


def test_mark_nonexistent_notification_returns_404(logged_client):
    client, _ = logged_client
    resp = client.post("/notifications/99999/read")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# test-emit (dev-only)
# ---------------------------------------------------------------------------


def test_test_emit_creates_notifications(logged_client):
    client, _ = logged_client
    resp = client.post(
        "/admin/notifications/test-emit",
        json={
            "event_type": "recipe_published",
            "summary": "x",
            "target_url": "/x",
            "user_ids": [1],
        },
    )
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert resp.get_json()["emitted"] == 1


def test_test_emit_default_user_ids_is_all_users(logged_client):
    """If user_ids is omitted, fan out to all users (dev convenience)."""
    client, _ = logged_client
    resp = client.post(
        "/admin/notifications/test-emit",
        json={"event_type": "recipe_published", "summary": "x", "target_url": "/x"},
    )
    assert resp.status_code == 200
    # Only user 1 exists in the test fixture
    assert resp.get_json()["emitted"] == 1


def test_test_emit_rejects_invalid_event_type(logged_client):
    client, _ = logged_client
    resp = client.post(
        "/admin/notifications/test-emit",
        json={"event_type": "not_in_first_cut", "summary": "x", "target_url": "/x", "user_ids": [1]},
    )
    assert resp.status_code == 400


def test_test_emit_rejects_oversized_summary(logged_client):
    client, _ = logged_client
    resp = client.post(
        "/admin/notifications/test-emit",
        json={"event_type": "recipe_published", "summary": "a" * 201, "target_url": "/x", "user_ids": [1]},
    )
    assert resp.status_code == 400
