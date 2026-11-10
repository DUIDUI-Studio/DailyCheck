"""Issue #103: every full-suite gate includes the MCP directory and rejects new failures."""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _run_owned(command, tmp_path, *, env, cwd=None, timeout=30):
    """Keep timeout cleanup attached to this fixture's entire process group."""
    proc = subprocess.Popen(command, cwd=cwd, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)
    receipt = tmp_path / "process.json"
    facts = {"pid": proc.pid, "pgid": proc.pid, "before_IO": True}
    receipt.write_text(json.dumps(facts))
    expired = None
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        expired = exc
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            out, err = proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            out, err = proc.communicate()
    finally:
        proc.wait()
        # Even a normally exited leader must not leave fixture descendants.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 2
        while True:
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                facts["group_gone"] = True
                break
            if time.monotonic() >= deadline:
                facts["group_gone"] = False
                break
            time.sleep(.01)
        facts.update(positive_wait=True, native_exit=proc.returncode)
        receipt.write_text(json.dumps(facts))
    assert facts["group_gone"], "owned fixture group exit remains unproven"
    if expired:
        raise expired
    return subprocess.CompletedProcess(command, proc.returncode, out, err)


def _owned_env(tmp_path):
    env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ")
           if key in os.environ}
    env.update(HOME=str(tmp_path), TMPDIR=str(tmp_path),
               PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    return env


def _session(nodeid):
    # Wiring fixture only; native pytest completion is separately exercised in
    # test_pytest_gate_completeness.py with actual subprocesses.
    return {"schema_version": 1, "pytest_exit": 1, "selected": 2, "selected_unique": 2,
            "completed": 2, "uncompleted": [], "unexpected": [], "error_nodeids": [],
            "failed_nodeids": [nodeid],
            "counts": {"passed": 1, "failed": 1, "skipped": 0, "xfailed": 0, "xpassed": 0, "error": 0}}


ENTRYPOINTS = (
    ".github/workflows/ci.yml",
    "scripts/blue_green/verify.sh",
    "scripts/agent_framework/generate_reports.sh",
)


@pytest.mark.parametrize("entrypoint", ENTRYPOINTS)
def test_full_suite_entrypoint_includes_mcp(entrypoint):
    source = (ROOT / entrypoint).read_text()
    if entrypoint == "scripts/blue_green/verify.sh":
        assert '"${REPO_ROOT}/scripts/blue_green/verify_pytest.py" "${test_db_dir}"' in source
        assert re.search(r"\n\s+tests/ --basetemp=", source)
        delegate = (ROOT / "scripts/blue_green/verify_pytest.py").read_text()
        assert '"-m", "pytest", *sys.argv[2:]' in delegate
        assert not re.search(r"--(?:ignore(?:-glob)?|deselect)", delegate)
    else:
        assert "-m pytest" in source
    assert not re.search(r"--(?:ignore(?:-glob)?|deselect)(?:=|\s+)[^\n]*mcp_server", source)
    assert "check_known_failures.py" in source


@pytest.mark.parametrize("new_failure", [False, True], ids=["known-failure", "new-failure"])
def test_verify_full_suite_uses_isolated_db_and_preserves_failure_gate(tmp_path, new_failure):
    source = (ROOT / "scripts/blue_green/verify.sh").read_text()
    function = source.split("_verify_check1_pytest() {", 1)[1].split("\n}\n", 1)[0]
    record = tmp_path / "invocation.json"
    repo = tmp_path / "repo"
    for name in ("scripts/blue_green/verify_pytest.py", "scripts/check_known_failures.py",
                 "tests/known_failures.txt"):
        destination = repo / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, destination)
    known = next(
        line for line in (ROOT / "tests/known_failures.txt").read_text().splitlines()
        if line and not line.startswith("#")
    )
    nodeid = "tests/mcp_server/test_new.py::test_regression" if new_failure else known
    # The real helper chooses the child's DB/environment and launches this finite
    # module from its owned cwd. No interpreter fallback can run the real suite.
    (repo / "pytest.py").write_text(
        "import json, os, sys\nfrom pathlib import Path\n"
        "db = Path(os.environ['DAILYCHECK_DB_DIR'])\n"
        "expected = ['tests/', '--basetemp=' + str(db / 'pytest'), '-q', '--tb=line',\n"
        "            '-rfE', '--color=no', '-p', 'scripts.ci.pytest_evidence',\n"
        "            '--dc-evidence=' + str(db / 'session.json')]\n"
        "assert sys.argv[1:] == expected, 'unexpected pytest fixture argument shape'\n"
        "assert Path(os.environ['HOME']) == db / 'home'\n"
        f"Path({str(record)!r}).write_text(json.dumps({{'args': sys.argv[1:], "
        "'db': os.environ['DAILYCHECK_DB_DIR'], 'module': __file__}))\n"
        f"(db / 'session.json').write_text(json.dumps({_session(nodeid)!r}))\n"
        f"print('FAILED {nodeid} - seeded gate test')\n"
        "print('1 failed, 1 passed in 0.01s')\n"
        "sys.exit(1)\n"
    )
    env = dict(_owned_env(tmp_path), REPO_ROOT=str(repo), DC_PYTHON_BIN=sys.executable,
               DAILYCHECK_DB_DIR=str(tmp_path / "host-db"), VERIFY_TMP_DIR=str(tmp_path))
    proc = _run_owned(
        ["bash", "-c", "_verify_check1_pytest() {" + function + "\n}\n_verify_check1_pytest"],
        tmp_path, env=env,
    )
    assert proc.returncode == int(new_failure), proc.stdout + proc.stderr
    invocation = json.loads(record.read_text())
    assert "tests/" in invocation["args"]
    assert not any(arg.startswith("--ignore") for arg in invocation["args"])
    selected = Path(invocation["db"])
    assert selected != tmp_path / "host-db"
    assert selected.is_relative_to(tmp_path)
    assert not selected.exists(), "verify must clean up its temporary test database directory"
    assert invocation["module"] == str(repo / "pytest.py")
    evidence = json.loads((tmp_path / "verify-1-pytest-evidence.json").read_text())
    assert evidence["native_exit"] == 1
    assert evidence["session"] == _session(nodeid)
    valid_record = record.read_bytes()
    denied = tmp_path / "unexpected-selection"
    denied.mkdir()
    # Drive the same unchanged helper/checker with a deliberately wrong caller
    # selection. The finite port must deny before writing session or record.
    bad_function = function.replace("tests/ --basetemp=", "tests/mcp_server/ --basetemp=")
    bad = _run_owned(
        ["bash", "-c", "_verify_check1_pytest() {" + bad_function + "\n}\n_verify_check1_pytest"],
        denied, env=dict(env, VERIFY_TMP_DIR=str(denied)),
    )
    assert bad.returncode == 1
    assert "unexpected pytest fixture argument shape" in (denied / "verify-1-pytest.log").read_text()
    assert not (denied / "verify-1-pytest-evidence.json").exists()
    assert record.read_bytes() == valid_record


