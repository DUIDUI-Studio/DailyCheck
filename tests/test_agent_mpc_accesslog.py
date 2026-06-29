"""Tests for the Agent MPC access.log JSON appender (T6).

Spec §4.1: every MPC call must append one JSON line to access.log with
{ts, agent_token_id, path, method, status, duration_ms}. The path is
tested via monkeypatching the module-level constant.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime

import pytest

hashlib = __import__("hashlib")
hashlib.scrypt = lambda *a, **kw: (_ for _ in ()).throw(NotImplementedError())  # type: ignore[attr-defined]

from werkzeug.security import generate_password_hash  # noqa: E402

from blueprints.agent_mpc import _write_mpc_access_log  # noqa: E402


def test_access_log_writes_one_json_line(tmp_path, monkeypatch):
    """Calling the helper appends exactly one valid JSON line to the file."""
    log_path = tmp_path / "access.log"
    monkeypatch.setattr("blueprints.agent_mpc._ACCESS_LOG_PATH", log_path)
    _write_mpc_access_log(
        token_id=42, method="GET", path="/api/v1/items",
        status=200, duration_ms=15,
    )
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["agent_token_id"] == 42
    assert rec["method"] == "GET"
    assert rec["path"] == "/api/v1/items"
    assert rec["status"] == 200
    assert rec["duration_ms"] == 15
    assert "ts" in rec


def test_access_log_appends_multiple_lines(tmp_path, monkeypatch):
    """Successive calls append (not overwrite)."""
    log_path = tmp_path / "access.log"
    monkeypatch.setattr("blueprints.agent_mpc._ACCESS_LOG_PATH", log_path)
    _write_mpc_access_log(1, "GET", "/api/v1/items", 200, 5)
    _write_mpc_access_log(1, "GET", "/api/v1/categories", 200, 7)
    _write_mpc_access_log(2, "POST", "/api/v1/restock", 401, 3)
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    recs = [json.loads(l) for l in lines]
    assert [r["path"] for r in recs] == [
        "/api/v1/items", "/api/v1/categories", "/api/v1/restock",
    ]


def test_access_log_handles_null_token(tmp_path, monkeypatch):
    """A failed auth (no token verified) records agent_token_id=None."""
    log_path = tmp_path / "access.log"
    monkeypatch.setattr("blueprints.agent_mpc._ACCESS_LOG_PATH", log_path)
    _write_mpc_access_log(None, "GET", "/api/v1/items", 401, 2)
    rec = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert rec["agent_token_id"] is None
    assert rec["status"] == 401


def test_access_log_never_crashes_on_permission_error(tmp_path, monkeypatch):
    """The helper must NEVER raise — observability must not break the route.

    If the log file is unwritable (e.g. a directory), the helper must
    swallow the OSError and let the request finish normally. This is
    critical because the spec requires every call to log, but operators
    must still see responses when log infra is broken.
    """
    # Point at a path inside a non-existent directory → writes will fail.
    log_path = tmp_path / "nonexistent_subdir" / "access.log"
    monkeypatch.setattr("blueprints.agent_mpc._ACCESS_LOG_PATH", log_path)
    # Should not raise
    _write_mpc_access_log(1, "GET", "/api/v1/items", 200, 5)
