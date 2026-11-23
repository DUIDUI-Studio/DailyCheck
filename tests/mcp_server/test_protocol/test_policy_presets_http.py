"""Issue #39 policy presets through real admin login and the StreamableHTTP entry point."""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import socket
import sqlite3
import threading
import time
from contextlib import asynccontextmanager, closing, contextmanager

import httpx
import pytest
import uvicorn
from jsonschema import validate
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from werkzeug.datastructures import MultiDict
from werkzeug.security import check_password_hash, generate_password_hash

from mcp_server import main
from mcp_server.data.unit_of_work import policy_read_connection
from mcp_server.protocol.registry import REGISTRY, get_policy_actions
from mcp_server.service.auth import authenticate
from mcp_server.service.policy import POLICY_TOKEN_PREFIX, PRODUCTION_CONSUMPTION_READ, SAFE_FIELDS

STORES = ("store_a", "store_b", "store_c", "store_d")
OUTSIDE = "other_store"
DC = "distribution"
LEGACY_GRANTS = {action: SAFE_FIELDS[action] for action in ("inventory.read", "orders.read")}
ITEM_ARGS = {"item_id": 1, "warehouse_code": STORES[0]}
INITIALIZE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "store-read-regression", "version": "1"},
    },
}


def _snapshot(root):
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*") if path.is_file() and path.name != "access.log"
    }


def _headers(token):
    headers = {"Accept": "application/json, text/event-stream"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    return headers


def _db_update(path, sql, args=()):
    with closing(sqlite3.connect(path)) as conn, conn:
        return conn.execute(sql, args).fetchone()


@pytest.fixture
def storefronts(mcp_test_env):
    from db import init_warehouse_db

    env = mcp_test_env
    paths = {}
    master = env["master"]
    with closing(sqlite3.connect(master)) as conn, conn:
        password = generate_password_hash("local-test-only", method="pbkdf2:sha256:1000")
        conn.executemany(
            "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
            "VALUES (?, ?, ?, ?, 'test')",
            [(1, "owner", password, 1), (2, "member", password, 0), (3, "another-admin", password, 1)],
        )
        for index, code in enumerate((*STORES, OUTSIDE, DC), start=1):
            # URI metacharacters ensure the existing-only connection encodes paths.
            path = paths[code] = env["warehouses"] / f"{code} #?.db"
            init_warehouse_db(path)
            _db_update(
                path, "INSERT INTO items (id, sku, name, category_id, quantity, unit, unit_cost, updated_at) "
                "VALUES (1, ?, 'internal-name', 1, ?, 'kg', 999, 'stock-update')",
                (f"sku-{code}", index + 0.5),
            )
            _db_update(path, "PRAGMA user_version=6")
            conn.execute(
                "INSERT INTO warehouses (code, name, db_path, warehouse_type, created_at) "
                "VALUES (?, ?, ?, ?, 'test')",
                (code, f"name-{code}", f"db/warehouses/{path.name}",
                 "distribution_center" if code == DC else "storefront"),
            )
        conn.execute(
            "INSERT INTO warehouse_users (user_id, warehouse_id, role) "
            "VALUES (1, 1, 'admin'), (2, 1, 'staff')"
        )
        conn.executemany(
            "INSERT INTO store_orders (id, order_no, store_warehouse_code, dc_warehouse_code, "
            "flow_type, status, requested_by, note, expected_delivery_date, created_at, updated_at, "
            "approved_at, delivered_at) VALUES (?, ?, ?, ?, ?, ?, 1, 'internal-note', ?, ?, ?, ?, ?)",
            [
                (1, "own-A", STORES[0], DC, "store_order", "delivered",
                 "2026-01-03", "2026-01-01", "2026-01-04", "2026-01-02", "2026-01-04"),
                (2, "foreign-participating", OUTSIDE, STORES[0], "store_order", "pending",
                 None, "2026-01-01", "2026-01-01", None, None),
                (3, "internal-transfer", STORES[0], DC, "dc_transfer", "approved",
                 None, "2026-01-01", "2026-01-01", None, None),
                (4, "own-B", STORES[1], DC, "store_order", "pending",
                 None, "2026-01-01", "2026-01-01", None, None),
                (5, "own-C", STORES[2], DC, "store_order", "approved",
                 None, "2026-01-01", "2026-01-01", None, None),
                (6, "own-D", STORES[3], DC, "store_order", "shipped",
                 None, "2026-01-01", "2026-01-01", None, None),
            ],
        )
        conn.executemany(
            "INSERT INTO store_order_items (id, order_id, canonical_id, source_warehouse_code, "
            "quantity, unit, created_at) VALUES (?, ?, 1, ?, 7, 'kg', 'test')",
            [(1, 1, STORES[0]), (2, 1, STORES[1]), (3, 1, STORES[2]), (4, 1, OUTSIDE),
             (5, 1, None), (6, 1, None), (7, 2, STORES[0])],
        )
        conn.executemany(
            "INSERT INTO store_order_deliveries (id, order_id, delivery_no, warehouse_code, "
            "shipped_at, delivered_at, tracking_note, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'warehouse-delivered-internal', 'internal-tracking', 'test')",
            [(1, 1, "d1", STORES[0], "ship-A"), (2, 1, "d2", STORES[1], "ship-B"),
             (3, 1, "d3", OUTSIDE, "private-outside-shipment"), (4, 1, "d4", None, "legacy-ship"),
             (5, 1, "d5", None, "private-attributed-ship"), (6, 2, "d6", None, "private-foreign-shipment")],
        )
        conn.execute(
            "INSERT INTO store_order_delivery_items (delivery_id, order_item_id, warehouse_code, "
            "item_id, quantity, op_no, created_at) VALUES (5, 6, ?, 1, 1, 'outsource', 'test')",
            (OUTSIDE,),
        )
        conn.executemany(
            "INSERT INTO store_order_receipts (order_id, order_item_id, quantity, received_by, "
            "note, created_at, applied) VALUES (?, ?, 4, 1, 'internal-receipt-note', ?, ?)",
            [(1, 1, "receipt-A", 1), (1, 2, "receipt-B", 1), (1, 3, "pending-receipt", 0),
             (1, 4, "private-outside-receipt", 1), (1, 5, "legacy-receipt", 1),
             (1, 6, "private-attributed-receipt", 1), (2, 7, "private-foreign-receipt", 1),
             (1, 7, "private-mismatched-line", 1)],
        )
    return env, paths


@contextmanager
def _listener():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        main._build_app(), host="127.0.0.1", port=port, lifespan="on", log_level="error", ws="none",
    ))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    try:
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        yield f"http://127.0.0.1:{port}/api/mcp/"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive()


@asynccontextmanager
async def _session(url, raw):
    async with httpx.AsyncClient(headers=_headers(raw), trust_env=False, timeout=10) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


