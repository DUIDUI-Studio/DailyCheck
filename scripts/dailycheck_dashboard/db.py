"""scripts.dailycheck_dashboard.db —— SQLite 快照存储。

Schema 设计原则：
  1. snapshots 一行 = 一次完整渲染（瞬时状态）
  2. issues_snapshot / prs_snapshot / worktrees_snapshot 是 children，按
     snapshot_id 关联；PK 是 (snapshot_id, number) / (snapshot_id, path)
  3. summary_json 存 KPI 数字快照（不查 children 也能 diff）
  4. 不存 HTML 内容（HTML 走文件路径，避免 db 膨胀）

线程/进程安全：单进程串行写入；systemd 单元也是单实例（Type=simple）。
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path

SCHEMA_VERSION = 1

# 触发来源（与 inbox.jsonl action 对齐 + 手动出活 + 运维刷新）
TRIGGER_KINDS = frozenset({
    "pr_opened",
    "pr_synchronize",
    "pr_reopened",
    "manual",
    "init",
    "deploy",   # deploy_pipeline.sh 尾部钩子（PR 合并部署完成时）
    "timer",    # dc-dashboard-refresh.timer 兜底刷新
})


def _log(msg: str) -> None:
    print(f"[dashboard.db] {msg}", flush=True)


@contextmanager
def connect(db_path: Path):
    """打开 SQLite 连接；统一 row_factory + WAL + foreign_keys。

    Yield sqlite3.Connection；调用方负责 commit/rollback 决策。
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(db_path),
        timeout=10.0,
        isolation_level=None,  # autocommit；显式 BEGIN/COMMIT
    )
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        yield conn
    finally:
        conn.close()


