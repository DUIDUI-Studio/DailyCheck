"""数字校验：确认回复里的数字能在工具返回里找到出处。

**为什么需要这一层**：模型不会改库（工具集里没有写函数，库还是只读打开的），
但它会**算错数**——员工问「六个仓加起来多少」，它可能自己把六个数字加一遍，
而不是用服务端算好的 ``total_quantity``。大模型的算术不可靠，而库存数字错一次
就没人再信这个机器人了。

做法是事后对账：把工具返回里出现过的所有数值收成一个可信集合，再扫回复里的
数字，找出没有出处的。命中的话由上层触发一次「只修数字」的重写。

**为什么只盯大数**：回复里天然有大量合法却无出处的数字——「6 个仓」「前 3 项」
「一共 12 条」都是模型自己数出来的，全查会 100% 误报，把正常的回答也打回去重写。
真正会伤到业务的只有库存量和金额这类大数，所以小于 ``SMALL_NUMBER_PASS`` 的
数字直接放行，只盯它们以上的。
"""

from __future__ import annotations

import re
from typing import Any

# 这个数以下的数字不校验。序号、条数、「前 N 项」都在这个量级，
# 查了只会制造误报；而库存量和金额基本都在这之上。
SMALL_NUMBER_PASS = 20

# 回复里的数字：支持千分位与小数，前后不能紧贴字母数字
# （否则 ``PP017``、``wh_000`` 里的数字会被当成业务数据）。
# ``*`` / ``×`` 也加进排除集：``40*30`` 是规格，不是库存量。
_NUMBER_RE = re.compile(r"(?<![\w.*×])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(?![\w*×])")

# 规格表达式：``标签纸40*30``、``20 × 30``、``A4 规格 21x29``。
# 这类数字属于品名/规格，不参与库存与金额对账，否则天天误报。
_SPEC_RE = re.compile(r"\d+(?:\.\d+)?\s*[*×xX]\s*\d+(?:\.\d+)?")

# 年份不该被当成库存量。1900-2100 之外的值仍然照查。
_YEAR_RANGE = range(1900, 2101)


def _number_variants(value: float) -> set[str]:
    """一个数值的可能写法。

    工具返回 ``5.666666``，模型可能写 ``5.67``、``5.7`` 或 ``6``；反过来
    工具返回 ``468``，模型可能写 ``468.0``。都算有出处，所以生成多个写法。
    整数不做四舍五入变体——``5.67`` 不该被 ``6`` 蒙混过关。
    """
    if value == int(value):
        return {str(int(value))}
    out = {f"{value:.3f}".rstrip("0").rstrip(".")}
    for digits in (1, 2):
        out.add(f"{round(value, digits):.{digits}f}".rstrip("0").rstrip("."))
    return out


def collect_numbers(payload: Any, _depth: int = 0) -> set[str]:
    """递归收集工具返回里出现过的所有数字。

    数字字段直接收；**字符串字段也收**（复用 ``extract_numbers``）。

    为什么字符串也算出处：工具返回的文字是我们自己生成的，里面的数字都有
    依据——``"name": "标签纸40*30"`` 的规格、``"note": "共 12 个品项"`` 的
    计数，都是事实。反过来，如果只认数字类型，模型照抄品名里的规格就会被
    判成幻觉，天天触发无谓的校正（实测踩过：``标签纸40*30`` 的 30 被当成
    编造的库存量）。
    """
    if _depth > 12:  # 防御异常深的嵌套，工具返回不会这么深
        return set()
    out: set[str] = set()
    if isinstance(payload, bool):
        return set()
    if isinstance(payload, (int, float)):
        out |= _number_variants(float(payload))
    elif isinstance(payload, str):
        out |= set(extract_numbers(payload))
    elif isinstance(payload, dict):
        for value in payload.values():
            out |= collect_numbers(value, _depth + 1)
    elif isinstance(payload, list | tuple):
        for value in payload:
            out |= collect_numbers(value, _depth + 1)
    return out


def extract_numbers(text: str) -> list[str]:
    """从回复文本里提取数字，返回规范化后的字符串（去千分位逗号）。

    规格表达式（``40*30``）整段跳过——它描述的是商品尺寸，不是业务数据。
    """
    text = text or ""
    spec_spans = [match.span() for match in _SPEC_RE.finditer(text)]

    found: list[str] = []
    for match in _NUMBER_RE.finditer(text):
        if any(start <= match.start() < end for start, end in spec_spans):
            continue
        raw = match.group(1)
        # 百分数不是库存量也不是金额，跳过（"缺 50%" 不该被当成数字 50 去对账）
        tail = text[match.end() : match.end() + 1]
        if tail in {"%", "％"}:
            continue
        normalized = raw.replace(",", "")
        if normalized.endswith(".0"):
            normalized = normalized[:-2]
        found.append(normalized)
    return found


def _is_year(value: float) -> bool:
    return value == int(value) and int(value) in _YEAR_RANGE


def find_untraceable(
    reply: str, trusted: set[str], min_value: float = SMALL_NUMBER_PASS
) -> list[str]:
    """找出回复里没有出处、且大到值得追究的数字（保持出现顺序，去重）。

    返回空列表代表这份回答在数字上是干净的。
    """
    suspicious: list[str] = []
    seen: set[str] = set()
    for text in extract_numbers(reply):
        if text in seen:
            continue
        try:
            value = float(text)
        except ValueError:
            continue
        if value <= min_value:
            continue
        if _is_year(value):
            continue
        if text in trusted:
            continue
        seen.add(text)
        suspicious.append(text)
    return suspicious


def build_correction_prompt(
    question: str, original: str, suspicious: list[str], tool_digest: str
) -> str:
    """给「只修数字」的那次调用用的提示词。

    明确禁止重新组织语言：改写整段容易引入新的错，也会让用户看到答案大变样。
    """
    numbers = "、".join(suspicious)
    return (
        "你是库存数据的校对员。下面是一份回答和它的查询结果。\n"
        f"回答里这些数字在查询结果中找不到出处：{numbers}\n\n"
        "请重写这份回答，要求：\n"
        "1. 只订正数字，其余措辞、结构、顺序保持不变；\n"
        "2. 所有数字必须直接来自查询结果，不要自己相加、相减、估算；\n"
        "3. 如果某个数字确实无法从查询结果得出，改写那句话，不要说数字；\n"
        "4. 只输出重写后的回答正文，不要解释你改了什么。\n\n"
        f"【用户的问题】\n{question}\n\n"
        f"【查询结果】\n{tool_digest}\n\n"
        f"【原回答】\n{original}\n"
    )
