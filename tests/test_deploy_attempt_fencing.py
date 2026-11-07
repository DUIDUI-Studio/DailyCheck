"""Real pipeline/CLI children; pipes pin interleavings, all effects stay in tmp_path."""
from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
from contextlib import contextmanager, suppress

import pytest

from tests.test_deploy_pipeline import (
    SHA,
    _calls,
    _ds,
    _env,
    _pipe,
    _state,
)
from tests.test_deploy_pipeline import (
    sandbox as sandbox,
)


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("PYTHON", sys.executable)
    monkeypatch.setenv("PYTHONUNBUFFERED", "1")
    monkeypatch.setenv("DAILYCHECK_DEPLOY_REPORT", "0")
    monkeypatch.setenv("DAILYCHECK_DASHBOARD_HOOK", "0")
    monkeypatch.setenv("DAILYCHECK_BG_LOCK_FILE", str(tmp_path / "bg.lock"))
    monkeypatch.setenv("DAILYCHECK_PROD_AGENT_INBOX", str(tmp_path / "inbox"))
    monkeypatch.setenv("DAILYCHECK_GITEA_TOKEN_FILE", str(tmp_path / "absent-token"))
    bindir = tmp_path / "deny-host"
    bindir.mkdir()
    for command in ("systemctl", "nginx", "curl", "journalctl", "ssh"):
        path = bindir / command
        path.write_text("#!/bin/sh\nexit 99\n")
        path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")


def _replace_owner(sb):
    state = _state(sb)
    state["lock"]["token"] = "successor-token"
    state["lock"]["heartbeat_at"] = 2000000000
    (sb["state"] / "state.json").write_text(json.dumps(state))
    (sb["state"] / "lock.token").write_text("successor-token\n")
    return state["lock"]


@contextmanager
def held_pipeline(sb, stage="up.sh"):
    """A real stage emits READY then blocks in read(2), with no timing guesses."""
    ready_r, ready_w = os.pipe()
    gate_r, gate_w = os.pipe()
    hold = sb["tmp"] / "hold.py"
    hold.write_text(
        "import os\n"
        "os.write(int(os.environ['READY_FD']), b'R')\n"
        "assert os.read(int(os.environ['GATE_FD']), 1) == b'G'\n"
    )
    script = sb["dir"] / stage
    original = script.read_text()
    script.write_text(
        '#!/usr/bin/env bash\n'
        'if [[ -n "${READY_FD:-}" ]]; then\n'
        f'  "{sys.executable}" "{hold}" || exit 99\n'
        'fi\n' + original
    )
    proc = subprocess.Popen(
        ["bash", str(sb["dir"] / "deploy_pipeline.sh"), "42", SHA,
         "--observe-min", "0", "--heartbeat-interval", "1"],
        env=_env(sb, READY_FD=ready_w, GATE_FD=gate_r),
        pass_fds=(ready_w, gate_r), start_new_session=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    os.close(ready_w)
    os.close(gate_r)
    try:
        assert select.select([ready_r], [], [], 10)[0], "stage never became ready"
        assert os.read(ready_r, 1) == b"R"
        yield proc, gate_w
    finally:
        with suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGCONT)
            os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate(timeout=10)
        os.close(ready_r)
        os.close(gate_w)


def _resume(proc, gate):
    os.killpg(proc.pid, signal.SIGCONT)
    os.write(gate, b"G")
    return proc.communicate(timeout=15)


def test_paused_stale_pipeline_excludes_second_normal_attempt(sandbox):
    with held_pipeline(sandbox) as (first, gate):
        os.killpg(first.pid, signal.SIGSTOP)
        st = _state(sandbox)
        token = st["lock"]["token"]
        st["lock"]["heartbeat_at"] = 0
        (sandbox["state"] / "state.json").write_text(json.dumps(st))
        second = _pipe(sandbox, "43", "f" * 40, "--observe-min", "0")
        assert first.poll() is None, "paused predecessor is still alive"
        assert second.returncode == 10, second.stdout + second.stderr
        assert _state(sandbox)["lock"]["token"] == token
        assert _calls(sandbox) == []
        out, err = _resume(first, gate)
        assert first.returncode == 0, out + err
        assert _state(sandbox)["deployed"]["sha"] == SHA


def test_same_pr_successor_token_cannot_be_adopted(sandbox):
    with held_pipeline(sandbox) as (proc, gate):
        successor = _replace_owner(sandbox)
        out, err = _resume(proc, gate)
        assert proc.returncode != 0, out + err
        assert [c.split()[0] for c in _calls(sandbox)] == ["up.sh"]
        assert _state(sandbox)["lock"] == successor
        assert _state(sandbox)["deployed"] is None


def test_authoritative_release_failure_never_reports_success(sandbox):
    ds = sandbox["dir"] / "deploy_state.py"
    ds.rename(sandbox["dir"] / "real_state.py")
    ds.write_text(
        'import runpy, sys\n'
        'from pathlib import Path\n'
        'if len(sys.argv) > 1 and sys.argv[1] == "release": sys.exit(11)\n'
        'runpy.run_path(str(Path(__file__).with_name("real_state.py")), run_name="__main__")\n'
    )
    reporter = sandbox["dir"] / "deploy_report.py"
    reporter.write_text(
        'import json, sys\n'
        'from pathlib import Path\n'
        f'Path({str(sandbox["tmp"] / "report-args")!r}).write_text(json.dumps(sys.argv))\n'
    )
    proc = _pipe(sandbox, "42", SHA, "--observe-min", "0", DAILYCHECK_DEPLOY_REPORT="1")
    assert proc.returncode != 0, proc.stdout + proc.stderr
    args = json.loads((sandbox["tmp"] / "report-args").read_text())
    assert args[args.index("--rc") + 1] != "0"
    assert _state(sandbox)["deployed"] is None
    assert "全链完成" not in proc.stdout


def test_full_sha_collision_does_not_skip_deployment(sandbox):
    other = SHA[:12] + "0" * 28
    assert _ds(sandbox, "init", "--sha", other).returncode == 0
    proc = _pipe(sandbox, "42", SHA, "--observe-min", "0")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert [c.split()[0] for c in _calls(sandbox)] == [
        "up.sh", "verify.sh", "promote.sh", "switch.sh",
    ]
    assert _state(sandbox)["deployed"]["sha"] == SHA


def test_heartbeat_loop_pins_token_once(sandbox):
    assert _ds(sandbox, "acquire", "--pr", "42", "--sha", SHA).returncode == 0
    proc = subprocess.Popen(
        [sys.executable, str(sandbox["dir"] / "deploy_state.py"),
         "heartbeat", "--loop", "--interval", "1", "--max-beats", "2"],
        env=_env(sandbox), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert select.select([proc.stdout], [], [], 10)[0]
        assert json.loads(proc.stdout.readline())["ok"]
        successor = _replace_owner(sandbox)
        out, err = proc.communicate(timeout=10)
        assert proc.returncode == 11, out + err
        assert _state(sandbox)["lock"] == successor
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
