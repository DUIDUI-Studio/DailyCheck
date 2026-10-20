"""凭证装载的单元测试。"""

import pytest

from bot.config_pure import (
    ENV_BOT_ID,
    ENV_BOT_SECRET,
    BotConfigError,
    load_bot_config,
    mask_secret,
)


class TestLoadBotConfig:
    def test_正常装载(self):
        config = load_bot_config({ENV_BOT_ID: "bot-1", ENV_BOT_SECRET: "s3cret"})
        assert config.bot_id == "bot-1"
        assert config.secret == "s3cret"

    def test_去除首尾空白(self):
        config = load_bot_config({ENV_BOT_ID: " bot-1 ", ENV_BOT_SECRET: " s3cret "})
        assert config.bot_id == "bot-1"
        assert config.secret == "s3cret"

    def test_缺_bot_id_报错并指名(self):
        with pytest.raises(BotConfigError) as exc:
            load_bot_config({ENV_BOT_SECRET: "s3cret"})
        assert ENV_BOT_ID in str(exc.value)

    def test_缺_secret_报错并指名(self):
        with pytest.raises(BotConfigError) as exc:
            load_bot_config({ENV_BOT_ID: "bot-1"})
        assert ENV_BOT_SECRET in str(exc.value)

    def test_空字符串等同缺失(self):
        with pytest.raises(BotConfigError):
            load_bot_config({ENV_BOT_ID: "  ", ENV_BOT_SECRET: "s3cret"})


class TestSecretMasking:
    def test_repr_不泄露_secret(self):
        config = load_bot_config({ENV_BOT_ID: "bot-1", ENV_BOT_SECRET: "topsecretvalue"})
        assert "topsecretvalue" not in repr(config)
        assert "tops***" in repr(config)

    def test_短_secret_全遮(self):
        assert mask_secret("abc") == "***"

    def test_空_secret_返回空(self):
        assert mask_secret("") == ""
