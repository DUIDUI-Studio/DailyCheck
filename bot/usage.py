"""bot 工具调用计数（Issue #79 PR1）——最小可核验来源。

不另造遥测平台：每次真实工具调用向 **DB 同级的** ``bot_tool_calls.log``
追加一行 JSON；管理页读取最近 7 天聚合。文件路径可由 ``BOT_TOOL_CALL_LOG``
覆盖（bot 与 Web 两个进程需指向同一路径；默认都从 ``DAILYCHECK_DB_DIR``
的父目录推导，因此共用数据库时天然一致）。

为什么不是 master.db 新表：bot 的数据库连接全是 ``mode=ro``（§3.12 三层只读
防护），为计数开一条写连接会把「工具层只读」的边界撕开；文件追加更小、可审计，
且不影响业务库。失败只记 WARNING，绝不让计数拖垮问答。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("bot.usage")

CALL_LOG_ENV = "BOT_TOOL_CALL_LOG"
DEFAULT_DAYS = 7


def default_call_log_path(master_db_path: str | Path | None = None) -> Path:
    """计数文件路径：env 优先；否则放在库目录的父目录（仓库根）。"""
    override = os.environ.get(CALL_LOG_ENV)
    if override:
        return Path(override).expanduser()
    if master_db_path is not None:
        return Path(master_db_path).expanduser().resolve().parent.parent / (
            "bot_tool_calls.log"
        )
    from bot.tools import db_dir

    return db_dir().parent / "bot_tool_calls.log"


def record_tool_call(
    tool_name: str,
    ok: bool = True,
    path: str | Path | None = None,
) -> None:
    """追加一次调用记录；任何 I/O 失败都只告警，不影响回答。"""
    target = Path(path) if path is not None else default_call_log_path()
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tool": tool_name,
        "ok": bool(ok),
    }
    payload = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        fd = os.open(str(target), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
    except OSError as exc:
        logger.warning("工具调用计数写入失败（%s）: %s", target, exc)


def _parse_ts(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    value = raw.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # 兼容旧记录：无时区的按 UTC 解释（写入侧始终带 +00:00）。
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def read_tool_call_counts(
    path: str | Path | None = None,
    days: int = DEFAULT_DAYS,
    now: datetime | None = None,
) -> dict[str, int]:
    """聚合最近 ``days`` 天每个工具的调用数；坏行/缺文件返回空/跳过。"""
    target = Path(path) if path is not None else default_call_log_path()
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    cutoff = current.astimezone(timezone.utc) - timedelta(days=days)

    counts: dict[str, int] = {}
    try:
        handle = target.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return counts

    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            tool = record.get("tool")
            if not isinstance(tool, str) or not tool:
                continue
            ts = _parse_ts(record.get("ts"))
            if ts is None or ts < cutoff:
                continue
            counts[tool] = counts.get(tool, 0) + 1
    return counts
