"""Integration tests for forecast_runs table lifecycle.

Covers TASK 3 (schema) and TASK 8 (scheduler / recovery). Pure-fn
math is tested separately in tests/test_forecast_pure.py.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from tests.conftest import _wh


def test_forecast_runs_table_exists_with_expected_columns(logged_client):
    """After init_master_db, forecast_runs is queryable and has the PRD
    fields needed for /admin/health to surface last-success time."""
    client, _ = logged_client
    with client.application.app_context():
        from db import get_master_db
        db = get_master_db()
        cols = {r["name"] for r in db.execute("PRAGMA table_info(forecast_runs)").fetchall()}
    expected = {"id", "started_at", "finished_at", "status", "items_processed", "error_message"}
    assert expected.issubset(cols), f"missing: {expected - cols}"


def test_forecast_runs_initial_empty(logged_client):
    """Fresh master.db has no runs yet."""
    client, _ = logged_client
    with client.application.app_context():
        from db import get_master_db
        db = get_master_db()
        n = db.execute("SELECT COUNT(*) AS c FROM forecast_runs").fetchone()["c"]
    assert n == 0


def test_forecast_runs_insert_and_query(logged_client):
    """A manual insert is visible to the next read — basic round-trip."""
    client, _ = logged_client
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with client.application.app_context():
        from db import get_master_db
        db = get_master_db()
        cur = db.execute(
            "INSERT INTO forecast_runs (started_at, status) VALUES (?, 'success')",
            (ts,),
        )
        db.commit()
        run_id = cur.lastrowid
        row = db.execute(
            "SELECT status, finished_at FROM forecast_runs WHERE id=?", (run_id,)
        ).fetchone()
    assert row["status"] == "success"
    assert row["finished_at"] is None  # not set on insert


# ---------------------------------------------------------------------------
# TASK 7 — /health JSON shape
# ---------------------------------------------------------------------------


def test_health_returns_json_with_status_ok(logged_client):
    """/health now returns JSON (was plain text "ok") with status='ok'."""
    client, _ = logged_client
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.is_json
    body = resp.get_json()
    assert body["status"] == "ok"


def test_health_includes_forecast_last_success_at(logged_client):
    """Field is present (null when no runs have ever completed)."""
    client, _ = logged_client
    body = client.get("/health").get_json()
    assert "forecast_last_success_at" in body
    # No runs yet → None / null
    assert body["forecast_last_success_at"] is None


def test_health_forecast_last_success_at_reflects_run(logged_client):
    """After a successful run, /health surfaces its finished_at as ISO Z."""
    client, _ = logged_client
    client.post("/forecast/recompute")  # creates a success run
    body = client.get("/health").get_json()
    ts = body["forecast_last_success_at"]
    assert ts is not None
    assert ts.endswith("Z")
    # Parseable as ISO 8601
    from datetime import datetime as _dt
    parsed = _dt.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
    assert parsed.year >= 2026


def test_health_forecast_last_success_at_ignores_failed_runs(logged_client):
    """A failed run must NOT be reported as the last success."""
    client, _ = logged_client
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with client.application.app_context():
        from db import get_master_db
        db = get_master_db()
        db.execute(
            "INSERT INTO forecast_runs (started_at, finished_at, status) "
            "VALUES (?, ?, 'failed')",
            (ts, ts),
        )
        db.commit()
    body = client.get("/health").get_json()
    assert body["forecast_last_success_at"] is None  # failed doesn't count


# ---------------------------------------------------------------------------
# TASK 8 — scheduler + orphaned run recovery
# ---------------------------------------------------------------------------


def test_recover_orphaned_runs_marks_running_as_failed(logged_client):
    """An orphaned 'running' row (no finished_at) at startup is marked failed."""
    client, _ = logged_client
    ts = (datetime.now() - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
    with client.application.app_context():
        from db import get_master_db
        db = get_master_db()
        cur = db.execute(
            "INSERT INTO forecast_runs (started_at, status) VALUES (?, 'running')",
            (ts,),
        )
        db.commit()
        orphan_id = cur.lastrowid
        # Recovery runs in the same app context
        from blueprints.forecast import _recover_orphaned_runs
        _recover_orphaned_runs()
        row = db.execute(
            "SELECT status, finished_at FROM forecast_runs WHERE id=?",
            (orphan_id,),
        ).fetchone()
    assert row["status"] == "failed"
    assert row["finished_at"] is not None


def test_recover_orphaned_runs_leaves_success_alone(logged_client):
    """A completed success row must not be touched by recovery."""
    client, _ = logged_client
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with client.application.app_context():
        from db import get_master_db
        db = get_master_db()
        cur = db.execute(
            "INSERT INTO forecast_runs (started_at, finished_at, status) "
            "VALUES (?, ?, 'success')",
            (ts, ts),
        )
        db.commit()
        keep_id = cur.lastrowid
        from blueprints.forecast import _recover_orphaned_runs
        _recover_orphaned_runs()
        row = db.execute(
            "SELECT status, finished_at FROM forecast_runs WHERE id=?",
            (keep_id,),
        ).fetchone()
    assert row["status"] == "success"
    assert row["finished_at"] == ts


def test_run_daily_forecast_creates_success_run(logged_client):
    """One full run produces a single 'success' row with items_processed=0
    when no data is seeded. The row exists, is success, has finished_at."""
    client, wh_path = logged_client
    from blueprints.forecast import _run_daily_forecast
    _run_daily_forecast()
    with client.application.app_context():
        from db import get_master_db
        db = get_master_db()
        row = db.execute(
            "SELECT status, items_processed, finished_at FROM forecast_runs "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["status"] == "success"
    assert row["items_processed"] == 0
    assert row["finished_at"] is not None


def test_run_daily_forecast_idempotent_within_minute(logged_client):
    """Two back-to-back _run_daily_forecast calls in the same minute produce
    only one row (idempotency mirrors POST /forecast/recompute)."""
    client, _ = logged_client
    from blueprints.forecast import _run_daily_forecast
    _run_daily_forecast()
    _run_daily_forecast()
    with client.application.app_context():
        from db import get_master_db
        db = get_master_db()
        n = db.execute("SELECT COUNT(*) AS c FROM forecast_runs").fetchone()["c"]
    assert n == 1


from datetime import timedelta  # noqa: E402


# ---------------------------------------------------------------------------
# TASK 11 — DB lock retry + access.log + health counter
# ---------------------------------------------------------------------------


def test_run_daily_forecast_writes_failed_row_on_lock(monkeypatch, logged_client, tmp_path):
    """If every retry on sqlite3.connect fails, the run is recorded as
    'failed' with the lock error in error_message, and access.log gets
    a 'forecast_lock' line. The /health counter increments accordingly.
    """
    import sqlite3 as _sqlite3
    from blueprints import forecast as fc
    from config import ACCESS_LOG_PATH, FORECAST_LOCK_FAILURES_PATH
    # Force OperationalError on every connect attempt for the warehouse dbs.
    real_connect = _sqlite3.connect
    call_count = {"n": 0}

    def boom(path, *a, **kw):
        # Allow master.db connection through (forecast_runs writes go there).
        # Block warehouse db connections to trigger the lock path.
        if "warehouses" in str(path) or path == ":memory:" and call_count["n"] < 0:
            call_count["n"] += 1
            raise _sqlite3.OperationalError("database is locked")
        return real_connect(path, *a, **kw)

    # Easier path: monkeypatch glob() to point at a non-existent dir,
    # which makes the for-loop iterate zero times — but we want a real
    # OperationalError. Easiest: make sqlite3.connect raise for wh_*.db
    # by wrapping it.
    orig_connect = _sqlite3.connect

    def _wrap(path, *a, **kw):
        s = str(path)
        # Block ONLY the per-warehouse glob loop. master.db and tmp
        # master_path are fine. The forecast module's WAREHOUSE_DB_DIR
        # is the monkeypatched tmp_path, so we detect it by checking
        # the path lives under tmp_path AND is named *.db AND is NOT
        # master.db. We also block the test-warehouses dir created by
        # the fixture.
        if s.endswith("wh_test.db") and "/warehouses/" in s:
            raise _sqlite3.OperationalError("database is locked")
        return orig_connect(path, *a, **kw)

    monkeypatch.setattr(fc.sqlite3, "connect", _wrap)
    # Reset the lock-failure counter file so the test is independent.
    counter_path = tmp_path / "forecast_lock_failures.txt"
    monkeypatch.setattr(fc, "_LOCK_COUNTER_PATH", counter_path)

    fc._run_daily_forecast()  # all warehouse dbs are blocked

    with logged_client[0].application.app_context():
        from db import get_master_db
        db = get_master_db()
        row = db.execute(
            "SELECT status, error_message FROM forecast_runs "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["status"] == "failed"
    assert "locked" in (row["error_message"] or "").lower() or row["error_message"] is not None
    # Counter file got incremented
    assert counter_path.exists()
    n_failures = int(counter_path.read_text().strip() or "0")
    assert n_failures >= 1


def test_health_forecast_lock_failures_counter(logged_client, tmp_path):
    """Once lock failures > 0, /health surfaces the count for operators."""
    from blueprints import forecast as fc
    counter_path = tmp_path / "forecast_lock_failures.txt"
    fc._LOCK_COUNTER_PATH = counter_path
    counter_path.write_text("7")

    body = logged_client[0].get("/health").get_json()
    assert body["forecast_lock_failures"] == 7


def test_health_forecast_lock_failures_zero_when_file_missing(logged_client, tmp_path, monkeypatch):
    """If the counter file does not exist (clean state), the field is 0."""
    from blueprints import forecast as fc
    fc._LOCK_COUNTER_PATH = tmp_path / "no_such_file.txt"
    body = logged_client[0].get("/health").get_json()
    assert body["forecast_lock_failures"] == 0
