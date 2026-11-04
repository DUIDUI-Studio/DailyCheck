"""Actual loopback HTTP with CSRF and separately signed store/DC sessions."""

import http.client
import json
import sqlite3
import threading
import urllib.parse
from html.parser import HTMLParser
from http.cookies import SimpleCookie

import pytest
from werkzeug.serving import make_server

from blueprints import order_changes_pure as ocp
from blueprints import store_ordering_pure as sop
from tests import test_order_changes_pure as domain
from tests.test_order_changes_pure import (
    commercial,
    new_order,
    rows,
)

change_env = domain.change_env


class Inputs(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.values = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("name"):
            self.values[attrs["name"]] = attrs.get("value", "")


class SessionHTTP:
    def __init__(self, server, app, user, warehouse, output):
        self.server, self.output = server, output
        self.cookie = app.session_interface.get_signing_serializer(app).dumps(
            {"user_id": user, "warehouse_id": warehouse}
        )
        self.csrf = None
        self.count = 0
        self.actor = f"{user}-{warehouse}"

    def request(self, path, data=None, ajax=False, csrf=True):
        submitted = None if data is None else dict(data)
        if submitted is not None and csrf and self.csrf:
            submitted["csrf_token"] = self.csrf
        headers = {"Cookie": "session=" + self.cookie}
        if ajax:
            headers["X-Requested-With"] = "fetch"
        if submitted is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=15)
        connection.request(
            "GET" if data is None else "POST",
            path,
            body=urllib.parse.urlencode(submitted) if submitted is not None else None,
            headers=headers,
        )
        response = connection.getresponse()
        status, response_headers, body = (
            response.status,
            dict(response.getheaders()),
            response.read().decode(),
        )
        for name, value in response.getheaders():
            if name.lower() == "set-cookie":
                cookie = SimpleCookie(value)
                if "session" in cookie:
                    self.cookie = cookie["session"].value
        connection.close()
        inputs = Inputs(body).values
        if inputs.get("csrf_token"):
            self.csrf = inputs["csrf_token"]
        self.count += 1
        self.output.mkdir(exist_ok=True)
        (self.output / f"{self.actor}-{self.count:03d}.json").write_text(
            json.dumps(
                {
                    "path": path,
                    "method": "GET" if data is None else "POST",
                    "data": submitted,
                    "status": status,
                    "headers": response_headers,
                    "body": body,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        with sqlite3.connect(
            (self.output.parent / "master.db").as_uri() + "?mode=ro", uri=True
        ) as db:
            (self.output / f"{self.actor}-{self.count:03d}.sql").write_text(
                "\n".join(db.iterdump())
            )
        return status, body, response_headers


@pytest.fixture
def loopback(change_env):
    e = change_env
    app = e["app"]
    app.config["WTF_CSRF_FORCE_ENABLED"] = True
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    e["http_sessions"] = {}

    def session(user=4, warehouse=1):
        identity = user, warehouse
        if identity not in e["http_sessions"]:
            e["http_sessions"][identity] = SessionHTTP(
                server, app, user, warehouse, e["root"] / "http-raw"
            )
        return e["http_sessions"][identity]

    e["http"] = session
    e["server"] = server
    try:
        yield e
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        assert not thread.is_alive()


def http_publish(e, order, operation="cancel", **extra):
    dc = e["http"]()
    base = f"/store-ordering/orders/{order['id']}"
    status, html, headers = dc.request(base + "/changes/new")
    assert status == 200 and headers["Cache-Control"] == "no-store"
    values = Inputs(html).values
    data = {
        "base_change_revision": values["base_change_revision"],
        "request_key": values["request_key"],
        "reason": "HTTP缺货整份方案",
        "operation_" + str(order["order_items"][0]["id"]): operation,
        **extra,
    }
    status, preview, _ = dc.request(base + "/changes", {**data, "action": "preview"})
    assert status == 200, preview
    assert not rows(e["master"], "store_order_change_proposals")
    digest = Inputs(preview).values["seen_digest"]
    status, body, headers = dc.request(
        base + "/changes", {**data, "action": "publish", "seen_digest": digest}
    )
    assert status == 302, body
    return headers["Location"], data, digest


def test_real_http_preview_publish_notify_whole_accept_prg_and_csrf(loopback):
    e, m = loopback, loopback["master"]
    order = new_order(e)
    before = commercial(m, order["id"])
    url, data, digest = http_publish(e, order)
    assert commercial(m, order["id"]) == before
    proposed = rows(m, "notifications")
    assert any(row[2] == "store_order_change_proposed" and row[1] == 1 for row in proposed)
    store = e["http"](1, 3)
    status, html, headers = store.request(url)
    assert status == 200 and headers["Cache-Control"] == "no-store"
    assert "同意整份方案" in html and "不同意并留言" in html
    fields = Inputs(html).values
    decision = {"request_key": "http-accept", "seen_digest": fields["seen_digest"], "agree": "1"}
    assert store.request(url + "/accept", decision, csrf=False)[0] == 400
    assert store.request(url + "/accept", {**decision, "agree": ""})[0] == 409
    assert store.request(url + "/accept", {**decision, "subset": "1"})[0] == 409
    assert store.request(url + "/accept", decision)[0] == 302
    final = sop.get_order_detail(m, order["id"])
    assert final["order_items"][0]["quantity"] == 0
    count = len(rows(m, "notifications"))
    assert store.request(url + "/accept", decision, ajax=True)[0] == 200
    assert len(rows(m, "notifications")) == count
    assert (
        store.request(
            url + "/reject",
            {"request_key": "opposite", "seen_digest": fields["seen_digest"], "note": "改变主意"},
            ajax=True,
        )[0]
        == 409
    )
    # A completed create replay is independent of its now changed business baseline.
    response = e["http"]().request(
        f"/store-ordering/orders/{order['id']}/changes",
        {**data, "action": "publish", "seen_digest": digest},
        ajax=True,
    )
    assert response[0] == 200 and json.loads(response[1])["ok"]
    (e["root"] / "final-master.sql").write_text("\n".join(m.iterdump()))


def test_get_is_zero_write_wrong_pair_404_revoked_post_403(loopback):
    e, m = loopback, loopback["master"]
    order = new_order(e)
    url, _, _ = http_publish(e, order)
    store = e["http"](1, 3)
    status, html, _ = store.request(url)
    assert status == 200
    domain_tables = (
        "store_orders",
        "store_order_items",
        "store_order_change_proposals",
        "store_order_change_lines",
        "store_order_change_commands",
        "store_order_change_events",
        "notifications",
    )
    before = {table: rows(m, table) for table in domain_tables}
    assert store.request(url)[0] == 200
    assert {table: rows(m, table) for table in domain_tables} == before
    assert (
        store.request(url.replace("/orders/" + str(order["id"]) + "/", "/orders/99999/"))[0] == 404
    )
    m.execute("UPDATE warehouse_users SET role='viewer' WHERE user_id=1 AND warehouse_id=3")
    m.commit()
    response = store.request(
        url + "/accept",
        {"request_key": "revoked", "seen_digest": Inputs(html).values["seen_digest"], "agree": "1"},
    )
    assert response[0] == 403
    assert ocp.proposal_detail(m, order["id"], 1)["status"] == "pending"


def test_feature_flag_disables_new_commands_preserves_read_withdraw(loopback, monkeypatch):
    import config

    e = loopback
    order = new_order(e)
    url, data, digest = http_publish(e, order)
    store = e["http"](1, 3)
    _, enabled_html, _ = store.request(url)
    stored_digest = Inputs(enabled_html).values["seen_digest"]
    monkeypatch.setattr(config, "ORDER_CHANGES_ENABLED", False)
    dc = e["http"]()
    assert dc.request(url)[0] == 200
    assert dc.request(f"/store-ordering/orders/{order['id']}/changes/new")[0] == 409
    assert (
        dc.request(
            f"/store-ordering/orders/{order['id']}/changes",
            {**data, "action": "publish", "seen_digest": digest},
            ajax=True,
        )[0]
        == 409
    )
    store = e["http"](1, 3)
    _, html, _ = store.request(url)
    assert (
        store.request(
            url + "/accept",
            {"request_key": "off", "seen_digest": stored_digest, "agree": "1"},
            ajax=True,
        )[0]
        == 409
    )
    assert dc.request(url + "/withdraw", {"request_key": "flag-off-withdraw"})[0] == 302
    assert not ocp.blocked_lines(e["master"], order["id"])


def test_http_reject_requires_note_and_escapes_reason(loopback):
    e = loopback
    order = new_order(e)
    url, _, _ = http_publish(e, order)
    store = e["http"](5, 3)
    _, html, _ = store.request(url)
    fields = Inputs(html).values
    assert (
        store.request(
            url + "/reject",
            {"request_key": "blank", "seen_digest": fields["seen_digest"], "note": ""},
            ajax=True,
        )[0]
        == 409
    )
    assert (
        store.request(
            url + "/reject",
            {
                "request_key": "reject",
                "seen_digest": fields["seen_digest"],
                "note": "<script>alert(1)</script>",
            },
        )[0]
        == 302
    )
    status, html, _ = store.request(url)
    assert status == 200 and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert(1)</script>" not in html
