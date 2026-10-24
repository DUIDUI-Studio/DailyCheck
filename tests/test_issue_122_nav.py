"""Issue #122（2026-10-19）—— 订货审批导航入口可达性。

背景：DC 用户（distribution_center 仓库类型）反馈"审批只能通过通知，网站上
找不到入口"。根因：base.html 侧栏 + /land 的「门店订货」模块组里，
只有 admin 的子项和 storefront 的「我的订单」子项，没有 DC 用户的「订货审批」。
本测试断言三种角色（admin / DC staff / DC manager / storefront manager /
storefront staff）下，导航链接集对该角色正确显隐「订货审批」。

铁律：「侧栏链接集 == /land 链接集」（`test_carrier_sets_stay_bidirectional`
在 tests/test_reports_health.py 已建），所以本测试同时校验两侧。"""
from __future__ import annotations

import re

import pytest

# Issue #122 验收：/store-ordering/review 是导航可达的入口（DC 视角）
REVIEW_PATH = "/store-ordering/review"
# Issue #19（2026-10-24）：订单管理与订货审批合并为同一页面，admin 的入口是
# /admin/orders，DC 的是 /review —— 两条 URL 渲染同一张模板。
ADMIN_ORDERS_PATH = "/store-ordering/admin/orders"


def _sidebar_links(body: str) -> set[str]:
    start = body.index('<aside class="sidebar')
    end = body.index("</aside>", start)
    return set(re.findall(r'href="([^"]+)"', body[start:end]))


def _land_links(body: str) -> set[str]:
    """从 /land 响应里抽出所有 land-card 内的 href（导航入口集合）。"""
    start = body.index('<div class="land-grid">')
    end = body.index("</main>", start)
    return set(re.findall(r'href="([^"]+)"', body[start:end]))


class TestNavHasReviewEntry:
    """Issue #122：DC 用户能在导航里看到「订货审批」入口（侧栏 + /land 都给）。"""

    @pytest.mark.parametrize("role", ["staff", "manager", "admin"])
    def test_dc_user_sees_review_in_sidebar_and_land(self, nav_client_factory, role):
        """DC 仓库类型下，无论 staff/manager，审批入口都该可达。"""
        client, _ = nav_client_factory("distribution_center", role)
        sidebar = _sidebar_links(client.get("/dashboard").get_data(as_text=True))
        land = _land_links(client.get("/land").get_data(as_text=True))
        assert REVIEW_PATH in sidebar, (
            f"DC {role} 侧栏缺 {REVIEW_PATH}；导航组里只暴露了 admin 子项，"
            f"DC 用户找不到入口。完整侧栏：{sidebar}"
        )
        assert REVIEW_PATH in land, (
            f"DC {role} /land 缺 {REVIEW_PATH}；侧栏 / /land 链接集必须一致。完整：{land}"
        )

    def test_platform_admin_sees_order_board_in_sidebar_and_land(self, nav_client_factory):
        """平台管理员全权可见（任意仓库类型）。

        Issue #19 合并后 admin 的订单入口是 /admin/orders（同一页面、平台视角）；
        #122 的验收点「审批入口导航可达」由它满足，故此处断言该 URL 而非 /review。
        """
        client, _ = nav_client_factory("storefront", "staff", is_admin=True)
        sidebar = _sidebar_links(client.get("/dashboard").get_data(as_text=True))
        land = _land_links(client.get("/land").get_data(as_text=True))
        assert ADMIN_ORDERS_PATH in sidebar
        assert ADMIN_ORDERS_PATH in land
        # 合并后同一页面：点进去就是订单板（待审批页签在）。
        resp = client.get(ADMIN_ORDERS_PATH)
        assert resp.status_code == 200
        assert "待审批" in resp.get_data(as_text=True)

    @pytest.mark.parametrize("role", ["staff", "manager"])
    def test_storefront_user_does_not_see_review(self, nav_client_factory, role):
        """storefront 角色不审批他人的订单，不应看到入口（gate 收紧）。"""
        client, _ = nav_client_factory("storefront", role)
        sidebar = _sidebar_links(client.get("/dashboard").get_data(as_text=True))
        land = _land_links(client.get("/land").get_data(as_text=True))
        assert REVIEW_PATH not in sidebar, (
            f"storefront {role} 不应看到 {REVIEW_PATH}：审批是 DC / admin 权限。"
        )
        assert REVIEW_PATH not in land
