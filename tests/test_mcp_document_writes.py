"""Issue #87: real warehouse writes, independent observers and process races."""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import sqlite3
from contextlib import contextmanager
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from mcp_server.data import unit_of_work as uow
from mcp_server.data import warehouse as data
from mcp_server.infra.errors import McpError, ValidationError, WriteError
from mcp_server.protocol.tools import get_tools
from mcp_server.service import inbound, outbound
from mcp_server.service.auth import AuthContext, set_current_auth

CTX = AuthContext(1, ["/api/v1/*"], ["/api/v1/*"], None)
KEY = {"doc_no": "juleng:JL-1", "line_no": "01"}


@pytest.fixture
def env(logged_client, monkeypatch):
    client, path = logged_client
    monkeypatch.setattr(uow, "MASTER_DB", str(path.parent.parent / "master.db"))
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO items(id,sku,name,category_id,quantity,updated_at) "
            "VALUES(1,'a','A',1,100,'2026-10-30')"
        )
    return client, path


def observe(path):
    with sqlite3.connect(path) as conn:
        return (
            conn.execute("SELECT quantity FROM items WHERE id=1").fetchone()[0],
            conn.execute("SELECT count(*) FROM restock_requests").fetchone()[0],
            conn.execute("SELECT count(*) FROM outbound_requests").fetchone()[0],
            conn.execute("SELECT count(*) FROM document_write_keys").fetchone()[0],
            conn.execute("SELECT count(*) FROM stock_movements").fetchone()[0],
        )


def create(direction="outbound", quantity=1, **kw):
    fn = outbound.create_outbound if direction == "outbound" else inbound.create_restock
    return fn(1, quantity, "wh_test", CTX, **kw)


@pytest.mark.parametrize("direction", ["outbound", "restock"])
def test_legacy_positional_and_fraction(env, direction):
    fn = outbound.create_outbound if direction == "outbound" else inbound.create_restock
    first = fn(1, 1, "wh_test", CTX, "legacy reason")
    second = fn(1, 0.25, "wh_test", CTX, "legacy reason")
    assert type(first["id"]) is int and first["id"] != second["id"]
    assert first["doc_no"] is None and second["quantity"] == 0.25
    assert second["factor"] == 1 and second["replayed"] is False
    assert observe(env[1]) == (
        (98.75 if direction == "outbound" else 101.25),
        int(direction == "restock") * 2,
        int(direction == "outbound") * 2,
        0,
        2,
    )


@pytest.mark.parametrize(
    "quantity,factor,effective",
    [(1, 10, 10), (0.25, 1, 0.25), (0.125, 10, 1.25), (1.005, 1, 1), (1.015, 1, 1.02)],
)
@pytest.mark.parametrize("direction", ["outbound", "restock"])
def test_conversion_once_half_even(env, direction, quantity, factor, effective):
    with localcontext() as ctx:
        ctx.prec = 3
        r = create(direction, quantity, factor=factor, **KEY)
    assert r["quantity"] == effective
    sign = -1 if direction == "outbound" else 1
    assert observe(env[1])[0] == pytest.approx(100 + sign * effective)
    with sqlite3.connect(env[1]) as conn:
        request = conn.execute(
            f"SELECT requested_quantity,input_quantity,factor FROM {direction}_requests"
        ).fetchone()
        assert request == (effective, str(Decimal(str(quantity))), str(factor))
        assert conn.execute("SELECT delta FROM stock_movements").fetchone()[0] == sign * effective
    assert create(direction, quantity, factor=factor, **KEY)["replayed"]
    if direction == "outbound":
        outbound.rollback_outbound(r["id"], "wh_test", CTX)
        assert observe(env[1])[0] == 100
        assert create(direction, quantity, factor=factor, **KEY)["rolled_back"] == 1
        with pytest.raises(ValidationError, match="already_rolled_back"):
            outbound.rollback_outbound(r["id"], "wh_test", CTX)
        assert observe(env[1])[0] == 100