async def _call(session, name, args, *, error=None):
    result = await session.call_tool(name, args)
    assert result.isError is (error is not None), result
    assert len(result.content) == 1 and result.content[0].type == "text"
    payload = json.loads(result.content[0].text)
    if error is not None:
        assert payload["error"] == error
    return payload


def test_migrated_outside_fulfillment_never_becomes_unattributed(issued_policy):
    from migrations import migrate_multi_warehouse_fulfillment

    env, _, raw, _ = issued_policy
    with closing(sqlite3.connect(env["master"])) as conn, conn:
        # Restore the pre-v4 source columns/table shape. Historical receipts
        # could have fulfilled quantity but no accumulated shipped quantity.
        conn.executescript("""
            DROP TRIGGER order_item_identity_immutable;
            DROP TABLE store_order_delivery_items;
            ALTER TABLE store_order_items DROP COLUMN source_warehouse_code;
            ALTER TABLE store_order_items DROP COLUMN ship_op_no;
            ALTER TABLE store_order_deliveries DROP COLUMN warehouse_code;
            UPDATE store_order_items
               SET shipped_quantity=0, fulfilled_quantity=4 WHERE id=1;
        """)
        result = migrate_multi_warehouse_fulfillment(conn, {})
        assert result["anomalies"] == [{
            "order_item_id": 1, "order_id": 1, "quantity": 7.0,
            "shipped_quantity": 0.0, "fulfilled_quantity": 4.0,
        }]
        assert conn.execute("SELECT source_warehouse_code FROM store_order_items WHERE id=1").fetchone() == (None,)
        assert conn.execute("SELECT warehouse_code FROM store_order_deliveries WHERE id=1").fetchone() == (DC,)
        assert conn.execute("SELECT COUNT(*) FROM store_order_delivery_items").fetchone() == (0,)
        # A post-migration legacy NULL batch must not be relabeled either.
        conn.execute("UPDATE store_order_deliveries SET warehouse_code=NULL WHERE id=4")
    before = _snapshot(env["root"])

    async def exercise(url):
        async with _session(url, raw) as scoped:
            page = await _call(scoped, "store_order_list", {"warehouse_code": STORES[0], "status": "delivered"})
            assert [row["order_no"] for row in page["orders"]] == ["own-A"]
            assert page["orders"][0]["fulfillment"] == {
                "warehouses": [],
                "unattributed_shipments": [],
                "unattributed_receipts": [],
            }

    with _listener() as url:
        asyncio.run(exercise(url))
    assert _snapshot(env["root"]) == before


def test_mixed_fulfillment_keeps_unknown_facts_with_outside_primary_source(issued_policy):
    from blueprints.store_ordering_pure import _primary_source_warehouse

    env, _, raw, _ = issued_policy
    sources = [*STORES[:3], DC, DC, DC, DC]
    primary = _primary_source_warehouse(sources)
    assert primary == DC
    with closing(sqlite3.connect(env["master"])) as conn, conn:
        conn.execute(
            "INSERT INTO store_orders (id, order_no, store_warehouse_code, dc_warehouse_code, "
            "status, requested_by, created_at, updated_at) "
            "VALUES (8, 'mixed-outside-primary', ?, ?, 'shipped', 1, 'test', 'test')",
            (STORES[0], primary),
        )
        conn.executemany(
            "INSERT INTO store_order_items (id, order_id, canonical_id, source_warehouse_code, "
            "quantity, unit, created_at) VALUES (?, 8, 1, ?, 7, 'kg', 'test')",
            enumerate([*sources, None, None], start=101),
        )
        conn.executemany(
            "INSERT INTO store_order_deliveries (id, order_id, delivery_no, warehouse_code, shipped_at, created_at) "
            "VALUES (?, 8, ?, ?, ?, 'test')",
            [
                (20, "mixed-unknown", None, "unknown-mixed-shipment"),
                (21, "mixed-allowed", STORES[0], "allowed-mixed-shipment"),
                (22, "mixed-known-outside", DC, "hidden-explicit-shipment"),
                (23, "mixed-linked-outside", None, "hidden-linked-shipment"),
            ],
        )
        conn.execute(
            "INSERT INTO store_order_delivery_items (delivery_id, order_item_id, warehouse_code, "
            "item_id, quantity, op_no, created_at) VALUES (23, 109, ?, 1, 1, 'mixed-outside-source', 'test')",
            (DC,),
        )
        conn.executemany(
            "INSERT INTO store_order_receipts (order_id, order_item_id, quantity, received_by, created_at, applied) "
            "VALUES (8, ?, 1, 1, ?, 1)",
            [(101, "allowed-mixed-receipt"), (104, "hidden-explicit-receipt"),
             (108, "unknown-mixed-receipt"), (109, "hidden-linked-receipt")],
        )
        conn.execute(
            "INSERT INTO store_order_deliveries (order_id, delivery_no, shipped_at, created_at) "
            "VALUES (4, 'unknown-no-recorded-source', 'unlinked-shipment', 'test')"
        )
    before = _snapshot(env["root"])

    async def exercise(url):
        async with _session(url, raw) as scoped:
            page = await _call(scoped, "store_order_list", {"warehouse_code": STORES[0], "status": "shipped"})
            assert [row["order_no"] for row in page["orders"]] == ["mixed-outside-primary"]
            assert page["orders"][0]["fulfillment"] == {
                "warehouses": [
                    {"warehouse_code": STORES[0], "warehouse_name": f"name-{STORES[0]}",
                     "shipments": ["allowed-mixed-shipment"], "receipts": ["allowed-mixed-receipt"]},
                    *({"warehouse_code": code, "warehouse_name": f"name-{code}", "shipments": [], "receipts": []}
                      for code in STORES[1:3]),
                ],
                "unattributed_shipments": ["unknown-mixed-shipment"],
                "unattributed_receipts": ["unknown-mixed-receipt"],
            }
            no_source = await _call(scoped, "store_order_list", {"warehouse_code": STORES[1]})
            assert [row["order_no"] for row in no_source["orders"]] == ["own-B"]
            assert no_source["orders"][0]["fulfillment"] == {
                "warehouses": [],
                "unattributed_shipments": ["unlinked-shipment"],
                "unattributed_receipts": [],
            }

    with _listener() as url:
        asyncio.run(exercise(url))
    assert _snapshot(env["root"]) == before



ADMIN = "/admin/agent-tokens"
CREATE = f"{ADMIN}/create"


def _hidden(response, name):
    return re.search(fr'name="{name}" value="([^"]+)"', response.get_data(as_text=True))[1]


def _login(app, name="owner"):
    client = app.test_client()
    login = client.get("/login")
    assert client.post("/login", data={
        "username": name, "password": "local-test-only", "csrf_token": _hidden(login, "csrf_token"),
    }).status_code == 302
    picker = client.get("/select-warehouse")
    assert client.post("/select-warehouse/1", data={
        "csrf_token": _hidden(picker, "csrf_token"),
    }).status_code == 302
    return client


