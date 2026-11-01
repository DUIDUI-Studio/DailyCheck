"""Issue #111: real source failures cannot take down order detail enrichment."""
from __future__ import annotations

import sqlite3
import time
from contextlib import closing, contextmanager

import pytest
from flask import template_rendered

from blueprints import store_ordering_pure as sop
from tests import multi_warehouse_env as builder
from tests.test_order_detail_source_stock import _info, _update_line
from tests.test_order_detail_source_stock import env as env


@contextmanager
def damaged_source(env, damage, source="dc_beta"):
    path = env["paths"][source]
    locker = None
    if damage == "missing":
        path.unlink()
    elif damage == "unopenable":
        # Opening a directory as a SQLite file fails even for privileged runners.
        path.unlink()
        path.mkdir()
    elif damage == "empty":
        path.write_bytes(b"")
    elif damage == "corrupt":
        path.write_bytes(b"not a database" * 100)
    elif damage == "locked":
        locker = builder.connect(path)
        locker.execute("BEGIN EXCLUSIVE")
    else:
        with closing(builder.connect(path)) as conn:
            if damage == "missing_items":
                conn.execute("DROP TABLE items")
            elif damage == "missing_column":
                conn.execute("ALTER TABLE items RENAME COLUMN selling_price TO unavailable_price")
            elif damage == "missing_categories":
                conn.execute("PRAGMA foreign_keys=OFF")
                conn.execute("DROP TABLE categories")
            else:
                raise AssertionError(damage)
            conn.commit()
    try:
        yield
    finally:
        if locker is not None:
            locker.rollback()
            locker.close()


def snapshot(env):
    directory = env["master_path"].parent
    return {str(p.relative_to(directory)): p.read_bytes()
            for p in directory.rglob("*") if p.is_file()}


def assert_isolated(env, info, bad=202):
    good = 201 if bad == 202 else 202
    assert info[env["lines"][bad]] == {
        "name": "品项B" if bad == 202 else "品项A", "dc_available": 0.0,
        "unit_price": 0.0, "category_name": "包材",
    }
    assert info[env["lines"][good]] == {
        "name": "品项A" if good == 201 else "品项B",
        "dc_available": 7.0 if good == 201 else 25.0,
        "unit_price": 9.9 if good == 201 else 1.5,
        "category_name": "alpha category" if good == 201 else "beta category",
    }


def http_info(env):
    env["app"].config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)
    contexts = []

    def capture(sender, template, context, **extra):
        if template.name == "store_ordering/order_detail.html":
            contexts.append(context)

    with template_rendered.connected_to(capture, env["app"]):
        response = env["client"].get(f"/store-ordering/orders/{env['order_id']}")
    assert response.status_code == 200
    return contexts[0]["dc_items_info"]


@pytest.mark.parametrize("damage", ["corrupt", "empty", "missing_items", "missing_column",
                                    "missing_categories", "locked", "missing", "unopenable"])
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("surface", ["helper", "http"])
def test_real_source_failure_isolated_in_helper_and_http(env, damage, legacy, surface):
    source, bad = ("dc_alpha", 201) if legacy else ("dc_beta", 202)
    if legacy:
        _update_line(env, "UPDATE store_order_items SET source_warehouse_code=NULL, "
                     "dc_item_id=NULL WHERE canonical_id=201")
    with damaged_source(env, damage, source):
        before = snapshot(env)
        assert_isolated(env, _info(env) if surface == "helper" else http_info(env), bad)
        assert snapshot(env) == before  # no creation, repair, migration or data write


def track_sources(monkeypatch):
    real_connect = sqlite3.connect
    opened = []

    def connect(database, *args, **kwargs):
        conn = real_connect(database, *args, **kwargs)
        if kwargs.get("uri") and "mode=ro" in str(database):
            statements = []
            busy_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            conn.set_trace_callback(statements.append)
            opened.append((str(database), conn, statements, busy_ms))
        return conn

    monkeypatch.setattr(sqlite3, "connect", connect)
    return opened


