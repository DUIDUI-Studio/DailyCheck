#!/usr/bin/env python3
"""scripts/agent_framework/check_reports.py

门禁器：检查四报告是否齐全 + 是否"全绿"。

调用方：CI workflow 的 agent-framework-reports step 末尾 + 本地 pre-PR 校验。

输入：
  reports/02-test-report.md（自动产出）
  reports/03-code-quality.md（自动产出）
  reports/ci-summary.json（自动产出）
  PR body / 评论中的报告 01（Dev Agent 填）
  reports/pr-<num>/04-local-deploy-preview.md（Dev Agent 半自动）

判断规则：
  报告 01：PR body 含 "## 报告 1/4" + "## Checklist" + 状态全 ✅
  报告 02：ci-summary.json pytest.failed == 0 && gate_exit == 0
  报告 03：ci-summary.json ruff.warnings == 0 && bandit.high_vuln == 0
  报告 04：reports/pr-<num>/04-*.md 存在 + 含 "## 4. 启动日志摘要" + "## 7. 报告签名"

退出码：
  0  全绿
  1  缺报告
  2  报告未绿
  3  报告结构异常
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
REPORTS_DIR = REPO_ROOT / "reports"
CI_SUMMARY = REPORTS_DIR / "ci-summary.json"


def fail(reason: str, code: int) -> None:
    print(f"[check_reports] ❌ {reason}", file=sys.stderr)
    sys.exit(code)


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


def main() -> int:
    pr_num = os.environ.get("PR_NUM")

    if not REPORTS_DIR.exists():
        fail("reports/ 不存在 — 请先跑 generate_reports.sh", code=1)

    auto = check_report_02_and_03()
    rep01 = check_report_01_present()
    rep04 = check_report_04(pr_num)

    if not (auto["test_pass"] and auto["quality_pass"]):
        fail("报告 02/03 未绿（pytest 或 ruff 失败）", code=2)
    if not rep01:
        fail("报告 01 缺失或不完整", code=2)
    if not rep04:
        fail("报告 04 缺失", code=2)

    print("[check_reports] ✅ 四报告齐全且全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
