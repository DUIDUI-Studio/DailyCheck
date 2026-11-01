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
    elif damage.startswith("schema_"):
        corrupt_stored_schema(path, damage)
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
                                    "missing_categories", "locked", "missing", "unopenable",
                                    "schema_incomplete", "schema_no_suffix", "schema_syntax",
                                    "schema_duplicate", "schema_punctuation"])
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


@pytest.mark.parametrize("damage", ["locked", "schema_incomplete", "schema_no_suffix"])
def test_many_lines_one_unavailable_source_has_one_small_wait_and_closes(env, monkeypatch, damage):
    _update_line(env, "INSERT INTO store_order_items "
                 "(order_id, canonical_id, quantity, unit, source_warehouse_code, dc_item_id, created_at) "
                 "SELECT order_id, 207, quantity, unit, source_warehouse_code, ?, created_at "
                 "FROM store_order_items WHERE canonical_id=202", (env["beta_item_ids"][207],))
    # More than two rows makes retry-per-line regressions visible by connection/query counts.
    _update_line(env, "INSERT INTO store_order_items "
                 "(order_id, canonical_id, quantity, unit, source_warehouse_code, dc_item_id, created_at) "
                 "SELECT order_id, 202, quantity, unit, source_warehouse_code, dc_item_id, created_at "
                 "FROM store_order_items WHERE canonical_id=202")
    with damaged_source(env, damage):
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
        print(f"real source {damage}: {elapsed:.3f}s, configured waits={[c[3] for c in opened]}ms")
        assert_isolated(env, info)
        assert all(line["dc_available"] == 0 for key, line in info.items()
                   if key != env["lines"][201])
        assert len(opened) == 2  # one for the healthy source, one for all locked rows
        assert calls.count("dc_beta") == 1  # no repeated waits on the same failed connection
        assert all(0 < busy_ms <= 500 for _, _, _, busy_ms in opened)
        # A loose secondary bound; deterministic timeout/open-count assertions carry the contract.
        assert elapsed < 4
        assert_closed(opened)


@pytest.mark.parametrize("damage", ["normal", "empty", "corrupt", "missing_categories",
                                    "schema_incomplete", "schema_no_suffix", "schema_syntax",
                                    "schema_duplicate", "schema_punctuation"])
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
                raise error
            return super().execute(sql, *args, **kwargs)

        def fetchall(self):
            nonlocal fired
            if phase == "resolver_fetch" and "FROM items WHERE" in self.statement and not fired:
                fired = True
                error = sqlite3.DatabaseError("database disk image is malformed")
                raise error
            return super().fetchall()

        def fetchone(self):
            nonlocal fired
            if phase == "enrichment_fetch" and "LEFT JOIN categories" in self.statement and not fired:
                fired = True
                error = sqlite3.DatabaseError("database disk image is malformed")
                raise error
            return super().fetchone()

    monkeypatch.setattr(sqlite3, "Cursor", FaultCursor)
    opened = track_sources(monkeypatch)
    assert_isolated(env, _info(env), bad=201)
    assert fired
    assert len(opened) == 2
    assert_closed(opened)



@pytest.mark.parametrize("error_type", [sqlite3.ProgrammingError, sqlite3.IntegrityError,
                                       sqlite3.InternalError])
def test_legacy_metadata_free_programming_errors_are_not_availability(env, monkeypatch, error_type):
    original_cursor = sqlite3.Cursor

    class BrokenCursor(original_cursor):
        def execute(self, sql, *args, **kwargs):
            if "FROM items WHERE" in sql:
                # A known availability-looking message must not override error type.
                raise error_type("database is locked")
            return super().execute(sql, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "Cursor", BrokenCursor)
    opened = track_sources(monkeypatch)
    with pytest.raises(error_type, match="database is locked"):
        _info(env)
    assert_closed(opened)


