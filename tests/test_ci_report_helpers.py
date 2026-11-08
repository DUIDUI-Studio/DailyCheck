"""Report-helper regressions use actual Git/Ruff and explicit pytest DATA reuse."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.check_known_failures import make_evidence

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def report_repo(tmp_path):
    assert shutil.which("ruff"), "report-helper tests require the CI-pinned Ruff"
    repo = tmp_path / "repo"
    (repo / "scripts/agent_framework").mkdir(parents=True)
    (repo / "tests").mkdir()
    script = repo / "scripts/agent_framework/generate_reports.sh"
    script.write_text((ROOT / script.relative_to(repo)).read_text().replace("/tmp/", f"{tmp_path}/"))
    for name in ("scripts/check_known_failures.py", "tests/known_failures.txt"):
        shutil.copyfile(ROOT / name, repo / name)
    (repo / "pyproject.toml").write_text('[tool.ruff.lint]\nselect = ["E", "F"]\n')
    filenames = ["first.py", "second file.py", "line\nbreak.py", "-option.py"]
    for name in [*filenames, "deleted.py"]:
        (repo / name).write_text("value = 1\n")

    def git(*args):
        command = ["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=report-fixture",
                   "-c", "user.email=report@example.invalid", "-C", str(repo), *args]
        return subprocess.check_output(command, text=True).strip()

    git("init", "-q")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    for name in filenames:
        (repo / name).write_text("value = 2\n")
    (repo / "deleted.py").unlink()
    git("add", "-A")
    git("commit", "-qm", "changed Python files and deletion")
    # An unrelated untracked file must not enter the changed-file lint selection.
    (repo / "unrelated.py").write_text("invalid python !\n")
    text = "1 passed in 0.01s\n"
    session = {"schema_version": 1, "pytest_exit": 0, "selected": 1, "selected_unique": 1,
               "completed": 1, "uncompleted": [], "unexpected": [], "error_nodeids": [],
               "failed_nodeids": [], "counts": {"passed": 1, "failed": 0, "skipped": 0,
                                                   "xfailed": 0, "xpassed": 0, "error": 0}}
    evidence = make_evidence(text, 0, session)
    output = tmp_path / "upstream.txt"
    proof = tmp_path / "upstream.evidence.json"
    output.write_text(text)
    proof.write_text(json.dumps(evidence))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    python = bin_dir / "python3"
    invoked = tmp_path / "forbidden-pytest"
    python.write_text(
        f"#!{sys.executable}\nimport os, sys\nfrom pathlib import Path\n"
        "if sys.argv[1:3] == ['-m', 'pytest']:\n"
        f"    Path({str(invoked)!r}).touch()\n    sys.exit(99)\n"
        f"os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])\n"
    )
    python.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
           "BASE_SHA": base, "DAILYCHECK_CI_TMP": str(tmp_path), "TMPDIR": str(tmp_path),
           "PYTEST_REUSE_FILE": str(output), "PYTEST_REUSE_EVIDENCE": str(proof)}

    def run():
        result = subprocess.run(["bash", str(script)], cwd=repo, env=env,
                                capture_output=True, text=True, timeout=30)
        reports = repo / "reports"
        assert not invoked.exists(), "report reuse must never launch pytest"
        assert (reports / "pytest-output.txt").read_text() == text
        assert json.loads((reports / "pytest-evidence.json").read_text()) == evidence
        assert not (tmp_path / "ruff-files-af.txt").exists()
        return result, json.loads((reports / "ci-summary.json").read_text())

    return SimpleNamespace(repo=repo, git=git, base=base, env=env, run=run)


def test_report_lints_separate_paths_with_spaces_newlines_and_option_prefix(report_repo):
    result, summary = report_repo.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert summary["ruff"]["warnings"] == 0
    report = (report_repo.repo / "reports/03-code-quality.md").read_text()
    assert json.loads(report.split("```", 2)[1]) == []


@pytest.mark.parametrize("damage,expected,warnings", [
    ("violation", 1, 1), ("missing-file", 1, 1), ("invalid-ruff", 2, None),
    ("invalid-base", 2, None),
])
def test_report_preserves_real_ruff_failures(report_repo, damage, expected, warnings):
    repo = report_repo.repo
    if damage == "violation":
        (repo / "first.py").write_text("import os\n")
    elif damage == "missing-file":
        (repo / "first.py").unlink()
    elif damage == "invalid-ruff":
        (repo / "pyproject.toml").write_text('[tool.ruff.lint]\nselect = ["NOT_A_RULE"]\n')
    else:
        report_repo.env["BASE_SHA"] = "missing-base-ref"
    result, summary = report_repo.run()
    assert result.returncode == expected, result.stdout + result.stderr
    assert summary["ruff"]["warnings"] == warnings
    report = (repo / "reports/03-code-quality.md").read_text()
    assert "⚠️" in report
    if damage == "missing-file":
        assert "E902" in report
    if damage == "violation":
        assert "F401" in report


def test_report_no_changed_python_paths_is_clean(report_repo):
    report_repo.env["BASE_SHA"] = report_repo.git("rev-parse", "HEAD")
    result, summary = report_repo.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert summary["ruff"]["warnings"] == 0
    assert "no python files changed" in (report_repo.repo / "reports/03-code-quality.md").read_text()


@pytest.mark.parametrize("fail", [False, True])
def test_preview_cleans_isolated_database_on_success_and_assertion(tmp_path, monkeypatch, fail):
    path = ROOT / "scripts/dev_smoke_t11_e2e.py"
    spec = importlib.util.spec_from_file_location("report_smoke_cleanup", path)
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    database_dir = tmp_path / "isolated-db"
    database_dir.mkdir()
    monkeypatch.setattr(smoke, "_make_test_env", lambda: (database_dir, database_dir / "master.db"))
    client = object()
    app = SimpleNamespace(config={}, test_client=lambda: client)
    monkeypatch.setitem(sys.modules, "app", SimpleNamespace(create_app=lambda: app))
    failure = AssertionError("observable smoke failure")

    def exercise(actual_client, master):
        assert actual_client is client and master == database_dir / "master.db"
        if fail:
            raise failure

    monkeypatch.setattr(smoke, "_exercise_flow", exercise)
    if fail:
        with pytest.raises(AssertionError) as caught:
            smoke.e2e_smoke()
        assert caught.value is failure
    else:
        smoke.e2e_smoke()
    assert not database_dir.exists()
