"""Real pytest processes and strict native/session/output handoff regressions."""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from scripts.check_known_failures import validate_evidence

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts/check_known_failures.py"
SUMMARY = ROOT / "scripts/ci/pytest_summary.py"


@pytest.fixture()
def run_pytest(tmp_path):
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "scripts").mkdir()
    shutil.copy2(CHECKER, repo / "scripts/check_known_failures.py")
    known = repo / "tests/known_failures.txt"
    known.write_text("tests/test_cases.py::test_known\n", encoding="utf-8")

    def run(source, *args, conftest=""):
        (repo / "tests/test_cases.py").write_text(source, encoding="utf-8")
        (repo / "conftest.py").write_text(conftest, encoding="utf-8")
        session = tmp_path / "session.json"
        session.unlink(missing_ok=True)
        env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTEST_ADDOPTS": "", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
        command = [sys.executable, "-m", "pytest", "tests/", "--rootdir", str(repo),
                   "-p", "scripts.ci.pytest_evidence", f"--dc-evidence={session}",
                   "-q", "--tb=no", "-rfE", "--color=no", *args]
        result = subprocess.run(command, cwd=repo, env=env, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=30, check=False)
        output = tmp_path / "output.txt"
        output.write_text(result.stdout, encoding="utf-8")
        native = tmp_path / "native.exit"
        native.write_text(str(result.returncode) + "\n", encoding="utf-8")
        return {"repo": repo, "output": output, "native": native, "session": session,
                "result": result, "evidence": tmp_path / "evidence.json"}
    return run


def gate(run, *extra, with_native=True):
    command = [sys.executable, str(run["repo"] / "scripts/check_known_failures.py"), str(run["output"])]
    if with_native:
        command += ["--pytest-exit-file", str(run["native"]), "--session-file", str(run["session"]),
                    "--write-evidence", str(run["evidence"])]
    return subprocess.run([*command, *extra], text=True, capture_output=True, timeout=30, check=False)


@pytest.mark.parametrize("source,native,expected", [
    ("def test_pass(): assert True\n", 0, 0),
    ("def test_known(): assert False\n", 1, 0),
    ("def test_new(): assert False\n", 1, 1),
    ("import pytest\ndef test_skip(): pytest.skip('intentional')\n", 0, 0),
])
def test_genuine_complete_outcomes(run_pytest, source, native, expected):
    run = run_pytest(source)
    assert run["result"].returncode == native
    checked = gate(run)
    assert checked.returncode == expected, checked.stdout + checked.stderr
    counts = validate_evidence(run["output"].read_text(), json.loads(run["evidence"].read_text()))
    assert sum(counts.values()) == 1


def test_parameterized_scope_stays_exact_without_alias_count_inflation(run_pytest):
    run = run_pytest("import pytest\n@pytest.mark.parametrize('value', ['with space', 'other'])\ndef test_known(value): assert False\n")
    checked = gate(run)
    assert checked.returncode == 1, "base-only known entry must not authorize new parameter variants"
    session = json.loads(run["session"].read_text())
    ids = session["failed_nodeids"]
    assert len(ids) == session["counts"]["failed"] == 2
    known = run["repo"] / "tests/known_failures.txt"
    # Legacy policy required both actual parameter nodeid AND its base alias.
    known.write_text("\n".join(ids) + "\n")
    assert gate(run).returncode == 1, "exact-only nodes without the legacy base are still unauthorized"
    base = "tests/test_cases.py::test_known"
    known.write_text(base + "\n" + ids[0] + "\n")
    checked = gate(run)
    assert checked.returncode == 1 and ids[1] in checked.stdout
    known.write_text(base + "\n" + "\n".join(ids) + "\n")
    checked = gate(run)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert "当前失败 2 个" in checked.stdout


@pytest.mark.parametrize("source,args,conftest", [
    ("import package_that_does_not_exist_114\n", (), ""),
    ("import pytest\n@pytest.fixture\ndef bad(): raise RuntimeError('setup')\ndef test_known(bad): pass\n", (), ""),
    ("import pytest\n@pytest.fixture\ndef bad():\n yield\n raise RuntimeError('teardown')\ndef test_known(bad): pass\n", (), ""),
    ("def test_known(): raise KeyboardInterrupt\n", (), ""),
    ("# no tests\n", (), ""),
    ("import os, signal\ndef test_known(): os.kill(os.getpid(), signal.SIGTERM)\n", (), ""),
    ("def test_pass(): pass\n", ("--dc-invalid-option-114",), ""),
    ("import pytest\ndef test_known(): pytest.exit('stop', returncode=1)\n", (), ""),
    ("def test_known(): assert False\ndef test_not_reached(): assert True\n", ("-x",), ""),
    ("def test_pass(): pass\n", (), "def pytest_collection_modifyitems(items): raise RuntimeError('internal')\n"),
])
def test_real_pytest_abnormal_or_incomplete_runs_reject(run_pytest, source, args, conftest):
    run = run_pytest(source, *args, conftest=conftest)
    checked = gate(run)
    assert checked.returncode != 0
    assert "OK:" not in checked.stdout


