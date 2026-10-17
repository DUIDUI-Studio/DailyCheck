"""Unit tests for blueprints/wecom_notify_pure.py（企微群机器人推送）。

约定：
- **不透网**：所有用例 monkeypatch ``urllib.request.urlopen``（或更上层的
  ``send_webhook_markdown``），断言只看「调用了几次 / 发了什么 / 返回值」。
- **不含真实密钥**：webhook 一律是运行期拼出来的假地址；另有专门用例断言
  日志里永不出现 key。
- 覆盖口径：模板字段与链接、base_url 尾斜杠、未配置不发送、errcode 成功 /
  业务失败、首败重试成功（2 次调用）、HTTP / 超时 / 非 JSON、background
  两态、异常只记日志不抛。
"""
from __future__ import annotations

import json
import logging
import urllib.error

import pytest

import config
from blueprints import wecom_notify_pure as wn
from blueprints.store_ordering_pure import EVENT_ORDER_APPROVED, EVENT_ORDER_SUBMITTED

# 运行期拼接的假地址（刻意拆开，避免被密钥扫描规则误判为真实 webhook）
FAKE_KEY = "fake" + "-" + "0" * 20
FAKE_WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" + FAKE_KEY
OK_BODY = b'{"errcode":0,"errmsg":"ok"}'


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


class _Recorder:
    """按脚本回放 urlopen：元素是 bytes（响应体）或异常实例。"""

    def __init__(self, script: list | None = None):
        self.script = list(script or [])
        self.calls: list[dict] = []

    def __call__(self, request, timeout=None):
        self.calls.append({
            "url": request.full_url,
            "data": request.data,
            "timeout": timeout,
            "method": request.get_method(),
        })
        action = self.script.pop(0) if self.script else OK_BODY
        if isinstance(action, BaseException):
            raise action
        return _FakeResponse(action)


@pytest.fixture
def urlopen(monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(wn.urllib.request, "urlopen", recorder)
    return recorder


@pytest.fixture
def enabled(monkeypatch):
    """把 webhook / base_url 换成假值（不改真实 env）。"""
    monkeypatch.setattr(config, "WECOM_STORE_ORDER_WEBHOOK", FAKE_WEBHOOK)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://example.invalid")
    return FAKE_WEBHOOK


def _order(**over):
    order = {
        "id": 42,
        "order_no": "SO-20261016-0007",
        "store_warehouse_code": "wh_003",
        "dc_warehouse_code": "wh_000",
        "created_at": "2026-10-16 09:07:05",
        "shipping_fee": 987.65,
        "order_items": [
            {"canonical_id": 1, "quantity": 2},
            {"canonical_id": 2, "quantity": 10.5},
        ],
    }
    order.update(over)
    return order


def _payload(call: dict) -> dict:
    return json.loads(call["data"].decode("utf-8"))


# ---------------------------------------------------------------------------
# 模板
# ---------------------------------------------------------------------------

def test_markdown_full_template():
    md = wn.build_store_order_markdown(_order(), base_url="https://kjtang.cn")
    assert md.splitlines() == [
        "## 新订货单待处理",
        "> 单号：SO-20261016-0007",
        "> 门店：wh_003",
        "> 品项：2 项 / 共 12.5 件",
        "> 时间：10-16 09:07",
        "[查看单据](https://kjtang.cn/store-ordering/orders/42)",
    ]


@pytest.mark.parametrize("base_url", ["https://kjtang.cn", "https://kjtang.cn/", " https://kjtang.cn// "])
def test_markdown_base_url_trailing_slash_normalized(base_url):
    md = wn.build_store_order_markdown(_order(), base_url=base_url)
    assert "[查看单据](https://kjtang.cn/store-ordering/orders/42)" in md
    assert "//store-ordering" not in md


def test_markdown_without_base_url_keeps_relative_link():
    md = wn.build_store_order_markdown(_order())
    assert "[查看单据](/store-ordering/orders/42)" in md


def test_markdown_has_no_amount():
    md = wn.build_store_order_markdown(_order(shipping_fee=987.65))
    assert "987.65" not in md
    assert "金额" not in md
    assert "¥" not in md


def test_markdown_omits_counts_when_unavailable():
    order = {"id": 7, "order_no": "SO-X", "store_warehouse_code": "wh_009"}
    md = wn.build_store_order_markdown(order, base_url="https://kjtang.cn")
    assert "品项" not in md
    assert "> 单号：SO-X" in md
    assert "[查看单据](https://kjtang.cn/store-ordering/orders/7)" in md


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"item_count": 5, "total_qty": 7}, "> 品项：5 项 / 共 7 件"),
        ({"item_count": 5}, "> 品项：5 项"),
        ({"total_qty": 7.0}, "> 品项：共 7 件"),
        ({"item_count": 0, "total_qty": 0}, "> 品项：0 项 / 共 0 件"),
    ],
)
def test_markdown_counts_variants(kwargs, expected):
    order = {"id": 1, "order_no": "SO-Y", "store_warehouse_code": "wh_001"}
    assert expected in wn.build_store_order_markdown(order, **kwargs)


