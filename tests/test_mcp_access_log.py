"""Issue #85: MCP `call_tool` 接线 access_log 的验收测试。

测试通过 `build_server()` 真实注册的 `call_tool` 处理器执行：
  - 成功读 / 写各一条日志；
  - McpError 403 / 400、未知工具 400、非预期异常 500 都不记成功；
  - 每次进入路由恰一条记录，token_id 来自请求级 AuthContext；
  - 日志不含 token / header / arguments / reason；
  - 写日志失败不改变响应，也不重复执行 handler。

日志文件用临时路径（monkeypatch access_log._ACCESS_LOG_PATH），不碰生产
BASE_DIR/access.log。路径选择（配置数据根 vs 旧代码根）另用独立解释器
验证，不依赖 monkeypatch 绑定。
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import mcp_server.data.unit_of_work as uow
import mcp_server.infra.access_log as access_log_module
import mcp_server.protocol.server as protocol
from mcp_server.service.auth import AuthContext, set_current_auth

WAREHOUSE_CODE = "WH001"
TS = "2026-10-25 09:00:00"
TOKEN_ID = 7


class CapturingServer:
    """只替换 MCP SDK 装饰器注册，保留真实 build_server 里的 call_tool 逻辑。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.handler = None

    def list_tools(self):
        return lambda handler: handler

    def call_tool(self):
        def register(handler):
            self.handler = handler
            return handler

        return register


@pytest.fixture
def mcp_env(tmp_path, monkeypatch):
    """真实临时 master.db + warehouse.db + 隔离的 access.log 路径。"""
    import config as config_module
    import db as db_module

    master_path = tmp_path / "master.db"
    wh_dir = tmp_path / "warehouses"
    wh_dir.mkdir()
    log_path = tmp_path / "access.log"

    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_dir)
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", wh_dir)
    monkeypatch.setattr(uow, "MASTER_DB", str(master_path))
    monkeypatch.setattr(access_log_module, "_ACCESS_LOG_PATH", log_path)

    db_module.init_master_db()
    wh_path = wh_dir / f"{WAREHOUSE_CODE}.db"
    db_module.init_warehouse_db(wh_path)

    conn = sqlite3.connect(wh_path)
    conn.execute(
        "INSERT INTO items (id, sku, name, category_id, quantity, updated_at) "
        "VALUES (1, 'SKU-1', '测试品项', 1, 100, ?)",
        (TS,),
    )
    conn.commit()
    conn.close()

    master = sqlite3.connect(master_path)
    master.execute(
        "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) "
        "VALUES (1, ?, '测试仓', ?, 'storefront', ?)",
        (WAREHOUSE_CODE, str(wh_path), TS),
    )
    master.commit()
    master.close()

    yield {"master_path": master_path, "wh_path": wh_path, "log_path": log_path}


@pytest.fixture
def call_tool_route(monkeypatch):
    """捕获 build_server 注册的真实 call_tool 处理器。"""
    captured = CapturingServer("dailycheck-mcp")
    monkeypatch.setattr(protocol, "Server", lambda _name: captured)
    protocol.build_server()
    assert captured.handler is not None
    return captured.handler


@pytest.fixture
def auth_ctx():
    ctx = AuthContext(
        token_id=TOKEN_ID,
        allowed_read_paths=["/api/v1/restock"],
        allowed_write_paths=["/api/v1/restock"],
        allowed_warehouses=[WAREHOUSE_CODE],
    )
    set_current_auth(ctx)
    yield ctx
    set_current_auth(None)


def _call(route, name, arguments=None):
    return asyncio.run(route(name, arguments))


def _entries(log_path):
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]


class TestSuccessfulCalls:
    def test_read_call_logs_one_entry(self, mcp_env, call_tool_route, auth_ctx):
        result = _call(call_tool_route, "restock_list", {"warehouse_code": WAREHOUSE_CODE})

        payload = json.loads(result[0].text)
        assert payload["items"] == []
        entries = _entries(mcp_env["log_path"])
        assert len(entries) == 1
        record = entries[0]
        assert record["agent_token_id"] == TOKEN_ID
        assert record["tool_name"] == "restock_list"
        assert record["method"] == "tools/call"
        assert record["path"] == "/api/mcp/"
        assert record["status"] == 200
        assert isinstance(record["duration_ms"], int)
        assert record["duration_ms"] >= 0
        assert record["ts"]

    def test_write_call_logs_one_entry(self, mcp_env, call_tool_route, auth_ctx):
        result = _call(
            call_tool_route,
            "restock_create",
            {
                "item_id": 1,
                "quantity": 5,
                "warehouse_code": WAREHOUSE_CODE,
                "reason": "demo",
            },
        )

        payload = json.loads(result[0].text)
        assert payload["item_id"] == 1
        entries = _entries(mcp_env["log_path"])
        assert len(entries) == 1
        assert entries[0]["tool_name"] == "restock_create"
        assert entries[0]["status"] == 200

    def test_each_call_appends_exactly_one_entry(self, mcp_env, call_tool_route, auth_ctx):
        for _ in range(2):
            _call(call_tool_route, "restock_list", {"warehouse_code": WAREHOUSE_CODE})

        entries = _entries(mcp_env["log_path"])
        assert len(entries) == 2
        assert {e["status"] for e in entries} == {200}


