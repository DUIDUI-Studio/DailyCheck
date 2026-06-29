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
