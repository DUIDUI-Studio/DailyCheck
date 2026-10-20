"""数字校验纯函数的单测。"""

from __future__ import annotations

import pytest

from bot import verify_pure
from bot.verify_pure import (
    build_correction_prompt,
    collect_numbers,
    extract_numbers,
    find_untraceable,
)


class TestCollectNumbers:
    def test_收集嵌套结构里的数字(self):
        payload = {
            "total_quantity": 5.67,
            "details": [{"quantity": 1.2, "safety_stock": 0}, {"quantity": 4.47}],
        }
        trusted = collect_numbers(payload)
        # 小数会带上多精度写法，所以用包含而不是全等
        assert {"5.67", "1.2", "0", "4.47"} <= trusted

    def test_字符串里的数字不算出处(self):
        # note 是工具自己写的说明文字，不能给模型的数字当靠山
        payload = {"note": "共 12 个品项", "total_items": 127}
        trusted = collect_numbers(payload)
        assert "127" in trusted
        assert "12" not in trusted

    def test_布尔值不被当成数字(self):
        assert collect_numbers({"ok": True, "below_safety": False}) == set()

    def test_空结构返回空集(self):
        assert collect_numbers({}) == set()
        assert collect_numbers([]) == set()
        assert collect_numbers(None) == set()

    def test_整数不生成四舍五入变体(self):
        # 5.67 不该因为 round 到 6 而被认为有出处
        variants = verify_pure._number_variants(5.67)
        assert "6" not in variants
        assert "5.67" in variants

    def test_小数生成多精度写法(self):
        variants = verify_pure._number_variants(5.666666)
        assert "5.67" in variants
        assert "5.7" in variants

    def test_整数原样(self):
        assert verify_pure._number_variants(468.0) == {"468"}


class TestExtractNumbers:
    def test_千分位归一(self):
        assert extract_numbers("一共 1,234 箱") == ["1234"]

    def test_小数保留(self):
        assert extract_numbers("合计 5.67 箱") == ["5.67"]

    def test_SKU里的数字不算(self):
        # PP017 / wh_000 里的数字不是业务数据
        assert extract_numbers("PP017 在 wh_000 里") == []

    def test_百分数跳过(self):
        assert extract_numbers("缺 50%") == []

    def test_空文本(self):
        assert extract_numbers("") == []

    def test_多个数字按出现顺序(self):
        assert extract_numbers("A 仓 12 箱，B 仓 3.5 箱") == ["12", "3.5"]


class TestFindUntraceable:
    def test_全部有出处时返回空(self):
        reply = "冷冻芒果酱一共 5.67 箱，售价 468 元"
        trusted = {"5.67", "468"}
        assert find_untraceable(reply, trusted) == []

    def test_编造的大数被揪出(self):
        # 员工问「六个仓加起来多少」，模型自己加出一个 125
        reply = "冷冻芒果酱一共 125 箱"
        trusted = {"5.67", "1.2", "4.47"}
        assert find_untraceable(reply, trusted) == ["125"]

    def test_小数字放行(self):
        # 「6 个仓」「前 3 项」是模型自己数的，不该打回
        reply = "分布在 6 个仓，前 3 项如下"
        assert find_untraceable(reply, set()) == []

    def test_年份放行(self):
        assert find_untraceable("数据更新于 2026 年", set()) == []

    def test_小数容差可追溯(self):
        # 工具返回 5.666666，模型写 5.67 算有出处
        trusted = collect_numbers({"total": 5.666666})
        assert find_untraceable("合计 5.67 箱", trusted) == []

    def test_相同可疑数字只报一次(self):
        assert find_untraceable("999 箱不够，999 箱很少", set()) == ["999"]

    def test_边界值按阈值判定(self):
        # SMALL_NUMBER_PASS=20 表示 20 及以下放行
        assert find_untraceable("20 箱", set()) == []
        assert find_untraceable("21 箱", set()) == ["21"]

    def test_自定义阈值(self):
        assert find_untraceable("21 箱", set(), min_value=100) == []


class TestCorrectionPrompt:
    def test_包含可疑数字与原始材料(self):
        prompt = build_correction_prompt(
            question="芒果酱还有多少",
            original="一共 12.5 箱",
            suspicious=["12.5"],
            tool_digest='{"total_quantity": 5.67}',
        )
        assert "12.5" in prompt
        assert "芒果酱还有多少" in prompt
        assert "5.67" in prompt
        assert "只订正数字" in prompt

    def test_多个可疑数字用顿号连接(self):
        prompt = build_correction_prompt("q", "a", ["111", "222"], "{}")
        assert "111、222" in prompt


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0.5 箱", ["0.5"]),
        ("库存为 0", ["0"]),
        ("没有数字", []),
        ("A001 共 5 箱", ["5"]),
    ],
)
def test_extract_numbers_parametrized(text, expected):
    assert extract_numbers(text) == expected
