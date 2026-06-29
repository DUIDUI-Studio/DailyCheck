"""Quick-range buttons on /summary (PRD §2.6 + spec §6).

The page must render 6 quick-range buttons (本周/上周/本月/上月/本季/本年)
that trigger client-side navigation to ?start=...&end=.... The static JS
file `static/summary_dates.js` carries the date math.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

BUTTON_LABELS = ["本周", "上周", "本月", "上月", "本季", "本年"]


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


def test_summary_page_has_all_six_quick_buttons(tmp_path, monkeypatch):
    """All 6 button labels (本周/上周/本月/上月/本季/本年) must render."""
    client, _ = _login_as_admin(tmp_path, monkeypatch)
    resp = client.get("/summary")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    for label in BUTTON_LABELS:
        assert label in body, f"Quick button '{label}' missing from /summary"
