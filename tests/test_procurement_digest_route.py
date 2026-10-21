"""`/forecast/digest*` 路由测试（Issue #145）。

三条纪律：
  * 预览页与定时邮件同一条装配链 —— 页面能开就说明装配没坏；
  * 手动发送的 HTTP 状态码必须区分「没配凭证」与「SMTP 失败」，
    且成功判据只认响应体里的 `sent`（不靠 200）；
  * 权限是平台级：非超管一律 403，不能因为「只是预览」就放行。
"""
from __future__ import annotations

from datetime import date


def test_digest_preview_renders_for_platform_admin(logged_client):
    client, _wh = logged_client
    resp = client.get("/forecast/digest")
    assert resp.status_code == 200
    assert resp.mimetype == "text/html"
    body = resp.get_data(as_text=True)
    assert "配送中心采购建议" in body
    # 预览页顶部必须回显邮件主题，方便核对收件内容
    assert "邮件主题" in body


def test_digest_preview_rejects_non_admin(manager_client):
    client, _wh = manager_client
    resp = client.get("/forecast/digest")
    assert resp.status_code == 403


def test_digest_send_requires_configuration(logged_client, monkeypatch):
    import config as config_module

    monkeypatch.setattr(config_module, "DIGEST_MAIL_USER", "", raising=False)
    monkeypatch.setattr(config_module, "DIGEST_MAIL_PASSWORD", "", raising=False)
    client, _wh = logged_client
    resp = client.post("/forecast/digest/send")
    assert resp.status_code == 503
    body = resp.get_json()
    assert body["sent"] is False
    assert body["reason"] == "not_configured"


def test_digest_send_success_returns_sent_true(logged_client, monkeypatch):
    from blueprints import procurement_digest

    monkeypatch.setattr(
        procurement_digest,
        "send_digest",
        lambda **kwargs: {"sent": True, "reason": "ok", "rows": 3,
                          "recipients": ["o***@corp.example"]},
    )
    client, _wh = logged_client
    resp = client.post("/forecast/digest/send")
    assert resp.status_code == 200
    assert resp.get_json()["sent"] is True


def test_digest_send_failure_maps_to_502(logged_client, monkeypatch):
    from blueprints import procurement_digest

    monkeypatch.setattr(
        procurement_digest,
        "send_digest",
        lambda **kwargs: {"sent": False, "reason": "send_failed"},
    )
    client, _wh = logged_client
    resp = client.post("/forecast/digest/send")
    assert resp.status_code == 502
    assert resp.get_json()["sent"] is False


def test_digest_send_rejects_non_admin(manager_client):
    client, _wh = manager_client
    resp = client.post("/forecast/digest/send")
    assert resp.status_code == 403


def test_digest_send_requires_post(logged_client):
    client, _wh = logged_client
    resp = client.get("/forecast/digest/send")
    assert resp.status_code == 405


def test_collect_digest_on_storefront_only_env_has_no_dc(logged_client):
    """只有一个门店仓时：清单为空 + 明确告警「没找到 DC」，而不是静默空表。"""
    from blueprints.procurement_digest import collect_digest

    digest = collect_digest(today=date(2026, 10, 27))
    assert digest["item_count"] == 0
    assert digest["dc_configured"] is False
    assert any("未找到配送中心仓" in w for w in digest["warnings"])
