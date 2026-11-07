"""`scripts/check_known_failures.py`（CI 零新增失败门禁）测试。

这个脚本是 CI 的**放行裁判**，它坏了不会有人发现 —— 坏了的表现是「全绿」。
所以它的每个放行条件都要有反例守着。

2026-10-25 实测事故：调用方把 pytest 输出先 `grep ^FAILED | sed 's/^FAILED //'`
处理了一遍再喂进来，正则匹配不到任何行 → 失败集为空 → 无条件放行，22 个失败
（含 1 个真实净增）被判定为「0 个新增」。本文件的 `test_prefixed_input_is_rejected`
就是这条护栏的回归用例。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_known_failures.py"
KNOWN = REPO_ROOT / "tests" / "known_failures.txt"


def _run(tmp_path: Path, text: str) -> subprocess.CompletedProcess:
    out = tmp_path / "pytest-out.txt"
    out.write_text(text, encoding="utf-8")
    ids = [line.split(" - ", 1)[0].removeprefix("FAILED ") for line in text.splitlines() if line.startswith("FAILED ")]
    passed = re.search(r"(\d+) passed", text)
    count = int(passed[1]) if passed else 0
    native = 2 if "Interrupted:" in text else (1 if ids else 0)
    session = {"schema_version": 1, "pytest_exit": native, "selected": count + len(ids),
               "selected_unique": count + len(ids), "completed": count + len(ids),
               "uncompleted": [], "unexpected": [], "error_nodeids": [], "failed_nodeids": ids,
               "counts": {"passed": count, "failed": len(ids), "skipped": 0, "xfailed": 0, "xpassed": 0, "error": 0}}
    meta = tmp_path / "session.json"
    meta.write_text(json.dumps(session), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(out), "--pytest-exit", str(native), "--session-file", str(meta)],
        capture_output=True, text=True, timeout=30,
    )


def _a_known_case() -> str:
    for line in KNOWN.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    raise AssertionError("known_failures.txt 为空，基线已丢失")


def test_all_failures_known_passes(tmp_path):
    node = _a_known_case()
    proc = _run(tmp_path, f"FAILED {node} - assert 1 == 2\n1 failed, 100 passed in 1s\n")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "OK" in proc.stdout


def test_new_failure_blocks(tmp_path):
    proc = _run(
        tmp_path,
        "FAILED tests/definitely_new.py::test_x - boom\n1 failed, 100 passed in 1s\n",
    )
    assert proc.returncode == 1
    assert "tests/definitely_new.py::test_x" in proc.stdout


def test_prefixed_input_is_rejected(tmp_path):
    """摘要说 failed，却解析不出 FAILED 行 —— 必须拒绝，不能假装全绿。

    这是「喂了剥过前缀的文件」的特征，也是假绿的唯一入口。
    """
    proc = _run(
        tmp_path,
        "tests/some.py::test_a\ntests/other.py::test_b\n2 failed, 100 passed in 1s\n",
    )
    assert proc.returncode == 1, "剥前缀输入被误放行 —— 零净增门禁失效"
    assert "原始输出" in proc.stdout


def test_empty_output_blocks(tmp_path):
    assert _run(tmp_path, "").returncode == 1


def test_zero_passed_blocks(tmp_path):
    proc = _run(tmp_path, "0 passed, 3 skipped in 1s\n")
    assert proc.returncode == 1


def test_collection_error_blocks(tmp_path):
    proc = _run(
        tmp_path,
        "ERROR tests/x.py - ModuleNotFoundError\n"
        "!!! Interrupted: 1 error during collection !!!\n"
        "1 error in 1s\n",
    )
    assert proc.returncode == 1


def test_usage_error(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 2