def assert_closed(opened):
    assert opened
    for _, conn, _, _ in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            conn.execute("SELECT 1")


def test_many_lines_one_locked_source_has_one_small_wait_and_closes(env, monkeypatch):
    _update_line(env, "INSERT INTO store_order_items "
                 "(order_id, canonical_id, quantity, unit, source_warehouse_code, dc_item_id, created_at) "
                 "SELECT order_id, 207, quantity, unit, source_warehouse_code, ?, created_at "
                 "FROM store_order_items WHERE canonical_id=202", (env["beta_item_ids"][207],))
    # More than two rows makes retry-per-line regressions visible by connection/query counts.
    _update_line(env, "INSERT INTO store_order_items "
                 "(order_id, canonical_id, quantity, unit, source_warehouse_code, dc_item_id, created_at) "
                 "SELECT order_id, 202, quantity, unit, source_warehouse_code, dc_item_id, created_at "
                 "FROM store_order_items WHERE canonical_id=202")
    with damaged_source(env, "locked"):
        opened = track_sources(monkeypatch)
        calls = []
        real_resolve = sop.resolve_fulfillment_item

        def resolve(*args, **kwargs):
            calls.append(kwargs["warehouse_code"])
            return real_resolve(*args, **kwargs)

        monkeypatch.setattr(sop, "resolve_fulfillment_item", resolve)
        started = time.monotonic()
        info = _info(env)
        elapsed = time.monotonic() - started
        print(f"real exclusive lock: {elapsed:.3f}s, configured waits={[c[3] for c in opened]}ms")
        assert_isolated(env, info)
        assert all(line["dc_available"] == 0 for key, line in info.items()
                   if key != env["lines"][201])
        assert len(opened) == 2  # one for the healthy source, one for all locked rows
        assert calls.count("dc_beta") == 1  # no repeated waits on the same failed connection
        assert all(0 < busy_ms <= 500 for _, _, _, busy_ms in opened)
        # A loose secondary bound; deterministic timeout/open-count assertions carry the contract.
        assert elapsed < 4
        assert_closed(opened)


@pytest.mark.parametrize("damage", ["normal", "empty", "corrupt", "missing_categories"])
def test_normal_and_failed_source_connections_always_close(env, monkeypatch, damage):
    opened = track_sources(monkeypatch)
    if damage == "normal":
        assert _info(env)[env["lines"][202]]["dc_available"] == 25
    else:
        with damaged_source(env, damage):
            assert_isolated(env, _info(env))
    assert len(opened) == 2
    assert_closed(opened)


@pytest.mark.parametrize("phase", ["resolver", "enrichment", "initial", "fallback"])
def test_master_database_errors_are_not_source_fallback(env, monkeypatch, phase):
    opened = track_sources(monkeypatch)
    real_resolve = sop.resolve_fulfillment_item
    with closing(builder.connect(env["master_path"])) as master:
        if phase == "initial":
            master.execute("DROP TABLE store_order_items")
        elif phase == "fallback":
            master.execute("DROP TABLE canonical_categories")
            master.commit()
            _update_line(env, "UPDATE store_order_items SET dc_item_id=-1")
        else:
            def resolve(master_conn, *args, **kwargs):
                if phase == "resolver":
                    master_conn.execute("SELECT * FROM missing_master_table")
                result = real_resolve(master_conn, *args, **kwargs)
                if phase == "enrichment":
                    master_conn.execute("DROP TABLE canonical_conflicts")
                return result
            monkeypatch.setattr(sop, "resolve_fulfillment_item", resolve)
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            sop.get_dc_items_for_order_detail(master, env["order_id"])
    if phase != "initial":
        assert_closed(opened)


