"""企微智能机器人长连接 —— 接入测试（最小闭环）。

目标只有一件事：**证明这条链路通**。收到群里 @ 的消息，原样回显关键的
几个字段，顺便把 ``from.userid`` 是明文还是加密暴露出来。

跑法::

    export WECOM_BOT_ID=你的BotID
    export WECOM_BOT_SECRET=你的Secret
    .venv/bin/python -m bot.probe

然后在群里 @机器人 发一句话。收到的原始 frame 会完整打进日志，
用于核对字段（也用于对照企微后台的 userid）。

前置条件（在企微管理后台做，一次性）：
  1. 智能机器人 → 创建 → 可见范围只勾测试人员
  2. 配置页 → API 模式 → 选「长连接」→ 记下 BotID / Secret
  3. 把机器人拉进测试群

为什么不走「接收消息回调 URL」：长连接是出站 WebSocket，不用开防火墙、
不用公网证书、不用做消息加解密。代价是要自己保活（SDK 已带心跳和重连）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from bot.config_pure import BotConfig, BotConfigError, load_bot_config, mask_secret
from bot.probe_pure import build_probe_reply

try:
    from aibot import WSClient, WSClientOptions  # type: ignore[import-not-found]
except ImportError as exc:  # pragma: no cover - 环境问题，不走单测
    raise SystemExit(
        "缺少企微官方 SDK。先装：.venv/bin/pip install wecom-aibot-python-sdk"
    ) from exc

logger = logging.getLogger("bot.probe")

# 回复超时：企微对被动回复有时限，卡住就放弃，不要拖着整条链路。
REPLY_TIMEOUT_SECONDS = 5.0

# 凭证类错误码：命中就退出，不要假装还在跑。853000 = bot_id/secret 不匹配。
# 故意写成整数：字符串元组会被密钥扫描器误判成「账号:密码」对（GitGuardian
# 报的 Authentication Tuple 就是这么来的），而错误码本身是数字。
AUTH_ERROR_CODES = (853000, 853001)
AUTH_ERROR_HINT = "invalid bot_id or secret"


class _SdkLogger:
    """把 SDK 的日志并进标准 logging，避免两套时间格式混在一起。

    SDK 内部一律传已格式化的字符串（f-string），这里原样转发。
    """

    def __init__(self, name: str = "bot.sdk") -> None:
        self._log = logging.getLogger(name)

    def debug(self, message: str, *args: object) -> None:
        self._log.debug(message, *args)

    def info(self, message: str, *args: object) -> None:
        self._log.info(message, *args)

    def warn(self, message: str, *args: object) -> None:
        self._log.warning(message, *args)

    def error(self, message: str, *args: object) -> None:
        self._log.error(message, *args)


def force_direct_connection() -> None:
    """强制长连接直连，绕开本机代理。

    2026-10-21 实测：同一个 BotID/Secret，经本机 HTTP 代理转发时企微返回
    ``853000 invalid bot_id or secret``，直连则 ``errcode 0``。而同一对凭证
    走 REST ``aibot/get_token`` 无论哪条路都成功 —— 所以不是凭证问题，
    是代理链路把长连接的订阅帧弄坏了。

    ``websockets`` 17 的 ``connect()`` 默认读 urllib 的代理设置（macOS 上
    还会读系统代理），SDK 又不暴露 ``proxy`` 参数，只能在调用前替换函数。
    这个 patch 必须在 ``client.connect()`` 之前完成。
    """
    import websockets

    current = websockets.connect
    if getattr(current, "_dailycheck_direct", False):
        return

    def _direct_connect(*args: object, **kwargs: object) -> object:
        kwargs["proxy"] = None
        return current(*args, **kwargs)

    _direct_connect._dailycheck_direct = True  # type: ignore[attr-defined]
    websockets.connect = _direct_connect  # type: ignore[assignment]
    logger.info("已强制长连接直连（忽略本机代理设置）")


def _log_task_error(task: asyncio.Task[object]) -> None:
    """create_task 的异常默认只进 event loop handler，这里捞出来打日志。"""
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.error("消息处理失败：%s", error, exc_info=error)


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    task.add_done_callback(_log_task_error)


async def _send_reply(client: WSClient, frame: dict, text: str) -> None:
    """回复一条消息。

    长连接模式下被动回复**只接受 ``stream`` 类型**：用 ``msgtype: text`` 会被
    服务端拒成 ``40008 invalid message type``（2026-10-21 实测，官方文档
    「长连接模式下不再有流式刷新回调，需主动推送直到 finish=true」即指此）。
    这里一次发完，直接 ``finish=True``；stream id 用 msgid，保证后续若改成
    分段更新时仍落在同一个气泡里。
    """
    req_id = (frame.get("headers") or {}).get("req_id", "")
    message_id = str((frame.get("body") or {}).get("msgid") or req_id or "stream")
    try:
        ack = await asyncio.wait_for(
            client.reply_stream(frame, stream_id=message_id, content=text, finish=True),
            timeout=REPLY_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.error("回复超时 req_id=%s", req_id)
        return
    except Exception as exc:  # noqa: BLE001 - 对外通道，任何异常都不该掀掉连接
        logger.error("回复异常 req_id=%s err=%s", req_id, exc)
        return

    if isinstance(ack, dict) and ack.get("errcode"):
        logger.error(
            "回复被拒 req_id=%s errcode=%s errmsg=%s",
            req_id,
            ack.get("errcode"),
            ack.get("errmsg"),
        )
        return
    logger.info("已回复 req_id=%s", req_id)


async def _handle_text(client: WSClient, frame: dict) -> None:
    body = frame.get("body") or {}
    logger.info("收到文本消息，原始 frame：%s", json.dumps(frame, ensure_ascii=False))
    await _send_reply(client, frame, build_probe_reply(body))


async def amain(config: BotConfig) -> int:
    """跑起来就一直挂着，直到 Ctrl+C 或凭证被拒。返回进程退出码。"""
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
        if any(str(code) in message for code in AUTH_ERROR_CODES) or AUTH_ERROR_HINT in message:
            auth_rejected = True
            logger.error("凭证被拒。核对 WECOM_BOT_ID / WECOM_BOT_SECRET 后重跑。")
            stop.set()

    def _on_authenticated() -> None:
        logger.info("认证成功，机器人已上线。去群里 @ 机器人发一句话。")

    client.on("connected", lambda: logger.info("WebSocket 已连接，正在认证…"))
    client.on("authenticated", _on_authenticated)
    client.on("disconnected", lambda reason: logger.warning("连接断开：%s", reason))
    client.on("reconnecting", lambda attempt: logger.warning("第 %s 次重连…", attempt))
    client.on("error", _on_error)

    client.on("message.text", lambda frame: _spawn(_handle_text(client, frame)))

    def _on_other(frame: dict) -> None:
        body = frame.get("body") or {}
        logger.info("收到非文本消息 msgtype=%s", body.get("msgtype"))
        _spawn(_send_reply(client, frame, build_probe_reply(body)))

    for event in ("message.image", "message.mixed", "message.voice", "message.file"):
        client.on(event, _on_other)

    await client.connect()
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
    exit_code = 0
    try:
        exit_code = asyncio.run(amain(config))
    except KeyboardInterrupt:
        logger.info("收到 Ctrl+C，已退出")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