def _post(client, action, fields):
    lookup = ADMIN if client.get(ADMIN).status_code == 200 else "/login"
    csrf = _hidden(client.get(lookup), "csrf_token")
    return client.post(CREATE, data=MultiDict([("csrf_token", csrf), ("policy_action", action), *fields]))


def _publish(client, slug, grants):
    fields = [("preset_name", slug)]
    fields.extend((f"field_{action}", field) for action, selected in grants.items() for field in selected)
    return _post(client, "publish", fields)


def _preview(client, label, version, warehouses):
    return _post(client, "preview", [
        ("token_name", label), ("preset", version), *(("policy_warehouses", code) for code in warehouses),
    ])


def _confirm(client, preview):
    return _post(client, "confirm", [("confirmation", _hidden(preview, "confirmation"))])


def _read_receipt(response):
    assert response.status_code == 201
    assert response.headers["Cache-Control"] == "private, no-store"
    bearer = re.search(r'id="token-display" type="text" readonly value="([^"]+)"',
                       response.get_data(as_text=True))[1]
    identity = int(re.search(r'id="mcp-policy-token-id" data-token-id="(\d+)"',
                             response.get_data(as_text=True))[1])
    assert bearer.startswith(POLICY_TOKEN_PREFIX)
    assert all(bearer not in value for value in response.headers.getlist("Set-Cookie"))
    return bearer, identity


@pytest.fixture
def policy_admin(storefronts, monkeypatch):
    from app import create_app

    env, _ = storefronts
    monkeypatch.setattr("blueprints.audit_pure.ADMIN_AUDIT_LOG", env["root"] / "admin-audit.log")
    monkeypatch.setattr("blueprints.forecast._start_scheduler", lambda: None)
    monkeypatch.setattr("blueprints.procurement_digest.start_scheduler", lambda: None)
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_FORCE_ENABLED=True)
    return app, _login(app)


@pytest.fixture
def issued_policy(storefronts, policy_admin):
    env, paths = storefronts
    _, admin = policy_admin
    assert _publish(admin, "stock-and-orders", LEGACY_GRANTS).status_code == 302
    raw, identity = _read_receipt(_confirm(admin, _preview(admin, "four stores", "stock-and-orders:1", STORES)))
    return env, paths, raw, identity


