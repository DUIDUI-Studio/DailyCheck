"""Integration tests for Agent MPC write routes (T5).

Covers all 3 write paths from spec §1:
  POST /api/v1/restock               — submit restock request
  POST /api/v1/procurement/recompute  — mark item procurement invalid
  POST /api/v1/forecast/recompute     — manual recompute (idempotent)

Each test: success (write works) + whitelist (POST to a non-writable
path is rejected with 403).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime

import pytest

hashlib.scrypt = lambda *a, **kw: (_ for _ in ()).throw(NotImplementedError())  # type: ignore[attr-defined]

from werkzeug.security import generate_password_hash  # noqa: E402

from tests.conftest import _seed_item  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures (mirror test_agent_mpc_route.py — small, intentional duplication)
# ---------------------------------------------------------------------------


@pytest.fixture
def mpc_client(tmp_path, monkeypatch):
    import db as db_module
    import config as config_module
    master_path = tmp_path / "master.db"
    wh_dir = tmp_path / "warehouses"
    wh_dir.mkdir()
    wh_path = wh_dir / "wh_test.db"
    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_dir)
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", wh_dir)
    monkeypatch.setattr(
        "blueprints.agent_mpc._ACCESS_LOG_PATH", tmp_path / "access.log"
    )

    from db import init_master_db, init_warehouse_db
    init_master_db()
    init_warehouse_db(wh_path)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    m = sqlite3.connect(master_path)
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (1, 'admin', 'x', 1, ?)", (ts,))
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, created_at) "
        "VALUES (1, 'wh_test', 'Test WH', ?, ?)", (str(wh_path), ts))
    m.commit()
    m.close()

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client(), master_path, wh_path


def _make_token(master_path, name, raw_token, *, revoked=False,
                read_paths=(), write_paths=(),
                wh_codes=()):
    h = generate_password_hash(raw_token, method="pbkdf2:sha256")
    conn = sqlite3.connect(master_path)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        """INSERT INTO agent_tokens
           (name, token_hash, created_by, created_at, revoked_at,
            allowed_read_paths_json, allowed_write_paths_json,
            allowed_warehouse_codes_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            name, h, 1, ts,
            ts if revoked else None,
            json.dumps(list(read_paths)),
            json.dumps(list(write_paths)),
            json.dumps(list(wh_codes)),
        ),
    )
    conn.commit()
    token_id = cur.lastrowid
    conn.close()
    return token_id


def _bearer(raw_token):
    return {"Authorization": f"Bearer {raw_token}"}


# ---------------------------------------------------------------------------
# POST /api/v1/restock
# ---------------------------------------------------------------------------


def test_restock_write_success(mpc_client):
    client, master_path, wh_path = mpc_client
    item_id, _ = _seed_item(wh_path, "restA", qty=10, unit_cost=2)
    _make_token(master_path, "t", "tok",
                write_paths=("/api/v1/restock",))
    r = client.post(
        "/api/v1/restock",
        json={
            "warehouse_code": "wh_test",
            "reason": "agent restock",
            "items": [{"item_id": item_id, "qty": 5}],
        },
        headers=_bearer("tok"),
    )
    assert r.status_code == 200, r.data
    body = r.get_json()
    assert body["ok"] is True
    assert body["created_count"] == 1
    # DB side-effect: quantity went up by 5
    conn = sqlite3.connect(wh_path)
    row = conn.execute("SELECT quantity FROM items WHERE id=?", (item_id,)).fetchone()
    conn.close()
    assert row[0] == 15


def test_restock_write_path_not_in_whitelist(mpc_client):
    client, master_path, _ = mpc_client
    _make_token(master_path, "t", "tok",
                write_paths=("/api/v1/forecast/recompute",))  # not /restock
    r = client.post(
        "/api/v1/restock",
        json={"warehouse_code": "wh_test", "items": []},
        headers=_bearer("tok"),
    )
    assert r.status_code == 403


def test_restock_missing_warehouse(mpc_client):
    client, master_path, _ = mpc_client
    _make_token(master_path, "t", "tok",
                write_paths=("/api/v1/restock",))
    r = client.post(
        "/api/v1/restock",
        json={"items": [{"item_id": 1, "qty": 1}]},
        headers=_bearer("tok"),
    )
    assert r.status_code == 400
    assert r.get_json() == {"error": "warehouse_code_required"}