def test_markdown_prefers_store_name_over_code():
    md = wn.build_store_order_markdown(_order(store_name="新世界店"))
    assert "> 门店：新世界店" in md
    assert "wh_003" not in md


def test_markdown_submitted_at_overrides_created_at():
    md = wn.build_store_order_markdown(_order(), submitted_at="2026-12-31 23:58:59")
    assert "> 时间：12-31 23:58" in md


@pytest.mark.parametrize(
    "created_at",
    ["2026-10-16 09:07", "2026-10-16 09:07:05.123456", "2026-10-16T09:07:05"],
)
def test_markdown_tolerates_timestamp_shapes(created_at):
    md = wn.build_store_order_markdown(_order(created_at=created_at))
    assert "> 时间：10-16 09:07" in md


def test_markdown_omits_time_when_unparseable():
    md = wn.build_store_order_markdown(_order(created_at="not-a-time"))
    assert "时间" not in md
    assert "> 单号：SO-20261016-0007" in md


def test_markdown_has_no_emoji():
    """P0：文案不许出现 emoji / 变体选择符。"""
    md = wn.build_store_order_markdown(_order(), base_url="https://kjtang.cn")
    forbidden = [ch for ch in md if ord(ch) >= 0x1F000 or ch in "\ufe0f\u2764\u2b50"]
    assert forbidden == []


def test_order_item_counts():
    assert wn.order_item_counts(_order()) == (2, 12.5)
    assert wn.order_item_counts({"order_items": []}) == (None, None)
    assert wn.order_item_counts({}) == (None, None)
    assert wn.order_item_counts(None) == (None, None)


# ---------------------------------------------------------------------------
# 发送
# ---------------------------------------------------------------------------

def test_send_success(urlopen):
    assert wn.send_webhook_markdown(FAKE_WEBHOOK, "## hi") is True
    assert len(urlopen.calls) == 1
    call = urlopen.calls[0]
    assert call["url"] == FAKE_WEBHOOK
    assert call["timeout"] == 3  # 默认 3s 超时
    assert call["method"] == "POST"
    assert _payload(call) == {"msgtype": "markdown", "markdown": {"content": "## hi"}}


def test_send_errcode_nonzero_retries_once(urlopen):
    urlopen.script = [b'{"errcode":93000,"errmsg":"invalid webhook key"}'] * 2
    assert wn.send_webhook_markdown(FAKE_WEBHOOK, "## hi") is False
    assert len(urlopen.calls) == 2


def test_send_retry_then_success(urlopen):
    urlopen.script = [urllib.error.URLError("connection reset"), OK_BODY]
    assert wn.send_webhook_markdown(FAKE_WEBHOOK, "## hi") is True
    assert len(urlopen.calls) == 2


@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.HTTPError(FAKE_WEBHOOK, 500, "boom", {}, None),
        urllib.error.URLError("dns"),
        TimeoutError("timed out"),
        OSError("socket closed"),
        b"<html>502 Bad Gateway</html>",
        b'{"errmsg":"no errcode"}',
    ],
)
def test_send_failures_return_false_and_never_raise(urlopen, failure):
    urlopen.script = [failure] * 4
    assert wn.send_webhook_markdown(FAKE_WEBHOOK, "## hi") is False
    assert len(urlopen.calls) == 2  # 1 次发送 + 1 次重试


def test_send_unexpected_exception_swallowed(urlopen):
    urlopen.script = [RuntimeError("bug in transport")] * 2
    assert wn.send_webhook_markdown(FAKE_WEBHOOK, "## hi") is False


