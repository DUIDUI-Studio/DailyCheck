"""Gene stockout acceptance against separate synthetic Web and MCP loopback processes."""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from jsonschema import validate
from werkzeug.security import generate_password_hash

import routing_writer_fence as writer
from mcp_server.protocol.registry import REGISTRY
from mcp_server.service.policy import ROUTING_FIELDS, encode_grants
from mcp_server.service.routing_approvals import FIXED_PRESET
from migrations import MIGRATIONS
from migrations.routing_stockout import ROUTING_DDL
from migrations_runner import migrate_down, migrate_up
from scripts.check_routing_web_rollback import main as check_rollback
from tests import multi_warehouse_env
from tests.mcp_server.test_protocol.test_policy_presets_http import (
    INITIALIZE,
    _call,
    _headers,
    _session,
)

TARGET = {"canonical_id": 201, "warehouse_code": "dc_alpha"}
READ, PAUSE, RESTORE = "routing_item_status", "routing_pause_item", "routing_restore_item"
CART = "/store-ordering/cart"
CATALOG = "/store-ordering/catalog"
TEST_PASSWORD = "synthetic-store-only"
WEB_PROGRAM = """
import sys
import time
from pathlib import Path
from werkzeug.serving import make_server
from app import app
from blueprints import audit_pure
from blueprints import store_ordering_pure as ordering

gate = Path(sys.argv[1])
audit_pure.ADMIN_AUDIT_LOG = gate / "admin_audit.log"
original = ordering._resolve_routed_cart_lines
def resolve(*args):
    if (gate / 'arm').exists():
        (gate / 'submit_inside').touch()
        for _ in range(500):
            if (gate / 'submit_continue').exists():
                break
            time.sleep(.01)
        else:
            raise RuntimeError('fixture did not release submit')
    return original(*args)
ordering._resolve_routed_cart_lines = resolve
make_server('127.0.0.1', 0, app, threaded=True, fd=int(sys.argv[2])).serve_forever()
"""


def _seed(db_dir, monkeypatch, *, approvals=True):
    env = multi_warehouse_env.build(db_dir, monkeypatch)
    master = env["master_path"]
    credentials = {}
    with closing(sqlite3.connect(master)) as conn, conn:
        pw = generate_password_hash(TEST_PASSWORD, method="pbkdf2:sha256:1000")
        conn.execute(
            "UPDATE users SET password_hash=?, last_warehouse_id=3, is_admin=(id=2) WHERE id IN (1, 2)",
            (pw,),
        )
        subjects = [("legacy", 2)]
        if approvals:
            conn.execute("INSERT INTO mcp_policy_routing_principals VALUES ('fixture-principal', 1, 3, NULL)")
            conn.executemany(
                "INSERT INTO mcp_policy_routing_approvals VALUES (?, 'fixture-principal', ?, ?, NULL)",
                [("approval-a", 201, "dc_alpha"), ("approval-g", 207, "dc_beta")],
            )
            subjects += [("gene", 3), ("non_gene", 2), ("read_subset", 3)]
        for name, actor in subjects:
            routing = name != "legacy"
            token = ("mp." if routing else "") + secrets.token_urlsafe(32)
            credentials[name] = token
            token_id = conn.execute(
                "INSERT INTO agent_tokens (name, token_hash, token_prefix, created_by, created_at, "
                "allowed_read_paths_json, allowed_warehouse_codes_json) VALUES (?, ?, ?, 2, 'test', ?, ?)",
                (name, generate_password_hash(token, method="pbkdf2:sha256:1000"), token[:8],
                 "[]" if routing else '["/api/v1/warehouses"]', "[]" if routing else '["dc_alpha"]'),
            ).lastrowid
            if routing:
                grant = encode_grants(
                    {"routing.read": ["canonical"]} if name == "read_subset" else
                    {key: list(value) for key, value in ROUTING_FIELDS.items()}, routing=True,
                )
                conn.execute("INSERT INTO mcp_policy_presets VALUES (?, 1, ?, 2, 'test', NULL, NULL)", (name, grant))
                conn.execute(
                    "INSERT INTO mcp_policy_bindings VALUES (?, ?, 1, ?, '[]', ?, 2, 'test', NULL, NULL)",
                    (token_id, name, grant, "synthetic-" + name),
                )
                conn.execute(
                    "INSERT INTO mcp_policy_routing_scopes VALUES (?, 1, ?, ?)",
                    (token_id, actor, '[[201,"dc_alpha","approval-a"],[207,"dc_beta","approval-g"]]'),
                )
        if approvals:
            conn.execute("PRAGMA user_version=10")
    if not approvals:
        assert migrate_up(master, MIGRATIONS, db_dir.parent / "master-bootstrap-backups")[-1] == 10
    return env, credentials