@pytest.mark.parametrize("native", [-15, 2, 3, 4, 5, 130, "", "None", "true", "0 extra"])
def test_missing_invalid_or_abnormal_native_exit_rejects(run_pytest, native):
    run = run_pytest("def test_pass(): pass\n")
    run["native"].write_text(str(native), encoding="utf-8")
    checked = gate(run)
    assert checked.returncode != 0
    assert "OK:" not in checked.stdout


def test_cli_cannot_gate_a_fragment_without_provenance(run_pytest):
    run = run_pytest("def test_pass(): pass\n")
    assert gate(run, with_native=False).returncode == 2
    run["session"].unlink()
    assert gate(run).returncode == 1


@pytest.mark.parametrize("damage", ["truncate", "missing_failed", "duplicate_failed", "wrong_failed_count", "wrong_passed_count", "interrupt_after_footer", "interrupt_before_footer", "trailer"])
def test_native_one_and_some_known_results_cannot_hide_damaged_output(run_pytest, damage):
    run = run_pytest("def test_known(): assert False\ndef test_pass(): pass\n")
    text = run["output"].read_text()
    failed = next(line for line in text.splitlines() if line.startswith("FAILED "))
    if damage == "truncate":
        text = text.rsplit("1 failed", 1)[0]
    elif damage == "missing_failed":
        text = text.replace(failed + "\n", "")
    elif damage == "duplicate_failed":
        text = text.replace(failed, failed + "\n" + failed)
    elif damage == "wrong_failed_count":
        text = text.replace("1 failed,", "2 failed,")
    elif damage == "wrong_passed_count":
        text = text.replace("1 passed", "200 passed")
    elif damage == "interrupt_after_footer":
        text += "KeyboardInterrupt\n"
    elif damage == "interrupt_before_footer":
        text = text.replace("1 failed,", "KeyboardInterrupt\n1 failed,")
    else:
        text += "unexpected incomplete trailer\n"
    run["output"].write_text(text, encoding="utf-8")
    assert gate(run).returncode == 1


@pytest.mark.parametrize("field,value", [("completed", 0), ("selected", 2), ("pytest_exit", 0), ("uncompleted", ["missing"]), ("counts", {}), ("schema_version", 2)])
def test_session_metadata_must_be_consistent(run_pytest, field, value):
    run = run_pytest("def test_known(): assert False\n")
    session = json.loads(run["session"].read_text())
    session[field] = value
    run["session"].write_text(json.dumps(session), encoding="utf-8")
    assert gate(run).returncode == 1