@pytest.mark.parametrize(
    "field,value",
    [("quantity", x) for x in (0, -1, True, "1", None, float("nan"), float("inf"), 0.001, 10**400)]
    + [("factor", x) for x in (0, -1, True, "1", None, float("nan"), float("inf"))]
    + [("item_id", x) for x in (0, -1, True, 1.0, "1")]
    + [("doc_no", x) for x in (None, "", " a", "a ", "a\n", "a" * 129, 1)]
    + [("line_no", x) for x in (None, "", "01 ", "a\x00", "a" * 65, 1)],
)
def test_schema_invalid_zero_writes(env, field, value):
    args = dict(item_id=1, quantity=1, warehouse_code="wh_test", ctx=CTX, **KEY)
    args[field] = value
    with pytest.raises(ValidationError):
        inbound.create_restock(**args)
    assert observe(env[1]) == (100, 0, 0, 0, 0)


def test_schema_pair_scope_and_namespace(env):
    with pytest.raises(ValidationError):
        create(doc_no="opaque")
    with pytest.raises(ValidationError):
        create(line_no="01")
    for doc, line in [
        ("opaque", "01"),
        ("opaque", "1"),
        ("Opaque", "01"),
        ("other:JL-1", "01"),
        ("juleng:JL-1", "01"),
    ]:
        create(doc_no=doc, line_no=line)
    create("restock", **KEY)
    assert observe(env[1]) == (96, 1, 5, 6, 6)
    forbidden = AuthContext(2, [], [], ["elsewhere"])
    with pytest.raises(McpError, match="forbidden_warehouse"):
        inbound.create_restock(1, 1, "wh_test", forbidden, **KEY)


def test_schema_published_matches_runtime():
    published = {
        t["name"]: t
        for t in json.loads(Path("docs/integrations/dailycheck-mcp/tools.json").read_text())[
            "tools"
        ]
    }
    for tool in get_tools():
        if tool.name in published:
            assert tool.description == published[tool.name]["description"]
        if tool.name in ("restock_create", "outbound_create"):
            schema = getattr(tool, "inputSchema", None) or tool.input_schema
            assert schema == published[tool.name]["inputSchema"]
            assert schema["required"] == ["item_id", "quantity", "warehouse_code"]
            assert schema["properties"]["quantity"]["type"] == "number"
            assert "<source>:" in schema["properties"]["doc_no"]["description"]


def test_schema_tool_passthrough_and_error_serialization(env, monkeypatch):
    from mcp_server.protocol import server as protocol

    class CaptureServer:
        def __init__(self, *_):
            pass

        def list_tools(self):
            return lambda fn: fn

        def call_tool(self):
            def register(fn):
                self.handler = fn
                return fn

            return register

    monkeypatch.setattr(protocol, "Server", CaptureServer)
    call_tool = protocol.build_server().handler
    set_current_auth(CTX)
    try:
        args = dict(item_id=1, quantity=0.125, factor=10, warehouse_code="wh_test", **KEY)
        first = json.loads(asyncio.run(call_tool("restock_create", args))[0].text)
        assert first["quantity"] == 1.25 and first["input_quantity"] == 0.125
        args["factor"] = 1
        conflict = json.loads(asyncio.run(call_tool("restock_create", args))[0].text)
        assert conflict == {
            "error": "document_conflict",
            "message": "document_line_payload_mismatch",
        }
        args["factor"] = None
        assert (
            json.loads(asyncio.run(call_tool("restock_create", args))[0].text)["error"]
            == "validation_error"
        )
    finally:
        set_current_auth(None)


