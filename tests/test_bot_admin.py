"""Issue #79 PR1：/admin/bot-agent 配置台。

覆盖：
- 平台管理员鉴权 + 默认 CSRF（缺 token 400）。
- 34 主行 = 实时 get_tools() 29 + bot 自有 5；注入注册项自动出现。
- 4 个旧名兼容子条目；get_price 只作诊断。
- 模型名只读、5 档思考强度单选、写项标红、未接入状态。
- 保存持久化 + updated_by + 共享审计。
- 最近 7 天调用数来自最小 JSONL 计数源。
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from mcp.types import Tool

from bot.tool_schema import TOOL_NAMES


def _master(wh_path):
    conn = sqlite3.connect(wh_path.parent.parent / "master.db")
    conn.row_factory = sqlite3.Row
    return conn


def _db_config(wh_path, key):
    with _master(wh_path) as conn:
        row = conn.execute(
            "SELECT value, updated_at, updated_by FROM bot_agent_config WHERE key=?",
            (key,),
        ).fetchone()
    return row


def _main_rows(html):
    return re.findall(r'data-tool-row="([^"]+)"', html)


def _legacy_rows(html):
    return re.findall(r'data-legacy-row="([^"]+)"', html)


def _row_block(html, name):
    start = html.index(f'data-tool-row="{name}"')
    end = html.index("</tr>", start)
    return html[start:end]


def _csrf(html):
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    assert m, "页面应渲染 csrf_token"
    return m.group(1)


def test_non_platform_admin_is_rejected(staff_client):
    client, _ = staff_client

    assert client.get("/admin/bot-agent").status_code == 403


def test_page_renders_live_registry_plus_own_tools(logged_client):
    client, wh_path = logged_client
    from mcp_server.protocol.tools import get_tools

    resp = client.get("/admin/bot-agent")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    main = _main_rows(html)
    assert len(main) == 34, f"应 34 主行（29 MCP + 5 自有），实际 {len(main)}"
    assert set(main) == {t.name for t in get_tools()} | {
        "search_items",
        "list_low_stock",
        "list_store_overview",
        "list_orders",
        "get_project_help",
    }
    assert set(_legacy_rows(html)) == {
        "list_warehouses",
        "list_warehouse_items",
        "get_item_detail",
        "get_item_consumption",
    }
    assert "get_price" in html, "D6：get_price 应单列诊断"
    assert "待确认" not in html, "当前 29 项都应有明确登记"


def test_injected_registry_tool_appears_without_static_list_change(
    logged_client, monkeypatch
):
    client, _ = logged_client
    import blueprints.bot_admin as bot_admin

    original = bot_admin.get_tools

    def _with_probe():
        return original() + [
            Tool(
                name="probe_new_tool",
                title="Probe",
                description="Injected probe.",
                inputSchema={"type": "object", "properties": {}},
            )
        ]

    monkeypatch.setattr(bot_admin, "get_tools", _with_probe)

    html = client.get("/admin/bot-agent").get_data(as_text=True)

    assert len(_main_rows(html)) == 35
    assert 'data-tool-row="probe_new_tool"' in html
    assert 'data-status-code="pending"' in _row_block(html, "probe_new_tool")


def test_model_name_is_read_only_and_effort_has_five_choices(
    logged_client, monkeypatch
):
    client, _ = logged_client
    monkeypatch.setenv("LLM_MODEL", "Test-Model-X")

    html = client.get("/admin/bot-agent").get_data(as_text=True)

    assert "Test-Model-X" in html
    assert 'name="llm.model"' not in html
    radios = re.findall(r'name="reasoning_effort"[^>]*value="([^"]+)"', html)
    assert radios == ["low", "medium", "high", "xhigh", "max"]
    assert 'value="none"' not in html


def test_write_rows_are_marked_red_and_not_connected(logged_client):
    client, _ = logged_client

    html = client.get("/admin/bot-agent").get_data(as_text=True)
    block = _row_block(html, "restock_create")

    assert 'data-write="1"' in block
    assert 'data-status-code="not_connected"' in block
    assert "写" in block
    assert "风险" in html or "写操作" in html


def test_seven_day_counts_come_from_jsonl_source(logged_client, monkeypatch, tmp_path):
    client, _wh_path = logged_client
    now = datetime.now(timezone.utc)
    log = tmp_path / "bot_tool_calls.log"
    monkeypatch.setenv("BOT_TOOL_CALL_LOG", str(log))
    records = [
        {"ts": (now - timedelta(days=1)).isoformat(), "tool": "list_orders"},
        {"ts": (now - timedelta(days=2)).isoformat(), "tool": "list_orders"},
        {"ts": (now - timedelta(days=3)).isoformat(), "tool": "list_orders"},
        {"ts": (now - timedelta(days=8)).isoformat(), "tool": "list_orders"},
        {"ts": (now - timedelta(days=1)).isoformat(), "tool": "search_items"},
    ]
    log.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )

    html = client.get("/admin/bot-agent").get_data(as_text=True)

    assert 'data-call-count="3"' in _row_block(html, "list_orders")
    assert 'data-call-count="1"' in _row_block(html, "search_items")
    assert 'data-call-count="0"' in _row_block(html, "warehouse_list")


def test_save_persists_effective_values_and_shared_audit(
    logged_client, monkeypatch
):
    client, wh_path = logged_client
    import blueprints.audit_pure as audit_pure

    audit_log = wh_path.parent.parent / "admin_audit.log"
    monkeypatch.setattr(audit_pure, "ADMIN_AUDIT_LOG", audit_log)
    client.application.config["WTF_CSRF_FORCE_ENABLED"] = True

    html = client.get("/admin/bot-agent").get_data(as_text=True)
    token = _csrf(html)
    data = {
        "csrf_token": token,
        "reasoning_effort": "high",
        "write_allowed_userids": "U1, U2",
        **{f"tool.{name}": "1" for name in TOOL_NAMES if name != "list_orders"},
    }

    resp = client.post("/admin/bot-agent/save", data=data)

    assert resp.status_code == 302
    effort = _db_config(wh_path, "llm.reasoning_effort")
    assert effort["value"] == "high"
    assert effort["updated_by"] == "admin"
    assert effort["updated_at"]
    assert _db_config(wh_path, "tool.list_orders")["value"] == "0"
    assert _db_config(wh_path, "tool.search_items")["value"] == "1"
    assert _db_config(wh_path, "write.allowed_userids")["value"] == "U1,U2"

    audit = audit_log.read_text(encoding="utf-8")
    assert "bot-agent" in audit
    assert "admin" in audit
    assert "list_orders" in audit


def test_invalid_reasoning_effort_is_rejected_and_not_persisted(logged_client):
    client, wh_path = logged_client
    client.application.config["WTF_CSRF_FORCE_ENABLED"] = True
    token = _csrf(client.get("/admin/bot-agent").get_data(as_text=True))

    resp = client.post(
        "/admin/bot-agent/save",
        data={
            "csrf_token": token,
            "reasoning_effort": "none",
            **{f"tool.{name}": "1" for name in TOOL_NAMES},
        },
    )

    assert resp.status_code == 302
    assert _db_config(wh_path, "llm.reasoning_effort") is None


def test_post_without_csrf_is_rejected(logged_client):
    client, _ = logged_client
    client.application.config["WTF_CSRF_FORCE_ENABLED"] = True

    resp = client.post(
        "/admin/bot-agent/save",
        data={
            "reasoning_effort": "high",
            **{f"tool.{name}": "1" for name in TOOL_NAMES},
        },
    )

    assert resp.status_code == 400
