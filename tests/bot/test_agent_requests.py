"""Issue #79 PR1：真实请求体与执行过滤。

这些用例走 ``bot.agent`` 的真实请求构造路径，只把 aiohttp 换成捕获器：
- reasoning_effort 必须在**请求体顶层**（不是 tools 里、不是 header）。
- tools 数组必须按「消息开始时读取一次」的有效配置过滤。
- 主答与数字校正这两个 LLM 调用都要显式带 reasoning_effort。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from bot import agent
from bot.agent import (
    _run_tool,
    _simple_completion,
    _stream_once,
    answer_stream,
)
from bot.agent_config import BotAgentConfig
from bot.tool_schema import TOOL_NAMES
from bot.usage import read_tool_call_counts


class _Lines:
    def __init__(self, lines):
        self._lines = lines

    def __aiter__(self):
        async def _gen():
            for line in self._lines:
                yield line

        return _gen()


class _FakeResponse:
    def __init__(self, *, lines=None, json_data=None):
        self.status = 200
        self._lines = lines or []
        self._json = json_data or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def text(self):
        return ""

    async def json(self, content_type=None):
        return self._json

    @property
    def content(self):
        return _Lines(self._lines)


class _FakeSession:
    last = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.posts = []
        _FakeSession.last = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def post(self, url, json=None, headers=None):
        self.posts.append({"url": url, "json": json, "headers": headers})
        return _FakeResponse(
            lines=[
                b'data: {"choices":[{"delta":{"content":"hi"}}]}\n',
                b"data: [DONE]\n",
            ],
            json_data={"choices": [{"message": {"content": "ok"}}]},
        )


def _capture(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setattr("bot.agent.aiohttp.ClientSession", _FakeSession)
    return _FakeSession


def _config(effort="high", enabled=None):
    return BotAgentConfig(
        reasoning_effort=effort,
        enabled_tools=frozenset(enabled if enabled is not None else TOOL_NAMES),
        write_allowed_userids=(),
    )


def test_stream_request_puts_reasoning_effort_at_top_level(monkeypatch):
    fake = _capture(monkeypatch)

    asyncio.run(_stream_once([{"role": "user", "content": "hi"}], None, _config()))

    payload = fake.last.posts[-1]["json"]
    assert payload["reasoning_effort"] == "high"
    assert payload["model"] == "test-model"
    assert "reasoning_effort" not in payload["tools"][0]


def test_stream_request_tools_follow_effective_config(monkeypatch):
    fake = _capture(monkeypatch)

    asyncio.run(
        _stream_once(
            [{"role": "user", "content": "hi"}],
            None,
            _config(enabled={"search_items"}),
        )
    )

    names = {tool["function"]["name"] for tool in fake.last.posts[-1]["json"]["tools"]}
    assert names == {"search_items"}


def test_auxiliary_completion_also_carries_reasoning_effort(monkeypatch):
    fake = _capture(monkeypatch)

    asyncio.run(_simple_completion("system", "user", "xhigh"))

    payload = fake.last.posts[-1]["json"]
    assert payload["reasoning_effort"] == "xhigh"


def test_answer_stream_reads_effective_config_once_per_message(monkeypatch):
    calls = {"n": 0}

    def _load():
        calls["n"] += 1
        return _config()

    async def _fake_stream(messages, on_delta, config):
        assert config.reasoning_effort == "high"
        return {"content": "答"}

    monkeypatch.setattr("bot.agent.load_effective_config", _load)
    monkeypatch.setattr("bot.agent._stream_once", _fake_stream)

    result = asyncio.run(answer_stream("问题"))

    assert result == "答"
    assert calls["n"] == 1


def test_run_tool_rejects_disabled_tool_without_dispatch(monkeypatch):
    called = []
    monkeypatch.setitem(
        agent._DISPATCH,
        "list_orders",
        lambda: called.append(True) or {"ok": True},
    )

    text, payload = _run_tool(
        "list_orders", "{}", allowed_tools=frozenset({"search_items"})
    )

    assert json.loads(text)["error"] == "unknown_tool"
    assert payload is None
    assert called == [], "被关闭的工具绝不能进入 dispatch"


def test_run_tool_records_real_call_for_seven_day_count(monkeypatch, tmp_path):
    log = tmp_path / "bot_tool_calls.log"
    monkeypatch.setenv("BOT_TOOL_CALL_LOG", str(log))
    monkeypatch.setitem(agent._DISPATCH, "list_orders", lambda: {"ok": True})

    text, payload = _run_tool(
        "list_orders", "{}", allowed_tools=frozenset({"list_orders"})
    )

    assert payload == {"ok": True}
    assert json.loads(text) == {"ok": True}
    counts = read_tool_call_counts(path=log, now=datetime.now(timezone.utc))
    assert counts["list_orders"] == 1


def test_pr2_actual_request_excludes_disabled_restock_and_execution_rejects(monkeypatch, tmp_path):
    import sqlite3

    from bot.agent_config import load_effective_config

    master = tmp_path / 'master.db'
    with sqlite3.connect(master) as conn:
        conn.execute('CREATE TABLE bot_agent_config (key TEXT, value TEXT)')
        conn.execute("INSERT INTO bot_agent_config VALUES ('tool.restock_create','0')")
    config = load_effective_config(master)
    fake = _capture(monkeypatch)
    asyncio.run(_stream_once([{'role': 'user', 'content': '入库3箱'}], None, config))
    request = fake.last.posts[-1]['json']
    names = {tool['function']['name'] for tool in request['tools']}
    assert 'restock_create' not in names
    assert {'outbound_create', 'outbound_rollback'} <= names
    text, payload = _run_tool('restock_create', '{"warehouse_code":"wh_a","item_id":1,"quantity":3}', config=config)
    assert payload is None and json.loads(text)['error'] == 'unknown_tool'


def test_all_eight_new_reads_are_explicit_opt_in_and_removed_on_next_message(monkeypatch, tmp_path):
    import sqlite3

    from bot.agent_config import load_effective_config
    from bot.tool_contract import EXTRA_READS

    master = tmp_path / 'master.db'
    with sqlite3.connect(master) as conn:
        conn.execute('CREATE TABLE bot_agent_config (key TEXT PRIMARY KEY, value TEXT)')
    fake = _capture(monkeypatch)
    for name in sorted(EXTRA_READS):
        with sqlite3.connect(master) as conn:
            conn.execute('INSERT OR REPLACE INTO bot_agent_config VALUES (?, ?)', ('tool.' + name, '1'))
        config = load_effective_config(master)
        asyncio.run(_stream_once([{'role': 'user', 'content': '查询'}], None, config))
        names = {t['function']['name'] for t in fake.last.posts[-1]['json']['tools']}
        assert name in names
        with sqlite3.connect(master) as conn:
            conn.execute('UPDATE bot_agent_config SET value=? WHERE key=?', ('0', 'tool.' + name))
        config = load_effective_config(master)
        asyncio.run(_stream_once([{'role': 'user', 'content': '查询'}], None, config))
        names = {t['function']['name'] for t in fake.last.posts[-1]['json']['tools']}
        assert name not in names
        assert _run_tool(name, '{}', config=config)[1] is None