def test_two_issued_preset_versions_share_the_existing_route_and_legacy_catalog(storefronts, policy_admin, monkeypatch):
    env, paths = storefronts
    master = env["master"]
    app, admin = policy_admin
    member = _login(app, "member")
    other_admin = _login(app, "another-admin")
    other_session = _login(app)
    assert _publish(member, "stock-and-orders", LEGACY_GRANTS).status_code == 403
    assert _publish(admin, "stock-and-orders", {"inventory.read": ["unit_cost"]}).status_code == 400
    assert _publish(admin, "stock-and-orders", {}).status_code == 400
    assert _publish(admin, "stock-and-orders", LEGACY_GRANTS).status_code == 302
    assert _post(admin, "preview", [("token_name", "empty"), ("preset", "stock-and-orders:1")]).status_code == 400
    assert _preview(admin, "broad", "stock-and-orders:1", [*STORES, DC]).status_code == 400
    assert _preview(admin, "duplicated", "stock-and-orders:1", [STORES[0]] * 2).status_code == 400

    before_preview = _snapshot(env["root"])
    preview = _preview(admin, "four stores", "stock-and-orders:1", STORES)
    assert preview.status_code == 200 and preview.headers["Cache-Control"] == "private, no-store"
    effective = preview.get_data(as_text=True)
    assert 'id="mcp-policy-effective"' in effective
    assert all(word in effective for word in (*STORES, "inventory.read", "orders.read", "stock-and-orders v1",
                                               "current_stock", "fulfillment"))
    assert all(word in effective for word in (
        "签发人 #1", "items_detail", "store_order_list", "所有写入均拒绝", "旧版读取路径",
        "旧版写入路径", "旧版仓库范围", "回退到旧版本时", "仓库名称",
    ))
    assert 'id="token-display"' not in effective and _snapshot(env["root"]) == before_preview
    assert _confirm(other_admin, preview).status_code == 403
    assert _confirm(other_session, preview).status_code == 403
    assert _post(admin, "confirm", [("confirmation", _hidden(preview, "confirmation") + "x")]).status_code == 400
    assert admin.post(CREATE, data={"policy_action": "confirm", "confirmation": _hidden(preview, "confirmation")}).status_code == 400
    with monkeypatch.context() as expired:
        expired.setattr("blueprints.agent_token_policies._PREVIEW_MAX_AGE_SECONDS", -1)
        assert _confirm(admin, preview).status_code == 400
    _db_update(master, """
        CREATE TRIGGER fixture_binding_failure BEFORE INSERT ON mcp_policy_bindings
        BEGIN SELECT RAISE(ABORT, 'fixture binding failure'); END
    """)
    before_failure = _snapshot(env["root"])
    failed = _confirm(admin, preview)
    assert failed.status_code == 409 and b'id="token-display"' not in failed.data
    assert _snapshot(env["root"]) == before_failure
    _db_update(master, "DROP TRIGGER fixture_binding_failure")

    first_raw, first_id = _read_receipt(_confirm(admin, preview))
    first_snapshot = _db_update(master, "SELECT * FROM mcp_policy_bindings WHERE token_id=?", (first_id,))
    assert _confirm(admin, preview).status_code == 409
    assert _confirm(admin, _preview(admin, "four stores", "stock-and-orders:1", STORES)).status_code == 409

    small = {"orders.read": ["order_no", "status"]}
    assert _publish(admin, "stock-and-orders", small).status_code == 302
    second_raw, second_id = _read_receipt(
        _confirm(admin, _preview(admin, "one store", "stock-and-orders:2", [STORES[1]])),
    )
    assert first_id != second_id and first_raw != second_raw
    assert _db_update(master, "SELECT * FROM mcp_policy_bindings WHERE token_id=?", (first_id,)) == first_snapshot
    listing = admin.get(ADMIN).get_data(as_text=True)
    second_listing = re.search(
        fr'class="policy-binding" data-token-id="{second_id}".*?</td>', listing, re.S,
    )[0]
    assert all(word in second_listing for word in (
        "stock-and-orders v2", STORES[1], "签发人 #1", "store_order_list", "order_no", "status",
        "所有写入均拒绝", "旧版读取路径", "旧版写入路径", "旧版仓库范围", "回退到旧版本时",
    ))
    assert "items_detail" not in second_listing and "fulfillment" not in second_listing
    for token_id, raw, version, codes in (
        (first_id, first_raw, 1, STORES), (second_id, second_raw, 2, STORES[1:2]),
    ):
        row = _db_update(
            master, "SELECT a.token_hash, a.token_prefix, a.encrypted_token, a.created_by, "
            "a.allowed_read_paths_json, a.allowed_write_paths_json, a.allowed_warehouse_codes_json, "
            "b.preset_version, b.warehouse_codes_json, b.created_by "
            "FROM agent_tokens a JOIN mcp_policy_bindings b ON b.token_id=a.id WHERE a.id=?", (token_id,),
        )
        assert check_password_hash(row[0], raw)
        assert row[1:8] == (raw[:8], None, 1, "[]", "[]", "[]", version)
        assert json.loads(row[8]) == list(codes) and row[9] == 1
        assert raw not in admin.get(ADMIN).get_data(as_text=True)
        assert raw not in (env["root"] / "admin-audit.log").read_text()
        with admin.session_transaction() as browser:
            assert raw not in repr(dict(browser))

    configured = paths[STORES[0]]
    target = env["root"] / "portable-warehouse.db"
    configured.rename(target)
    configured.symlink_to(target)
    ordinary = secrets.token_urlsafe(32)
    _db_update(
        master,
        "INSERT INTO agent_tokens (name, token_hash, token_prefix, created_by, created_at, "
        "allowed_read_paths_json, allowed_write_paths_json, allowed_warehouse_codes_json) "
        "VALUES ('ordinary', ?, ?, 1, 'test', '[\"/api/v1/store-orders\"]', '[]', ?)",
        (generate_password_hash(ordinary, method="pbkdf2:sha256:1000"), ordinary[:8], json.dumps(STORES)),
    )
    monkeypatch.setenv("DAILYCHECK_MCP_TOKEN", "synthetic-shared-secret")
    before_reads = _snapshot(env["root"])

    async def exercise(url):
        async with (_session(url, first_raw) as first, _session(url, second_raw) as second,
                    _session(url, "synthetic-shared-secret") as legacy):
            assert {t.name for t in (await legacy.list_tools()).tools} == {
                entry.name for entry in REGISTRY if not entry.policy_only
            }
            assert {t.name for t in (await first.list_tools()).tools} == {"items_detail", "store_order_list"}
            second_tools = (await second.list_tools()).tools
            assert [t.name for t in second_tools] == ["store_order_list"]
            assert "fulfillment" not in second_tools[0].description
            assert all(t.inputSchema.get("additionalProperties") is not False
                       for t in (await legacy.list_tools()).tools
                       if t.name in {"items_detail", "store_order_list"})
            for index, code in enumerate(STORES, start=1):
                assert await _call(first, "items_detail", {**ITEM_ARGS, "warehouse_code": code}) == [{
                    "sku": f"sku-{code}", "current_stock": index + 0.5, "unit": "kg", "updated_at": "stock-update",
                }]
            page = await _call(first, "store_order_list", {})
            assert {order["order_no"] for order in page["orders"]} == {"own-A", "own-B", "own-C", "own-D"}
            assert "fulfillment" in page["orders"][0]
            assert (await _call(second, "store_order_list", {}))["orders"] == [{"order_no": "own-B", "status": "pending"}]
            await _call(second, "items_detail", {**ITEM_ARGS, "warehouse_code": STORES[1]}, error="forbidden")
            await _call(second, "store_order_list", {"warehouse_code": STORES[0]}, error="forbidden")
            await _call(first, "store_order_list", {"warehouse_code": OUTSIDE}, error="forbidden")
            await _call(first, "store_order_list", {"fields": ["note"]}, error="validation_error")
            await _call(first, "items_detail", {**ITEM_ARGS, "item_id": True}, error="validation_error")
            for name in ("store_order_detail", "warehouse_list", "restock_create"):
                await _call(first, name, {}, error="forbidden")
        async with _session(url, ordinary) as legacy_acl:
            orders = await _call(legacy_acl, "store_order_list", {})
            assert any(row["order_no"] == "foreign-participating" and row["note"] == "internal-note"
                       for row in orders["orders"])
        assert _snapshot(env["root"]) == before_reads

    with _listener() as url, httpx.Client(trust_env=False, timeout=10) as http:
        asyncio.run(exercise(url))
        revoke = admin.post(f"{ADMIN}/{first_id}/revoke", data={
            "csrf_token": _hidden(admin.get(ADMIN), "csrf_token"),
        })
        assert revoke.status_code == 302
        assert http.post(url, json=INITIALIZE, headers=_headers(first_raw)).status_code == 401
        assert http.post(url, json=INITIALIZE, headers=_headers(second_raw)).status_code == 200
        assert _confirm(admin, preview).status_code == 409
    revoked = _db_update(master, "SELECT a.revoked_at, b.revoked_at, b.revoked_by FROM agent_tokens a "
                         "JOIN mcp_policy_bindings b ON b.token_id=a.id WHERE a.id=?", (first_id,))
    assert revoked[0] and revoked[0] == revoked[1] and revoked[2] == 1
    listing = admin.get(ADMIN).get_data(as_text=True)
    revoked_listing = re.search(fr'class="policy-binding" data-token-id="{first_id}".*?</td>', listing, re.S)[0]
    assert f'class="policy-revocation">撤销人 #1 · {revoked[0]}' in revoked_listing


def test_order_status_filter_requires_status_in_policy_mask(issued_policy, policy_admin, monkeypatch):
    from mcp_server.protocol import policy as dispatch

    env, _, full, _ = issued_policy
    _, admin = policy_admin
    assert _publish(admin, "order-number", {"orders.read": ["order_no"]}).status_code == 302
    restricted, _ = _read_receipt(_confirm(
        admin, _preview(admin, "order numbers only", "order-number:1", [STORES[1]]),
    ))
    called = []
    handler = dispatch._HANDLERS["store_order_list"]

    def track_query(policy, fields, **arguments):
        called.append((policy.token_id, arguments))
        return handler(policy, fields, **arguments)

    monkeypatch.setitem(dispatch._HANDLERS, "store_order_list", track_query)
    before = _snapshot(env["root"])

    async def exercise(url):
        async with _session(url, full) as full_session, _session(url, restricted) as limited:
            full_tool = next(t for t in (await full_session.list_tools()).tools if t.name == "store_order_list")
            assert "status" in full_tool.inputSchema["properties"]
            page = await _call(full_session, "store_order_list", {"status": "pending", "warehouse_code": STORES[1]})
            assert page["total"] == 1 and page["orders"][0]["status"] == "pending"

            limited_tools = (await limited.list_tools()).tools
            assert [tool.name for tool in limited_tools] == ["store_order_list"]
            assert set(limited_tools[0].inputSchema["properties"]) == {"warehouse_code", "limit", "offset"}
            page = await _call(limited, "store_order_list", {"warehouse_code": STORES[1]})
            assert page["orders"] == [{"order_no": "own-B"}]
            count_before = len(called)
            for status in ("pending", "delivered"):
                data = await _call(limited, "store_order_list", {"status": status}, error="validation_error")
                assert data == {"error": "validation_error", "message": "invalid_arguments"}
            assert len(called) == count_before
            assert "status" in next(
                tool for tool in (await full_session.list_tools()).tools if tool.name == "store_order_list"
            ).inputSchema["properties"]

    with _listener() as url:
        asyncio.run(exercise(url))
    assert _snapshot(env["root"]) == before