def _child_env(db_dir, tmp_dir, probe_key, *, mode):
    # Construct a minimal environment so ambient service credentials and proxies never enter the lab.
    return {
        "PATH": os.defpath,
        "TMPDIR": str(tmp_dir),
        "DAILYCHECK_DB_DIR": str(db_dir),
        "DAILYCHECK_SECRET_KEY": secrets.token_urlsafe(32),
        "DAILYCHECK_MULTI_WAREHOUSE": "1" if mode else "0",
        "DAILYCHECK_ROUTING_PROBE_SECRET": probe_key,
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


@contextmanager
def _process(args, env, *, headers=None):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        reservation.listen()
        port = reservation.getsockname()[1]
        fd = reservation.fileno()
        process = subprocess.Popen([sys.executable, *args, str(fd)], env=env, pass_fds=(fd,))
    base = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(trust_env=False, timeout=1) as probe:
            for _ in range(150):
                assert process.poll() is None, "loopback process exited during startup"
                try:
                    if probe.get(base + "/health", headers=headers).status_code == 200:
                        break
                except httpx.TransportError:
                    time.sleep(.03)
            else:
                pytest.fail("loopback process did not start")
        yield base
    finally:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            raise


@contextmanager
def _services(root, tokens, *, web_mode=True, mcp_mode=True, mcp_dry=False, web_dry=False,
              valid_key=True, web_db=None):
    key = secrets.token_urlsafe(32)
    web_env = _child_env(web_db or root / "db", root, key, mode=web_mode)
    mcp_env = _child_env(root / "db", root, key if valid_key else "", mode=mcp_mode)
    if mcp_dry:
        mcp_env["DRY_RUN"] = "true"
    if web_dry:
        web_env["DRY_RUN"] = "true"
    with _process(["-c", WEB_PROGRAM, str(root)], web_env) as web:
        mcp_env["DAILYCHECK_ROUTING_WEB_PORT"] = web.rsplit(":", 1)[1]
        with _process(["-m", "uvicorn", "mcp_server.main:_build_app", "--factory", "--fd"],
                      mcp_env, headers=_headers(tokens["legacy"])) as mcp:
            yield web, mcp


def _csrf(html):
    return re.search(r'name="csrf_token"[^>]+value="([^"]+)"', html).group(1)


@contextmanager
def _login(web, name):
    with httpx.Client(base_url=web, follow_redirects=True, trust_env=False, timeout=10) as browser:
        login = browser.get("/login")
        response = browser.post("/login", data={
            "username": name, "password": TEST_PASSWORD, "csrf_token": _csrf(login.text),
        })
        assert response.status_code == 200 and browser.cookies.get("session")
        yield browser


def _post(browser, path, csrf, **fields):
    return browser.post(path, data={"csrf_token": csrf, **fields})


def _add(browser, csrf, cid=201):
    return _post(browser, CART + "/add", csrf, canonical_id=cid, quantity="1", unit="件")


def _sql(path, query, args=()):
    with closing(sqlite3.connect(path)) as conn, conn:
        return conn.execute(query, args).fetchall()


def _arguments(snapshot, operation, *, key=None, **extra):
    status = snapshot["routing"]
    target = {**TARGET, **extra}
    if operation == RESTORE:
        target["pause_id"] = status["pause_id"] or "missing"
    key = key or secrets.token_hex(8)
    return {
        **target, "expected_version": status["version"], "idempotency_key": key,
        "evidence": {
            **target, "kind": "manual_fulfillment_confirmation", "reference": "CHECK-" + key,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "quantity": "0" if operation == PAUSE else "6.25", "unit": "件",
        },
    }


async def _rpc(mcp, token, name, args, *, error=None):
    async with _session(mcp + "/api/mcp/", token) as client:
        payload = await _call(client, name, args, error=error)
    if not error and name in {READ, PAUSE, RESTORE}:
        validate(payload, next(entry.output_shape for entry in REGISTRY if entry.name == name))
    return payload


def _run(mcp, token, name, args, *, error=None):
    return asyncio.run(_rpc(mcp, token, name, args, error=error))


@pytest.fixture
def lab(tmp_path, monkeypatch):
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    env, tokens = _seed(db_dir, monkeypatch)
    return tmp_path, env, tokens


def test_gene_storefront_stockout(lab):
    root, env, tokens = lab
    master = env["master_path"]
    wh_before = {p: p.read_bytes() for p in env["paths"].values()}
    canonical_before = _sql(master, "SELECT * FROM canonical_items")
    routes_before = _sql(master, "SELECT * FROM item_routing")
    expected = {"mcp_multi_warehouse": True, "web_multi_warehouse": True, "writes_enabled": True}
    with _services(root, tokens) as (web, mcp), _login(web, "store_staff") as store, _login(web, "alpha_mgr") as admin:
        gene = tokens["gene"]
        info = _run(mcp, gene, READ, TARGET)
        assert info["mode"] == expected and info["routing"]["orderable"]
        assert info["canonical"]["name"] == "品项A" and info["canonical"]["unit"] == "件"
        assert info["routing"]["effective_source"] == "dc_alpha"
        assert info["binding"]["items"][0]["sku"] == "DCA-A"
        assert set(info["canonical"]) == {"id", "name", "canonical_sku", "status", "unit",
                                          "gram_per_unit", "aux_unit", "aux_rate"}
        assert set(info["binding"]["items"][0]) == {"id", "sku", "unit", "is_active", "is_alias", "is_orderable"}
        assert list(_run(mcp, tokens["read_subset"], READ, TARGET)) == ["canonical"]
        second_pair = _run(mcp, gene, READ, {"canonical_id": 207, "warehouse_code": "dc_beta"})
        assert second_pair["routing"]["configured_source"] == "dc_beta"
        _run(mcp, tokens["read_subset"], PAUSE, {}, error="forbidden")
        _run(mcp, gene, READ, {**TARGET, "warehouse_code": "dc_beta"}, error="forbidden")
        _run(mcp, gene, READ, {**TARGET, "warehouse_code": "rd"}, error="forbidden")
        _run(mcp, gene, READ, {**TARGET, "canonical_id": 203}, error="forbidden")
        _run(mcp, gene, READ, {**TARGET, "fields": ["unit_cost"]}, error="validation_error")
        _run(mcp, gene, "canonical_detail", {"canonical_id": 201}, error="forbidden")
        _run(mcp, gene, "canonical_update", {"canonical_id": 201, "name": "forbidden"}, error="forbidden")
        _run(mcp, tokens["legacy"], PAUSE, {}, error="forbidden")
        assert [item["code"] for item in _run(mcp, tokens["legacy"], "warehouse_list", {})] == ["dc_alpha"]

        with httpx.Client(trust_env=False, timeout=5) as client:
            for path in ("/health", "/sse", "/messages/"):
                assert client.get(mcp + path, headers=_headers(gene)).status_code == 401
            assert client.post(mcp + "/api/mcp/", headers=_headers(tokens["non_gene"]),
                               json=INITIALIZE).status_code == 401
            forged = client.get(web + "/health", headers={
                "X-DailyCheck-Routing-Proof": "0" * 64, "X-DailyCheck-Routing-Nonce": "1" * 64,
                "X-Forwarded-For": "203.0.113.44",
            })
            assert forged.status_code == 403 and "snapshot" not in forged.json()

        _run(mcp, gene, RESTORE, _arguments(info, RESTORE), error="active_pause_required")
        _run(mcp, gene, PAUSE, _arguments(info, PAUSE, warehouse_code="dc_beta"), error="forbidden")
        _run(mcp, gene, PAUSE, _arguments(info, PAUSE, canonical_id=203), error="forbidden")
        _run(mcp, gene, PAUSE, {**_arguments(info, PAUSE), "new_warehouse_code": "dc_beta"}, error="validation_error")
        assert _sql(master, "SELECT COUNT(*) FROM item_routing_stockout_events") == [(0,)]
        catalog = store.get(CATALOG)
        assert "品项A" in catalog.text and "品项F" in catalog.text and "品项E" not in catalog.text
        csrf = _csrf(catalog.text)
        assert "已加入购物车" in _add(store, csrf).text
        assert "提交成功" in _post(store, CART + "/submit", csrf).text
        historical = _sql(master, "SELECT * FROM store_order_items")
        assert _sql(master, "SELECT source_warehouse_code, status FROM store_order_items") == [("dc_alpha", "pending")]
        _add(store, csrf)
        _add(store, csrf, 206)
        assert {r[0] for r in _sql(master, "SELECT canonical_id FROM store_order_cart_items")} == {201, 206}
        pause_args = _arguments(info, PAUSE)
        receipt = _run(mcp, gene, PAUSE, pause_args)
        assert receipt["warehouse_code"] == "dc_alpha" and receipt["actor_id"] == 3
        assert receipt == _run(mcp, gene, PAUSE, pause_args)
        _run(mcp, gene, PAUSE, {**pause_args, "expected_version": receipt["version"]}, error="idempotency_conflict")

        blocked = _run(mcp, gene, READ, TARGET)
        assert blocked["routing"] == {
            "configured_source": "dc_alpha", "effective_source": None, "orderable": False,
            "reason": "stockout_paused", "version": receipt["version"], "pause_id": receipt["pause_id"],
            "availability": "manual_recovery_confirmation_required", "existing_open_orders": 1,
        }
        assert check_rollback(["--db", str(master)]) == 1
        catalog = store.get(CATALOG).text
        assert "品项A" not in catalog and "品项F" in catalog
        assert "品项A 缺货停订" in _add(store, csrf).text
        batch = _post(store, CART + "/add-batch", csrf, **{"selected[]": "201", "qty[]": "1", "unit[]": "件"})
        assert "品项A 缺货停订" in batch.text
        assert "品项A" in _post(store, CART + "/submit", csrf).text
        assert _sql(master, "SELECT * FROM store_order_items") == historical
        quantities = _sql(master, "SELECT id, canonical_id, quantity FROM store_order_cart_items ORDER BY canonical_id")
        assert [(cid, qty) for _, cid, qty in quantities] == [(201, 1), (206, 1)]
        _post(store, CART + "/remove/" + str(quantities[0][0]), csrf)
        assert "提交成功" in _post(store, CART + "/submit", csrf).text
        assert _sql(master, "SELECT canonical_id FROM store_order_items ORDER BY id") == [(201,), (206,)]
        admin_page = admin.get("/admin/item-routing")
        admin_csrf = _csrf(admin_page.text)
        assert _post(admin, "/admin/agent-tokens/create", admin_csrf, policy_action="publish",
                     preset_name="unapproved-routing", **{"field_routing.pause": "receipt"}).status_code == 400
        assert _post(admin, "/admin/item-routing/201", admin_csrf, dc_code="dc_beta").status_code == 409
        assert _post(admin, "/admin/item-routing/201/delete", admin_csrf).status_code == 409
        baseline = {"dc_code": "dc_alpha", "updated_at": multi_warehouse_env.TS}
        assert admin.post("/admin/item-routing/batch", headers={"X-CSRFToken": admin_csrf}, json={
            "changes": [{"operation": "set", "canonical_id": 201, "dc_code": "dc_beta", "baseline": baseline}],
        }).status_code == 409
        assert _sql(master, "SELECT * FROM item_routing") == routes_before

        args = _arguments(blocked, RESTORE)
        for patch, error in [
            ({"kind": "book_quantity"}, "validation_error"),
            ({"warehouse_code": "dc_beta"}, "validation_error"),
            ({"canonical_id": 203}, "validation_error"),
            ({"unit": "kg"}, "validation_error"),
            ({"quantity": "0"}, "validation_error"),
            ({"pause_id": "wrong"}, "validation_error"),
            ({"observed_at": receipt["evidence"]["observed_at"]}, "validation_error"),
            ({"reference": receipt["evidence"]["reference"]}, "evidence_already_used"),
        ]:
            _run(mcp, gene, RESTORE, {**args, "evidence": {**args["evidence"], **patch}}, error=error)
        _run(mcp, gene, RESTORE, {**args, "pause_id": "wrong"}, error="active_pause_required")
        _run(mcp, gene, RESTORE, {**args, "expected_version": info["routing"]["version"]},
             error="routing_version_conflict")
        assert _sql(master, "SELECT operation FROM item_routing_stockout_events") == [("pause",)]
        assert _sql(env["paths"]["dc_alpha"], "SELECT quantity FROM items WHERE canonical_id=201") == [(100,)]
        restored = _run(mcp, gene, RESTORE, args)
        assert restored["pause_id"] == receipt["pause_id"] and restored["after_state"] == "open"
        assert restored == _run(mcp, gene, RESTORE, args)
        assert receipt == _run(mcp, gene, PAUSE, pause_args)
        assert check_rollback(["--db", str(master)]) == 0
        assert "品项A" in store.get(CATALOG).text
        _add(store, csrf)
        assert "提交成功" in _post(store, CART + "/submit", csrf).text
        assert _sql(master, "SELECT source_warehouse_code FROM store_order_items ORDER BY id") == [
            ("dc_alpha",), ("dc_alpha",), ("dc_alpha",),
        ]
        assert _sql(master, "SELECT * FROM item_routing") == routes_before
        assert _sql(master, "SELECT * FROM canonical_items") == canonical_before
        # Ignore the normal Web admin audit DB among warehouses. The original/DC stock and bindings are unchanged.
        assert env["paths"]["dc_alpha"].read_bytes() == wh_before[env["paths"]["dc_alpha"]]
    audit = [json.loads(line) for line in (root / "db" / "access.log").read_text().splitlines()]
    assert any(record.get("tool_name") == PAUSE and record["status"] == 200 for record in audit)


@pytest.mark.parametrize("options,web_on,mcp_on", [
    ({"web_mode": False, "mcp_mode": False}, False, False),
    ({"web_mode": False}, False, True),
    ({"mcp_mode": False}, True, False),
    ({"mcp_dry": True}, True, True),
    ({"web_dry": True}, True, True),
    ({"valid_key": False}, None, True),
])
def test_stockout_rejects_unverified_modes(lab, options, web_on, mcp_on):
    root, env, tokens = lab
    with _services(root, tokens, **options) as (_, mcp):
        info = _run(mcp, tokens["gene"], READ, TARGET)
        assert info["mode"] == {"mcp_multi_warehouse": mcp_on, "web_multi_warehouse": web_on, "writes_enabled": False}
        for op in (PAUSE, RESTORE):
            _run(mcp, tokens["gene"], op, _arguments(info, op), error="routing_mode_unavailable")
    assert _sql(env["master_path"], "SELECT COUNT(*) FROM item_routing_stockout_events") == [(0,)]


def test_submit_holds_master_through_route_resolution(lab):
    root, env, tokens = lab
    with _services(root, tokens) as (web, mcp), _login(web, "store_staff") as store:
        csrf = _csrf(store.get(CATALOG).text)
        info = _run(mcp, tokens["gene"], READ, TARGET)
        _add(store, csrf)
        (root / "arm").touch()
        with ThreadPoolExecutor(max_workers=2) as executor:
            submit = executor.submit(_post, store, CART + "/submit", csrf)
            try:
                for _ in range(300):
                    if (root / "submit_inside").exists():
                        break
                    time.sleep(.01)
                assert (root / "submit_inside").exists()
                path = env["master_path"]
                with closing(sqlite3.connect(path, timeout=0)) as contender:
                    with pytest.raises(sqlite3.OperationalError, match="locked"):
                        contender.execute("BEGIN IMMEDIATE")
                pause = executor.submit(_run, mcp, tokens["gene"], PAUSE, _arguments(info, PAUSE))
            finally:
                (root / "submit_continue").touch()
            assert "提交成功" in submit.result().text
            assert pause.result()["after_state"] == "paused"
        assert _sql(path, "SELECT source_warehouse_code FROM store_order_items") == [("dc_alpha",)]
        assert "缺货停订" in _add(store, csrf).text


def test_ordinary_admin_route_aba_invalidates_gene_version(lab):
    root, env, tokens = lab
    master = env["master_path"]
    with _services(root, tokens) as (web, mcp), _login(web, "alpha_mgr") as admin:
        baseline = _run(mcp, tokens["gene"], READ, TARGET)
        csrf = _csrf(admin.get("/admin/item-routing").text)
        assert _post(admin, "/admin/item-routing/201", csrf, dc_code="dc_beta").status_code == 200
        off_source = _run(mcp, tokens["gene"], READ, TARGET)
        assert off_source["routing"]["configured_source"] is None
        _run(mcp, tokens["gene"], PAUSE, _arguments(off_source, PAUSE), error="original_source_required")
        assert _post(admin, "/admin/item-routing/201", csrf, dc_code="dc_alpha").status_code == 200
        current = _run(mcp, tokens["gene"], READ, TARGET)
        assert current["routing"]["configured_source"] == baseline["routing"]["configured_source"]
        assert current["routing"]["version"] != baseline["routing"]["version"]
        _run(mcp, tokens["gene"], PAUSE, _arguments(baseline, PAUSE), error="routing_version_conflict")
        assert _sql(master, "SELECT COUNT(*) FROM item_routing_stockout_events") == [(0,)]


def test_live_hold_survives_disabled_mcp_and_web(lab):
    root, env, tokens = lab
    gene = tokens["gene"]
    with _services(root, tokens) as (_, mcp):
        info = _run(mcp, gene, READ, TARGET)
        receipt = _run(mcp, gene, PAUSE, _arguments(info, PAUSE))
    with _services(root, tokens, web_mode=False, mcp_mode=False) as (web, mcp), _login(web, "store_staff") as store:
        paused = _run(mcp, gene, READ, TARGET)
        assert paused["routing"]["pause_id"] == receipt["pause_id"]
        _run(mcp, gene, RESTORE, _arguments(paused, RESTORE), error="routing_mode_unavailable")
        catalog = store.get(CATALOG + "?dc=dc_alpha")
        assert "品项A" not in catalog.text
        csrf = _csrf(catalog.text)
        attempted = _post(store, CART + "/add", csrf, **TARGET, dc="dc_alpha", quantity="1", unit="件")
        assert "品项A 缺货停订" in attempted.text
    assert check_rollback(["--db", str(env["master_path"])]) == 1


def test_production_routing_writer_shares_rollback_fence(lab, monkeypatch):
    root, env, tokens = lab
    master = env["master_path"]
    fence = root / "routing-writes.lock"
    fence.write_text("enabled\n")
    monkeypatch.setattr(writer, "_open_fence", lambda _: os.open(fence, os.O_RDONLY))
    key = secrets.token_urlsafe(32)
    # Supply an isolated inode in place of the production /etc path. The
    # production-mode MCP still exercises the real file state and flock.
    program = """
import os, sys, routing_writer_fence
path, socket_fd = sys.argv[1:]
routing_writer_fence._open_fence = lambda _: os.open(path, os.O_RDONLY)
sys.argv = ['uvicorn', 'mcp_server.main:_build_app', '--factory', '--fd', socket_fd]
from uvicorn import main
main()
"""
    web_env = _child_env(root / "db", root, key, mode=True)
    with _process(["-c", WEB_PROGRAM, str(root)], web_env) as web:
        mcp_env = dict(web_env, DAILYCHECK_ENV="production",
                       DAILYCHECK_ROUTING_WEB_PORT=web.rsplit(":", 1)[1])
        with _process(["-c", program, str(fence)], mcp_env,
                      headers=_headers(tokens["legacy"])) as mcp:
            gene = tokens["gene"]
            status = _run(mcp, gene, READ, TARGET)
            assert status["mode"]["writes_enabled"]
            args = _arguments(status, PAUSE)
            fence.write_text("disabled\n")
            with writer.routing_writer_fence(switching=True) as fd:
                child = subprocess.Popen(
                    [sys.executable, "-c", "import sys; sys.stdin.read(1)"],
                    pass_fds=(fd,), stdin=subprocess.PIPE,
                )
            try:
                assert child.poll() is None
                with pytest.raises(BlockingIOError), writer.routing_writer_fence(switching=True):
                    pytest.fail("inherited helper lost the interlock")
                assert not _run(mcp, gene, READ, TARGET)["mode"]["writes_enabled"]
                _run(mcp, gene, PAUSE, args, error="routing_storage_unavailable")
                assert _sql(master, "SELECT id FROM item_routing_stockout_events") == []
            finally:
                child.communicate(b"x", timeout=8)
            assert child.returncode == 0
            _run(mcp, gene, PAUSE, args, error="routing_storage_unavailable")
            assert _sql(master, "SELECT id FROM item_routing_stockout_events") == []
            fence.write_text("enabled\n")
            _sql(master, "PRAGMA user_version=9")
            assert not _run(mcp, gene, READ, TARGET)["mode"]["writes_enabled"]
            _run(mcp, gene, PAUSE, args, error="routing_mode_unavailable")
            _sql(master, "PRAGMA user_version=10")
            receipt = _run(mcp, gene, PAUSE, args)
            assert _sql(master, "SELECT id FROM item_routing_stockout_events") == [(receipt["event_id"],)]


@pytest.mark.parametrize("mutation", ["inactive", "alias", "unit", "duplicate", "unorderable"])
def test_routed_eligibility_matches_web_and_gene(lab, mutation):
    root, env, tokens = lab
    with _services(root, tokens) as (web, mcp), _login(web, "store_staff") as store:
        csrf = _csrf(store.get(CATALOG).text)
        _add(store, csrf)
        warehouse = env["paths"]["dc_alpha"]
        changes = {
            "inactive": "is_active=0", "alias": "is_alias=1", "unit": "unit='kg'",
            "unorderable": "is_orderable=0",
        }
        if mutation == "duplicate":
            _sql(warehouse, """INSERT INTO items (sku,name,category_id,canonical_id,unit,updated_at)
                               SELECT 'duplicate','duplicate',category_id,canonical_id,unit,updated_at
                               FROM items WHERE canonical_id=201""")
        else:
            _sql(warehouse, f"UPDATE items SET {changes[mutation]} WHERE canonical_id=201")
        info = _run(mcp, tokens["gene"], READ, TARGET)
        expected = "warehouse_not_orderable" if mutation == "unorderable" else "binding_unavailable"
        assert info["routing"]["reason"] == expected and not info["routing"]["orderable"]
        catalog = store.get(CATALOG).text
        assert "品项A" not in catalog and "品项F" in catalog
        assert "品项A 当前不可订" in _add(store, csrf).text
        assert "品项A" in _post(store, CART + "/submit", csrf).text
        assert _sql(env["master_path"], "SELECT id FROM store_orders") == []



def test_v9_upgrade_and_guard(lab):
    root, env, _ = lab
    master = env["master_path"]
    legacy = root / "legacy.db"
    with closing(sqlite3.connect(master)) as src, closing(sqlite3.connect(legacy)) as dst:
        src.backup(dst)
    with closing(sqlite3.connect(legacy)) as conn, conn:
        for ddl in reversed(ROUTING_DDL):
            kind, name = re.search(r"CREATE (?:UNIQUE )?(TABLE|TRIGGER|INDEX) IF NOT EXISTS (\w+)", ddl).groups()
            conn.execute(f"DROP {kind} {name}")
        conn.execute("PRAGMA user_version=9")
    old_rows = _sql(legacy, "SELECT * FROM item_routing")
    assert check_rollback(["--db", str(legacy)]) == 2
    assert migrate_up(legacy, MIGRATIONS, root / "backups") == [10]
    assert migrate_up(legacy, MIGRATIONS, root / "backups") == []
    assert _sql(legacy, "SELECT * FROM item_routing") == old_rows
    backup, = (root / "backups").glob("*9-to-10*")
    assert _sql(backup, "PRAGMA user_version") == [(9,)]
    with pytest.raises(RuntimeError, match="audit must be retained"):
        migrate_down(legacy, 9, MIGRATIONS, root / "backups")
    assert _sql(legacy, "PRAGMA user_version") == [(10,)]
    assert check_rollback(["--db", str(legacy)]) == 0


def _issuance_command(root, master, secret, *args, password=TEST_PASSWORD):
    env = _child_env(root / "db", root, "", mode=False)
    env["DAILYCHECK_ROUTING_ISSUANCE_SECRET"] = secret
    return subprocess.run(
        [sys.executable, "-m", "scripts.routing_policy_token", "--db", str(master), *args],
        env=env, input=password + "\n", capture_output=True, text=True, timeout=10,
    )


def test_routing_token_approved_preview_issue_revoke(lab):
    root, env, tokens = lab
    master = env["master_path"]
    secret = secrets.token_urlsafe(32)
    preview = ("preview", "--issuer", "2", "--name", "cli-gene-fixture", "--preset", "gene", "--version", "1")
    before = master.read_bytes()
    for actor, code in [("2", "dc_alpha"), ("3", "dc_beta")]:
        denied = _issuance_command(root, master, secret, *preview, "--actor", actor, "--pair", "201", code)
        assert denied.returncode != 0 and not denied.stdout
    permitted = (*preview, "--actor", "3", "--pair", "201", "dc_alpha")
    result = _issuance_command(root, master, secret, *permitted)
    assert result.returncode == 0, result.stderr
    confirmation = json.loads(result.stdout)["confirmation"]
    assert master.read_bytes() == before
    private = root / "private-token"
    private.mkdir(mode=0o700)
    token_file = private / "bearer"
    wrong = _issuance_command(root, master, secret, "issue", "--issuer", "3",
                              "--confirmation", confirmation, "--token-file", str(token_file))
    assert wrong.returncode != 0 and master.read_bytes() == before
    wrong_password = _issuance_command(root, master, secret, *permitted, password="incorrect")
    assert wrong_password.returncode != 0 and master.read_bytes() == before

    command = ("issue", "--issuer", "2", "--token-file", str(token_file), "--confirmation", confirmation)
    _sql(master, "UPDATE mcp_policy_routing_approvals SET revoked_at='test' WHERE canonical_id=201")
    assert _issuance_command(root, master, secret, *command).returncode != 0
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        _sql(master, "UPDATE mcp_policy_routing_approvals SET revoked_at=NULL WHERE canonical_id=201")
    _sql(master, "INSERT INTO mcp_policy_routing_approvals VALUES ('approval-new', 'fixture-principal', "
                 "201, 'dc_alpha', NULL)")
    assert _issuance_command(root, master, secret, *command).returncode != 0
    result = _issuance_command(root, master, secret, *permitted)
    assert result.returncode == 0, result.stderr
    command = (*command[:-1], json.loads(result.stdout)["confirmation"])
    # An injected scope INSERT failure leaves neither a token nor a partial binding.
    _sql(master, "CREATE TRIGGER scope_write_failure BEFORE INSERT ON mcp_policy_routing_scopes "
                 "BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
    failed = _issuance_command(root, master, secret, *command)
    assert failed.returncode != 0 and not token_file.exists()
    assert _sql(master, "SELECT id FROM agent_tokens WHERE name='cli-gene-fixture'") == []
    assert _sql(master, "SELECT COUNT(*) FROM mcp_policy_bindings") == [(3,)]
    _sql(master, "DROP TRIGGER scope_write_failure")
    token_file.write_text("existing")
    assert _issuance_command(root, master, secret, *command).returncode != 0
    assert token_file.read_text() == "existing"
    token_file.unlink()
    private.chmod(0o755)
    assert _issuance_command(root, master, secret, *command).returncode != 0
    assert not token_file.exists()
    private.chmod(0o700)
    issued = _issuance_command(root, master, secret, *command)
    assert issued.returncode == 0, issued.stderr
    identity = json.loads(issued.stdout)["token_id"]
    token = token_file.read_text().strip()
    assert "mp." not in issued.stdout and token.startswith("mp.")
    assert token_file.stat().st_mode & 0o777 == 0o600
    assert _sql(master, "SELECT allowed_read_paths_json, allowed_write_paths_json, "
                        "allowed_warehouse_codes_json FROM agent_tokens WHERE id=?", (identity,)) == [
        ("[]", "[]", "[]"),
    ]
    assert _issuance_command(root, master, secret, *command).returncode != 0
    with _services(root, tokens) as (web, mcp), _login(web, "alpha_mgr") as admin:
        info = _run(mcp, token, READ, TARGET)
        assert info["routing"]["orderable"]
        args = _arguments(info, PAUSE)
        assert _run(mcp, token, PAUSE, args)["actor_id"] == 3
        with httpx.Client(trust_env=False, timeout=5) as client:
            formerly_approved = client.post(
                mcp + "/api/mcp/", headers=_headers(tokens["gene"]), json=INITIALIZE,
            )
            assert formerly_approved.status_code == 401
            csrf = _csrf(admin.get("/admin/agent-tokens").text)
            response = _post(admin, f"/admin/agent-tokens/{identity}/revoke", csrf)
            assert response.status_code == 200
            denied = client.post(mcp + "/api/mcp/", headers=_headers(token), json=INITIALIZE)
            assert denied.status_code == 401
        assert _sql(master, "SELECT COUNT(*) FROM item_routing_stockout_events") == [(1,)]
        row, = _sql(master, "SELECT a.revoked_at, b.revoked_at, b.revoked_by FROM agent_tokens a "
                            "JOIN mcp_policy_bindings b ON a.id=b.token_id WHERE a.id=?", (identity,))
        assert row[0] and row[0] == row[1] and row[2] == 2


def test_empty_v10_authorization_to_live_token_and_irreversible_revocation(tmp_path, monkeypatch):
    root = tmp_path
    db_dir = root / "db"
    db_dir.mkdir()
    env, tokens = _seed(db_dir, monkeypatch, approvals=False)
    master = env["master_path"]
    secret = secrets.token_urlsafe(32)

    def cmd(*args, **kwargs):
        return _issuance_command(root, master, secret, *args, **kwargs)

    def prepare(operation, reference, *scope):
        before = master.read_bytes()
        result = cmd("authorization-preview", "--operation", operation, "--issuer", "2",
                     "--reference", reference, *scope)
        assert result.returncode == 0, result.stderr
        assert master.read_bytes() == before
        return json.loads(result.stdout)["confirmation"]

    def confirm(proof, *options, **kwargs):
        return cmd("authorization-confirm", "--issuer", "2", "--confirmation", proof, *options, **kwargs)

    def applied(result):
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    for table in ("mcp_policy_presets", "mcp_policy_routing_principals", "mcp_policy_routing_approvals",
                  "mcp_policy_routing_admin_events"):
        assert _sql(master, f"SELECT COUNT(*) FROM {table}") == [(0,)]
    scope = ("--actor", "3", "--pair", "201", "dc_alpha")
    selection = ("preview", "--issuer", "2", "--actor", "3", "--pair", "201", "dc_alpha",
                 "--name", "real-entry-gene-fixture", "--preset", FIXED_PRESET, "--version", "1")
    assert cmd(*selection).returncode != 0
    preset_proof = prepare("install-preset", "FIXTURE-CONFIG-17")
    preset = applied(confirm(preset_proof))
    assert preset == {"operation": "install-preset", "preset_name": FIXED_PRESET, "preset_version": 1}
    after_preset = master.read_bytes()
    assert cmd(*selection).returncode != 0  # A preset alone cannot issue a credential.
    assert confirm(preset_proof).returncode != 0
    for invalid_scope in [
        ("--actor", "999", "--pair", "201", "dc_alpha"),
        ("--actor", "3", "--pair", "201", "dc_beta"),
    ]:
        assert cmd("authorization-preview", "--operation", "approve", "--issuer", "2",
                   "--reference", "BAD-FIXTURE-DECISION", *invalid_scope).returncode != 0
    assert cmd("authorization-preview", "--operation", "approve", "--issuer", "3",
               "--reference", "NOT-ADMIN", *scope).returncode != 0
    assert cmd("authorization-preview", "--operation", "approve", "--issuer", "2",
               "--reference", "MULTIPLE-PAIRS", *scope, "--pair", "207", "dc_beta").returncode != 0
    assert master.read_bytes() == after_preset

    _sql(master, "INSERT INTO tenants (id, name, created_at) VALUES (2, 'Synthetic other tenant', 'test')")
    _sql(master, "UPDATE canonical_items SET tenant_id=2 WHERE id=201")
    foreign_snapshot = master.read_bytes()
    foreign = cmd("authorization-preview", "--operation", "approve", "--issuer", "2",
                  "--reference", "OTHER-TENANT-ITEM", *scope)
    assert foreign.returncode != 0 and "Canonical item must belong" in foreign.stderr
    assert master.read_bytes() == foreign_snapshot
    _sql(master, "UPDATE canonical_items SET tenant_id=1 WHERE id=201")
    after_preset = master.read_bytes()

    approval_proof = prepare("approve", "FIXTURE-PETER-DECISION-18", *scope)
    assert confirm(approval_proof, password="wrong").returncode != 0
    assert master.read_bytes() == after_preset
    # A normal route write refreshes the opaque version even when the warehouse is the same.
    _sql(master, "UPDATE item_routing SET updated_by=2 WHERE canonical_id=201")
    after_route_revision = master.read_bytes()
    assert confirm(approval_proof).returncode != 0
    assert master.read_bytes() == after_route_revision
    approval_proof = prepare("approve", "FIXTURE-PETER-DECISION-18", *scope)
    # Fail the final audit write and verify that neither the principal nor the pair persists.
    _sql(master, "CREATE TRIGGER fixture_no_approval_audit BEFORE INSERT ON mcp_policy_routing_admin_events "
                 "BEGIN SELECT RAISE(ABORT, 'fixture audit failure'); END")
    assert confirm(approval_proof).returncode != 0
    assert _sql(master, "SELECT COUNT(*) FROM mcp_policy_routing_principals") == [(0,)]
    assert _sql(master, "SELECT COUNT(*) FROM mcp_policy_routing_approvals") == [(0,)]
    _sql(master, "DROP TRIGGER fixture_no_approval_audit")

    approval = applied(confirm(approval_proof))["approval_id"]
    after_approval = master.read_bytes()
    assert confirm(approval_proof).returncode != 0
    assert master.read_bytes() == after_approval
    assert _sql(master, "SELECT is_admin FROM users WHERE id=3") == [(0,)]
    assert _sql(master, "SELECT COUNT(*) FROM mcp_policy_bindings") == [(0,)]
    other_subject = cmd("authorization-preview", "--operation", "approve", "--issuer", "2",
                        "--reference", "OTHER-SUBJECT", "--actor", "2", "--pair", "207", "dc_beta")
    assert other_subject.returncode != 0

    issuance = applied(cmd(*selection))["confirmation"]
    private = root / "issued-token"
    private.mkdir(mode=0o700)
    token_file = private / "bearer"
    applied(cmd("issue", "--issuer", "2", "--confirmation", issuance, "--token-file", str(token_file)))
    raw = token_file.read_text().strip()
    with _services(root, tokens) as (_, mcp):
        info = _run(mcp, raw, READ, TARGET)
        _run(mcp, raw, READ, {"canonical_id": 207, "warehouse_code": "dc_beta"}, error="forbidden")
        pause = _run(mcp, raw, PAUSE, _arguments(info, PAUSE))
        assert (pause["actor_id"], pause["after_state"]) == (3, "paused")
        revoke_proof = prepare("revoke", "FIXTURE-PETER-REVOCATION-19", *scope)
        assert applied(confirm(revoke_proof))["approval_id"] == approval
        after_revoke = master.read_bytes()
        assert confirm(revoke_proof).returncode != 0
        assert master.read_bytes() == after_revoke
        with httpx.Client(trust_env=False, timeout=5) as client:
            assert client.post(mcp + "/api/mcp/", headers=_headers(raw), json=INITIALIZE).status_code == 401
            stale = prepare("approve", "FIXTURE-PETER-STALE-20", *scope)
            concurrent = prepare("approve", "FIXTURE-PETER-CONCURRENT-21", *scope)
            intervening = applied(confirm(concurrent))["approval_id"]
            revoke_again = prepare("revoke", "FIXTURE-PETER-REVOCATION-22", *scope)
            assert applied(confirm(revoke_again))["approval_id"] == intervening
            after_intervening_generation = master.read_bytes()
            assert confirm(stale).returncode != 0
            assert master.read_bytes() == after_intervening_generation
            new = applied(confirm(prepare("approve", "FIXTURE-PETER-DECISION-23", *scope)))["approval_id"]
            assert len({approval, intervening, new}) == 3
            assert client.post(mcp + "/api/mcp/", headers=_headers(raw), json=INITIALIZE).status_code == 401
        assert check_rollback(["--db", str(master)]) == 1
        assert _sql(master, "SELECT pause_id FROM item_routing_stockout_state WHERE canonical_id=201") == [
            (pause["pause_id"],),
        ]

    audit = _sql(master, "SELECT operation, issuer_id, decision_ref, decided_at, approval_id "
                         "FROM mcp_policy_routing_admin_events ORDER BY decided_at")
    assert [(op, issuer, ref, aid) for op, issuer, ref, _, aid in audit] == [
        ("install-preset", 2, "FIXTURE-CONFIG-17", None),
        ("approve", 2, "FIXTURE-PETER-DECISION-18", approval),
        ("revoke", 2, "FIXTURE-PETER-REVOCATION-19", approval),
        ("approve", 2, "FIXTURE-PETER-CONCURRENT-21", intervening),
        ("revoke", 2, "FIXTURE-PETER-REVOCATION-22", intervening),
        ("approve", 2, "FIXTURE-PETER-DECISION-23", new),
    ]
    assert all(datetime.fromisoformat(event[3]).tzinfo is not None for event in audit)
    assert _sql(master, "SELECT p.actor_id, a.canonical_id, a.warehouse_code FROM mcp_policy_routing_approvals a "
                        "JOIN mcp_policy_routing_principals p ON p.id=a.principal_id WHERE a.id=?", (approval,)) == [
        (3, 201, "dc_alpha"),
    ]
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        _sql(master, "DELETE FROM mcp_policy_routing_admin_events WHERE operation='revoke'")


def main():
    """Leave a synthetic lab running in an interactive terminal for a standard MCP client."""
    from tempfile import TemporaryDirectory

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit("Open an interactive terminal to keep the synthetic bearer out of captured logs")
    with TemporaryDirectory(prefix="dc-routing-client-") as directory, pytest.MonkeyPatch.context() as patch:
        root = Path(directory)
        patch.setenv("TMPDIR", str(root))
        # The fixture modifies these paths before it creates the Flask app.
        patch.setenv("DAILYCHECK_DB_DIR", str(root / "db"))
        db_dir = root / "db"
        db_dir.mkdir()
        _, tokens = _seed(db_dir, patch)
        with _services(root, tokens) as (web, mcp):
            print(f"Storefront: {web}{CATALOG}\nLogin: store_staff / {TEST_PASSWORD}")
            print(f"StreamableHTTP: {mcp}/api/mcp/\nSynthetic Authorization: Bearer {tokens['gene']}")
            input("Press Enter to stop both isolated services: ")


if __name__ == "__main__":
    main()
