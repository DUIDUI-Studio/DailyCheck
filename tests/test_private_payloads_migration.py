"""Issue162: the private receipt table must install through the registered migration."""
from __future__ import annotations

import json
import re
import sqlite3

from db import FEEDBACK_DELIVERY_RECEIPTS_DDL, MASTER_SCHEMA, init_master_db
from migrations import MIGRATIONS
from migrations.feedback_receipts import registered_up as feedback_receipts_up
from migrations.routing_stockout import ROUTING_DDL

EXPECTED_COLUMNS = {
    "ticket_id", "provider", "host", "repository_id", "repository", "kind",
    "request_id", "draft_sha256", "payload_json", "payload_sha256", "admin_id",
    "comment_id", "state", "owner", "generation", "lease_until", "remote_id",
    "remote_number", "remote_url", "updated_at",
}
POLICY_TABLES = {"mcp_policy_bindings", "mcp_policy_presets"}


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


def _table_state(path):
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        columns = {row[1] for row in db.execute(
            "PRAGMA table_info(feedback_delivery_receipts)")}
        version = db.execute("PRAGMA user_version").fetchone()[0]
        ticket = db.execute(
            "SELECT title,body,status FROM feedback_tickets").fetchone()
        comment = db.execute(
            "SELECT body,dispatched_at FROM feedback_comments").fetchone()
    return tables, columns, version, dict(ticket) if ticket else None, \
        dict(comment) if comment else None


def test_master_schema_uses_the_registered_receipt_ddl():
    assert FEEDBACK_DELIVERY_RECEIPTS_DDL.strip() in MASTER_SCHEMA
    assert [migration[0] for migration in MIGRATIONS if migration[2] is feedback_receipts_up] == [8]


def test_registered_migration_up_installs_receipt_table_on_pre162_master(
        tmp_path, monkeypatch):
    import cli
    from app import create_app
    from migrations_runner import pending_versions

    master = _prepare(monkeypatch, tmp_path)
    init_master_db()
    with sqlite3.connect(master) as db:
        db.execute(
            "INSERT INTO users (id,username,password_hash,is_admin,created_at) "
            "VALUES (1,'synthetic-admin','x',1,'2026-11-07')")
        db.execute(
            "INSERT INTO feedback_tickets "
            "(ticket_no,user_id,title,body,status,created_at,updated_at) "
            "VALUES ('FB-20261107-0001',1,'Synthetic historical ticket',"
            "'Synthetic historical body','analyzed','2026-11-07','2026-11-07')")
        ticket_id = db.execute("SELECT id FROM feedback_tickets").fetchone()[0]
        db.execute(
            "INSERT INTO feedback_comments (ticket_id,user_id,body,is_admin,created_at) "
            "VALUES (?,1,'Synthetic historical comment',1,'2026-11-07')", (ticket_id,))
        db.commit()

    legacy = tmp_path / "pre162-v7-master.db"
    with sqlite3.connect(legacy) as db:
        sqlite3.connect(master).backup(db)
        for ddl in reversed(ROUTING_DDL):
            kind, name = re.search(r"CREATE (?:UNIQUE )?(TABLE|TRIGGER|INDEX) IF NOT EXISTS (\w+)", ddl).groups()
            db.execute(f"DROP {kind} {name}")
        db.execute("DROP TABLE feedback_delivery_receipts")
        db.execute("DROP TABLE mcp_policy_bindings")
        db.execute("DROP TABLE mcp_policy_presets")
        db.execute("PRAGMA user_version=7")
        db.commit()
    legacy_tables, _, legacy_version, expected_ticket, expected_comment = _table_state(legacy)
    assert legacy_version == 7
    assert not legacy_tables & (POLICY_TABLES | {"feedback_delivery_receipts"})

    cli_base = tmp_path / "owned-cli-base"
    monkeypatch.setattr(cli, "BASE_DIR", cli_base)
    app = create_app()
    runner = app.test_cli_runner()

    status = runner.invoke(args=["migrate", "--db", str(legacy), "status"])
    up = runner.invoke(args=["migrate", "--db", str(legacy), "up"])
    tables, columns, version, ticket, comment = _table_state(legacy)
    backups = sorted((cli_base / "backups" / "migrations").glob("*.db"))

    observed = {
        "status_native_exit": status.exit_code, "status_output": status.output,
        "up_native_exit": up.exit_code, "up_output": up.output,
        "registered_versions": [row[0] for row in MIGRATIONS],
        "pending_after_up": pending_versions(legacy, MIGRATIONS),
        "receipt_table_exists_after_up": "feedback_delivery_receipts" in tables,
        "user_version_after_up": version, "backup_count": len(backups),
        "historical_ticket_preserved": ticket == expected_ticket,
        "historical_comment_preserved": comment == expected_comment,
    }
    (tmp_path / "migration-contract-observed.json").write_text(
        json.dumps(observed, indent=2))
    print(json.dumps(observed, indent=2))

    assert status.exit_code == up.exit_code == 0
    assert "待应用: [8, 9, 10]" in status.output
    assert "applied: [8, 9, 10]" in up.output
    assert "feedback_delivery_receipts" in tables
    assert POLICY_TABLES <= tables
    assert columns == EXPECTED_COLUMNS
    assert version == 10
    assert pending_versions(legacy, MIGRATIONS) == []
    assert len(backups) == 3
    assert ticket == expected_ticket
    assert comment == expected_comment
    backups_by_version = {state[2]: state for state in map(_table_state, backups)}
    assert set(backups_by_version) == {7, 8, 9}
    assert "mcp_policy_routing_admin_events" in tables
    assert "mcp_policy_routing_admin_events" not in backups_by_version[9][0]
    intermediate_tables, intermediate_columns, _, intermediate_ticket, intermediate_comment = backups_by_version[8]
    assert "feedback_delivery_receipts" in intermediate_tables
    assert not POLICY_TABLES & intermediate_tables
    assert intermediate_columns == EXPECTED_COLUMNS
    assert (intermediate_ticket, intermediate_comment) == (expected_ticket, expected_comment)

    # Repeating up is a no-op: no additional backup, schema change, or row churn.
    again = runner.invoke(args=["migrate", "--db", str(legacy), "up"])
    tables_again, columns_again, version_again, ticket_again, comment_again = \
        _table_state(legacy)
    backups_again = sorted((cli_base / "backups" / "migrations").glob("*.db"))
    assert again.exit_code == 0
    assert "（无待应用迁移）" in again.output
    assert (tables_again, columns_again, version_again) == (tables, columns, version)
    assert (ticket_again, comment_again) == (ticket, comment)
    assert backups_again == backups

    # Startup initialization remains the idempotent no-op fallback.
    init_master_db()
    with sqlite3.connect(master) as db:
        db.execute("DROP TABLE feedback_delivery_receipts")
        db.commit()
    init_master_db()
    init_master_db()
    _, columns_after_init, _, _, _ = _table_state(master)
    assert columns_after_init == EXPECTED_COLUMNS
