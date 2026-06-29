"""Tests for the agent_tokens table in master.db.

Subproject 6 (Agent MPC) — PRD §2.3.2 requires a per-platform token
table. This test confirms the schema is created by init_master_db().
"""
from __future__ import annotations

import sqlite3


def test_agent_tokens_table_exists(logged_client):
    """init_master_db() must create the agent_tokens table."""
    master_path, _ = _paths(logged_client)
    conn = sqlite3.connect(master_path)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='agent_tokens'"
    ).fetchall()
    conn.close()
    names = [r[0] for r in rows]
    assert "agent_tokens" in names


def test_agent_tokens_table_columns(logged_client):
    """All columns from spec §2.1 must exist with the right types."""
    master_path, _ = _paths(logged_client)
    conn = sqlite3.connect(master_path)
    cols = {
        r[1]: r[2]
        for r in conn.execute("PRAGMA table_info(agent_tokens)").fetchall()
    }
    conn.close()
    expected = {
        "id", "name", "token_hash", "created_by", "created_at",
        "revoked_at", "allowed_read_paths_json",
        "allowed_write_paths_json", "allowed_warehouse_codes_json",
    }
    assert expected.issubset(set(cols.keys())), f"missing: {expected - set(cols.keys())}"
    # id must be INTEGER PK
    assert "INTEGER" in cols["id"].upper()
    # token_hash must be unique (has UNIQUE constraint)
    conn = sqlite3.connect(master_path)
    idx = {
        r[1]
        for r in conn.execute("PRAGMA index_list(agent_tokens)").fetchall()
    }
    conn.close()
    # PRAGMA index_list returns index names; we only check that some
    # unique index was auto-created. The auto-name is sqlite_autoindex_*.
    assert any("agent_tokens" in i for i in idx), "expected auto index on agent_tokens"


def test_agent_tokens_insert_and_query(logged_client):
    """A basic insert + select round-trip works (token_hash is the PK in
    the logical sense — UNIQUE constraint enforced)."""
    master_path, _ = _paths(logged_client)
    conn = sqlite3.connect(master_path)
    conn.execute(
        """INSERT INTO agent_tokens
           (name, token_hash, created_by, created_at,
            allowed_read_paths_json, allowed_write_paths_json,
            allowed_warehouse_codes_json)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        ("test-tok", "h:abc", 1, "2026-06-29 10:00:00", "[]", "[]", "[]"),
    )
    conn.commit()
    row = conn.execute(
        "SELECT name, token_hash FROM agent_tokens WHERE token_hash=?",
        ("h:abc",),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "test-tok"


def test_agent_tokens_token_hash_unique(logged_client):
    """UNIQUE on token_hash — duplicate insert must fail."""
    master_path, _ = _paths(logged_client)
    conn = sqlite3.connect(master_path)
    conn.execute(
        """INSERT INTO agent_tokens
           (name, token_hash, created_by, created_at) VALUES (?, ?, ?, ?)""",
        ("a", "h:dup", 1, "2026-06-29 10:00:00"),
    )
    conn.commit()
    try:
        conn.execute(
            """INSERT INTO agent_tokens
               (name, token_hash, created_by, created_at) VALUES (?, ?, ?, ?)""",
            ("b", "h:dup", 1, "2026-06-29 10:00:01"),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return
    conn.close()
    raise AssertionError("duplicate token_hash should violate UNIQUE")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _paths(logged_client):
    """Return (master.db path, warehouse db path) for the temp fixture."""
    _, wh_path = logged_client
    # master.db lives next to wh_test.db in tmp_path
    master_path = wh_path.parent.parent / "master.db"
    return master_path, wh_path
