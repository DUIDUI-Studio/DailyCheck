"""Token 验证与 AuthContext。"""
from __future__ import annotations

import contextvars
import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from werkzeug.security import check_password_hash

from mcp_server.data.unit_of_work import master_connection, policy_master_connection
from mcp_server.infra.errors import UnauthorizedError
from mcp_server.service.policy import POLICY_TOKEN_PREFIX, BoundPolicy, load_binding

# Plaintext prefix length kept for an indexed lookup. 8 chars of a
# token_urlsafe(32) secret reveal nothing usable, while turning the lookup
# into a single indexed row instead of a full-table pbkdf2 scan.
# MUST match config.AGENT_TOKEN_PREFIX_LEN (the value written at creation).
TOKEN_PREFIX_LEN = 8

_SELECT_COLS = (
    "SELECT id, token_hash, revoked_at, allowed_read_paths_json, "
    "allowed_write_paths_json, allowed_warehouse_codes_json, rate_limit "
    "FROM agent_tokens "
)
_BY_PREFIX = _SELECT_COLS + "WHERE token_prefix = ? AND revoked_at IS NULL"
_POLICY_CREDENTIAL = re.compile(re.escape(POLICY_TOKEN_PREFIX) + r"[A-Za-z0-9_-]{43}")


def reserved_credential(raw: str) -> bool:
    return raw.lower().startswith(POLICY_TOKEN_PREFIX)


def token_prefix(raw: str) -> str:
    """Return the indexed lookup prefix for a raw token."""
    return raw[:TOKEN_PREFIX_LEN]


@dataclass
class AuthContext:
    token_id: int
    allowed_read_paths: list[str]
    allowed_write_paths: list[str]
    allowed_warehouses: list[str] | None  # None = all warehouses
    rate_limit: int | None = None  # 每分钟配额，None = 默认（rate_limit_pure）
    read_only: bool = False  # Explicit bot query connection boundary.
    warehouse_base_dir: str | None = None  # Bot data root, independent of code/CWD.
    policy: BoundPolicy | None = None


def _authenticate_policy(raw: str) -> AuthContext | None:
    with policy_master_connection() as conn:
        rows = conn.execute(_BY_PREFIX, (token_prefix(raw),)).fetchall()
        matches = [row for row in rows if row["token_hash"] and check_password_hash(row["token_hash"], raw)]
        if len(matches) != 1:
            return None
        row = matches[0]
        if any(row[key] != "[]" for key in (
            "allowed_read_paths_json", "allowed_write_paths_json", "allowed_warehouse_codes_json",
        )):
            return None
        policy = load_binding(conn, row["id"])
        if policy is None:
            return None
        return AuthContext(row["id"], [], [], [], row["rate_limit"], read_only=True, policy=policy)


def authenticate(authorization_header: str, *, allow_policy: bool = False) -> AuthContext | None:
    """验证 Bearer token，返回 AuthContext 或 None。

    快路径：按 token_prefix 索引等值查 → 最多一次 pbkdf2 校验。
    回退：token_prefix 为 NULL 的历史行（建 token_prefix 之前签发的），
    走小范围扫描；已吊销的行不在任一查询里，避免为死 token 付 pbkdf2。
    """
    if not authorization_header.startswith("Bearer "):
        return None
    presented = authorization_header[len("Bearer "):]
    raw = presented.strip()
    if reserved_credential(raw):
        if allow_policy and _POLICY_CREDENTIAL.fullmatch(presented):
            return _authenticate_policy(raw)
        return None
    if not raw:
        return None
    with master_connection() as conn:
        rows = conn.execute(
            _BY_PREFIX,
            (token_prefix(raw),),
        ).fetchall()
        if not rows:
            rows = conn.execute(
                _SELECT_COLS + "WHERE token_prefix IS NULL AND revoked_at IS NULL"
            ).fetchall()
    for row in rows:
        if row["token_hash"] is None:
            continue
        if check_password_hash(row["token_hash"], raw):
            try:
                read_paths = json.loads(row["allowed_read_paths_json"] or "[]")
                write_paths = json.loads(row["allowed_write_paths_json"] or "[]")
                wh_codes = json.loads(row["allowed_warehouse_codes_json"] or "null")
            except (ValueError, TypeError):
                continue
            return AuthContext(
                token_id=row["id"],
                allowed_read_paths=read_paths,
                allowed_write_paths=write_paths,
                allowed_warehouses=wh_codes,
                rate_limit=row["rate_limit"],
            )
    return None


def check_warehouse(ctx: AuthContext, warehouse_code: str) -> bool:
    """检查 warehouse_code 是否在 token 白名单中。"""
    if ctx.allowed_warehouses is None:
        return True  # None = all warehouses
    return warehouse_code in ctx.allowed_warehouses


def check_path(ctx: AuthContext, method: str, path: str) -> bool:
    """检查 (method, path) 是否在 token 白名单中。"""
    from mcp_server.agent_mpc_pure import path_matches
    paths = (
        ctx.allowed_write_paths if method != "GET"
        else ctx.allowed_read_paths
    )
    for pat in paths:
        if path_matches(pat, path):
            return True
    return False


# ---------------------------------------------------------------------------
# Request-scoped auth context
#
# Tool handlers are plain sync functions invoked from the MCP SDK with just
# (name, arguments) — they cannot see the ASGI scope. AuthMiddleware therefore
# publishes the request's own AuthContext here, and handlers read it back via
# resolve_ctx(). Without this, every handler re-authenticated the *process*
# env token, so a caller's per-token warehouse/path ACLs were ignored.
# ---------------------------------------------------------------------------

_current_auth: contextvars.ContextVar[AuthContext | None] = contextvars.ContextVar(
    "dailycheck_current_auth", default=None
)


def set_current_auth(ctx: AuthContext | None) -> None:
    """Publish the AuthContext of the in-flight request (called by AuthMiddleware)."""
    _current_auth.set(ctx)


@contextmanager
def request_auth(ctx: AuthContext) -> Iterator[None]:
    token = _current_auth.set(ctx)
    try:
        yield
    finally:
        _current_auth.reset(token)


def resolve_ctx() -> AuthContext:
    """Return the AuthContext governing the current tool call.

    HTTP transport: AuthMiddleware already authenticated the caller's own
    Bearer token and published it via set_current_auth() — use it, so that
    token's warehouse/path ACLs actually apply.

    stdio transport (local Claude Code etc.): there is no ASGI scope and no
    per-request token, so the process-level DAILYCHECK_MCP_TOKEN *is* the
    identity. That fallback is a required path there, not a convenience.

    Raises UnauthorizedError when neither source yields a context.
    """
    ctx = _current_auth.get()
    if ctx is not None:
        if ctx.policy is not None:
            raise UnauthorizedError("Use the bound MCP policy")
        return ctx
    token = os.environ.get("DAILYCHECK_MCP_TOKEN", "")
    if not token:
        raise UnauthorizedError("no auth context; DAILYCHECK_MCP_TOKEN not set")
    ctx = authenticate(f"Bearer {token}")
    if ctx is None:
        raise UnauthorizedError("invalid token")
    return ctx
