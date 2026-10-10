"""迁移 runner（P0-6）测试：版本门控 / 备份 / 幂等 / down / 真实注册表兼容。"""
import sqlite3
from pathlib import Path

import pytest

from migrations import MIGRATIONS
from migrations_runner import (
    backup_db,
    current_version,
    migrate_down,
    migrate_up,
    pending_versions,
)


def _make_db(tmp_path: Path, version: int = 0) -> Path:
    p = tmp_path / "master.db"
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE IF NOT EXISTS marker (id INTEGER)")
    conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()
    conn.close()
    return p


def _fake_migrations():
    """两个假迁移：v1 建表 t1，v2 建表 t2；down 各自 DROP。"""
    def up1(conn):
        conn.execute("CREATE TABLE t1 (id INTEGER)")

    def down1(conn):
        conn.execute("DROP TABLE t1")

    def up2(conn):
        conn.execute("CREATE TABLE t2 (id INTEGER)")

    def down2(conn):
        conn.execute("DROP TABLE t2")

    return [(1, "t1", up1, down1), (2, "t2", up2, down2)]


def test_migrate_up_applies_pending_in_order(tmp_path):
    db = _make_db(tmp_path)
    applied = migrate_up(db, _fake_migrations(), tmp_path / "bak")
    assert applied == [1, 2]
    conn = sqlite3.connect(db)
    assert current_version(conn) == 2
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert {"t1", "t2"} <= tables


def test_migrate_up_idempotent_second_run(tmp_path):
    db = _make_db(tmp_path)
    migrate_up(db, _fake_migrations(), tmp_path / "bak")
    applied = migrate_up(db, _fake_migrations(), tmp_path / "bak")
    assert applied == []


def test_migrate_up_creates_backup_before_each_step(tmp_path):
    db = _make_db(tmp_path)
    bak = tmp_path / "bak"
    migrate_up(db, _fake_migrations(), bak)
    files = sorted(bak.glob("*.db"))
    assert len(files) == 2  # 每个迁移一个备份
    # 备份文件可打开且含数据
    conn = sqlite3.connect(files[0])
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()


def test_migrate_down_reverts_and_restocks_version(tmp_path):
    db = _make_db(tmp_path)
    migs = _fake_migrations()
    migrate_up(db, migs, tmp_path / "bak")
    reverted = migrate_down(db, 0, migs, tmp_path / "bak")
    assert reverted == [2, 1]
    conn = sqlite3.connect(db)
    assert current_version(conn) == 0
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "t1" not in tables and "t2" not in tables


def test_migrate_down_to_intermediate(tmp_path):
    db = _make_db(tmp_path)
    migs = _fake_migrations()
    migrate_up(db, migs, tmp_path / "bak")
    reverted = migrate_down(db, 1, migs, tmp_path / "bak")
    assert reverted == [2]
    conn = sqlite3.connect(db)
    assert current_version(conn) == 1
    conn.close()


def test_failed_migration_leaves_version_unchanged(tmp_path):
    def bad_up(conn):
        conn.execute("CREATE TABLE bad_table (id INTEGER)")
        raise RuntimeError("boom")

    db = _make_db(tmp_path)
    migs = [(1, "bad", bad_up, lambda conn: None)]
    with pytest.raises(RuntimeError):
        migrate_up(db, migs, tmp_path / "bak")
    conn = sqlite3.connect(db)
    assert current_version(conn) == 0
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "bad_table" not in tables  # 事务回滚


def test_pending_versions(tmp_path):
    db = _make_db(tmp_path, version=1)
    assert pending_versions(db, _fake_migrations()) == [2]
    db2 = _make_db(tmp_path / "sub", version=2) if False else _make_db(
        tmp_path, version=2)
    # 注意：两个 db 同名会覆盖，重建第二个
    (tmp_path / "master2.db").write_bytes(b"")
    db2 = tmp_path / "master2.db"
    conn = sqlite3.connect(db2)
    conn.execute("PRAGMA user_version = 2")
    conn.close()
    assert pending_versions(db2, _fake_migrations()) == []


def test_real_registry_baseline_up_on_legacy_db(tmp_path):
    """真实 MIGRATIONS 基线在老库（user_version=0、含 agent_tokens 旧结构）应能跑通。"""
    db = tmp_path / "master.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE agent_tokens (id INTEGER PRIMARY KEY, token_hash TEXT)"
    )
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()
    applied = migrate_up(db, MIGRATIONS, tmp_path / "bak")
    assert applied == [1]
    conn = sqlite3.connect(db)
    assert current_version(conn) == 1
    cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_tokens)")}
    assert "rate_limit" in cols
    # 基线 down 应拒绝
    with pytest.raises(RuntimeError):
        migrate_down(db, 0, MIGRATIONS, tmp_path / "bak")
    conn.close()


def test_backup_db_invalid_source_fails(tmp_path):
    with pytest.raises(FileNotFoundError):
        backup_db(tmp_path / "nope.db", tmp_path / "bak", "x")
