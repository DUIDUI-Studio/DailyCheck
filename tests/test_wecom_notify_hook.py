"""挂钩层测试：订货单提交点 → 企微通道（blueprints/store_ordering.py）。

只钉挂钩包装 ``_notify_wecom_new_order``，不跑完整路由：
- 计数从订单行项目取，取不到就传 None（模板据此省略「品项」行）
- 后台发送（不阻塞下单请求）
- 通道内部异常一律吞掉，最多留 WARNING——订单已经落库，通知失败不得回滚

真值语义由 tests/test_wecom_notify_pure.py 覆盖，这里只保证接线正确。
"""
from __future__ import annotations

import logging

from blueprints import store_ordering as so
from blueprints import wecom_notify_pure as wn


def _order(**over):
    order = {
        "id": 42,
        "order_no": "SO-20261016-0007",
        "store_warehouse_code": "wh_003",
        "created_at": "2026-10-16 09:07:05",
        "order_items": [{"quantity": 2}, {"quantity": 10.5}],
    }
    order.update(over)
    return order


def _capture(monkeypatch):
    seen: dict = {}

    def _record(order, item_count=None, total_qty=None, background=True):
        seen.update(
            order=order, item_count=item_count, total_qty=total_qty, background=background,
        )

    monkeypatch.setattr(wn, "notify_store_order_submitted", _record)
    return seen


def test_hook_passes_order_and_counts(monkeypatch):
    seen = _capture(monkeypatch)
    so._notify_wecom_new_order(_order())
    assert seen["order"]["order_no"] == "SO-20261016-0007"
    assert seen["item_count"] == 2
    assert seen["total_qty"] == 12.5
    assert seen["background"] is True  # 异步：不阻塞下单请求


def test_hook_omits_counts_when_unavailable(monkeypatch):
    seen = _capture(monkeypatch)
    so._notify_wecom_new_order(_order(order_items=[], id=43))
    assert seen["item_count"] is None
    assert seen["total_qty"] is None


def test_hook_swallows_channel_exception(monkeypatch, caplog):
    def _boom(*_args, **_kwargs):
        raise RuntimeError("wecom transport down")

    monkeypatch.setattr(wn, "notify_store_order_submitted", _boom)
    with caplog.at_level(logging.WARNING):
        so._notify_wecom_new_order(_order())  # 不得抛
    assert "wecom_notify_hook_failed" in caplog.text
    assert "SO-20261016-0007" in caplog.text


def test_hook_ignores_empty_order(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        wn, "notify_store_order_submitted", lambda *a, **k: calls.append((a, k)),
    )
    so._notify_wecom_new_order(None)
    so._notify_wecom_new_order({})
    assert calls == []
