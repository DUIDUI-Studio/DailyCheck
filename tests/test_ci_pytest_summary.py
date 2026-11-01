"""Compact pytest job-output extraction tests.

The reports job must receive enough of the test job's pytest output to preserve
the known-failures gate, without transporting tracebacks or the full suite log.
"""

from __future__ import annotations

import base64
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "scripts" / "ci" / "pytest_summary.py"


def _load_helper():
    spec = importlib.util.spec_from_file_location("pytest_summary_under_test", HELPER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def helper():
    return _load_helper()


SAMPLE_OUTPUT = """\
..F..E
FAILED tests/unit/test_known.py::test_known - AssertionError: old baseline
traceback line that must not be transported
ERROR tests/broken.py - ModuleNotFoundError: broken import
!!!! Interrupted: 1 error during collection !!!!
1 failed, 1 error, 17 passed, 2 warnings in 1.25s
extra tail line 1
extra tail line 2
extra tail line 3
extra tail line 4
extra tail line 5
"""


def test_extracts_status_and_collection_markers_plus_final_summary(helper):
    summary = helper.extract_pytest_summary(SAMPLE_OUTPUT)

    assert "FAILED tests/unit/test_known.py::test_known" in summary
    assert "ERROR tests/broken.py" in summary
    assert "Interrupted: 1 error during collection" in summary
    assert "traceback line that must not be transported" not in summary
    assert summary.splitlines()[-5:] == SAMPLE_OUTPUT.splitlines()[-5:]


def test_extraction_is_deterministic_and_preserves_source_order(helper):
    first = helper.extract_pytest_summary(SAMPLE_OUTPUT)
    second = helper.extract_pytest_summary(SAMPLE_OUTPUT)

    assert first == second
    status_lines = [line for line in first.splitlines() if line.startswith(("FAILED ", "ERROR "))]
    assert status_lines == [
        "FAILED tests/unit/test_known.py::test_known - AssertionError: old baseline",
        "ERROR tests/broken.py - ModuleNotFoundError: broken import",
    ]


def test_collection_error_without_interrupted_line_is_kept(helper):
    source = "ERROR tests/broken.py - ImportError\n1 error in 0.2s\n"
    summary = helper.extract_pytest_summary(source)

    assert "ERROR tests/broken.py" in summary


def test_oversized_compact_summary_fails_instead_of_truncating_required_lines(helper, monkeypatch):
    monkeypatch.setattr(helper, "MAX_SUMMARY_BYTES", 10)

    with pytest.raises(ValueError, match="too large"):
        helper.extract_pytest_summary(SAMPLE_OUTPUT)


def test_writes_single_base64_github_output(helper, tmp_path):
    source = tmp_path / "pytest-output.txt"
    output = tmp_path / "github-output"
    source.write_text(SAMPLE_OUTPUT, encoding="utf-8")

    assert helper.main([str(source), "--output-file", str(output)]) == 0

    line = output.read_text(encoding="utf-8").rstrip("\n")
    prefix = f"{helper.OUTPUT_NAME}="
    assert line.startswith(prefix)
    encoded = line.removeprefix(prefix)
    assert "\n" not in encoded
    assert base64.b64decode(encoded).decode("utf-8") == helper.extract_pytest_summary(SAMPLE_OUTPUT)


def test_missing_input_returns_usage_error(helper, tmp_path):
    assert helper.main([str(tmp_path / "missing.txt"), "--output-file", str(tmp_path / "out")]) == 2