def test_registered_policy_actions_match_read_adapters():
    from mcp_server.protocol import policy as dispatch

    expected = {
        "items_detail": "inventory.read", "store_order_list": "orders.read",
        "production_consumption": PRODUCTION_CONSUMPTION_READ,
    }
    assert get_policy_actions() == expected
    assert {entry.name: entry.mode for entry in REGISTRY if entry.policy_action} == dict.fromkeys(expected, "read")
    assert dispatch._HANDLERS.keys() == dispatch._INPUT_SCHEMAS.keys() == expected.keys()


PRODUCTION_TOOL = "production_consumption"
PRODUCTION_CODES = ("wh_002", "wh_003", "wh_004", "wh_006")
PRODUCTION_ARGS = {"start_date": "2026-11-16", "end_date": "2026-11-23", "view": "day"}
PRODUCTION_SHAPE = next(entry.output_shape for entry in REGISTRY if entry.name == PRODUCTION_TOOL)


@pytest.fixture
def production_stores(storefronts, policy_admin):
    env, paths = storefronts
    _, admin = policy_admin
    for index, (old, code) in enumerate(zip(STORES, PRODUCTION_CODES, strict=True)):
        _db_update(env["master"], "UPDATE warehouses SET code=? WHERE code=?", (code, old))
        path = paths[code] = paths[old]
        with closing(sqlite3.connect(path)) as conn, conn:
            # Same current label/identity across stores, but no historical unit or mapping.
            conn.execute("UPDATE items SET sku='shared', canonical_id=101, unit=?", ("kg" if index == 0 else "g",))
            conn.execute(
                "INSERT INTO production_runs (id, product_id, output_qty, note, created_by, created_at) "
                "VALUES (1, 1, 10, 'private run note', 'private creator', '2026-11-16 00:00:00')"
            )
            conn.execute(
                "INSERT INTO production_run_items (id, run_id, item_id, planned_qty, actual_qty, planned_default) "
                "VALUES (1, 1, 1, 4, ?, 0)", (0 if index == 0 else 4,),
            )
    path = paths[PRODUCTION_CODES[0]]
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executemany(
            "INSERT INTO production_runs (id, product_id, output_qty, rolled_back, created_at) VALUES (?, 1, 10, ?, ?)",
            [
                (2, 0, "2026-11-15 23:59:59"), (3, 0, "2026-11-16 23:59:59"),
                (4, 0, "2026-11-17 00:00:00"), (5, 1, "2026-11-17 00:00:01"),
                (6, 0, "2026-11-22 23:59:59"), (7, 0, "2026-11-23 00:00:00"),
            ],
        )
        conn.executemany(
            "INSERT INTO outbound_requests "
            "(id, item_id, requested_quantity, reason, status, rolled_back, created_at, source_type) "
            "VALUES (?, ?, 4, ?, '出库', ?, '2026-11-17 00:00:00', ?)",
            [
                (41, 1, "生产领料(run=#4)", 0, "production"),
                (42, 2, "生产领料(run=#4)", 0, "production"),  # different material
                (43, 1, "生产领料(run=#3)", 0, "production"),  # different batch
                (44, 1, "生产领料(run=#4)", 1, "production"),  # rolled back
                (45, 1, "生产领料(run=#4)", 0, "manual"),      # wrong source
                (46, 1, "生产领料(run=#4)", 0, "production"),  # reused link
                (47, 1, "生产领料(run=#6)", 0, "production"),  # no structured link
            ],
        )
        conn.executemany(
            "INSERT INTO production_run_items "
            "(id, run_id, item_id, planned_qty, actual_qty, planned_default, outbound_req_id) "
            "VALUES (?, ?, 1, 4, ?, ?, ?)",
            [
                (2, 2, 4, 0, None), (3, 3, 4, 0, None),
                (4, 4, 4, 1, 41), (5, 5, 4, 0, None), (6, 6, 4, 1, None), (7, 7, 4, 0, None),
                (8, 4, 4, 1, 42), (9, 4, 4, 1, 43), (10, 4, 4, 1, 44), (11, 4, 4, 1, 45),
                (12, 4, 4, 1, 46), (13, 4, 4, 1, 46), (14, 4, 5, 1, 99),
                (15, 4, 5, 1, 48), (16, 4, 4, 1, 49),
            ],
        )
        conn.executemany(
            "INSERT INTO outbound_requests "
            "(id, item_id, requested_quantity, reason, status, rolled_back, created_at, source_type) "
            "VALUES (?, 1, 5, '生产领料(run=#4)', '出库', 0, '2026-11-17 00:00:00', 'production')",
            [(48,), (49,)],
        )
        conn.execute(
            "INSERT INTO stock_movements (item_id, action, delta, note, created_at) "
            "VALUES (1, '生产消耗', -4, 'private movement', '2026-11-17 00:00:00')"
        )
    # An earlier schema must remain readable without a migration or backfill.
    path = paths["wh_006"]
    _db_update(path, "DROP INDEX IF EXISTS idx_pruni_outbound")
    _db_update(path, "ALTER TABLE production_run_items DROP COLUMN outbound_req_id")
    _db_update(path, "ALTER TABLE production_run_items DROP COLUMN planned_default")
    return env, paths, admin


@pytest.fixture
def production_tokens(production_stores):
    env, paths, admin = production_stores
    action = PRODUCTION_CONSUMPTION_READ
    assert _publish(admin, "production", {action: ["summary", "lines"]}).status_code == 302
    preview = _preview(admin, "four production stores", "production:1", PRODUCTION_CODES)
    assert preview.status_code == 200
    assert all(value in preview.get_data(as_text=True) for value in (PRODUCTION_TOOL, action, *PRODUCTION_CODES))
    full, identity = _read_receipt(_confirm(admin, preview))
    assert _db_update(
        env["master"], "SELECT allowed_read_paths_json, allowed_write_paths_json, "
        "allowed_warehouse_codes_json FROM agent_tokens WHERE id=?", (identity,),
    ) == ("[]", "[]", "[]")
    assert _publish(admin, "production", {action: ["summary"]}).status_code == 302
    partial, _ = _read_receipt(_confirm(admin, _preview(admin, "one store", "production:2", ["wh_003"])))
    return env, paths, admin, full, partial, identity


