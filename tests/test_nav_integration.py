"""Integration tests for nav entry visibility + landing page cards.

2026-10-13 导航模块化（feat/nav-modules）后的口径：
- 导航按「总览 / 库存管理 / 生产管理 / 门店订货 / 采购 / 通知 / 管理员」
  7 模块分组；模块下无可见子项时整块不渲染。
- 采购模块（采购建议 / 预测看板）与管理模块（账号 / Token / 品项数据）
  均为 admin 可见；staff 不可见。
- 侧边栏断言用 /dashboard（带侧栏的页面）；/land 断言只针对模块卡片。
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Sidebar nav entries
# ---------------------------------------------------------------------------


def test_sidebar_includes_forecast_for_admin(logged_client):
    """admin 侧边栏的「采购」分组含 /forecast（预测看板）。"""
    client, _ = logged_client
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    assert "/forecast" in body


def test_sidebar_procurement_hidden_for_staff(staff_client):
    """staff 侧边栏不得出现采购模块入口（采购建议 / 预测均为 admin 专属）。"""
    client, _ = staff_client
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    assert "/procurement/store" not in body
    assert 'nav-group-title">采购</p>' not in body


def test_sidebar_includes_notifications_for_staff(staff_client):
    """/notifications link visible to all logged-in users."""
    client, _ = staff_client
    resp = client.get("/dashboard")
    body = resp.data.decode("utf-8")
    assert "/notifications" in body


def test_sidebar_forecast_hidden_for_staff(staff_client):
    """Forecast requires admin. Staff must NOT see the link."""
    client, _ = staff_client
    body = client.get("/dashboard").data.decode("utf-8")
    # Sidebar should not contain the forecast entry (specifically the
    # /forecast url with a label, not /forecast/item/<n> etc.)
    import re
    # Find hrefs that point to /forecast (not /forecast/item/...)
    hrefs = re.findall(r'href="(/forecast[^"]*)"', body)
    staff_forecast_hrefs = [
        h for h in hrefs
        if not h.startswith("/forecast/item/") and not h.startswith("/forecast/product/")
    ]
    assert staff_forecast_hrefs == [], f"staff should not see /forecast link, found: {staff_forecast_hrefs}"


# ---------------------------------------------------------------------------
# Sidebar module groups (2026-10-13 导航模块化)
# ---------------------------------------------------------------------------


def test_sidebar_module_groups_for_admin(logged_client):
    """admin 侧边栏呈现 7 个模块分组 + 新补入口（生产记录 / 订单管理）。"""
    client, _ = logged_client
    body = client.get("/dashboard").data.decode("utf-8")
    for title in ("总览", "库存管理", "生产管理", "门店订货", "采购", "通知", "管理员"):
        assert f'nav-group-title">{title}</p>' in body, f"missing nav group: {title}"
    assert "/production/runs" in body  # 生产记录（新补入口）
    assert "/store-ordering/admin/orders" in body  # 订单管理（新补入口）


def test_sidebar_module_groups_for_staff(staff_client):
    """staff 侧边栏只含其可见模块分组；无采购 / 管理员。"""
    client, _ = staff_client
    body = client.get("/dashboard").data.decode("utf-8")
    for title in ("总览", "库存管理", "生产管理", "门店订货", "通知"):
        assert f'nav-group-title">{title}</p>' in body, f"missing nav group: {title}"
    assert 'nav-group-title">采购</p>' not in body
    assert 'nav-group-title">管理员</p>' not in body
    assert "/users" not in body


# ---------------------------------------------------------------------------
# Land page cards
# ---------------------------------------------------------------------------


def test_land_page_admin_sees_all_module_cards(logged_client):
    """admin 的 /land 每模块一张卡，href 指向各模块首屏。"""
    client, _ = logged_client
    body = client.get("/land").data.decode("utf-8")
    for href in (
        "/dashboard",               # 总览
        "/inventory",               # 库存管理
        "/production",              # 生产管理
        "/store-ordering/catalog",  # 门店订货
        "/procurement/store",       # 采购
        "/notifications",           # 通知
        "/users",                   # 管理员
    ):
        assert f'href="{href}"' in body, f"admin land page missing module card href: {href}"


def test_land_page_forecast_merged_into_procurement(logged_client):
    """预测看板已并入「采购」模块：land 不再有独立 /forecast 卡。"""
    client, _ = logged_client
    body = client.get("/land").data.decode("utf-8")
    assert "预测" in body
    assert 'href="/forecast"' not in body


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


def test_land_page_staff_sees_modules_without_admin(staff_client):
    """staff 的 /land 只含其模块卡：无采购 / 管理员 / 预测。"""
    client, _ = staff_client
    body = client.get("/land").data.decode("utf-8")
    # 可见模块卡
    assert "/inventory" in body      # 库存管理
    assert "/production" in body     # 生产管理
    assert "/notifications" in body  # 通知
    # 不可见
    assert "/procurement/store" not in body
    assert "/users" not in body
    import re
    hrefs = re.findall(r'href="(/forecast[^"]*)"', body)
    non_item_hrefs = [
        h for h in hrefs
        if not h.startswith("/forecast/item/") and not h.startswith("/forecast/product/")
    ]
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
    # The badge uses class="nav-badge">N</span>. Look for that exact pattern.
    import re
    assert re.search(r'class="nav-badge"[^>]*>(\d+)</span>', body), "expected unread count badge on /land"


# ---------------------------------------------------------------------------
# Subproject 6 — Agent MPC nav + land card (admin only)
# ---------------------------------------------------------------------------
