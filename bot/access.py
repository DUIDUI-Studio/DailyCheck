"""Trusted per-message policy; no token lookup, model parameters or ambient auth.

BOT_AUTH_POLICY points to an operator-owned JSON file keyed by exact WeCom userid.
Missing policy preserves legacy global inventory reads, but grants no writes or
canonical global reads. With a policy file, unknown users have no data access.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from bot.agent_config import verified_owner_userid
from mcp_server.service.auth import AuthContext


@dataclass(frozen=True)
class SenderAccess:
    userid: str
    read_paths: tuple[str, ...] = ("*",)
    write_paths: tuple[str, ...] = ()
    warehouses: tuple[str, ...] | None = None
    global_read: bool = False
    identity_verified: bool = False

    def read_context(self) -> AuthContext:
        return AuthContext(
            token_id=0, allowed_read_paths=list(self.read_paths), allowed_write_paths=[],
            allowed_warehouses=None if self.warehouses is None else list(self.warehouses),
            read_only=True,
        )


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
        raise ValueError("policy entries must be string arrays")
    return tuple(value)


def load_sender_access(userid: str | None) -> SenderAccess:
    who = userid if isinstance(userid, str) else ""
    path = os.environ.get("BOT_AUTH_POLICY")
    if not path:
        return SenderAccess(who)
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        row = data.get(who, {})
        if not isinstance(row, dict):
            raise ValueError("invalid sender policy")
        warehouses = row.get("warehouses", [])
        global_read = row.get("global_read", False)
        if not isinstance(global_read, bool):
            raise ValueError("global_read must be boolean")
        return SenderAccess(
            who, _strings(row.get("read_paths", [])), _strings(row.get("write_paths", [])),
            None if warehouses is None else _strings(warehouses), global_read,
            bool(verified_owner_userid()),
        )
    except (OSError, ValueError, TypeError, AttributeError):
        return SenderAccess(who, (), (), (), False)