def test_send_retries_zero_means_single_attempt(urlopen):
    urlopen.script = [b'{"errcode":1}']
    assert wn.send_webhook_markdown(FAKE_WEBHOOK, "## hi", retries=0) is False
    assert len(urlopen.calls) == 1


def test_send_empty_url_does_not_network(urlopen):
    assert wn.send_webhook_markdown("", "## hi") is False
    assert urlopen.calls == []


def test_send_errcode_as_string_zero_accepted(urlopen):
    urlopen.script = [b'{"errcode":"0","errmsg":"ok"}']
    assert wn.send_webhook_markdown(FAKE_WEBHOOK, "## hi") is True


def test_send_never_logs_webhook_key(urlopen, caplog):
    urlopen.script = [urllib.error.URLError("down")] * 2
    with caplog.at_level(logging.WARNING):
        wn.send_webhook_markdown(FAKE_WEBHOOK, "## hi")
    assert FAKE_KEY not in caplog.text
    assert "key=***" in caplog.text


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (FAKE_WEBHOOK, "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=***"),
        ("", ""),
        ("not-a-url", "***not-a-url"),
    ],
)
def test_mask_webhook_url(url, expected):
    assert wn.mask_webhook_url(url) == expected


# ---------------------------------------------------------------------------
# 业务入口
# ---------------------------------------------------------------------------

def test_event_map_registers_submitted_only():
    assert wn.WEBHOOK_EVENT_MAP[EVENT_ORDER_SUBMITTED] == "WECOM_STORE_ORDER_WEBHOOK"
    assert EVENT_ORDER_APPROVED not in wn.WEBHOOK_EVENT_MAP


def test_notify_disabled_when_webhook_empty(monkeypatch, urlopen):
    monkeypatch.setattr(config, "WECOM_STORE_ORDER_WEBHOOK", "")
    assert wn.notify_store_order_submitted(_order()) is None
    assert urlopen.calls == []


def test_notify_sync_sends_once(enabled, urlopen):
    wn.notify_store_order_submitted(_order(), background=False)
    assert len(urlopen.calls) == 1
    content = _payload(urlopen.calls[0])["markdown"]["content"]
    assert "SO-20261016-0007" in content
    assert "https://example.invalid/store-ordering/orders/42" in content


def test_notify_background_uses_daemon_thread(enabled, urlopen, monkeypatch):
    captured: dict = {}

    class _Thread:
        def __init__(self, target=None, args=(), name=None, daemon=None):
            captured.update(target=target, args=args, name=name, daemon=daemon)

        def start(self):
            captured["started"] = True

    monkeypatch.setattr(wn.threading, "Thread", _Thread)
    wn.notify_store_order_submitted(_order())
    assert captured["started"] is True
    assert captured["daemon"] is True  # 不阻塞进程退出
    assert captured["target"] is wn._send_safely
    assert urlopen.calls == []  # 请求线程内零网络调用


def test_notify_swallows_send_exception(enabled, caplog, monkeypatch):
    def _boom(*_args, **_kwargs):
        raise RuntimeError("transport exploded")

    monkeypatch.setattr(wn, "send_webhook_markdown", _boom)
    with caplog.at_level(logging.WARNING):
        wn.notify_store_order_submitted(_order(), background=False)
    assert "wecom_notify_failed" in caplog.text
    assert "SO-20261016-0007" in caplog.text
    assert FAKE_KEY not in caplog.text


def test_notify_swallows_internal_error(enabled, caplog, monkeypatch):
    def _boom(*_args, **_kwargs):
        raise ValueError("build failed")

    monkeypatch.setattr(wn, "build_store_order_markdown", _boom)
    with caplog.at_level(logging.WARNING):
        wn.notify_store_order_submitted(_order(), background=False)
    assert "wecom_notify_error" in caplog.text


def test_notify_unregistered_event_is_noop(enabled, urlopen):
    wn.notify_store_order_event(EVENT_ORDER_APPROVED, _order(), background=False)
    assert urlopen.calls == []  # 扩展位未登记 → 不推送


def test_notify_tolerates_empty_order(enabled, urlopen):
    wn.notify_store_order_submitted({}, background=False)
    assert len(urlopen.calls) == 1  # 模板退化，但不抛、不崩
