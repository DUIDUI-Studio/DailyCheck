"""Issue #16: strict read-only policy over a real loopback MCP server.

The production Starlette app/lifespan is used unchanged; only the explicit
policy argument is new.  Every assertion is caller-observable (wire response,
file bytes, audit line), not a mirror of internal helpers.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import secrets
import shutil
import socket
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import uvicorn
from werkzeug.security import generate_password_hash

from mcp_server import readonly
from mcp_server.main import _build_app

CODE = "approved"
OTHER = "other"
LEGACY = "legacy"
BAD = "bad"
WAL = "wal"
ENCODED = "enc?#"

POLICY_CODES = (CODE, LEGACY, BAD, WAL, ENCODED)
READ_PATHS = ["/api/v1/warehouses", "/api/v1/items"]
WRITE_TOOLS = (
    "restock_create",
    "outbound_create",
    "outbound_rollback",
    "canonical_create",
    "canonical_update",
    "canonical_claim",
    "canonical_unclaim",
    "canonical_fanout",
    "canonical_resolve_conflict",
    "canonical_set_binding_active",
)
NONPILOT_READS = (
    "movements_list",
    "canonical_list",
    "canonical_detail",
    "store_order_list",
    "store_order_detail",
    "wms_stock_list",
    "procurement_store",
    "warehouse_consumption",
    "item_forecast",
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot(root: Path, *, exclude=()) -> dict[str, str]:
    excluded = set(exclude)
    return {
        str(path.relative_to(root)): _sha(path)
        for path in root.rglob("*")
        if path.is_file()
        and path.name != "access.log"
        and str(path.relative_to(root)) not in excluded
    }


def _tree(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


def _user_version(path: Path) -> int:
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


def _read_paths(strict_http) -> list[str]:
    with sqlite3.connect(strict_http["master"]) as conn:
        return json.loads(
            conn.execute(
                "SELECT allowed_read_paths_json FROM agent_tokens WHERE id = ?",
                (strict_http["token_id"],),
            ).fetchone()[0]
        )


def _set_read_paths(strict_http, paths: list[str]) -> None:
    with sqlite3.connect(strict_http["master"]) as conn:
        conn.execute(
            "UPDATE agent_tokens SET allowed_read_paths_json = ? WHERE id = ?",
            (json.dumps(paths), strict_http["token_id"]),
        )
        conn.commit()


def _deadline(*, hours: int = 2) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(hours=hours)
    ).isoformat().replace("+00:00", "Z")


def _insert_token(master, *, warehouses, read_paths=READ_PATHS, write_paths=(), name="lucy"):
    raw = secrets.token_urlsafe(32)
    with sqlite3.connect(master) as conn:
        cursor = conn.execute(
            "INSERT INTO agent_tokens "
            "(name, token_hash, token_prefix, created_by, created_at, "
            "allowed_read_paths_json, allowed_write_paths_json, allowed_warehouse_codes_json) "
            "VALUES (?, ?, ?, 1, datetime('now'), ?, ?, ?)",
            (
                name,
                generate_password_hash(raw, method="pbkdf2:sha256:1000"),
                raw[:8],
                json.dumps(read_paths),
                json.dumps(list(write_paths)),
                json.dumps(warehouses),
            ),
        )
        return raw, cursor.lastrowid


def _init_warehouse(path: Path, *, code: str, quantity: int, unit_cost: float) -> None:
    from db import init_warehouse_db

    init_warehouse_db(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO items (id, sku, name, category_id, quantity, safety_stock, "
            "unit, unit_cost, gram_per_unit, updated_at) "
            "VALUES (1, ?, ?, 1, ?, 1, 'pcs', ?, 0, '2026-11-01')",
            (code, f"{code} item", quantity, unit_cost),
        )
        conn.commit()


def _legacy_schema(path: Path, *, quantity: int) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE categories (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE items (
                id INTEGER PRIMARY KEY, sku TEXT, name TEXT, category_id INTEGER,
                quantity INTEGER, safety_stock INTEGER, unit TEXT,
                unit_cost REAL, gram_per_unit REAL, updated_at TEXT);
            INSERT INTO categories (id, name) VALUES (1, 'legacy');
            """
        )
        conn.execute(
            "INSERT INTO items VALUES (1, 'L1', 'Legacy item', 1, ?, 1, 'pcs', 1.5, 0, '2026-01-01')",
            (quantity,),
        )
        conn.commit()


