"""Warehouse v7 registry, first-request migration, isolation and rollback evidence."""

from __future__ import annotations

import multiprocessing
import sqlite3
import subprocess
import sys

import pytest

import db
from db import warehouse_migrate as runner
from mcp_server.data.unit_of_work import warehouse_connection
from migrations import MIGRATIONS
from migrations.warehouse import WAREHOUSE_MIGRATIONS


def legacy(path, version=6):
    with sqlite3.connect(path) as conn:
        conn.executescript(db.WAREHOUSE_SCHEMA)
    db._migrate_warehouse_baseline(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO items(id,sku,name,category_id,quantity,updated_at) "
            "VALUES(1,'x','X',1,100,'2026-10-30')"
        )
        conn.execute(
            "INSERT INTO restock_requests(item_id,requested_quantity,reason,created_at) "
            "VALUES(1,.25,'juleng-sync:old:01','2026-10-30')"
        )
        conn.execute(f"PRAGMA user_version={version}")
    return path


def snapshot(path):
    with sqlite3.connect(path) as conn:
        return (
            conn.execute("PRAGMA user_version").fetchone()[0],
            list(conn.execute("SELECT name,sql FROM sqlite_master ORDER BY name")),
            list(conn.execute("SELECT * FROM restock_requests")),
        )


@pytest.mark.parametrize("version", [0, 4, 5, 6])
def test_migration_old_and_historical_defaults(tmp_path, version):
    path = legacy(tmp_path / "old.db", version)
    db.migrate_warehouse_db_columns(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
        assert conn.execute(
            "SELECT doc_no,line_no,input_quantity,factor,requested_quantity FROM restock_requests"
        ).fetchone() == (None, None, None, "1", 0.25)
        assert conn.execute("SELECT count(*) FROM document_write_keys").fetchone()[0] == 0
        # The permanent key is a real composite PK with an independent request uniqueness guard.
        pk = [(r[1], r[5]) for r in conn.execute("PRAGMA table_info(document_write_keys)") if r[5]]
        assert pk == [("direction", 1), ("doc_no", 2), ("line_no", 3)]
        for table in ("restock_requests", "outbound_requests"):
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name=?",
                (f"uq_{table.split('_')[0]}_document_line",),
            ).fetchone()[0]
            assert "WHERE doc_no IS NOT NULL AND line_no IS NOT NULL" in sql
            assert "COLLATE BINARY" in sql
    backups = list((tmp_path / "backups/migrations").glob("*.db"))
    assert len(backups) == (2 if version < 6 else 1)
    for backup in backups:
        with sqlite3.connect(backup) as conn:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    before = snapshot(path)
    db.migrate_warehouse_db_columns(path)
    assert snapshot(path) == before
    assert list((tmp_path / "backups/migrations").glob("*.db")) == backups


def test_migration_new_partial_and_no_repeat_alter(tmp_path, monkeypatch):
    path = tmp_path / "new.db"
    db.init_warehouse_db(path)
    assert snapshot(path)[0] == 7
    partial = legacy(tmp_path / "partial.db")
    with sqlite3.connect(partial) as conn:
        conn.execute("ALTER TABLE restock_requests ADD COLUMN doc_no TEXT DEFAULT NULL")
        conn.execute("UPDATE restock_requests SET doc_no='unpaired-history'")
    db.migrate_warehouse_db_columns(partial)
    with sqlite3.connect(partial) as conn:
        assert (
            conn.execute("SELECT doc_no FROM restock_requests").fetchone()[0] == "unpaired-history"
        )
    monkeypatch.setattr(
        runner, "backup", lambda *_: pytest.fail("current version must not back up")
    )
    db.migrate_warehouse_db_columns(path)
    db.migrate_warehouse_db_columns(partial)


