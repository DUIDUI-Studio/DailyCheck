"""scripts.dailycheck_dashboard.ops_db —— 运维事实的存储层（事件型，不是快照型）。

为什么单独一个模块：
  db.py 的三张表是**快照型**（snapshot_id 外键，每次渲染整份写一份），
  而部署事件 / 日 token / 验证结果天然是**时间序列**——每 15 分钟渲染一次，
  按快照存会 96 份/天的重复。所以这两类分开存，各自幂等 upsert。

与 db.py 的关系：只借用 connect()（WAL + foreign_keys + autocommit 语义），
不修改 db.py 的任何既有表；init_ops_schema() 由 CLI 显式调用。
"""
from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

from .db import _now_iso, connect

SCHEMA_VERSION = 1

_OPS_DDL = """
CREATE TABLE IF NOT EXISTS deploy_events (
    pr            INTEGER NOT NULL,
    sha           TEXT    NOT NULL,
    started_at    TEXT    NOT NULL,
    finished_at   TEXT,
    total_sec     INTEGER,
    max_gap_sec   INTEGER,
    segments_json TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    PRIMARY KEY (pr, sha)
);

CREATE TABLE IF NOT EXISTS token_daily (
    day             TEXT    NOT NULL,
    source          TEXT    NOT NULL,
    sessions        INTEGER NOT NULL DEFAULT 0,
    input_tok       INTEGER NOT NULL DEFAULT 0,
    output_tok      INTEGER NOT NULL DEFAULT 0,
    cache_read_tok  INTEGER NOT NULL DEFAULT 0,
    cache_write_tok INTEGER NOT NULL DEFAULT 0,
    reasoning_tok   INTEGER NOT NULL DEFAULT 0,
    tool_calls      INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT    NOT NULL,
    PRIMARY KEY (day, source)
);

CREATE TABLE IF NOT EXISTS verifications (
    pr             INTEGER NOT NULL,
    generated_at   TEXT    NOT NULL,
    passed         INTEGER NOT NULL,
    total          INTEGER NOT NULL,
    checks_json    TEXT    NOT NULL,
    path           TEXT,
    updated_at     TEXT    NOT NULL,
    PRIMARY KEY (pr, generated_at)
);

CREATE INDEX IF NOT EXISTS idx_deploy_events_started
    ON deploy_events(started_at);
CREATE INDEX IF NOT EXISTS idx_token_daily_day
    ON token_daily(day);
CREATE INDEX IF NOT EXISTS idx_verifications_generated
    ON verifications(generated_at);
"""


def init_ops_schema(db_path: Path) -> None:
    """幂等建表（autocommit 下 executescript 自带 commit）。"""
    with connect(db_path) as conn:
        with closing(conn.cursor()) as cur:
            cur.executescript(_OPS_DDL)


def upsert_deploy_events(db_path: Path, events: list[dict]) -> int:
    """幂等写部署事件。主键 (pr, sha)，重复摄取只覆盖不新增。"""
    if not events:
        return 0
    init_ops_schema(db_path)
    now = _now_iso()
    with connect(db_path) as conn:
        with closing(conn.cursor()) as cur:
            cur.execute("BEGIN IMMEDIATE")
            try:
                cur.executemany(
                    """
                    INSERT OR REPLACE INTO deploy_events
                        (pr, sha, started_at, finished_at, total_sec,
                         max_gap_sec, segments_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            int(e["pr"]),
                            e.get("sha") or "",
                            e["started_at"],
                            e.get("finished_at"),
                            int(e.get("total_sec") or 0),
                            int(e.get("max_gap_sec") or 0),
                            json.dumps(e.get("segments") or [], ensure_ascii=False),
                            now,
                        )
                        for e in events
                    ],
                )
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise
    return len(events)


def upsert_token_daily(db_path: Path, rows: list[dict]) -> int:
    """幂等写日 token 聚合。主键 (day, source)。"""
    if not rows:
        return 0
    init_ops_schema(db_path)
    now = _now_iso()
    with connect(db_path) as conn:
        with closing(conn.cursor()) as cur:
            cur.execute("BEGIN IMMEDIATE")
            try:
                cur.executemany(
                    """
                    INSERT OR REPLACE INTO token_daily
                        (day, source, sessions, input_tok, output_tok,
                         cache_read_tok, cache_write_tok, reasoning_tok,
                         tool_calls, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            r["day"], r["source"],
                            int(r.get("sessions") or 0),
                            int(r.get("input_tok") or 0),
                            int(r.get("output_tok") or 0),
                            int(r.get("cache_read_tok") or 0),
                            int(r.get("cache_write_tok") or 0),
                            int(r.get("reasoning_tok") or 0),
                            int(r.get("tool_calls") or 0),
                            now,
                        )
                        for r in rows
                    ],
                )
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise
    return len(rows)


def upsert_verifications(db_path: Path, recs: list[dict]) -> int:
    """幂等写验证结果。主键 (pr, generated_at)。"""
    if not recs:
        return 0
    init_ops_schema(db_path)
    now = _now_iso()
    with connect(db_path) as conn:
        with closing(conn.cursor()) as cur:
            cur.execute("BEGIN IMMEDIATE")
            try:
                cur.executemany(
                    """
                    INSERT OR REPLACE INTO verifications
                        (pr, generated_at, passed, total, checks_json, path, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            int(r["pr"]),
                            r["generated_at"],
                            int(r.get("passed") or 0),
                            int(r.get("total") or 0),
                            json.dumps(r.get("checks") or [], ensure_ascii=False),
                            r.get("path"),
                            now,
                        )
                        for r in recs
                    ],
                )
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise
    return len(recs)


# ------------------------------------------------------------ 查询
def _rows(db_path: Path, sql: str, params: tuple = ()) -> list[dict]:
    if not db_path.exists():
        return []
    with connect(db_path) as conn:
        with closing(conn.cursor()) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]


def recent_deploy_events(db_path: Path, limit: int = 10) -> list[dict]:
    rows = _rows(
        db_path,
        "SELECT * FROM deploy_events ORDER BY started_at DESC LIMIT ?",
        (limit,),
    )
    for r in rows:
        try:
            r["segments"] = json.loads(r.pop("segments_json") or "[]")
        except json.JSONDecodeError:
            r["segments"] = []
    return rows


def token_series(db_path: Path, days: int = 7) -> list[dict]:
    return _rows(
        db_path,
        "SELECT * FROM token_daily WHERE day >= date('now', ?) ORDER BY day, source",
        (f"-{int(days)} day",),
    )


def recent_verifications(db_path: Path, limit: int = 6) -> list[dict]:
    rows = _rows(
        db_path,
        "SELECT * FROM verifications ORDER BY generated_at DESC LIMIT ?",
        (limit,),
    )
    for r in rows:
        try:
            r["checks"] = json.loads(r.pop("checks_json") or "[]")
        except json.JSONDecodeError:
            r["checks"] = []
    return rows
