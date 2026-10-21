#!/usr/bin/env python3
"""门禁证据化改造的回归网（2026-10-21）。

覆盖三件事，都是「把自述变成证据」：
  1. mutation_check  —— 变异声明真的被注入、测试真的变红、文件真的还原
  2. Checklist 机验  —— 判定来自 ci-summary/diff/mutation.json，不是打勾
  3. known_failures  —— 红线 1「只许收缩」有了执行器

最要紧的两条安全属性（漏了会静默坏掉）：
  - **还原必须哈希一致**：变异后没还原 = 工作树被污染，后续所有结论失效
  - **测试仍绿必须判 uncovered**：否则门禁恒绿，等于没做
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from scripts import check_known_failures_shrink as shrink
from scripts.agent_framework import check_reports, mutation_check

# ------------------------------------------------------------ mutation_check


def _write_repo(tmp: Path, *, mod_body: str, test_body: str) -> Path:
    """最小可测仓库：一个模块 + 一个测试文件。"""
    (tmp / "mod.py").write_text(mod_body, encoding="utf-8")
    (tmp / "test_mod.py").write_text(test_body, encoding="utf-8")
    return tmp


MOD = "def f():\n    return 1\n\n\ndef g():\n    return 5\n"
TEST = "from mod import f\n\n\ndef test_f():\n    assert f() == 1\n"


def _mut(**over) -> dict:
    base = {
        "id": "M1", "file": "mod.py",
        "find": "    return 1", "replace": "    return 2",
        "test": "test_mod.py::test_f",
    }
    base.update(over)
    return base


def test_parse_declarations_empty_when_section_absent():
    assert mutation_check.parse_declarations("# 无关内容\n") == []


def test_parse_declarations_rejects_missing_json_block():
    with pytest.raises(SystemExit) as e:
        mutation_check.parse_declarations("## 变异验证\n\n没有代码块\n")
    assert e.value.code == 2


def test_parse_declarations_rejects_bad_json():
    with pytest.raises(SystemExit) as e:
        mutation_check.parse_declarations("## 变异验证\n\n```json\n[{bad}]\n```\n")
    assert e.value.code == 2


def test_parse_declarations_rejects_missing_field():
    body = '## 变异验证\n\n```json\n[{"id": "M1", "file": "mod.py"}]\n```\n'
    with pytest.raises(SystemExit) as e:
        mutation_check.parse_declarations(body)
    assert e.value.code == 2


def test_parse_declarations_only_accepts_expect_fail():
    """expect=pass（等价变异）没有看守价值，不给入口。"""
    body = (
        '## 变异验证\n\n```json\n[{"id":"M1","file":"mod.py","find":"a",'
        '"replace":"b","test":"t.py::t","expect":"pass"}]\n```\n'
    )
    with pytest.raises(SystemExit) as e:
        mutation_check.parse_declarations(body)
    assert e.value.code == 2


def test_run_one_verifies_when_test_actually_breaks(tmp_path, monkeypatch):
    _write_repo(tmp_path, mod_body=MOD, test_body=TEST)
    monkeypatch.setattr(mutation_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mutation_check, "changed_files", lambda: {"mod.py"})

    r = mutation_check.run_one(_mut(), {"mod.py"})
    assert r["outcome"] == "verified"
    assert r["returncode"] == 1
    # 还原检查：原文件必须一字不差
    assert (tmp_path / "mod.py").read_text(encoding="utf-8") == MOD


def test_run_one_reports_uncovered_when_test_stays_green(tmp_path, monkeypatch):
    """核心：注入变异后测试仍绿 = 该测试没看守住，必须判 uncovered。

    这是门禁唯一有意义的时刻——若这里恒判 verified，整个机制就是装饰品。
    """
    _write_repo(tmp_path, mod_body=MOD, test_body=TEST)
    monkeypatch.setattr(mutation_check, "REPO_ROOT", tmp_path)

    # 变异 g()，但没有任何测试覆盖 g()
    r = mutation_check.run_one(
        _mut(id="M2", find="    return 5", replace="    return 6"), {"mod.py"})
    assert r["outcome"] == "uncovered"
    assert r["returncode"] == 0
    assert "没看守住" in r["note"]
    assert (tmp_path / "mod.py").read_text(encoding="utf-8") == MOD


def test_run_one_rejects_find_not_unique(tmp_path, monkeypatch):
    """find 出现多次 = 声明有歧义，必须拒绝而不是随便替换第一个。"""
    _write_repo(
        tmp_path,
        mod_body="def f():\n    return 1\n\n\ndef h():\n    return 1\n",
        test_body=TEST,
    )
    monkeypatch.setattr(mutation_check, "REPO_ROOT", tmp_path)
    r = mutation_check.run_one(_mut(), {"mod.py"})
    assert r["outcome"] == "error"
    assert "2 次" in r["note"]


def test_run_one_rejects_file_outside_pr_diff(tmp_path, monkeypatch):
    """变异点必须落在本 PR 改动文件内——防止挑无关文件凑数。"""
    _write_repo(tmp_path, mod_body=MOD, test_body=TEST)
    monkeypatch.setattr(mutation_check, "REPO_ROOT", tmp_path)
    r = mutation_check.run_one(_mut(), {"some_other.py"})
    assert r["outcome"] == "error"
    assert "改动范围" in r["note"]


def test_run_one_restores_even_when_declaration_is_invalid(tmp_path, monkeypatch):
    """还原不依赖验证成功与否：声明错了也不能把文件留在变异态。"""
    _write_repo(tmp_path, mod_body=MOD, test_body=TEST)
    monkeypatch.setattr(mutation_check, "REPO_ROOT", tmp_path)
    mutation_check.run_one(_mut(find="不存在的代码"), {"mod.py"})
    assert (tmp_path / "mod.py").read_text(encoding="utf-8") == MOD


# --------------------------------------------------- Checklist 机验（证据表）


def test_is_forbidden_blocks_runtime_artifacts():
    assert check_reports._is_forbidden("backups/x.tar.gz")
    assert check_reports._is_forbidden("logs/app.log")
    assert check_reports._is_forbidden("data/master.db")
    assert check_reports._is_forbidden("scripts/__pycache__/a.pyc")
    assert check_reports._is_forbidden(".workbuddy/memory/x.md")


def test_is_forbidden_allows_db_package_and_normal_code():
    """db/ 是 Python 包，不能因为前缀像数据库就拦下。"""
    assert not check_reports._is_forbidden("db/models.py")
    assert not check_reports._is_forbidden("scripts/app.py")
    assert not check_reports._is_forbidden("docs/ARCHITECTURE.md")


def test_is_forbidden_blocks_gate_artifacts():
    """产物入库会让「本次证据」变成「上次残留」，必须直接拦。

    .gitignore 挡不住 `git add -f`，所以门禁要再挡一层。
    """
    assert check_reports._is_forbidden("reports/mutation.json")
    assert check_reports._is_forbidden("reports/ci-summary.json")


def test_checklist_marks_unknown_without_diff(tmp_path, monkeypatch):
    """拿不到 diff 时标「—」，不编造「✅ 无运行时文件」。"""
    monkeypatch.setattr(check_reports, "MUTATION_JSON", tmp_path / "absent.json")
    table, hard = check_reports.build_checklist_table(
        changed=None, gate=0, ruff_warn=0)
    assert "未提供 BASE_SHA" in table
    assert hard == []


def test_checklist_fails_on_runtime_file_in_pr():
    table, hard = check_reports.build_checklist_table(
        changed={"scripts/app.py", "backups/secret.tar.gz"}, gate=0, ruff_warn=0)
    assert "❌" in table
    assert any("运行时" in h for h in hard)


def test_checklist_runs_bash_syntax_check_for_real(tmp_path, monkeypatch):
    """bash -n 是真跑，不是读声明。"""
    bad = tmp_path / "broken.sh"
    bad.write_text("if [ 1 -eq 1 ]; then\n  echo hi\n", encoding="utf-8")
    monkeypatch.setattr(check_reports, "REPO_ROOT", tmp_path)
    table, hard = check_reports.build_checklist_table(
        changed={"broken.sh"}, gate=0, ruff_warn=0)
    assert "bash -n" in table
    assert any("shell 语法错误" in h for h in hard)


def test_checklist_warns_but_does_not_block_arch_sync(tmp_path, monkeypatch):
    """架构同步由 pre-commit 强制，CI 只做交叉提示——不硬拦，避免误伤。"""
    monkeypatch.setattr(check_reports, "MUTATION_JSON", tmp_path / "absent.json")
    table, hard = check_reports.build_checklist_table(
        changed={"scripts/app.py"}, gate=0, ruff_warn=0)
    assert "| 架构文档同步 | ⚠️" in table
    assert hard == []


def test_checklist_reads_mutation_artifact(tmp_path, monkeypatch):
    """有 uncovered 必须硬失败——这就是变异验证的价值所在。"""
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "mutation.json").write_text(json.dumps({
        "declared": 2, "verified": 1, "uncovered": 1, "errored": 0,
        "results": [],
    }), encoding="utf-8")
    monkeypatch.setattr(check_reports, "MUTATION_JSON", reports / "mutation.json")

    table, hard = check_reports.build_checklist_table(
        changed={"scripts/app.py", "docs/ARCHITECTURE.md"}, gate=0, ruff_warn=0)
    assert "没看守住 1" in table
    assert any("假绿" in h for h in hard)


def test_checklist_warns_when_logic_changed_without_mutation(tmp_path, monkeypatch):
    """改了 scripts 却不声明变异：不阻断（防编造），但必须显眼。"""
    monkeypatch.setattr(check_reports, "MUTATION_JSON", tmp_path / "nope.json")
    table, hard = check_reports.build_checklist_table(
        changed={"scripts/app.py", "docs/ARCHITECTURE.md"}, gate=0, ruff_warn=0)
    assert "| 变异验证 | ⚠️" in table
    assert hard == []


def test_checklist_clean_when_all_evidence_green(tmp_path, monkeypatch):
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "mutation.json").write_text(json.dumps({
        "declared": 1, "verified": 1, "uncovered": 0, "errored": 0,
        "results": [],
    }), encoding="utf-8")
    monkeypatch.setattr(check_reports, "MUTATION_JSON", reports / "mutation.json")

    table, hard = check_reports.build_checklist_table(
        changed={"scripts/app.py", "docs/ARCHITECTURE.md"}, gate=0, ruff_warn=0)
    assert hard == []
    assert "| 变异验证 | ✅" in table


# ------------------------------------------- known_failures「只许收缩」守卫


def _setup_known(tmp_path, monkeypatch, *, current: str, base: str | None):
    known = tmp_path / "known_failures.txt"
    known.write_text(current, encoding="utf-8")
    monkeypatch.setattr(shrink, "KNOWN_FILE", known)
    monkeypatch.setattr(shrink, "read_at_base", lambda _b: base)
    monkeypatch.setattr(sys, "argv", ["x", "--base", "deadbeef"])


def test_shrink_allows_unchanged_list(tmp_path, monkeypatch):
    content = "# 注释\ntests/a.py::t1\ntests/b.py::t2\n"
    _setup_known(tmp_path, monkeypatch, current=content, base=content)
    assert shrink.main() == 0


def test_shrink_blocks_expansion(tmp_path, monkeypatch):
    base = "# 注释\ntests/a.py::t1\n"
    current = base + "tests/new.py::t_new\n"
    _setup_known(tmp_path, monkeypatch, current=current, base=base)
    assert shrink.main() == 1


def test_shrink_allows_contraction(tmp_path, monkeypatch):
    base = "tests/a.py::t1\ntests/b.py::t2\n"
    current = "tests/a.py::t1\n"
    _setup_known(tmp_path, monkeypatch, current=current, base=base)
    assert shrink.main() == 0


def test_shrink_skips_when_base_unreadable(tmp_path, monkeypatch):
    """读不到基线时明说「无法比对」，不放过也不误杀。"""
    _setup_known(tmp_path, monkeypatch, current="tests/a.py::t1\n", base=None)
    assert shrink.main() == 0
