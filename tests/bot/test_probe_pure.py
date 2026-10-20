"""接入测试纯逻辑单元测试。

这一版没有 DB、没有网络，测的都是「拿到回调之后怎么解读」这段，
也是后面正式指令解析要复用的地基。
"""

from bot.probe_pure import build_probe_reply, guess_userid_state, strip_mention


class TestStripMention:
    def test_剥掉群聊_at_前缀(self):
        assert strip_mention("@库存查询 库存 A001") == "库存 A001"

    def test_只_at_没内容时返回空串(self):
        assert strip_mention("@库存查询") == ""

    def test_无_at_的原样返回(self):
        assert strip_mention("库存 A001") == "库存 A001"

    def test_空串不炸(self):
        assert strip_mention("") == ""

    def test_多个空格不残留(self):
        assert strip_mention("@库存查询   库存 A001") == "库存 A001"

    def test_全角空格也能剥(self):
        assert strip_mention("@库存查询\u3000库存 A001") == "库存 A001"

    def test_正文中间的_at_不动(self):
        assert strip_mention("查一下 @张三 的库存") == "查一下 @张三 的库存"


class TestGuessUseridState:
    def test_英文名判明文(self):
        assert guess_userid_state("zhangsan") == "疑似明文"

    def test_点线下划线判明文(self):
        assert guess_userid_state("zhang.san_01") == "疑似明文"

    def test_空值(self):
        assert guess_userid_state("") == "缺失"

    def test_超长串判加密(self):
        assert guess_userid_state("a" * 40) == "疑似加密"

    def test_base64_特殊字符判加密(self):
        assert guess_userid_state("Zm9vYmFy+/==") == "疑似加密"


class TestBuildProbeReply:
    def _body(self, **overrides):
        body = {
            "msgid": "MSGID123456789",
            "aibotid": "BOT",
            "chatid": "CHATID123456789",
            "chattype": "group",
            "from": {"userid": "zhangsan"},
            "msgtype": "text",
            "text": {"content": "@库存查询 库存 A001"},
        }
        body.update(overrides)
        return body

    def test_正常文本回显关键字段(self):
        reply = build_probe_reply(self._body())
        assert "pong" in reply
        assert "原文：库存 A001" in reply
        assert "疑似明文" in reply
        assert "群聊" in reply

    def test_只_at_没内容时给出提示(self):
        reply = build_probe_reply(self._body(text={"content": "@库存查询"}))
        assert "没带内容" in reply

    def test_非文本类型不报错且说明类型(self):
        reply = build_probe_reply(self._body(msgtype="image"))
        assert "image" in reply
        assert "pong" in reply

    def test_单聊会话标签(self):
        reply = build_probe_reply(self._body(chattype="single"))
        assert "单聊" in reply

    def test_缺字段不抛异常(self):
        reply = build_probe_reply({})
        assert "pong" in reply

    def test_from_缺失时不抛异常(self):
        body = self._body()
        body.pop("from")
        assert "pong" in build_probe_reply(body)
