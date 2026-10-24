"""PR 模板必须自身通过报告 01 门禁。

背景（2026-10-24 实测）：模板开头的 HTML 注释里写了门禁标题的字面文本与
未消解记号，而 `check_report_01_present` 是按字面 split 切段的 —— 注释里的
标题把切点提前，注释里的记号随后被算进被检段落，导致**每个用模板建的 PR
一开始就是红的**，且改代码无用（要改正文）。这个文件把「模板自身合规」钉死。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"
CHECKER = ROOT / "scripts" / "agent_framework" / "check_reports.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_reports_under_test", CHECKER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def template_body() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def test_template_exists_and_has_three_required_sections(template_body):
    assert "## 报告 1/4" in template_body
    assert "## Checklist" in template_body
    assert "## 生产侧事项" in template_body


def test_template_passes_report01_gate(monkeypatch, template_body):
    """模板原样作为 PR body 时必须 PASS —— 这是「新 PR 初始不红」的前提。"""
    monkeypatch.setenv("PR_BODY", template_body)
    monkeypatch.delenv("PR_BODY_FILE", raising=False)
    assert _load_checker().check_report_01_present() is True


def test_template_has_no_unsolved_marker_inside_checked_segment(template_body):
    """被检段落里不得出现未消解记号（与门禁同一口径，报错更直白）。"""
    segment = template_body.split("## 报告 1/4")[1].split("## 报告 2/4")[0]
    assert "❌" not in segment, (
        "「报告 1/4」段内出现未消解记号，门禁会判红；"
        "占位状态请用「⬜ 待填」"
    )