@pytest.mark.parametrize("direction", ["outbound", "restock"])
def test_replay_conflict_reason_orphan_and_list(env, direction):
    r = create(direction, factor=10, reason="first", **KEY)
    before = observe(env[1])
    for _ in range(3):
        again = create(direction, 1.0, factor=10.0, reason="changed", **KEY)
        assert again["id"] == r["id"] and again["replayed"] and not again["applied"]
    assert observe(env[1]) == before
    for qty, factor in ((2, 5), (1, 9), (2, 10)):
        with pytest.raises(WriteError, match="document_line_payload_mismatch"):
            create(direction, qty, factor=factor, **KEY)
    fn = outbound.create_outbound if direction == "outbound" else inbound.create_restock
    with pytest.raises(WriteError, match="document_line_payload_mismatch"):
        fn(999, 1, "wh_test", CTX, factor=10, **KEY)
    with sqlite3.connect(env[1]) as conn:
        assert conn.execute(f"SELECT reason FROM {direction}_requests").fetchone()[0] == "first"
        conn.execute("DELETE FROM items WHERE id=1")
    assert create(direction, factor=10, **KEY)["replayed"]
    listing = (outbound.list_outbound if direction == "outbound" else inbound.list_restock)(
        "wh_test", CTX
    )
    assert listing["total"] == 1 and listing["items"][0]["item_name"] is None
    assert {
        k: listing["items"][0][k] for k in ("doc_no", "line_no", "input_quantity", "factor")
    } == {**KEY, "input_quantity": "1", "factor": 10}


@pytest.mark.parametrize("direction", ["outbound", "restock"])
def test_rollback_delete_never_resurrects(env, direction):
    r = create(direction, factor=10, **KEY)
    assert env[0].post(f"/{direction}/{r['id']}/delete").status_code == 302
    assert observe(env[1]) == (100, 0, 0, 1, 2)
    with pytest.raises(WriteError) as exc:
        create(direction, factor=10, **KEY)
    assert exc.value.to_dict() == {
        "error": "document_line_deleted",
        "message": "document_line_deleted",
        "request_id": r["id"],
        "committed": True,
    }
    with pytest.raises(WriteError, match="document_line_payload_mismatch"):
        create(direction, 2, factor=10, **KEY)
    assert observe(env[1]) == (100, 0, 0, 1, 2)


def test_replay_state_conflict(env):
    create(**KEY)
    with sqlite3.connect(env[1]) as conn:
        conn.execute("UPDATE outbound_requests SET requested_quantity=2")
    with pytest.raises(WriteError, match="document_line_state_conflict"):
        create(**KEY)
    assert observe(env[1])[0] == 99