def test_migration_mcp_first_read_and_write_without_web(tmp_path):
    path = legacy(tmp_path / "mcp.db")
    with warehouse_connection(str(path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
        assert conn.execute("SELECT factor FROM restock_requests").fetchone()[0] == "1"
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE restock_requests SET input_quantity='0.25' WHERE id=1")
        conn.commit()
    assert snapshot(path)[2][0][-2:] == ("0.25", "1")


def _migrate_process(path, barrier, queue):
    barrier.wait(timeout=15)
    try:
        with warehouse_connection(path) as conn:
            queue.put(conn.execute("PRAGMA user_version").fetchone()[0])
    except Exception as exc:
        queue.put(repr(exc))


def test_migration_race_symlink_and_target_isolation(tmp_path):
    path = legacy(tmp_path / "real.db")
    alias = tmp_path / "alias.db"
    alias.symlink_to(path)
    green = legacy(tmp_path / "green.db")
    ctx = multiprocessing.get_context("spawn")
    barrier, queue = ctx.Barrier(2), ctx.Queue()
    workers = [
        ctx.Process(target=_migrate_process, args=(str(p), barrier, queue)) for p in (path, alias)
    ]
    for worker in workers:
        worker.start()
    assert [queue.get(timeout=25) for _ in workers] == [7, 7]
    for worker in workers:
        worker.join(timeout=10)
        assert worker.exitcode == 0
    assert len(list((tmp_path / "backups/migrations").glob("real.db-v6-to-7*"))) == 1
    assert not alias.with_name("alias.db.migration.lock").exists()
    assert snapshot(green)[0] == 6
    db.migrate_warehouse_db_columns(green)
    assert len(list((tmp_path / "backups/migrations").glob("green.db-v6-to-7*"))) == 1


def test_migration_failure_backup_refuses_upgrade(tmp_path, monkeypatch):
    path = legacy(tmp_path / "old.db")
    before = snapshot(path)
    monkeypatch.setattr(
        runner, "backup", lambda *_: (_ for _ in ()).throw(OSError("backup failed"))
    )
    with pytest.raises(OSError, match="backup failed"):
        db.migrate_warehouse_db_columns(path)
    assert snapshot(path) == before


@pytest.mark.parametrize("point", ["ALTER TABLE", "CREATE UNIQUE INDEX", "PRAGMA user_version=7"])
def test_migration_failure_ddl_index_stamp_atomic(tmp_path, monkeypatch, point):
    path = legacy(tmp_path / "old.db")
    original = runner.connect_existing
    before = snapshot(path)

    class Failure(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            cur = super().execute(sql, parameters)
            if sql.startswith(point):
                raise sqlite3.OperationalError("injected migration failure")
            return cur

    monkeypatch.setattr(
        runner, "connect_existing", lambda path: sqlite3.connect(path, factory=Failure)
    )
    with pytest.raises(sqlite3.OperationalError, match="injected"):
        db.migrate_warehouse_db_columns(path)
    assert snapshot(path) == before
    monkeypatch.setattr(runner, "connect_existing", original)
    db.migrate_warehouse_db_columns(path)
    assert snapshot(path)[0] == 7


def test_migration_failure_wrong_column_and_index(tmp_path):
    for kind in ("column", "index"):
        path = legacy(tmp_path / f"{kind}.db")
        with sqlite3.connect(path) as conn:
            if kind == "column":
                conn.execute("ALTER TABLE restock_requests ADD COLUMN factor INTEGER")
            else:
                conn.execute("CREATE INDEX uq_restock_document_line ON restock_requests(item_id)")
        before = snapshot(path)
        with pytest.raises(RuntimeError, match="incompatible"):
            db.migrate_warehouse_db_columns(path)
        assert snapshot(path) == before


def test_migration_backup_restore_and_down_refusal(tmp_path):
    path = legacy(tmp_path / "old.db")
    before = snapshot(path)
    # WAL-resident data must be included by the sqlite backup API.
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("UPDATE restock_requests SET reason='wal-committed'")
    conn.commit()
    before = snapshot(path)
    db.migrate_warehouse_db_columns(path)
    after = snapshot(path)
    result = subprocess.run(
        [sys.executable, "-m", "db.warehouse_migrate", "--db", str(path), "down", "--target", "6"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0 and "verified backup restore required" in result.stderr
    assert snapshot(path) == after
    backup = next((tmp_path / "backups/migrations").glob("*.db"))
    restored = tmp_path / "restored-isolated.db"
    with sqlite3.connect(backup) as source, sqlite3.connect(restored) as target:
        source.backup(target)
        assert target.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert snapshot(restored) == before
    assert snapshot(path) == after  # restore never overwrites the live target
    conn.close()


def test_target_master_future_missing_schema_cli(tmp_path, monkeypatch):
    master = tmp_path / "master.db"
    monkeypatch.setattr(db, "MASTER_DB", master)
    db.init_master_db()
    with sqlite3.connect(master) as conn:
        conn.execute("PRAGMA user_version=7")
        original = list(conn.iterdump())
    missing = tmp_path / "missing.db"
    wrong = tmp_path / "wrong.db"
    with sqlite3.connect(wrong) as conn:
        conn.execute("CREATE TABLE arbitrary(x)")
    future = legacy(tmp_path / "future.db", 8)
    for path in (master, missing, wrong, future):
        for command in ("status", "up"):
            result = subprocess.run(
                [sys.executable, "-m", "db.warehouse_migrate", "--db", str(path), command],
                capture_output=True,
                text=True,
            )
            assert result.returncode != 0
    assert not missing.exists()
    with sqlite3.connect(master) as conn:
        assert list(conn.iterdump()) == original
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
    assert snapshot(future)[0] == 8
    from migrations.order_changes import registered_up as master_order_changes_up
    from migrations.warehouse import up_document_lines
    assert [m[0] for m in MIGRATIONS] == list(range(1, 8))
    assert MIGRATIONS[-1][2] is master_order_changes_up
    assert WAREHOUSE_MIGRATIONS[0][2] is up_document_lines
    assert all(m[2] is not up_document_lines for m in MIGRATIONS)
    assert all(m[2] is not master_order_changes_up for m in WAREHOUSE_MIGRATIONS)
    assert db.WAREHOUSE_SCHEMA_VERSION == 7
    assert WAREHOUSE_MIGRATIONS[0][0] == 7
    assert "doc_no" not in db.WAREHOUSE_SCHEMA and "document_write_keys" not in db.WAREHOUSE_SCHEMA


def test_migration_mcp_first_service_write(tmp_path, monkeypatch):
    from mcp_server.service import inbound
    from mcp_server.service.auth import AuthContext

    path = legacy(tmp_path / "first-write.db")
    monkeypatch.setattr(inbound, "resolve_warehouse", lambda _: {"db_path": str(path)})
    created = inbound.create_restock(
        1, 0.125, "any", AuthContext(1, [], [], None), doc_no="opaque", line_no="01", factor=10
    )
    assert created["quantity"] == 1.25
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
        assert conn.execute("SELECT quantity FROM items").fetchone()[0] == 101.25
        assert conn.execute("SELECT count(*) FROM document_write_keys").fetchone()[0] == 1


def test_migration_old_v5_without_op_no(tmp_path):
    path = legacy(tmp_path / "v5.db", 5)
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE stock_movements DROP COLUMN op_no")
        conn.execute("ALTER TABLE products DROP COLUMN is_hidden")
    db.migrate_warehouse_db_columns(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
        assert "op_no" in [r[1] for r in conn.execute("PRAGMA table_info(stock_movements)")]
        assert "is_hidden" in [r[1] for r in conn.execute("PRAGMA table_info(products)")]
