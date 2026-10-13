"""Integration tests for nav entry visibility + landing page cards.

2026-10-13 导航模块化（feat/nav-modules）后的口径：
- 导航按「总览 / 库存管理 / 生产管理 / 门店订货 / 采购 / 通知 / 管理员」
  7 模块分组；模块下无可见子项时整块不渲染。
- 采购模块（采购建议 / 预测看板）与管理模块（账号 / Token / 品项数据）
  均为 admin 可见；staff 不可见。
- 侧边栏断言用 /dashboard（带侧栏的页面）；/land 断言只针对模块卡片。

2026-10-14 审核整改（fix/pr-18-nav-modules-defects）：
- 三载体（侧边栏 / 移动底栏 / `/land`）必须共用同一套模块归属与角色口径，故新增
  「侧边栏模块集 == /land 模块卡集」+「侧栏链接 ⊆ /land ∪ 移动底栏」两组不变量。
- 移动端（<961px）侧边栏 display:none，「更多」→ `/land` 是唯一全模块入口，
  故新增移动端可达性断言。v0.1 只链模块首屏，导致 汇总/盘点/入库/出库 在移动端
  整页断链 —— 本文件此前只断言侧边栏与 /land 的文案，是该回归漏检的直接原因。

⚠️ 断言取链范围必须收窄到**载体内部**（`aside.sidebar` / `nav.mobile-nav` /
`div.land-grid`），不能扫整页：整页含 ctx-bar 的「注销」(`/logout`)，它会
`session.clear()`，一旦被 GET 到，排序在它之后的链接全部以匿名身份拿到 302，
`status_code < 400` 恒真 —— 这是本文件第 2 轮审核抓到的假绿。
"""
from __future__ import annotations

import re

import pytest

# ---------------------------------------------------------------------------
# 解析辅助：把 base.html 的导航载体切出来，避免「全页 grep」把 ctx-bar / 正文误判为导航
# ---------------------------------------------------------------------------

# 会改 session 的路径：GET 后登录态失效，必须排除在可达性扫描之外
_SESSION_MUTATING = ("/logout",)


def _block(body: str, opener: str, closer: str) -> str:
    """取出 opener 起的整段（到其后第一个 closer 为止）；opener 不存在即失败。

    用 index 而非正则：opener 缺失时抛 ValueError（显式失败），不会静默返回空串
    让后续断言变成假绿。
    """
    start = body.index(opener)
    end = body.index(closer, start)
    return body[start:end]


def _sidebar_block(body: str) -> str:
    return _block(body, '<aside class="sidebar', "</aside>")


def _mobile_nav_block(body: str) -> str:
    return _block(body, '<nav class="mobile-nav', "</nav>")


def _land_grid(body: str) -> str:
    """只取 `/land` 的模块卡区域（到 `</main>` 为止）。

    刻意不扫整页：整页含 ctx-bar 的 `/logout`（见模块 docstring）。
    """
    return _block(body, '<div class="land-grid">', "</main>")


def _hrefs(block: str) -> list[str]:
    return re.findall(r'href="([^"]+)"', block)


def _group_titles(block: str) -> set[str]:
    # 容忍额外 class（如 nav-group-title nav-group-title--sub）而不误判为「模块消失」
    return set(re.findall(r'class="[^"]*nav-group-title[^"]*">([^<]+)</p>', block))


def _card_titles(body: str) -> set[str]:
    return set(re.findall(r'class="[^"]*\bland-card\b[^"]*">\s*<h3>([^<]+)</h3>', body))


def _assert_all_reachable(client, hrefs, *, context: str) -> None:
    """载体内的每个 href 都必须可达（<400）：挡住「导航暴露无权/不存在页面」。

    排除 session-mutating 路径（`/logout`）—— 它会清 session，使同批后续断言变假绿。
    """
    targets = sorted(set(hrefs) - set(_SESSION_MUTATING))
    assert targets, f"{context}: 载体解析为空（断言会假绿），请检查模板结构"
    for href in targets:
        resp = client.get(href)
        assert resp.status_code < 400, f"{context}: {href} -> {resp.status_code}"


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
# 角色 × 仓库类型矩阵：三载体模块集一致 + 移动端覆盖侧栏全部链接
# ---------------------------------------------------------------------------

