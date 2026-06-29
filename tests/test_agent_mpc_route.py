"""Integration tests for Agent MPC read routes (T4).

Covers all 9 read paths from spec §1:
  /api/v1/items            GET — list of items
  /api/v1/items/<id>       GET — single item detail
  /api/v1/movements        GET — outbound + stock_movements combined
  /api/v1/forecast/item/<id>  GET — re-use subproject 1 logic
  /api/v1/procurement/store  GET — re-use subproject 2 logic
  /api/v1/procurement/hub    GET — re-use subproject 2 logic
  /api/v1/categories        GET — warehouse db categories
  /api/v1/templates         GET — master.db publish_templates (empty fallback)
  /api/v1/notifications/feed  GET — empty stub (Agents do not consume yet)

Each route has: success / 400 warehouse_code_required / 403 (path or warehouse).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime

import pytest

# Python 3.9 macOS: hashlib.scrypt missing → werkzeug default fails.
# Mirror cli.py's workaround.
hashlib.scrypt = lambda *a, **kw: (_ for _ in ()).throw(NotImplementedError())  # type: ignore[attr-defined]

from werkzeug.security import generate_password_hash  # noqa: E402

from tests.conftest import _seed_item, _seed_outbound  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mpc_client(tmp_path, monkeypatch):
    """A Flask test client with a fresh master.db + a wh_test warehouse,
    ready for Agent MPC. No session is set — MPC routes use Bearer."""
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

    # Silence the access.log writes from polluting the real on-disk log.
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
                read_paths=("/*",), write_paths=("/*",),
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


def _bearer(client, raw_token):
    """Return a helper that adds the Authorization header."""
    return {"Authorization": f"Bearer {raw_token}"}


# ---------------------------------------------------------------------------
# Auth gate — applies to all routes
# ---------------------------------------------------------------------------


def test_no_auth_header_returns_401(mpc_client):
    """Missing Authorization → 401 on every MPC route."""
    client, _, _ = mpc_client
    for path in (
        "/api/v1/items?warehouse_code=wh_test",
        "/api/v1/categories?warehouse_code=wh_test",
    ):
        r = client.get(path)
        assert r.status_code == 401, f"{path} should be 401, got {r.status_code}"


def test_invalid_token_returns_401(mpc_client):
    """Wrong token → 401."""
    client, _, _ = mpc_client
    r = client.get(
        "/api/v1/items?warehouse_code=wh_test",
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# /api/v1/items  (list)
# ---------------------------------------------------------------------------


def test_items_list_success(mpc_client):
    client, master_path, wh_path = mpc_client
    _seed_item(wh_path, "testA", qty=5, unit_cost=2)
    _make_token(master_path, "t", "tok", read_paths=("/api/v1/items",))
    r = client.get(
        "/api/v1/items?warehouse_code=wh_test",
        headers=_bearer(client, "tok"),
    )
    assert r.status_code == 200
    body = r.get_json()
    assert "items" in body
    assert any(it["name"] == "testA" for it in body["items"])


def test_items_list_missing_warehouse_returns_400(mpc_client):
    client, master_path, _ = mpc_client
    _make_token(master_path, "t", "tok", read_paths=("/api/v1/items",))
    r = client.get(
        "/api/v1/items",
        headers=_bearer(client, "tok"),
    )
    assert r.status_code == 400
    assert r.get_json() == {"error": "warehouse_code_required"}


def test_items_list_path_forbidden_returns_403(mpc_client):
    client, master_path, _ = mpc_client
    _make_token(master_path, "t", "tok",
                read_paths=("/api/v1/categories",))  # not /items
    r = client.get(
        "/api/v1/items?warehouse_code=wh_test",
        headers=_bearer(client, "tok"),
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# /api/v1/items/<id>
# ---------------------------------------------------------------------------


def test_items_detail_success(mpc_client):
    client, master_path, wh_path = mpc_client
    item_id, _ = _seed_item(wh_path, "detailA", qty=5, unit_cost=2)
    _make_token(master_path, "t", "tok", read_paths=("/api/v1/items",))
    r = client.get(
        f"/api/v1/items/{item_id}?warehouse_code=wh_test",
        headers=_bearer(client, "tok"),
    )
    assert r.status_code == 200
    body = r.get_json()
    assert body["id"] == item_id
    assert body["name"] == "detailA"


def test_items_detail_not_found(mpc_client):
    client, master_path, _ = mpc_client
    _make_token(master_path, "t", "tok", read_paths=("/api/v1/items",))
    r = client.get(
        "/api/v1/items/99999?warehouse_code=wh_test",
        headers=_bearer(client, "tok"),
    )
    assert r.status_code == 404


def test_items_detail_warehouse_forbidden(mpc_client):
    client, master_path, wh_path = mpc_client
    item_id, _ = _seed_item(wh_path, "detailB", qty=5, unit_cost=2)
    _make_token(master_path, "t", "tok",
                read_paths=("/api/v1/items",), wh_codes=("other_wh",))
    r = client.get(
        f"/api/v1/items/{item_id}?warehouse_code=wh_test",
        headers=_bearer(client, "tok"),
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# /api/v1/movements
# ---------------------------------------------------------------------------


def test_movements_success(mpc_client):
    client, master_path, wh_path = mpc_client
    item_id, _ = _seed_item(wh_path, "movA", qty=10, unit_cost=2)
    _seed_outbound(wh_path, item_id, 3)
    _make_token(master_path, "t", "tok", read_paths=("/api/v1/movements",))
    r = client.get(
        "/api/v1/movements?warehouse_code=wh_test",
        headers=_bearer(client, "tok"),
    )
    assert r.status_code == 200
    body = r.get_json()
    assert "movements" in body
    assert body["warehouse_code"] == "wh_test"
    # The seeded outbound must be present
    types = {m["type"] for m in body["movements"]}
    assert "outbound" in types


def test_movements_missing_warehouse(mpc_client):
    client, master_path, _ = mpc_client
    _make_token(master_path, "t", "tok", read_paths=("/api/v1/movements",))
    r = client.get("/api/v1/movements", headers=_bearer(client, "tok"))
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# /api/v1/forecast/item/<id>
# ---------------------------------------------------------------------------


def test_forecast_item_success(mpc_client):
    client, master_path, wh_path = mpc_client
    item_id, _ = _seed_item(wh_path, "fcA", qty=10, unit_cost=2)
    # 10 outbounds so it leaves cold_start
    for _ in range(10):
        _seed_outbound(wh_path, item_id, 2)
    _make_token(master_path, "t", "tok", read_paths=("/api/v1/forecast",))
    r = client.get(
        f"/api/v1/forecast/item/{item_id}?warehouse_code=wh_test",
        headers=_bearer(client, "tok"),
    )
    assert r.status_code == 200
    body = r.get_json()
    assert body["data_status"] == "ok"
    assert body["daily_avg"] > 0


def test_forecast_item_missing_warehouse(mpc_client):
    client, master_path, _ = mpc_client
    _make_token(master_path, "t", "tok", read_paths=("/api/v1/forecast",))
    r = client.get("/api/v1/forecast/item/1", headers=_bearer(client, "tok"))
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# /api/v1/procurement/store
# ---------------------------------------------------------------------------


def test_procurement_store_success(mpc_client):
    client, master_path, wh_path = mpc_client
    item_id, _ = _seed_item(wh_path, "procA", qty=5, unit_cost=2)
    for _ in range(10):
        _seed_outbound(wh_path, item_id, 2)
    _make_token(master_path, "t", "tok", read_paths=("/api/v1/procurement",))
    r = client.get(
        "/api/v1/procurement/store?warehouse_code=wh_test",
        headers=_bearer(client, "tok"),
    )
    assert r.status_code == 200
    body = r.get_json()
    assert body["warehouse_code"] == "wh_test"
    assert isinstance(body["items"], list)


def test_procurement_store_missing_warehouse(mpc_client):
    client, master_path, _ = mpc_client
    _make_token(master_path, "t", "tok", read_paths=("/api/v1/procurement",))
    r = client.get("/api/v1/procurement/store", headers=_bearer(client, "tok"))
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# /api/v1/procurement/hub
# ---------------------------------------------------------------------------


def test_procurement_hub_success(mpc_client):
    client, master_path, _ = mpc_client
    _make_token(master_path, "t", "tok", read_paths=("/api/v1/procurement",))
    r = client.get(
        "/api/v1/procurement/hub?warehouse_code=wh_test",
        headers=_bearer(client, "tok"),
    )
    assert r.status_code == 200
    body = r.get_json()
    assert "items" in body
    assert isinstance(body["items"], list)


def test_procurement_hub_missing_warehouse(mpc_client):
    client, master_path, _ = mpc_client
    _make_token(master_path, "t", "tok", read_paths=("/api/v1/procurement",))
    r = client.get("/api/v1/procurement/hub", headers=_bearer(client, "tok"))
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# /api/v1/categories
# ---------------------------------------------------------------------------


def test_categories_success(mpc_client):
    client, master_path, _ = mpc_client
    _make_token(master_path, "t", "tok", read_paths=("/api/v1/categories",))
    r = client.get(
        "/api/v1/categories?warehouse_code=wh_test",
        headers=_bearer(client, "tok"),
    )
    assert r.status_code == 200
    body = r.get_json()
    assert "categories" in body
    assert isinstance(body["categories"], list)
    # The init seeded fixed categories → at least one entry
    assert len(body["categories"]) >= 1


def test_categories_missing_warehouse(mpc_client):
    client, master_path, _ = mpc_client
    _make_token(master_path, "t", "tok", read_paths=("/api/v1/categories",))
    r = client.get("/api/v1/categories", headers=_bearer(client, "tok"))
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# /api/v1/templates
# ---------------------------------------------------------------------------


def test_templates_success(mpc_client):
    """templates is platform-level (master.db publish_templates) — no
    warehouse_code required. Returns empty list if no subproject 4 yet."""
    client, master_path, _ = mpc_client
    _make_token(master_path, "t", "tok", read_paths=("/api/v1/templates",))
    r = client.get(
        "/api/v1/templates",
        headers=_bearer(client, "tok"),
    )
    assert r.status_code == 200
    body = r.get_json()
    assert "templates" in body
    assert isinstance(body["templates"], list)


def test_templates_path_forbidden(mpc_client):
    client, master_path, _ = mpc_client
    _make_token(master_path, "t", "tok", read_paths=("/api/v1/items",))
    r = client.get("/api/v1/templates", headers=_bearer(client, "tok"))
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# /api/v1/notifications/feed  (empty stub)
# ---------------------------------------------------------------------------


def test_notifications_feed_returns_empty(mpc_client):
    """Spec: Agent does not consume notifications yet — always empty."""
    client, master_path, _ = mpc_client
    _make_token(master_path, "t", "tok", read_paths=("/api/v1/notifications",))
    r = client.get(
        "/api/v1/notifications/feed",
        headers=_bearer(client, "tok"),
    )
    assert r.status_code == 200
    body = r.get_json()
    assert body.get("events") == []
    assert body.get("unread_count") == 0


def test_notifications_feed_no_warehouse_required(mpc_client):
    """This stub does not require warehouse_code."""
    client, master_path, _ = mpc_client
    _make_token(master_path, "t", "tok", read_paths=("/api/v1/notifications",))
    r = client.get(
        "/api/v1/notifications/feed",
        headers=_bearer(client, "tok"),
    )
    assert r.status_code == 200
