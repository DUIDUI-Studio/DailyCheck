"""企微长连接接入层：收消息 → LLM 问答 → 流式回复。

和 ``probe.py`` 的区别：probe 是接入探针（回显原始字段，用于验证链路），
serve 是正式形态。两者共用同一套连接管理，探针留着排障用。

**流畅性的实现**：长连接的 ``stream`` 类型是「累积覆盖」语义——后一次推送
会替换前一次展示的内容。所以先推一句「正在查…」把白屏时间压到 0.3 秒内，
LLM 回来后再逐段追加，最后 ``finish=True`` 收尾。员工看到的是打字机效果，
不是等 4 秒后突然蹦出一大段。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from typing import Any

from bot import agent
from bot.config_pure import BotConfig, BotConfigError, load_bot_config, mask_secret
from bot.probe import AUTH_ERROR_CODES, _SdkLogger, force_direct_connection
from bot.probe_pure import strip_mention

try:
    from aibot import WSClient, WSClientOptions  # type: ignore[import-not-found]
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "缺少企微官方 SDK。先装：.venv/bin/pip install wecom-aibot-python-sdk"
    ) from exc

logger = logging.getLogger("bot.serve")

REPLY_TIMEOUT_SECONDS = 30.0
THINKING_TEXT = "正在查…"
FALLBACK_TEXT = "我这边出了点问题，稍后再问一次试试。"
TIMEOUT_TEXT = "这个问题查得有点久，换个具体点的说法试试？比如直接说品项名。"
# 流式推送节流：模型每秒可能吐几十个 delta，逐条推会把长连接打满。
STREAM_THROTTLE_SECONDS = 0.6
# 首 token 等多久是运气：实测同一句话 1.0s 到 16s 都出现过（模型侧波动，
# 不是我们代码慢）。这期间界面一直显示同一句「正在查…」，员工会以为卡死。
# 每 5 秒换一句，三次封顶——再多就成了噪音。
PROGRESS_TICK_SECONDS = 5.0
PROGRESS_TEXTS = (
    "正在查数据…",
    "数据有点多，还在查…",
    "快好了，稍等…",
)
EMPTY_TEXT = (
    "你只 @ 了我一下，没带问题。\n"
    "可以这样问：「冷冻芒果酱还有多少」「哪些快断货了」「新天地店有什么」"
)

# 每个员工只保留最近几轮。内存态，进程重启即清空——这是有意的，
# 库存问答不需要跨会话记忆，存久了反而会答非所问。
HISTORY: dict[str, list[dict[str, str]]] = {}
HISTORY_TURNS = 6


def _remember(userid: str, question: str, reply: str) -> None:
    turns = HISTORY.setdefault(userid, [])
    turns.append({"role": "user", "content": question})
    turns.append({"role": "assistant", "content": reply})
    excess = len(turns) - HISTORY_TURNS * 2
    if excess > 0:
        del turns[:excess]


async def _push(client: WSClient, frame: dict, message_id: str, text: str, finish: bool) -> None:
    """推一次流式内容。失败只记日志，不中断后续推送。"""
    try:
        ack = await asyncio.wait_for(
            client.reply_stream(frame, stream_id=message_id, content=text, finish=finish),
            timeout=REPLY_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - 对外通道，不该掀掉连接
        logger.error("推送失败 finish=%s err=%s", finish, exc)
        return

    if isinstance(ack, dict) and ack.get("errcode"):
        logger.error("推送被拒 errcode=%s errmsg=%s", ack.get("errcode"), ack.get("errmsg"))


async def _progress_ticker(push: Any, stop: asyncio.Event) -> None:
    """首 token 之前的滚动状态。一旦有正文或阶段提示就永久停下。

    流式推送是「累积覆盖」语义，所以这里每次推的都是一句独立的短文案，
    不会污染后面正式回答的内容。
    """
    for text in PROGRESS_TEXTS:
        try:
            await asyncio.wait_for(stop.wait(), timeout=PROGRESS_TICK_SECONDS)
            return  # 已经开吐正文（或流程结束），不必再刷状态
        except asyncio.TimeoutError:
            pass
        await push(text)


async def _handle_text(client: WSClient, frame: dict) -> None:
    body = frame.get("body") or {}
    msgid = str(body.get("msgid") or "stream")
    userid = str((body.get("from") or {}).get("userid") or "")
    raw = str((body.get("text") or {}).get("content") or "")
    question = strip_mention(raw)

    logger.info("[%s] 提问：%s", userid or "?", question or "(空)")

    if not question:
        await _push(client, frame, msgid, EMPTY_TEXT, finish=True)
        return

    started = time.monotonic()
    # 推送串行化：滚动状态和正文推送是两个协程，并发调 reply_stream 会让
    # 「正在查数据…」盖掉已经流出来的正文（累积覆盖语义）。
    push_lock = asyncio.Lock()

    async def push(text: str, finish: bool = False) -> None:
        async with push_lock:
            await _push(client, frame, msgid, text, finish)

    # 先占位，把感知延迟压到最低
    await push(THINKING_TEXT)

    stop_ticker = asyncio.Event()
    ticker = asyncio.create_task(_progress_ticker(push, stop_ticker))

    last_push = 0.0

    async def on_delta(text: str) -> None:
        """模型每吐一段就推一次，但要节流 —— 每秒几十次 HTTP 会把连接打满。"""
        nonlocal last_push
        stop_ticker.set()
        now = time.monotonic()
        if now - last_push < STREAM_THROTTLE_SECONDS:
            return
        last_push = now
        await push(text)

    async def on_progress(text: str) -> None:
        """工具跑完、等模型组织语言时推一句状态，别让界面干等着。"""
        stop_ticker.set()
        await push(text)

    try:
        reply = await agent.answer_stream(
            question,
            history=HISTORY.get(userid),
            on_delta=on_delta,
            on_progress=on_progress,
        )
    except agent.LLMError as exc:
        logger.error("LLM 失败（%.1fs）：%s", time.monotonic() - started, exc)
        await push(TIMEOUT_TEXT if "超时" in str(exc) else FALLBACK_TEXT, finish=True)
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("回答问题失败：%s", exc)
        await push(FALLBACK_TEXT, finish=True)
        return
    finally:
        stop_ticker.set()
        ticker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ticker

    reply = (reply or "").strip() or "没查到相关内容。"
    await push(reply, finish=True)

    _remember(userid, question, reply)
    logger.info(
        "[%s] 已回复 %d 字，耗时 %.1fs", userid or "?", len(reply), time.monotonic() - started
    )


async def amain(config: BotConfig) -> int:
    force_direct_connection()
    options = WSClientOptions(
        bot_id=config.bot_id,
        secret=config.secret,
        heartbeat_interval=30000,
        max_reconnect_attempts=-1,
        request_timeout=10000,
        logger=_SdkLogger(),
    )
    client = WSClient(options)

    stop = asyncio.Event()
    auth_rejected = False

    def _on_error(error: object) -> None:
        nonlocal auth_rejected
        message = str(error)
        logger.error("连接错误：%s", message)
        if any(str(code) in message for code in AUTH_ERROR_CODES) or "invalid bot_id" in message:
            auth_rejected = True
            logger.error("凭证被拒。核对 WECOM_BOT_ID / WECOM_BOT_SECRET 后重跑。")
            stop.set()

    def _spawn(coro: Any) -> None:
        task = asyncio.create_task(coro)

        def _log_error(t: asyncio.Task) -> None:
            if t.cancelled():
                return
            error = t.exception()
            if error is not None:
                logger.error("消息处理异常：%s", error, exc_info=error)

        task.add_done_callback(_log_error)

    client.on("connected", lambda: logger.info("WebSocket 已连接，正在认证…"))
    client.on(
        "authenticated",
        lambda: logger.info("认证成功，库存助手已上线"),
    )
    client.on("disconnected", lambda reason: logger.warning("连接断开：%s", reason))
    client.on("reconnecting", lambda attempt: logger.warning("第 %s 次重连…", attempt))
    client.on("error", _on_error)
    client.on("message.text", lambda frame: _spawn(_handle_text(client, frame)))

    def _on_other(frame: dict) -> None:
        body = frame.get("body") or {}
        msgid = str(body.get("msgid") or "stream")
        logger.info("收到非文本消息 msgtype=%s", body.get("msgtype"))
        _spawn(_push(client, frame, msgid, "目前只支持文字提问，可以打字告诉我。", finish=True))

    for event in ("message.image", "message.mixed", "message.voice", "message.file"):
        client.on(event, _on_other)

    await client.connect()
    logger.info(
        "库目录=%s | 模型=%s | 在线中，Ctrl+C 退出",
        os.environ.get("DAILYCHECK_DB_DIR") or "(默认 db/)",
        os.environ.get("LLM_MODEL") or agent.DEFAULT_MODEL,
    )
    try:
        await stop.wait()
    finally:
        client.disconnect()
    return 3 if auth_rejected else 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        config = load_bot_config(os.environ)
    except BotConfigError as exc:
        logger.error("%s", exc)
        return 2

    logger.info("准备连接 BotID=%s Secret=%s", config.bot_id, mask_secret(config.secret))
    try:
        return asyncio.run(amain(config))
    except KeyboardInterrupt:
        logger.info("收到 Ctrl+C，已退出")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
