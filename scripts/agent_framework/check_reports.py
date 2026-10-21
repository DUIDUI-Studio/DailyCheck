#!/usr/bin/env python3
"""scripts/agent_framework/check_reports.py

门禁器：检查四报告是否齐全 + 是否"全绿" + Checklist 是否**有证据**。

调用方：CI workflow 的 agent-framework-reports step 末尾 + 本地 pre-PR 校验。

输入：
  reports/02-test-report.md（自动产出）
  reports/03-code-quality.md（自动产出）
  reports/ci-summary.json（自动产出）
  reports/mutation.json（mutation_check.py 产出）
  PR body / 评论中的报告 01（Dev Agent 填）
  reports/pr-<num>/04-local-deploy-preview.md（Dev Agent 半自动）

判断规则：
  报告 01：PR body 含 "## 报告 1/4" + "## Checklist" + "## 生产侧事项" + 状态全 ✅
  报告 02：ci-summary.json pytest.failed == 0 && gate_exit == 0
  报告 03：ci-summary.json ruff.warnings == 0 && bandit.high_vuln == 0
  报告 04：reports/pr-<num>/04-*.md 存在 + 含 "## 4. 启动日志摘要" + "## 7. 报告签名"
  Checklist：见 build_checklist_table() —— 逐项用真实证据判定，不再采信打勾

退出码：
  0  全绿
  1  缺报告
  2  报告未绿 / Checklist 有硬失败
  3  报告结构异常
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
REPORTS_DIR = REPO_ROOT / "reports"
CI_SUMMARY = REPORTS_DIR / "ci-summary.json"
MUTATION_JSON = REPORTS_DIR / "mutation.json"

# 绝不允许混进 PR 的运行时/产物路径前缀。命中即硬失败——它们一旦进 main，
# 后续每次 clone 都带着，且不可 revert 掉历史。db/ 是 Python 包，单独放行。
FORBIDDEN_PREFIXES = (
    "backups/", "logs/", ".venv/", ".workbuddy/", "graphify-out/",
    "issue-reports/", "__pycache__/", ".pytest_cache/", "node_modules/",
)
FORBIDDEN_SUFFIXES = (".db", ".log", ".pyc", ".sqlite", ".sqlite3")
# 门禁自身的运行时产物。.gitignore 已挡一层，但 `git add -f` 能绕过
# ——所以门禁再挡一层：产物入库会让「本次证据」变成「上次残留」。
FORBIDDEN_EXACT = (
    "reports/mutation.json",
    "reports/ci-summary.json",
)


def fail(reason: str, code: int) -> None:
    print(f"[check_reports] ❌ {reason}", file=sys.stderr)
    sys.exit(code)


def _changed_files() -> set[str] | None:
    """PR 改动文件集。取不到 base/head → None（该维度标「未提供」而非编造）。"""
    base = os.environ.get("BASE_SHA", "").strip()
    head = os.environ.get("HEAD_SHA", "").strip()
    if not base or not head:
        return None
    try:
        out = subprocess.run(
            ["git", "diff", "--name-only", f"{base}...{head}"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return {ln.strip() for ln in out.stdout.splitlines() if ln.strip()}


def check_report_02_and_03() -> dict:
    """报告 02/03 由 CI 自动产出，解析 ci-summary.json。"""
    if not CI_SUMMARY.exists():
        fail(f"missing {CI_SUMMARY} — generate_reports.sh 未运行", code=1)

    try:
        summary = json.loads(CI_SUMMARY.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        fail(f"ci-summary.json malformed: {e}", code=3)

    failed = summary.get("pytest", {}).get("failed", -1)
    gate = summary.get("pytest", {}).get("gate_exit", -1)
    ruff_warn = summary.get("ruff", {}).get("warnings", -1)
    bandit_high_raw = summary.get("bandit", {}).get("high_vuln", -1)
    radon_high_raw = summary.get("radon", {}).get("high_cc", -1)

    # 兼容字符串数字（如 "0"）和整数。None / "N/A" 视为工具未装，跳过该指标。
    def _as_int_or_none(v):
        if v is None or v == "N/A":
            return None
        try:
            return int(v)
        except (ValueError, TypeError):
            return None

    bandit_high = _as_int_or_none(bandit_high_raw)
    radon_high = _as_int_or_none(radon_high_raw)

    print(f"[check_reports] 报告 02: pytest failed={failed}, gate_exit={gate}（全绿标准=gate_exit==0，failed 数仅记录）")
    print(f"[check_reports] 报告 03: ruff warnings={ruff_warn}, bandit high={bandit_high}, radon high={radon_high}")

    # 报告 02 全绿定义：known_failures gate 通过（gate_exit=0）。
    # 理由：DailyCheck 基线 16 failed 是 known_failures 内的，不算"未绿"。
    # "零新增门禁"在 ci.yml 已先验过 pytest 输出比对 known_failures.txt。
    # 报告 03 全绿定义：ruff + bandit 高危都 0（radon 不强求，因 C/D 函数级别仅参考）。
    quality_pass = ruff_warn == 0 and (bandit_high is None or bandit_high == 0)
    return {
        "test_pass": gate == 0,
        "quality_pass": quality_pass,
        "summary": summary,
        "gate": gate,
        "ruff_warn": ruff_warn,
    }


def check_report_01_present() -> bool:
    """报告 01：Dev Agent 在 PR body 必填。

    由 GitHub Actions 通过 ${{ github.event.pull_request.body }} 传入。
    本地运行也可手动指定：PR_BODY_FILE=path/to/pr_body.md
    """
    pr_body = ""
    pr_body_file = os.environ.get("PR_BODY_FILE")
    if pr_body_file and Path(pr_body_file).exists():
        pr_body = Path(pr_body_file).read_text(encoding="utf-8")
    else:
        # 本地运行：从环境变量 PR_BODY 取
        pr_body = os.environ.get("PR_BODY", "")

    if "## 报告 1/4" not in pr_body:
        print("[check_reports] 报告 01: ❌ PR body 缺 '## 报告 1/4' 段落")
        return False

    if "## Checklist" not in pr_body:
        print("[check_reports] 报告 01: ❌ PR body 缺 '## Checklist' 段落")
        return False

    # 生产侧事项（2026-10-15 merge 政策改版新增）：Prod Agent merge 判定的输入，
    # 缺失 = Prod 拿到的 PR 没有部署参考。段落内容不查 ❌（写法铁律见 workflow.md §三′）。
    if "## 生产侧事项" not in pr_body:
        print("[check_reports] 报告 01: ❌ PR body 缺 '## 生产侧事项' 段落")
        return False

    # 检查状态：必须没有 ❌
    if "❌" in pr_body.split("## 报告 1/4")[1].split("## 报告 2/4")[0]:
        print("[check_reports] 报告 01: ❌ 含未消解的 ❌ 状态")
        return False

    print("[check_reports] 报告 01: ✅ PR body 含完整段落")
    return True


def check_report_04(pr_num: str | None) -> bool:
    """报告 04：Dev Agent 半自动产出，本地脚本 generate 后贴路径。"""
    if not pr_num:
        # 本地运行：扫 reports/ 下任意 04-*.md
        candidates = list(REPORTS_DIR.glob("pr-*/04-local-deploy-preview.md"))
        if not candidates:
            print("[check_reports] 报告 04: ❌ reports/pr-*/04-*.md 不存在")
            return False
        report = sorted(candidates)[-1]
    else:
        report = REPORTS_DIR / f"pr-{pr_num}" / "04-local-deploy-preview.md"
        if not report.exists():
            print(f"[check_reports] 报告 04: ❌ {report} 不存在")
            return False

    content = report.read_text(encoding="utf-8")
    for required in ["## 3. 启动日志摘要", "## 7. 报告签名"]:
        if required not in content:
            print(f"[check_reports] 报告 04: ❌ 缺段落 '{required}'")
            return False

    print(f"[check_reports] 报告 04: ✅ {report}")
    return True


# ---------------------------------------------------------------- Checklist


def _is_forbidden(path: str) -> bool:
    if path.startswith("db/"):  # Python 包，不是数据库文件
        return False
    if path in FORBIDDEN_EXACT:
        return True
    if path.startswith(FORBIDDEN_PREFIXES):
        return True
    return path.endswith(FORBIDDEN_SUFFIXES)


def _load_mutation() -> dict | None:
    if not MUTATION_JSON.exists():
        return None
    try:
        return json.loads(MUTATION_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def build_checklist_table(*, changed: set[str] | None,
                          gate: int, ruff_warn: int) -> tuple[str, list[str]]:
    """生成 Checklist 判定表。

    设计原则（2026-10-21）：**不采信打勾，逐项给证据**。
    能机验的必须机验；机验不了的如实标「自述」——把「未被验证的声明」
    从一片绿色里挑出来，比伪造成 ✅ 有价值得多。

    返回 (markdown 表, 硬失败原因列表)。空列表 = 无硬失败。
    """
    rows: list[tuple[str, str, str]] = []
    hard_fail: list[str] = []

    # 1) ruff（ci-summary 真值）
    rows.append((
        "ruff 0 告警（改动 .py）",
        "✅" if ruff_warn == 0 else "❌",
        f"ci-summary ruff.warnings={ruff_warn}",
    ))
    if ruff_warn != 0:
        hard_fail.append(f"ruff 有 {ruff_warn} 条告警")

    # 2) pytest 零新增（gate 真值）
    rows.append((
        "pytest 零新增失败",
        "✅" if gate == 0 else "❌",
        f"check_known_failures gate_exit={gate}",
    ))
    if gate != 0:
        hard_fail.append(f"pytest 零新增门禁未过（gate_exit={gate}）")

    if changed is None:
        rows.append(("未提交运行时文件", "—", "未提供 BASE_SHA/HEAD_SHA，无法比对"))
        rows.append(("架构文档同步", "—", "同上"))
        rows.append(("bash -n 冒烟", "—", "同上"))
    else:
        # 3) 运行时文件（可机验，且必须硬拦：污染仓库不可回滚）
        bad = sorted(p for p in changed if _is_forbidden(p))
        rows.append((
            "未提交运行时文件",
            "✅" if not bad else "❌",
            "diff 无 db/backups/logs/.venv 等" if not bad else f"混入：{', '.join(bad[:5])}",
        ))
        if bad:
            hard_fail.append(f"PR 混入运行时/产物文件：{', '.join(bad[:5])}")

        # 4) 架构文档同步（pre-commit 已强制，CI 做交叉提示；不硬拦避免误伤）
        touches_code = any(
            p.startswith(("scripts/", "tests/")) and p.endswith((".py", ".sh"))
            for p in changed
        )
        arch_ok = "docs/ARCHITECTURE.md" in changed
        if not touches_code:
            rows.append(("架构文档同步", "—", "本 PR 未改 scripts/ tests/ 代码"))
        elif arch_ok:
            rows.append(("架构文档同步", "✅", "同 PR 已改 docs/ARCHITECTURE.md"))
        else:
            rows.append((
                "架构文档同步", "⚠️",
                "改了 scripts/tests 但未见 ARCHITECTURE.md（pre-commit 应已拦）",
            ))

        # 5) bash -n（真跑，不是声明）
        shells = [p for p in changed if p.endswith(".sh") and (REPO_ROOT / p).exists()]
        if not shells:
            rows.append(("bash -n 冒烟", "—", "本 PR 未改 shell 脚本"))
        else:
            broken = []
            for s in shells:
                proc = subprocess.run(
                    ["bash", "-n", s], cwd=REPO_ROOT,
                    capture_output=True, text=True,
                )
                if proc.returncode != 0:
                    broken.append(s)
            rows.append((
                "bash -n 冒烟",
                "✅" if not broken else "❌",
                f"校验 {len(shells)} 个脚本" if not broken else f"语法错：{', '.join(broken)}",
            ))
            if broken:
                hard_fail.append(f"shell 语法错误：{', '.join(broken)}")

    # 6) 变异验证（mutation_check.py 产出；无产物 ≠ 已验）
    mut = _load_mutation()
    declared = (mut or {}).get("declared", 0)
    touches_logic = changed is None or any(
        p.startswith("scripts/") and p.endswith(".py") for p in changed
    )
    if not declared:
        # 「没产物」与「有产物但声明为空」同等看待：都是没验。
        # 改了 scripts/*.py 却无声明 → ⚠️（显眼但不阻断，防为过门禁编造变异）。
        rows.append((
            "变异验证",
            "⚠️" if touches_logic else "—",
            "未声明（改了 scripts/*.py 但 reports/mutation.json 无有效声明）"
            if touches_logic else "未声明（无逻辑代码变更）",
        ))
    else:
        verified = mut.get("verified", 0)
        uncovered = mut.get("uncovered", 0)
        errored = mut.get("errored", 0)
        ok = uncovered == 0 and errored == 0 and verified == mut.get("declared", 0)
        rows.append((
            "变异验证",
            "✅" if ok else "❌",
            f"声明 {mut.get('declared')} 条，看守住 {verified}，"
            f"没看守住 {uncovered}，声明有误 {errored}",
        ))
        if uncovered:
            hard_fail.append(f"{uncovered} 条变异注入后测试仍绿（假绿）")
        if errored:
            hard_fail.append(f"{errored} 条变异声明有误")

    lines = ["| Checklist 项 | 判定 | 证据 |", "|---|---|---|"]
    lines += [f"| {a} | {b} | {c} |" for a, b, c in rows]
    return "\n".join(lines), hard_fail


def main() -> int:
    pr_num = os.environ.get("PR_NUM")

    if not REPORTS_DIR.exists():
        fail("reports/ 不存在 — 请先跑 generate_reports.sh", code=1)

    auto = check_report_02_and_03()
    rep01 = check_report_01_present()
    rep04 = check_report_04(pr_num)

    table, hard_fail = build_checklist_table(
        changed=_changed_files(),
        gate=auto["gate"],
        ruff_warn=auto["ruff_warn"],
    )
    print("\n[check_reports] Checklist 机验结果（不采信打勾，逐项给证据）：")
    print(table)
    print()

    if not (auto["test_pass"] and auto["quality_pass"]):
        fail("报告 02/03 未绿（pytest 或 ruff 失败）", code=2)
    if not rep01:
        fail("报告 01 缺失或不完整", code=2)
    if not rep04:
        fail("报告 04 缺失", code=2)
    if hard_fail:
        fail("Checklist 硬失败：" + "；".join(hard_fail), code=2)

    # 未声明变异验证不阻断（避免为了过门禁编造变异），但必须显眼：
    # Prod Agent 的 merge 判定要读这一行，而不是只看到「全绿」。
    if "| 变异验证 | ⚠️" in table:
        print("[check_reports] 注意：本 PR 改了 scripts/*.py 但未声明变异验证。"
              "Prod Agent merge 判定时请确认该 PR 是否真的没有可变异的关键断言。")

    print("[check_reports] ✅ 四报告齐全且全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
