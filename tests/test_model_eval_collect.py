"""Issue #91：scripts/model_eval 账本采集器测试。

用 tmp_path 合成迷你 transcript（Claude jsonl / Codex rollout / dispatch.jsonl），
覆盖 join、worktree 复用切分、缺 worktree_path 降级、缺价目、Codex 累计值、
损坏行容错与「跑两次输出一致」的幂等。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.model_eval.collect import claude_dir_name, main
from scripts.model_eval.collect_pure import (
    Usage,
    build_ledger,
    parse_claude_transcript,
    parse_codex_transcript,
    parse_ts,
)


def _epoch(ts: str) -> float:
    value = parse_ts(ts)
    assert value is not None
    return value


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _claude_line(
    cwd: str,
    message_id: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_write: int = 0,
    reasoning: int = 0,
) -> dict:
    return {
        "cwd": cwd,
        "timestamp": "2026-10-30T02:00:00Z",
        "type": "assistant",
        "uuid": f"{message_id}-line",
        "message": {
            "id": message_id,
            "model": model,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_write,
                "output_tokens_details": {"thinking_tokens": reasoning},
            },
        },
    }


def _write_claude(claude_dir: Path, worktree: str, session: str, rows: list[dict], mtime: str) -> Path:
    path = claude_dir / claude_dir_name(worktree) / f"{session}.jsonl"
    rows = [dict(r, timestamp=mtime) if r.get("type") == "assistant" else r for r in rows]
    _write_jsonl(path, rows)
    stamp = _epoch(mtime)
    os.utime(path, (stamp, stamp))
    return path


def _codex_rollout(cwd: str, model: str | None, totals: list[dict]) -> list[dict]:
    rows = [
        {"type": "session_meta", "payload": {"cwd": cwd}},
        {"type": "turn_context", "payload": {"cwd": cwd, "model": model}},
    ]
    for total in totals:
        rows.append(
            {
                "type": "token_usage_record",
                "payload": {"thread_id": "thread-1", "thread_token_usage": total},
            }
        )
    return rows


def _total(input_tokens: int, output_tokens: int, cached: int = 0, reasoning: int = 0) -> dict:
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": 0,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning,
    }


def _write_codex(codex_dir: Path, name: str, rows: list[dict], mtime: str) -> Path:
    path = codex_dir / "2026" / "10" / "30" / f"rollout-{name}.jsonl"
    rows = [dict(r, timestamp=datetime.fromtimestamp(_epoch(mtime) + i, timezone.utc).isoformat()) for i, r in enumerate(rows)]
    _write_jsonl(path, rows)
    stamp = _epoch(mtime)
    os.utime(path, (stamp, stamp))
    return path


def _price(input_: float = 1.0, output: float = 2.0, cache_read: float = 0.5, cache_write: float = 3.0) -> dict:
    return {
        "input": input_,
        "output": output,
        "cache_read": cache_read,
        "cache_write": cache_write,
    }


def _dispatch(ts: str, issue: int, **data) -> dict:
    if parse_ts(ts) is not None:
        data.setdefault("started_at", ts)
        data.setdefault("ended_at", (datetime.fromtimestamp(_epoch(ts), timezone.utc) + timedelta(hours=2)).isoformat())
    return {"ts": ts, "type": "dispatch", "issue": issue, "data": data}


def _run_cli(tmp_path: Path, dispatch_rows: list[dict], prices: dict, *, dry_run: bool = False):
    dispatch = _write_jsonl(tmp_path / "dispatch.jsonl", dispatch_rows)
    pricing = tmp_path / "pricing.json"
    pricing.write_text(json.dumps({"currency": "CNY", "prices": prices}), encoding="utf-8")
    claude_dir = tmp_path / "claude"
    codex_dir = tmp_path / "codex"
    out = tmp_path / "eval" / "ledger.jsonl"
    argv = [
        "collect",
        "--no-orca",
        "--dispatch-jsonl",
        str(dispatch),
        "--out",
        str(out),
        "--claude-dir",
        str(claude_dir),
        "--codex-dir",
        str(codex_dir),
        "--pricing",
        str(pricing),
    ]
    if dry_run:
        argv.append("--dry-run")
    return main(argv), out


def _read_ledger(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# ============================================================
# 正常 join
# ============================================================
def test_normal_join_claude_and_codex(tmp_path, capsys):
    worktree = "/Users/x/orca/workspaces/DailyCheck/wt-a"
    _write_claude(
        tmp_path / "claude",
        worktree,
        "session-1",
        [
            _claude_line(worktree, "msg-1", "qwen3.8-max", 1000, 200),
            _claude_line(worktree, "msg-1", "qwen3.8-max", 1000, 200),  # 重复行只计一次
            _claude_line(worktree, "msg-2", "qwen3.8-max", 500, 100, cache_read=300, cache_write=50),
        ],
        "2026-10-30T11:00:00+08:00",
    )
    _write_codex(
        tmp_path / "codex",
        "a",
        _codex_rollout(worktree, "gpt-6.1-sol", [_total(1000, 200), _total(2000, 500, cached=400)]),
        "2026-10-30T11:30:00+08:00",
    )

    code, out = _run_cli(
        tmp_path,
        [_dispatch("2026-10-30T10:00:00+08:00", 91, worktree_path=worktree, model="qwen3.8-max")],
        {"qwen3.8-max": _price(), "gpt-6.1-sol": _price()},
    )
    assert code == 0
    rows = _read_ledger(out)
    assert len(rows) == 1
    row = rows[0]
    assert row["issue"] == 91
    assert row["model_requested"] == "qwen3.8-max"
    assert row["model_effective"] is None
    assert row["actual_models"] == ["gpt-6.1-sol", "qwen3.8-max"]
    assert row["agent"] == "mixed"
    assert row["tokens"] == {
        "input": 1500 + (2000 - 400),
        "output": 300 + 500,
        "cache_read": 300 + 400,
        "cache_write": 50,
        "reasoning": 0,
    }
    expected_cost = (3100 * 1 + 800 * 2 + 700 * 0.5 + 50 * 3) / 1_000_000
    assert row["cost"] == pytest.approx(expected_cost)
    assert "multiple_models" in row["warnings"]
    capsys.readouterr()


def test_claude_duplicate_message_id_counted_once():
    lines = [
        json.dumps(_claude_line("/w", "msg-1", "m", 100, 10)),
        json.dumps(_claude_line("/w", "msg-1", "m", 100, 10)),
        json.dumps(_claude_line("/w", "msg-2", "m", 50, 5)),
    ]
    transcript = parse_claude_transcript(lines, "/fake.jsonl", 0.0)
    assert transcript.usage_by_model["m"] == Usage(input=150, output=15)


# ============================================================
# 同一 worktree 复用：按 mtime 切分
# ============================================================
def test_worktree_reuse_splits_by_measured_intervals(tmp_path):
    worktree = "/Users/x/orca/workspaces/DailyCheck/wt-reuse"
    _write_claude(
        tmp_path / "claude",
        worktree,
        "session-early",
        [_claude_line(worktree, "m1", "qwen3.8-max", 100, 10)],
        "2026-10-30T11:00:00+08:00",
    )
    _write_claude(
        tmp_path / "claude",
        worktree,
        "session-late",
        [_claude_line(worktree, "m2", "qwen3.8-max", 900, 90)],
        "2026-10-30T13:00:00+08:00",
    )

    code, out = _run_cli(
        tmp_path,
        [
            _dispatch("2026-10-30T10:00:00+08:00", 1, worktree_path=worktree),
            _dispatch("2026-10-30T12:00:00+08:00", 2, worktree_path=worktree),
        ],
        {"qwen3.8-max": _price()},
    )
    assert code == 0
    rows = _read_ledger(out)
    assert [row["issue"] for row in rows] == [1, 2]
    assert rows[0]["tokens"]["input"] == 100
    assert rows[1]["tokens"]["input"] == 900


def test_transcript_outside_dispatch_windows_is_not_attributed(tmp_path):
    worktree = "/Users/x/orca/workspaces/DailyCheck/wt-window"
    _write_claude(
        tmp_path / "claude",
        worktree,
        "session-before",
        [_claude_line(worktree, "m1", "qwen3.8-max", 100, 10)],
        "2026-10-30T09:00:00+08:00",
    )

    code, out = _run_cli(
        tmp_path,
        [_dispatch("2026-10-30T10:00:00+08:00", 3, worktree_path=worktree)],
        {"qwen3.8-max": _price()},
    )
    assert code == 0
    rows = _read_ledger(out)
    assert rows[0]["tokens"] is None
    assert "no_transcripts" in rows[0]["warnings"]


# ============================================================
# 降级
# ============================================================
def test_missing_worktree_path_row_is_kept(tmp_path):
    code, out = _run_cli(
        tmp_path,
        [_dispatch("2026-10-30T10:00:00+08:00", 4, worker_id="ctx_1", task_id="task_1")],
        {},
    )
    assert code == 0
    rows = _read_ledger(out)
    assert len(rows) == 1
    assert rows[0]["tokens"] is None
    assert rows[0]["cost"] is None
    assert "no_worktree_path" in rows[0]["warnings"]
    assert rows[0]["worker_id"] == "ctx_1"


def test_missing_pricing_model_yields_null_cost(tmp_path):
    worktree = "/Users/x/orca/workspaces/DailyCheck/wt-price"
    _write_claude(
        tmp_path / "claude",
        worktree,
        "session-1",
        [_claude_line(worktree, "m1", "mystery-model", 100, 10)],
        "2026-10-30T11:00:00+08:00",
    )

    code, out = _run_cli(
        tmp_path,
        [_dispatch("2026-10-30T10:00:00+08:00", 5, worktree_path=worktree)],
        {"qwen3.8-max": _price()},
    )
    assert code == 0
    row = _read_ledger(out)[0]
    assert row["tokens"] is not None
    assert row["cost"] is None
    assert "no_pricing:mystery-model" in row["warnings"]


# ============================================================
# Codex 累计值
# ============================================================
def test_codex_cumulative_total_uses_last_record_only(tmp_path):
    worktree = "/Users/x/orca/workspaces/DailyCheck/wt-codex"
    _write_codex(
        tmp_path / "codex",
        "cumulative",
        _codex_rollout(worktree, "gpt-6.1-sol", [_total(111, 11), _total(222, 22)]),
        "2026-10-30T11:00:00+08:00",
    )

    code, out = _run_cli(
        tmp_path,
        [_dispatch("2026-10-30T10:00:00+08:00", 6, worktree_path=worktree)],
        {"gpt-6.1-sol": _price()},
    )
    assert code == 0
    row = _read_ledger(out)[0]
    assert row["tokens"]["input"] == 222
    assert row["tokens"]["output"] == 22
    assert row["agent"] == "codex"


def test_codex_legacy_token_count_uses_last_cumulative():
    lines = [
        json.dumps({"type": "session_meta", "payload": {"cwd": "/w", "model": "gpt-6.1-sol"}}),
        json.dumps(
            {"type": "token_count", "payload": {"info": {"total_token_usage": _total(111, 11)}}}
        ),
        json.dumps(
            {"type": "token_count", "payload": {"info": {"total_token_usage": _total(222, 22)}}}
        ),
    ]
    transcript = parse_codex_transcript(lines, "/fake-rollout.jsonl", 0.0)
    assert transcript.usage_by_model["gpt-6.1-sol"] == Usage(input=222, output=22)


# ============================================================
# 损坏行容错
# ============================================================
def test_corrupt_lines_degrade_without_dropping_row(tmp_path):
    worktree = "/Users/x/orca/workspaces/DailyCheck/wt-corrupt"
    path = tmp_path / "claude" / claude_dir_name(worktree) / "session-bad.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        "{not json\n"
        + json.dumps(_claude_line(worktree, "m1", "qwen3.8-max", 100, 10))
        + "\n",
        encoding="utf-8",
    )
    stamp = _epoch("2026-10-30T11:00:00+08:00")
    os.utime(path, (stamp, stamp))

    dispatch = _write_jsonl(
        tmp_path / "dispatch.jsonl",
        [
            _dispatch("2026-10-30T10:00:00+08:00", 7, worktree_path=worktree),
            {"ts": "not-a-ts", "type": "dispatch", "issue": 8, "data": {}},
        ],
    )
    pricing = tmp_path / "pricing.json"
    pricing.write_text(
        json.dumps({"currency": "CNY", "prices": {"qwen3.8-max": _price()}}), encoding="utf-8"
    )
    out = tmp_path / "eval" / "ledger.jsonl"

    code = main(
        [
            "collect",
        "--no-orca",
            "--dispatch-jsonl",
            str(dispatch),
            "--out",
            str(out),
            "--claude-dir",
            str(tmp_path / "claude"),
            "--codex-dir",
            str(tmp_path / "codex"),
            "--pricing",
            str(pricing),
        ]
    )
    assert code == 0
    rows = _read_ledger(out)
    assert [row["issue"] for row in rows] == [7, 8]
    assert rows[0]["tokens"]["input"] == 100
    assert any("bad_lines" in warning for warning in rows[0]["warnings"])
    assert "bad_ts" in rows[1]["warnings"]
    assert rows[1]["tokens"] is None


def test_unreadable_dispatch_file_exits_nonzero(tmp_path):
    code = main(
        [
            "collect",
        "--no-orca",
            "--dispatch-jsonl",
            str(tmp_path / "missing.jsonl"),
            "--out",
            str(tmp_path / "ledger.jsonl"),
            "--claude-dir",
            str(tmp_path / "claude"),
            "--codex-dir",
            str(tmp_path / "codex"),
            "--pricing",
            str(tmp_path / "pricing.json"),
        ]
    )
    assert code == 2
    assert not (tmp_path / "ledger.jsonl").exists()


# ============================================================
# 幂等 / dry-run
# ============================================================
def test_cli_is_idempotent(tmp_path):
    worktree = "/Users/x/orca/workspaces/DailyCheck/wt-idem"
    _write_claude(
        tmp_path / "claude",
        worktree,
        "session-1",
        [_claude_line(worktree, "m1", "qwen3.8-max", 100, 10)],
        "2026-10-30T11:00:00+08:00",
    )
    dispatch_rows = [_dispatch("2026-10-30T10:00:00+08:00", 9, worktree_path=worktree)]

    assert _run_cli(tmp_path, dispatch_rows, {"qwen3.8-max": _price()})[0] == 0
    first = (tmp_path / "eval" / "ledger.jsonl").read_bytes()

    assert _run_cli(tmp_path, dispatch_rows, {"qwen3.8-max": _price()})[0] == 0
    second = (tmp_path / "eval" / "ledger.jsonl").read_bytes()
    assert first == second


def test_dry_run_prints_without_writing(tmp_path, capsys):
    worktree = "/Users/x/orca/workspaces/DailyCheck/wt-dry"
    _write_claude(
        tmp_path / "claude",
        worktree,
        "session-1",
        [_claude_line(worktree, "m1", "qwen3.8-max", 100, 10)],
        "2026-10-30T11:00:00+08:00",
    )

    code, out = _run_cli(
        tmp_path,
        [_dispatch("2026-10-30T10:00:00+08:00", 10, worktree_path=worktree)],
        {"qwen3.8-max": _price()},
        dry_run=True,
    )
    assert code == 0
    assert not out.exists()
    printed = capsys.readouterr().out.strip().splitlines()
    assert len(printed) == 1
    assert json.loads(printed[0])["issue"] == 10


# ============================================================
# 纯函数：缺 worktree_path 的派单不参与归属
# ============================================================
def test_build_ledger_pure_missing_worktree():
    transcripts = [parse_claude_transcript([], "/x.jsonl", 0.0)]
    rows = build_ledger([_dispatch("2026-10-30T10:00:00+08:00", 11)], transcripts, {})
    assert rows[0]["tokens"] is None
    assert rows[0]["warnings"] == ["no_worktree_path"]
