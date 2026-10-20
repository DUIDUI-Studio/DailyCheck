"""命令行问一句，验证「LLM + 工具」这条链路，不经过企业微信。

    DAILYCHECK_DB_DIR=/tmp/dc-snap/db .venv/bin/python -m bot.ask "冷冻芒果酱还有多少"

存在的意义：企微那一层出问题时，能立刻判断是模型/工具的问题还是接入的问题。
"""

from __future__ import annotations

import asyncio
import logging
import sys

from bot.agent import LLMError, answer


async def _run(question: str) -> int:
    try:
        reply = await answer(question)
    except LLMError as exc:
        print(f"LLM 不可用：{exc}")
        return 1
    print(reply)
    return 0


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s | %(message)s")
    question = " ".join(sys.argv[1:]).strip()
    if not question:
        print('用法: python -m bot.ask "冷冻芒果酱还有多少"')
        return 2
    return asyncio.run(_run(question))


if __name__ == "__main__":
    raise SystemExit(main())