SCHEMA_VARIANTS = {
    "schema_incomplete": ("items", "CREATE TABLE items("),
    "schema_no_suffix": ("items", "invalid schema SQL"),
    "schema_syntax": ("items", "CREATE TABL items(id INTEGER)"),
    "schema_duplicate": ("items", "CREATE TABLE items(id INTEGER, id TEXT)"),
    "schema_punctuation": ("items) - odd", 'CREATE TABLE "items) - odd"('),
}


def corrupt_stored_schema(path, variant):
    name, stored_sql = SCHEMA_VARIANTS[variant]
    with closing(builder.connect(path)) as conn:
        if name != "items":
            conn.execute('ALTER TABLE items RENAME TO "items) - odd"')
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute("UPDATE sqlite_master SET sql=? WHERE name=?", (stored_sql, name))
        conn.commit()


def source_read_injection(env, monkeypatch, error):
    """Raise the exact supplied object inside a source cursor read boundary."""
    original_cursor = sqlite3.Cursor

    class InjectedCursor(original_cursor):
        def execute(self, sql, *args, **kwargs):
            if "FROM items WHERE" in sql:
                raise error
            return super().execute(sql, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "Cursor", InjectedCursor)
    return track_sources(monkeypatch)


@pytest.mark.parametrize("error_type", [sqlite3.ProgrammingError, sqlite3.IntegrityError,
                                       sqlite3.InternalError, sqlite3.DataError, RuntimeError])
@pytest.mark.parametrize("message", ["malformed database schema (items)",
                                    'malformed database schema (odd) - near "TABL": syntax error'])
@pytest.mark.parametrize("code", [None, 11])
def test_schema_envelope_never_overrides_negative_type_identity(env, monkeypatch, error_type, message, code):
    error = error_type(message)
    if code is not None:
        error.sqlite_errorcode = code
    opened = source_read_injection(env, monkeypatch, error)
    with pytest.raises(error_type) as caught:
        _info(env)
    assert caught.value is error
    assert_closed(opened)


@pytest.mark.parametrize("error_type", [sqlite3.DatabaseError, sqlite3.OperationalError])
@pytest.mark.parametrize("code", [1, 8, 9, 23, 99])
def test_schema_envelope_cannot_override_negative_metadata_identity(env, monkeypatch, error_type, code):
    error = error_type("malformed database schema (items) - incomplete input")
    error.sqlite_errorcode = code
    opened = source_read_injection(env, monkeypatch, error)
    with pytest.raises(error_type) as caught:
        _info(env)
    assert caught.value is error
    assert_closed(opened)


@pytest.mark.parametrize("error_type", [sqlite3.DatabaseError, sqlite3.OperationalError])
@pytest.mark.parametrize("code", [11, 523])
def test_primary_and_extended_corruption_metadata_still_isolate(env, monkeypatch, error_type, code):
    original_cursor = sqlite3.Cursor
    error = error_type("opaque engine detail")
    error.sqlite_errorcode = code
    fired = False

    class InjectedCursor(original_cursor):
        def execute(self, sql, *args, **kwargs):
            nonlocal fired
            if "FROM items WHERE" in sql and not fired:
                fired = True
                raise error
            return super().execute(sql, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "Cursor", InjectedCursor)
    opened = track_sources(monkeypatch)
    assert_isolated(env, _info(env), bad=201)
    assert fired
    assert_closed(opened)


@pytest.mark.parametrize("error_type", [sqlite3.DatabaseError, sqlite3.OperationalError])
@pytest.mark.parametrize("message", ["incomplete input", 'near "TABL": syntax error',
                                    "malformed database schema", "prefix malformed database schema (items)",
                                    "malformed JSON", "unexpected db defect", "database is locked unexpectedly",
                                    "unknown operation", "incorrect binding"])
def test_unknown_legacy_messages_keep_exception_identity(env, monkeypatch, error_type, message):
    error = error_type(message)
    opened = source_read_injection(env, monkeypatch, error)
    with pytest.raises(error_type) as caught:
        _info(env)
    assert caught.value is error
    assert_closed(opened)