@pytest.mark.parametrize("direction", ["restock", "outbound"])
@pytest.mark.parametrize("point", ["request", "document_write_keys", "items", "stock_movements"])
def test_failure_four_writes_rollback(env, direction, point, monkeypatch):
    service = inbound if direction == "restock" else outbound
    point = direction + "_requests" if point == "request" else point
    original = service.warehouse_connection

    class Inject(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            cur = super().execute(sql, parameters)
            if (sql.lstrip().startswith("INSERT INTO " + point)) or (
                point == "items" and sql.lstrip().startswith("UPDATE items")
            ):
                raise RuntimeError("injected after " + point)
            return cur

    @contextmanager
    def connection(path):
        conn = sqlite3.connect(path, factory=Inject)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    monkeypatch.setattr(service, "warehouse_connection", connection)
    with pytest.raises(RuntimeError, match="injected"):
        create(direction, **KEY)
    assert observe(env[1]) == (100, 0, 0, 0, 0)
    monkeypatch.setattr(service, "warehouse_connection", original)
    assert create(direction, **KEY)["applied"]


def test_failure_commit_response_lost_and_cache_retry(env, monkeypatch):
    original = outbound._mark_invalid
    monkeypatch.setattr(
        outbound, "_mark_invalid", lambda _: (_ for _ in ()).throw(OSError("master failed"))
    )
    with pytest.raises(WriteError) as exc:
        create(factor=10, **KEY)
    assert exc.value.to_dict() == {
        "error": "cache_invalidation_failed",
        "message": "cache_invalidation_failed",
        "committed": True,
        "request_id": 1,
        "retry_safe": True,
    }
    assert observe(env[1]) == (90, 0, 1, 1, 1)
    monkeypatch.setattr(outbound, "_mark_invalid", original)
    assert create(factor=10, **KEY)["replayed"]
    assert observe(env[1]) == (90, 0, 1, 1, 1)
    # A client that loses the successful reply also retries the original key.
    create(factor=10, **KEY)
    assert observe(env[1]) == (90, 0, 1, 1, 1)


def test_failure_helper_has_no_commit(env):
    with sqlite3.connect(env[1]) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        data.create_restock(conn, 1, 2, "raw caller")
        assert observe(env[1]) == (100, 0, 0, 0, 0)
        conn.rollback()
    assert observe(env[1]) == (100, 0, 0, 0, 0)


def test_failure_stock_read_is_inside_lock(env, monkeypatch):
    trace = []
    original = outbound.warehouse_connection

    @contextmanager
    def traced(path):
        with original(path) as conn:
            conn.set_trace_callback(trace.append)
            yield conn

    monkeypatch.setattr(outbound, "warehouse_connection", traced)
    create(**KEY)
    begin = next(i for i, s in enumerate(trace) if s == "BEGIN IMMEDIATE")
    stock = next(i for i, s in enumerate(trace) if s.startswith("SELECT quantity FROM items"))
    assert begin < stock
    assert any("AND quantity >=" in s for s in trace)


def _process_write(path, master, barrier, queue, quantity, doc, operation="create", request_id=1):
    """Spawn entry: each process opens its own real SQLite connections."""
    import config
    import db
    from app import create_app
    from mcp_server.service import document_writes

    uow.MASTER_DB = master
    document_writes.resolve_warehouse = lambda code: {"db_path": path}
    outbound.resolve_warehouse = lambda code: {"db_path": path}
    if operation.startswith("web"):
        config.MASTER_DB = db.MASTER_DB = Path(master)
        config.WAREHOUSE_DB_DIR = db.WAREHOUSE_DB_DIR = Path(path).parent
        app = create_app()
        app.config["TESTING"] = True
        client = app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = 1
            session["warehouse_id"] = 1
    barrier.wait(timeout=20)
    try:
        if operation == "create":
            r = outbound.create_outbound(1, quantity, "wh_test", CTX, doc_no=doc, line_no="01")
            queue.put(("ok", r["replayed"]))
        elif operation == "rollback":
            outbound.rollback_outbound(request_id, "wh_test", CTX)
            queue.put(("ok", False))
        else:
            response = client.post(f"/outbound/{request_id}/{operation[4:]}")
            queue.put(("web", response.status_code))
    except McpError as exc:
        queue.put((exc.code, exc.message))


def race(env, jobs):
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(len(jobs))
    queue = ctx.Queue()
    workers = [
        ctx.Process(
            target=_process_write,
            args=(str(env[1]), str(env[1].parent.parent / "master.db"), barrier, queue, *job),
        )
        for job in jobs
    ]
    for worker in workers:
        worker.start()
    outputs = [queue.get(timeout=35) for _ in workers]
    for worker in workers:
        worker.join(timeout=10)
        assert worker.exitcode == 0
    return outputs


def test_concurrency_same_key(env):
    outputs = race(env, [(1, "same"), (1, "same"), (1, "same")])
    assert sorted(outputs) == [("ok", False), ("ok", True), ("ok", True)]
    assert observe(env[1]) == (99, 0, 1, 1, 1)


def test_concurrency_payload_conflict(env):
    outputs = race(env, [(1, "same"), (2, "same")])
    assert sorted(o[0] for o in outputs) == ["document_conflict", "ok"]
    assert observe(env[1])[2:] == (1, 1, 1)


def test_concurrency_different_keys_stock(env):
    with sqlite3.connect(env[1]) as conn:
        conn.execute("UPDATE items SET quantity=10")
    outputs = race(env, [(6, "a"), (6, "b")])
    assert sorted(o[0] for o in outputs) == ["ok", "validation_error"]
    assert observe(env[1]) == (4, 0, 1, 1, 1)


@pytest.mark.parametrize("operation", ["web:rollback", "web:delete"])
def test_concurrency_rollback_web_mcp(env, operation):
    create(factor=10, **KEY)
    race(env, [(1, "unused", "rollback"), (1, "unused", operation)])
    assert observe(env[1])[0] == 100
    assert observe(env[1])[-1] == 2
    if operation == "web:delete":
        with pytest.raises(WriteError, match="document_line_deleted"):
            create(factor=10, **KEY)


def test_replay_unique_race_rechecks_new_transaction(env, monkeypatch):
    from mcp_server.service import document_writes as documents

    first = create(**KEY)
    original = documents.journal
    calls = []

    def miss_once(conn, direction, payload):
        calls.append(conn.in_transaction)
        if len(calls) == 1:
            return None
        return original(conn, direction, payload)

    monkeypatch.setattr(documents, "journal", miss_once)
    again = create(**KEY)  # request UNIQUE failure -> rollback -> new transaction -> replay
    assert again["id"] == first["id"] and again["replayed"]
    assert calls == [True, True]
    assert observe(env[1]) == (99, 0, 1, 1, 1)


def test_failure_non_unique_integrity_not_swallowed(env, monkeypatch):
    def fail(*args, **kwargs):
        raise sqlite3.IntegrityError("NOT NULL constraint failed: request.reason")

    monkeypatch.setattr(inbound, "_create_restock", fail)
    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
        create("restock", **KEY)
    assert observe(env[1]) == (100, 0, 0, 0, 0)


def test_failure_busy_bounded_and_no_half_write(env):
    import time

    with sqlite3.connect(env[1]) as lock:
        lock.execute("BEGIN IMMEDIATE")
        start = time.monotonic()
        with pytest.raises(WriteError) as error:
            create(**KEY)
        assert 4.5 <= time.monotonic() - start < 8
        assert error.value.http_status == 503
        assert error.value.to_dict() == {
            "error": "database_busy",
            "message": "database_busy",
            "retry_safe": True,
        }
        assert observe(env[1]) == (100, 0, 0, 0, 0)
        lock.rollback()
    assert create(**KEY)["applied"]


def test_failure_cache_unkeyed_and_rollback_retry(env, monkeypatch):
    original = outbound._mark_invalid

    def unavailable(_):
        raise OSError("master unavailable")

    monkeypatch.setattr(outbound, "_mark_invalid", unavailable)
    with pytest.raises(WriteError) as error:
        create()
    assert error.value.to_dict()["retry_safe"] is False
    assert observe(env[1]) == (99, 0, 1, 0, 1)
    with pytest.raises(WriteError) as error:
        outbound.rollback_outbound(1, "wh_test", CTX)
    assert error.value.to_dict()["committed"] is True
    assert observe(env[1]) == (100, 0, 1, 0, 2)
    calls = []

    def repaired(item_id):
        calls.append(item_id)
        original(item_id)

    monkeypatch.setattr(outbound, "_mark_invalid", repaired)
    with pytest.raises(ValidationError, match="already_rolled_back"):
        outbound.rollback_outbound(1, "wh_test", CTX)
    assert calls == [1] and observe(env[1]) == (100, 0, 1, 0, 2)


def test_replay_warehouse_isolation(env, monkeypatch):
    import db

    other = env[1].parent / "other.db"
    db.init_warehouse_db(other)
    with sqlite3.connect(other) as conn:
        conn.execute(
            "INSERT INTO items(id,sku,name,category_id,quantity,updated_at) VALUES(1,'x','X',1,100,'now')"
        )
    first = create("restock", **KEY)
    monkeypatch.setattr(inbound, "resolve_warehouse", lambda _: {"db_path": str(other)})
    second = create("restock", **KEY)
    assert not first["replayed"] and not second["replayed"]
    assert observe(env[1]) == observe(other) == (101, 1, 0, 1, 1)


def test_replay_status_not_reapplied(env):
    r = create("restock", **KEY)
    env[0].post(f"/restock/{r['id']}/status", data={"status": "提交"})
    assert create("restock", **KEY)["status"] == "提交"
    assert observe(env[1]) == (101, 1, 0, 1, 1)


def test_failure_no_commit_in_outbound_or_rollback_helpers(env):
    create(**KEY)
    with sqlite3.connect(env[1]) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        data.rollback_outbound(conn, 1)
        assert observe(env[1]) == (99, 0, 1, 1, 1)
        conn.rollback()
        conn.execute("BEGIN IMMEDIATE")
        data.create_outbound(conn, 1, 2, None)
        assert observe(env[1]) == (99, 0, 1, 1, 1)
        conn.rollback()
    assert observe(env[1]) == (99, 0, 1, 1, 1)


def test_conversion_real_cent_loss_and_large_payloads(env):
    with pytest.raises(ValidationError, match="quantity_out_of_range"):
        create("restock", 9007199254740991, factor=0.01, **KEY)
    assert observe(env[1]) == (100, 0, 0, 0, 0)
    # Huge finite source integers remain valid JSON numbers when factor yields a REAL.
    r = create("restock", 10**320, factor=1e-320, **KEY)
    assert r["input_quantity"] == 10**320 and r["quantity"] == 1
    assert json.loads(json.dumps(r, allow_nan=False))["input_quantity"] == 10**320
    assert create("restock", 10**320, factor=1e-320, **KEY)["replayed"]


def test_replay_cutover_fixture_checkpoint(env, tmp_path):
    """Exercise the documented H/N cutover on a temporary source/checkpoint, not a sync client."""
    history = create("restock", reason="juleng-sync:old:01")
    h = {"old:01": history["id"]}
    source = [
        ("old:01", "historical"),
        ("late:01", "new"),
        ("late:01", "new"),
        ("same-second:02", "new"),
        ("unknown:01", "quarantined"),
        ("later:01", "new"),
    ]
    checkpoint = tmp_path / "source-checkpoint.json"
    handled = []
    for external, state in source:
        if external in h:
            continue
        if state == "quarantined":
            break
        doc, line = external.split(":")
        create("restock", doc_no="juleng:" + doc, line_no=line)
        handled.append(external)
        checkpoint.write_text(json.dumps({"handled": handled, "H": h}))
    saved = json.loads(checkpoint.read_text())
    assert saved["handled"] == ["late:01", "late:01", "same-second:02"]
    assert saved["H"] == h and "unknown:01" not in saved["handled"]
    assert observe(env[1]) == (103, 3, 0, 2, 3)
    with sqlite3.connect(env[1]) as conn:
        assert (
            conn.execute(
                "SELECT doc_no FROM restock_requests WHERE id=?", (history["id"],)
            ).fetchone()[0]
            is None
        )


def test_failure_conditional_stock_update_is_enforced(env):
    # Simulate a database trigger changing stock during request insertion; the
    # conditional UPDATE must still fail, and the request/journal must roll back.
    with sqlite3.connect(env[1]) as conn:
        conn.execute(
            "CREATE TRIGGER stock_changed AFTER INSERT ON outbound_requests "
            "BEGIN UPDATE items SET quantity=0 WHERE id=NEW.item_id; END"
        )
    with pytest.raises(ValidationError, match="insufficient_stock"):
        create(**KEY)
    assert observe(env[1]) == (100, 0, 0, 0, 0)
