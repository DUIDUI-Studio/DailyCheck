"""Integration tests for /summary custom date range (PRD §2.6).

The view should accept ?start=YYYY-MM-DD&end=YYYY-MM-DD and fall back
to the legacy 7-day window when both are absent. The legacy ?range=
param is preserved for back-compat but ignored (spec §0.1).
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
from datetime import datetime

import pytest


def _login_as_admin(tmp_path, monkeypatch):
    import db as db_module
    from db import init_master_db, init_warehouse_db
    master_path = tmp_path / "master.db"
    wh_path = tmp_path / "wh.db"
    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", tmp_path)
    init_master_db()
    init_warehouse_db(wh_path)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    m = sqlite3.connect(master_path)
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (1, 'admin', 'x', 1, ?)", (ts,))
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, created_at) "
        "VALUES (1, 'wh_t', 'T', ?, ?)", (str(wh_path), ts))
    m.execute(
        "INSERT INTO warehouse_users (user_id, warehouse_id, role) "
        "VALUES (1, 1, 'admin')")
    m.commit()
    m.close()
    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = 1
        s["warehouse_id"] = 1
    return client, wh_path


# ---------------------------------------------------------------------------
# §5.2 integration cases
# ---------------------------------------------------------------------------


def test_summary_200_with_both_dates(tmp_path, monkeypatch):
    """GET /summary?start=2026-06-01&end=2026-06-30 → 200."""
    client, _ = _login_as_admin(tmp_path, monkeypatch)
    resp = client.get("/summary?start=2026-06-01&end=2026-06-30")
    assert resp.status_code == 200
    # The view must surface the parsed dates in the rendered context
    # so the template can label the date range.
    assert "2026-06-01" in resp.data.decode("utf-8")


def test_summary_200_with_start_only(tmp_path, monkeypatch):
    """GET /summary?start=2026-06-01 → 200 (end defaults to today)."""
    client, _ = _login_as_admin(tmp_path, monkeypatch)
    today = _dt.date.today().strftime("%Y-%m-%d")
    resp = client.get("/summary?start=2026-06-01")
    assert resp.status_code == 200
    assert today in resp.data.decode("utf-8")


def test_summary_400_start_after_end(tmp_path, monkeypatch):
    """start > end → 400 + flash."""
    client, _ = _login_as_admin(tmp_path, monkeypatch)
    resp = client.get("/summary?start=2026-06-30&end=2026-06-01", follow_redirects=False)
    assert resp.status_code == 400
    body = resp.data.decode("utf-8")
    assert "开始日期不能晚于结束日期" in body


def test_summary_400_invalid_format(tmp_path, monkeypatch):
    """Invalid start format → 400 + flash."""
    client, _ = _login_as_admin(tmp_path, monkeypatch)
    resp = client.get("/summary?start=not-a-date", follow_redirects=False)
    assert resp.status_code == 400
    body = resp.data.decode("utf-8")
    assert "日期格式应为 YYYY-MM-DD" in body


def test_summary_range_7d_uses_legacy_default(tmp_path, monkeypatch):
    """?range=7d → ignored, falls back to legacy 7d default → 200."""
    client, _ = _login_as_admin(tmp_path, monkeypatch)
    resp = client.get("/summary?range=7d")
    assert resp.status_code == 200
