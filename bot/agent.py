"""LLM 编排：自然语言 → 工具调用 → 说人话（流式版）。

**为什么要流式**：非流式下模型必须把整段答案生成完才返回，300 字的回答
实测要 20-60 秒，员工等不了。改成流式后，首个 token 通常 1-3 秒就到，之后
边生成边推给企微，感知延迟从「等一大段」变成「立刻看到字在动」。

四个刻意的取舍：

1. **aiohttp 而不是 urllib**：需要逐行读 SSE，urllib 的同步读取塞进线程池
   后回调跨线程很别扭。aiohttp 的 ``async for line in resp.content`` 天然
   适配 asyncio。
2. **``trust_env=False``**：本机代理会破坏企微长连接（已踩过 853000），
   LLM 这条同样禁掉，不赌。
3. **工具结果截断**：一次 search 可能吐回几十条跨仓记录，不截断既撑上下文
   又拖慢生成。
4. **工具并行执行**：模型一条消息里可能要查好几个品项，串行会让每轮多花
   好几秒。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable, Collection
from pathlib import Path
from typing import Any

import aiohttp

from bot import tools
from bot.agent_config import (
    DEFAULT_MODEL,
    DEFAULT_REASONING_EFFORT,
    BotAgentConfig,
    load_effective_config,
    normalize_reasoning_effort,
    resolve_model_name,
)
from bot.prompt import SYSTEM_PROMPT
from bot.tool_schema import TOOL_NAMES, TOOL_SCHEMAS
from bot.usage import record_tool_call
from bot.verify_pure import build_correction_prompt, collect_numbers, find_untraceable

logger = logging.getLogger("bot.agent")

# serve.py 用 agent.DEFAULT_MODEL 打启动日志；保留该公开名字。
__all__ = ["DEFAULT_MODEL", "LLMError", "answer", "answer_stream", "split_for_stream"]

DEFAULT_BASE_URL = "https://api.minimaxi.com"

# 3 轮足够覆盖「先搜候选 → 再查详情」的绝大多数问法。
# 原来给 5 轮，实测有个含糊问题连跑 4 轮工具、60 秒才超时，员工体验极差。
MAX_TOOL_ROUNDS = 3
MAX_TOOL_RESULT_CHARS = 4000
MAX_OUTPUT_TOKENS = 800
LLM_TIMEOUT_SECONDS = 45
READ_TIMEOUT_SECONDS = 20
# 数字校正是一次「只改数字」的小调用。实测 5 秒左右，卡 10 秒兜底：
# 超时就保留原答，不能让一次校对把员工晾在那儿。
CORRECTION_TIMEOUT_SECONDS = 10.0
CORRECTION_MAX_DIGEST_CHARS = 3000
CORRECTION_MAX_TOKENS = 900
CORRECTION_SYSTEM = "你是库存数据的校对员，只负责订正数字，不做其他改写。"

_DISPATCH = {
    "search_items": tools.search_items,
    "get_item_detail": tools.get_item_detail,
    "get_price": tools.get_price,
    "list_low_stock": tools.list_low_stock,
    "list_warehouses": tools.list_warehouses,
    "list_warehouse_items": tools.list_warehouse_items,
    "list_store_overview": tools.list_store_overview,
    "get_item_consumption": tools.get_item_consumption,
    "list_orders": tools.list_orders,
    "get_project_help": tools.get_project_help,
}

HISTORY_TURNS = 6

DeltaCallback = Callable[[str], Awaitable[None]]


class LLMError(RuntimeError):
    """LLM 侧不可用（网络、额度、鉴权）。上层据此给用户一句人话。"""


def _load_api_key(env: dict[str, str] | None = None) -> str:
    """取 API key：环境变量优先，退回到 WorkBuddy 的 MCP 配置。"""
    env = env if env is not None else os.environ
    for name in ("LLM_API_KEY", "MINIMAX_API_KEY"):
        value = (env.get(name) or "").strip()
        if value:
            return value

    mcp_config = Path.home() / ".workbuddy" / "mcp.json"
    try:
        data = json.loads(mcp_config.read_text(encoding="utf-8"))
        return data["mcpServers"]["minimax"]["env"]["MINIMAX_API_KEY"]
    except (OSError, KeyError, json.JSONDecodeError):
        return ""


def _config() -> tuple[str, str, str]:
    base_url = (os.environ.get("LLM_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    model = resolve_model_name()
    return base_url, model, _load_api_key()


def _tool_schemas_for(enabled_tools: Collection[str]) -> list[dict[str, Any]]:
    """只暴露本条消息有效配置允许的工具（顺序与注册表一致）。"""
    allowed = frozenset(enabled_tools) & TOOL_NAMES
    return [schema for schema in TOOL_SCHEMAS if schema["function"]["name"] in allowed]


def _request_payload(
    messages: list[dict[str, Any]], model: str, config: BotAgentConfig
) -> dict[str, Any]:
    """构造主答请求体。

    ``reasoning_effort`` 必须位于**顶层**（MiniMax/OpenAI 兼容协议），
    不能塞进 messages/tools；测试用捕获器钉住这个位置。
    """
    return {
        "model": model,
        "messages": messages,
        "tools": _tool_schemas_for(config.enabled_tools),
        "tool_choice": "auto",
        "reasoning_effort": config.reasoning_effort,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "stream": True,
    }


def _default_config() -> BotAgentConfig:
    """仅给直接调用 ``_stream_once`` 的调试路径兜底；消息链必须传入快照。"""
    return BotAgentConfig(
        reasoning_effort=DEFAULT_REASONING_EFFORT,
        enabled_tools=frozenset(TOOL_NAMES),
        write_allowed_userids=(),
    )


_PROMPT_CACHE: str | None = None


def build_system_prompt() -> str:
    """把仓库清单直接拼进提示词。

    实测教训：不写清单时，模型会先调一次 ``list_warehouses`` 去查编码，
    白白多一轮往返 —— 每次 2-3 秒，直接吃掉一半响应速度。仓库是低频变更
    的配置，拼进提示词比让模型现查划算得多。

    缓存到进程生命周期；新增仓库后重启进程即可刷新。
    """
    global _PROMPT_CACHE
    if _PROMPT_CACHE is not None:
        return _PROMPT_CACHE

    brief = tools.warehouse_brief()
    if not brief:
        return SYSTEM_PROMPT

    lines = "\n".join(f"- {w['name']}：{w['code']}" for w in brief)
    _PROMPT_CACHE = (
        f"{SYSTEM_PROMPT}\n\n"
        "## 仓库清单（已确认，直接用编码，不要为了查编码再调用 list_warehouses）\n"
        f"{lines}\n"
    )
    return _PROMPT_CACHE


# 结果过长时优先砍这些列表字段——它们是「条数多」而不是「字段长」，
# 砍掉尾部既保住结构又不会让模型误读数字。
_TRUNCATABLE_KEYS = ("alerts", "details", "items", "top_items", "categories")


def _shrink_largest_list(payload: dict[str, Any], budget: int) -> dict[str, Any]:
    """按列表字段对半砍，直到能塞进预算或只剩一条。"""
    for key in _TRUNCATABLE_KEYS:
        rows = payload.get(key)
        if not isinstance(rows, list) or len(rows) <= 1:
            continue
        trimmed = dict(payload)
        kept = rows
        while len(kept) > 1 and len(json.dumps(trimmed, ensure_ascii=False)) > budget:
            kept = kept[: max(1, len(kept) // 2)]
            trimmed[key] = kept
            trimmed["truncated"] = f"{key} 只保留了前 {len(kept)} 条，共 {len(rows)} 条"
        return trimmed
    return payload


def _to_tool_text(payload: dict[str, Any]) -> str:
    """把工具结果序列化成给模型看的文本。

    **不能硬截断**：原来直接切字符串会切出半截 JSON，模型看到残缺对象只能瞎猜，
    数字对账也会因为解析失败而丢掉整份可信数据。这里改成砍列表尾部，保持 JSON
    合法；实在砍不下去（超长字符串）就明确回一个「结果过大，请缩小范围」。
    """
    text = json.dumps(payload, ensure_ascii=False)
    if len(text) <= MAX_TOOL_RESULT_CHARS:
        return text

    shrunk = _shrink_largest_list(payload, MAX_TOOL_RESULT_CHARS)
    text = json.dumps(shrunk, ensure_ascii=False)
    if len(text) <= MAX_TOOL_RESULT_CHARS:
        return text

    return json.dumps(
        {
            "ok": False,
            "error": "result_too_large",
            "message": "查询结果太大，请缩小范围：指定仓库，或给更精确的品名/SKU",
        },
        ensure_ascii=False,
    )


def _run_tool(
    name: str,
    raw_args: str,
    allowed_tools: Collection[str] | None = None,
) -> tuple[str, dict[str, Any] | None]:
    """执行单个工具。

    ``allowed_tools`` 是**同一条消息**的有效执行集合（消息开始时读取一次）。
    被关闭的工具、旧历史/伪造调用一律返回 ``unknown_tool``，且不进入 dispatch。
    默认值仅用于调试/旧单测；消息链必须显式传快照。

    返回 ``(给模型看的文本, 完整结果对象)`` 两样东西：文本可能被裁剪过，而
    对象始终完整——数字对账必须看完整数据，不能看裁过的版本。
    """
    allowed = (TOOL_NAMES if allowed_tools is None else frozenset(allowed_tools)) & TOOL_NAMES
    if name not in allowed:
        return json.dumps({"ok": False, "error": "unknown_tool", "name": name}), None
    try:
        args = json.loads(raw_args) if raw_args.strip() else {}
    except json.JSONDecodeError:
        return json.dumps({"ok": False, "error": "invalid_arguments_json"}), None
    if not isinstance(args, dict):
        return json.dumps({"ok": False, "error": "arguments_must_be_object"}), None

    try:
        result = _DISPATCH[name](**args)
    except TypeError as exc:
        record_tool_call(name, ok=False)
        return (
            json.dumps(
                {"ok": False, "error": "bad_arguments", "message": str(exc)},
                ensure_ascii=False,
            ),
            None,
        )
    except Exception as exc:  # noqa: BLE001 - 单个工具失败不该中断整轮对话
        record_tool_call(name, ok=False)
        logger.warning("工具 %s 执行失败: %s", name, exc)
        return (
            json.dumps(
                {"ok": False, "error": "tool_failed", "message": str(exc)},
                ensure_ascii=False,
            ),
            None,
        )
    record_tool_call(name, ok=True)
    return _to_tool_text(result), result


async def _stream_once(
    messages: list[dict[str, Any]],
    on_delta: DeltaCallback | None,
    config: BotAgentConfig | None = None,
) -> dict[str, Any]:
    """一次流式调用，返回拼装好的 assistant 消息。

    边收边回调 ``on_delta(累积文本)`` —— 企微的 stream 是覆盖语义，每次推
    当前累积全文即可，不用自己算增量。

    ``config`` 是消息级快照；``tools`` 与 ``reasoning_effort`` 都来自它。
    未传时仅给调试路径兜底（9 项 + medium）。
    """
    base_url, model, api_key = _config()
    if not api_key:
        raise LLMError("没有可用的 LLM API key")

    payload = _request_payload(messages, model, config or _default_config())
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    content = ""
    reasoning = ""
    tool_calls: dict[int, dict[str, Any]] = {}

    timeout = aiohttp.ClientTimeout(total=LLM_TIMEOUT_SECONDS, sock_read=READ_TIMEOUT_SECONDS)
    # trust_env=False：不读 HTTP_PROXY 等环境变量，和企微长连接一样强制直连
    async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
        try:
            async with session.post(
                f"{base_url}/v1/chat/completions", json=payload, headers=headers
            ) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:300]
                    raise LLMError(f"HTTP {resp.status}: {body}")

                async for raw_line in resp.content:
                    line = raw_line.decode("utf-8", "ignore").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}

                    piece = delta.get("content")
                    if piece:
                        content += piece
                        if on_delta is not None:
                            await on_delta(content)
                    if delta.get("reasoning_content"):
                        reasoning += delta["reasoning_content"]

                    for call in delta.get("tool_calls") or []:
                        index = int(call.get("index", 0))
                        slot = tool_calls.setdefault(
                            index,
                            {
                                "id": "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            },
                        )
                        if call.get("id"):
                            slot["id"] = call["id"]
                        function = call.get("function") or {}
                        if function.get("name"):
                            slot["function"]["name"] = function["name"]
                        if function.get("arguments"):
                            slot["function"]["arguments"] += function["arguments"]
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise LLMError("模型响应超时") from exc
        except aiohttp.ClientError as exc:
            raise LLMError(f"连接失败: {exc}") from exc

    message: dict[str, Any] = {"role": "assistant", "content": content}
    if reasoning:
        # M3 的思考链跨工具轮次要保持连续，必须原样回传
        message["reasoning_content"] = reasoning
    if tool_calls:
        message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
    return message


async def _simple_completion(
    system: str,
    user: str,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
) -> str:
    """一次不带工具、不流式的补全。只给数字校正用。

    这里刻意不开流式：校正结果要在推送前完整拿到、重新校验一遍，边流边推
    会把没验过的内容直接暴露给员工。

    辅助 LLM 调用也必须显式携带同一条消息的 ``reasoning_effort``（顶层），
    否则主答与校正会跑在不同思考强度上。
    """
    base_url, model, api_key = _config()
    if not api_key:
        raise LLMError("没有可用的 LLM API key")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "reasoning_effort": normalize_reasoning_effort(reasoning_effort),
        "max_tokens": CORRECTION_MAX_TOKENS,
        "temperature": 0.2,
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=CORRECTION_TIMEOUT_SECONDS)

    async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
        try:
            async with session.post(
                f"{base_url}/v1/chat/completions", json=payload, headers=headers
            ) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:300]
                    raise LLMError(f"HTTP {resp.status}: {body}")
                data = await resp.json(content_type=None)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise LLMError("数字校正超时") from exc
        except aiohttp.ClientError as exc:
            raise LLMError(f"数字校正连接失败: {exc}") from exc

    choices = data.get("choices") or []
    if not choices:
        raise LLMError("数字校正返回为空")
    return ((choices[0].get("message") or {}).get("content") or "").strip()


def verify_enabled() -> bool:
    """数字校验开关。出线上问题可以 ``BOT_VERIFY_NUMBERS=0`` 一键关掉。"""
    raw = (os.environ.get("BOT_VERIFY_NUMBERS") or "1").strip().lower()
    return raw not in {"0", "false", "off"}


async def _verified_answer(
    question: str,
    content: str,
    tool_payloads: list[dict[str, Any]],
    on_delta: DeltaCallback | None,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
) -> str:
    """对账回复里的数字，发现无出处的大数就重写一次。

    **只在命中时付代价**：正常回答不触发额外调用，所以不影响平时那 1-3 秒的首字
    时间。命中是低频事件（模型自己加减算错），多花一次往返换数字可信。
    """
    if not content or not tool_payloads or not verify_enabled():
        return content

    trusted: set[str] = set()
    for payload in tool_payloads:
        trusted |= collect_numbers(payload)
    if not trusted:
        return content

    suspicious = find_untraceable(content, trusted)
    if not suspicious:
        return content

    logger.warning(
        "回复含无出处数字 %s，触发校正｜原文：%s", suspicious, content[:200].replace("\n", " ")
    )
    digest = json.dumps(tool_payloads, ensure_ascii=False)[:CORRECTION_MAX_DIGEST_CHARS]
    prompt = build_correction_prompt(question, content, suspicious, digest)

    try:
        corrected = await _simple_completion(
            CORRECTION_SYSTEM, prompt, reasoning_effort=reasoning_effort
        )
    except LLMError as exc:
        logger.error("数字校正失败，保留原答：%s", exc)
        return content

    # 校正版明显短了，多半是模型跑偏成「抱歉我无法…」，宁可留原答
    if len(corrected) < len(content) * 0.5:
        logger.warning("校正版过短（%d 字），放弃", len(corrected))
        return content

    remaining = find_untraceable(corrected, trusted)
    if remaining:
        logger.warning("校正后仍有可疑数字 %s（已采用校正版）", remaining)
    else:
        logger.info("数字校正完成")

    # 覆盖已推送的中间态：企微的 stream 是累积覆盖语义
    if on_delta is not None:
        await on_delta(corrected)
    return corrected


async def answer_stream(
    question: str,
    history: list[dict[str, str]] | None = None,
    on_delta: DeltaCallback | None = None,
    on_progress: DeltaCallback | None = None,
) -> str:
    """流式回答。每收到一段文本就回调 on_delta，返回值是完整答案。

    ``on_progress`` 在工具跑完、等模型组织语言的那几秒里给一句状态。
    模型首 token 有 2 秒左右的固定开销，这段空白靠状态提示填。
    """
    messages: list[dict[str, Any]] = [{"role": "system", "content": build_system_prompt()}]
    if history:
        messages.extend(history[-HISTORY_TURNS:])
    messages.append({"role": "user", "content": question})

    # 每条消息只读一次有效配置；实际请求与执行过滤共用这份快照。
    config = load_effective_config()

    tool_payloads: list[dict[str, Any]] = []
    last_content = ""

    for round_index in range(MAX_TOOL_ROUNDS):
        message = await _stream_once(messages, on_delta, config)
        last_content = message.get("content") or ""

        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            content = (last_content or "").strip() or "没查到相关内容。"
            break

        messages.append(message)
        logger.info(
            "第 %d 轮工具调用：%s",
            round_index + 1,
            [(c.get("function") or {}).get("name") for c in tool_calls],
        )
        # 并行执行同一轮里的多个工具调用，别串着等
        results = await asyncio.gather(
            *[
                asyncio.to_thread(
                    _run_tool,
                    (call.get("function") or {}).get("name", ""),
                    (call.get("function") or {}).get("arguments", ""),
                    config.enabled_tools,
                )
                for call in tool_calls
            ]
        )
        for call, (text, payload) in zip(tool_calls, results, strict=True):
            messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": text})
            if payload is not None:
                tool_payloads.append(payload)

        if on_progress is not None:
            await on_progress("已查到数据，正在整理…")
    else:
        logger.warning("工具调用超过 %d 轮，强制收尾", MAX_TOOL_ROUNDS)
        content = (last_content or "").strip() or (
            "这个问题要查的东西有点多，能再具体一点吗？比如告诉我品项名或者仓库名。"
        )

    return await _verified_answer(
        question, content, tool_payloads, on_delta, config.reasoning_effort
    )


async def answer(question: str, history: list[dict[str, str]] | None = None) -> str:
    """不带回调的封装，给命令行调试用。"""
    return await answer_stream(question, history=history, on_delta=None)


def split_for_stream(text: str, chunk_size: int = 60) -> list[str]:
    """把文本切段（非流式兜底时用），优先在标点处断开。"""
    pieces: list[str] = []
    remaining = text.strip()
    while len(remaining) > chunk_size:
        window = remaining[:chunk_size]
        cut = max(window.rfind("。"), window.rfind("\n"), window.rfind("，"))
        cut = cut + 1 if cut > chunk_size // 3 else chunk_size
        pieces.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        pieces.append(remaining)
    return pieces
