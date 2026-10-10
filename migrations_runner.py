"""master.db 迁移 runner（P0-6）。纯 sqlite3 逻辑，无 Flask 依赖。

设计：
- 版本门控 = PRAGMA user_version（沿用既有仓库约定）。
- **每个迁移步骤执行前强制 sqlite3 backup API 备份**到 backups/migrations/，
  备份失败拒绝迁移。
- up/down 都幂等：已应用的版本跳过；down 到 target 后停。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from migrations import Migration

FMT = "%Y%m%d-%H%M%S"


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else 0


def backup_db(db_path: Path, backup_dir: Path, tag: str) -> Path:
    """sqlite3 backup API 热备份（在线安全）。失败抛异常，调用方中止。"""
    if not db_path.exists():
        raise FileNotFoundError(f"备份源不存在: {db_path}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime(FMT)
    dest = backup_dir / f"{db_path.stem}-v{tag}-{ts}.db"
    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return dest


def migrate_up(
    db_path: Path,
    migrations: list[Migration],
    backup_dir: Path,
) -> list[int]:
    """应用所有未执行迁移（version 升序）。返回本次应用的版本列表。"""
    applied: list[int] = []
    with closing_conn(db_path) as conn:
        current = current_version(conn)
        for version, _desc, up, _down in sorted(migrations, key=lambda m: m[0]):
            if version <= current:
                continue
            backup_db(db_path, backup_dir, f"{current}-to-{version}")
            conn.execute("BEGIN")
            try:
                up(conn)
                conn.execute(f"PRAGMA user_version = {version}")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            applied.append(version)
    return applied


def migrate_down(
    db_path: Path,
    target_version: int,
    migrations: list[Migration],
    backup_dir: Path,
) -> list[int]:
    """回滚到 target_version（version 降序执行 down）。返回本次回滚的版本列表。"""
    reverted: list[int] = []
    with closing_conn(db_path) as conn:
        current = current_version(conn)
        for version, _desc, _up, down in sorted(
            migrations, key=lambda m: m[0], reverse=True
        ):
            if version <= target_version or version > current:
                continue
            backup_db(db_path, backup_dir, f"{current}-down-from-{version}")
            conn.execute("BEGIN")
            try:
                down(conn)
                conn.execute(f"PRAGMA user_version = {version - 1}")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            reverted.append(version)
    return reverted


def pending_versions(
    db_path: Path, migrations: list[Migration]
) -> list[int]:
    with closing_conn(db_path) as conn:
        current = current_version(conn)
        return sorted(v for v, *_ in migrations if v > current)


class closing_conn:
    """sqlite3 连接上下文（row_factory 不设，迁移用元组即可）。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.conn: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        if not self.db_path.exists():
            # 首次迁移空库：初始化空文件，PRAGMA user_version=0
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        return self.conn

    def __exit__(self, *exc) -> None:
        if self.conn is not None:
            self.conn.close()