class TestFailureStatuses:
    def test_forbidden_mcp_error_is_logged_as_403(self, mcp_env, call_tool_route):
        set_current_auth(
            AuthContext(
                token_id=TOKEN_ID,
                allowed_read_paths=["/api/v1/restock"],
                allowed_write_paths=[],
                allowed_warehouses=[WAREHOUSE_CODE],
            )
        )
        try:
            result = _call(
                call_tool_route,
                "restock_create",
                {"item_id": 1, "quantity": 1, "warehouse_code": WAREHOUSE_CODE},
            )
        finally:
            set_current_auth(None)

        assert json.loads(result[0].text) == {
            "error": "forbidden",
            "message": "forbidden_path",
        }
        entries = _entries(mcp_env["log_path"])
        assert len(entries) == 1
        assert entries[0]["status"] == 403
        assert entries[0]["status"] != 200

    def test_validation_mcp_error_is_logged_as_400(self, mcp_env, call_tool_route, auth_ctx):
        result = _call(
            call_tool_route,
            "restock_create",
            {"item_id": 1, "quantity": 0, "warehouse_code": WAREHOUSE_CODE},
        )

        assert json.loads(result[0].text)["error"] == "validation_error"
        entries = _entries(mcp_env["log_path"])
        assert len(entries) == 1
        assert entries[0]["status"] == 400

    def test_unknown_tool_logs_400_and_raises(self, mcp_env, call_tool_route, auth_ctx):
        with pytest.raises(ValueError, match="Unknown tool: no_such_tool"):
            _call(call_tool_route, "no_such_tool", {})

        entries = _entries(mcp_env["log_path"])
        assert len(entries) == 1
        assert entries[0]["status"] == 400
        assert entries[0]["tool_name"] == "no_such_tool"

    def test_unexpected_exception_logs_500_and_reraises(
        self, mcp_env, call_tool_route, auth_ctx, monkeypatch
    ):
        def boom(_args):
            raise RuntimeError("boom")

        monkeypatch.setitem(protocol._tool_map, "boom_tool", boom)

        with pytest.raises(RuntimeError, match="boom"):
            _call(call_tool_route, "boom_tool", {})

        entries = _entries(mcp_env["log_path"])
        assert len(entries) == 1
        assert entries[0]["status"] == 500
        assert entries[0]["status"] != 200
        assert entries[0]["tool_name"] == "boom_tool"

    def test_missing_request_context_logs_null_token(self, mcp_env, call_tool_route, monkeypatch):
        monkeypatch.delenv("DAILYCHECK_MCP_TOKEN", raising=False)
        set_current_auth(None)

        result = _call(call_tool_route, "restock_list", {"warehouse_code": WAREHOUSE_CODE})

        assert json.loads(result[0].text)["error"] == "unauthorized"
        entries = _entries(mcp_env["log_path"])
        assert len(entries) == 1
        assert entries[0]["agent_token_id"] is None
        assert entries[0]["status"] == 401


