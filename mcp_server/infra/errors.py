"""MCP Server 统一错误类型。"""
from __future__ import annotations


class McpError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 400) -> None:
        self.code = code
        self.message = message
        self.http_status = http_status
        super().__init__(message)

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message}


class UnauthorizedError(McpError):
    def __init__(self, message: str = "unauthorized") -> None:
        super().__init__("unauthorized", message, 401)


class ForbiddenError(McpError):
    def __init__(self, message: str = "forbidden") -> None:
        super().__init__("forbidden", message, 403)


class NotFoundError(McpError):
    def __init__(self, message: str = "not_found") -> None:
        super().__init__("not_found", message, 404)


class ValidationError(McpError):
    def __init__(self, message: str) -> None:
        super().__init__("validation_error", message, 400)
