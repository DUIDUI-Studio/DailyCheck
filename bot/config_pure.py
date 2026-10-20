"""企微智能机器人凭证读取。

凭证只从环境变量取，不落盘、不进日志。``BotConfig.__repr__`` 被重写为
屏蔽 secret —— dataclass 默认 repr 会把 secret 原样打出来，一旦有人
``logger.info("%s", config)`` 就泄露。

获取方式：企微管理后台 → 智能机器人 → 配置页 → 开启「API 模式」→ 选「长连接」，
页面会给 BotID 与 Secret（Secret 是长连接专用，与回调 URL 模式的
Token/EncodingAESKey 不是一回事）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

ENV_BOT_ID = "WECOM_BOT_ID"
ENV_BOT_SECRET = "WECOM_BOT_SECRET"


class BotConfigError(ValueError):
    """配置缺失或非法。"""


@dataclass(frozen=True)
class BotConfig:
    bot_id: str
    secret: str

    def __repr__(self) -> str:  # noqa: D105 - 目的就是不复述父类行为
        return f"BotConfig(bot_id={self.bot_id!r}, secret={mask_secret(self.secret)})"


def load_bot_config(env: Mapping[str, str]) -> BotConfig:
    """从环境变量装载配置；缺一项就抛错，不做兜底默认值。

    宁可启动失败也不要用空凭证去连——空凭证会拿到一个语焉不详的
    订阅失败，排查成本远高于启动时报错。
    """
    bot_id = (env.get(ENV_BOT_ID) or "").strip()
    secret = (env.get(ENV_BOT_SECRET) or "").strip()

    missing = [
        name for name, value in ((ENV_BOT_ID, bot_id), (ENV_BOT_SECRET, secret)) if not value
    ]
    if missing:
        raise BotConfigError(
            "缺少环境变量："
            + "、".join(missing)
            + "。去企微管理后台 → 智能机器人 → 配置页 → 开启「API 模式 → 长连接」后获取。"
        )
    return BotConfig(bot_id=bot_id, secret=secret)


def mask_secret(secret: str) -> str:
    """日志脱敏：只留前 4 位。"""
    if not secret:
        return ""
    if len(secret) <= 4:
        return "***"
    return secret[:4] + "***"
