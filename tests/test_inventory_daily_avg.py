"""/inventory 日均消耗分母 = 固定 7 的渲染测试。"""
from tests.conftest import _seed_item, _seed_outbound


def test_inventory_card_daily_avg_suffix_is_7d(logged_client):
    """7 日消耗 = 14 件时,卡片日均行含「/7d」后缀(固定 7 天分母)。"""
    client, wh_path = logged_client
    item_id, _ = _seed_item(wh_path, "avgItemA", qty=100, unit_cost=10)
    _seed_outbound(wh_path, item_id, qty=14)  # 7 日消耗 = 14,日均 = 2

    resp = client.get("/inventory")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    # /7d 后缀至少出现 2 次:「7日消耗」行 + 「日均消耗」行
    assert body.count("/7d") >= 2
    # 日均数字 = 14 / 7 = 2,卡片行里出现
    assert ">2<small>件/7d</small>" in body


def test_inventory_zero_consume_renders_zero_not_dash(logged_client):
    """7 日消耗 = 0 时,卡片日均渲染 0件/7d,而不是 —(占位)。"""
    client, wh_path = logged_client
    _seed_item(wh_path, "avgItemZero", qty=100, unit_cost=10)
    # 不插入任何 outbound,7 日消耗 = 0

    resp = client.get("/inventory")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    # 卡片日均行 = 0/7 = 0,渲染为 0件/7d
    assert ">0<small>件/7d</small>" in body
    # 「日均消耗」标签后面紧跟 — 的占位不应出现
    assert "日均消耗</span>\n          <span class=\"metric-value small\">\n            —" not in body


def test_inventory_card_daily_avg_matches_top_summary(logged_client):
    """卡片内日均 = 顶部汇总块「7日日均」(两者口径完全一致)。

    日均 = 21 / 7 = 3,卡片内 + 顶部汇总各出现一次。
    """
    client, wh_path = logged_client
    item_id, _ = _seed_item(wh_path, "avgItemB", qty=100, unit_cost=10)
    _seed_outbound(wh_path, item_id, qty=21)  # 7 日消耗 = 21,日均 = 3

    resp = client.get("/inventory")
    body = resp.data.decode("utf-8")
    # 顶部汇总「7日日均」= ns.consume_qty / 7 = 21/7 = 3
    # 卡片日均 = c7 / 7 = 21/7 = 3
    # 期望 ">3<small>件/7d</small>" 出现至少 1 次(卡片内)
    assert ">3<small>件/7d</small>" in body
    # 顶部汇总块「7日日均」渲染: inv-stat-value 里含 3(followed by label closing)
    assert 'inv-stat-value">3</span>' in body
