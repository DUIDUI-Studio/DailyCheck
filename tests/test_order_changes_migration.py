"""Actual master files: init/runner interleave, preservation and atomic retry."""
import sqlite3

import pytest

from migrations import order_changes as migration


def legacy(tmp_path):
    conn = sqlite3.connect(tmp_path / 'master.db')
    conn.execute('CREATE TABLE store_orders(id INTEGER PRIMARY KEY)')
    conn.execute('CREATE TABLE store_order_items(id INTEGER PRIMARY KEY, order_id INTEGER, quantity REAL, canonical_id INTEGER, source_warehouse_code TEXT, unit TEXT, dc_item_id INTEGER)')
    conn.execute('INSERT INTO store_orders VALUES(1)')
    conn.execute("INSERT INTO store_order_items VALUES(1,1,-3,1,NULL,'件',NULL)")
    conn.commit()
    return conn


def test_legacy_repeated_and_interleaved_up_preserves_unknown(tmp_path):
    conn = legacy(tmp_path)
    for _ in range(3):
        migration.registered_up(conn)
        conn.commit()
    assert conn.execute('SELECT original_quantity,unit_price_snapshot,product_snapshot FROM store_order_items').fetchone() == (-3, None, None)
    assert conn.execute('SELECT change_revision,shipping_rule_snapshot FROM store_orders').fetchone() == (0, None)
    assert conn.execute('PRAGMA user_version').fetchone()[0] == 0
    assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    with pytest.raises(RuntimeError, match='不可破坏性down'):
        migration.registered_down(conn)
    conn.close()


def test_helper_failure_rolls_back_all_ddl_and_backfill(tmp_path, monkeypatch):
    conn = legacy(tmp_path)
    ddl = migration.DDL
    monkeypatch.setattr(migration, 'DDL', (*ddl, 'INVALID SQL'))
    with pytest.raises(sqlite3.OperationalError):
        migration.registered_up(conn)
    assert 'original_quantity' not in [r[1] for r in conn.execute('PRAGMA table_info(store_order_items)')]
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='store_order_ship_claims'").fetchone()
    monkeypatch.setattr(migration, 'DDL', ddl)
    migration.registered_up(conn)
    assert conn.execute('SELECT original_quantity FROM store_order_items').fetchone()[0] == -3
    conn.close()


def test_init_runner_and_repeated_init_share_schema(tmp_path, monkeypatch):
    import config
    import db
    from migrations import MIGRATIONS
    from migrations_runner import migrate_up

    path = tmp_path / 'master.db'
    monkeypatch.setattr(db, 'MASTER_DB', path)
    monkeypatch.setattr(db, 'WAREHOUSE_DB_DIR', tmp_path / 'warehouses')
    monkeypatch.setattr(config, 'MASTER_DB', path)
    db.init_master_db()
    assert migrate_up(path, MIGRATIONS, tmp_path / 'backups') == list(range(1, 8))
    db.init_master_db()
    assert migrate_up(path, MIGRATIONS, tmp_path / 'backups') == []
    with sqlite3.connect(path) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 7
        assert len(conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'store_order_change_%'").fetchall()) == 4
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
