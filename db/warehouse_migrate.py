"""Explicit single-warehouse migration entry, shared by Web and MCP first requests."""

from __future__ import annotations

import argparse
import fcntl
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path

from migrations.warehouse import WAREHOUSE_MIGRATIONS


def connect_existing(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=5)


def validate(path: Path) -> int:
    if not path.is_file():
        raise RuntimeError(f"warehouse file does not exist: {path}")
    with closing(connect_existing(path)) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if (
            not {"items", "restock_requests", "outbound_requests"} <= tables
            or {"users", "warehouses"} <= tables
        ):
            raise RuntimeError("target is not a warehouse database")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        from db import WAREHOUSE_SCHEMA_VERSION

        if version > WAREHOUSE_SCHEMA_VERSION:
            raise RuntimeError("unknown future warehouse version")
        return version


@contextmanager
def migration_lock(path: Path):
    with path.with_name(path.name + ".migration.lock").open("a") as lock:
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("warehouse migration lock timeout") from None
                time.sleep(0.02)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def backup(path: Path, old: int, new: int) -> Path:
    directory = path.parent / "backups" / "migrations"
    directory.mkdir(parents=True, exist_ok=True)
    name = f"{path.name}-v{old}-to-{new}-{datetime.now():%Y%m%d%H%M%S%f}-{uuid.uuid4().hex}.db"
    target = directory / name
    with closing(connect_existing(path)) as source, closing(sqlite3.connect(target)) as dest:
        source.backup(dest)
        if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("warehouse backup integrity check failed")
    return target


def upgrade(db_path: Path | str) -> None:
    from db import WAREHOUSE_SCHEMA_VERSION, _migrate_warehouse_baseline

    path = Path(db_path).resolve()
    if validate(path) == WAREHOUSE_SCHEMA_VERSION:
        return
    with migration_lock(path):
        version = validate(path)
        if version < 6:
            backup(path, version, 6)
            # Legacy executescript may persist partial DDL. It never stamps v7.
            _migrate_warehouse_baseline(path)
            version = validate(path)
        for target, _description, up, _down in WAREHOUSE_MIGRATIONS:
            if version >= target:
                continue
            backup(path, version, target)
            with closing(connect_existing(path)) as conn:
                try:
                    conn.execute("PRAGMA busy_timeout=5000")
                    conn.execute("BEGIN IMMEDIATE")
                    if conn.execute("PRAGMA user_version").fetchone()[0] != version:
                        raise RuntimeError("warehouse version changed during migration")
                    up(conn)
                    conn.execute(f"PRAGMA user_version={target}")
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
            version = target


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("up")
    down = commands.add_parser("down")
    down.add_argument("--target", type=int, required=True)
    args = parser.parse_args(argv)
    path = args.db.resolve()
    try:
        version = validate(path)
        if args.command == "up":
            upgrade(path)
            version = validate(path)
        elif args.command == "down":
            if args.target != 6 or version != 7:
                raise RuntimeError("unsupported warehouse downgrade")
            with closing(connect_existing(path)) as conn:
                WAREHOUSE_MIGRATIONS[0][3](conn)
        print(f"warehouse user_version={version}")
    except (RuntimeError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