def test_production_consumption_sdk(production_tokens, monkeypatch):
    env, paths, admin, full, partial, identity = production_tokens
    ordinary = secrets.token_urlsafe(32)
    _db_update(
        env["master"], "INSERT INTO agent_tokens "
        "(name, token_hash, token_prefix, created_by, created_at, allowed_read_paths_json, "
        "allowed_write_paths_json, allowed_warehouse_codes_json) "
        "VALUES ('synthetic-wide', ?, ?, 1, 'test', '[\"*\"]', '[\"*\"]', '[\"*\"]')",
        (generate_password_hash(ordinary, method="pbkdf2:sha256:1000"), ordinary[:8]),
    )
    assert authenticate(f"Bearer {full}") is None
    assert not next(entry for entry in REGISTRY if entry.name == PRODUCTION_TOOL).bot_approved
    with closing(sqlite3.connect(paths["wh_002"])) as writer, writer:
        assert writer.execute("PRAGMA journal_mode=PERSIST").fetchone()[0] == "persist"
        writer.execute("UPDATE items SET unit='box' WHERE id=1")
        writer.commit()
    before = _snapshot(env["root"])
    assert any(name.endswith("-journal") for name in before)

    async def exercise(url):
        async with (_session(url, full) as session, _session(url, partial) as narrow,
                    _session(url, ordinary) as legacy):
            assert [tool.name for tool in (await session.list_tools()).tools] == [PRODUCTION_TOOL]
            assert PRODUCTION_TOOL not in {tool.name for tool in (await legacy.list_tools()).tools}
            legacy_call = await legacy.call_tool(PRODUCTION_TOOL, PRODUCTION_ARGS)
            assert legacy_call.isError
            original_open = sqlite3.connect

            def no_warehouse_open(database, *args, **kwargs):
                assert all(path.name.split(" ")[0] not in str(database) for path in paths.values()), database
                return original_open(database, *args, **kwargs)

            with monkeypatch.context() as guarded:
                guarded.setattr(sqlite3, "connect", no_warehouse_open)
                await _call(narrow, PRODUCTION_TOOL, {**PRODUCTION_ARGS, "warehouse_code": "wh_002"}, error="forbidden")
                await _call(session, PRODUCTION_TOOL, {**PRODUCTION_ARGS, "warehouse_code": OUTSIDE}, error="forbidden")
                await _call(narrow, PRODUCTION_TOOL, {**PRODUCTION_ARGS, "view": "lines"}, error="forbidden")
                await _call(session, "outbound_create", {}, error="forbidden")
            page = await _call(narrow, PRODUCTION_TOOL, PRODUCTION_ARGS)
            assert page["warehouses"] == ["wh_003"] and len(page["summary"]) == 1

            day = await _call(session, PRODUCTION_TOOL, PRODUCTION_ARGS)
            week = await _call(session, PRODUCTION_TOOL, {**PRODUCTION_ARGS, "view": "week"})
            for page in (day, week):
                validate(page, PRODUCTION_SHAPE)
                assert page["warehouses"] == list(PRODUCTION_CODES)
                assert page["timezone"] == "Asia/Shanghai"
                assert page["unit_status"] == "historical_unit_unavailable"
                assert page["cross_store_rollup"] == "historical_mapping_unavailable"
                assert all(g["unit"] is None for g in page["summary"])
                assert all(g[key] is None for key in ("planned_qty", "manual_qty", "system_default_qty", "unknown_qty")
                           for g in page["summary"])
            assert len(week["summary"]) == 4
            counts = ("planned_count", "manual_count", "system_default_count", "unknown_count")
            for group in week["summary"]:
                assert group["period_start"] == "2026-11-16"
                assert all(group[key] == sum(row[key] for row in day["summary"]
                                            if row["warehouse_code"] == group["warehouse_code"])
                           for key in counts)
            assert [week["summary"][0][key] for key in counts] == [13, 2, 1, 10]
            assert [week["summary"][-1][key] for key in counts] == [1, 0, 0, 1]
            assert [(group["period_start"], group["planned_count"]) for group in day["summary"][:3]] == [
                ("2026-11-16", 2), ("2026-11-17", 10), ("2026-11-22", 1),
            ]

            line_args = {**PRODUCTION_ARGS, "view": "lines", "warehouse_code": "wh_002"}
            lines = await _call(session, PRODUCTION_TOOL, line_args)
            validate(lines, PRODUCTION_SHAPE)
            records = {row["line_id"]: row for row in lines["lines"]}
            assert list(records) == [1, 3, 4, 8, 9, 10, 11, 12, 13, 14, 15, 16, 6]
            assert records[1]["created_at"] == "2026-11-16T00:00:00+08:00"
            assert records[1]["booked_qty"] == 0 and records[1]["booked_source"] == "manual"
            assert records[3]["planned_qty"] == records[3]["booked_qty"] == 4
            assert records[3]["booked_source"] == "manual"
            assert records[4]["planned_qty"] == records[4]["booked_qty"] == 4
            assert records[4]["booked_source"] == "system_default"
            assert all(records[i]["booked_source"] == "unknown" for i in (6, 8, 9, 10, 11, 12, 13, 14, 15, 16))
            assert (records[15]["planned_qty"], records[15]["booked_qty"]) == (4, 5)
            assert (records[16]["planned_qty"], records[16]["booked_qty"]) == (4, 4)
            assert records[14]["booked_qty"] == 5
            assert all(set(row) == {
                "warehouse_code", "item_id", "unit", "run_id", "line_id", "created_at",
                "planned_qty", "booked_qty", "booked_source",
            } for row in records.values())
            sample = day["summary"][0]["sample"]
            linked = await _call(session, PRODUCTION_TOOL, {
                **line_args, "run_id": sample["run_id"], "line_id": sample["line_id"],
            })
            assert linked["lines"] == [records[1]]
            first_lines = await _call(session, PRODUCTION_TOOL, {**line_args, "limit": 4})
            next_lines = await _call(session, PRODUCTION_TOOL, {
                **line_args, "limit": 4, "cursor": first_lines["next_cursor"],
            })
            assert first_lines["lines"] + next_lines["lines"] == lines["lines"][:8]
            next_week = await _call(session, PRODUCTION_TOOL, {
                **line_args, "start_date": "2026-11-23", "end_date": "2026-11-30",
            })
            assert [row["line_id"] for row in next_week["lines"]] == [7]
            empty = await _call(session, PRODUCTION_TOOL, {**line_args, "run_id": 5})
            assert empty["range_empty"] and empty["page_empty"] and not empty["truncated"]
            assert empty["lines"] == [] and empty["next_cursor"] is None
            for changes in (
                {"start_date": "2026-11-17", "view": "week"}, {"start_date": "2026-11-23"},
                {"start_date": "20261116"}, {"end_date": "2027-03-01"}, {"limit": 201}, {"run_id": 1},
            ):
                await _call(session, PRODUCTION_TOOL, {**PRODUCTION_ARGS, **changes}, error="validation_error")

            page = await _call(session, PRODUCTION_TOOL, {**PRODUCTION_ARGS, "limit": 1})
            assert page["truncated"] and page["summary"] == day["summary"][:1]
            first_cursor = page["next_cursor"]
            await _call(session, PRODUCTION_TOOL, {
                **PRODUCTION_ARGS, "limit": 1, "cursor": "bad" + first_cursor,
            }, error="validation_error")
            for changes in ({"limit": 2}, {"warehouse_code": "wh_002"}, {"start_date": "2026-11-15"}):
                await _call(session, PRODUCTION_TOOL, {
                    **PRODUCTION_ARGS, "limit": 1, "cursor": first_cursor, **changes,
                }, error="validation_error")
            await _call(narrow, PRODUCTION_TOOL, {
                **PRODUCTION_ARGS, "limit": 1, "cursor": first_cursor,
            }, error="validation_error")
            collected = page["summary"].copy()
            while page["truncated"]:
                page = await _call(session, PRODUCTION_TOOL, {
                    **PRODUCTION_ARGS, "limit": 1, "cursor": page["next_cursor"],
                })
                collected.extend(page["summary"])
            assert collected == day["summary"] and page["next_cursor"] is None and not page["page_empty"]

    with _listener() as url, httpx.Client(trust_env=False, timeout=10) as http:
        asyncio.run(exercise(url))
        assert _snapshot(env["root"]) == before
        audit = [json.loads(line) for line in env["log"].read_text().splitlines()]
        assert {row["status"] for row in audit if row["tool_name"] == PRODUCTION_TOOL} >= {200, 400, 403}
        assert not any(raw in env["log"].read_text() for raw in (full, partial, ordinary))
        for endpoint in ("/sse", "/messages/"):
            assert http.post(url.replace("/api/mcp/", endpoint), headers=_headers(full), json=INITIALIZE).status_code == 401
        assert admin.post(f"{ADMIN}/{identity}/revoke", data={
            "csrf_token": _hidden(admin.get(ADMIN), "csrf_token"),
        }).status_code == 302
        assert http.post(url, headers=_headers(full), json=INITIALIZE).status_code == 401
        assert http.post(url, headers=_headers(partial), json=INITIALIZE).status_code == 200


