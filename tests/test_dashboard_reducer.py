"""Dashboard projection of deterministic reducer state (Issue #56)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from scripts.dailycheck_dashboard import fetchers, render


def _reducer_db(path: Path) -> Path:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE trigger_queue (
                repo TEXT NOT NULL,
                pr_number INTEGER NOT NULL,
                head_sha TEXT NOT NULL,
                event TEXT NOT NULL,
                route TEXT NOT NULL,
                state TEXT NOT NULL,
                due_at_ms INTEGER NOT NULL,
                lease_expires_ms INTEGER,
                attempts INTEGER NOT NULL,
                collapsed_count INTEGER NOT NULL,
                last_error TEXT NOT NULL
            );
            CREATE TABLE provider_state (
                provider TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                consecutive_failures INTEGER NOT NULL,
                cooldown_until_ms INTEGER NOT NULL,
                last_error TEXT NOT NULL,
                updated_at_ms INTEGER NOT NULL
            );
            CREATE TABLE pr_state (
                repo TEXT NOT NULL,
                pr_number INTEGER NOT NULL,
                head_sha TEXT NOT NULL,
                checks_status TEXT NOT NULL,
                review_status TEXT NOT NULL,
                deploy_status TEXT NOT NULL,
                last_event TEXT NOT NULL,
                next_action TEXT NOT NULL,
                updated_at_ms INTEGER NOT NULL,
                merge_ready_at_ms INTEGER,
                merge_ready_trigger_id TEXT,
                gate_reason TEXT NOT NULL
            );
            """
        )
        conn.executemany(
            """
            INSERT INTO trigger_queue VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("eric/dailycheck", 56, "a" * 40, "check_suite", "wdg-ci", "pending", 9_000, None, 0, 1, ""),
                ("eric/dailycheck", 57, "b" * 40, "deploy_result", "dailycheck-deploy", "claimed", 8_000, 99_000, 1, 1, ""),
                ("eric/dailycheck", 58, "c" * 40, "check_suite", "wdg-ci", "done", 7_000, None, 1, 1, ""),
            ],
        )
        conn.execute(
            "INSERT INTO provider_state VALUES ('hermes', 'open', 2, 123456, 'quota', 1)"
        )
        conn.execute(
            """
            INSERT INTO pr_state VALUES (
                'eric/dailycheck', 56, ?, 'success', 'open', '', 'check_suite:completed:success',
                'merge', 10_000, 10_000, 'trigger-from-56', 'ready'
            )
            """,
            ("a" * 40,),
        )
    return path


def test_fetch_reducer_state_reports_queue_provider_and_merge_ready(tmp_path):
    state = fetchers.fetch_reducer_state(_reducer_db(tmp_path / "reducer.sqlite3"))

    assert state["available"] is True
    assert state["queue"] == {"pending": 1, "claimed": 1, "done": 1}
    assert state["provider"] == {
        "provider": "hermes",
        "state": "open",
        "consecutive_failures": 2,
        "cooldown_until_ms": 123456,
        "last_error": "quota",
        "updated_at_ms": 1,
    }
    assert state["merge_ready"] == [
        {
            "repo": "eric/dailycheck",
            "pr_number": 56,
            "head_sha": "a" * 40,
            "ready_at_ms": 10_000,
            "trigger_id": "trigger-from-56",
            "checks_status": "success",
            "gate_reason": "ready",
        }
    ]


def test_fetch_reducer_state_is_honestly_unavailable_when_db_is_missing(tmp_path):
    state = fetchers.fetch_reducer_state(tmp_path / "missing.sqlite3")

    assert state["available"] is False
    assert state["reason"] == "state_unavailable"
    assert "queue" not in state
    assert state["merge_ready"] == []


def _render(state: dict) -> str:
    return render.render(
        render.RenderInput(
            created_at="2026-10-26T18:00:00+08:00",
            trigger_kind="manual",
            trigger_event_id=None,
            head_sha="d" * 40,
            origin_main_sha="e" * 40,
            summary={
                "open_issues": 0,
                "merged_7d_total": 0,
                "worktrees_total": 0,
                "closed_unmerged": 0,
            },
            reducer_state=state,
        )
    )


def test_dashboard_renders_reducer_state_instead_of_stale_task_claims(tmp_path):
    state = fetchers.fetch_reducer_state(_reducer_db(tmp_path / "reducer.sqlite3"))

    html = _render(state)

    assert "pending 1" in html
    assert "claimed 1" in html
    assert "done 1" in html
    assert "PR #56" in html
    assert "123456" in html
    assert "首个全自动 merge 闭环 = PR #52" not in html
    assert "Issue #51（watcher retry-gating）待修" not in html
    assert "02ee9e01cb13" not in html


def test_dashboard_marks_reducer_state_unavailable_without_fabricating_zero(tmp_path):
    state = fetchers.fetch_reducer_state(tmp_path / "missing.sqlite3")

    html = _render(state)

    assert "reducer 状态不可用" in html
    assert "pending 0" not in html
    assert "merge-ready PR 无记录" not in html
