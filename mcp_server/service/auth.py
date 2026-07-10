"""Token 验证与 AuthContext。"""
from __future__ import annotations

import json
from dataclasses import dataclass

from werkzeug.security import check_password_hash

from mcp_server.data.unit_of_work import master_connection


@dataclass
class AuthContext:
    token_id: int
    allowed_read_paths: list[str]
    allowed_write_paths: list[str]
    allowed_warehouses: list[str] | None  # None = all warehouses


def authenticate(authorization_header: str) -> AuthContext | None:
    """验证 Bearer token，返回 AuthContext 或 None。"""
    if not authorization_header.startswith("Bearer "):
        return None
    raw = authorization_header[len("Bearer "):].strip()
    if not raw:
        return None
    with master_connection() as conn:
        rows = conn.execute(
            "SELECT id, token_hash, allowed_read_paths_json, "
            "allowed_write_paths_json, allowed_warehouse_codes_json "
            "FROM agent_tokens WHERE token_hash IS NOT NULL"
        ).fetchall()
    for row in rows:
        if row["token_hash"] is None:
            continue
        if check_password_hash(row["token_hash"], raw):
            if row["revoked_at"] is not None:
                return None
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