_M_OVERVIEW = {"总览", "库存管理", "生产管理", "门店订货", "通知"}
_M_ADMIN_EXTRA = _M_OVERVIEW | {"管理员"}
_M_ALL = _M_ADMIN_EXTRA | {"采购"}
_M_DC = {"总览", "门店订货", "通知"}
_M_RD = {"总览", "生产管理", "通知", "管理员"}

# (warehouse_type, role, is_admin, 期望模块集)
ROLE_MATRIX = [
    ("storefront", "staff", False, _M_OVERVIEW),
    ("storefront", "manager", False, _M_OVERVIEW),
    ("storefront", "admin", False, _M_ADMIN_EXTRA),
    ("storefront", "staff", True, _M_ALL),
    ("distribution_center", "staff", False, _M_DC),
    ("distribution_center", "manager", False, _M_DC),
    ("distribution_center", "admin", False, _M_DC | {"管理员"}),
    ("rd", "staff", False, _M_RD),
    ("rd", "manager", False, _M_RD),
    ("rd", "staff", True, _M_ALL),
]


def _matrix_id(case):
    wh, role, is_admin, _ = case
    return f"{wh}-{role}-{'platform_admin' if is_admin else 'normal'}"


@pytest.mark.parametrize("case", ROLE_MATRIX, ids=_matrix_id)
def test_nav_carriers_share_same_modules(nav_client_factory, case):
    """侧边栏分组集 == /land 模块卡集 == 期望集（三载体共用同一套口径）。"""
    wh_type, role, is_admin, expected = case
    client, _ = nav_client_factory(wh_type, role, is_admin=is_admin)

    sidebar_title = _group_titles(_sidebar_block(client.get("/dashboard").data.decode("utf-8")))
    card_titles = _card_titles(client.get("/land").data.decode("utf-8"))

    assert sidebar_title == expected, f"sidebar modules: {sorted(sidebar_title)}"
    assert card_titles == expected, f"land card modules: {sorted(card_titles)}"


@pytest.mark.parametrize("case", ROLE_MATRIX, ids=_matrix_id)
def test_land_and_mobile_cover_every_sidebar_link(nav_client_factory, case):
    """不变量：侧边栏的每个链接都必须能从 /land 或移动底栏到达。

    <961px 侧边栏 `display:none`，此时「更多」→ /land + 底栏是全部入口。只断言
    「模块标题一致」不够：模块标题不变而卡内漏掉一条二级页链接，导航在移动端
    就断链（第 1 轮 #1 即此类）。故这里逐链接做集合差。
    """
    wh_type, role, is_admin, _ = case
    client, _ = nav_client_factory(wh_type, role, is_admin=is_admin)

    dash = client.get("/dashboard").data.decode("utf-8")
    sidebar = set(_hrefs(_sidebar_block(dash)))
    mobile = set(_hrefs(_mobile_nav_block(dash)))
    land = set(_hrefs(_land_grid(client.get("/land").data.decode("utf-8"))))

    assert sidebar and land, "载体解析为空（断言会假绿）"
    missing = sidebar - land - mobile
    assert not missing, f"{_matrix_id(case)}: 侧栏链接在移动端无入口: {sorted(missing)}"


