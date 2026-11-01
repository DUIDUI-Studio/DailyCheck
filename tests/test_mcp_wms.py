"""WMS bridge MCP tools — 契约测试（不连真实库）。

覆盖三件事：
1. 三个工具都走 token 的 path 白名单，没授权就 Forbidden；
2. 货主边界：SQL 参数恒等于服务端配置的 customer_id，调用方无法改；
3. 配置缺失 / 入参非法时给出稳定错误码，而不是裸异常。
"""
from __future__ import annotations

import pytest

from mcp_server.infra.errors import ForbiddenError, McpError
from mcp_server.protocol.tools import wms as tools_wms
from mcp_server.service import wms as svc_wms
from mcp_server.service.auth import AuthContext

CTX = AuthContext(1, ["/api/v1/*"], [], None)
NO_PATH_CTX = AuthContext(2, ["/api/v1/items"], [], None)


@pytest.fixture()
def wms_env(monkeypatch):
    monkeypatch.setenv("DAILYCHECK_WMS_HOST", "wms.example.invalid")
    monkeypatch.setenv("DAILYCHECK_WMS_USER", "u")
    monkeypatch.setenv("DAILYCHECK_WMS_PASSWORD", "s3cr3t-WMS-pass")
    monkeypatch.setenv("DAILYCHECK_WMS_CUSTOMER_ID", "155")


def _capture(monkeypatch):
    calls: list[tuple] = []

    def fake_run(sql, params, cfg=None):
        calls.append((sql, params))
        return [{"ok": True}]

    monkeypatch.setattr(svc_wms, "_run", fake_run)
    return calls


# --- 权限 -----------------------------------------------------------------

def test_stock_list_requires_path(wms_env, monkeypatch):
    _capture(monkeypatch)
    with pytest.raises(ForbiddenError):
        tools_wms.wms_stock_list_impl({}, ctx=NO_PATH_CTX)


def test_stock_summary_requires_path(wms_env, monkeypatch):
    _capture(monkeypatch)
    with pytest.raises(ForbiddenError):
        tools_wms.wms_stock_summary_impl({}, ctx=NO_PATH_CTX)


def test_documents_requires_path(wms_env, monkeypatch):
    _capture(monkeypatch)
    with pytest.raises(ForbiddenError):
        tools_wms.wms_documents_list_impl({"direction": "in"}, ctx=NO_PATH_CTX)


def test_authorized_ctx_reaches_service(wms_env, monkeypatch):
    calls = _capture(monkeypatch)
    assert tools_wms.wms_stock_summary_impl({}, ctx=CTX) == [{"ok": True}]
    assert calls, "authorized call must hit the service layer"


# --- 货主边界 --------------------------------------------------------------

def test_customer_id_is_server_side_only(wms_env, monkeypatch):
    """调用方传 customer_id 之类的参数不应影响 SQL 的货主过滤。"""
    calls = _capture(monkeypatch)
    tools_wms.wms_stock_list_impl({"customer_id": 999, "limit": 5}, ctx=CTX)
    _sql, params = calls[0]
    assert params == (155,), "first bind param must be the configured tenant"


def test_customer_id_env_override(wms_env, monkeypatch):
    monkeypatch.setenv("DAILYCHECK_WMS_CUSTOMER_ID", "42")
    calls = _capture(monkeypatch)
    tools_wms.wms_stock_summary_impl({}, ctx=CTX)
    assert calls[0][1] == (42,)


# --- 配置 ------------------------------------------------------------------

def test_unconfigured_raises(monkeypatch):
    for key in ("DAILYCHECK_WMS_HOST", "DAILYCHECK_WMS_USER", "DAILYCHECK_WMS_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(McpError) as exc:
        svc_wms.load_config()
    assert exc.value.code == "wms_not_configured"


def test_config_repr_hides_password(wms_env):
    cfg = svc_wms.load_config()
    text = repr(cfg)
    assert "password" not in text
    assert cfg.password not in text
    assert cfg.port == 24331
    assert cfg.database == "hywms_rentian"


# --- 入参校验 --------------------------------------------------------------

@pytest.mark.parametrize("bad", [0, -1, "abc"])
def test_limit_validation(wms_env, bad):
    with pytest.raises(McpError) as exc:
        svc_wms.list_stock(limit=bad)
    assert exc.value.code == "validation_error"


def test_limit_is_clamped(wms_env, monkeypatch):
    calls = _capture(monkeypatch)
    svc_wms.list_stock(limit=10_000)
    assert f"TOP {svc_wms.MAX_LIMIT}" in calls[0][0]


def test_documents_direction_validation(wms_env):
    with pytest.raises(McpError) as exc:
        svc_wms.list_documents(direction="sideways")
    assert exc.value.code == "validation_error"


def test_documents_direction_out_uses_outbound_table(wms_env, monkeypatch):
    calls = _capture(monkeypatch)
    svc_wms.list_documents(direction="out")
    assert "biz_storeOut" in calls[0][0]


def test_documents_date_from_binds_since(wms_env, monkeypatch):
    calls = _capture(monkeypatch)
    svc_wms.list_documents(direction="in", date_from="2026-10-01")
    assert calls[0][1] == (155, "2026-10-01", "2026-10-01")


# --- 错误包装 --------------------------------------------------------------

def test_query_failure_maps_to_mcp_error(wms_env, monkeypatch):
    class Boom(Exception):
        pass

    class FakeCursor:
        def execute(self, *_a, **_k):
            raise Boom("nope")

        def fetchall(self):
            return []

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def close(self):
            pass

    monkeypatch.setattr(svc_wms, "_connect", lambda cfg: FakeConn())
    with pytest.raises(McpError) as exc:
        svc_wms.summarize_stock()
    assert exc.value.code == "wms_query_failed"
