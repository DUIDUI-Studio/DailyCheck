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
