"""Per-message PR2 configuration, with a fixed PR1 read-only failure fallback."""
from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bot.tool_contract import EXTRA_READS, LEGACY_PARENTS, MCP_NAMES
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

    只包含固定批准池；动态注册项与七个 canonical 写永远不能通过配置授权。
    """

    reasoning_effort: str
    enabled_tools: frozenset[str]
    write_allowed_userids: tuple[str, ...]
    degraded: bool = False
    degraded_reason: str | None = None
    configured_tools: frozenset[str] | None = None

    def __post_init__(self) -> None:
        normalized = normalize_reasoning_effort(self.reasoning_effort)
        if normalized != self.reasoning_effort:
            raise ConfigError("reasoning_effort 必须使用小写规范值")
        if not self.enabled_tools <= (TOOL_NAMES | MCP_NAMES):
            raise ConfigError("enabled_tools 含未批准工具名")


def _fallback(reason: str) -> BotAgentConfig:
    return BotAgentConfig(
        reasoning_effort=DEFAULT_REASONING_EFFORT,
        enabled_tools=frozenset(TOOL_NAMES),
        write_allowed_userids=(),
        degraded=True,
        degraded_reason=reason,
    )


def verified_owner_userid() -> str:
    """Operator assertion after Owner/Prod verification; never infer from historical LinJi."""
    value = (os.environ.get("BOT_OWNER_USERID") or "").strip()
    return value if value and "," not in value and not any(c.isspace() for c in value) else ""


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
    """读取一次配置；原 14 默认 on，八读显式 on，任何异常仅回退旧九项。

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
    enabled = set(TOOL_NAMES | (MCP_NAMES - EXTRA_READS))
    write_userids = (verified_owner_userid() or "LinJi",)

    try:
        for key, value in rows:
            if key == KEY_REASONING_EFFORT:
                reasoning_effort = normalize_reasoning_effort(value)
            elif key == KEY_WRITE_ALLOWED_USERIDS:
                write_userids = normalize_userids(value)
            elif isinstance(key, str) and key.startswith(TOOL_KEY_PREFIX):
                name = key[len(TOOL_KEY_PREFIX):]
                # 只认批准池；未知项/七个 canonical 写无法通过配置解锁。
                if name in TOOL_NAMES | MCP_NAMES:
                    if not parse_tool_enabled(value):
                        enabled.discard(name)
                    else:
                        enabled.add(name)
    except (ConfigError, TypeError, AttributeError) as exc:
        logger.warning("bot 配置非法，回退 9 项 + medium（%s）", exc)
        return _fallback(str(exc))

    configured_tools = frozenset(enabled)
    for child, parent in LEGACY_PARENTS.items():
        if parent not in enabled:
            enabled.discard(child)

    return BotAgentConfig(
        reasoning_effort=reasoning_effort,
        enabled_tools=frozenset(enabled),
        write_allowed_userids=write_userids,
        configured_tools=configured_tools,
    )