@pytest.mark.parametrize("variant", ["schema_incomplete", "schema_no_suffix"])
def test_real_schema_corruption_from_master_inside_resolver_is_not_tagged(env, monkeypatch, tmp_path, variant):
    path = tmp_path / "scratch-master.db"
    with closing(builder.connect(path)) as master:
        master.execute("CREATE TABLE items(id INTEGER)")
        master.commit()
    corrupt_stored_schema(path, variant)
    observed = []
    real_resolve = sop.resolve_fulfillment_item
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as scratch_master:
        def resolve(master, source, **kwargs):
            result = real_resolve(master, source, **kwargs)
            if kwargs["warehouse_code"] == "dc_beta":
                try:
                    scratch_master.execute("SELECT * FROM items").fetchall()
                except sqlite3.DatabaseError as error:
                    observed.append(error)
                    raise
            return result

        monkeypatch.setattr(sop, "resolve_fulfillment_item", resolve)
        opened = track_sources(monkeypatch)
        before = snapshot(env)
        with pytest.raises(sqlite3.DatabaseError) as caught:
            _info(env)
        assert len(observed) == 1 and caught.value is observed[0]
        assert str(caught.value).startswith("malformed database schema (")
        assert snapshot(env) == before
        assert_closed(opened)


@pytest.mark.parametrize("variant", [None, "schema_incomplete"])
def test_actual_source_writes_propagate_identity_even_when_corrupt(env, monkeypatch, variant):
    if variant is not None:
        corrupt_stored_schema(env["paths"]["dc_beta"], variant)
    original_cursor = sqlite3.Cursor
    real_resolve = sop.resolve_fulfillment_item
    observed = []

    class WriteCursor(original_cursor):
        def execute(self, sql, *args, **kwargs):
            try:
                return super().execute(sql, *args, **kwargs)
            except sqlite3.DatabaseError as error:
                if sql.startswith("UPDATE items"):
                    observed.append(error)
                raise

    def resolve(master, source, **kwargs):
        if kwargs["warehouse_code"] == "dc_beta":
            source.execute("UPDATE items SET quantity=0")
        return real_resolve(master, source, **kwargs)

    monkeypatch.setattr(sqlite3, "Cursor", WriteCursor)
    monkeypatch.setattr(sop, "resolve_fulfillment_item", resolve)
    opened = track_sources(monkeypatch)
    before = snapshot(env)
    with pytest.raises(sqlite3.DatabaseError) as caught:
        _info(env)
    assert len(observed) == 1 and caught.value is observed[0]
    if variant is None:
        assert "readonly" in str(caught.value)
    else:
        assert str(caught.value).startswith("malformed database schema (")
    if hasattr(caught.value, "sqlite_errorcode"):
        assert caught.value.sqlite_errorcode & 255 == (8 if variant is None else 11)
    assert snapshot(env) == before
    assert_closed(opened)


@pytest.mark.parametrize("error_type", [sqlite3.DatabaseError, sqlite3.OperationalError])
@pytest.mark.parametrize("message", ["malformed database schema (another_object)",
                                    "malformed database schema () - incomplete input",
                                    'malformed database schema (odd) - near "TABL": syntax error',
                                    "malformed database schema (items) - duplicate column name: id",
                                    "malformed database schema (items) - invalid rootpage",
                                    "malformed database schema (items) - unknown extension diagnostic"])
def test_metadata_free_schema_family_isolates_without_hardcoded_object_or_reason(env, monkeypatch, error_type, message):
    original_cursor = sqlite3.Cursor
    error = error_type(message)
    fired = False

    class InjectedCursor(original_cursor):
        def execute(self, sql, *args, **kwargs):
            nonlocal fired
            if "FROM items WHERE" in sql and args and args[0] == (202,):
                fired = True
                raise error
            return super().execute(sql, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "Cursor", InjectedCursor)
    opened = track_sources(monkeypatch)
    assert_isolated(env, _info(env))
    assert fired
    assert_closed(opened)