def _bad_schema(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE categories (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE items (
                id INTEGER PRIMARY KEY, sku TEXT, name TEXT, category_id INTEGER,
                quantity INTEGER, safety_stock INTEGER, unit TEXT, updated_at TEXT);
            INSERT INTO categories (id, name) VALUES (1, 'bad');
            INSERT INTO items VALUES (1, 'B1', 'Bad item', 1, 3, 1, 'pcs', '2026-01-01');
            """
        )
        conn.commit()


def _prebuilt_wal(path: Path, *, quantity: int) -> None:
    """Copy a live WAL (and its shared-memory index) into a closed fixture.

    ``immutable=1`` is deliberately not used anywhere: a read-only connection
    must see committed WAL contents and may update ``-shm`` read marks.
    """
    source = path.with_name(path.name + "-src.db")
    writer = sqlite3.connect(source)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.executescript(
        """
        CREATE TABLE categories (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE items (
            id INTEGER PRIMARY KEY, sku TEXT, name TEXT, category_id INTEGER,
            quantity INTEGER, safety_stock INTEGER, unit TEXT,
            unit_cost REAL, gram_per_unit REAL, updated_at TEXT);
        INSERT INTO categories (id, name) VALUES (1, 'wal');
        """
    )
    writer.execute(
        "INSERT INTO items VALUES (1, 'W1', 'WAL item', 1, ?, 1, 'pcs', 3.0, 0, '2026-01-02')",
        (quantity,),
    )
    writer.commit()
    for suffix in ("", "-wal", "-shm"):
        shutil.copyfile(str(source) + suffix, str(path) + suffix)
    writer.close()
    source.unlink()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(source) + suffix)
        if sidecar.exists():
            sidecar.unlink()


@pytest.fixture
def strict_http(mcp_test_env):
    env = mcp_test_env
    root = Path(env["root"])
    master = Path(env["master"])
    warehouses = Path(env["warehouses"])

    _init_warehouse(warehouses / f"{CODE}.db", code=CODE, quantity=10, unit_cost=2.5)
    _init_warehouse(warehouses / f"{OTHER}.db", code=OTHER, quantity=99, unit_cost=9.9)
    _legacy_schema(warehouses / f"{LEGACY}.db", quantity=7)
    _bad_schema(warehouses / f"{BAD}.db")
    _prebuilt_wal(warehouses / f"{WAL}.db", quantity=21)
    _init_warehouse(warehouses / "enc?#.db", code="E1", quantity=4, unit_cost=0.5)

    files = {
        CODE: warehouses / f"{CODE}.db",
        OTHER: warehouses / f"{OTHER}.db",
        LEGACY: warehouses / f"{LEGACY}.db",
        BAD: warehouses / f"{BAD}.db",
        WAL: warehouses / f"{WAL}.db",
        ENCODED: warehouses / "enc?#.db",
    }
    with sqlite3.connect(master) as conn:
        for code, path in files.items():
            conn.execute(
                "INSERT INTO warehouses (code, name, db_path, created_at) "
                "VALUES (?, ?, ?, datetime('now'))",
                (code, code, str(path)),
            )

    raw, token_id = _insert_token(master, warehouses=list(POLICY_CODES))
    policy_data = {
        "schema_version": 1,
        "token_id": token_id,
        "expires_at": _deadline(),
        "master_path": str(master.resolve()),
        "warehouse_root": str(warehouses.resolve()),
        "warehouses": {code: str(files[code].resolve()) for code in POLICY_CODES},
    }
    policy_path = root / "policy.json"
    policy_path.write_text(json.dumps(policy_data), encoding="utf-8")
    policy = readonly.ReadonlyPolicy.load(policy_path)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            _build_app(policy),
            host="127.0.0.1",
            port=port,
            log_level="error",
            lifespan="on",
            ws="none",
        )
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    try:
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started, "Uvicorn failed to start the strict MCP app"
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=10
        ) as client:
            yield {
                "client": client,
                "env": env,
                "root": root,
                "master": master,
                "warehouses": warehouses,
                "files": files,
                "raw": raw,
                "token_id": token_id,
                "policy": policy,
                "policy_path": policy_path,
            }
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive(), "strict MCP test server did not shut down"


def _session(strict, token=None) -> dict:
    token = strict["raw"] if token is None else token
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
    }
    init = strict["client"].post(
        "/api/mcp/",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "issue-16-readonly", "version": "1"},
            },
        },
    )
    assert init.status_code == 200, init.text
    headers["MCP-Protocol-Version"] = init.json()["result"]["protocolVersion"]
    initialized = strict["client"].post(
        "/api/mcp/",
        headers=headers,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert initialized.status_code == 202, initialized.text
    return headers


@contextlib.contextmanager
def _serve(policy):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            _build_app(policy),
            host="127.0.0.1",
            port=port,
            log_level="error",
            lifespan="on",
            ws="none",
        )
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=10
        ) as client:
            yield client
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive()


def _initialize_body() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "issue-16-support", "version": "1"},
        },
    }


def _rpc(strict, headers, method, params=None, request_id=2) -> dict:
    body = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    response = strict["client"].post("/api/mcp/", headers=headers, json=body)
    assert response.status_code == 200, response.text
    return response.json()["result"]


def _tool(strict, headers, name, arguments=None, request_id=3):
    return _rpc(
        strict,
        headers,
        "tools/call",
        {"name": name, "arguments": arguments or {}},
        request_id=request_id,
    )


def _json(result) -> dict:
    return json.loads(result["content"][0]["text"])


def _audit(strict) -> list[dict]:
    log = Path(strict["env"]["log"])
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_tools_list_is_exactly_the_three_pilot_tools(strict_http):
    headers = _session(strict_http)
    tools = _rpc(strict_http, headers, "tools/list")["tools"]
    assert [tool["name"] for tool in tools] == ["warehouse_list", "items_list", "items_detail"]


def test_three_pilot_calls_return_expected_inventory_values(strict_http):
    headers = _session(strict_http)
    before = _snapshot(strict_http["root"])
    warehouses = _json(_tool(strict_http, headers, "warehouse_list", request_id=10))
    items = _json(
        _tool(strict_http, headers, "items_list", {"warehouse_code": CODE}, request_id=11)
    )
    detail = _json(
        _tool(
            strict_http,
            headers,
            "items_detail",
            {"item_id": 1, "warehouse_code": CODE},
            request_id=12,
        )
    )
    assert warehouses == [
        {"code": code, "name": code} for code in sorted(POLICY_CODES)
    ]
    assert len(items) == 1
    item = items[0]
    assert item["id"] == 1
    assert item["sku"] == CODE
    assert item["name"] == f"{CODE} item"
    assert item["current_stock"] == 10
    assert item["safety_stock"] == 1
    assert item["unit"] == "pcs"
    assert item["unit_cost"] == 2.5
    assert item["gram_per_unit"] == 0
    assert detail == items
    assert _snapshot(strict_http["root"]) == before
    entries = _audit(strict_http)
    assert [entry["tool_name"] for entry in entries] == [
        "warehouse_list",
        "items_list",
        "items_detail",
    ]
    assert all(entry["status"] == 200 for entry in entries)


@pytest.mark.parametrize("name", WRITE_TOOLS + NONPILOT_READS + ("future_tool_xyz",))
def test_disallowed_tools_are_rejected_before_any_business_open(strict_http, name):
    headers = _session(strict_http)
    before = _snapshot(strict_http["root"])
    result = _tool(strict_http, headers, name, {"warehouse_code": CODE})
    assert result.get("isError") is True
    assert result["content"][0]["text"] == "Unknown tool"
    assert _snapshot(strict_http["root"]) == before
    entries = _audit(strict_http)
    assert entries[-1]["tool_name"] == readonly.STRICT_REJECTED_TOOL_LOG_NAME
    assert entries[-1]["status"] == 400


@pytest.mark.parametrize(
    "arguments",
    [
        {"warehouse_code": CODE, "read_only": True},
        {"warehouse_code": CODE, "sql": "select 1"},
        {"warehouse_code": CODE, "db_path": "/tmp/x.db"},
    ],
)
def test_items_list_rejects_extra_or_missing_arguments(strict_http, arguments):
    headers = _session(strict_http)
    before = _snapshot(strict_http["root"])
    result = _tool(strict_http, headers, "items_list", arguments)
    assert result.get("isError") is not True
    assert _json(result) == {"error": "validation_error", "message": "invalid_arguments"}
    assert _snapshot(strict_http["root"]) == before


@pytest.mark.parametrize(
    "arguments",
    [
        {"item_id": True, "warehouse_code": CODE},
        {"item_id": "1", "warehouse_code": CODE},
        {"item_id": 1, "warehouse_code": CODE, "profile": "root"},
    ],
)
def test_items_detail_rejects_bad_types_and_extra_arguments(strict_http, arguments):
    headers = _session(strict_http)
    before = _snapshot(strict_http["root"])
    result = _tool(strict_http, headers, "items_detail", arguments)
    assert result.get("isError") is True or _json(result)["error"] == "validation_error"
    assert _snapshot(strict_http["root"]) == before


def test_cross_warehouse_request_fails_before_target_open(strict_http):
    headers = _session(strict_http)
    before = _snapshot(strict_http["root"])
    result = _tool(strict_http, headers, "items_list", {"warehouse_code": OTHER})
    assert _json(result) == {"error": "forbidden", "message": "forbidden_warehouse"}
    assert _snapshot(strict_http["root"]) == before


def test_metadata_repoint_to_another_file_is_rejected(strict_http):
    headers = _session(strict_http)
    master = strict_http["master"]
    with sqlite3.connect(master) as conn:
        original = conn.execute(
            "SELECT db_path FROM warehouses WHERE code = ?", (CODE,)
        ).fetchone()[0]
        conn.execute(
            "UPDATE warehouses SET db_path = ? WHERE code = ?",
            (str(strict_http["files"][OTHER]), CODE),
        )
        conn.commit()
    try:
        before = _snapshot(strict_http["root"])
        result = _tool(strict_http, headers, "items_list", {"warehouse_code": CODE})
        assert _json(result) == {"error": "forbidden", "message": "forbidden_warehouse"}
        assert _snapshot(strict_http["root"]) == before
    finally:
        with sqlite3.connect(master) as conn:
            conn.execute(
                "UPDATE warehouses SET db_path = ? WHERE code = ?", (original, CODE)
            )
            conn.commit()


@pytest.mark.parametrize(
    "target",
    ["missing.db", "../escaped.db", "link-outside.db"],
)
def test_metadata_escape_or_absent_target_is_rejected_without_creation(
    strict_http, target
):
    headers = _session(strict_http)
    warehouses = strict_http["warehouses"]
    if target == "link-outside.db":
        outside = strict_http["root"].parent / "outside-issue16.db"
        outside.write_bytes(b"not a database")
        link = warehouses / target
        link.symlink_to(outside)
        resolved_target = str(link)
    else:
        resolved_target = str(warehouses / target)
    master = strict_http["master"]
    with sqlite3.connect(master) as conn:
        original = conn.execute(
            "SELECT db_path FROM warehouses WHERE code = ?", (CODE,)
        ).fetchone()[0]
        conn.execute(
            "UPDATE warehouses SET db_path = ? WHERE code = ?", (resolved_target, CODE)
        )
        conn.commit()
    try:
        result = _tool(strict_http, headers, "items_list", {"warehouse_code": CODE})
        assert _json(result) == {"error": "forbidden", "message": "forbidden_warehouse"}
        if target == "missing.db":
            assert not (warehouses / target).exists()
    finally:
        with sqlite3.connect(master) as conn:
            conn.execute(
                "UPDATE warehouses SET db_path = ? WHERE code = ?", (original, CODE)
            )
            conn.commit()


@pytest.mark.parametrize(
    "warehouses, write_paths",
    [
        (None, ()),
        ([], ()),
        ([CODE, OTHER], ()),
        (list(POLICY_CODES), ("/api/v1/restock",)),
    ],
)
def test_token_scope_not_contained_by_policy_is_rejected(mcp_test_env, warehouses, write_paths):
    """A token whose scope is wider/invalid never authenticates under a policy."""
    master = Path(mcp_test_env["master"])
    root = Path(mcp_test_env["root"])
    warehouses_dir = Path(mcp_test_env["warehouses"])
    _init_warehouse(warehouses_dir / f"{CODE}.db", code=CODE, quantity=10, unit_cost=2.5)
    with sqlite3.connect(master) as conn:
        conn.execute(
            "INSERT INTO warehouses (code, name, db_path, created_at) VALUES (?, ?, ?, datetime('now'))",
            (CODE, CODE, str(warehouses_dir / f"{CODE}.db")),
        )
    raw, token_id = _insert_token(
        master, warehouses=warehouses, write_paths=write_paths
    )
    policy_data = {
        "schema_version": 1,
        "token_id": token_id,
        "expires_at": _deadline(),
        "master_path": str(master.resolve()),
        "warehouse_root": str(warehouses_dir.resolve()),
        "warehouses": {CODE: str((warehouses_dir / f"{CODE}.db").resolve())},
    }
    policy_path = root / "policy-scope.json"
    policy_path.write_text(json.dumps(policy_data), encoding="utf-8")
    policy = readonly.ReadonlyPolicy.load(policy_path)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            _build_app(policy), host="127.0.0.1", port=port, log_level="error",
            lifespan="on", ws="none",
        )
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=10
        ) as client:
            response = client.post(
                "/api/mcp/",
                headers={
                    "Authorization": f"Bearer {raw}",
                    "Accept": "application/json, text/event-stream",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "scope-test", "version": "1"},
                    },
                },
            )
            assert response.status_code == 401, response.text
            assert response.json() == {
                "error": "unauthorized",
                "message": "Invalid or missing token",
            }
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive()


def test_revoked_token_and_environment_shortcut_are_rejected(strict_http, monkeypatch):
    headers = _session(strict_http)
    assert _json(_tool(strict_http, headers, "items_list", {"warehouse_code": CODE}))[
        0
    ]["current_stock"] == 10
    with sqlite3.connect(strict_http["master"]) as conn:
        conn.execute(
            "UPDATE agent_tokens SET revoked_at = datetime('now') WHERE id = ?",
            (strict_http["token_id"],),
        )
        conn.commit()
    monkeypatch.setenv("DAILYCHECK_MCP_TOKEN", strict_http["raw"])
    response = strict_http["client"].post(
        "/api/mcp/",
        headers={
            "Authorization": f"Bearer {strict_http['raw']}",
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": 99,
            "method": "tools/list",
        },
    )
    assert response.status_code == 401, response.text


def test_expired_policy_is_rejected(strict_http):
    policy = strict_http["policy"]
    expired = readonly.ReadonlyPolicy(
        token_id=policy.token_id,
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        master_path=policy.master_path,
        warehouse_root=policy.warehouse_root,
        warehouses=policy.warehouses,
    )
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            _build_app(expired), host="127.0.0.1", port=port, log_level="error",
            lifespan="on", ws="none",
        )
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=10
        ) as client:
            response = client.post(
                "/api/mcp/",
                headers={
                    "Authorization": f"Bearer {strict_http['raw']}",
                    "Accept": "application/json, text/event-stream",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "expired", "version": "1"},
                    },
                },
            )
            assert response.status_code == 401, response.text
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive()


def test_legacy_schema_reads_without_migrating(strict_http):
    headers = _session(strict_http)
    legacy = strict_http["files"][LEGACY]
    before = _sha(legacy)
    version_before = _user_version(legacy)
    backups = legacy.parent / "backups"
    backups_before = _tree(backups)
    result = _json(_tool(strict_http, headers, "items_list", {"warehouse_code": LEGACY}))
    assert result[0]["current_stock"] == 7
    assert _sha(legacy) == before
    assert _user_version(legacy) == version_before == 0
    assert not (legacy.with_name(legacy.name + ".migration.lock")).exists()
    assert _tree(backups) == backups_before


def test_incompatible_schema_fails_closed_without_migrating(strict_http):
    headers = _session(strict_http)
    bad = strict_http["files"][BAD]
    before = _sha(bad)
    backups = bad.parent / "backups"
    backups_before = _tree(backups)
    result = _json(_tool(strict_http, headers, "items_list", {"warehouse_code": BAD}))
    assert result == {
        "error": "warehouse_schema_incompatible",
        "message": "warehouse schema is incompatible",
    }
    assert _sha(bad) == before
    assert not (bad.with_name(bad.name + ".migration.lock")).exists()
    assert _tree(backups) == backups_before


def test_missing_approved_file_fails_closed_and_is_not_created(strict_http):
    headers = _session(strict_http)
    target = strict_http["files"][CODE]
    backup = target.with_name(target.name + ".saved")
    shutil.copyfile(target, backup)
    target.unlink()
    try:
        result = _json(_tool(strict_http, headers, "items_list", {"warehouse_code": CODE}))
        assert result == {
            "error": "warehouse_unavailable",
            "message": "warehouse data source unavailable",
        }
        assert not target.exists()
    finally:
        shutil.copyfile(backup, target)
        backup.unlink()


def test_prebuilt_wal_is_read_correctly_without_immutable(strict_http):
    headers = _session(strict_http)
    wal = strict_http["files"][WAL]
    main_before = _sha(wal)
    wal_before = _sha(Path(str(wal) + "-wal"))
    result = _json(_tool(strict_http, headers, "items_list", {"warehouse_code": WAL}))
    assert result[0]["current_stock"] == 21
    assert _sha(wal) == main_before
    assert _sha(Path(str(wal) + "-wal")) == wal_before
    # The read-only connection may update ``-shm`` read marks; that sidecar is
    # documented as a SQLite WAL limitation, not hidden with immutable=1.


def test_encoded_filename_is_read(strict_http):
    headers = _session(strict_http)
    result = _json(_tool(strict_http, headers, "items_list", {"warehouse_code": ENCODED}))
    assert result[0]["current_stock"] == 4


def test_context_isolation_after_errors_and_between_tokens(strict_http):
    headers = _session(strict_http)
    other_raw, _ = _insert_token(strict_http["master"], warehouses=list(POLICY_CODES))
    assert _json(_tool(strict_http, headers, "items_list", {"warehouse_code": CODE}))[
        0
    ]["current_stock"] == 10
    missing = _json(
        _tool(
            strict_http,
            headers,
            "items_detail",
            {"item_id": 9999, "warehouse_code": CODE},
            request_id=20,
        )
    )
    assert missing == {"error": "not_found", "message": "not_found"}
    # A different valid table token is not the pinned identity.
    response = strict_http["client"].post(
        "/api/mcp/",
        headers={
            "Authorization": f"Bearer {other_raw}",
            "Accept": "application/json, text/event-stream",
        },
        json={"jsonrpc": "2.0", "id": 30, "method": "tools/list"},
    )
    assert response.status_code == 401
    assert _json(_tool(strict_http, headers, "items_list", {"warehouse_code": CODE}))[
        0
    ]["current_stock"] == 10


def test_concurrent_requests_do_not_borrow_identity(strict_http):
    _session(strict_http)
    other_raw, _ = _insert_token(strict_http["master"], warehouses=list(POLICY_CODES))

    def call(token: str) -> int:
        response = strict_http["client"].post(
            "/api/mcp/",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 40,
                "method": "tools/call",
                "params": {"name": "items_list", "arguments": {"warehouse_code": CODE}},
            },
        )
        return response.status_code

    tokens = [strict_http["raw"], other_raw] * 5
    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(call, tokens))
    assert statuses == [200, 401] * 5


def test_warehouse_list_has_its_own_path_check_and_policy_does_not_widen_acl(strict_http):
    headers = _session(strict_http)
    original = _read_paths(strict_http)
    try:
        _set_read_paths(strict_http, ["/api/v1/items"])
        assert _json(_tool(strict_http, headers, "warehouse_list", request_id=50)) == {
            "error": "forbidden",
            "message": "forbidden_path",
        }
        assert _json(
            _tool(strict_http, headers, "items_list", {"warehouse_code": CODE}, request_id=51)
        )[0]["current_stock"] == 10

        _set_read_paths(strict_http, ["/api/v1/warehouses"])
        assert _json(
            _tool(strict_http, headers, "items_list", {"warehouse_code": CODE}, request_id=52)
        ) == {"error": "forbidden", "message": "forbidden_path"}
        assert len(_json(_tool(strict_http, headers, "warehouse_list", request_id=53))) == len(
            POLICY_CODES
        )
    finally:
        _set_read_paths(strict_http, original)


def test_missing_pinned_token_id_is_rejected(strict_http):
    policy = strict_http["policy"]
    missing = readonly.ReadonlyPolicy(
        token_id=policy.token_id + 1,
        expires_at=policy.expires_at,
        master_path=policy.master_path,
        warehouse_root=policy.warehouse_root,
        warehouses=policy.warehouses,
    )
    with _serve(missing) as client:
        response = client.post(
            "/api/mcp/",
            headers={
                "Authorization": f"Bearer {strict_http['raw']}",
                "Accept": "application/json, text/event-stream",
            },
            json=_initialize_body(),
        )
        assert response.status_code == 401


def test_strict_mode_disables_the_env_shortcut_even_for_a_valid_token(strict_http, monkeypatch):
    monkeypatch.setenv("DAILYCHECK_MCP_TOKEN", strict_http["raw"])
    response = strict_http["client"].post(
        "/api/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    assert response.status_code == 401


def test_health_uses_the_strict_readonly_master(strict_http):
    response = strict_http["client"].get(
        "/health", headers={"Authorization": f"Bearer {strict_http['raw']}"}
    )
    assert response.status_code == 200
    assert response.json()["checks"]["db"] == "ok"
