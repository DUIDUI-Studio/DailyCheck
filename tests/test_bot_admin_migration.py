"""Issue #79：master.db.bot_agent_config 的初始化与迁移注册。

合同：
- 新库 init_master_db() 后表存在。
- 存量库（旧 user_version）跑注册迁移 v3 后表存在。
- init_master_db() 可重复调用，不破坏已有配置值。
"""
from __future__ import annotations

import sqlite3

from db import MASTER_SCHEMA, init_master_db
from migrations import MIGRATIONS
from migrations_runner import current_version, migrate_down, migrate_up

_TABLE = "bot_agent_config"
_EXPECTED_COLUMNS = {"key", "value", "updated_at", "updated_by"}


def _prepare(monkeypatch, tmp_path):
    import config as config_module
    import db as db_module

    master = tmp_path / "master.db"
    wh_dir = tmp_path / "warehouses"
    wh_dir.mkdir()
    monkeypatch.setattr(db_module, "MASTER_DB", master)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_dir)
    monkeypatch.setattr(config_module, "MASTER_DB", master)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", wh_dir)
    return master


def _table_info(path):
    conn = sqlite3.connect(path)
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({_TABLE})")}
    finally:
        conn.close()


def test_master_schema_declares_bot_agent_config():
    assert "CREATE TABLE IF NOT EXISTS bot_agent_config" in MASTER_SCHEMA


def test_init_master_db_creates_table(monkeypatch, tmp_path):
    master = _prepare(monkeypatch, tmp_path)

    init_master_db()

    assert _table_info(master) == _EXPECTED_COLUMNS


def test_init_master_db_is_idempotent_and_preserves_config(monkeypatch, tmp_path):
    master = _prepare(monkeypatch, tmp_path)
    init_master_db()

    conn = sqlite3.connect(master)
    conn.execute(
        "INSERT INTO bot_agent_config (key, value, updated_at, updated_by) "
        "VALUES ('llm.reasoning_effort', 'high', '2026-10-29 10:00:00', 'admin')"
    )
    conn.commit()
    conn.close()

    init_master_db()
    init_master_db()

    conn = sqlite3.connect(master)
    row = conn.execute(
        "SELECT value, updated_by FROM bot_agent_config "
        "WHERE key='llm.reasoning_effort'"
    ).fetchone()
    conn.close()
    assert row == ("high", "admin")


def test_registered_migration_v3_creates_table_on_existing_db(tmp_path):
    master = tmp_path / "master.db"
    conn = sqlite3.connect(master)
    conn.execute("CREATE TABLE marker (id INTEGER)")
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()

    v3 = [m for m in MIGRATIONS if m[0] == 3]
    assert len(v3) == 1, "bot_agent_config 必须有唯一 v3 迁移"

    applied = migrate_up(master, v3, tmp_path / "bak")

    assert applied == [3]
    assert _table_info(master) == _EXPECTED_COLUMNS
    conn = sqlite3.connect(master)
    assert current_version(conn) == 3
    conn.close()


def test_registered_migration_v3_down_drops_table(tmp_path):
    master = tmp_path / "master.db"
    conn = sqlite3.connect(master)
    conn.execute("CREATE TABLE marker (id INTEGER)")
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()
    v3 = [m for m in MIGRATIONS if m[0] == 3]
    migrate_up(master, v3, tmp_path / "bak")

    reverted = migrate_down(master, 2, v3, tmp_path / "bak")

    assert reverted == [3]
    assert _table_info(master) == set()
    conn = sqlite3.connect(master)
    assert current_version(conn) == 2
    conn.close()
