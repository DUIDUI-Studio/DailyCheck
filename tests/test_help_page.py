"""Public help content, navigation and the existing protected feedback flow."""
from __future__ import annotations

import html
import re
import sqlite3
from urllib.parse import parse_qs, urlsplit

import pytest
from werkzeug.security import generate_password_hash

FEATURES = [
    "品类管理", "库存品管理", "出库及回滚", "盘点差异记录",
    "库存搜索和低库存提示", "补货申请及状态流转",
    "配方驱动的生产录入", "面向 AI 的 MCP 数据访问服务",
]


def _anonymous(client):
    with client.session_transaction() as session:
        session.clear()
    return client


def _feedback_link(body):
    section = body.split('id="help-feedback"', 1)[1].split("</section>", 1)[0]
    return html.unescape(re.search(r'href="([^"]+)"', section)[1]), section


def test_anonymous_help_content_and_login_navigation(logged_client):
    client, _ = logged_client
    _anonymous(client)
    response = client.get("/help")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "手机端优先的轻量库存管理系统" in body
    listing = body.split('class="help-features"', 1)[1].split("</ol>", 1)[0]
    assert re.findall(r"<li>(.*?)</li>", listing) == FEATURES
    assert body.index("项目介绍") < body.index("功能列表") < body.index("意见反馈")
    assert 'id="feedback-modal"' not in body
    assert 'href="/help"' in client.get("/login").get_data(as_text=True)


@pytest.mark.parametrize("role", ["staff", "manager", "admin", "viewer", "finance"])
@pytest.mark.parametrize("warehouse_type", ["storefront", "distribution_center", "rd"])
def test_help_and_top_navigation_for_every_role(nav_client_factory, role, warehouse_type):
    client, _ = nav_client_factory(warehouse_type, role)
    assert client.get("/help").status_code == 200
    body = client.get("/land").get_data(as_text=True)
    top_bar = body.split('class="ctx-bar"', 1)[1].split("</div>", 1)[0]
    assert 'href="/help"' in top_bar


def test_help_without_selected_warehouse_keeps_feedback_guard(staff_client):
    client, _ = staff_client
    with client.session_transaction() as session:
        session.pop("warehouse_id")
    assert client.get("/help").status_code == 200
    response = client.post("/feedback/api", data={"title": "test", "body": "test"})
    assert response.status_code == 409
    assert response.get_json()["reason"] == "no_warehouse"


def test_help_clears_deleted_user_session_without_requiring_login(logged_client):
    client, _ = logged_client
    with client.session_transaction() as session:
        session["user_id"] = 99999
    assert client.get("/help").status_code == 200
    with client.session_transaction() as session:
        assert "user_id" not in session
    assert urlsplit(client.get("/inventory").location).path == "/login"


def test_logged_in_help_reuses_feedback_modal_and_fallback(staff_client):
    client, _ = staff_client
    body = client.get("/help").get_data(as_text=True)
    href, section = _feedback_link(body)
    assert 'data-feedback-open' in section
    assert body.count('id="feedback-modal"') == 1
    assert body.count('id="feedback-modal-form"') == 1
    assert 'action="/feedback"' in body
    assert 'data-endpoint="/feedback/api"' in body
    assert re.search(r'name="csrf_token" value="[^"]+"', body)
    assert href == "/feedback/new?page=/help"
    fallback = client.get(href)
    assert fallback.status_code == 200
    assert 'action="/feedback"' in fallback.get_data(as_text=True)


def test_anonymous_feedback_login_and_picker_preserve_existing_destination(staff_client):
    import config
    from db import get_master_db

    client, _ = staff_client
    with client.application.app_context():
        master = get_master_db()
        master.execute("UPDATE users SET password_hash=? WHERE id=2",
                       (generate_password_hash("help-test-password"),))
        master.commit()
    _anonymous(client)
    href, section = _feedback_link(client.get("/help").get_data(as_text=True))
    assert 'data-feedback-open' not in section
    login = client.get(href)
    assert login.status_code == 302
    assert urlsplit(login.location).path == "/login"
    assert parse_qs(urlsplit(login.location).query)["next"] == [href]
    response = client.post(login.location, data={
        "username": "staff1", "password": "help-test-password",
    }, follow_redirects=True)
    assert response.request.path == "/select-warehouse"
    body = response.get_data(as_text=True)
    action = html.unescape(re.search(r'<form[^>]*action="([^"]+)"', body)[1])
    selected = client.post(action, follow_redirects=True)
    assert selected.status_code == 200
    assert selected.request.path == "/feedback/new"
    assert selected.request.args["page"] == "/help"
    # Login/selection opened the existing form; no feedback has been submitted.
    with sqlite3.connect(config.MASTER_DB) as db:
        assert db.execute("SELECT COUNT(*) FROM feedback_tickets").fetchone()[0] == 0


@pytest.mark.parametrize("path", ["/inventory", "/items", "/feedback/new", "/users"])
def test_public_help_does_not_open_protected_pages(logged_client, path):
    client, _ = logged_client
    _anonymous(client)
    assert client.get("/help").status_code == 200
    response = client.get(path)
    assert response.status_code == 302
    assert urlsplit(response.location).path == "/login"


def test_anonymous_feedback_post_still_rejected(staff_client):
    import config

    client, _ = staff_client
    client.application.config["WTF_CSRF_FORCE_ENABLED"] = True
    body = _anonymous(client).get("/help").get_data(as_text=True)
    assert 'id="feedback-modal"' not in body
    assert client.post("/feedback/api", data={"title": "test", "body": "test"}).status_code == 400
    client.application.config["WTF_CSRF_FORCE_ENABLED"] = False
    response = client.post("/feedback/api", data={"title": "test", "body": "test"},
                           headers={"X-Requested-With": "fetch", "Accept": "application/json"})
    assert response.status_code == 401
    assert "ticket_no" not in response.get_json()
    with sqlite3.connect(config.MASTER_DB) as db:
        assert db.execute("SELECT COUNT(*) FROM feedback_tickets").fetchone()[0] == 0
