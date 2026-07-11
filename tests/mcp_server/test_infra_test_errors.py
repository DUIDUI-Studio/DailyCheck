"""Test MCP Server error types."""
from mcp_server.infra.errors import (
    McpError,
    UnauthorizedError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)


def test_mcp_error_to_dict():
    err = McpError("test_code", "test message", 400)
    assert err.to_dict() == {"error": "test_code", "message": "test message"}


def test_unauthorized_error():
    err = UnauthorizedError()
    assert err.http_status == 401
    assert err.code == "unauthorized"


def test_unauthorized_error_custom_message():
    err = UnauthorizedError("custom unauthorized message")
    assert err.http_status == 401
    assert err.code == "unauthorized"
    assert err.message == "custom unauthorized message"


def test_forbidden_error():
    err = ForbiddenError()
    assert err.http_status == 403
    assert err.code == "forbidden"


def test_forbidden_error_custom_message():
    err = ForbiddenError("custom forbidden message")
    assert err.http_status == 403
    assert err.code == "forbidden"
    assert err.message == "custom forbidden message"


def test_not_found_error():
    err = NotFoundError()
    assert err.http_status == 404
    assert err.code == "not_found"


def test_not_found_error_custom_message():
    err = NotFoundError("item not found")
    assert err.http_status == 404
    assert err.message == "item not found"


def test_validation_error():
    err = ValidationError("invalid input")
    assert err.http_status == 400
    assert err.code == "validation_error"
    assert err.message == "invalid input"


def test_error_is_exception():
    err = McpError("test_code", "test message")
    assert isinstance(err, Exception)
