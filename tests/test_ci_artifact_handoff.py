"""Static contracts for the artifact-v4-free CI pytest handoff."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
SUMMARY_HELPER = ROOT / "scripts" / "ci" / "pytest_summary.py"
KNOWN_FAILURES_CHECKER = ROOT / "scripts" / "check_known_failures.py"
KNOWN_FAILURES = ROOT / "tests" / "known_failures.txt"


def _load_summary_helper():
    spec = importlib.util.spec_from_file_location("ci_summary_contract", SUMMARY_HELPER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _job(workflow: str, name: str) -> str:
    match = re.search(
        rf"^  {re.escape(name)}:\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
        workflow,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"missing workflow job: {name}"
    return match.group("body")


def test_artifact_v4_actions_are_entirely_removed(workflow):
    assert "upload-artifact" not in workflow
    assert "download-artifact" not in workflow
    assert "artifact@v4" not in workflow


def test_two_required_repository_check_jobs_remain(workflow):
    assert re.search(r"^  test:\s*$", workflow, flags=re.MULTILINE)
    assert re.search(r"^  agent-framework-reports:\s*$", workflow, flags=re.MULTILINE)


def test_test_job_exports_the_compact_summary_only(workflow):
    test_job = _job(workflow, "test")

    assert "outputs:" in test_job
    assert "pytest_summary_b64: ${{ steps.pytest_summary.outputs.pytest_summary_b64 }}" in test_job
    assert "python3 scripts/ci/pytest_summary.py /tmp/pytest-output.txt" in test_job
    assert test_job.index("Zero-new-failure gate") < test_job.index(
        "Create compact pytest summary output"
    )


def test_reports_job_decodes_to_the_documented_reuse_path(workflow):
    reports_job = _job(workflow, "agent-framework-reports")

    assert "PYTEST_SUMMARY_B64: ${{ needs.test.outputs.pytest_summary_b64 }}" in reports_job
    assert "mkdir -p /tmp/ci-pytest" in reports_job
    assert "base64 --decode > /tmp/ci-pytest/pytest-result.json" in reports_job
    assert "--evidence-output /tmp/ci-pytest/pytest-evidence.json" in reports_job
    assert "PYTEST_REUSE_FILE: /tmp/ci-pytest/pytest-output.txt" in reports_job


def test_reports_job_keeps_all_existing_gates(workflow):
    reports_job = _job(workflow, "agent-framework-reports")

    for required in (
        "Generate reports 02 + 03",
        "Generate report 04 (local deploy preview)",
        "Check four reports gate",
        "Redline check",
        "PR hygiene check",
    ):
        assert required in reports_job


def test_generate_reports_keeps_direct_execution_fallback():
    script = (ROOT / "scripts" / "agent_framework" / "generate_reports.sh").read_text(
        encoding="utf-8"
    )

    assert 'REUSE_FILE="${PYTEST_REUSE_FILE:-/tmp/ci-pytest/pytest-output.txt}"' in script
    assert 'if [[ -e "${REUSE_FILE}"' in script
    assert "refusing fallback" in script
    assert 'echo "[generate_reports] running pytest ..."' in script


def _a_known_failure() -> str:
    for line in KNOWN_FAILURES.read_text(encoding="utf-8").splitlines():
        candidate = line.strip()
        if candidate and not candidate.startswith("#"):
            return candidate
    raise AssertionError("known_failures.txt is empty")


def _run_known_failures_gate(tmp_path: Path, compact_source: str) -> subprocess.CompletedProcess:
    helper = _load_summary_helper()
    summary = helper.extract_pytest_summary(compact_source)
    output = tmp_path / "pytest-compact.txt"
    output.write_text(summary, encoding="utf-8")
    native = 2 if "ERROR " in summary else 1
    ids = [line.split(" - ", 1)[0].removeprefix("FAILED ") for line in summary.splitlines() if line.startswith("FAILED ")]
    session = {"schema_version": 1, "pytest_exit": native, "selected": 101, "selected_unique": 101,
               "completed": 101, "uncompleted": [], "unexpected": [], "error_nodeids": [], "failed_nodeids": ids,
               "counts": {"passed": 100, "failed": len(ids), "skipped": 0, "xfailed": 0, "xpassed": 0, "error": 0}}
    meta = tmp_path / "session.json"
    meta.write_text(json.dumps(session), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(KNOWN_FAILURES_CHECKER), str(output), "--pytest-exit", str(native), "--session-file", str(meta)],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_compact_summary_preserves_known_failure_semantics(tmp_path):
    known = _a_known_failure()
    source = f"FAILED {known} - old baseline\n1 failed, 100 passed in 1s\n"

    proc = _run_known_failures_gate(tmp_path, source)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "OK" in proc.stdout


def test_compact_summary_still_blocks_new_failures(tmp_path):
    source = "FAILED tests/definitely_new.py::test_x - boom\n1 failed, 100 passed in 1s\n"

    proc = _run_known_failures_gate(tmp_path, source)

    assert proc.returncode == 1
    assert "tests/definitely_new.py::test_x" in proc.stdout


def test_compact_summary_still_blocks_collection_errors(tmp_path):
    source = (
        "ERROR tests/broken.py - ImportError\n"
        "!!!! Interrupted: 1 error during collection !!!!\n"
        "1 error in 1s\n"
    )

    proc = _run_known_failures_gate(tmp_path, source)

    assert proc.returncode == 1
