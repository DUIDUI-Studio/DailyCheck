"""Integration tests for nav entry visibility + landing page cards.

PRD §1.3 + subproject 1/2/3 specs: forecast (manager+), procurement
(staff+), notifications (all logged-in).
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Sidebar nav entries
# ---------------------------------------------------------------------------


def test_sidebar_includes_forecast_for_manager(logged_client):
    """/forecast link visible to admin (manager+)."""
    client, _ = logged_client
    resp = client.get("/land")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    assert "/forecast" in body


def test_sidebar_includes_procurement_for_staff(staff_client):
    """/procurement/store link visible to staff (everyone)."""
    client, _ = staff_client
    resp = client.get("/land")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    assert "/procurement/store" in body


def test_sidebar_includes_notifications_for_staff(staff_client):
    """/notifications link visible to all logged-in users."""
    client, _ = staff_client
    resp = client.get("/land")
    body = resp.data.decode("utf-8")
    assert "/notifications" in body


def test_sidebar_forecast_hidden_for_staff(staff_client):
    """Forecast requires manager+. Staff must NOT see the link."""
    client, _ = staff_client
    body = client.get("/land").data.decode("utf-8")
    # Sidebar should not contain the forecast entry (specifically the
    # /forecast url with a label, not /forecast/item/<n> etc.)
    # The simplest check: the sidebar block does not contain a link to
    # "/forecast" without an item id suffix.
    import re
    # Find hrefs that point to /forecast (not /forecast/item/...)
    hrefs = re.findall(r'href="(/forecast[^"]*)"', body)
    staff_forecast_hrefs = [h for h in hrefs if not h.startswith("/forecast/item/") and not h.startswith("/forecast/product/")]
    assert staff_forecast_hrefs == [], f"staff should not see /forecast link, found: {staff_forecast_hrefs}"


# ---------------------------------------------------------------------------
# Land page cards
# ---------------------------------------------------------------------------


def test_land_page_has_forecast_card(logged_client):
    """Admin sees a forecast card on /land."""
    client, _ = logged_client
    body = client.get("/land").data.decode("utf-8")
    # Card title contains the substring 预测
    assert "预测" in body
    # Card links to /forecast
    assert "/forecast" in body


def test_land_page_has_procurement_card(logged_client):
    """Admin sees a procurement card on /land."""
    client, _ = logged_client
    body = client.get("/land").data.decode("utf-8")
    assert "采购" in body
    assert "/procurement/store" in body


def test_land_page_has_notifications_card(logged_client):
    """Admin sees a notifications card on /land."""
    client, _ = logged_client
    body = client.get("/land").data.decode("utf-8")
    assert "通知" in body
    assert "/notifications" in body


def test_land_page_staff_sees_procurement_and_notifications_not_forecast(staff_client):
    """Staff sees procurement + notifications cards but NOT forecast."""
    client, _ = staff_client
    body = client.get("/land").data.decode("utf-8")
    # Has procurement
    assert "采购" in body
    # Has notifications
    assert "通知" in body
    # Does NOT have forecast (the card should be hidden for staff)
    import re
    hrefs = re.findall(r'href="(/forecast[^"]*)"', body)
    non_item_hrefs = [h for h in hrefs if not h.startswith("/forecast/item/") and not h.startswith("/forecast/product/")]
    assert non_item_hrefs == [], f"staff land page should not have /forecast link, found: {non_item_hrefs}"


# ---------------------------------------------------------------------------
# Notification badge in ctx-bar (PRD §1.1 A6: top badge)
# ---------------------------------------------------------------------------


def test_notifications_badge_shows_unread_count_for_admin(logged_client):
    """When admin has unread notifications, ctx-bar shows a badge with count."""
    client, _ = logged_client
    # Emit a notification for user 1
    client.post(
        "/admin/notifications/test-emit",
        json={"event_type": "recipe_published", "summary": "测试", "target_url": "/x", "user_ids": [1]},
    )
    body = client.get("/land").data.decode("utf-8")
    # ctx-bar (or nav) should show a count > 0 next to /notifications
    assert "通知" in body
    # The unread count '1' should appear next to the notifications link
    # (loose check — we just want a digit near "通知")
    import re
    # find "通知" then a digit within ~30 chars
    assert re.search(r"通知[^0-9]{0,30}[1-9]", body), "expected unread count badge near 通知 link"
