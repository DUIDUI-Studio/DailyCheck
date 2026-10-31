"""Issue #103: every full-suite gate includes the MCP directory and rejects new failures."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINTS = (
    ".github/workflows/ci.yml",
    "scripts/blue_green/verify.sh",
    "scripts/agent_framework/generate_reports.sh",
)


@pytest.mark.parametrize("entrypoint", ENTRYPOINTS)
def test_full_suite_entrypoint_includes_mcp(entrypoint):
    source = (ROOT / entrypoint).read_text()
    assert "-m pytest" in source
    assert not re.search(r"--(?:ignore(?:-glob)?|deselect)(?:=|\s+)[^\n]*mcp_server", source)
    assert "check_known_failures.py" in source


@pytest.mark.parametrize("new_failure", [False, True], ids=["known-failure", "new-failure"])
def test_verify_full_suite_uses_isolated_db_and_preserves_failure_gate(tmp_path, new_failure):
    source = (ROOT / "scripts/blue_green/verify.sh").read_text()
    function = source.split("_verify_check1_pytest() {", 1)[1].split("\n}\n", 1)[0]
    function = function.replace("/tmp/verify-1-pytest.log", str(tmp_path / "pytest.log"))
    record = tmp_path / "invocation.json"
    fake_python = tmp_path / "python"
    known = next(
        line for line in (ROOT / "tests/known_failures.txt").read_text().splitlines()
        if line and not line.startswith("#")
    )
    nodeid = "tests/mcp_server/test_new.py::test_regression" if new_failure else known
    fake_python.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "if sys.argv[1:3] == ['-m', 'pytest']:\n"
        f"    Path({str(record)!r}).write_text(json.dumps({{'args': sys.argv[1:], "
        "'db': os.environ.get('DAILYCHECK_DB_DIR')}))\n"
        f"    print('FAILED {nodeid} - seeded gate test')\n"
        "    print('1 failed, 1 passed in 0.01s')\n"
        "    sys.exit(1)\n"
        f"os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])\n"
    )
    fake_python.chmod(0o755)
    env = dict(os.environ, REPO_ROOT=str(ROOT), DC_PYTHON_BIN=str(fake_python),
               DAILYCHECK_DB_DIR=str(tmp_path / "host-db"), TMPDIR=str(tmp_path))
    proc = subprocess.run(
        ["bash", "-c", "_verify_check1_pytest() {" + function + "\n}\n_verify_check1_pytest"],
        env=env, text=True, capture_output=True, timeout=30,
    )
    assert proc.returncode == int(new_failure), proc.stdout + proc.stderr
    invocation = json.loads(record.read_text())
    assert "tests/" in invocation["args"]
    assert not any(arg.startswith("--ignore") for arg in invocation["args"])
    selected = Path(invocation["db"])
    assert selected != tmp_path / "host-db"
    assert selected.is_relative_to(tmp_path)
    assert not selected.exists(), "verify must clean up its temporary test database directory"


@pytest.mark.parametrize("reuse", [False, True], ids=["fallback", "reuse"])
@pytest.mark.parametrize("new_failure", [False, True], ids=["known-failure", "new-failure"])
def test_report_generation_keeps_real_gate_status_and_reports(tmp_path, reuse, new_failure):
    import shutil

    repo = tmp_path / "repo"
    script = repo / "scripts/agent_framework/generate_reports.sh"
    script.parent.mkdir(parents=True)
    # All outputs (including the script's legacy /tmp log constants) stay in this sandbox.
    script.write_text((ROOT / "scripts/agent_framework/generate_reports.sh").read_text().replace(
        '"/tmp/', f'"{tmp_path}/',
    ))
    shutil.copy2(ROOT / "scripts/check_known_failures.py", repo / "scripts/check_known_failures.py")
    (repo / "tests").mkdir()
    shutil.copy2(ROOT / "tests/known_failures.txt", repo / "tests/known_failures.txt")
    known = next(line for line in (repo / "tests/known_failures.txt").read_text().splitlines()
                 if line and not line.startswith("#"))
    nodeid = "tests/mcp_server/test_new.py::test_regression" if new_failure else known
    output = f"FAILED {nodeid} - seeded gate test\n1 failed, 1 passed in 0.01s\n"
    reuse_file = tmp_path / "reuse.txt"
    if reuse:
        reuse_file.write_text(output)
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    python = fakebin / "python3"
    python.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "if sys.argv[1:3] == ['-m', 'pytest']:\n"
        f"    print({output!r}, end='')\n"
        "    sys.exit(1)\n"
        f"os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])\n"
    )
    python.chmod(0o755)
    for name, body in {"git": "[ \"$1\" != rev-parse ] || echo fix/issue-103-mcp-permissions-ci",
                       "radon": ":", "bandit": ":"}.items():
        tool = fakebin / name
        tool.write_text(f"#!/bin/sh\n{body}\nexit 0\n")
        tool.chmod(0o755)
    env = dict(os.environ, PATH=f"{fakebin}:{os.environ['PATH']}",
               PYTEST_REUSE_FILE=str(reuse_file), HEAD_SHA="a" * 40, BASE_SHA="b" * 40)
    proc = subprocess.run(["bash", str(script)], cwd=repo, env=env,
                          capture_output=True, text=True, timeout=30)
    summary = json.loads((repo / "reports/ci-summary.json").read_text())
    assert proc.returncode == int(new_failure), proc.stdout + proc.stderr
    assert summary["pytest"] == {"passed": 1, "failed": 1, "gate_exit": int(new_failure)}
    assert nodeid in (repo / "reports/02-test-report.md").read_text()
    assert (repo / "reports/03-code-quality.md").exists()
    assert ("复用 test job" in proc.stdout) == reuse
