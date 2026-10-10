"""品项「创建权 / 发布权」收敛回归（2026-10-10）。

背景：品项主档（warehouse.items）此前存在三条分散入口，导致门店与研发中心
各自能造品项、互相不一致：

  1. `/items` POST            —— 门店 / 研发中心手工新增品项
  2. `/items/publish`         —— 研发中心把品项「发布 / 同步」到门店
  3. 页面上的「新增库存品」表单 与 rd 侧「主数据扇出 / 同步历史」按钮

owner 2026-10-10 拍板收敛：**品项主档的唯一创建入口 = 平台管理员的
「品项主数据」(`/canonical/*`)**；门店与研发中心一律只能消费
（查看 / 出入库 / 生产 / 盘点 / 调整 / 订货 / 配方）。

本文件锁定收敛后的行为：
  · `/items` POST 对 admin / 门店 / 研发中心**一律零写入**，仅 flash + 302
  · `/items` 页面不再渲染「新增库存品」表单，也不再有 rd 的发布/同步按钮
  · `/items/publish` 仍只做重定向（→ 主数据扇出），不再是真发布通道
  · `/items/publish/history` 只读保留（历史审计）
"""
from __future__ import annotations

import sqlite3

from tests.conftest import _seed_item, _wh

CREATE_FORM_MARKERS = ('name="name"', '新增库存品</button>')
PUBLISH_BUTTON_MARKERS = ("items.publish/history", "/items/publish/history",
                          "/canonical/fanout")


def _count_items(wh_path) -> int:
    conn = _wh(wh_path)
    n = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    conn.close()
    return n


def _set_warehouse_type(wh_path, wh_type: str) -> None:
    """把测试仓的业务类型改成 rd / storefront（master.db 在 wh_path 的祖父目录）。"""
    master = wh_path.parent.parent / "master.db"
    conn = sqlite3.connect(master)
    conn.execute("UPDATE warehouses SET warehouse_type=? WHERE id=1", (wh_type,))
    conn.commit()
    conn.close()


# ──────────────────────── /items POST：创建权已关闭 ────────────────────────

def test_items_post_rejected_for_admin_storefront(logged_client):
    """平台管理员在门店上下文提交新增，也必须零写入（创建权只在主数据页）。"""
    client, wh_path = logged_client
    before = _count_items(wh_path)

    resp = client.post(
        "/items",
        data={"name": "__LOCKDOWN_PROBE__", "category_id": "1",
              "quantity": "0", "unit_cost": "0", "selling_price": "0"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/items")
    assert _count_items(wh_path) == before, "收敛后 /items POST 不得新增任何行"


def test_items_post_rejected_for_rd_warehouse(logged_client):
    """研发中心上下文同样零写入。"""
    client, wh_path = logged_client
    _set_warehouse_type(wh_path, "rd")
    before = _count_items(wh_path)

    resp = client.post(
        "/items",
        data={"name": "__LOCKDOWN_PROBE_RD__", "category_id": "1",
              "quantity": "0", "unit_cost": "12", "selling_price": "30"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert _count_items(wh_path) == before


def test_items_post_flashes_guidance(logged_client):
    """拒绝后必须给出门店看得懂的引导（不是静默失败）。"""
    client, wh_path = logged_client
    resp = client.post(
        "/items",
        data={"name": "__LOCKDOWN_PROBE__", "category_id": "1"},
        follow_redirects=True,
    )
    body = resp.get_data(as_text=True)
    assert "品项主数据" in body
    assert "不再自行新增" in body


# ──────────────────────── /items 页面：无创建 / 发布入口 ────────────────────────

def test_items_page_has_no_create_form(logged_client):
    client, wh_path = logged_client
    _seed_item(wh_path, "在售冰碗", 5, 10)

    body = client.get("/items").get_data(as_text=True)
    for marker in CREATE_FORM_MARKERS:
        assert marker not in body, f"收敛后页面不应再出现 {marker!r}"


def test_items_page_shows_guidance_and_link(logged_client):
    client, wh_path = logged_client
    body = client.get("/items").get_data(as_text=True)
    assert "不再自行新增品项" in body
    # admin 能看到去主数据的入口
    assert "/canonical/list" in body


def test_items_page_has_no_publish_or_sync_button(logged_client):
    """rd 侧「主数据扇出 / 同步历史」按钮已移除。"""
    client, wh_path = logged_client
    _set_warehouse_type(wh_path, "rd")

    body = client.get("/items").get_data(as_text=True)
    assert "同步历史" not in body
    assert "主数据扇出" not in body
    for marker in PUBLISH_BUTTON_MARKERS:
        assert marker not in body, f"rd 品项页不应再出现 {marker!r}"


# ──────────────────────── /items/publish：仅重定向，不再真发布 ────────────────────────

def test_publish_route_redirects_to_fanout(logged_client):
    client, wh_path = logged_client
    for verb in ("GET", "POST"):
        resp = client.open("/items/publish", method=verb,
                           data={"summary": "probe"}, follow_redirects=False)
        assert resp.status_code == 302, f"{verb} /items/publish 应重定向"
        assert resp.headers["Location"].endswith("/canonical/fanout")


def test_publish_history_stays_readable(logged_client):
    """/items/publish/history 保留为只读审计页。"""
    client, wh_path = logged_client
    resp = client.get("/items/publish/history")
    assert resp.status_code == 200


# ──────────────────────── 门店员工：本就无权限 ────────────────────────

def test_staff_cannot_reach_items_create(staff_client):
    """staff 连 /items 都进不去（require_platform_admin），更不可能新增。"""
    client, wh_path = staff_client
    before = _count_items(wh_path)
    resp = client.post("/items", data={"name": "__PROBE__", "category_id": "1"})
    assert resp.status_code == 403
    assert _count_items(wh_path) == before
