"""bot 测试的默认隔离：工具调用计数落临时文件，不污染仓库根目录。"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_bot_tool_call_log(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_TOOL_CALL_LOG", str(tmp_path / "bot_tool_calls.log"))
    monkeypatch.setattr("blueprints.audit_pure.ADMIN_AUDIT_LOG", tmp_path / "admin_audit.log")
