from mcp_server.service.auth import check_warehouse, check_path, AuthContext


def test_check_warehouse_none_allows_all():
    ctx = AuthContext(1, [], [], None)
    assert check_warehouse(ctx, "WH001") is True


def test_check_warehouse_whitelist():
    ctx = AuthContext(1, [], [], ["WH001", "WH002"])
    assert check_warehouse(ctx, "WH001") is True
    assert check_warehouse(ctx, "WH999") is False


def test_check_path_get():
    ctx = AuthContext(1, ["/api/v1/items"], [], None)
    assert check_path(ctx, "GET", "/api/v1/items") is True
    assert check_path(ctx, "GET", "/api/v1/items/1") is True
    assert check_path(ctx, "POST", "/api/v1/items") is False


def test_check_path_wildcard():
    ctx = AuthContext(1, ["/api/v1/items/*"], [], None)
    assert check_path(ctx, "GET", "/api/v1/items/123") is True
    assert check_path(ctx, "GET", "/api/v1/items/a/b") is True
    # Wildcard requires at least one char after prefix
    assert check_path(ctx, "GET", "/api/v1/items") is False
