"""CLI commands for platform administration.

Run via:
    flask --app app init-master
    flask --app app migrate-legacy
    flask --app app create-warehouse <code> <name>
    flask --app app create-user <username> <password> [--admin]
    flask --app app assign-role <username> <warehouse_code> <role>
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime

import click
from flask import Flask
from werkzeug.security import generate_password_hash

from config import BASE_DIR, MASTER_DB
from db import init_master_db
from db.migrate import migrate_legacy_inventory


def register_cli(app: Flask) -> None:
    app.cli.add_command(init_master_cmd)
    app.cli.add_command(migrate_legacy_cmd)
    app.cli.add_command(create_warehouse_cmd)
    app.cli.add_command(create_user_cmd)
    app.cli.add_command(assign_role_cmd)
    app.cli.add_command(list_users_cmd)


@click.command("init-master")
def init_master_cmd() -> None:
    """Create master.db schema (idempotent)."""
    init_master_db()
    click.echo(f"Initialized {MASTER_DB}")


@click.command("migrate-legacy")
def migrate_legacy_cmd() -> None:
    """Copy the legacy inventory.db into warehouses/wh_001.db."""
    target = migrate_legacy_inventory()
    click.echo(f"Migrated legacy inventory.db → {target}")


@click.command("create-warehouse")
@click.argument("code")
@click.argument("name")
def create_warehouse_cmd(code: str, name: str) -> None:
    """Register a new warehouse. The db file is created if missing."""
    from db import init_warehouse_db
    from config import WAREHOUSE_DB_DIR

    db_path = WAREHOUSE_DB_DIR / f"{code}.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    init_warehouse_db(db_path)

    rel_path = str(db_path.relative_to(BASE_DIR))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with closing(sqlite3.connect(MASTER_DB)) as conn:
        try:
            conn.execute(
                "INSERT INTO warehouses (code, name, db_path, created_at) VALUES (?, ?, ?, ?)",
                (code, name, rel_path, now),
            )
            conn.commit()
            click.echo(f"Created warehouse {code} ({name})")
        except sqlite3.IntegrityError:
            click.echo(f"Warehouse {code} already exists", err=True)


@click.command("create-user")
@click.argument("username")
@click.argument("password")
@click.option("--admin", is_flag=True, help="Grant platform admin (all warehouses).")
def create_user_cmd(username: str, password: str, admin: bool) -> None:
    """Create a user account with the given password."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pwhash = generate_password_hash(password)
    with closing(sqlite3.connect(MASTER_DB)) as conn:
        try:
            conn.execute(
                "INSERT INTO users (username, password_hash, is_admin, created_at) VALUES (?, ?, ?, ?)",
                (username, pwhash, 1 if admin else 0, now),
            )
            conn.commit()
            click.echo(f"Created user {username}{' (admin)' if admin else ''}")
        except sqlite3.IntegrityError:
            click.echo(f"User {username} already exists", err=True)


@click.command("assign-role")
@click.argument("username")
@click.argument("warehouse_code")
@click.argument("role", type=click.Choice(["staff", "manager", "admin"]))
def assign_role_cmd(username: str, warehouse_code: str, role: str) -> None:
    """Grant <role> on <warehouse_code> to <username>."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with closing(sqlite3.connect(MASTER_DB)) as conn:
        conn.row_factory = sqlite3.Row
        user = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        wh = conn.execute("SELECT id FROM warehouses WHERE code=?", (warehouse_code,)).fetchone()
        if user is None:
            click.echo(f"No user {username}", err=True)
            return
        if wh is None:
            click.echo(f"No warehouse {warehouse_code}", err=True)
            return
        conn.execute(
            "INSERT OR REPLACE INTO warehouse_users (user_id, warehouse_id, role) VALUES (?, ?, ?)",
            (user["id"], wh["id"], role),
        )
        conn.commit()
        click.echo(f"{username} → {warehouse_code} as {role}")


@click.command("list-users")
def list_users_cmd() -> None:
    """Show all users and their warehouse roles."""
    with closing(sqlite3.connect(MASTER_DB)) as conn:
        conn.row_factory = sqlite3.Row
        users = conn.execute("SELECT id, username, is_admin FROM users").fetchall()
        for u in users:
            tag = " [admin]" if u["is_admin"] else ""
            click.echo(f"  {u['username']}{tag}")
            roles = conn.execute(
                """SELECT w.code, wu.role FROM warehouse_users wu
                   JOIN warehouses w ON w.id = wu.warehouse_id
                   WHERE wu.user_id=?""",
                (u["id"],),
            ).fetchall()
            for r in roles:
                click.echo(f"    {r['code']}: {r['role']}")
