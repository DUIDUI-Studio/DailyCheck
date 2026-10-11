"""CSRF 保护（P0-3）行为测试。

约定：create_app() 里 _csrf_testing_gate 在 TESTING=True 时默认关闭 CSRF
（存量测试零改动）；本文件通过 WTF_CSRF_FORCE_ENABLED=True 显式开启后，
验证强制拦截与 token 放行语义。
"""
import re

import pytest


def _force_csrf(client):
    client.application.config["WTF_CSRF_FORCE_ENABLED"] = True


@pytest.fixture
def csrf_client(logged_client):
    """开启 CSRF 的登录态客户端（app 为 fixture 内新建，无跨用例污染）。"""
    client, wh_path = logged_client
    _force_csrf(client)
    return client, wh_path


def _csrf_token_from(html: str) -> str:
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    assert m, "页面应渲染 csrf_token hidden input"
    return m.group(1)


def test_login_page_renders_csrf_token(csrf_client):
    client, _ = csrf_client
    r = client.get("/login")
    assert r.status_code == 200
    assert 'name="csrf_token"' in r.get_data(as_text=True)


def test_post_without_token_rejected_400(csrf_client):
    """登录态下无 token POST 必须被 CSRF 层 400 拦截（删除品项端点）。"""
    client, _ = csrf_client
    r = client.post("/items/1/delete")
    assert r.status_code == 400


def test_login_post_without_token_rejected_400(csrf_client):
    client, _ = csrf_client
    r = client.post("/login", data={"username": "x", "password": "y"})
    assert r.status_code == 400


def test_login_post_with_rendered_token_passes_csrf(csrf_client):
    """带登录页渲染出来的 token：CSRF 层放行（状态码非 400，凭据校验另行处理）。"""
    client, _ = csrf_client
    page = client.get("/login").get_data(as_text=True)
    token = _csrf_token_from(page)
    r = client.post(
        "/login", data={"username": "x", "password": "y", "csrf_token": token}
    )
    assert r.status_code != 400


def test_items_delete_with_token_passes_csrf(csrf_client):
    """带同会话渲染的 token：CSRF 层放行（后续 404/302 均证明过了 CSRF）。"""
    client, _ = csrf_client
    page = client.get("/login").get_data(as_text=True)
    token = _csrf_token_from(page)
    r = client.post("/items/1/delete", data={"csrf_token": token})
    assert r.status_code != 400


def test_testing_mode_default_disables_csrf(logged_client):
    """存量契约：TESTING 下未开 FORCE，无 token POST 不被 400。"""
    client, _ = logged_client
    r = client.post("/items/1/delete")
    assert r.status_code != 400


def test_force_flag_round_trip(csrf_client):
    """FORCE 开启后关掉，行为回到 TESTING 默认（防 gate 逻辑写反）。"""
    client, _ = csrf_client
    client.application.config["WTF_CSRF_FORCE_ENABLED"] = False
    r = client.post("/items/1/delete")
    assert r.status_code != 400
