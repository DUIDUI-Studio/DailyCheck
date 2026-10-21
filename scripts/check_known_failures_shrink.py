#!/usr/bin/env python3
"""scripts/check_known_failures_shrink.py

红线 1 的机器守卫：`tests/known_failures.txt` **只许收缩不许扩张**。

为什么需要（2026-10-21）：
  workflow.md §四 红线 1 与 CI 头注释都写着「清单只许收缩」，但从 2026-10-10
  起这条一直没有机器守卫 —— 往清单里加一条已知失败，CI 照样全绿。
  等于「你可以任意扩大免死范围，没人拦」。红线没有执行器就不是红线。

判据：与 base 提交的清单逐条比对，**新增条目即失败**；删除条目放行（收缩是
     被鼓励的，已知项修好后本就该删，防止清单变成垃圾桶）。

用法：python scripts/check_known_failures_shrink.py --base <sha>
退出码：0 未扩张 / 1 出现新增条目 / 2 用法错
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REL_PATH = "tests/known_failures.txt"
KNOWN_FILE = REPO_ROOT / REL_PATH


def load_ids(text: str) -> set[str]:
    return {
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


def read_at_base(base: str) -> str | None:
    """取 base 提交里的清单内容。取不到（新文件/浅克隆）→ None。"""
    try:
        proc = subprocess.run(
            ["git", "show", f"{base}:{REL_PATH}"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", required=True, help="基线提交（PR 的 base sha）")
    args = ap.parse_args()

    if not KNOWN_FILE.exists():
        print(f"FAIL: {REL_PATH} 不存在", file=sys.stderr)
        return 2

    base_text = read_at_base(args.base)
    if base_text is None:
        # 不断言：读不到基线时既不放过也不误杀，明说无法比对。
        print(f"[shrink] 警告：无法读取 {args.base}:{REL_PATH}，跳过扩张检查")
        return 0

    base_ids = load_ids(base_text)
    cur_ids = load_ids(KNOWN_FILE.read_text(encoding="utf-8"))

    added = sorted(cur_ids - base_ids)
    removed = sorted(base_ids - cur_ids)

    print(f"[shrink] 基线 {len(base_ids)} 条 → 当前 {len(cur_ids)} 条"
          f"（新增 {len(added)}，移除 {len(removed)}）")
    if removed:
        print("[shrink] 已收缩（好事）：")
        for nodeid in removed:
            print(f"  - {nodeid}")

    if added:
        print(
            "\nFAIL: tests/known_failures.txt 出现新增条目。\n"
            "     红线 1「清单只许收缩不许扩张」——新增已知失败等于扩大免死范围。\n"
            "     正确做法：修掉失败，而不是把失败登记进清单。\n"
            "     若确属外部不可控因素导致的新基线，须 owner 拍板后单独走红线流程。",
            file=sys.stderr,
        )
        for nodeid in added:
            print(f"  + {nodeid}", file=sys.stderr)
        return 1

    print("[shrink] ✅ 未扩张")
    return 0


if __name__ == "__main__":
    sys.exit(main())
