"""Integration tests for Agent MPC auth helpers (T3).

verify_token() reads `Authorization: Bearer <token>`, looks up the
hash in master.db, and returns the row (or None). check_path_allowed()
uses path_matches() against the row's allowed_read/write_paths_json.

We test these via a tiny Flask app (the real one is wired up in T4)
because the helpers depend on `flask.request` and the master db.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime

import pytest

# Python 3.9 lacks hashlib.scrypt on macOS; werkzeug tries to use it
# as default. Force pbkdf2 for the test (mirrors cli.py workaround).
hashlib.scrypt = lambda *a, **kw: (_ for _ in ()).throw(NotImplementedError())  # type: ignore[attr-defined]

from werkzeug.security import generate_password_hash  # noqa: E402

from blueprints.agent_mpc import (  # noqa: E402
    check_path_allowed, check_warehouse_allowed, verify_token,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_token_row(master_path, name, raw_token, *, revoked=False,
                    read_paths=("/*",), write_paths=("/*",),
                    wh_codes=("[]",)):
    """Insert a row into agent_tokens and return (id, raw_token, row_dict)."""
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
            wh_codes if isinstance(wh_codes, str) else json.dumps(list(wh_codes)),
        ),
    )
    conn.commit()
    token_id = cur.lastrowid
    conn.close()
    return token_id


def _install_app(monkeypatch, master_path, wh_path):
    """Re-create a fresh Flask app with the given master.db path.

    Uses the same conftest-style monkeypatch as logged_client but
    without seeding users/warehouses. We don't need them — these tests
    only exercise the MPC helpers, which talk to master.db directly.
    """
    import db as db_module
    import config as config_module
    wh_dir = wh_path.parent
    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_dir)
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", wh_dir)

    from db import init_master_db
    init_master_db()

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app


@pytest.fixture
def mpc_app(tmp_path, monkeypatch):
    """Fresh app + master.db for auth-helper tests (no logged user)."""
    master_path = tmp_path / "master.db"
    wh_dir = tmp_path / "warehouses"
    wh_dir.mkdir()
    wh_path = wh_dir / "wh_test.db"
    app = _install_app(monkeypatch, master_path, wh_path)
    return app, master_path, wh_path


# ---------------------------------------------------------------------------
# verify_token
# ---------------------------------------------------------------------------


def test_verify_token_missing_header(mpc_app):
    """No Authorization header → None."""
    app, _, _ = mpc_app
    with app.test_request_context("/", method="GET"):
        assert verify_token() is None


def test_verify_token_wrong_token(mpc_app):
    """A garbage token that doesn't match any row → None."""
    app, master_path, _ = mpc_app
    _make_token_row(master_path, "real", "correct-token",
                    read_paths=("/*",), write_paths=("/*",))
    with app.test_request_context(
        "/", method="GET",
        headers={"Authorization": "Bearer wrong-token"},
    ):
        assert verify_token() is None


def test_verify_token_revoked(mpc_app):
    """Revoked token → None (even if hash matches)."""
    app, master_path, _ = mpc_app
    _make_token_row(master_path, "rev", "secret",
                    revoked=True,
                    read_paths=("/*",), write_paths=("/*",))
    with app.test_request_context(
        "/", method="GET",
        headers={"Authorization": "Bearer secret"},
    ):
        assert verify_token() is None


def test_verify_token_valid_returns_row(mpc_app):
    """Valid header + non-revoked token → row dict with id, name, etc."""
    app, master_path, _ = mpc_app
    _make_token_row(master_path, "good", "good-token",
                    read_paths=("/*",), write_paths=("/*",))
    with app.test_request_context(
        "/", method="GET",
        headers={"Authorization": "Bearer good-token"},
    ):
        row = verify_token()
    assert row is not None
    assert row["name"] == "good"
    assert isinstance(row["id"], int)
    # JSON list columns must be parsed back to Python lists
    assert isinstance(row["allowed_read_paths_json"], str)
    # parse it
    parsed = json.loads(row["allowed_read_paths_json"])
    assert parsed == ["/*"]


def test_verify_token_wrong_scheme(mpc_app):
    """`Basic xyz` or other non-Bearer scheme → None."""
    app, master_path, _ = mpc_app
    _make_token_row(master_path, "good", "good-token",
                    read_paths=("/*",), write_paths=("/*",))
    with app.test_request_context(
        "/", method="GET",
        headers={"Authorization": "Basic good-token"},
    ):
        assert verify_token() is None


# ---------------------------------------------------------------------------
# check_path_allowed
# ---------------------------------------------------------------------------


def test_check_path_allowed_read_match():
    """read_paths=['/api/v1/*'] matches GET /api/v1/items."""
    row = {
        "allowed_read_paths_json": json.dumps(["/api/v1/*"]),
        "allowed_write_paths_json": json.dumps([]),
        "allowed_warehouse_codes_json": json.dumps([]),
    }
    assert check_path_allowed(row, "GET", "/api/v1/items") is True


def test_check_path_allowed_read_miss():
    """Path not in read list → False."""
    row = {
        "allowed_read_paths_json": json.dumps(["/api/v1/items"]),
        "allowed_write_paths_json": json.dumps(["/api/v1/restock"]),
        "allowed_warehouse_codes_json": json.dumps([]),
    }
    assert check_path_allowed(row, "GET", "/api/v1/categories") is False


def test_check_path_allowed_write_match():
    """write_paths=['/api/v1/restock'] matches POST /api/v1/restock."""
    row = {
        "allowed_read_paths_json": json.dumps([]),
        "allowed_write_paths_json": json.dumps(["/api/v1/restock"]),
        "allowed_warehouse_codes_json": json.dumps([]),
    }
    assert check_path_allowed(row, "POST", "/api/v1/restock") is True


def test_check_path_allowed_post_uses_write_paths():
    """A POST must be authorized by write_paths, NOT read_paths."""
    row = {
        "allowed_read_paths_json": json.dumps(["/api/v1/restock"]),
        "allowed_write_paths_json": json.dumps([]),
        "allowed_warehouse_codes_json": json.dumps([]),
    }
    # Even though the path is in the read list, POST must be authorized
    # by the write list.
    assert check_path_allowed(row, "POST", "/api/v1/restock") is False


# ---------------------------------------------------------------------------
# check_warehouse_allowed
# ---------------------------------------------------------------------------


def test_check_warehouse_allowed_empty_list_means_all():
    """An empty allowed_warehouse_codes_json list authorizes ALL warehouses."""
    row = {"allowed_warehouse_codes_json": json.dumps([])}
    assert check_warehouse_allowed(row, "wh_test") is True
    assert check_warehouse_allowed(row, "any_other") is True


def test_check_warehouse_allowed_specific_match():
    row = {"allowed_warehouse_codes_json": json.dumps(["wh_test", "wh_test2"])}
    assert check_warehouse_allowed(row, "wh_test") is True
    assert check_warehouse_allowed(row, "wh_test2") is True
    assert check_warehouse_allowed(row, "wh_other") is False


def test_check_warehouse_allowed_none():
    """Missing warehouse_code → False (caller already 400'd this)."""
    row = {"allowed_warehouse_codes_json": json.dumps(["wh_test"])}
    assert check_warehouse_allowed(row, None) is False
