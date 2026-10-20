"""接入测试的纯逻辑：@ 前缀剥离、userid 明文判定、回显文本构造。

这一版的目的不是查库存，而是**用一次 @ 验证整条链路**，并且顺手回答
方案里那个必须先确认的问题：``from.userid`` 到底是明文还是加密。

判定结果只有「疑似」二字 —— 企微只在机器人创建者是超管时才给明文，
加密串的形态官方没有公开约定，用长度 + 字符集做启发式，不做断言。
真正要确认，看后台能不能对上这个人。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# 群聊里 @机器人 时 text.content 形如 "@库存查询 库存 A001"。
# 非贪婪 + 必须跟空白：没空格说明整条消息就是个 @，剥完为空，交给上层提示。
_MENTION_PREFIX = re.compile(r"^@\S+?\s+")

# 企微明文 userid 通常是英文名 / 工号，短且只含常规字符。
# 超过 32 位基本可以认定是加密串（base64 风格，官方未公开具体形态）。
_PLAIN_USERID = re.compile(r"^[A-Za-z0-9_.\-]{1,32}$")

_HELP_HINT = "试一下：@机器人 你好"


def strip_mention(text: str) -> str:
    """剥掉群聊 @机器人 留下的前缀，返回真正的用户输入。

    只剥开头第一段 ``@xxx`` 加其后的空白，正文原样保留。纯文本消息不动。

    两种边角情况：
    - 整条消息就是个 ``@机器人``（无正文）→ 返回空串，由上层提示"没带内容"
    - ``@`` 出现在正文中间（``查一下 @张三 的库存``）→ 原样保留，不误伤
    """
    if not text:
        return ""
    stripped = _MENTION_PREFIX.sub("", text, count=1)
    if stripped == text:
        # 没匹配到"@xxx + 空白"：要么整条就是 @xxx，要么 @ 不在开头
        if text.startswith("@") and not re.search(r"\s", text):
            return ""
        return text.strip()
    return stripped.strip()


def guess_userid_state(userid: str) -> str:
    """粗判 userid 是明文还是加密，只返回「疑似」结论。"""
    if not userid:
        return "缺失"
    if len(userid) <= 32 and _PLAIN_USERID.match(userid):
        return "疑似明文"
    return "疑似加密"


def _short(value: str, keep: int = 10) -> str:
    """截断长标识，保留可对照的前缀。"""
    if not value:
        return "无"
    if len(value) <= keep:
        return value
    return value[:keep] + "…"


def build_probe_reply(body: Mapping[str, Any]) -> str:
    """根据回调 body 生成回显文本。

    参数是企微回调的 ``body``（``aibot_msg_callback`` 里那层），字段全部
    用 ``.get`` 读取 —— 回调结构随消息类型变化，缺字段不能抛异常。
    """
    msgtype = str(body.get("msgtype") or "unknown")
    sender = str((body.get("from") or {}).get("userid") or "")
    chattype = str(body.get("chattype") or "unknown")
    chat_label = {"group": "群聊", "single": "单聊"}.get(chattype, chattype)

    text_block = body.get("text") or {}
    raw_text = str(text_block.get("content") or "")
    content = strip_mention(raw_text)

    if msgtype != "text":
        return (
            f"pong · 接入正常\n\n"
            f"这条是「{msgtype}」类型，接入测试只处理文字消息。\n"
            f"发送者：{sender or '无'}（{guess_userid_state(sender)}）\n"
            f"会话：{chat_label}"
        )

    if not content:
        return (
            f"pong · 接入正常\n\n"
            f"你只 @ 了我一下，没带内容。\n"
            f"{_HELP_HINT}\n"
            f"发送者：{sender or '无'}（{guess_userid_state(sender)}）\n"
            f"会话：{chat_label}"
        )

    lines = [
        "pong · 接入正常",
        "",
        f"原文：{content}",
        f"会话：{chat_label}（id {_short(str(body.get('chatid') or ''))}）",
        f"发送者：{sender or '无'}（{guess_userid_state(sender)}）",
        f"消息 ID：{_short(str(body.get('msgid') or ''))}",
        "",
        "这条 sender 就是后面做「企微 userid → 系统账号」映射的键。",
    ]
    return "\n".join(lines)