class TestLogHygiene:
    def test_legacy_writer_call_keeps_original_fields(self, mcp_env):
        """新增 tool_name 是可选字段；旧调用方的记录形状不变。"""
        access_log_module.write_mcp_access_log(3, "GET", "/api/v1/items", 200, 5)

        record = _entries(mcp_env["log_path"])[0]
        assert set(record) == {
            "ts",
            "agent_token_id",
            "path",
            "method",
            "status",
            "duration_ms",
        }
        assert record["agent_token_id"] == 3
        assert record["status"] == 200

    def test_log_omits_arguments_reason_and_token_material(
        self, mcp_env, call_tool_route, auth_ctx
    ):
        _call(
            call_tool_route,
            "restock_create",
            {
                "item_id": 1,
                "quantity": 5,
                "warehouse_code": WAREHOUSE_CODE,
                "reason": "TOPSECRET-REASON",
                "authorization": "Bearer TOPSECRET-TOKEN",
            },
        )

        raw = mcp_env["log_path"].read_text(encoding="utf-8")
        assert "TOPSECRET-REASON" not in raw
        assert "TOPSECRET-TOKEN" not in raw
        assert "authorization" not in raw.lower()
        assert "arguments" not in raw.lower()
        assert raw.count("\n") == 1

    def test_unknown_tool_name_is_bounded_and_single_line(
        self, mcp_env, call_tool_route, auth_ctx
    ):
        name = "line1\nINJECTED-" + "x" * 300

        with pytest.raises(ValueError):
            _call(call_tool_route, name, {})

        raw = mcp_env["log_path"].read_text(encoding="utf-8")
        assert raw.count("\n") == 1
        entries = _entries(mcp_env["log_path"])
        assert len(entries) == 1
        assert len(entries[0]["tool_name"]) == protocol._MAX_TOOL_NAME_LEN

    def test_log_write_failure_keeps_response_and_runs_handler_once(
        self, mcp_env, call_tool_route, auth_ctx, monkeypatch
    ):
        calls = {"count": 0}

        def counted(_args):
            calls["count"] += 1
            return {"ok": True}

        def failing_write(**_kwargs):
            raise OSError("disk full")

        monkeypatch.setitem(protocol._tool_map, "counted_tool", counted)
        monkeypatch.setattr(protocol, "write_mcp_access_log", failing_write)

        result = _call(call_tool_route, "counted_tool", {"secret": "ignored"})

        assert json.loads(result[0].text) == {"ok": True}
        assert calls["count"] == 1
        assert _entries(mcp_env["log_path"]) == []


REPO_ROOT = Path(__file__).resolve().parent.parent
_PROBE = """
import json
from pathlib import Path
import mcp_server.infra.access_log as al
selected = Path(al._ACCESS_LOG_PATH)
out = {"selected": str(selected), "is_file_before": selected.is_file()}
if __import__("os").environ.get("PROBE_WRITE") == "1":
    al.write_mcp_access_log(11, "tools/call", "/api/mcp/", 200, 3, tool_name="restock_list")
    out["is_file_after"] = selected.is_file()
    out["bytes"] = selected.read_text(encoding="utf-8") if selected.is_file() else None
print(json.dumps(out, sort_keys=True))
"""


def _probe(*, db_dir: str | None, write: bool) -> dict:
    """Run the real module in a fresh interpreter without patching its path."""
    env = os.environ.copy()
    env.pop("DAILYCHECK_DB_DIR", None)
    if db_dir is not None:
        env["DAILYCHECK_DB_DIR"] = db_dir
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PROBE_WRITE"] = "1" if write else "0"
    proc = subprocess.run(
        [sys.executable, "-B", "-c", _PROBE],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    return json.loads(proc.stdout)


class TestConfiguredAccessLogPath:
    """`DAILYCHECK_DB_DIR` 非空时审计写入配置数据根，否则保持旧代码根位置。"""

    def test_nonempty_db_dir_writes_exact_record_to_configured_root(self, tmp_path):
        data_root = tmp_path / "data"
        data_root.mkdir()
        root_log = REPO_ROOT / "access.log"
        before = root_log.read_bytes() if root_log.exists() else None

        result = _probe(db_dir=str(data_root), write=True)

        log_path = data_root / "access.log"
        assert result["selected"] == str(log_path)
        assert result["is_file_after"] is True
        entries = _entries(log_path)
        assert len(entries) == 1
        assert entries[0] == {
            "ts": entries[0]["ts"],
            "agent_token_id": 11,
            "path": "/api/mcp/",
            "method": "tools/call",
            "status": 200,
            "duration_ms": 3,
            "tool_name": "restock_list",
        }
        assert entries[0]["ts"]
        # 只读代码根不得新增或改动文件。
        if before is None:
            assert not root_log.exists()
        else:
            assert root_log.read_bytes() == before

    @pytest.mark.parametrize("env_value", [None, ""])
    def test_unset_or_empty_db_dir_keeps_legacy_location(self, env_value):
        root_log = REPO_ROOT / "access.log"
        before = root_log.read_bytes() if root_log.exists() else None

        result = _probe(db_dir=env_value, write=False)

        assert result["selected"] == str(root_log)
        if before is None:
            assert not root_log.exists()
        else:
            assert root_log.read_bytes() == before

    def test_configured_root_write_failure_is_best_effort(self, tmp_path):
        data_root = tmp_path / "data"
        data_root.mkdir()
        (data_root / "access.log").mkdir()  # append fails: IsADirectoryError
        root_log = REPO_ROOT / "access.log"
        before = root_log.read_bytes() if root_log.exists() else None

        result = _probe(db_dir=str(data_root), write=True)

        assert result["selected"] == str(data_root / "access.log")
        assert result["is_file_after"] is False
        assert "Traceback" not in json.dumps(result)
        # 失败不产生旧位置回退写。
        if before is None:
            assert not root_log.exists()
        else:
            assert root_log.read_bytes() == before