def init_schema(db_path: Path) -> None:
    """幂等建表。已存在则跳过；用于新 db 与已部署 db 升级。

    autocommit 模式（isolation_level=None）下 executescript 自带 commit，
    所以 DDL 单条跑就够；不显式开 BEGIN/COMMIT。
    """
    with connect(db_path) as conn:
        with closing(conn.cursor()) as cur:
            cur.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    trigger_kind TEXT NOT NULL,
                    trigger_event_id TEXT,
                    head_sha TEXT,
                    origin_main_sha TEXT,
                    summary_json TEXT NOT NULL,
                    html_path TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS issues_snapshot (
                    snapshot_id INTEGER NOT NULL,
                    number INTEGER NOT NULL,
                    state TEXT,
                    title TEXT,
                    created_at TEXT,
                    closed_at TEXT,
                    labels TEXT,
                    PRIMARY KEY (snapshot_id, number),
                    FOREIGN KEY (snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS prs_snapshot (
                    snapshot_id INTEGER NOT NULL,
                    number INTEGER NOT NULL,
                    state TEXT,
                    title TEXT,
                    head_ref TEXT,
                    created_at TEXT,
                    merged_at TEXT,
                    PRIMARY KEY (snapshot_id, number),
                    FOREIGN KEY (snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS worktrees_snapshot (
                    snapshot_id INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    branch TEXT,
                    head_sha TEXT,
                    status_label TEXT,
                    PRIMARY KEY (snapshot_id, path),
                    FOREIGN KEY (snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_snapshots_created
                    ON snapshots(created_at);
                CREATE INDEX IF NOT EXISTS idx_issues_number
                    ON issues_snapshot(number);
                CREATE INDEX IF NOT EXISTS idx_prs_number
                    ON prs_snapshot(number);
                """
            )
            cur.execute(
                "SELECT version FROM schema_version WHERE version = ?",
                (SCHEMA_VERSION,),
            )
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (SCHEMA_VERSION, _now_iso()),
                )


def _now_iso() -> str:
    """CST+8 ISO 8601（与生产 unit 时区对齐；本地 epoch + 8h 偏移显示）。"""
    # 用本地 time 即可（dev 端 CST+8，prod 端 unit 未设 TZ 但 daemon.sh 启动时设了）
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def insert_snapshot(
    db_path: Path,
    *,
    trigger_kind: str,
    trigger_event_id: str | None,
    head_sha: str | None,
    origin_main_sha: str | None,
    summary: dict,
    html_path: str,
    issues: list[dict],
    prs: list[dict],
    worktrees: list[dict],
) -> int:
    """一次事务里写完整快照。返回新 snapshot.id。"""
    if trigger_kind not in TRIGGER_KINDS:
        raise ValueError(f"invalid trigger_kind: {trigger_kind!r}")
    init_schema(db_path)  # 幂等
    with connect(db_path) as conn:
        with closing(conn.cursor()) as cur:
            cur.execute("BEGIN IMMEDIATE")
            try:
                cur.execute(
                    """
                    INSERT INTO snapshots
                        (created_at, trigger_kind, trigger_event_id,
                         head_sha, origin_main_sha, summary_json, html_path)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _now_iso(),
                        trigger_kind,
                        trigger_event_id,
                        head_sha,
                        origin_main_sha,
                        json.dumps(summary, ensure_ascii=False, sort_keys=True),
                        html_path,
                    ),
                )
                snap_id = int(cur.lastrowid)

                cur.executemany(
                    """
                    INSERT INTO issues_snapshot
                        (snapshot_id, number, state, title, created_at, closed_at, labels)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            snap_id,
                            int(i["number"]),
                            i.get("state"),
                            i.get("title"),
                            i.get("created_at"),
                            i.get("closed_at"),
                            ",".join(i.get("labels") or []),
                        )
                        for i in issues
                    ],
                )
                cur.executemany(
                    """
                    INSERT INTO prs_snapshot
                        (snapshot_id, number, state, title, head_ref, created_at, merged_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            snap_id,
                            int(p["number"]),
                            p.get("state"),
                            p.get("title"),
                            p.get("head_ref"),
                            p.get("created_at"),
                            p.get("merged_at"),
                        )
                        for p in prs
                    ],
                )
                cur.executemany(
                    """
                    INSERT INTO worktrees_snapshot
                        (snapshot_id, path, branch, head_sha, status_label)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            snap_id,
                            w["path"],
                            w.get("branch"),
                            w.get("head_sha"),
                            w.get("status_label"),
                        )
                        for w in worktrees
                    ],
                )

                cur.execute("COMMIT")
                _log(f"inserted snapshot id={snap_id} trigger={trigger_kind} html={html_path}")
                return snap_id
            except Exception:
                cur.execute("ROLLBACK")
                raise


def latest_snapshot(db_path: Path) -> dict | None:
    """最近一份完整快照（包含 summary + html_path）。"""
    if not db_path.exists():
        return None
    with connect(db_path) as conn:
        with closing(conn.cursor()) as cur:
            cur.execute(
                "SELECT * FROM snapshots ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
            if row is None:
                return None
            return _row_to_snapshot(row)


def previous_snapshot(db_path: Path) -> dict | None:
    """倒数第二份（与 latest 对比）。"""
    if not db_path.exists():
        return None
    with connect(db_path) as conn:
        with closing(conn.cursor()) as cur:
            cur.execute(
                "SELECT * FROM snapshots ORDER BY id DESC LIMIT 2"
            )
            rows = cur.fetchall()
            if len(rows) < 2:
                return None
            return _row_to_snapshot(rows[1])


def list_snapshots(db_path: Path, limit: int = 20) -> list[dict]:
    """列出最近 N 份摘要（不含 children）。"""
    if not db_path.exists():
        return []
    with connect(db_path) as conn:
        with closing(conn.cursor()) as cur:
            cur.execute(
                "SELECT * FROM snapshots ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            return [_row_to_snapshot(r) for r in cur.fetchall()]


def _row_to_snapshot(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "trigger_kind": row["trigger_kind"],
        "trigger_event_id": row["trigger_event_id"],
        "head_sha": row["head_sha"],
        "origin_main_sha": row["origin_main_sha"],
        "summary": json.loads(row["summary_json"]) if row["summary_json"] else {},
        "html_path": row["html_path"],
    }