def test_production_consumption_scope_and_source_fail_closed(production_tokens, monkeypatch):
    env, paths, admin, full, _, _ = production_tokens
    app = admin.application
    member = _login(app, "member")
    assert _publish(member, "forbidden", {PRODUCTION_CONSUMPTION_READ: ["lines"]}).status_code == 403
    assert _publish(admin, "too-much", {PRODUCTION_CONSUMPTION_READ: ["unit_cost"]}).status_code == 400
    assert _preview(admin, "outside", "production:1", ["wh_002", OUTSIDE]).status_code == 400
    assert _preview(admin, "dc", "production:1", ["wh_002", DC]).status_code == 400
    preview = _preview(admin, "role changed", "production:1", ["wh_002"])
    master = env["master"]
    _db_update(master, "UPDATE warehouses SET warehouse_type='distribution_center' WHERE code='wh_002'")
    assert _confirm(admin, preview).status_code == 409
    with _listener() as url, httpx.Client(trust_env=False, timeout=10) as http:
        assert http.post(url, headers=_headers(full), json=INITIALIZE).status_code == 401
    _db_update(master, "UPDATE warehouses SET warehouse_type='storefront' WHERE code='wh_002'")

    async def exercise(url):
        args = {**PRODUCTION_ARGS, "limit": 1}
        path = paths["wh_002"]
        async with _session(url, full) as session:
            first = await _call(session, PRODUCTION_TOOL, args)
            more = {**args, "cursor": first["next_cursor"]}
            _db_update(path, "UPDATE production_run_items SET actual_qty=1 WHERE id=1")
            before = _snapshot(env["root"])
            await _call(session, PRODUCTION_TOOL, more, error="stale_cursor")
            assert _snapshot(env["root"]) == before
            with monkeypatch.context() as budget:
                budget.setattr("mcp_server.service.production_consumption.MAX_SOURCE_LINES", 1)
                await _call(session, PRODUCTION_TOOL, args, error="range_too_dense")
            _db_update(path, "UPDATE production_runs SET created_at='2026-11-16T00:00:00Z' WHERE id=1")
            await _call(session, PRODUCTION_TOOL, args, error="source_unavailable")
            _db_update(path, "UPDATE production_runs SET created_at='2026-11-16 00:00:00' WHERE id=1")
            with closing(sqlite3.connect(path)) as writer, writer:
                assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
                writer.execute("UPDATE production_run_items SET actual_qty=3 WHERE id=1")
                writer.commit()
                before = _snapshot(env["root"])
                assert any(name.endswith("-wal") for name in before)
                assert any(name.endswith("-shm") for name in before)
                await _call(session, PRODUCTION_TOOL, args, error="source_unavailable")
                assert _snapshot(env["root"]) == before
            path.unlink()
            before = _snapshot(env["root"])
            await _call(session, PRODUCTION_TOOL, args, error="source_unavailable")
            assert _snapshot(env["root"]) == before

    with _listener() as url:
        asyncio.run(exercise(url))


def test_production_consumption_rejects_physical_alias_before_read(production_tokens, monkeypatch):
    env, paths, _, full, partial, _ = production_tokens
    configured, other = paths["wh_002"], paths["wh_003"]
    target = env["root"] / "external-warehouse.db"
    configured.rename(target)
    configured.symlink_to(target)

    async def exercise(url):
        async with _session(url, full) as session, _session(url, partial) as narrow:
            args = {**PRODUCTION_ARGS, "view": "lines", "warehouse_code": "wh_002"}
            before = _snapshot(env["root"])
            page = await _call(session, PRODUCTION_TOOL, args)
            assert page["lines"][0]["booked_qty"] == 0
            assert _snapshot(env["root"]) == before

            other.rename(other.with_suffix(".original"))
            os.link(target, other)
            assert other.resolve() != target.resolve()
            assert other.samefile(configured)
            before = _snapshot(env["root"])
            original_open = sqlite3.connect

            def master_only(database, *args, **kwargs):
                assert env["master"].as_uri() in str(database), database
                return original_open(database, *args, **kwargs)

            with monkeypatch.context() as guarded:
                guarded.setattr(sqlite3, "connect", master_only)
                await _call(narrow, PRODUCTION_TOOL, PRODUCTION_ARGS, error="source_unavailable")
                await _call(session, PRODUCTION_TOOL, args, error="source_unavailable")
                await _call(session, PRODUCTION_TOOL, PRODUCTION_ARGS, error="source_unavailable")
            assert _snapshot(env["root"]) == before
            unaffected = await _call(session, PRODUCTION_TOOL, {
                **PRODUCTION_ARGS, "warehouse_code": "wh_004",
            })
            assert unaffected["summary"][0]["warehouse_code"] == "wh_004"

    with _listener() as url:
        asyncio.run(exercise(url))


