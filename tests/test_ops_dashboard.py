"""运维看板路由 /ops：只读托管渲染产物 + 平台超管门禁。

背景：看板产物在 VPS 上是 /var/lib/dailycheck-dashboard/latest.html，
由 dc-dashboard-refresh.timer 每 15 分钟重写。此前没有任何 URL 能打开它，
只能 ssh/scp 取文件。本路由把它挂到应用内，复用登录态。
"""
from __future__ import annotations

import permissions_manifest
from blueprints import ops as ops_module

SAMPLE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>DailyCheck · 交付看板</title></head>
<body><h1>交付看板</h1><div id="marker">SENTINEL-OK</div></body></html>
"""

NO_HEAD = "<html><body><p>SENTINEL-NOHEAD</p></body></html>"


def _serve(tmp_path, monkeypatch, text: str) -> None:
    path = tmp_path / "latest.html"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("DAILYCHECK_OPS_DASHBOARD", str(path))


def test_admin_sees_rendered_file(logged_client, tmp_path, monkeypatch):
    client, _ = logged_client
    _serve(tmp_path, monkeypatch, SAMPLE)
    resp = client.get("/ops")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "SENTINEL-OK" in body
    assert "交付看板" in body


def test_admin_response_is_not_cached(logged_client, tmp_path, monkeypatch):
    """文件每 15 分钟被覆盖，浏览器缓存会让用户一直看旧图。"""
    client, _ = logged_client
    _serve(tmp_path, monkeypatch, SAMPLE)
    resp = client.get("/ops")
    assert "no-store" in resp.headers.get("Cache-Control", "")


def test_autorefresh_meta_injected_after_head(logged_client, tmp_path, monkeypatch):
    client, _ = logged_client
    _serve(tmp_path, monkeypatch, SAMPLE)
    body = client.get("/ops").get_data(as_text=True)
    marker = '<meta http-equiv="refresh" content="300">'
    assert marker in body
    # 落在 head 内，不能跑到 <html> 之前（否则部分浏览器不认）
    assert body.index("<head>") < body.index(marker) < body.index("SENTINEL-OK")


def test_autorefresh_meta_prepended_when_no_head(logged_client, tmp_path, monkeypatch):
    client, _ = logged_client
    _serve(tmp_path, monkeypatch, NO_HEAD)
    body = client.get("/ops").get_data(as_text=True)
    assert body.startswith('<meta http-equiv="refresh" content="300">')
    assert "SENTINEL-NOHEAD" in body


def test_missing_file_returns_explainer_not_500(logged_client, tmp_path, monkeypatch):
    """产物没生成时给可执行提示，不能 500 也不能假装有数据。"""
    client, _ = logged_client
    missing = tmp_path / "not-created.html"
    monkeypatch.setenv("DAILYCHECK_OPS_DASHBOARD", str(missing))
    resp = client.get("/ops")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "看板还没有生成" in body
    assert "dc-dashboard-refresh" in body
    assert str(missing) in body


def test_empty_file_treated_as_missing(logged_client, tmp_path, monkeypatch):
    client, _ = logged_client
    _serve(tmp_path, monkeypatch, "   \n")
    body = client.get("/ops").get_data(as_text=True)
    assert "看板还没有生成" in body


def test_non_admin_forbidden(viewer_client, tmp_path, monkeypatch):
    """内容含部署耗时与 token 用量，非平台超管不得查看。"""
    client, _ = viewer_client
    _serve(tmp_path, monkeypatch, SAMPLE)
    resp = client.get("/ops")
    assert resp.status_code == 403


def test_anonymous_redirected_to_login(logged_client, tmp_path, monkeypatch):
    client, _ = logged_client
    _serve(tmp_path, monkeypatch, SAMPLE)
    with client.session_transaction() as sess:
        sess.clear()
    resp = client.get("/ops")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_default_path_is_vps_artifact(monkeypatch):
    monkeypatch.delenv("DAILYCHECK_OPS_DASHBOARD", raising=False)
    assert str(ops_module.dashboard_path()) == ops_module.DEFAULT_PATH
    assert ops_module.DEFAULT_PATH == "/var/lib/dailycheck-dashboard/latest.html"


def test_route_registered_as_platform_admin():
    """权限矩阵是单一事实来源：路由未登记会被矩阵测试拦下，这里再锁一次意图。"""
    assert permissions_manifest.ROUTE_MATRIX["ops.ops_dashboard"] == permissions_manifest.PLATFORM
