"""Issue #103: real TCP StreamableHTTP auth, path and warehouse boundaries."""
from __future__ import annotations

import json
import secrets
import socket
import sqlite3
import threading
import time

import httpx
import pytest
import uvicorn
from werkzeug.security import generate_password_hash

from mcp_server.main import _build_app

TOOLS = {
    "warehouse_consumption": "/api/v1/consumption/warehouse",
    "item_consumption": "/api/v1/consumption/item/<id>",
}


def _snapshot(root):
    """Include schema, data, migrations/backups and SQLite sidecars, not just row counts."""
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*") if path.is_file() and path.name != "access.log"
    }


@pytest.fixture
def http_mcp(mcp_test_env):
    """Bind a loopback ephemeral port; use the production app and lifespan unchanged."""
    env = mcp_test_env
    with sqlite3.connect(env["master"]) as conn:
        conn.execute(
            "INSERT INTO users (id, username, password_hash, created_at) "
            "VALUES (1, 'http-test', 'unused', datetime('now'))"
        )
        from db import init_warehouse_db
        for code in ("allowed", "other"):
            path = env["warehouses"] / f"{code}.db"
            init_warehouse_db(path)
            with sqlite3.connect(path) as wh:
                wh.execute(
                    "INSERT INTO items (id, sku, name, category_id, quantity, updated_at) "
                    "VALUES (1, ?, ?, 1, 10, datetime('now'))", (code, code),
                )
                wh.execute(
                    "INSERT INTO outbound_requests "
                    "(item_id, requested_quantity, reason, created_at) "
                    "VALUES (1, 3, 'HTTP seed', datetime('now', '-1 day'))"
                )
                # An unauthorized query must not even trigger the normal MCP migration path.
                wh.execute("PRAGMA user_version=6")
            conn.execute(
                "INSERT INTO warehouses (code, name, db_path, created_at) "
                "VALUES (?, ?, ?, datetime('now'))", (code, code, str(path)),
            )

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        _build_app(), host="127.0.0.1", port=port, log_level="error", lifespan="on",
        ws="none",
    ))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    try:
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started, "Uvicorn failed to start the real MCP app"
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=10) as client:
            yield env, client
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive(), "MCP test server did not shut down"


def _token(env, read_paths, warehouses, *, write_paths=()):
    raw = secrets.token_urlsafe(32)
    with sqlite3.connect(env["master"]) as conn:
        cursor = conn.execute(
            "INSERT INTO agent_tokens "
            "(name, token_hash, token_prefix, created_by, created_at, "
            "allowed_read_paths_json, allowed_write_paths_json, allowed_warehouse_codes_json) "
            "VALUES ('http-test', ?, ?, 1, datetime('now'), ?, ?, ?)",
            (generate_password_hash(raw, method="pbkdf2:sha256:1000"), raw[:8],
             json.dumps(read_paths), json.dumps(list(write_paths)), json.dumps(warehouses)),
        )
        return raw, cursor.lastrowid


def _call(client, token, name, warehouse="allowed"):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}
    init = client.post("/api/mcp/", headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "issue-103-regression", "version": "1"},
        },
    })
    assert init.status_code == 200, init.text
    headers["MCP-Protocol-Version"] = init.json()["result"]["protocolVersion"]
    initialized = client.post("/api/mcp/", headers=headers, json={
        "jsonrpc": "2.0", "method": "notifications/initialized",
    })
    assert initialized.status_code == 202, initialized.text
    args = {"warehouse_code": warehouse}
    if name == "item_consumption":
        args.update(item_id=1, include_turnover=True)
    response = client.post("/api/mcp/", headers=headers, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": name, "arguments": args},
    })
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


def _assert_audit(env, token_id, name, status):
    entries = [json.loads(line) for line in env["log"].read_text().splitlines()]
    assert len(entries) == 1
    assert entries[0]["agent_token_id"] == token_id
    assert entries[0]["tool_name"] == name
    assert entries[0]["status"] == status


@pytest.mark.parametrize("name", TOOLS)
@pytest.mark.parametrize("grant", ["empty", "unrelated", "sibling", "write-only"])
def test_consumption_http_denies_missing_read_path_without_db_changes(http_mcp, name, grant):
    env, client = http_mcp
    sibling = next(path for tool, path in TOOLS.items() if tool != name)
    read_paths = {"empty": [], "unrelated": ["/api/v1/items"],
                  "sibling": [sibling], "write-only": []}[grant]
    write_paths = [TOOLS[name]] if grant == "write-only" else []
    raw, token_id = _token(env, read_paths, ["allowed"], write_paths=write_paths)
    before = _snapshot(env["root"])
    result = _call(client, raw, name)
    assert result == {"error": "forbidden", "message": "forbidden_path"}
    assert _snapshot(env["root"]) == before
    _assert_audit(env, token_id, name, 403)


@pytest.mark.parametrize("name", TOOLS)
@pytest.mark.parametrize("warehouses", [["allowed"], []])
def test_consumption_http_denies_cross_warehouse_without_db_changes(http_mcp, name, warehouses):
    env, client = http_mcp
    raw, token_id = _token(env, [TOOLS[name]], warehouses)
    before = _snapshot(env["root"])
    result = _call(client, raw, name, "other")
    assert result == {"error": "forbidden", "message": "forbidden_warehouse"}
    assert _snapshot(env["root"]) == before
    _assert_audit(env, token_id, name, 403)


@pytest.mark.parametrize("name", TOOLS)
@pytest.mark.parametrize("grant", ["exact", "parent", "wildcard", "all"])
def test_consumption_http_allows_original_read_path(http_mcp, name, grant):
    env, client = http_mcp
    path = {"exact": TOOLS[name], "parent": "/api/v1/consumption",
            "wildcard": "/api/v1/consumption/*", "all": "*"}[grant]
    raw, token_id = _token(env, [path], ["allowed"])
    result = _call(client, raw, name)
    if name == "warehouse_consumption":
        assert len(result["items"]) == 1
        assert result["items"][0]["consume_qty"] == 3
    else:
        assert result["item_id"] == 1
        assert result["consume_7d"]["qty"] == 3
        assert "inventory_turnover" in result
    _assert_audit(env, token_id, name, 200)


def test_consumption_http_token_context_does_not_bleed_between_requests(http_mcp):
    env, client = http_mcp
    allowed, _ = _token(env, ["*"], None)
    denied, token_id = _token(env, [], ["allowed"])
    assert _call(client, allowed, "item_consumption")["consume_7d"]["qty"] == 3
    before = _snapshot(env["root"])
    for name in ("items_list", "warehouse_consumption", "item_consumption"):
        assert _call(client, denied, name) == {"error": "forbidden", "message": "forbidden_path"}
    assert _snapshot(env["root"]) == before
    entries = [json.loads(line) for line in env["log"].read_text().splitlines()]
    assert [entry["status"] for entry in entries] == [200, 403, 403, 403]
    assert {entry["agent_token_id"] for entry in entries[1:]} == {token_id}