def test_policy_dry_run_revoke_refuses_without_changes(issued_policy, policy_admin, monkeypatch):
    env, _, raw, token_id = issued_policy
    app, admin = policy_admin
    csrf = _hidden(admin.get(ADMIN), "csrf_token")
    before = _snapshot(env["root"])

    async def usable(url):
        async with _session(url, raw) as session:
            assert await _call(session, "items_detail", ITEM_ARGS)

    with _listener() as url, httpx.Client(trust_env=False, timeout=10) as http:
        app.config["DRY_RUN"] = True
        try:
            response = admin.post(f"{ADMIN}/{token_id}/revoke", data={"csrf_token": csrf})
            assert response.status_code == 409 and "演练模式未撤销策略令牌" in response.get_data(as_text=True)
            assert _snapshot(env["root"]) == before
            asyncio.run(usable(url))
        finally:
            app.config["DRY_RUN"] = False
        with monkeypatch.context() as absent_write:
            absent_write.setattr("blueprints.agent_tokens.revoke_policy_token", lambda *_args: False)
            response = admin.post(f"{ADMIN}/{token_id}/revoke", data={"csrf_token": csrf})
            assert response.status_code == 409 and "未撤销" in response.get_data(as_text=True)
        assert _snapshot(env["root"]) == before
        assert admin.post(f"{ADMIN}/{token_id}/revoke", data={"csrf_token": csrf}).status_code == 302
        assert http.post(url, json=INITIALIZE, headers=_headers(raw)).status_code == 401
    agent_at, binding_at, revoker = _db_update(
        env["master"], "SELECT a.revoked_at, b.revoked_at, b.revoked_by "
        "FROM agent_tokens a JOIN mcp_policy_bindings b ON b.token_id=a.id WHERE a.id=?", (token_id,),
    )
    assert agent_at and binding_at == agent_at and revoker == 1


def test_policy_admission_fails_closed_for_legacy_grants_invalid_scopes_and_unavailable_sources(issued_policy, monkeypatch):
    env, paths, raw, identity = issued_policy
    master = env["master"]
    assert authenticate(f"Bearer {raw}") is None
    monkeypatch.setenv("DAILYCHECK_MCP_TOKEN", raw)
    with _listener() as url, httpx.Client(trust_env=False, timeout=10) as http:
        def check(expected=401, token=raw, target=url):
            before = _snapshot(env["root"])
            assert http.post(target, json=INITIALIZE, headers=_headers(token)).status_code == expected
            assert _snapshot(env["root"]) == before

        check(200)
        check(token=raw[:-1])
        check(token=None)
        for path in ("/sse", "/messages/", "/health", "/api/mcp"):
            check(target=url.replace("/api/mcp/", path))
        duplicate_auth = http.post(url, json=INITIALIZE, headers=[
            *list(_headers(raw).items()), ("Authorization", "Bearer synthetic-shared-secret"),
        ])
        assert duplicate_auth.status_code == 401
        for column in ("allowed_read_paths_json", "allowed_write_paths_json", "allowed_warehouse_codes_json"):
            _db_update(master, f"UPDATE agent_tokens SET {column}='[\"*\"]' WHERE id=?", (identity,))
            check()
            _db_update(master, f"UPDATE agent_tokens SET {column}='[]' WHERE id=?", (identity,))
        _db_update(master, "UPDATE warehouses SET warehouse_type='distribution_center' WHERE code=?", (STORES[1],))
        check()
        _db_update(master, "UPDATE warehouses SET warehouse_type='storefront' WHERE code=?", (STORES[1],))
        _db_update(master, "UPDATE mcp_policy_presets SET disabled_by=1, disabled_at='test'")
        check()
        _db_update(master, "UPDATE mcp_policy_presets SET disabled_by=NULL, disabled_at=NULL")
        check(200)

        path = paths[STORES[0]]
        async def check_inventory():
            async with _session(url, raw) as session:
                await _call(session, "items_detail", ITEM_ARGS, error="source_unavailable")

        with closing(sqlite3.connect(path)) as writer, writer:
            assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
            writer.execute("UPDATE items SET quantity = 30 WHERE id = 1")
            writer.commit()
            before = _snapshot(env["root"])
            asyncio.run(check_inventory())
            assert _snapshot(env["root"]) == before
        before = _snapshot(env["root"])
        asyncio.run(check_inventory())
        assert _snapshot(env["root"]) == before

        _db_update(master, "DROP TRIGGER mcp_policy_binding_immutable")
        _db_update(master, "UPDATE mcp_policy_bindings SET warehouse_codes_json='[]' WHERE token_id=?", (identity,))
        check()
        _db_update(master, "DROP TABLE mcp_policy_bindings")
        check()


def test_v8_upgrade_and_binding_triggers_preserve_legacy_metadata(mcp_test_env):
    from migrations import MIGRATIONS
    from migrations_runner import migrate_up

    env = mcp_test_env
    master = env["master"]
    with closing(sqlite3.connect(master)) as conn, conn:
        conn.executescript("DROP TABLE mcp_policy_bindings; DROP TABLE mcp_policy_presets; PRAGMA user_version=8;")
        conn.execute(
            "INSERT INTO agent_tokens (name, token_hash, token_prefix, created_by, created_at, "
            "allowed_read_paths_json, allowed_write_paths_json, allowed_warehouse_codes_json) "
            "VALUES ('historical', 'existing-hash', NULL, 1, 'test', '[\"*\"]', '[\"*\"]', 'null')",
        )
    metadata = _db_update(master, "SELECT * FROM agent_tokens WHERE name='historical'")
    assert migrate_up(master, MIGRATIONS, env["root"] / "migration-backup") == [9]
    assert _db_update(master, "PRAGMA user_version") == (9,)
    assert _db_update(master, "SELECT * FROM agent_tokens WHERE name='historical'") == metadata
    assert migrate_up(master, MIGRATIONS, env["root"] / "migration-backup") == []
    original_schema = _snapshot(env["root"])
    with policy_read_connection(master) as connection:
        for sql in ("UPDATE agent_tokens SET revoked_at='foo'", "CREATE TEMP TABLE scratch (id)",
                    "PRAGMA query_only=OFF", "ATTACH DATABASE ':memory:' AS writer"):
            with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
                connection.execute(sql)
    assert _snapshot(env["root"]) == original_schema


def test_version_and_binding_snapshots_cannot_be_updated(issued_policy):
    env, _, _, identity = issued_policy
    before = _snapshot(env["root"])
    with pytest.raises(sqlite3.IntegrityError, match="binding.*immutable"):
        _db_update(env["master"], "UPDATE mcp_policy_bindings SET preset_version=2 WHERE token_id=?", (identity,))
    with pytest.raises(sqlite3.IntegrityError, match="versions.*immutable"):
        _db_update(env["master"], "UPDATE mcp_policy_presets SET grants_json='{}'")
    assert _snapshot(env["root"]) == before