def test_restock_no_items_400(mpc_client):
    client, master_path, _ = mpc_client
    _make_token(master_path, "t", "tok",
                write_paths=("/api/v1/restock",))
    r = client.post(
        "/api/v1/restock",
        json={"warehouse_code": "wh_test", "items": []},
        headers=_bearer("tok"),
    )
    assert r.status_code == 400
    body = r.get_json()
    assert body.get("error") == "no_items"


def test_restock_invalid_qty_400(mpc_client):
    client, master_path, wh_path = mpc_client
    item_id, _ = _seed_item(wh_path, "badQty", qty=10, unit_cost=2)
    _make_token(master_path, "t", "tok",
                write_paths=("/api/v1/restock",))
    r = client.post(
        "/api/v1/restock",
        json={
            "warehouse_code": "wh_test",
            "items": [{"item_id": item_id, "qty": -1}],
        },
        headers=_bearer("tok"),
    )
    assert r.status_code == 400
    body = r.get_json()
    assert "error" in body
    assert body.get("field") in (None, "items")  # field annotation optional


# ---------------------------------------------------------------------------
# POST /api/v1/procurement/recompute
# ---------------------------------------------------------------------------


def test_procurement_recompute_success(mpc_client):
    client, master_path, wh_path = mpc_client
    item_id, _ = _seed_item(wh_path, "procRecA", qty=5, unit_cost=2)
    # Seed a valid cache row
    conn = sqlite3.connect(master_path)
    conn.execute(
        """INSERT INTO procurement_cache
           (item_id, warehouse_code, computed_at, daily_avg, current_qty,
            in_transit_qty, safety_stock, suggested_qty, invalid)
           VALUES (?, 'wh_test', '2026-01-01T00:00:00Z', 1, 5, 0, 14, 9, 0)""",
        (item_id,),
    )
    conn.commit()
    conn.close()
    _make_token(master_path, "t", "tok",
                write_paths=("/api/v1/procurement/recompute",))
    r = client.post(
        "/api/v1/procurement/recompute",
        json={"warehouse_code": "wh_test", "item_id": item_id},
        headers=_bearer("tok"),
    )
    assert r.status_code == 200
    assert r.get_json() == {"ok": True}
    # Cache row should now be invalid
    conn = sqlite3.connect(master_path)
    row = conn.execute(
        "SELECT invalid FROM procurement_cache WHERE item_id=?",
        (item_id,),
    ).fetchone()
    conn.close()
    assert row[0] == 1


def test_procurement_recompute_path_forbidden(mpc_client):
    client, master_path, _ = mpc_client
    _make_token(master_path, "t", "tok",
                write_paths=("/api/v1/restock",))  # not this path
    r = client.post(
        "/api/v1/procurement/recompute",
        json={"warehouse_code": "wh_test", "item_id": 1},
        headers=_bearer("tok"),
    )
    assert r.status_code == 403


def test_procurement_recompute_missing_item_id(mpc_client):
    client, master_path, _ = mpc_client
    _make_token(master_path, "t", "tok",
                write_paths=("/api/v1/procurement/recompute",))
    r = client.post(
        "/api/v1/procurement/recompute",
        json={"warehouse_code": "wh_test"},
        headers=_bearer("tok"),
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# POST /api/v1/forecast/recompute
# ---------------------------------------------------------------------------


def test_forecast_recompute_success(mpc_client):
    client, master_path, _ = mpc_client
    _make_token(master_path, "t", "tok",
                write_paths=("/api/v1/forecast/recompute",))
    r = client.post(
        "/api/v1/forecast/recompute",
        json={"warehouse_code": "wh_test"},
        headers=_bearer("tok"),
    )
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert isinstance(body["last_run_id"], int)
    assert body["last_run_id"] > 0


def test_forecast_recompute_path_forbidden(mpc_client):
    client, master_path, _ = mpc_client
    _make_token(master_path, "t", "tok",
                write_paths=("/api/v1/restock",))  # not this path
    r = client.post(
        "/api/v1/forecast/recompute",
        json={"warehouse_code": "wh_test"},
        headers=_bearer("tok"),
    )
    assert r.status_code == 403
