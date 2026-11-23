"""Issue #39 policy presets through real admin login and the StreamableHTTP entry point."""
from __future__ import annotations

import asyncio
import json
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
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from werkzeug.datastructures import MultiDict
from werkzeug.security import check_password_hash, generate_password_hash

from mcp_server import main
from mcp_server.data.unit_of_work import policy_read_connection
from mcp_server.protocol.registry import REGISTRY, get_policy_actions
from mcp_server.service.auth import authenticate
from mcp_server.service.policy import POLICY_TOKEN_PREFIX, SAFE_FIELDS

STORES = ("store_a", "store_b", "store_c", "store_d")
OUTSIDE = "other_store"
DC = "distribution"
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
    assert _publish(admin, "stock-and-orders", SAFE_FIELDS).status_code == 302
    raw, identity = _read_receipt(_confirm(admin, _preview(admin, "four stores", "stock-and-orders:1", STORES)))
    return env, paths, raw, identity


def test_two_issued_preset_versions_share_the_existing_route_and_legacy_catalog(storefronts, policy_admin, monkeypatch):
    env, paths = storefronts
    master = env["master"]
    app, admin = policy_admin
    member = _login(app, "member")
    other_admin = _login(app, "another-admin")
    other_session = _login(app)
    assert _publish(member, "stock-and-orders", SAFE_FIELDS).status_code == 403
    assert _publish(admin, "stock-and-orders", {"inventory.read": ["unit_cost"]}).status_code == 400
    assert _publish(admin, "stock-and-orders", {}).status_code == 400
    assert _publish(admin, "stock-and-orders", SAFE_FIELDS).status_code == 302
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
            assert {t.name for t in (await legacy.list_tools()).tools} == {entry.name for entry in REGISTRY if not entry.policy_only}
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
        "routing_item_status": "routing.read", "routing_pause_item": "routing.pause",
        "routing_restore_item": "routing.restore",
    }
    assert get_policy_actions() == expected
    assert {entry.name: entry.mode for entry in REGISTRY if entry.policy_action} == {
        name: "write" if action in {"routing.pause", "routing.restore"} else "read"
        for name, action in expected.items()
    }
    assert dispatch._HANDLERS.keys() == dispatch._INPUT_SCHEMAS.keys() == expected.keys()


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
    assert migrate_up(master, MIGRATIONS, env["root"] / "migration-backup") == [9, 10]
    assert _db_update(master, "PRAGMA user_version") == (10,)
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
