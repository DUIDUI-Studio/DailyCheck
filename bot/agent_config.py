"""Bot 有效配置（Issue #79 PR1）：每条消息读取一次。

设计边界
--------
- **只读** master.db 的 ``bot_agent_config``，不 import 任何业务写 service。
- **只收窄现有 9 项**：``TOOL_NAMES`` 是当前唯一可执行的 bot 工具集；
  ``tool.<mcp 名称>`` 或未知名称写成 on 也不能扩大执行集合（PR1 不接入 MCP）。
- **失败回退**：缺表 / 锁库 / 读取异常 / 坏配置 → 9 项全开 + ``medium``。
  回退绝不启用新写能力（9 项本来就是只读工具）。
- ``reasoning_effort`` 只接受 5 档；非法值（含 ``none``）不允许进入模型请求。

本模块刻意只依赖 stdlib + ``bot.tool_schema``，方便 Flask 配置页与 bot 进程共用。
"""
from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bot.tool_schema import TOOL_NAMES

logger = logging.getLogger("bot.agent_config")

REASONING_EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_REASONING_EFFORT = "medium"
DEFAULT_MODEL = "MiniMax-M3.1-Flash"

KEY_REASONING_EFFORT = "llm.reasoning_effort"
KEY_WRITE_ALLOWED_USERIDS = "write.allowed_userids"
TOOL_KEY_PREFIX = "tool."

_TRUE_VALUES = {"1", "true", "on", "yes"}
_FALSE_VALUES = {"0", "false", "off", "no"}


class ConfigError(ValueError):
    """配置值非法；调用方应整体回退，而不是保留半套开关。"""


def normalize_reasoning_effort(raw: str | None) -> str:
    """校验并归一化思考强度；空值/非法值一律抛 ConfigError。"""
    value = (raw or "").strip().lower()
    if value not in REASONING_EFFORTS:
        raise ConfigError(
            f"llm.reasoning_effort 非法（只允许 {'|'.join(REASONING_EFFORTS)}）"
        )
    return value


def parse_tool_enabled(raw: str | None) -> bool:
    """严格解析工具开关；非法值抛 ConfigError（触发整体回退）。"""
    value = (raw or "").strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ConfigError("tool 开关只允许 1/0、true/false、on/off")


def resolve_model_name(env: Mapping[str, str] | None = None) -> str:
    """模型名只读来源：部署侧 LLM_MODEL，缺省 MiniMax-M3.1-Flash。"""
    source = env if env is not None else os.environ
    return (source.get("LLM_MODEL") or DEFAULT_MODEL).strip() or DEFAULT_MODEL


@dataclass(frozen=True)
class BotAgentConfig:
    """一次消息的有效配置快照。

    ``enabled_tools`` 永远只会是 ``TOOL_NAMES`` 的子集；MCP 工具/未知名称
    不会出现在这里（PR1 不接入 MCP 执行能力）。
    """

    reasoning_effort: str
    enabled_tools: frozenset[str]
    write_allowed_userids: tuple[str, ...]
    degraded: bool = False
    degraded_reason: str | None = None

    def __post_init__(self) -> None:
        normalized = normalize_reasoning_effort(self.reasoning_effort)
        if normalized != self.reasoning_effort:
            raise ConfigError("reasoning_effort 必须使用小写规范值")
        if not self.enabled_tools <= TOOL_NAMES:
            raise ConfigError("enabled_tools 含非现有 bot 工具名")


def _fallback(reason: str) -> BotAgentConfig:
    return BotAgentConfig(
        reasoning_effort=DEFAULT_REASONING_EFFORT,
        enabled_tools=frozenset(TOOL_NAMES),
        write_allowed_userids=(),
        degraded=True,
        degraded_reason=reason,
    )


def normalize_userids(raw: str | None) -> tuple[str, ...]:
    """逗号分隔的企微 userid 白名单；空串 = 无白名单。"""
    seen: set[str] = set()
    ordered: list[str] = []
    for item in (raw or "").split(","):
        value = item.strip()
        if value and value not in seen:
            seen.add(value)
            ordered.append(value)
    return tuple(ordered)


def _read_rows(path: Path) -> list[tuple[Any, Any]]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    try:
        return list(conn.execute("SELECT key, value FROM bot_agent_config").fetchall())
    finally:
        conn.close()


def load_effective_config(
    master_db_path: str | Path | None = None,
) -> BotAgentConfig:
    """读取一次有效配置；任何异常都回退到 9 项 + medium。

    调用方（``bot.agent.answer_stream``）必须在每条消息开始时只调用一次，
    并把返回的快照同时用于请求体与 ``_run_tool`` 过滤。
    """
    if master_db_path is not None:
        path = Path(master_db_path)
    else:
        from bot.tools import db_dir

        path = db_dir() / "master.db"

    try:
        rows = _read_rows(path)
    except (sqlite3.Error, OSError) as exc:
        reason = f"read_failed:{type(exc).__name__}"
        logger.warning("bot 配置读取失败，回退 9 项 + medium（%s）", reason)
        return _fallback(reason)

    reasoning_effort = DEFAULT_REASONING_EFFORT
    enabled = set(TOOL_NAMES)
    write_userids: tuple[str, ...] = ()

    try:
        for key, value in rows:
            if key == KEY_REASONING_EFFORT:
                reasoning_effort = normalize_reasoning_effort(value)
            elif key == KEY_WRITE_ALLOWED_USERIDS:
                write_userids = normalize_userids(value)
            elif isinstance(key, str) and key.startswith(TOOL_KEY_PREFIX):
                name = key[len(TOOL_KEY_PREFIX):]
                # 只认现有 9 项；MCP/未知名称忽略，绝不扩大执行集合。
                if name in TOOL_NAMES:
                    if not parse_tool_enabled(value):
                        enabled.discard(name)
    except ConfigError as exc:
        logger.warning("bot 配置非法，回退 9 项 + medium（%s）", exc)
        return _fallback(str(exc))

    return BotAgentConfig(
        reasoning_effort=reasoning_effort,
        enabled_tools=frozenset(enabled),
        write_allowed_userids=write_userids,
    )
