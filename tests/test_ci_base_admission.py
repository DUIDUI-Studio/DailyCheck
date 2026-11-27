"""Exercise the inline GitHub PR admission step with a real checkout."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml"
ADMISSION = "Admit GitHub PR against current main"
REMOTE = "https://github.com/DUIDUI-Studio/DailyCheck.git"
REF = "refs/heads/main"


@pytest.fixture
def jobs():
    return yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)["jobs"]


@pytest.fixture
def admission(jobs):
    steps = jobs["test"]["steps"]
    names = [step["name"] for step in steps]
    index = names.index("Checkout candidate H on GitHub") + 1
    assert names[index] == ADMISSION
    return steps[index]


def test_admission_applies_only_to_github_pull_requests_to_main(jobs, admission):
    assert admission["if"] == (
        "github.server_url == 'https://github.com' && github.event_name == 'pull_request'"
        " && github.event.pull_request.base.ref == 'main'"
    )
    assert admission["env"]["PR_BASE_REF"] == "${{ github.event.pull_request.base.ref }}"
    assert admission["env"]["PR_HEAD_SHA"] == "${{ github.event.pull_request.head.sha }}"
    assert admission["env"]["PR_BASE_SHA"] == "${{ github.event.pull_request.base.sha }}"
    reports = jobs["agent-framework-reports"]
    assert all(step["name"] != ADMISSION for step in reports["steps"])
    assert reports["needs"] == "test"
    assert reports["steps"][0]["name"] == "Require successful complete upstream"


@pytest.fixture
def run_admission(tmp_path, admission):
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    git = shutil.which("git")
    command = [git, "-c", "core.hooksPath=/dev/null", "-C", str(workspace)]
    subprocess.run([*command, "init", "-q"], check=True)
    for commit in ("base", "candidate"):
        subprocess.run(
            [*command, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
             "commit", "--allow-empty", "-qm", commit], check=True,
        )
    head = subprocess.check_output([*command, "rev-parse", "HEAD"], text=True).strip()
    base = subprocess.check_output([*command, "rev-parse", "HEAD^"], text=True).strip()
    subprocess.run([*command, "update-ref", "refs/remotes/origin/main", base], check=True)
    ci_root = tmp_path / "job"
    ci_root.mkdir()
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "git"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = rev-parse ]; then exec "$REAL_GIT" "$@"; fi\n'
        'printf "%s\\n" "$@" > "$LOOKUP_LOG"\n'
        'test "$PWD" = "$CI_ROOT" || exit 99\n'
        'printf "%s" "$LOOKUP_OUTPUT"\n'
        'exit "$LOOKUP_STATUS"\n'
    )
    shim.chmod(0o755)
    log = tmp_path / "lookup"

    def run(*, output=None, status=0, **event):
        env = {
            **os.environ, "PATH": str(shim_dir) + os.pathsep + os.environ["PATH"],
            "REAL_GIT": git, "CI_ROOT": str(ci_root), "LOOKUP_LOG": str(log),
            "PR_HEAD_SHA": head, "PR_BASE_SHA": base, "PR_BASE_REF": "main",
            "PR_BASE_READ_TOKEN": "fixture-token",
            "LOOKUP_OUTPUT": f"{base}\t{REF}\n" if output is None else output.format(base=base),
            "LOOKUP_STATUS": str(status), **event,
        }
        result = subprocess.run(
            ["bash", "-e", "-c", admission["run"]], cwd=workspace, env=env,
            capture_output=True, text=True, timeout=25,
        )
        lookup = log.read_text().splitlines() if log.exists() else []
        return result, lookup, base

    return run


def test_fresh_matching_main_admits(run_admission):
    result, lookup, base = run_admission()
    assert result.returncode == 0, result.stderr
    assert lookup[-5:] == ["ls-remote", "--exit-code", "--refs", REMOTE, REF]
    assert base in result.stdout


def test_case_variant_base_does_not_query_main(run_admission):
    result, lookup, _ = run_admission(status=128, PR_BASE_REF="Main")
    assert result.returncode == 0 and not lookup


def test_stale_event_base_fails_even_when_cached_main_matches(run_admission):
    live = "921fab6d3d9528b27ced97b315e373cb65037f2f"
    result, lookup, base = run_admission(output=f"{live}\t{REF}\n")
    assert lookup and result.returncode != 0
    assert "stale event base" in result.stderr
    assert base in result.stderr and live in result.stderr


@pytest.mark.parametrize(("output", "status", "diagnostic"), [
    (None, 128, "lookup failed"),
    ("", 2, "lookup failed"),
    ("", 0, "invalid main ref response"),
    ("not-a-sha\trefs/heads/main\n", 0, "invalid main ref response"),
    ("{base}\trefs/heads/other\n", 0, "invalid main ref response"),
    ("{base}\trefs/heads/main\n" * 2, 0, "invalid main ref response"),
])
def test_unusable_lookup_fails_closed(run_admission, output, status, diagnostic):
    result, lookup, _ = run_admission(output=output, status=status)
    assert lookup and result.returncode != 0
    assert diagnostic in result.stderr


@pytest.mark.parametrize(("event", "diagnostic"), [
    ({"PR_HEAD_SHA": "1" * 40}, "checkout HEAD does not match"),
    ({"PR_HEAD_SHA": ""}, "invalid event H/B"),
    ({"PR_BASE_SHA": "xyz"}, "invalid event H/B"),
    ({"PR_BASE_SHA": "0" * 40}, "invalid event H/B"),
])
def test_invalid_candidate_fails_before_lookup(run_admission, event, diagnostic):
    result, lookup, _ = run_admission(**event)
    assert not lookup and result.returncode != 0
    assert diagnostic in result.stderr
