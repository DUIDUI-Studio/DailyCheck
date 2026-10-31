"""Compatibility export for the authoritative tool registration."""
from __future__ import annotations

from mcp.types import Tool


def get_tools() -> list[Tool]:
    """Return independent declarations in the original published order."""
    from mcp_server.protocol.registry import get_tools as registered_tools

    return registered_tools()
