#!/usr/bin/env python3
"""scripts/gen_invariants.py

不变量登记册生成器：把散落在测试 docstring 里的 `Invariant:` 标记汇成一份表。

为什么是「生成」而不是「手写」（2026-10-21）：
  登记册类文档在 agent 协作里必然腐烂 —— 没人记得更新，三个月后没人敢信。
  所以让事实锚在**可执行的守卫**上：

      def test_kpi_today_boundary_is_tz_independent():
          \"\"\"「今日」判据必须显式 CST，不依赖进程 TZ。

          Invariant: I-01 | 「今日」判据显式走 CST | source=#117
          \"\"\"

  登记册由这些标记派生。于是三种腐烂方式被结构性堵住：
    1. 登记了但没人守 → 标记只能写在测试函数 docstring 里，写在别处扫不到
    2. 测试被删悄悄少一条 → `--check` 对比 base 版 ID 集合，消失即红
       （退役须写进 invariants-retired.txt 并给原因）
    3. 文档膨胀成没人读 → 只登「真出过事」的，source 必须是事故/Issue 号

用法：
  python scripts/gen_invariants.py --write    # 重写登记册
  python scripts/gen_invariants.py --check    # 与提交版本比对，不一致退出 1
  python scripts/gen_invariants.py --check --base origin/main
                                             # 额外校验「ID 不许无声消失」

退出码：0 一致 / 1 不一致或有静默消失 / 2 用法或标记格式错误
"""
from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"
OUT = REPO_ROOT / "docs" / "agent-framework" / "invariants.md"
RETIRED = REPO_ROOT / "docs" / "agent-framework" / "invariants-retired.txt"

# Invariant: I-01 | 一句话陈述 | source=#117
MARK_RE = re.compile(r"Invariant:\s*(I-\d+)\s*\|\s*(.+?)\s*\|\s*source=(\S+)")
ID_RE = re.compile(r"^(I-\d+)\b")
RETIRED_RE = re.compile(r"^(I-\d+)\b")


def _iter_test_files() -> list[Path]:
    if not TESTS_DIR.exists():
        return []
    return sorted(TESTS_DIR.rglob("test_*.py"))


def scan_marks() -> list[dict]:
    """扫出所有 Invariant 标记。同一测试可登记多条不变量。"""
    found: list[dict] = []
    for path in _iter_test_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            # 解析不了的文件不静默跳过：让人知道有文件没被扫到
            print(f"[invariants] 警告：{path} 解析失败，已跳过", file=sys.stderr)
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            doc = ast.get_docstring(node)
            if not doc:
                continue
            for line in doc.splitlines():
                m = MARK_RE.search(line)
                if not m:
                    continue
                found.append({
                    "id": m.group(1),
                    "statement": m.group(2).strip(),
                    "source": m.group(3).strip(),
                    "guard": f"{path.relative_to(REPO_ROOT).as_posix()}::{node.name}",
                })
    return found


def load_retired() -> set[str]:
    if not RETIRED.exists():
        return set()
    ids: set[str] = set()
    for line in RETIRED.read_text(encoding="utf-8").splitlines():
        m = RETIRED_RE.match(line.strip())
        if m:
            ids.add(m.group(1))
    return ids


def validate(items: list[dict]) -> list[str]:
    """重复 ID / 缺字段 → 错误列表。"""
    errors: list[str] = []
    seen: dict[str, str] = {}
    for it in items:
        if it["id"] in seen:
            errors.append(
                f"{it['id']} 重复登记：{seen[it['id']]} 与 {it['guard']}"
            )
        else:
            seen[it["id"]] = it["guard"]
        if not it["source"].startswith("#"):
            errors.append(
                f"{it['id']} 的 source={it['source']!r} 不是 Issue 号"
                "（只登真出过事的，不登最佳实践）"
            )
    return errors


def render(items: list[dict]) -> str:
    items = sorted(items, key=lambda x: int(x["id"].split("-")[1]))
    lines = [
        "# 不变量登记册",
        "",
        "> **本文件由 `scripts/gen_invariants.py --write` 生成，禁手改。**",
        "> 事实来源是测试 docstring 里的 `Invariant:` 标记 —— 不变量只能登记在",
        "> **可执行的守卫**上，写在别处扫不到。这样「登记了但没人守」在结构上",
        "> 就不可能发生。",
        ">",
        "> 只登**真出过事**的（source 必须是 Issue 号），不登「最佳实践」——",
        "> 后者会让登记册膨胀成没人读的文档。",
        "",
        "| ID | 不变量 | 来源 | 守卫 |",
        "|---|---|---|---|",
    ]
    for it in items:
        lines.append(
            f"| {it['id']} | {it['statement']} | {it['source']} | "
            f"`{it['guard']}` |"
        )
    if not items:
        lines.append("| — | （尚无登记） | — | — |")

    retired = load_retired()
    lines += [
        "",
        "## 已退役",
        "",
        "见 `invariants-retired.txt`（手写，仅追加）。退役必须写明原因与替代项，",
        "否则 `--check --base` 会报「ID 静默消失」。",
        "",
    ]
    if retired:
        lines.append("退役 ID：" + "、".join(sorted(retired)))
    else:
        lines.append("（暂无）")
    return "\n".join(lines) + "\n"


def read_ids_at_base(base: str) -> set[str] | None:
    try:
        proc = subprocess.run(
            ["git", "show", f"{base}:{OUT.relative_to(REPO_ROOT).as_posix()}"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    ids: set[str] = set()
    for line in proc.stdout.splitlines():
        if line.startswith("| I-"):
            m = ID_RE.match(line[2:].strip())
            if m:
                ids.add(m.group(1))
    return ids


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--write", action="store_true", help="重写登记册")
    g.add_argument("--check", action="store_true", help="与提交版本比对（默认）")
    ap.add_argument("--base", default="", help="校验 ID 不许无声消失的基线提交")
    args = ap.parse_args()

    items = scan_marks()
    errors = validate(items)
    if errors:
        for e in errors:
            print(f"[invariants] FAIL: {e}", file=sys.stderr)
        return 2

    content = render(items)

    if args.write:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(content, encoding="utf-8")
        print(f"[invariants] 已写入 {OUT.relative_to(REPO_ROOT)}（{len(items)} 条）")
        return 0

    if not OUT.exists():
        print(f"[invariants] FAIL: {OUT.relative_to(REPO_ROOT)} 不存在，"
              "先跑 --write", file=sys.stderr)
        return 1
    if OUT.read_text(encoding="utf-8") != content:
        print(
            "[invariants] FAIL: 登记册与代码不一致 —— 改了测试的 Invariant 标记后"
            "忘了跑 `scripts/gen_invariants.py --write`",
            file=sys.stderr,
        )
        return 1

    if args.base:
        base_ids = read_ids_at_base(args.base)
        if base_ids is None:
            print(f"[invariants] 警告：读不到 {args.base} 的登记册，跳过消失检查")
        else:
            cur_ids = {it["id"] for it in items}
            vanished = base_ids - cur_ids - load_retired()
            if vanished:
                print(
                    "[invariants] FAIL: 以下不变量 ID 静默消失（守卫测试可能被删）：\n"
                    "  " + "、".join(sorted(vanished)) + "\n"
                    "  若确属退役，请写入 docs/agent-framework/invariants-retired.txt"
                    " 并说明原因与替代项。",
                    file=sys.stderr,
                )
                return 1

    print(f"[invariants] OK: {len(items)} 条不变量与代码一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
