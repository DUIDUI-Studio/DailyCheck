#!/usr/bin/env python3
"""不变量登记册的回归网（2026-10-21）。

登记册类文档最容易腐烂，所以这个机制的价值全在「防腐三件事」上：
  1. 标记只能写在可执行测试的 docstring 里 → 「登记了但没人守」不可能发生
  2. 生成物与代码必须一致 → `--check` 挡住「改了测试忘了重生成」
  3. ID 不许静默消失 → 守卫测试被删会被 `--check --base` 抓住

第 3 条是重点：如果删掉守卫测试后登记册只是悄悄少一行，那这套机制
就退化成了「一份没人维护的清单」。
"""
from __future__ import annotations

import sys
from pathlib import Path

from scripts import gen_invariants as gi


def _write_tests(tmp: Path, body: str) -> Path:
    tests = tmp / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    (tests / "test_demo.py").write_text(body, encoding="utf-8")
    return tests


def _patch_paths(monkeypatch, tmp: Path, tests: Path):
    monkeypatch.setattr(gi, "REPO_ROOT", tmp)
    monkeypatch.setattr(gi, "TESTS_DIR", tests)
    monkeypatch.setattr(gi, "OUT", tmp / "docs" / "invariants.md")
    monkeypatch.setattr(gi, "RETIRED", tmp / "docs" / "invariants-retired.txt")


GOOD = '''
def test_a():
    """做点什么。

    Invariant: I-01 | 甲必须成立 | source=#101
    """
    assert True


def test_b():
    """另一个。

    Invariant: I-02 | 乙必须成立 | source=#102
    """
    assert True
'''


def test_scan_finds_marks_with_guard_node(tmp_path, monkeypatch):
    tests = _write_tests(tmp_path, GOOD)
    _patch_paths(monkeypatch, tmp_path, tests)

    items = gi.scan_marks()
    assert {i["id"] for i in items} == {"I-01", "I-02"}
    assert items[0]["guard"].endswith("::test_a")
    assert items[0]["source"] == "#101"


def test_scan_ignores_prose_outside_docstring(tmp_path, monkeypatch):
    """注释/普通字符串里的 Invariant 不算——只能登记在守卫上。"""
    body = (
        "# Invariant: I-99 | 注释里的不算 | source=#1\n"
        "X = 'Invariant: I-98 | 字符串里的也不算 | source=#2'\n"
        "\n"
        "def test_a():\n"
        "    assert True\n"
    )
    tests = _write_tests(tmp_path, body)
    _patch_paths(monkeypatch, tmp_path, tests)
    assert gi.scan_marks() == []


def test_validate_rejects_duplicate_id(tmp_path, monkeypatch):
    body = (
        "def test_a():\n"
        '    """Invariant: I-01 | 甲 | source=#1"""\n'
        "    assert True\n"
        "\n"
        "def test_b():\n"
        '    """Invariant: I-01 | 甲又一遍 | source=#2"""\n'
        "    assert True\n"
    )
    tests = _write_tests(tmp_path, body)
    _patch_paths(monkeypatch, tmp_path, tests)

    errors = gi.validate(gi.scan_marks())
    assert any("重复登记" in e for e in errors)


def test_validate_rejects_non_issue_source(tmp_path, monkeypatch):
    """只登真出过事的：source 必须是 Issue 号，不许写「最佳实践」。"""
    body = (
        "def test_a():\n"
        '    """Invariant: I-01 | 甲 | source=good-practice"""\n'
        "    assert True\n"
    )
    tests = _write_tests(tmp_path, body)
    _patch_paths(monkeypatch, tmp_path, tests)

    errors = gi.validate(gi.scan_marks())
    assert any("不是 Issue 号" in e for e in errors)


def test_render_sorts_by_numeric_id(tmp_path, monkeypatch):
    body = (
        "def test_b():\n"
        '    """Invariant: I-10 | 十 | source=#10"""\n'
        "    assert True\n"
        "\n"
        "def test_a():\n"
        '    """Invariant: I-02 | 二 | source=#2"""\n'
        "    assert True\n"
    )
    tests = _write_tests(tmp_path, body)
    _patch_paths(monkeypatch, tmp_path, tests)
    out = gi.render(gi.scan_marks())
    assert out.index("I-02") < out.index("I-10"), "应按数字序而非字典序"


def test_check_fails_when_registry_stale(tmp_path, monkeypatch):
    """改了测试标记但没重跑 --write → 必须红。"""
    tests = _write_tests(tmp_path, GOOD)
    _patch_paths(monkeypatch, tmp_path, tests)
    out = tmp_path / "docs" / "invariants.md"
    out.parent.mkdir(parents=True)

    out.write_text("旧内容\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["x", "--check"])
    assert gi.main() == 1


def test_check_passes_after_write(tmp_path, monkeypatch):
    tests = _write_tests(tmp_path, GOOD)
    _patch_paths(monkeypatch, tmp_path, tests)
    monkeypatch.setattr(sys, "argv", ["x", "--write"])
    assert gi.main() == 0

    monkeypatch.setattr(sys, "argv", ["x", "--check"])
    assert gi.main() == 0


def test_check_fails_when_id_vanishes(tmp_path, monkeypatch):
    """核心防腐：守卫测试被删 → ID 静默消失 → 必须红，除非显式退役。"""
    tests = _write_tests(tmp_path, GOOD)
    _patch_paths(monkeypatch, tmp_path, tests)
    monkeypatch.setattr(sys, "argv", ["x", "--write"])
    gi.main()

    # 模拟「base 版本有 I-01 / I-02」而当前只剩 I-01
    monkeypatch.setattr(gi, "read_ids_at_base", lambda _b: {"I-01", "I-02"})
    (tests / "test_demo.py").write_text(
        "def test_a():\n"
        '    """Invariant: I-01 | 甲必须成立 | source=#101"""\n'
        "    assert True\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", ["x", "--write"])
    gi.main()

    monkeypatch.setattr(sys, "argv", ["x", "--check", "--base", "origin/main"])
    assert gi.main() == 1


def test_retired_id_is_allowed_to_vanished(tmp_path, monkeypatch):
    """显式退役放行——退役要写原因，不是删了就算。"""
    tests = _write_tests(tmp_path, GOOD)
    _patch_paths(monkeypatch, tmp_path, tests)
    retired = tmp_path / "docs" / "invariants-retired.txt"
    retired.parent.mkdir(parents=True)
    retired.write_text("I-02 # 被 I-07 取代：旧判据已合并\n", encoding="utf-8")

    (tests / "test_demo.py").write_text(
        "def test_a():\n"
        '    """Invariant: I-01 | 甲必须成立 | source=#101"""\n'
        "    assert True\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", ["x", "--write"])
    gi.main()

    monkeypatch.setattr(gi, "read_ids_at_base", lambda _b: {"I-01", "I-02"})
    monkeypatch.setattr(sys, "argv", ["x", "--check", "--base", "origin/main"])
    assert gi.main() == 0


def test_missing_registry_is_reported_not_silently_created(tmp_path, monkeypatch):
    """登记册不存在时 --check 要红，不能悄悄当作空表通过。"""
    tests = _write_tests(tmp_path, GOOD)
    _patch_paths(monkeypatch, tmp_path, tests)
    monkeypatch.setattr(sys, "argv", ["x", "--check"])
    assert gi.main() == 1
