"""编排层单测：只覆盖不依赖 LLM 的部分。

真实问答要打 MiniMax，不适合放进单测（慢、要额度、不稳定）。这里守的是
结果裁剪、工具错误兜底、数字校验的短路逻辑——这些出问题会直接让模型看错数据。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from bot import agent


class TestToolText:
    def test_短结果原样序列化(self):
        payload = {"ok": True, "total": 1}
        assert json.loads(agent._to_tool_text(payload)) == payload

    def test_超长时砍列表但保持合法_JSON(self):
        payload = {
            "ok": True,
            "count": 80,
            "alerts": [
                {"sku": f"PP{i:03d}", "name": "很长的品名" * 10, "note": "描述" * 20}
                for i in range(80)
            ],
        }
        text = agent._to_tool_text(payload)
        assert len(text) <= agent.MAX_TOOL_RESULT_CHARS
        parsed = json.loads(text)  # 关键：必须是合法 JSON，模型不能收到半截对象
        assert len(parsed["alerts"]) < 80
        assert parsed["truncated"].startswith("alerts 只保留了前")

    def test_砍不动时明确报错而不是截半截(self):
        payload = {"ok": True, "note": "x" * 5000}
        parsed = json.loads(agent._to_tool_text(payload))
        assert parsed["error"] == "result_too_large"
        assert "缩小范围" in parsed["message"]

    def test_小于两条的列表不动(self):
        payload = {"ok": True, "details": [{"sku": "A"}], "note": "y" * 5000}
        parsed = json.loads(agent._to_tool_text(payload))
        assert parsed["error"] == "result_too_large"


class TestShrink:
    def test_优先砍提醒列表(self):
        payload = {
            "alerts": [{"n": "a" * 200} for _ in range(20)],
            "categories": [{"n": "b"}],
        }
        shrunk = agent._shrink_largest_list(payload, 500)
        assert len(shrunk["alerts"]) < 20
        assert shrunk["categories"] == [{"n": "b"}]

    def test_至少保留一条(self):
        payload = {"alerts": [{"n": "a" * 500} for _ in range(4)]}
        shrunk = agent._shrink_largest_list(payload, 100)
        assert len(shrunk["alerts"]) == 1


class TestRunTool:
    def test_未知工具不抛异常(self):
        text, payload = agent._run_tool("rm_rf", "{}")
        assert json.loads(text)["error"] == "unknown_tool"
        assert payload is None

    def test_非法_JSON_参数(self):
        text, payload = agent._run_tool("search_items", "{not json")
        assert json.loads(text)["error"] == "invalid_arguments_json"
        assert payload is None

    def test_参数类型错误转结构化失败(self):
        text, _ = agent._run_tool("search_items", '{"不存在的参数": 1}')
        assert json.loads(text)["error"] == "bad_arguments"

    def test_空参数串按空对象处理(self):
        # search_items 缺 keyword 会 TypeError，这里只确认不崩
        text, _ = agent._run_tool("search_items", "")
        assert "error" in json.loads(text)


class TestVerifySwitch:
    def test_默认开启(self, monkeypatch):
        monkeypatch.delenv("BOT_VERIFY_NUMBERS", raising=False)
        assert agent.verify_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "off", "OFF"])
    def test_可关闭(self, monkeypatch, value):
        monkeypatch.setenv("BOT_VERIFY_NUMBERS", value)
        assert agent.verify_enabled() is False


class TestVerifiedAnswer:
    def test_没有工具结果时直接放行(self):
        # 不触发任何 LLM 调用
        result = asyncio.run(agent._verified_answer("q", "随便说点啥 999", [], None))
        assert result == "随便说点啥 999"

    def test_数字全部可追溯时原样返回(self):
        payload = {"total_quantity": 5.67, "count": 2}
        result = asyncio.run(
            agent._verified_answer("q", "一共 5.67 箱，分布在 2 个仓", [payload], None)
        )
        assert result == "一共 5.67 箱，分布在 2 个仓"

    def test_空回答直接放行(self):
        assert asyncio.run(agent._verified_answer("q", "", [{"a": 1}], None)) == ""
