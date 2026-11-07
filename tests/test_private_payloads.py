"""Issue162: synthetic context through native Flask, SQLite and loopback HTTP."""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from blueprints import feedback_dispatch as dispatch
from blueprints import feedback_pure as fp
from scripts.hosting import AccessRole, HostingClient, HostingConfig, HostingError, Provider

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "blue_green"))
import deploy_report  # noqa: E402

CODE = "DUIDUI-Studio/DailyCheck"
OPS = "DUIDUI-Studio/DailyCheck-ops"
API = "https://api.github.com"
WEB = "https://github.com"
PRIVATE = "SYNTHETIC_PRIVATE_BUSINESS_162"
SHA = "a" * 40


@pytest.fixture
def master_conn(tmp_path, monkeypatch):
    import config
    import db as module

    path, warehouses = tmp_path / "master.db", tmp_path / "warehouses"
    warehouses.mkdir()
    for target in (config, module):
        monkeypatch.setattr(target, "MASTER_DB", path)
        monkeypatch.setattr(target, "WAREHOUSE_DB_DIR", warehouses)
    module.init_master_db()
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("INSERT INTO users (id,username,password_hash,is_admin,created_at) VALUES (1,'synthetic-admin','x',1,'2026-11-07')")
    connection.commit()
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def http_api(tmp_path, monkeypatch):
    """Real curl/HTTP; only the executable shim can translate a pinned URL to loopback."""
    state = {
        "requests": [], "issues": [], "comments": [], "labels": [], "metadata": {},
        "drop_issue": False, "drop_comment": False, "status": {}, "raw": {},
        "issue_hook": None, "metadata_hook": None,
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def do_PATCH(self):
            self.respond()

        def respond(self):
            parsed = urlsplit(self.path)
            path, query = parsed.path, parse_qs(parsed.query)
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"]))) if self.headers.get("Content-Length") else None
            state["requests"].append({"method": self.command, "path": path,
                                      "query": query, "payload": payload,
                                      "auth": self.headers.get("Authorization")})
            status, data = 200, None
            if path in {f"/repos/{OPS}", f"/repos/{CODE}"}:
                repo = path.removeprefix("/repos/")
                private = repo == OPS
                data = {"id": 1449125557 if private else 1448986421, "full_name": repo,
                        "private": private, "visibility": "private" if private else "public",
                        "has_issues": True, "archived": False, "disabled": False,
                        "url": API + path, "html_url": WEB + "/" + repo}
                data.update(state["metadata"])
                if state["metadata_hook"]:
                    state["metadata_hook"](data)
            elif path in {f"/repos/{OPS}/labels", "/repos/eric/dailycheck/labels"}:
                if self.command == "POST":
                    status, data = 201, {"id": 12, **payload}
                    state["labels"].append(data)
                else:
                    data = state["labels"]
            elif path in {f"/repos/{OPS}/issues", "/repos/eric/dailycheck/issues"}:
                if self.command == "POST":
                    number = 7
                    status, data = 201, {
                        "id": 107, "number": number, "state": "closed", **payload,
                        "repository_url": API + f"/repos/{OPS}",
                        "url": API + path + f"/{number}",
                        "html_url": WEB + f"/{OPS}/issues/{number}",
                    }
                    state["issues"].append(data)
                    if state["issue_hook"]:
                        state["issue_hook"](data)
                    if state["drop_issue"]:
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                        return
                else:
                    data = state["issues"]
            elif path == f"/repos/{CODE}/pulls/17":
                data = {"number": 17, "html_url": WEB + f"/{CODE}/pull/17",
                        "head": {"sha": SHA}, "base": {"repo": {"id": 1448986421}},
                        "body": state.get("pr_body", "")}
            elif path == f"/repos/{CODE}/issues/17/comments":
                if self.command == "POST":
                    status, data = 201, {"id": len(state["comments"]) + 201,
                                        "issue_url": API + f"/repos/{CODE}/issues/17", **payload}
                    state["comments"].append(data)
                    if state["drop_comment"]:
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                        return
                else:
                    data = state["comments"]
            else:
                status, data = 404, {}
            if isinstance(data, list):
                page, size = int(query.get("page", [1])[0]), int(query.get("per_page", [100])[0])
                data = data[(page - 1) * size:page * size]
            status = state["status"].get(path, status)
            raw = state["raw"].get(path, json.dumps(data))
            self.send_response(status)
            if 300 <= status < 400:
                self.send_header("Location", state["loopback"] + f"/repos/{CODE}")
            self.send_header("Content-Length", str(len(raw.encode())))
            self.end_headers()
            self.wfile.write(raw.encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state["loopback"] = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    shim = tmp_path / "loopback-curl"
    shim.write_text(f"""#!{sys.executable}
import os, sys
from urllib.parse import urlsplit
args = sys.argv[1:]
url = urlsplit(args[-1])
assert url.scheme == 'https' and url.netloc == 'api.github.com'
args[-1] = 'http://127.0.0.1:{server.server_port}' + url.path + ('?' + url.query if url.query else '')
os.execv('/usr/bin/curl', ['/usr/bin/curl', *args])
""")
    shim.chmod(0o755)
    for key in list(os.environ):
        if key.startswith("DAILYCHECK_GITHUB_") or key in {"GH_TOKEN", "GITHUB_TOKEN", "GITEA_TOKEN"}:
            monkeypatch.delenv(key)
    monkeypatch.setenv("DAILYCHECK_HOSTING_PROVIDER", "github")
    monkeypatch.setenv("DAILYCHECK_CURL", str(shim))
    monkeypatch.setenv("DAILYCHECK_GITHUB_OPERATIONS_REPO", OPS)
    monkeypatch.setenv("DAILYCHECK_GITHUB_OPERATIONS_REPO_ID", "1449125557")
    monkeypatch.setenv("DAILYCHECK_GITHUB_OPERATIONS_EXPECT_PRIVATE", "true")
    monkeypatch.setenv("DAILYCHECK_GITHUB_FEEDBACK_TOKEN", "synthetic-feedback-token")
    monkeypatch.setenv("DAILYCHECK_GITHUB_REPORT_TOKEN", "synthetic-report-token")
    monkeypatch.setenv("DAILYCHECK_GITHUB_READ_TOKEN", "synthetic-read-token")
    monkeypatch.setenv("DAILYCHECK_GITHUB_SHADOW_DIR", str(tmp_path / "shadow"))
    monkeypatch.setenv("DAILYCHECK_DEPLOY_EVENT_LOG", str(tmp_path / "shadow/events.jsonl"))
    monkeypatch.setenv("DAILYCHECK_PROD_AGENT_INBOX", str(tmp_path / "shadow/inbox.jsonl"))
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def ticket(db):
    value = fp.create_ticket(db, 1, "Synthetic feedback", PRIVATE, warehouse_code="synthetic-wh",
                             page_path="/items?synthetic=1")
    fp.mark_analyzing(db, value["id"], "/items")
    fp.save_analysis(db, value["id"], PRIVATE + "_ANALYSIS", "synthetic-model",
                     "Synthetic private draft", PRIVATE * 3)
    return fp.get_ticket(db, value["id"])


def receipt(db):
    return dict(db.execute("SELECT * FROM feedback_delivery_receipts").fetchone())


def writes(state):
    return [r for r in state["requests"] if r["method"] != "GET"]


def test_feedback_requires_explicit_private_destination():
    with pytest.raises(HostingError):
        HostingConfig.from_environment(AccessRole.FEEDBACK, {
            "DAILYCHECK_HOSTING_PROVIDER": "github",
            "DAILYCHECK_GITHUB_FEEDBACK_TOKEN": "synthetic-feedback-token",
        })


@pytest.mark.parametrize("changes", [
    {"DAILYCHECK_GITHUB_OPERATIONS_REPO": CODE},
    {"DAILYCHECK_GITHUB_OPERATIONS_REPO": "invalid/repo?redirect=public"},
    {"DAILYCHECK_GITHUB_OPERATIONS_REPO_ID": "1448986421"},
    {"DAILYCHECK_GITHUB_OPERATIONS_REPO_ID": ""},
    {"DAILYCHECK_GITHUB_OPERATIONS_EXPECT_PRIVATE": "false"},
    {"DAILYCHECK_GITHUB_API": "http://127.0.0.1:1"},
])
def test_private_config_identity_cannot_be_retargeted(changes):
    env = {"DAILYCHECK_HOSTING_PROVIDER": "github", "DAILYCHECK_GITHUB_OPERATIONS_REPO": OPS,
           "DAILYCHECK_GITHUB_OPERATIONS_REPO_ID": "1449125557",
           "DAILYCHECK_GITHUB_OPERATIONS_EXPECT_PRIVATE": "true", **changes}
    with pytest.raises(HostingError):
        HostingConfig.from_environment(AccessRole.FEEDBACK, env)


def test_github_legacy_renderer_cannot_publish_private_context(monkeypatch):
    monkeypatch.setenv("DAILYCHECK_HOSTING_PROVIDER", "github")
    with pytest.raises(HostingError):
        deploy_report.render(17, "a" * 40,
            {"rc": 21, "kind": "code", "stage": "verify", "scope": "in-scope",
             "reason": "SYNTHETIC_PRIVATE_CUSTOMER"},
            {"verdict": "aligned", "reason": "SYNTHETIC_PRIVATE_ANALYSIS"},
            "SYNTHETIC_PRIVATE_LOG")


@pytest.mark.parametrize("metadata", [
    {"id": 1448986421}, {"id": True}, {"full_name": CODE},
    {"private": False}, {"visibility": "public"}, {"visibility": "internal"},
    {"visibility": None}, {"has_issues": False}, {"archived": True}, {"disabled": True},
    {"url": "https://evil.invalid/repo"}, {"html_url": "https://evil.invalid/repo"},
])
def test_private_metadata_failure_keeps_draft_no_mutation(http_api, master_conn, metadata):
    value = ticket(master_conn)
    http_api["metadata"] = metadata
    assert dispatch.deliver_private(master_conn, value["id"], 1) is None
    assert not writes(http_api)
    assert fp.get_ticket(master_conn, value["id"]) == value


@pytest.mark.parametrize("status", [301, 302, 401, 403, 404, 410, 500])
def test_private_http_failure_no_public_fallback(http_api, master_conn, status):
    value = ticket(master_conn)
    http_api["status"][f"/repos/{OPS}"] = status
    assert dispatch.deliver_private(master_conn, value["id"], 1) is None
    assert not writes(http_api)
    assert fp.get_ticket(master_conn, value["id"])["draft_body"] == PRIVATE * 3
    assert all(OPS in r["path"] for r in http_api["requests"])


@pytest.mark.parametrize("raw", ["null", "[]", "{}", "not json", '{"id":1,"id":2}'])
def test_malformed_private_metadata_no_mutation(http_api, master_conn, raw):
    value = ticket(master_conn)
    http_api["raw"][f"/repos/{OPS}"] = raw
    assert dispatch.deliver_private(master_conn, value["id"], 1) is None
    assert not writes(http_api)


def test_metadata_is_checked_before_each_private_mutation(http_api, master_conn):
    value = ticket(master_conn)

    def change_visibility(data):
        if any(r["path"].endswith("/labels") and r["method"] == "POST" for r in http_api["requests"]):
            data["private"] = False

    http_api["metadata_hook"] = change_visibility
    assert dispatch.deliver_private(master_conn, value["id"], 1) is None
    assert [r["path"] for r in writes(http_api)] == [f"/repos/{OPS}/labels"]
    assert not http_api["issues"]
    assert receipt(master_conn)["state"] == "uncertain"


def test_operations_role_cannot_target_public_code_or_mutate_prs(http_api):
    client = HostingClient(HostingConfig.from_environment(AccessRole.FEEDBACK))
    for path in (f"/repos/{CODE}/issues", f"/repos/{CODE}/issues/17/comments",
                 f"/repos/{OPS}/pulls", f"/repos/{OPS}/issues/7/comments"):
        with pytest.raises(HostingError):
            client.request("POST", path, {"title": PRIVATE, "body": PRIVATE})
    with pytest.raises(HostingError):
        HostingConfig(Provider.GITHUB, API, CODE, AccessRole.FEEDBACK, "fake")
    assert not http_api["requests"]


def test_no_ambient_credentials_or_boolean_private_config(http_api, monkeypatch):
    for role in (AccessRole.READ, AccessRole.FEEDBACK, AccessRole.OPS_READ, AccessRole.REPORT):
        env = {"DAILYCHECK_HOSTING_PROVIDER": "github", "GH_TOKEN": "synthetic-owner",
               "GITHUB_TOKEN": "synthetic-owner", "GITEA_TOKEN": "synthetic-gitea",
               "DAILYCHECK_GITHUB_OPERATIONS_REPO": OPS,
               "DAILYCHECK_GITHUB_OPERATIONS_REPO_ID": "1449125557",
               "DAILYCHECK_GITHUB_OPERATIONS_EXPECT_PRIVATE": "true"}
        assert not HostingConfig.from_environment(role, env).token
    monkeypatch.setenv("DAILYCHECK_GITHUB_OPERATIONS_EXPECT_PRIVATE", "1")
    assert not dispatch.dispatch_enabled()
    assert dispatch.create_issue(PRIVATE, PRIVATE) is None
    assert not http_api["requests"]


def test_private_issue_capture_and_atomic_receipt(http_api, master_conn):
    value = ticket(master_conn)
    cid = fp.add_comment(master_conn, value["id"], 1, PRIVATE + "_ADMIN", is_admin=True)
    issue = dispatch.deliver_private(master_conn, value["id"], 1, cid)
    assert issue == {"id": 107, "number": 7, "url": WEB + f"/{OPS}/issues/7"}
    row = receipt(master_conn)
    assert (row["provider"], row["host"], row["repository_id"], row["kind"], row["state"]) == (
        "github", "github.com", 1449125557, "issue", "linked")
    posted = [r for r in writes(http_api) if r["path"].endswith("/issues")]
    assert len(posted) == 1
    assert posted[0]["payload"] == json.loads(row["payload_json"])
    assert posted[0]["payload"]["labels"] == ["user-feedback"]
    assert PRIVATE * 3 in posted[0]["payload"]["body"]
    stored = fp.get_ticket(master_conn, value["id"])
    assert stored["body"] == value["body"] and stored["analysis_text"] == value["analysis_text"]
    assert stored["draft_body"] == value["draft_body"] and stored["issue_url"] == issue["url"]
    assert master_conn.execute("SELECT dispatched_at FROM feedback_comments WHERE id=?", (cid,)).fetchone()[0]
    assert dispatch.deliver_private(master_conn, value["id"], 1) is None
    assert len([r for r in writes(http_api) if r["path"].endswith("/issues")]) == 1


def test_lost_creation_response_restart_closed_paginated_recovery(http_api, master_conn, monkeypatch):
    value = ticket(master_conn)
    http_api["drop_issue"] = True
    assert dispatch.deliver_private(master_conn, value["id"], 1) is None
    before = receipt(master_conn)
    assert before["state"] == "uncertain" and not fp.get_ticket(master_conn, value["id"])["issue_number"]
    http_api["drop_issue"] = False
    http_api["issues"].insert(0, {"id": 106, "body": "other issue"})
    monkeypatch.setattr(HostingClient, "PER_PAGE", 1)
    db_path = master_conn.execute("PRAGMA database_list").fetchone()[2]
    with sqlite3.connect(db_path) as restarted:
        restarted.row_factory = sqlite3.Row
        assert dispatch.deliver_private(restarted, value["id"], 1)["number"] == 7
    assert receipt(master_conn)["request_id"] == before["request_id"]
    assert len([r for r in writes(http_api) if r["path"].endswith("/issues")]) == 1
    pages = [r for r in http_api["requests"] if r["method"] == "GET" and r["path"].endswith("/issues")]
    assert [r["query"]["page"] for r in pages] == [["1"], ["2"], ["3"]]
    assert all(r["query"]["state"] == ["all"] for r in pages)


@pytest.mark.parametrize("change", ["zero", "multiple", "foreign", "kind", "body", "title"])
def test_ambiguous_creation_never_reposts(http_api, master_conn, change):
    value = ticket(master_conn)
    http_api["drop_issue"] = True
    assert dispatch.deliver_private(master_conn, value["id"], 1) is None
    http_api["drop_issue"] = False
    original = copy.deepcopy(http_api["issues"][0])
    if change == "zero":
        http_api["issues"].clear()
    elif change == "multiple":
        http_api["issues"].append({**original, "id": 108})
    elif change == "foreign":
        http_api["issues"][0]["repository_url"] = API + f"/repos/{CODE}"
    elif change == "kind":
        http_api["issues"][0]["pull_request"] = {}
    else:
        http_api["issues"][0][change] += " changed"
    for _ in range(2):
        assert dispatch.deliver_private(master_conn, value["id"], 1) is None
    assert receipt(master_conn)["request_id"] in original["body"]
    assert receipt(master_conn)["state"] == "uncertain"
    assert not fp.get_ticket(master_conn, value["id"])["issue_number"]
    assert len([r for r in writes(http_api) if r["path"].endswith("/issues")]) == 1


def test_changed_draft_preserved_stale_retry_cannot_overwrite(http_api, master_conn):
    value = ticket(master_conn)
    http_api["drop_issue"] = True
    dispatch.deliver_private(master_conn, value["id"], 1)
    old = receipt(master_conn)
    version = fp.draft_version(value["draft_title"], value["draft_body"])
    assert fp.save_private_draft(master_conn, value, "New private title", PRIVATE + " newer facts", version)
    refreshed = fp.get_ticket(master_conn, value["id"])
    assert not fp.save_private_draft(master_conn, refreshed, value["draft_title"], value["draft_body"], version)
    assert dispatch.deliver_private(master_conn, value["id"], 1) is None
    assert receipt(master_conn)["request_id"] == old["request_id"]
    assert receipt(master_conn)["state"] == "blocked"
    assert fp.get_ticket(master_conn, value["id"])["draft_body"].endswith("newer facts")


def test_expired_owner_cannot_finalize_or_create(http_api, master_conn):
    value = ticket(master_conn)
    first = fp.claim_private_delivery(master_conn, value["id"], 1)
    assert fp.begin_private_creation(master_conn, first)
    master_conn.execute("UPDATE feedback_delivery_receipts SET lease_until=0")
    master_conn.commit()
    path = master_conn.execute("PRAGMA database_list").fetchone()[2]
    with sqlite3.connect(path) as other:
        other.row_factory = sqlite3.Row
        second = fp.claim_private_delivery(other, value["id"], 1)
        assert second.action == "reconcile" and second.generation > first.generation
        assert not fp.begin_private_creation(other, second)
    assert not fp.finalize_private_delivery(master_conn, first,
                                          {"id": 107, "number": 7, "url": WEB + f"/{OPS}/issues/7"})
    assert not fp.begin_private_creation(master_conn, first)
    assert not fp.get_ticket(master_conn, value["id"])["issue_number"]
    assert not http_api["requests"]


def test_gitea_old_links_are_never_reinterpreted(http_api, master_conn):
    value = ticket(master_conn)
    url = "http://hub01:3000/eric/dailycheck/issues/7"
    fp.attach_issue(master_conn, value["id"], 7, url, 1)
    assert dispatch.deliver_private(master_conn, value["id"], 1) is None
    assert not writes(http_api)
    assert fp.get_ticket(master_conn, value["id"])["issue_url"] == url
    assert not master_conn.execute("SELECT * FROM feedback_delivery_receipts").fetchall()


def test_real_flask_concurrent_dispatch_single_post(http_api, logged_client):
    import config

    client, _ = logged_client
    client.post("/feedback", data={"title": "Synthetic feedback", "body": PRIVATE,
                                   "page_path": "/items", "category": "bug"})
    with sqlite3.connect(config.MASTER_DB) as conn:
        tid = conn.execute("SELECT id FROM feedback_tickets").fetchone()[0]
    client.post(f"/admin/feedback/{tid}/comment", data={"comment": PRIVATE + "_ADMIN"})
    assert client.post(f"/admin/feedback/{tid}/analyze", data={"mode": "template"}).status_code == 302
    with sqlite3.connect(config.MASTER_DB) as conn:
        conn.row_factory = sqlite3.Row
        value = fp.get_ticket(conn, tid)
    form = {"draft_title": value["draft_title"], "draft_body": value["draft_body"],
            "draft_version": fp.draft_version(value["draft_title"], value["draft_body"]),
            "approved": "true", "repo": CODE, "target": f"/repos/{CODE}/issues"}
    started, release = threading.Event(), threading.Event()

    def delay(_):
        started.set()
        assert release.wait(timeout=10)

    http_api["issue_hook"] = delay

    def post_review(comment):
        independent = client.application.test_client()
        with independent.session_transaction() as session:
            session["user_id"], session["warehouse_id"] = 1, 1
        return independent.post(f"/admin/feedback/{tid}/dispatch", data={**form, "comment": comment}).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(post_review, PRIVATE + " first review")
        try:
            assert started.wait(timeout=10)
            second = pool.submit(post_review, PRIVATE + " second review")
            assert second.result(timeout=10) == 302
        finally:
            release.set()
        assert first.result(timeout=10) == 302
    assert len(http_api["issues"]) == 1
    assert all(OPS in r["path"] for r in writes(http_api))
    body = http_api["issues"][0]["body"]
    assert PRIVATE in body and PRIVATE + "_ADMIN" in body and "admin" in body
    with sqlite3.connect(config.MASTER_DB) as conn:
        assert conn.execute("SELECT status,issue_url FROM feedback_tickets").fetchone() == (
            "dispatched", WEB + f"/{OPS}/issues/7")
        assert conn.execute("SELECT count(*) FROM feedback_comments").fetchone()[0] == 3
        assert conn.execute("SELECT count(*) FROM feedback_comments WHERE dispatched_at IS NOT NULL").fetchone()[0] == 1
    assert client.get(f"/admin/feedback/{tid}").status_code == 200


def test_old_schema_additive_upgrade_and_missing_receipt_fail_closed(http_api, master_conn):
    import db as module

    value = ticket(master_conn)
    fp.add_comment(master_conn, value["id"], 1, PRIVATE + " comment", is_admin=True)
    before = [tuple(r) for r in master_conn.execute("SELECT * FROM feedback_tickets")]
    comments = [tuple(r) for r in master_conn.execute("SELECT * FROM feedback_comments")]
    master_conn.execute("DROP TABLE feedback_delivery_receipts")
    master_conn.commit()
    assert dispatch.deliver_private(master_conn, value["id"], 1) is None
    assert not writes(http_api)
    module.init_master_db()
    module.init_master_db()
    assert [tuple(r) for r in master_conn.execute("SELECT * FROM feedback_tickets")] == before
    assert [tuple(r) for r in master_conn.execute("SELECT * FROM feedback_comments")] == comments
    assert not master_conn.execute("SELECT * FROM feedback_delivery_receipts").fetchall()
    assert dispatch.deliver_private(master_conn, value["id"], 1)["number"] == 7


@pytest.mark.parametrize("selected", [None, "gitea"])
def test_native_gitea_http_shape_ignores_conflicting_operations_config(http_api, monkeypatch, selected):
    if selected is None:
        monkeypatch.delenv("DAILYCHECK_HOSTING_PROVIDER")
    else:
        monkeypatch.setenv("DAILYCHECK_HOSTING_PROVIDER", selected)
    monkeypatch.setenv("DAILYCHECK_GITHUB_OPERATIONS_REPO", CODE)
    monkeypatch.setenv("DAILYCHECK_GITHUB_OPERATIONS_REPO_ID", "wrong")
    monkeypatch.setenv("DAILYCHECK_GITHUB_OPERATIONS_EXPECT_PRIVATE", "false")
    monkeypatch.setattr(dispatch.config, "FEEDBACK_GITEA_API", http_api["loopback"])
    monkeypatch.setattr(dispatch.config, "FEEDBACK_GITEA_REPO", "eric/dailycheck")
    monkeypatch.setattr(dispatch.config, "FEEDBACK_GITEA_TOKEN", "synthetic-gitea")
    monkeypatch.setattr(dispatch.config, "FEEDBACK_GITEA_WEB_BASE", "http://hub01:3000")
    http_api["labels"] = [{"id": 12, "name": "user-feedback"}]
    assert dispatch.create_issue("Synthetic Gitea", PRIVATE) == {
        "number": 7, "url": "http://hub01:3000/eric/dailycheck/issues/7"}
    assert writes(http_api) == [{
        "method": "POST", "path": "/repos/eric/dailycheck/issues", "query": {},
        "payload": {"title": "Synthetic Gitea", "body": PRIVATE, "labels": [12]},
        "auth": "token synthetic-gitea"}]


def test_confirmed_old_body_does_not_overwrite_new_private_facts(http_api, master_conn):
    value = ticket(master_conn)
    path = master_conn.execute("PRAGMA database_list").fetchone()[2]

    def edit_while_sending(_):
        with sqlite3.connect(path) as independent:
            independent.row_factory = sqlite3.Row
            original = fp.get_ticket(independent, value["id"])
            assert fp.save_private_draft(independent, original, "New facts", PRIVATE + " NEW",
                                        fp.draft_version(original["draft_title"], original["draft_body"]))

    http_api["issue_hook"] = edit_while_sending
    assert dispatch.deliver_private(master_conn, value["id"], 1) is None
    assert receipt(master_conn)["state"] == "blocked"
    assert receipt(master_conn)["remote_number"] == 7
    assert fp.get_ticket(master_conn, value["id"])["draft_body"] == PRIVATE + " NEW"
    assert not fp.get_ticket(master_conn, value["id"])["issue_number"]


def test_operations_read_uses_separate_token_file_and_cannot_mutate(http_api, tmp_path, monkeypatch):
    file = tmp_path / "synthetic-ops-read"
    file.write_text("synthetic-private-reader")
    monkeypatch.setenv("DAILYCHECK_GITHUB_OPS_READ_TOKEN_FILE", str(file))
    client = HostingClient(HostingConfig.from_environment(AccessRole.OPS_READ))
    client.verify_repository()
    assert client.pages(f"/repos/{OPS}/issues", params={"state": "all"}) == []
    assert all(r["auth"] == "Bearer synthetic-private-reader" for r in http_api["requests"])
    with pytest.raises(HostingError):
        client.request("POST", f"/repos/{OPS}/issues", {"body": PRIVATE})
    assert not writes(http_api)


def test_private_timeout_retains_original_and_does_not_retarget(http_api, master_conn, tmp_path, monkeypatch):
    value = ticket(master_conn)
    shim = tmp_path / "timeout-curl"
    shim.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(28)\n")
    shim.chmod(0o755)
    monkeypatch.setenv("DAILYCHECK_CURL", str(shim))
    assert dispatch.deliver_private(master_conn, value["id"], 1) is None
    assert not http_api["requests"]
    assert fp.get_ticket(master_conn, value["id"]) == value


def facts(**changes):
    return deploy_report.PublicDeployFacts(**{
        "pr": 17, "sha": SHA, "receipt": "7f8e7834-b92c-4b4a-8f8f-bdc07cd03f67",
        "outcome_at": "2026-11-07T08:00:00Z", "rc": 21, "stage": "verify", "kind": "code",
        "scope": "undetermined", "verdict": "aligned", "next": "autofix", **changes,
    })


def context(value, generation=1):
    body = deploy_report.render_public(value)
    digest = hashlib.sha256(json.dumps({"body": body}, ensure_ascii=False,
                                      sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return deploy_report.PublicationContext.parse({
        "provider": "github", "host": "github.com", "repository_id": 1448986421,
        "repository": CODE, "pr": value.pr, "sha": value.sha,
        "attempt_id": f"synthetic-private-attempt-{generation}", "generation": generation,
        "outcome_at": value.outcome_at, "public_receipt": value.receipt,
        "private_source": {"provider": "github", "host": "github.com", "repository_id": 1449125557,
                           "repository": OPS, "kind": "issue", "number": 7},
        "payload_sha256": digest,
    })


class SyntheticTrustedFence:
    """TEST ONLY trusted adapter with durable SQLite state; never imported by runtime."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS attempts (generation INTEGER PRIMARY KEY, context TEXT, state TEXT, comment_id INTEGER)")

    def seed(self, value):
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO attempts VALUES (?,?,'ready',0)",
                       (value.generation, json.dumps(dataclasses.asdict(value), sort_keys=True)))

    @contextmanager
    def claim(self, value, payload_sha256):
        with self.lock, sqlite3.connect(self.path) as db:
            expected = json.dumps(dataclasses.asdict(value), sort_keys=True)
            row = db.execute("SELECT context,state,comment_id FROM attempts WHERE generation=?",
                             (value.generation,)).fetchone()
            if row is None or row[0] != expected or value.payload_sha256 != payload_sha256:
                raise HostingError("synthetic_untrusted_receipt")

            class Claim:
                state, comment_id = row[1:]

                def check_current(self):
                    if db.execute("SELECT max(generation) FROM attempts").fetchone()[0] != value.generation:
                        raise HostingError("synthetic_stale_attempt")

                def intent(self):
                    self.check_current()
                    assert self.state == "ready"
                    db.execute("UPDATE attempts SET state='uncertain' WHERE generation=?", (value.generation,))
                    db.commit()
                    self.state = "uncertain"

                def bind(self, comment_id):
                    self.check_current()
                    db.execute("UPDATE attempts SET state='linked',comment_id=? WHERE generation=?",
                               (comment_id, value.generation))
                    db.commit()

            yield Claim()


def test_public_report_real_http_capture_recovery_and_late_attempt(http_api, tmp_path, monkeypatch):
    value = facts()
    ctx = context(value)
    path = tmp_path / "synthetic-fence.db"
    authority = SyntheticTrustedFence(path)
    authority.seed(ctx)
    http_api["drop_comment"] = True
    with pytest.raises(HostingError):
        deploy_report.publish_public(value, ctx, authority=authority)
    assert len(http_api["comments"]) == 1
    http_api["drop_comment"] = False
    http_api["comments"].insert(0, {"id": 200, "body": "unrelated"})
    monkeypatch.setattr(HostingClient, "PER_PAGE", 1)
    restarted = SyntheticTrustedFence(path)
    assert deploy_report.publish_public(value, ctx, authority=restarted) == 201
    assert deploy_report.publish_public(value, ctx, authority=restarted) == 201
    assert len(writes(http_api)) == 1
    newer = facts(receipt=str(uuid.uuid4()), rc=0, kind="success", stage="complete", next="none")
    newer_context = context(newer, 2)
    restarted.seed(newer_context)
    assert deploy_report.publish_public(newer, newer_context, authority=restarted) == 203
    with pytest.raises(HostingError, match="stale_attempt"):
        deploy_report.publish_public(value, ctx, authority=restarted)
    assert len(writes(http_api)) == 2
    assert all(r["method"] == "POST" and r["path"] == f"/repos/{CODE}/issues/17/comments" for r in writes(http_api))
    public = json.dumps(writes(http_api))
    assert PRIVATE not in public and OPS not in public and "synthetic-private-attempt" not in public
    assert deploy_report.MARKER in public and SHA in public


@pytest.mark.parametrize("change", ["zero", "multiple", "body", "foreign", "private_id"])
def test_public_report_uncertainty_cannot_repost_or_cross_sinks(http_api, tmp_path, change):
    value, path = facts(), tmp_path / "fence.db"
    ctx = context(value)
    authority = SyntheticTrustedFence(path)
    authority.seed(ctx)
    http_api["drop_comment"] = True
    with pytest.raises(HostingError):
        deploy_report.publish_public(value, ctx, authority=authority)
    http_api["drop_comment"] = False
    if change == "zero":
        http_api["comments"] = []
    elif change == "multiple":
        http_api["comments"].append({**http_api["comments"][0], "id": 202})
    elif change == "body":
        http_api["comments"][0]["body"] += PRIVATE
    elif change == "foreign":
        http_api["comments"][0]["issue_url"] = API + f"/repos/{OPS}/issues/17"
    else:
        with sqlite3.connect(path) as db:
            db.execute("UPDATE attempts SET comment_id=999")
    with pytest.raises(HostingError):
        deploy_report.publish_public(value, ctx, authority=authority)
    assert len(writes(http_api)) == 1


@pytest.mark.parametrize("field", ["pr", "sha", "receipt", "outcome_at", "rc", "stage", "kind", "scope", "verdict", "next"])
def test_private_values_cannot_enter_public_typed_fields(field):
    with pytest.raises(HostingError):
        facts(**{field: PRIVATE})


def test_context_json_never_grants_report_authority(http_api, tmp_path):
    value, ctx = facts(), context(facts())
    with pytest.raises(HostingError, match="not_wired"):
        deploy_report.publish_public(value, ctx)
    raw = dataclasses.asdict(ctx)
    raw["approved"] = raw["valid"] = True
    with pytest.raises(HostingError):
        deploy_report.PublicationContext.parse(raw)
    authority = SyntheticTrustedFence(tmp_path / "no-real-attempt.db")
    with pytest.raises(HostingError):
        deploy_report.publish_public(value, ctx, authority=authority)
    assert not http_api["requests"]


def test_cmd_write_keeps_private_evidence_and_projects_only_fixed_metadata(http_api, tmp_path, monkeypatch):
    value = facts()
    ctx = context(value)
    authority = SyntheticTrustedFence(tmp_path / "fence.db")
    authority.seed(ctx)
    canaries = [PRIVATE + suffix for suffix in (
        "_USER", "_WAREHOUSE", "_PAGE", "_ADMIN", "_ANALYSIS", "_PLAN", "_PATH",
        "_REQUIREMENT", "_EXPLANATION", "_ACTION", "_LOG", "_FETCH",
    )]
    tail = "\n".join(canaries) + "\npassword=synthetic-password Bearer synthetic-bearer"
    log = tmp_path / (canaries[6] + ".log")
    log.write_text(tail)
    tr = {"rc": value.rc, "stage": value.stage, "kind": value.kind, "scope": value.scope,
          "reason": " ".join(canaries[:7]) + " password=synthetic-password"}
    align = {"verdict": value.verdict, "reason": canaries[8], "action": canaries[9],
             "requirements": [{"id": "R-001", "text": canaries[7]}]}
    monkeypatch.setattr(deploy_report, "_assess", lambda *_: (tr, align, ""))
    context_file = tmp_path / "context.json"
    raw = dataclasses.asdict(ctx)
    raw["private_source"] = dict(zip(
        ("provider", "host", "repository_id", "repository", "kind", "number"), ctx.private_source, strict=True))
    context_file.write_text(json.dumps(raw))
    args = type("Args", (), {"pr": 17, "sha": SHA, "rc": 21, "stage": "verify", "repo": str(tmp_path),
                             "log": str(log), "base": "", "dry_run": False,
                             "publication_context": str(context_file)})()
    assert deploy_report.cmd_write(args, authority=authority) == 0
    captured = json.dumps([r["payload"] for r in writes(http_api)])
    assert writes(http_api)
    assert all(value not in captured for value in canaries)
    assert "synthetic-password" not in captured and "synthetic-bearer" not in captured
    private = "\n".join(p.read_text() for p in (tmp_path / "shadow/reports").glob("*.md"))
    assert all(value in private for value in canaries)
    assert "synthetic-password" not in private and "synthetic-bearer" not in private
    events = tmp_path / "shadow/events.jsonl"
    inbox = json.loads((tmp_path / "shadow/inbox.jsonl").read_text())
    assert canaries[0] in inbox["deploy_reason"] and canaries[6] in inbox["deploy_log"]
    assert "synthetic-password" not in inbox["deploy_reason"]
    from scripts.dailycheck_dashboard.ops_parse import parse_deploy_results

    assert parse_deploy_results(events) == [{"pr": 17, "sha": SHA, "rc": 21,
                                            "ts": json.loads(events.read_text())["ts"], "report_comment": 201}]
    assert deploy_report.MARKER in private
    public_body = http_api["comments"][0]["body"]
    assert deploy_report.MARKER in public_body and f"| Commit | `{SHA[:12]}` |" in public_body


def test_real_cli_without_trusted_adapter_is_blocked_and_retains_private_log(http_api, tmp_path):
    log = tmp_path / "private.log"
    log.write_text(PRIVATE + " password=synthetic-credential")
    context_file = tmp_path / "fake-authority.json"
    context_file.write_text(json.dumps({"approved": True, "valid": True, "sha": SHA,
                                       "receipt": str(uuid.uuid4())}))
    proc = subprocess.run([
        sys.executable, str(Path(deploy_report.__file__)), "write", "--pr", "17", "--sha", SHA,
        "--rc", "21", "--stage", "verify", "--log", str(log), "--repo", str(tmp_path),
        "--publication-context", str(context_file),
    ], capture_output=True, text=True, timeout=20, check=False)
    assert proc.returncode == 1
    assert not writes(http_api)
    assert PRIVATE not in proc.stdout + proc.stderr
    private = "".join(p.read_text() for p in (tmp_path / "shadow/reports").glob("*.md"))
    assert PRIVATE in private and "synthetic-credential" not in private
    assert (tmp_path / "shadow/events.jsonl").is_file()
    assert (tmp_path / "shadow/inbox.jsonl").is_file()


def public_draft(**changes):
    return {"title": "Handle empty selection", "problem": "Empty selections fail validation.",
            "synthetic_reproduction": "Create a synthetic row and select nothing.",
            "acceptance": "Return an actionable validation result.",
            "component": "feedback", "label": "bug", **changes}


class SyntheticTrustedReview:
    """TEST ONLY reviewer-role/receipt registry; no app boolean or public write."""

    def __init__(self, digest=None):
        self.digest = digest
        self.seen = []

    def verify_projection(self, receipt, **facts):
        self.seen.append(facts)
        if receipt != "synthetic-authorized-review" or (self.digest is not None and self.digest != facts["payload_sha256"]):
            raise ValueError("synthetic_review_missing_or_stale")


def test_public_summary_exact_review_and_separate_private_original(master_conn):
    original = ticket(master_conn)
    request_id = str(uuid.uuid4())
    review = SyntheticTrustedReview()
    payload = fp.public_engineering_projection(public_draft(), request_id,
                                               "synthetic-authorized-review", review=review)
    digest = review.seen[0]["payload_sha256"]
    strict = SyntheticTrustedReview(digest)
    assert fp.public_engineering_projection(public_draft(), request_id,
                                           "synthetic-authorized-review", review=strict) == payload
    with pytest.raises(ValueError):
        fp.public_engineering_projection(public_draft(problem="Changed technical scope"), request_id,
                                        "synthetic-authorized-review", review=strict)
    with pytest.raises(ValueError):
        fp.public_engineering_projection(public_draft(), request_id, "app-approved-true", review=strict)
    assert PRIVATE not in json.dumps(payload) and OPS not in json.dumps(payload)
    assert fp.get_ticket(master_conn, original["id"]) == original


@pytest.mark.parametrize("changes", [
    {"approved": True}, {"warehouse": PRIVATE}, {"analysis": PRIVATE}, {"label": PRIVATE},
    {"component": PRIVATE}, {"problem": "https://private.invalid/x"},
    {"problem": "![image](private.png)"}, {"problem": "Closes #7"},
    {"problem": "Part-Of: #7"}, {"problem": "DUIDUI-Studio/DailyCheck-ops"},
    {"problem": "password=synthetic"}, {"problem": "Bearer synthetic"},
    {"problem": "ghp_synthetic123"}, {"problem": "FB-20261107-0007"},
])
def test_public_summary_rejects_private_or_unknown_fields(changes):
    with pytest.raises(ValueError):
        fp.public_engineering_projection(public_draft(**changes), str(uuid.uuid4()),
                                        "synthetic-authorized-review", review=SyntheticTrustedReview())
