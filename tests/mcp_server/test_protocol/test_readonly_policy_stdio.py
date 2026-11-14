"""Issue #16: strict read-only policy over the real stdio transport.

Spawns the actual ``python -m mcp_server --transport stdio`` process, speaks
newline-delimited JSON-RPC to it, and proves a positive process exit.  This is
the second guarded entry path (HTTP is covered separately).
"""
from __future__ import annotations

import json
import os
import secrets
import selectors
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash

from mcp_server import readonly

REPO_ROOT = Path(__file__).resolve().parents[3]
CODE = "stdioapproved"


def _deadline() -> str:
    return (
        datetime.now(timezone.utc) + timedelta(hours=2)
    ).isoformat().replace("+00:00", "Z")


@pytest.fixture
def stdio_env(mcp_test_env):
    env = mcp_test_env
    root = Path(env["root"])
    master = Path(env["master"])
    warehouses = Path(env["warehouses"])
    from db import init_warehouse_db

    warehouse = warehouses / f"{CODE}.db"
    init_warehouse_db(warehouse)
    with sqlite3.connect(warehouse) as conn:
        conn.execute(
            "INSERT INTO items (id, sku, name, category_id, quantity, safety_stock, "
            "unit, unit_cost, gram_per_unit, updated_at) "
            "VALUES (1, 'S1', 'Stdio item', 1, 33, 1, 'pcs', 1.25, 0, '2026-11-01')"
        )
    with sqlite3.connect(master) as conn:
        conn.execute(
            "INSERT INTO warehouses (code, name, db_path, created_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            (CODE, CODE, str(warehouse)),
        )
    raw = secrets.token_urlsafe(32)
    with sqlite3.connect(master) as conn:
        cursor = conn.execute(
            "INSERT INTO agent_tokens "
            "(name, token_hash, token_prefix, created_by, created_at, "
            "allowed_read_paths_json, allowed_write_paths_json, allowed_warehouse_codes_json) "
            "VALUES ('stdio', ?, ?, 1, datetime('now'), ?, '[]', ?)",
            (
                generate_password_hash(raw, method="pbkdf2:sha256:1000"),
                raw[:8],
                json.dumps(["/api/v1/warehouses", "/api/v1/items"]),
                json.dumps([CODE]),
            ),
        )
        token_id = cursor.lastrowid
    policy_path = root / "stdio-policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "token_id": token_id,
                "expires_at": _deadline(),
                "master_path": str(master.resolve()),
                "warehouse_root": str(warehouses.resolve()),
                "warehouses": {CODE: str(warehouse.resolve())},
            }
        ),
        encoding="utf-8",
    )
    assert readonly.ReadonlyPolicy.load(policy_path).token_id == token_id
    return {"root": root, "raw": raw, "policy_path": policy_path, "token_id": token_id}


def _subprocess_env(root: Path, token: str) -> dict:
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "DAILYCHECK_DB_DIR": str(root),
        "DAILYCHECK_MCP_TOKEN": token,
        "DAILYCHECK_SECRET_KEY": "issue-16-test-secret",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "LANG": "C.UTF-8",
    }


class _Transport:
    def __init__(self, process: subprocess.Popen):
        self.process = process
        self.selector = selectors.DefaultSelector()
        self.selector.register(process.stdout, selectors.EVENT_READ)
        self.buffer = b""

    def send(self, message: dict) -> None:
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        self.process.stdin.flush()

    def read_message(self, *, timeout: float = 15.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            if self.buffer:
                line, _, remainder = self.buffer.partition(b"\n")
                self.buffer = remainder
                if line.strip():
                    return json.loads(line)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for stdio MCP response")
            events = self.selector.select(remaining)
            if not events:
                raise TimeoutError("timed out waiting for stdio MCP response")
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise EOFError("stdio MCP process closed stdout")
            self.buffer += chunk

    def close_stdin_and_wait(self, *, timeout: float = 15.0) -> int:
        self.process.stdin.close()
        try:
            return self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
            raise


def _start(stdio_env, token=None) -> _Transport:
    token = stdio_env["raw"] if token is None else token
    process = subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-m",
            "mcp_server",
            "--transport",
            "stdio",
            "--readonly-policy",
            str(stdio_env["policy_path"]),
        ],
        cwd=str(REPO_ROOT),
        env=_subprocess_env(stdio_env["root"], token),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return _Transport(process)


def _initialize(transport: _Transport) -> None:
    transport.send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "issue-16-stdio", "version": "1"},
            },
        }
    )
    response = transport.read_message()
    assert response["id"] == 1
    transport.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert transport.process.poll() is None, "server exited during initialization"


def test_stdio_strict_policy_serves_three_tools_and_exits_zero(stdio_env):
    transport = _start(stdio_env)
    stderr = b""
    try:
        _initialize(transport)
        transport.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        listed = transport.read_message()
        assert listed["id"] == 2
        assert [tool["name"] for tool in listed["result"]["tools"]] == [
            "warehouse_list",
            "items_list",
            "items_detail",
        ]
        transport.send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "items_list",
                    "arguments": {"warehouse_code": CODE},
                },
            }
        )
        called = transport.read_message()
        assert called["id"] == 3
        assert called["result"].get("isError") is not True
        payload = json.loads(called["result"]["content"][0]["text"])
        assert payload[0]["current_stock"] == 33
        transport.send(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "restock_create", "arguments": {}},
            }
        )
        rejected = transport.read_message()
        assert rejected["id"] == 4
        assert rejected["result"]["isError"] is True
        assert rejected["result"]["content"][0]["text"] == "Unknown tool"
    finally:
        try:
            returncode = transport.close_stdin_and_wait()
        finally:
            if transport.process.poll() is None:
                transport.process.kill()
                transport.process.wait(timeout=5)
            stderr = transport.process.stderr.read()
            transport.process.stderr.close()
            transport.process.stdout.close()
    assert returncode == 0, stderr.decode(errors="replace")[-2000:]


def test_stdio_wrong_token_is_rejected_but_process_exits_cleanly(stdio_env):
    transport = _start(stdio_env, token=secrets.token_urlsafe(32))
    try:
        _initialize(transport)
        transport.send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "items_list",
                    "arguments": {"warehouse_code": CODE},
                },
            }
        )
        called = transport.read_message()
        assert called["id"] == 3
        assert called["result"].get("isError") is not True
        assert json.loads(called["result"]["content"][0]["text"]) == {
            "error": "unauthorized",
            "message": "invalid token",
        }
        returncode = transport.close_stdin_and_wait()
    finally:
        if transport.process.poll() is None:
            transport.process.kill()
            transport.process.wait(timeout=5)
        transport.process.stderr.close()
        transport.process.stdout.close()
    assert returncode == 0
