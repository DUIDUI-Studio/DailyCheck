"""Integration tests for the /forecast blueprint.

Covers TASK 4 (item route), TASK 5 (product route), TASK 6 (manual
recompute), TASK 9 (role gate). UI / scheduler / E2E are tested elsewhere.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from tests.conftest import _seed_item, _seed_outbound, _seed_production_consumption


def _login_as(client, user_id=1, warehouse_id=1):
    """logged_client fixture already provides an admin session, but several
    tests need a different role. Use this helper for role-mutation tests."""
    with client.session_transaction() as s:
        s["user_id"] = user_id
        s["warehouse_id"] = warehouse_id


def _bind_role(client, wh_path, master_path, user_id, warehouse_id, role):
    """Insert/replace the warehouse_users row for a given user/warehouse."""
    import sqlite3
    m = sqlite3.connect(master_path)
    m.execute(
        "INSERT OR REPLACE INTO warehouse_users (user_id, warehouse_id, role) "
        "VALUES (?, ?, ?)",
        (user_id, warehouse_id, role),
    )
    m.commit()
    m.close()


# ---------------------------------------------------------------------------
# TASK 4 — /forecast/item/<id>
# ---------------------------------------------------------------------------


def test_forecast_item_unknown_id_returns_404(logged_client):
    client, _ = logged_client
    resp = client.get("/forecast/item/99999")
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "not_found"}


def test_forecast_item_no_outbound_is_cold_start(logged_client):
    """Item exists but no outbound in last 30d → cold_start, zeros."""
    client, wh_path = logged_client
    item_id, _ = _seed_item(wh_path, "coldA", qty=10, unit_cost=5)
    resp = client.get(f"/forecast/item/{item_id}")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["data_status"] == "cold_start"
    assert body["daily_avg"] == 0
    assert body["forecast_total"] == 0
    assert body["horizon_days"] == 14
    assert body["confidence"] == "cold_start"


def test_forecast_item_default_horizon_14(logged_client):
    client, wh_path = logged_client
    item_id, _ = _seed_item(wh_path, "h14", qty=10, unit_cost=5)
    # 7 outbounds of qty=2 each (n=7 → low confidence, not cold_start)
    for _ in range(7):
        _seed_outbound(wh_path, item_id, 2)
    resp = client.get(f"/forecast/item/{item_id}")
    body = resp.get_json()
    assert body["horizon_days"] == 14


def test_forecast_item_with_outbound_returns_numbers(logged_client):
    """7+ outbounds → ok, non-zero daily_avg, forecast_total = avg * horizon."""
    client, wh_path = logged_client
    item_id, _ = _seed_item(wh_path, "okA", qty=10, unit_cost=5)
    for _ in range(10):
        _seed_outbound(wh_path, item_id, 3)
    resp = client.get(f"/forecast/item/{item_id}?horizon_days=14")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["data_status"] == "ok"
    assert body["daily_avg"] > 0
    assert body["forecast_total"] == pytest.approx(body["daily_avg"] * 14, abs=0.01)
    assert body["confidence"] in ("low", "medium", "high")
    assert "warehouse_code" in body
    assert "computed_at" in body


def test_forecast_item_invalid_horizon_zero(logged_client):
    client, wh_path = logged_client
    item_id, _ = _seed_item(wh_path, "invA", qty=10, unit_cost=5)
    resp = client.get(f"/forecast/item/{item_id}?horizon_days=0")
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "invalid_horizon"}


def test_forecast_item_invalid_horizon_too_large(logged_client):
    client, wh_path = logged_client
    item_id, _ = _seed_item(wh_path, "invB", qty=10, unit_cost=5)
    resp = client.get(f"/forecast/item/{item_id}?horizon_days=91")
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "invalid_horizon"}


def test_forecast_item_invalid_horizon_non_integer(logged_client):
    client, wh_path = logged_client
    item_id, _ = _seed_item(wh_path, "invC", qty=10, unit_cost=5)
    resp = client.get(f"/forecast/item/{item_id}?horizon_days=abc")
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "invalid_horizon"}


def test_forecast_item_custom_horizon(logged_client):
    client, wh_path = logged_client
    item_id, _ = _seed_item(wh_path, "h30", qty=10, unit_cost=5)
    for _ in range(10):
        _seed_outbound(wh_path, item_id, 3)
    resp = client.get(f"/forecast/item/{item_id}?horizon_days=30")
    body = resp.get_json()
    assert body["horizon_days"] == 30
    assert body["forecast_total"] == pytest.approx(body["daily_avg"] * 30, abs=0.01)


def test_forecast_item_excludes_rolled_back_outbound(logged_client):
    """Rolled-back outbounds are not counted as consumption (PRD §2.1.3
    spec-self-decision: outbound_requests.rolled_back=0 only)."""
    client, wh_path = logged_client
    item_id, _ = _seed_item(wh_path, "rbA", qty=10, unit_cost=5)
    import sqlite3
    _seed_outbound(wh_path, item_id, 5)  # active: n=1 → cold_start
    # Add a rolled-back one — must be ignored
    conn = sqlite3.connect(wh_path)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO outbound_requests (item_id, requested_quantity, rolled_back, status, created_at) "
        "VALUES (?, 99, 1, '回滚', ?)",
        (item_id, ts),
    )
    conn.commit()
    conn.close()
    resp = client.get(f"/forecast/item/{item_id}")
    body = resp.get_json()
    # Only the 1 active outbound exists → cold_start (n=1, < 7)
    assert body["data_status"] == "cold_start"
    assert body["daily_avg"] == 0


def test_forecast_item_response_shape_stable(logged_client):
    """Lock the JSON contract: every documented key must be present, every
    value must have the right type. A schema-drift here breaks the Agent
    MPC consumer (subproject 6)."""
    client, wh_path = logged_client
    item_id, _ = _seed_item(wh_path, "shape", qty=10, unit_cost=5)
    resp = client.get(f"/forecast/item/{item_id}")
    body = resp.get_json()
    expected_keys = {
        "item_id", "warehouse_code", "horizon_days", "daily_avg",
        "forecast_total", "confidence", "computed_at", "data_status",
    }
    assert expected_keys.issubset(set(body.keys())), f"missing: {expected_keys - set(body.keys())}"
    assert isinstance(body["item_id"], int)
    assert isinstance(body["warehouse_code"], str)
    assert isinstance(body["horizon_days"], int)
    assert isinstance(body["daily_avg"], (int, float))
    assert isinstance(body["forecast_total"], (int, float))
    assert body["confidence"] in ("cold_start", "low", "medium", "high")
    assert body["data_status"] in ("ok", "cold_start")
    # computed_at must be ISO 8601 ending in Z
    assert body["computed_at"].endswith("Z")
