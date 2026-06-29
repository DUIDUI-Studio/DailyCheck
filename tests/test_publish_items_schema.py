"""Schema test for subproject 4 (item publish).

PRD §2.4.4: 4 new tables in master.db + items.created_by_publish_event_id.
"""
from __future__ import annotations

import sqlite3

import pytest


@pytest.mark.integration
def test_publish_templates_table_exists(logged_client):
    """publish_templates table must exist after init_master_db."""
    _, _ = logged_client
    from config import MASTER_DB
    conn = sqlite3.connect(MASTER_DB)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='publish_templates'"
    ).fetchall()
    conn.close()
    assert rows, "publish_templates table missing"


@pytest.mark.integration
def test_template_versions_table_exists(logged_client):
    _, _ = logged_client
    from config import MASTER_DB
    conn = sqlite3.connect(MASTER_DB)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='template_versions'"
    ).fetchall()
    conn.close()
    assert rows, "template_versions table missing"


@pytest.mark.integration
def test_publish_events_table_exists(logged_client):
    _, _ = logged_client
    from config import MASTER_DB
    conn = sqlite3.connect(MASTER_DB)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='publish_events'"
    ).fetchall()
    conn.close()
    assert rows, "publish_events table missing"


@pytest.mark.integration
def test_publish_event_items_table_exists(logged_client):
    _, _ = logged_client
    from config import MASTER_DB
    conn = sqlite3.connect(MASTER_DB)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='publish_event_items'"
    ).fetchall()
    conn.close()
    assert rows, "publish_event_items table missing"


@pytest.mark.integration
def test_items_has_created_by_publish_event_id_column(logged_client):
    """items table must have a created_by_publish_event_id column."""
    _, wh_path = logged_client
    conn = sqlite3.connect(wh_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()}
    conn.close()
    assert "created_by_publish_event_id" in cols, (
        f"items.created_by_publish_event_id missing; cols={sorted(cols)}"
    )