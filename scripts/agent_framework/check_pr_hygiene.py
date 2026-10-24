#!/usr/bin/env python3
"""PR 开工规范机验：分支命名、issue 关联、落后度。

背景
----
Agent 接手一个 issue 之后「怎么开工」此前**没有任何仓库内约束**。
`workflow.md` §二 只有一句「确认分支、确认改动范围」，没给判据 —— 于是这些
规矩实际只活在 Dev Agent 的本地记忆里，换 session、换 agent 就丢。

2026-10-24 一天内踩到的实证：

- worktree 的 upstream 指向已废弃的 `github-legacy/main`，无参数 `git push`
  把提交推到废弃远端（该 worktree 的 upstream 是 origin/main 而非预期分支）
- 分支落后 main → 两点 diff 把别人合入的改动算进本 PR，CI 红在自己没碰过的
  文件上（Issue #24）
- PR #22 把「红线机验 + CI diff 修复 + 文档三处」混成一个 PR，合并后无法单独
  回滚，而 Prod Agent 提的两条缺陷正好藏在里面（Issue #26）

本脚本把其中最硬的三条变成判据：

- H1 分支前缀合法（`fix/` `feat/` … 之外的名字会让分支列表没法按类筛）
- H2 分支名里的 issue 号必须与 body 的关联声明一致（`Closes #N` / `Part-Of: #N`）
- H3 不带 issue 号的分支必须在 body 声明 `No-Issue: <理由>`，不许悄悄绕过
- H4 落后 base 的提交数：只提示不拦 —— 落后不是错误，真实造成问题的是冲突

刻意不机验的（写进 `workflow.md` §二②′，靠 review）
--------------------------------------------------
- 开工前是否真的复现了问题（无法自动判断「复现」的真伪）
- 是否认领了 issue、有没有别人在做（需要读 issue 评论，判据不成立）
- PR 颗粒度是否真的「一个可独立回滚单元」（语义判断）
- worktree 是否从 origin/main 起、upstream 是否指对（CI 里看不到本地工作树）

用法
----
CI（由 ci.yml 的 agent-framework-reports job 调用）::

    python scripts/agent_framework/check_pr_hygiene.py \\
        --branch "$GITHUB_HEAD_REF" --body-file /tmp/pr/body.md

本地自检::

    python scripts/agent_framework/check_pr_hygiene.py \\
        --branch "$(git rev-parse --abbrev-ref HEAD)" --body-file /tmp/body.md
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# H1：分支前缀白名单。与 workflow.md §二②′ 的分支命名表逐条对应。
BRANCH_PREFIXES = (
    "fix/",
    "feat/",
    "chore/",
    "docs/",
    "ci/",
    "test/",
    "refactor/",
    "perf/",
)

# 分支名里的 issue 号：issue-26 / issue_26 / issue26 / issue 26 都认。
BRANCH_ISSUE_RE = re.compile(r"issue[-_ ]?(\d+)", re.I)

# body 里的关联声明。Closes 表示本 PR 收口该 issue；Part-Of 表示只是其中一部分。
CLOSES_RE = re.compile(r"\b(?:closes|fixes|resolves)\s+#(\d+)\b", re.I)
PART_OF_RE = re.compile(r"\bpart[-_ ]of\s*:?\s*#(\d+)\b", re.I)

# H3 的显式豁免声明。字段名固定，值自由。
NO_ISSUE_FIELD = "No-Issue:"

# H4 的提示阈值：落后超过这么多提交才提醒 rebase，且仍然 exit 0。
BEHIND_WARN_THRESHOLD = 30


@dataclass(frozen=True)
class Finding:
    """一条开工规范命中记录。"""

    rule: str
    detail: str
    evidence: str

    def render(self) -> str:
        return f"  [{self.rule}] {self.detail} —— {self.evidence}"


def branch_issue_number(branch: str) -> int | None:
    """从分支名里取 issue 号；取不到返回 None。"""
    matched = BRANCH_ISSUE_RE.search(branch)
    if not matched:
        return None
    return int(matched.group(1))


def has_no_issue_declaration(pr_body: str) -> str:
    """返回 `No-Issue:` 那一行（已 strip）；没有则空串。"""
    for line in pr_body.splitlines():
        stripped = line.strip().lstrip("-*> \t").strip()
        if stripped.startswith(NO_ISSUE_FIELD):
            value = stripped[len(NO_ISSUE_FIELD):].strip()
            if value:
                return stripped
    return ""


def linked_issue_numbers(pr_body: str) -> set[int]:
    """body 里声明关联的全部 issue 号（Closes 与 Part-Of 合并）。"""
    numbers = {int(n) for n in CLOSES_RE.findall(pr_body)}
    numbers |= {int(n) for n in PART_OF_RE.findall(pr_body)}
    return numbers


def classify(branch: str, pr_body: str) -> list[Finding]:
    """纯函数：给定分支名与 PR 正文，算出违反的开工规范条目。"""
    findings: list[Finding] = []
    branch = branch.strip()

    # H1：前缀白名单
    if not branch.startswith(BRANCH_PREFIXES):
        allowed = " ".join(BRANCH_PREFIXES)
        findings.append(
            Finding(
                rule="H1",
                detail="分支前缀不在白名单内",
                evidence=f"{branch}（允许：{allowed}）",
            )
        )

    issue_no = branch_issue_number(branch)
    linked = linked_issue_numbers(pr_body)

    if issue_no is not None:
        # H2：分支号必须与 body 的关联声明一致
        if issue_no not in linked:
            hint = f"Closes #{issue_no} 或 Part-Of: #{issue_no}"
            findings.append(
                Finding(
                    rule="H2",
                    detail="分支带 issue 号，但 body 未声明同一个号",
                    evidence=f"分支={branch} 期望 {hint}，实际声明={sorted(linked) or '无'}",
                )
            )
    else:
        # H3：无号分支必须明说原因，不许默认绕过
        if not has_no_issue_declaration(pr_body):
            findings.append(
                Finding(
                    rule="H3",
                    detail="分支不带 issue 号，且 body 未声明 No-Issue 理由",
                    evidence=f"分支={branch}",
                )
            )

    return findings


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


def commits_behind(base: str, head: str) -> int:
    """head 落后 base 多少个提交（base 有而 head 没有的）。

    只用于提示。落后本身不是错误 —— 分支开了几天、main 前进了是常态；
    真正造成事故的是「落后带来的两点 diff 误报」和「合并冲突」，那两条由
    `check_redlines.py` 的三点 diff 与 CI 的冲突检测分别兜住。
    """
    out = _git("rev-list", "--count", f"{head}..{base}")
    return int(out.strip() or 0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PR 开工规范机验")
    parser.add_argument(
        "--branch",
        default=os.environ.get("GITHUB_HEAD_REF") or os.environ.get("PR_BRANCH", ""),
        help="PR 源分支名（默认取 $GITHUB_HEAD_REF / $PR_BRANCH）",
    )
    parser.add_argument(
        "--body-file",
        default=os.environ.get("PR_BODY_FILE", ""),
        help="PR 正文文件路径（默认取 $PR_BODY_FILE）",
    )
    parser.add_argument(
        "--base",
        default=os.environ.get("BASE_SHA", ""),
        help="基线 commit（默认取 $BASE_SHA，缺省回退 origin/main）",
    )
    parser.add_argument("--head", default="HEAD", help="目标 commit（默认 HEAD）")
    args = parser.parse_args(argv)

    branch = args.branch.strip()
    if not branch:
        # 拿不到分支名不能静默放行 —— 放行等于门禁失效
        print("[check_pr_hygiene] 无法确定分支名：传 --branch 或设 $GITHUB_HEAD_REF")
        return 2

    if args.body_file and Path(args.body_file).exists():
        pr_body = Path(args.body_file).read_text(encoding="utf-8")
    else:
        pr_body = os.environ.get("PR_BODY", "")

    findings = classify(branch, pr_body)

    # H4 只提示，不参与判定
    base = args.base.strip() or "origin/main"
    behind_note = ""
    try:
        behind = commits_behind(base, args.head)
        if behind > BEHIND_WARN_THRESHOLD:
            behind_note = (
                f"[check_pr_hygiene] 提示：落后 {base[:8]} {behind} 个提交，"
                "建议 merge 一次以减少冲突与 diff 误判（不拦截）"
            )
    except RuntimeError:
        # 落后度算不出来不影响硬判据，只丢提示
        behind = -1

    if not findings:
        print(f"[check_pr_hygiene] 通过：分支 {branch}")
        if behind_note:
            print(behind_note)
        return 0

    print(f"[check_pr_hygiene] 命中 {len(findings)} 条开工规范：")
    for finding in findings:
        print(finding.render())
    print("")
    print("[check_pr_hygiene] 判据见 docs/agent-framework/workflow.md §二②′。")
    print(f"  无关联 issue 时在 body 加一行：{NO_ISSUE_FIELD} <为什么这个 PR 没有对应 issue>")
    if behind_note:
        print(behind_note)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
