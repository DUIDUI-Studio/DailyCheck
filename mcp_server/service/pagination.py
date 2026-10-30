"""MCP 列表分页参数解析（Issue #74）。

服务层也可被直接调用，`limit` / `offset` 可能是字符串、None 或小数。
这里统一做整数解析，非法值转成 MCP 结构化
`ValidationError`（`protocol/server.py` 的 `call_tool` 只捕获 `McpError`，
裸 `ValueError` / `TypeError` 会穿透成非结构化错误）。
"""
from __future__ import annotations

from mcp_server.infra.errors import ValidationError

DEFAULT_LIMIT = 100
MAX_LIMIT = 500


def _as_int(value: object, field: str) -> int:
    """严格解析整数：拒绝 None / 布尔 / 小数 / 非数字字符串。"""
    if isinstance(value, bool):
        raise ValidationError(f"{field} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            raise ValidationError(f"{field} must be an integer") from None
    raise ValidationError(f"{field} must be an integer")


def parse_limit(value: object = DEFAULT_LIMIT) -> int:
    """limit：默认 100，clamp 到 [1, 500]；非整数抛 ValidationError。"""
    return max(1, min(_as_int(value, "limit"), MAX_LIMIT))


def parse_offset(value: object = 0) -> int:
    """offset：默认 0，clamp 到 >=0；非整数抛 ValidationError。"""
    return max(0, _as_int(value, "offset"))
