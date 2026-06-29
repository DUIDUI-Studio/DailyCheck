"""Integration tests for /summary/export with custom date range (PRD §2.6.5).

The export view must accept the same start/end params as /summary.
The CSV filename should include the date range; the body should only
contain data within that range.
"""
from __future__ import annotations

import csv as _csv
import datetime as _dt
import io
import sqlite3
from datetime import datetime


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
# §5.2 export cases
# ---------------------------------------------------------------------------


def test_export_with_date_range(tmp_path, monkeypatch):
    """GET /summary/export?start=2026-06-01&end=2026-06-30 → 200 CSV.

    Filename must include the date range (spec §0.4: summary_<start>_to_<end>.csv).
    """
    client, _ = _login_as_admin(tmp_path, monkeypatch)
    resp = client.get("/summary/export?start=2026-06-01&end=2026-06-30")
    assert resp.status_code == 200
    cd = resp.headers.get("Content-Disposition", "")
    assert "summary_2026-06-01_to_2026-06-30" in cd or "summary-2026-06-01-to-2026-06-30" in cd


def test_export_range_7d_ignored(tmp_path, monkeypatch):
    """?range=7d → ignored, falls back to legacy default; filename keeps 7d tag.

    Per spec §0.1, range= is silently ignored, NOT translated to start/end.
    Filename still uses the 7d token (legacy compatibility).
    """
    client, _ = _login_as_admin(tmp_path, monkeypatch)
    resp = client.get("/summary/export?range=7d")
    assert resp.status_code == 200
    cd = resp.headers.get("Content-Disposition", "")
    # filename pattern: summary-YYYY-MM-DD-7d.csv (legacy)
    assert "-7d.csv" in cd


def test_export_csv_body_filters_by_date_range(tmp_path, monkeypatch):
    """CSV body must reflect the date filter: seed an outbound in-window
    and one out-of-window, verify only the in-window row appears in the
    consumed section.
    """
    client, wh_path = _login_as_admin(tmp_path, monkeypatch)
    conn = sqlite3.connect(wh_path)
    cat_id = conn.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()[0]
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # item
    conn.execute(
        "INSERT INTO items (sku, name, category_id, quantity, safety_stock, unit_cost, unit, gram_per_unit, updated_at) "
        "VALUES ('EXP-DR', 'daterangeItem', ?, 200, 0, 5, '件', 0, ?)",
        (cat_id, ts),
    )
    item_id = conn.execute("SELECT id FROM items WHERE name='daterangeItem'").fetchone()[0]
    # in-window outbound (2026-06-15)
    conn.execute(
        "INSERT INTO outbound_requests (item_id, requested_quantity, reason, rolled_back, created_at) "
        "VALUES (?, 50, NULL, 0, '2026-06-15 10:00:00')",
        (item_id,),
    )
    # out-of-window outbound (2025-12-01, way before window)
    conn.execute(
        "INSERT INTO outbound_requests (item_id, requested_quantity, reason, rolled_back, created_at) "
        "VALUES (?, 999, NULL, 0, '2025-12-01 10:00:00')",
        (item_id,),
    )
    conn.commit()
    conn.close()

    resp = client.get("/summary/export?start=2026-06-01&end=2026-06-30")
    body = resp.data.decode("utf-8-sig")
    # Parse CSV (handle the empty separator rows)
    rows = list(_csv.reader(io.StringIO(body)))
    # Section 3 (消耗排行) should include the in-window 50 amount (250.00)
    # but NOT the out-of-window 999 amount (4995.00).
    flat = " | ".join(",".join(r) for r in rows)
    assert "250.00" in flat, f"Expected in-window 50*5=250.00 in CSV. Got:\n{flat}"
    assert "4995.00" not in flat, f"Out-of-window 999*5=4995.00 leaked into CSV. Got:\n{flat}"