def test_compact_roundtrip_retains_real_native_and_session_evidence(run_pytest, tmp_path):
    run = run_pytest("def test_known(): assert False\ndef test_pass(): pass\n")
    assert gate(run).returncode == 0
    job = tmp_path / "github-output"
    result = subprocess.run([sys.executable, str(SUMMARY), str(run["output"]), "--evidence-file", str(run["evidence"]), "--output-file", str(job)], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
    envelope = tmp_path / "envelope.json"
    envelope.write_bytes(base64.b64decode(job.read_text().split("=", 1)[1]))
    compact, evidence = tmp_path / "compact.txt", tmp_path / "compact-evidence.json"
    result = subprocess.run([sys.executable, str(SUMMARY), "--decode", str(envelope), "--text-output", str(compact), "--evidence-output", str(evidence)], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(evidence.read_text())
    assert data["native_exit"] == 1
    assert data["session"]["completed"] == 2
    result = subprocess.run([sys.executable, str(run["repo"] / "scripts/check_known_failures.py"), str(compact), "--evidence-file", str(evidence)], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
    compact.write_text(compact.read_text() + "KeyboardInterrupt\n")
    result = subprocess.run([sys.executable, str(run["repo"] / "scripts/check_known_failures.py"), str(compact), "--evidence-file", str(evidence)], text=True, capture_output=True)
    assert result.returncode == 1


def test_compact_export_requires_valid_provenance(run_pytest, tmp_path):
    run = run_pytest("def test_pass(): pass\n")
    assert gate(run).returncode == 0
    run["output"].write_text("partial known output\n")
    result = subprocess.run([sys.executable, str(SUMMARY), str(run["output"]), "--evidence-file", str(run["evidence"]), "--output-file", str(tmp_path / "job")], text=True, capture_output=True)
    assert result.returncode != 0
    assert not (tmp_path / "job").exists()


@pytest.mark.parametrize("source,native,expected", [
    ("def test_pass(): pass\n", 0, 0),
    ("def test_known(): assert False\n", 1, 0),
    ("def test_known(): raise KeyboardInterrupt\n", 2, 1),
])
def test_actual_ci_capture_and_gate_steps_keep_native_exit(run_pytest, tmp_path, source, native, expected):
    run = run_pytest(source)
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    capture = workflow.split("      - name: Pytest (full suite, including MCP)\n", 1)[1].split("      - name:", 1)[0]
    capture = textwrap.dedent(capture.split("        run: |\n", 1)[1])
    check = workflow.split("      - name: Zero-new-failure gate\n", 1)[1].split("\n", 1)[0].strip().removeprefix("run: ")
    artifacts = tmp_path / "ci"
    artifacts.mkdir()
    def local(command):
        return command.replace("${CI_ROOT}/venv/bin/python", sys.executable).replace("/tmp/", str(artifacts) + "/")
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTEST_ADDOPTS": "", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "TMPDIR": str(tmp_path), "CI_ROOT": str(artifacts)}
    result = subprocess.run(["bash", "-c", local(capture)], cwd=run["repo"], env=env, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (artifacts / "pytest-native.exit").read_text().strip() == str(native)
    result = subprocess.run(["bash", "-c", local(check)], cwd=run["repo"], env=env, text=True, capture_output=True, timeout=30)
    assert result.returncode == expected, result.stdout + result.stderr


@pytest.mark.parametrize("damage", [None, "missing", "hash", "fake-native", "fake-complete", "fake-count", "new-failure-masked"])
def test_reports_gate_revalidates_native_session_hash(run_pytest, tmp_path, monkeypatch, damage):
    source = "def test_known(): assert False\ndef test_pass(): pass\n"
    if damage == "new-failure-masked":
        source = "def test_unknown(): assert False\ndef test_pass(): pass\n"
    run = run_pytest(source)
    assert gate(run).returncode == int(damage == "new-failure-masked")
    reports = tmp_path / "reports"
    reports.mkdir()
    shutil.copy2(run["output"], reports / "pytest-output.txt")
    shutil.copy2(run["evidence"], reports / "pytest-evidence.json")
    summary = {"pytest": {"gate_exit": 0, "passed": 1, "failed": 1, "native_exit": 1, "complete": True}, "ruff": {"warnings": 0}, "bandit": {"high_vuln": 0}}
    if damage == "missing":
        (reports / "pytest-evidence.json").unlink()
    elif damage == "hash":
        (reports / "pytest-output.txt").write_text("damaged known output")
    elif damage == "fake-native":
        summary["pytest"]["native_exit"] = 0
    elif damage == "fake-complete":
        summary["pytest"]["complete"] = False
    elif damage == "fake-count":
        summary["pytest"]["passed"] = 100
    path = reports / "ci-summary.json"
    path.write_text(json.dumps(summary))
    spec = importlib.util.spec_from_file_location("strict_reports_under_test", ROOT / "scripts/agent_framework/check_reports.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "CI_SUMMARY", path)
    monkeypatch.setattr(module, "load_known_ids", lambda: {"tests/test_cases.py::test_known"})
    assert module.check_report_02_and_03()["test_pass"] == (damage is None)


@pytest.mark.parametrize("native", [2, 3, 4, 5, 130])
def test_abnormal_native_cannot_be_attested_as_complete(run_pytest, native):
    run = run_pytest("def test_known(): assert False\n")
    session = json.loads(run["session"].read_text())
    session["pytest_exit"] = native
    run["session"].write_text(json.dumps(session))
    run["native"].write_text(str(native))
    assert gate(run).returncode == 1


def test_stale_evidence_hash_rejects_even_if_result_counts_match(run_pytest):
    run = run_pytest("def test_known(): assert False\n")
    assert gate(run).returncode == 0
    run["output"].write_text("different run header\n" + run["output"].read_text())
    checked = subprocess.run([sys.executable, str(run["repo"] / "scripts/check_known_failures.py"), str(run["output"]), "--evidence-file", str(run["evidence"])], text=True, capture_output=True)
    assert checked.returncode == 1
    assert "hash" in checked.stdout


def test_exception_word_in_normal_captured_output_is_not_interruption(run_pytest):
    run = run_pytest("def test_pass(): print('KeyboardInterrupt is just a word')\n", "-s")
    assert gate(run).returncode == 0


def test_known_failure_and_pass_cannot_hide_setup_error(run_pytest):
    run = run_pytest("import pytest\n@pytest.fixture\ndef bad(): raise RuntimeError('setup')\ndef test_known(): assert False\ndef test_other(bad): pass\ndef test_pass(): pass\n")
    assert run["result"].returncode == 1
    session = json.loads(run["session"].read_text())
    assert session["counts"]["failed"] == session["counts"]["passed"] == session["counts"]["error"] == 1
    assert gate(run).returncode == 1


@pytest.mark.parametrize("reuse_request", ["unset", "empty-file", "empty-evidence"])
def test_local_reports_ignore_stale_defaults_without_explicit_reuse(run_pytest, tmp_path, reuse_request):
    run = run_pytest("def test_old(): pass\n")
    assert gate(run).returncode == 0
    repo = run["repo"]
    defaults = tmp_path / "sandbox-defaults"
    stale = defaults / "ci-pytest/pytest-output.txt"
    stale.parent.mkdir(parents=True)
    shutil.copy2(run["output"], stale)
    stale_evidence = Path(str(stale) + ".evidence.json")
    shutil.copy2(run["evidence"], stale_evidence)
    old_bytes = stale.read_bytes(), stale_evidence.read_bytes()
    script = repo / "scripts/agent_framework/generate_reports.sh"
    script.parent.mkdir(parents=True)
    script.write_text((ROOT / "scripts/agent_framework/generate_reports.sh").read_text().replace("/tmp/", str(defaults) + "/"))
    plugin = repo / "scripts/ci/pytest_evidence.py"
    plugin.parent.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/ci/pytest_evidence.py", plugin)
    (repo / ".gitignore").write_text("reports/\n.pytest_cache/\n**/__pycache__/\n")

    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=True).stdout.strip()

    git("init", "-q", "-b", "main")
    git("config", "user.name", "Isolated report test")
    git("config", "user.email", "report-test@example.invalid")
    git("add", ".")
    git("commit", "-qm", "old tested source")
    previous = git("rev-parse", "HEAD")
    marker = tmp_path / "fresh-child-ran"
    (repo / "tests/test_cases.py").write_text(
        "from pathlib import Path\n\n\ndef test_current_first():\n"
        f"    Path({str(marker)!r}).write_text('fresh')\n\n\n"
        "def test_current_second():\n    assert True\n"
    )
    git("add", "tests/test_cases.py")
    git("commit", "-qm", "current untested source")
    current = git("rev-parse", "HEAD")
    assert current != previous
    tools = tmp_path / "quality-bin"
    tools.mkdir()
    for tool in ("radon", "bandit"):
        stub = tools / tool
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
    env = {**os.environ, "PATH": f"{Path(sys.executable).parent}:{tools}:{os.environ['PATH']}",
           "PYTHONPATH": str(repo), "PYTEST_ADDOPTS": "", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
           "TMPDIR": str(tmp_path), "BASE_SHA": previous, "HEAD_SHA": current}
    env.pop("PYTEST_REUSE_FILE", None)
    env.pop("PYTEST_REUSE_EVIDENCE", None)
    if reuse_request == "empty-file":
        env["PYTEST_REUSE_FILE"] = ""
    elif reuse_request == "empty-evidence":
        env["PYTEST_REUSE_EVIDENCE"] = ""
    result = subprocess.run(["bash", str(script)], cwd=repo, env=env, text=True, capture_output=True, timeout=30)
    data = json.loads((repo / "reports/ci-summary.json").read_text())
    assert data["head_sha"] == current
    assert (stale.read_bytes(), stale_evidence.read_bytes()) == old_bytes
    if reuse_request == "unset":
        assert result.returncode == 0, result.stdout + result.stderr
        assert marker.exists(), "stale default pair must not suppress the actual current pytest child"
        assert data["pytest"] == {"passed": 2, "failed": 0, "gate_exit": 0, "native_exit": 0, "complete": True}
        assert "running pytest" in result.stdout and "复用 test job" not in result.stdout
    else:
        assert result.returncode == 1, result.stdout + result.stderr
        assert not marker.exists(), "explicit invalid request must fail without fallback"
        assert data["pytest"]["gate_exit"] == 1 and data["pytest"]["complete"] is False
        assert data["pytest"]["native_exit"] is None
    assert (repo / "reports/02-test-report.md").exists() and (repo / "reports/03-code-quality.md").exists()


@pytest.mark.parametrize("damage", [None, "head", "base", "run", "attempt", "event-sha", "downgrade", "malformed", "null"])
def test_bound_ci_handoff_rejects_other_candidate_or_run(run_pytest, tmp_path, damage):
    run = run_pytest("def test_known(): assert False\ndef test_pass(): pass\n")
    assert gate(run).returncode == 0
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    producer = tmp_path / "producer-binding.json"
    env = {**os.environ, "CI_REPOSITORY": "DUIDUI-Studio/DailyCheck", "CI_RUN_ID": "100", "CI_RUN_ATTEMPT": "2",
           "CI_EVENT": "pull_request", "CI_HEAD_SHA": head, "CI_BASE_SHA": head, "CI_EVENT_SHA": "e" * 40}
    bind = subprocess.run([sys.executable, str(ROOT / "scripts/ci/candidate_binding.py"), str(producer)], cwd=ROOT, env=env, capture_output=True, text=True)
    assert bind.returncode == 0, bind.stderr
    binding = json.loads(producer.read_text())
    assert binding["tested_sha"] == binding["CI_HEAD_SHA"] != binding["CI_EVENT_SHA"]
    job = tmp_path / "job-output"
    encoded = subprocess.run([sys.executable, str(SUMMARY), str(run["output"]), "--evidence-file", str(run["evidence"]), "--binding-file", str(producer), "--output-file", str(job)], capture_output=True, text=True)
    assert encoded.returncode == 0, encoded.stderr
    payload = json.loads(base64.b64decode(job.read_text().split("=", 1)[1]))
    assert payload["schema_version"] == 2 and payload["evidence"]["native_exit"] == 1
    expected = dict(binding)
    changed = {"head": "CI_HEAD_SHA", "base": "CI_BASE_SHA", "run": "CI_RUN_ID", "attempt": "CI_RUN_ATTEMPT", "event-sha": "CI_EVENT_SHA"}
    if damage in changed:
        key = changed[damage]
        expected[key] = "999" if key in {"CI_RUN_ID", "CI_RUN_ATTEMPT"} else "f" * 40
        if damage == "head":
            expected["tested_sha"] = expected[key]
    if damage == "downgrade":
        payload.pop("binding")
        payload["schema_version"] = 1
    if damage == "malformed":
        expected = {"unknown": "identity"}
    if damage == "null":
        expected = None
    consumer = tmp_path / "consumer-binding.json"
    consumer.write_text(json.dumps(expected))
    envelope = tmp_path / "envelope.json"
    envelope.write_text(json.dumps(payload))
    text, evidence = tmp_path / "consumer-out.txt", tmp_path / "consumer-evidence.json"
    decoded = subprocess.run([sys.executable, str(SUMMARY), "--decode", str(envelope), "--binding-file", str(consumer), "--text-output", str(text), "--evidence-output", str(evidence)], capture_output=True, text=True)
    assert (decoded.returncode == 0) == (damage is None), decoded.stdout + decoded.stderr
    if damage is None:
        validated = validate_evidence(text.read_text(), json.loads(evidence.read_text()))
        assert validated["failed"] == 1 and validated["passed"] == 1
    else:
        assert not text.exists() and not evidence.exists()


def test_binding_cli_rejects_unchecked_head(tmp_path):
    env = {**os.environ, "CI_REPOSITORY": "DUIDUI-Studio/DailyCheck", "CI_RUN_ID": "100", "CI_RUN_ATTEMPT": "1",
           "CI_EVENT": "pull_request", "CI_HEAD_SHA": "f"*40, "CI_BASE_SHA": "e"*40, "CI_EVENT_SHA": "f"*40}
    output = tmp_path / "binding.json"
    result = subprocess.run([sys.executable, str(ROOT / "scripts/ci/candidate_binding.py"), str(output)], cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 1 and not output.exists()
