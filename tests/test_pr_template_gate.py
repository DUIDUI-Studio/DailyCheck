"""PR 模板与报告 01 门禁的契约。

机制实测（2026-10-24，务必读准，别第二次修错地方）：
`check_report_01_present` 判定的是 `pr_body.split("## 报告 1/4")[1].split("## 报告 2/4")[0]`。
`split` 会在**每一处**匹配上切开，所以 body 里出现标题字面几次就切几段：

- 只有正文那一处标题 → `[1]` = 真标题之后到文末 → **检查到 R 表**（正常）
- 注释里也写了一次标题 → `[1]` = 两处标题之间的注释残余很短一段，
  R 表整段被挤到 `[2]` → **R 表完全不被检查**，且注释残余里若带红叉就恒红、
  作者改 R 表无法自救。旧模板「一建 PR 就红且改代码无用」与 PR #10 之后
  「R 表一个字不填也绿」是同一个 bug 的两种表现。

修法（owner 2026-10-24 拍板 A）：**注释里不留标题字面，R 表占位恢复红叉，
门禁判定逻辑零改动**。PR #10 的错在于去改占位符字符，等于让门禁对未消解失明。

本文件把三条契约钉死：
  - 模板原样必须是红的（R 表未填 = 红，这是门禁存在的意义）
  - 把 R 表占位全部消解后必须能转绿（证明红是可自救的）
  - 模板注释里不许出现标题字面（否则 R 表逃检）
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"
CHECKER = ROOT / "scripts" / "agent_framework" / "check_reports.py"
SECTION_HEADING = "## 报告 1/4"

UNRESOLVED = "❌ 待填"
RESOLVED = "✅ 已确认"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_reports_under_test", CHECKER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _gate(monkeypatch, body: str) -> bool:
    monkeypatch.setenv("PR_BODY", body)
    monkeypatch.delenv("PR_BODY_FILE", raising=False)
    return _load_checker().check_report_01_present()


@pytest.fixture()
def template_body() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def test_template_exists_and_has_three_required_sections(template_body):
    assert SECTION_HEADING in template_body
    assert "## Checklist" in template_body
    assert "## 生产侧事项" in template_body


def test_template_heading_appears_exactly_once(template_body):
    """标题只能出现一次：出现两次会把被检段切成注释残余，R 表逃检。"""
    assert template_body.count(SECTION_HEADING) == 1, (
        f"「{SECTION_HEADING}」在模板里出现了 {template_body.count(SECTION_HEADING)} 次；"
        "注释里也不能写这串字面，否则 split 后门禁只看注释残余、R 表整体逃检"
    )


def test_template_placeholder_is_marker_the_gate_understands(template_body):
    """R 表占位必须是红叉：换成门禁不认识的字符 = 门禁对未消解状态失明。"""
    assert UNRESOLVED in template_body, (
        f"R 表占位须为「{UNRESOLVED}」，否则 check_report_01_present 不再判红"
    )


def test_template_as_is_is_red(monkeypatch, template_body):
    """模板原样 = R 表未填 = 必须红。这是门禁的立身之本，不是缺陷。"""
    assert _gate(monkeypatch, template_body) is False


def test_template_turns_green_after_resolving_placeholders(monkeypatch, template_body):
    """消解 R 表后必须能转绿 —— 把红叉全换成对勾，门禁就得放行。"""
    resolved = template_body.replace(UNRESOLVED, RESOLVED)
    assert "❌" not in resolved, "消解后模板内不应再有红叉"
    assert _gate(monkeypatch, resolved) is True

