"""Auth guard for MCP tool handlers."""
from __future__ import annotations

import os
from mcp_server.service.auth import authenticate, check_path, check_warehouse
from mcp_server.infra.errors import UnauthorizedError, ForbiddenError


def get_auth_header() -> str | None:
    return os.environ.get("DAILYCHECK_MCP_TOKEN")


def require_auth(method: str, path: str, warehouse_code: str | None = None):
    """Decorator that wraps a sync tool handler with auth checks."""

    def decorator(func):
        def wrapper(*args, **kwargs):
            auth_header = get_auth_header()
            ctx = authenticate(auth_header)
            if ctx is None:
                raise UnauthorizedError("invalid or missing token")
            if not check_path(ctx, method, path):
                raise ForbiddenError("forbidden_path")
            if warehouse_code and not check_warehouse(ctx, warehouse_code):
                raise ForbiddenError("forbidden_warehouse")
            return func(*args, **kwargs)

        wrapper.__name__ = func.__name__
        return wrapper

    return decorator
