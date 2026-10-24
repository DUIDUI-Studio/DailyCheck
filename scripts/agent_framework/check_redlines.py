#!/usr/bin/env python3
"""红线路径机验：PR 改动命中红线时必须带 owner 授权声明。

背景
----
`docs/agent-framework/workflow.md` §四 列了 6 条红线，规定这些 PR 必须上报
owner 拍板、Prod Agent 不得自行 merge。但此前这套规则**全靠自觉**：

- `check_reports.py` 只校验 PR body 格式，完全不看改动文件路径
- 2026-10-24 实测发现 PR #13 改了 `.github/workflows/ci.yml`（红线 2），
  被 Prod Agent 自行合并，没有上报、没有 PR 评论打回

本脚本把「这个 PR 改了什么」变成可机验的判据：命中红线路径 → 必须有授权
声明，否则 exit 1，CI 变红，Prod Agent 无法自动合并。

豁免约定
--------
PR body 里出现下面这一行（字段名固定，值自由）即视为已授权：

    Owner-Authorization: 2026-10-24 Eric「同意改 CI workflow」

这是**自述**，无法证伪 —— 但它的价值在于三点：写进 PR 就留了审计痕迹；
Prod Agent 不会主动伪造（伪造本身就是明面上的越线）；owner 事后能一眼看出
哪条授权被引用过。

不纳入机验的红线
----------------
- 红线 4「删除或弱化 ARCHITECTURE.md 铁律」：该文件每次子系统改动都要同步，
  按路径拦会制造大量噪音，反而训练出习惯性绕过。这条继续靠 review。
- 红线 3 的"ruleset 本体"在 Gitea 服务端，仓库里没有文件可查；这里只能拦
  `docs/agent-framework/branch-protection.md` 这份描述文档的改动。

用法
----
CI（由 ci.yml 的 agent-framework-reports job 调用）::

    BASE_SHA=<base sha> python scripts/agent_framework/check_redlines.py \\
        --body-file /tmp/pr/body.md

本地自检（默认对比 origin/main）::

    python scripts/agent_framework/check_redlines.py --body-file /tmp/body.md
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# 红线路径规则：rule -> (匹配方式, 模式, 人话说明)
# 匹配方式 "prefix" = 路径前缀；"exact" = 完全相等。
REDLINE_PATHS: tuple[tuple[str, str, str, str], ...] = (
    ("R2", "prefix", ".github/workflows/", "改 CI / 部署流水线自身"),
    ("R3", "exact", "docs/agent-framework/branch-protection.md", "改分支保护规则描述"),
    ("R6", "exact", "docs/agent-framework/workflow.md", "改本工作流文档 / merge 政策"),
    ("R6", "exact", "docs/agent-framework/session-bootstrap.md", "改 agent 常驻指令"),
)

# 红线 1 只拦「扩张」，收缩是允许的（清单只许收缩不许扩张）。
KNOWN_FAILURES_PATH = "tests/known_failures.txt"

# 红线 5：owner 在需求里标注「需拍板」的变更。
PENDING_MARKERS = ("需拍板", "需 owner 拍板", "needs-owner")

AUTH_FIELD = "Owner-Authorization:"


@dataclass(frozen=True)
class Hit:
    """一条红线命中记录。"""

    rule: str
    detail: str
    evidence: str

    def render(self) -> str:
        return f"  [{self.rule}] {self.detail} —— {self.evidence}"


def matches_redline_path(path: str) -> tuple[str, str] | None:
    """路径命中红线返回 (rule, 说明)，否则 None。"""
    normalized = path.strip()
    # 只剥前缀 "./"，不能用 lstrip("./") —— 那会把 ".github/..." 的点一起吃掉
    if normalized.startswith("./"):
        normalized = normalized[2:]
    for rule, mode, pattern, detail in REDLINE_PATHS:
        if mode == "prefix" and normalized.startswith(pattern):
            return rule, detail
        if mode == "exact" and normalized == pattern:
            return rule, detail
    return None


def has_authorization(pr_body: str) -> str:
    """返回授权声明那一行（已 strip）；没有则空串。"""
    for line in pr_body.splitlines():
        stripped = line.strip().lstrip("-*> \t").strip()
        if stripped.startswith(AUTH_FIELD):
            value = stripped[len(AUTH_FIELD):].strip()
            if value:
                return stripped
    return ""


def classify(
    changed_files: list[str],
    known_failures_added: int,
    pr_body: str,
) -> list[Hit]:
    """纯函数：给定改动集合，算出命中的红线条目。

    Args:
        changed_files: 相对仓库根的改动路径列表。
        known_failures_added: `tests/known_failures.txt` 的新增行数（净增才算扩张）。
        pr_body: PR 正文原文。
    """
    hits: list[Hit] = []

    # 红线 1：known_failures.txt 扩张
    if known_failures_added > 0:
        hits.append(
            Hit(
                rule="R1",
                detail="扩张 tests/known_failures.txt（清单只许收缩）",
                evidence=f"新增 {known_failures_added} 行",
            )
        )

    # 红线 2 / 3 / 6：按路径
    for path in changed_files:
        matched = matches_redline_path(path)
        if matched:
            rule, detail = matched
            hits.append(Hit(rule=rule, detail=detail, evidence=path))

    # 红线 5：正文自述「需拍板」却没有任何授权声明
    lowered = pr_body.lower()
    if any(marker in pr_body or marker in lowered for marker in PENDING_MARKERS):
        if not has_authorization(pr_body):
            hits.append(
                Hit(
                    rule="R5",
                    detail="PR 自述「需拍板」但未见 owner 授权声明",
                    evidence="正文含需拍板标记",
                )
            )

    return hits


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{result.stderr.strip()}")
    return result.stdout


def changed_files_between(base: str, head: str) -> list[str]:
    """基线到 HEAD 之间改动的文件（含新增/修改/删除/重命名）。"""
    output = _git("diff", "--name-only", f"{base}..{head}")
    return [line.strip() for line in output.splitlines() if line.strip()]


def known_failures_added_between(base: str, head: str) -> int:
    """known_failures.txt 的新增行数；文件没动或只删行则返回 0。"""
    out = _git("diff", "--numstat", f"{base}..{head}", "--", KNOWN_FAILURES_PATH)
    if not out.strip():
        return 0
    # numstat 格式：added<TAB>deleted<TAB>path（二进制为 "-"）
    first = out.splitlines()[0].split("\t")[0]
    try:
        return int(first)
    except ValueError:
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="红线路径机验")
    parser.add_argument(
        "--base",
        default=os.environ.get("BASE_SHA", ""),
        help="基线 commit（默认取 $BASE_SHA，缺省回退 origin/main）",
    )
    parser.add_argument("--head", default="HEAD", help="目标 commit（默认 HEAD）")
    parser.add_argument(
        "--body-file",
        default=os.environ.get("PR_BODY_FILE", ""),
        help="PR 正文文件路径（默认取 $PR_BODY_FILE）",
    )
    args = parser.parse_args(argv)

    base = args.base.strip()
    if not base:
        base = "origin/main"

    if args.body_file and Path(args.body_file).exists():
        pr_body = Path(args.body_file).read_text(encoding="utf-8")
    else:
        pr_body = os.environ.get("PR_BODY", "")

    try:
        files = changed_files_between(base, args.head)
        added = known_failures_added_between(base, args.head)
    except RuntimeError as exc:
        # 拿不到 diff 不能静默放行 —— 放行等于门禁失效
        print(f"[check_redlines] ❌ 无法计算改动集合：{exc}")
        return 2

    hits = classify(files, added, pr_body)
    if not hits:
        print(f"[check_redlines] ✅ 未命中红线（base={base[:8]}, 改动 {len(files)} 个文件）")
        return 0

    auth = has_authorization(pr_body)
    print(f"[check_redlines] 命中 {len(hits)} 条红线：")
    for hit in hits:
        print(hit.render())

    if auth:
        print(f"[check_redlines] ✅ 已声明授权：{auth}")
        return 0

    print("")
    print("[check_redlines] ❌ 命中红线但未见 owner 授权。")
    print("  按 docs/agent-framework/workflow.md §四，这些变更 Prod Agent 不得自行 merge。")
    print(f"  若已获授权，请在 PR body 加一行：{AUTH_FIELD} <日期> <谁> <授权内容>")
    return 1


if __name__ == "__main__":
    sys.exit(main())
