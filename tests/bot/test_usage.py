"""bot 工具调用计数（Issue #79 PR1）：最小 JSONL 来源 + 7 天窗口。

不另造遥测平台：每次真实工具调用向 DB 同级的 bot_tool_calls.log 追加一行，
管理页读取最近 7 天聚合。这里钉住时间边界、坏行容错与真实写入。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from bot.usage import (
    default_call_log_path,
    read_tool_call_counts,
    record_tool_call,
)

NOW = datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc)


def _line(ts: datetime, tool: str) -> str:
    return json.dumps(
        {"ts": ts.isoformat(), "tool": tool, "ok": True}, ensure_ascii=False
    )


def test_seven_day_boundary_is_inclusive(tmp_path):
    log = tmp_path / "bot_tool_calls.log"
    cutoff = NOW - timedelta(days=7)
    log.write_text(
        "\n".join(
            [
                _line(cutoff - timedelta(seconds=1), "list_orders"),  # 窗口外
                _line(cutoff, "list_orders"),  # 边界内
                _line(cutoff + timedelta(seconds=1), "list_orders"),
                _line(NOW, "search_items"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    counts = read_tool_call_counts(path=log, days=7, now=NOW)

    assert counts["list_orders"] == 2
    assert counts["search_items"] == 1


def test_missing_file_and_malformed_lines_are_ignored(tmp_path):
    assert read_tool_call_counts(path=tmp_path / "absent.log", now=NOW) == {}

    log = tmp_path / "bot_tool_calls.log"
    log.write_text(
        "not-json\n"
        + json.dumps({"ts": "not-a-time", "tool": "x"})
        + "\n"
        + json.dumps({"ts": NOW.isoformat(), "tool": 123})
        + "\n",
        encoding="utf-8",
    )
    assert read_tool_call_counts(path=log, now=NOW) == {}


def test_z_suffix_and_naive_timestamps_are_understood(tmp_path):
    log = tmp_path / "bot_tool_calls.log"
    log.write_text(
        json.dumps({"ts": "2026-10-29T11:00:00Z", "tool": "list_orders"})
        + "\n"
        + json.dumps({"ts": "2026-10-29T11:05:00", "tool": "list_orders"})
        + "\n",
        encoding="utf-8",
    )

    assert read_tool_call_counts(path=log, now=NOW)["list_orders"] == 2


def test_record_tool_call_appends_and_updates_count(tmp_path):
    log = tmp_path / "bot_tool_calls.log"

    record_tool_call("list_orders", ok=True, path=log)
    record_tool_call("list_orders", ok=False, path=log)

    lines = [line for line in log.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 2
    assert json.loads(lines[0])["tool"] == "list_orders"
    assert read_tool_call_counts(path=log, now=datetime.now(timezone.utc))[
        "list_orders"
    ] == 2


def test_default_log_path_lives_next_to_the_db_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("BOT_TOOL_CALL_LOG", raising=False)
    db_dir = tmp_path / "db"
    db_dir.mkdir()

    explicit = default_call_log_path(master_db_path=db_dir / "master.db")
    assert explicit == tmp_path / "bot_tool_calls.log"

    monkeypatch.setenv("BOT_TOOL_CALL_LOG", str(tmp_path / "override.log"))
    assert default_call_log_path(master_db_path=db_dir / "master.db") == (
        tmp_path / "override.log"
    )
