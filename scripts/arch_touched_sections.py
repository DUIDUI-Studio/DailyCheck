#!/usr/bin/env python3
"""Suggest which sections of docs/ARCHITECTURE.md need updating.

Run before `git add docs/ARCHITECTURE.md` to know what to refresh:

    # staged set (default — what `git commit` would actually commit):
    python scripts/arch_touched_sections.py

    # explicit paths:
    python scripts/arch_touched_sections.py --paths blueprints/items.py db/__init__.py

    # include the working tree (un-staged edits):
    python scripts/arch_touched_sections.py --all

The mapping lives in SUBSYSTEM_TO_SECTIONS at the bottom of this file —
keep it in sync with docs/ARCHITECTURE.md §附 (the "维护本文件的工作流"
table). The two are intentionally duplicated so the doc stays the
human-readable truth and this script stays the machine-checkable truth.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path


# ---------------------------------------------------------------------------
# Subsystem → doc sections mapping.
#
# Each pattern is anchored on the repo root (paths are repo-relative).
# Patterns are matched top-to-bottom; the first match wins, so list the
# most specific patterns first.
# ---------------------------------------------------------------------------

# (regex pattern, [doc section labels]).
# Section labels must match the section anchors in docs/ARCHITECTURE.md
# (rendered as GitHub-flavored markdown headings).
SUBSYSTEM_TO_SECTIONS: list[tuple[str, list[str]]] = [
    # Core entry points
    (r"^app\.py$",
     ["2. 系统拓扑（容器视角）", "3. 部署拓扑", "7. 请求生命周期", "8. 鉴权架构"]),
    (r"^config\.py$",
     ["2. 系统拓扑（容器视角）", "4. 数据架构", "13. 关键约定与坑"]),
    (r"^permissions\.py$",
     ["8. 鉴权架构"]),

    # Database layer
    (r"^db/(?!.*/test_).*\.py$",
     ["4. 数据架构", "13. 关键约定与坑"]),

    # Blueprints — map each blueprint file to the relevant sections.
    (r"^blueprints/auth\.py$",
     ["7. 请求生命周期", "8. 鉴权架构", "5. 应用层模块图", "6. 蓝图依赖关系"]),
    (r"^blueprints/items.*\.py$",
     ["5. 应用层模块图", "6. 蓝图依赖关系", "10. 业务领域模型"]),
    (r"^blueprints/canonical.*\.py$",
     ["5. 应用层模块图", "6. 蓝图依赖关系", "10. 业务领域模型", "10.2 Canonical 主数据治理", "10.4 Canonical 扇出流程"]),
    (r"^blueprints/store_ordering.*\.py$",
     ["5. 应用层模块图", "6. 蓝图依赖关系", "10. 业务领域模型", "10.3 Store Ordering 状态机"]),
    (r"^blueprints/recipe_cost.*\.py$",
     ["5. 应用层模块图", "6. 蓝图依赖关系", "14. pre-existing 风险清单"]),
    (r"^blueprints/forecast.*\.py$",
     ["5. 应用层模块图", "6. 蓝图依赖关系"]),
    (r"^blueprints/procurement.*\.py$",
     ["5. 应用层模块图", "6. 蓝图依赖关系"]),
    (r"^blueprints/notifications.*\.py$",
     ["5. 应用层模块图", "6. 蓝图依赖关系"]),
    (r"^blueprints/publish_recipe.*\.py$",
     ["5. 应用层模块图", "6. 蓝图依赖关系"]),
    (r"^blueprints/(stocktake|restock|outbound|production|adjustment|consumption|reports)\.py$",
     ["5. 应用层模块图", "6. 蓝图依赖关系"]),
    (r"^blueprints/(users|import_items|agent_tokens)\.py$",
     ["5. 应用层模块图", "6. 蓝图依赖关系", "8. 鉴权架构"]),
    (r"^blueprints/_helpers\.py$",
     ["5. 应用层模块图", "13. 关键约定与坑"]),

    # MCP server
    (r"^mcp_server/.*\.py$",
     ["9. MCP 架构"]),

    # Frontend
    (r"^templates/",
     ["11. PWA / 移动端", "5. 应用层模块图"]),
    (r"^static/",
     ["11. PWA / 移动端"]),

    # Tests
    (r"^tests/.*\.py$",
     ["12. 测试架构", "14. pre-existing 风险清单"]),

    # CI / deploy / tooling
    (r"^\.github/workflows/",
     ["3. 部署拓扑", "12. 测试架构", "15. 文档索引"]),
    (r"^deploy/",
     ["3. 部署拓扑"]),
    (r"^scripts/",
     ["3. 部署拓扑", "12. 测试架构", "15. 文档索引"]),

    # The doc itself
    (r"^docs/ARCHITECTURE\.md$",
     []),  # self-update — no extra sections required
]


def match_subsystem(path: str) -> list[str]:
    for pattern, sections in SUBSYSTEM_TO_SECTIONS:
        if re.search(pattern, path):
            return sections
    return []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def collect_staged() -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        check=True, capture_output=True, text=True,
    ).stdout
    return [p for p in out.splitlines() if p]


def collect_paths(args_paths: list[str]) -> list[str]:
    return list(args_paths)


def collect_all() -> list[str]:
    # staged + unstaged + untracked (excluding ignored).
    out = subprocess.run(
        ["git", "status", "--porcelain", "-uall"],
        check=True, capture_output=True, text=True,
    ).stdout
    paths: list[str] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        # porcelain: XY <path>   (XY may include R for renames, but path
        # is the destination side).
        # Format: "<status> <path>" or "<status1><status2> <path>".
        # Strip the leading 2 status chars + space.
        if len(line) < 4:
            continue
        path = line[3:].strip()
        # For renames/copies, take the second path after " -> ".
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(path)
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Suggest ARCHITECTURE.md sections to update based on changed paths."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--paths", nargs="+", help="explicit list of paths to check",
    )
    group.add_argument(
        "--all", action="store_true",
        help="check staged + unstaged + untracked (default: staged only)",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    try:
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=True, cwd=repo_root, capture_output=True,
        )
    except subprocess.CalledProcessError:
        print("!! not inside a git repository", file=sys.stderr)
        return 2

    if args.paths:
        paths = collect_paths(args.paths)
    elif args.all:
        paths = collect_all()
    else:
        paths = collect_staged()

    if not paths:
        print("No changed paths found — nothing to update.")
        return 0

    sections_by_path: dict[str, list[str]] = {}
    all_sections: set[str] = set()
    for path in paths:
        secs = match_subsystem(path)
        if secs:
            sections_by_path[path] = secs
            all_sections.update(secs)

    print("=" * 72)
    print("Architecture sections to update in docs/ARCHITECTURE.md")
    print("=" * 72)
    print()
    print("Changed paths:")
    for p in paths:
        marker = "✓" if p in sections_by_path else " "
        secs = sections_by_path.get(p, [])
        print(f"  {marker} {p}")
        for s in secs:
            print(f"        → §{s}")
    print()
    if all_sections:
        print("Suggested section headers to refresh:")
        for s in sorted(all_sections):
            print(f"  - §{s}")
    else:
        print("No architecture-relevant changes detected — doc update optional.")
    print()
    print("Reminder: this mapping mirrors docs/ARCHITECTURE.md §附.")
    print("If you add a new pattern here, update §附 (and vice versa).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