@pytest.mark.parametrize("case", ROLE_MATRIX, ids=_matrix_id)
def test_nav_links_all_reachable(nav_client_factory, case):
    """三载体的每个链接对该角色都必须可达（<400），不得出现 403/404/500 死链。"""
    wh_type, role, is_admin, _ = case
    client, _ = nav_client_factory(wh_type, role, is_admin=is_admin)
    label = _matrix_id(case)

    dash = client.get("/dashboard").data.decode("utf-8")
    _assert_all_reachable(client, _hrefs(_sidebar_block(dash)), context=f"{label} sidebar")
    _assert_all_reachable(client, _hrefs(_mobile_nav_block(dash)), context=f"{label} mobile-nav")
    _assert_all_reachable(
        client, _hrefs(_land_grid(client.get("/land").data.decode("utf-8"))),
        context=f"{label} land",
    )


# ---------------------------------------------------------------------------
# 移动底栏（2026-10-13 收敛为 ≤5 项 + 「更多」）
# ---------------------------------------------------------------------------

_MOBILE_5 = ["总览", "库存管理", "门店订货", "生产管理", "更多"]


@pytest.mark.parametrize(
    "wh_type,role,is_admin,expected_labels",
    [
        ("storefront", "staff", False, _MOBILE_5),
        ("storefront", "admin", False, _MOBILE_5),
        ("storefront", "staff", True, _MOBILE_5),
        ("distribution_center", "staff", False, ["总览", "门店订货", "更多"]),
        ("distribution_center", "manager", False, ["总览", "门店订货", "更多"]),
        ("rd", "staff", False, ["总览", "配方成本", "更多"]),
        # 平台管理员在 rd 仓：is_admin 让 库存管理/生产管理 都出现，且生产管理指 /production
        ("rd", "staff", True, _MOBILE_5),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_mobile_nav_labels_by_role(nav_client_factory, wh_type, role, is_admin, expected_labels):
    """底栏条目 3~5 项随角色动态（rd 无实体库存，生产管理项直指配方成本）。

    条目数含在 `expected_labels` 里（owner 拍板 ≤5），故不再另设区间断言 —— 那种
    「定长列表 == 期望」之后再断言 `3 <= len <= 5` 是永真式，只会稀释信号。
    """
    client, _ = nav_client_factory(wh_type, role, is_admin=is_admin)
    nav = _mobile_nav_block(client.get("/dashboard").data.decode("utf-8"))
    labels = re.findall(r">([^<>]+)</a>", nav)
    hrefs = _hrefs(nav)

    assert labels == expected_labels
    assert len(hrefs) == len(set(hrefs)), f"底栏出现重复链接: {hrefs}"


def test_mobile_can_reach_inventory_subpages(nav_client_factory):
    """移动端可达性守卫（历史回归）：底栏 + /land 必须覆盖库存管理模块的二级页。

    v0.1 把侧边栏里的 汇总 / 盘点 / 入库 / 出库 从底栏删掉却没有在 /land 补回，
    且 /inventory 页内没有任何出链 → 视口 <961px 时这 4 个页面无路可达。
    """
    client, _ = nav_client_factory("storefront", "staff")
    dash = client.get("/dashboard").data.decode("utf-8")
    reachable = set(_hrefs(_mobile_nav_block(dash)))
    reachable |= set(_hrefs(_land_grid(client.get("/land").data.decode("utf-8"))))

    for href in ("/summary", "/stocktake", "/restock", "/outbound"):
        assert href in reachable, f"移动端无法抵达 {href}（底栏 + /land 链接集合内缺失）"


def test_store_admin_can_reach_import_items_on_mobile(nav_client_factory):
    """单仓 role=admin：管理员模块在侧边栏与 /land 都要出现，且导入页可达。

    v0.1 侧边栏给 role=admin 渲染了「品项批量导入」，但 /land 的 gate 只有
    `is_admin or is_rd` → 移动端无路可达该页（三载体口径不一致）。
    """
    client, _ = nav_client_factory("storefront", "admin")
    dash = client.get("/dashboard").data.decode("utf-8")
    assert "/admin/import-items" in _sidebar_block(dash)

    assert "/admin/import-items" in _land_grid(client.get("/land").data.decode("utf-8"))
    assert "/admin/import-items" not in _hrefs(_mobile_nav_block(dash)), "底栏不应塞入低频入口"
    assert client.get("/admin/import-items").status_code < 400


def test_mobile_nav_rd_points_at_recipe_cost(nav_client_factory):
    """rd 的底栏「配方成本」直接指向 /recipe-cost/，不再需要多点一层。"""
    client, _ = nav_client_factory("rd", "staff")
    nav = _mobile_nav_block(client.get("/dashboard").data.decode("utf-8"))
    assert "/recipe-cost/" in nav
    assert "/production" not in nav


# ---------------------------------------------------------------------------
# Land page cards
# ---------------------------------------------------------------------------


def test_land_page_admin_sees_all_module_cards(logged_client):
    """admin 的 /land 每模块一张卡，卡内列出该模块各页。"""
    client, _ = logged_client
    body = client.get("/land").data.decode("utf-8")
    for href in (
        "/dashboard",               # 总览
        "/summary",                 # 总览 · 汇总
        "/inventory",               # 库存管理
        "/production",              # 生产管理
        "/store-ordering/catalog",  # 门店订货
        "/procurement/store",       # 采购
        "/notifications",           # 通知
        "/users",                   # 管理员
    ):
        assert f'href="{href}"' in body, f"admin land page missing module card href: {href}"


def test_land_forecast_link_lives_inside_procurement_card(logged_client):
    """预测看板并入「采购」模块：/land 无独立预测卡，只在采购卡内作为子项链接。"""
    client, _ = logged_client
    body = client.get("/land").data.decode("utf-8")
    cards = body.split('class="land-card">')[1:]
    procurement = [c for c in cards if "<h3>采购</h3>" in c]
    assert len(procurement) == 1, "expected exactly one 采购 module card"
    assert 'href="/forecast"' in procurement[0]
    assert "<h3>预测</h3>" not in body, "预测不应再独立成卡"


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
    hrefs = re.findall(r'href="(/forecast[^"]*)"', body)
    non_item_hrefs = [
        h for h in hrefs
        if not h.startswith("/forecast/item/") and not h.startswith("/forecast/product/")
    ]
    assert non_item_hrefs == [], f"staff land page should not have /forecast link, found: {non_item_hrefs}"


# ---------------------------------------------------------------------------
# 模块卡文案必须与该角色可见链接一致（DC 角色看不到「品项管理」时不得写它）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "wh_type,role,is_admin,expect_in_desc,reject_in_desc",
    [
        ("storefront", "staff", False, "订货目录 / 我的订单", "品项管理"),
        ("storefront", "admin", False, "订货目录 / 我的订单", "品项管理"),
        ("storefront", "staff", True, "订货目录 / 我的订单 / 审单 / 品项管理", None),
        ("distribution_center", "staff", False, "订货目录", "品项管理"),
        ("distribution_center", "manager", False, "订货目录 / 品项管理", None),
    ],
)
def test_land_order_card_desc_matches_links(
    nav_client_factory, wh_type, role, is_admin, expect_in_desc, reject_in_desc
):
    """「门店订货」卡描述不得列出该角色实际看不到的页面（DC staff 即此坑）。"""
    client, _ = nav_client_factory(wh_type, role, is_admin=is_admin)
    grid = _land_grid(client.get("/land").data.decode("utf-8"))
    card = [c for c in grid.split('class="land-card">') if "<h3>门店订货</h3>" in c]
    assert len(card) == 1, "expected exactly one 门店订货 card"
    desc = re.search(r'<p>(.*?)</p>', card[0], re.S).group(1).strip()
    assert desc == expect_in_desc
    if reject_in_desc:
        assert reject_in_desc not in desc


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
    assert re.search(r'class="nav-badge"[^>]*>(\d+)</span>', body), "expected unread count badge on /land"


# ---------------------------------------------------------------------------
# Subproject 6 — Agent MPC nav + land card (admin only)
# ---------------------------------------------------------------------------