def test_gate_fixture_timeout_reaps_owned_group(tmp_path):
    child = tmp_path / "child.json"
    leader = tmp_path / "leader.py"
    leader.write_text(
        'import json, signal, subprocess, sys, time\nfrom pathlib import Path\n'
        'p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])\n'
        f'receipt = Path({str(child)!r})\n'
        'receipt.write_text(json.dumps({"pid": p.pid, "before_IO": True}))\n'
        'def finish(signum, frame):\n'
        '    rc = p.wait()\n'
        '    receipt.write_text(json.dumps({"pid": p.pid, "positive_wait": True, "rc": rc}))\n'
        '    sys.exit(0)\n'
        'signal.signal(signal.SIGTERM, finish)\n'
        'while True: time.sleep(.05)\n'
    )
    with pytest.raises(subprocess.TimeoutExpired):
        _run_owned([sys.executable, "-B", str(leader)], tmp_path,
                   env=_owned_env(tmp_path), timeout=2)
    facts = json.loads(child.read_text())
    assert facts["positive_wait"] and facts["rc"] == -signal.SIGTERM
    with pytest.raises(ProcessLookupError):
        os.kill(facts["pid"], 0)
    assert json.loads((tmp_path / "process.json").read_text())["group_gone"]


@pytest.mark.parametrize("reuse", [False, True, "missing", "no-evidence", "tampered", "invalid-native"], ids=["fallback", "reuse", "missing", "no-evidence", "tampered", "invalid-native"])
@pytest.mark.parametrize("new_failure", [False, True], ids=["known-failure", "new-failure"])
def test_report_generation_keeps_real_gate_status_and_reports(tmp_path, reuse, new_failure):
    import shutil

    repo = tmp_path / "repo"
    script = repo / "scripts/agent_framework/generate_reports.sh"
    script.parent.mkdir(parents=True)
    # All outputs (including the script's legacy /tmp log constants) stay in this sandbox.
    script.write_text((ROOT / "scripts/agent_framework/generate_reports.sh").read_text().replace(
        '/tmp/', f'{tmp_path}/',
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
        from scripts.check_known_failures import make_evidence

        reuse_file.write_text(output)
        meta = Path(str(reuse_file) + ".evidence.json")
        meta.write_text(json.dumps(make_evidence(output, 1, _session(nodeid))))
        if reuse == "missing":
            reuse_file.unlink()
        elif reuse == "no-evidence":
            meta.unlink()
        elif reuse == "tampered":
            reuse_file.write_text(output + "KeyboardInterrupt\n")
        elif reuse == "invalid-native":
            data = json.loads(meta.read_text())
            data["native_exit"] = 0
            meta.write_text(json.dumps(data))
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    python = fakebin / "python3"
    python.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "if sys.argv[1:3] == ['-m', 'pytest']:\n"
        f"    Path({str(tmp_path / 'pytest-ran')!r}).touch()\n"
        "    meta = next(a.split('=', 1)[1] for a in sys.argv if a.startswith('--dc-evidence='))\n"
        f"    Path(meta).write_text(json.dumps({_session('PLACEHOLDER')!r}).replace('PLACEHOLDER', {nodeid!r}))\n"
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
    env = dict(_owned_env(tmp_path), PATH=f"{fakebin}:{os.environ['PATH']}", TMPDIR=str(tmp_path),
               HEAD_SHA="a" * 40, BASE_SHA="b" * 40)
    env.pop("PYTEST_REUSE_FILE", None)
    env.pop("PYTEST_REUSE_EVIDENCE", None)
    if reuse:
        env["PYTEST_REUSE_FILE"] = str(reuse_file)
    proc = _run_owned(["bash", str(script)], tmp_path, cwd=repo, env=env)
    summary = json.loads((repo / "reports/ci-summary.json").read_text())
    invalid = isinstance(reuse, str)
    assert proc.returncode == int(new_failure or invalid), proc.stdout + proc.stderr
    if invalid:
        assert summary["pytest"] == {"passed": None, "failed": None, "gate_exit": 1, "native_exit": None, "complete": False}
    else:
        assert summary["pytest"] == {"passed": 1, "failed": 1, "gate_exit": int(new_failure), "native_exit": 1, "complete": True}
    if reuse != "missing":
        assert nodeid in (repo / "reports/02-test-report.md").read_text()
    assert (tmp_path / "pytest-ran").exists() == (not reuse)
    assert (repo / "reports/03-code-quality.md").exists()
    assert ("复用 test job" in proc.stdout) == bool(reuse)
