"""master.db schema 迁移注册表（P0-6）。

约定：
- 每个迁移 = (version, 描述, up, down)；version 从 1 递增，落 PRAGMA user_version。
- up(conn) / down(conn) 在调用方的事务里执行（由 migrations_runner 包裹），
  内部只写 conn，不自己 commit。
- **新增 schema 变更一律加新迁移条目**；init_master_db() 的幂等 ALTER 仅作为
  兜底保留（测试/首启动），两者通过 CREATE IF NOT EXISTS / PRAGMA 守卫天然兼容。
- down 尽力提供；确实不可逆的迁移（如删列丢数据）down 里 raise RuntimeError，
  由 runbook 的「还原备份」路径兜底。
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable

Migration = tuple[int, str, Callable[[sqlite3.Connection], None],
                  Callable[[sqlite3.Connection], None]]


def _m001_up(conn: sqlite3.Connection) -> None:
    """基线：runner 启用前的 schema 全部由 init_master_db() 管理，此处只占版本号。"""
    # init_master_db 已建 login_throttle / agent_tokens.rate_limit（P0-4），
    # 这里幂等兜底一次，保证老库直接跑 runner 也能到位。
    conn.execute(
        """CREATE TABLE IF NOT EXISTS login_throttle (
            ip TEXT NOT NULL,
            username TEXT NOT NULL,
            failed_count INTEGER NOT NULL DEFAULT 0,
            locked_until TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (ip, username)
        )"""
    )
    cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_tokens)").fetchall()}
    if cols and "rate_limit" not in cols:
        conn.execute("ALTER TABLE agent_tokens ADD COLUMN rate_limit INTEGER")


def _m001_down(conn: sqlite3.Connection) -> None:
    # 基线不可逆（含历史全部 schema）；回退走备份还原。
    raise RuntimeError("migration 0001 是基线，不可 down；请还原备份")


def _m002_up(conn: sqlite3.Connection) -> None:
    """多租户 PoC：tenants 表 + users/warehouses/canonical_items.tenant_id（默认 1）。"""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tenants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL
        )"""
    )
    for table in ("users", "warehouses", "canonical_items"):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if cols and "tenant_id" not in cols:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN tenant_id INTEGER NOT NULL DEFAULT 1"
            )


def _m002_down(conn: sqlite3.Connection) -> None:
    # SQLite 3.35+ 支持 DROP COLUMN（prod 3.45.1 / dev 容器 ≥3.40）。
    for table in ("users", "warehouses", "canonical_items"):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if "tenant_id" in cols:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN tenant_id")
    conn.execute("DROP TABLE IF EXISTS tenants")


MIGRATIONS: list[Migration] = [
    (1, "baseline: login_throttle + agent_tokens.rate_limit（P0-4 收编）",
     _m001_up, _m001_down),
    (2, "multi-tenant PoC: tenants 表 + 三表 tenant_id（P0-1）",
     _m002_up, _m002_down),
]
