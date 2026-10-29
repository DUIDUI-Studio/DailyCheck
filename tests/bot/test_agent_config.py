"""bot 每条消息的有效配置读取（Issue #79 PR1）。

合同：
- 只读 master.db 的 bot_agent_config；每条消息调用一次。
- 缺表 / 锁库 / 读取异常 / 坏配置 → 回退「现有 9 个暴露工具 + medium」。
- 只允许 5 档思考强度；非法值（含 none）不得进入模型请求。
- 配置只能收窄现有 9 项；MCP 名称/未知名称写成 on 也不会进入执行集合。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bot.agent_config import (
    DEFAULT_REASONING_EFFORT,
    REASONING_EFFORTS,
    BotAgentConfig,
    ConfigError,
    load_effective_config,
    normalize_reasoning_effort,
)
from bot.tool_schema import TOOL_NAMES

_TABLE_SQL = """
CREATE TABLE bot_agent_config (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT,
    updated_by TEXT
)
"""


def _make_master(path: Path, rows: dict[str, str] | None = None) -> Path:
    conn = sqlite3.connect(path)
    conn.execute(_TABLE_SQL)
    for key, value in (rows or {}).items():
        conn.execute(
            "INSERT INTO bot_agent_config (key, value, updated_at, updated_by) "
            "VALUES (?, ?, '2026-10-29 10:00:00', 'tester')",
            (key, value),
        )
    conn.commit()
    conn.close()
    return path


def test_missing_table_falls_back_to_nine_tools_and_medium(tmp_path):
    master = tmp_path / "master.db"
    sqlite3.connect(master).close()

    cfg = load_effective_config(master)

    assert cfg.degraded is True
    assert cfg.reasoning_effort == "medium"
    assert set(cfg.enabled_tools) == TOOL_NAMES
    assert cfg.degraded_reason


def test_empty_table_uses_defaults(tmp_path):
    master = _make_master(tmp_path / "master.db")

    cfg = load_effective_config(master)

    assert cfg.degraded is False
    assert cfg.reasoning_effort == DEFAULT_REASONING_EFFORT
    assert set(cfg.enabled_tools) == TOOL_NAMES


@pytest.mark.parametrize("effort", REASONING_EFFORTS)
def test_all_five_efforts_are_accepted(tmp_path, effort):
    master = _make_master(
        tmp_path / "master.db", {"llm.reasoning_effort": effort}
    )

    cfg = load_effective_config(master)

    assert cfg.degraded is False
    assert cfg.reasoning_effort == effort


def test_valid_tool_switch_disables_one_tool(tmp_path):
    master = _make_master(
        tmp_path / "master.db",
        {
            "llm.reasoning_effort": "high",
            "tool.list_orders": "0",
            "tool.search_items": "1",
        },
    )

    cfg = load_effective_config(master)

    assert cfg.reasoning_effort == "high"
    assert "list_orders" not in cfg.enabled_tools
    assert "search_items" in cfg.enabled_tools
    assert set(cfg.enabled_tools) < TOOL_NAMES


def test_invalid_effort_none_falls_back(tmp_path):
    master = _make_master(
        tmp_path / "master.db",
        {"llm.reasoning_effort": "none", "tool.list_orders": "0"},
    )

    cfg = load_effective_config(master)

    assert cfg.degraded is True
    assert cfg.reasoning_effort == "medium"
    assert set(cfg.enabled_tools) == TOOL_NAMES, "坏配置不得保留半套开关"


def test_invalid_tool_value_falls_back(tmp_path):
    master = _make_master(
        tmp_path / "master.db", {"tool.list_orders": "maybe"}
    )

    cfg = load_effective_config(master)

    assert cfg.degraded is True
    assert set(cfg.enabled_tools) == TOOL_NAMES


def test_read_error_falls_back(tmp_path, monkeypatch):
    master = _make_master(tmp_path / "master.db")

    def _boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("bot.agent_config.sqlite3.connect", _boom)

    cfg = load_effective_config(master)

    assert cfg.degraded is True
    assert cfg.reasoning_effort == "medium"
    assert set(cfg.enabled_tools) == TOOL_NAMES


def test_mcp_and_unknown_tool_names_never_enter_execution_set(tmp_path):
    """MCP 名称/未知名称即使被写成 1，也不能扩大现有 9 项执行集合。"""
    master = _make_master(
        tmp_path / "master.db",
        {
            "tool.warehouse_list": "1",
            "tool.restock_create": "1",
            "tool.not_a_real_tool": "1",
        },
    )

    cfg = load_effective_config(master)

    assert set(cfg.enabled_tools) == TOOL_NAMES


def test_write_userids_are_parsed_but_do_not_enable_writes(tmp_path):
    master = _make_master(
        tmp_path / "master.db",
        {"write.allowed_userids": " U1, U2 ,, U1 "},
    )

    cfg = load_effective_config(master)

    assert cfg.write_allowed_userids == ("U1", "U2")
    assert set(cfg.enabled_tools) == TOOL_NAMES, "PR1 不因白名单开放写执行"


@pytest.mark.parametrize("bad", ["none", "", "HIGHEST", "off"])
def test_normalize_rejects_invalid_effort(bad):
    with pytest.raises(ConfigError):
        normalize_reasoning_effort(bad)


def test_config_object_requires_valid_effort():
    with pytest.raises(ConfigError):
        BotAgentConfig(
            reasoning_effort="none",
            enabled_tools=frozenset(TOOL_NAMES),
            write_allowed_userids=(),
        )