@pytest.mark.parametrize("error", [RuntimeError("unexpected resolver defect"),
                                   sqlite3.ProgrammingError("bad binding"),
                                   sqlite3.IntegrityError("write constraint"),
                                   sqlite3.DatabaseError("unexpected db defect")])
def test_unrelated_resolver_errors_propagate_and_close(env, monkeypatch, error):
    opened = track_sources(monkeypatch)

    def broken(*args, **kwargs):
        raise error

    monkeypatch.setattr(sop, "resolve_fulfillment_item", broken)
    with pytest.raises(type(error), match=str(error)):
        _info(env)
    assert_closed(opened)


@pytest.mark.parametrize("sql, match", [("SELEC broken", "syntax error"),
                                       ("UPDATE items SET quantity=0", "readonly")])
def test_source_programming_and_write_errors_propagate(env, monkeypatch, sql, match):
    opened = track_sources(monkeypatch)

    def broken(master, source, **kwargs):
        source.execute(sql)

    monkeypatch.setattr(sop, "resolve_fulfillment_item", broken)
    before = snapshot(env)
    with pytest.raises(sqlite3.OperationalError, match=match):
        _info(env)
    assert snapshot(env) == before
    assert_closed(opened)


def test_source_snapshot_survives_concurrent_committed_change(env, monkeypatch):
    path = env["paths"]["dc_beta"]
    with closing(builder.connect(path)) as setup:
        assert setup.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    opened = track_sources(monkeypatch)
    real_resolve = sop.resolve_fulfillment_item

    def resolve(master, source, **kwargs):
        result = real_resolve(master, source, **kwargs)
        if kwargs["warehouse_code"] == "dc_beta":
            with closing(builder.connect(path)) as writer:
                writer.execute("UPDATE items SET quantity=999, selling_price=999 WHERE canonical_id=202")
                writer.commit()
        return result

    monkeypatch.setattr(sop, "resolve_fulfillment_item", resolve)
    info = _info(env)[env["lines"][202]]
    assert info["dc_available"] == 25
    assert info["unit_price"] == 1.5
    with closing(builder.connect(path)) as check:
        assert check.execute("SELECT quantity FROM items WHERE canonical_id=202").fetchone()[0] == 999
    assert_closed(opened)


@pytest.mark.parametrize("phase", ["begin", "resolver_fetch", "enrichment_fetch"])
def test_source_availability_faults_at_snapshot_and_fetch_close(env, monkeypatch, phase):
    # Exercise less common driver failure points with real connections/cursors.
    # Raise a tagged SQLite corruption error once on the first (alpha) source.
    original_cursor = sqlite3.Cursor
    fired = False

    class FaultCursor(original_cursor):
        def execute(self, sql, *args, **kwargs):
            nonlocal fired
            self.statement = sql
            if phase == "begin" and sql == "BEGIN" and not fired:
                fired = True
                error = sqlite3.DatabaseError("database disk image is malformed")
                error.sqlite_errorcode = sqlite3.SQLITE_CORRUPT
                raise error
            return super().execute(sql, *args, **kwargs)

        def fetchall(self):
            nonlocal fired
            if phase == "resolver_fetch" and "FROM items WHERE" in self.statement and not fired:
                fired = True
                error = sqlite3.DatabaseError("database disk image is malformed")
                error.sqlite_errorcode = sqlite3.SQLITE_CORRUPT
                raise error
            return super().fetchall()

        def fetchone(self):
            nonlocal fired
            if phase == "enrichment_fetch" and "LEFT JOIN categories" in self.statement and not fired:
                fired = True
                error = sqlite3.DatabaseError("database disk image is malformed")
                error.sqlite_errorcode = sqlite3.SQLITE_CORRUPT
                raise error
            return super().fetchone()

    monkeypatch.setattr(sqlite3, "Cursor", FaultCursor)
    opened = track_sources(monkeypatch)
    assert_isolated(env, _info(env), bad=201)
    assert fired
    assert len(opened) == 2
    assert_closed(opened)
