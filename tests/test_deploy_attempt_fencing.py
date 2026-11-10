"""Real pipeline/CLI children; pipes pin interleavings, all effects stay in tmp_path."""
from __future__ import annotations

import fcntl
import json
import os
import select
import shutil
import signal
import sqlite3
import subprocess
import sys
from contextlib import contextmanager, suppress

import pytest

from tests.test_deploy_pipeline import (
    BG_DIR,
    SHA,
    _calls,
    _cand_env,
    _ds,
    _env,
    _head,
    _pipe,
    _state,
)
from tests.test_deploy_pipeline import (
    candidate_sandbox as candidate_sandbox,
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
    # Serialize the replacement with the real heartbeat writer, so a beat that
    # started earlier cannot overwrite our successor state after the marker.
    with (sb["state"] / ".state.lock").open("a+") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        state = _state(sb)
        state["lock"]["token"] = "successor-token"
        state["lock"]["heartbeat_at"] = 2000000000
        (sb["state"] / "state.json").write_text(json.dumps(state))
        (sb["state"] / "lock.token").write_text("successor-token\n")
        return state["lock"]


def _report_closure(sb):
    """Supply the real finite import closure even when the reporter is a port."""
    for name in ("deploy_triage_pure.py", "publication_authority.py"):
        shutil.copy2(BG_DIR / name, sb["dir"] / name)
    shutil.copy2(BG_DIR.parent / "hosting.py", sb["tmp"] / "scripts/hosting.py")


def _interpreter_port(sb, code):
    """Fault/gate the actual helper process, keeping its snapshot bytes intact."""
    port = sb["tmp"] / "interpreter-port"
    port.write_text(
        f"#!{sys.executable}\nimport os, sys\nfrom pathlib import Path\n"
        + code
        + f"os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])\n"
    )
    port.chmod(0o700)
    return str(port)


@contextmanager
def held_pipeline(sb, stage="up.sh", observe_min="0", **extra):
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
         "--observe-min", observe_min, "--heartbeat-interval", "1"],
        env=_env(sb, READY_FD=ready_w, GATE_FD=gate_r, **extra),
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
        if proc.poll() is None:
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


@pytest.mark.parametrize("stage", ["up.sh", "verify.sh", "promote.sh", "switch.sh"])
def test_paused_stale_pipeline_excludes_second_normal_attempt(sandbox, stage):
    with held_pipeline(sandbox, stage) as (first, gate):
        os.killpg(first.pid, signal.SIGSTOP)
        st = _state(sandbox)
        token = st["lock"]["token"]
        st["lock"]["heartbeat_at"] = 0
        (sandbox["state"] / "state.json").write_text(json.dumps(st))
        second = _pipe(sandbox, "43", "f" * 40, "--observe-min", "0")
        assert first.poll() is None, "paused predecessor is still alive"
        assert second.returncode == 10, second.stdout + second.stderr
        assert _state(sandbox)["lock"]["token"] == token
        assert not any(c.startswith("up.sh 43") for c in _calls(sandbox))
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
    python = _interpreter_port(
        sandbox,
        'if len(sys.argv) > 2 and Path(sys.argv[1]).name == "deploy_state.py" '
        'and sys.argv[2] == "release": sys.exit(11)\n',
    )
    _report_closure(sandbox)
    reporter = sandbox["dir"] / "deploy_report.py"
    reporter.write_text(
        'import json, sys\n'
        'from pathlib import Path\n'
        f'Path({str(sandbox["tmp"] / "report-args")!r}).write_text(json.dumps(sys.argv))\n'
    )
    proc = _pipe(sandbox, "42", SHA, "--observe-min", "0", DAILYCHECK_DEPLOY_REPORT="1",
                 PYTHON=python)
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


@pytest.mark.parametrize("stage", ["up.sh", "verify.sh", "promote.sh", "switch.sh"])
def test_real_stage_rechecks_original_attempt_after_entry_gap(sandbox, stage):
    shutil.copy2(BG_DIR / stage, sandbox["dir"] / stage)
    with held_pipeline(sandbox, stage, DAILYCHECK_DEPLOY_UNLOCKED="1") as (proc, gate):
        successor = _replace_owner(sandbox)
        out, err = _resume(proc, gate)
        assert proc.returncode != 0, out + err
        assert "attempt ownership lost" in (sandbox["tmp"] / "pipeline.log").read_text()
        assert _state(sandbox)["lock"] == successor
        assert _state(sandbox)["deployed"] is None


def test_clear_lease_does_not_release_live_process_fence(sandbox):
    with held_pipeline(sandbox) as (proc, gate):
        os.killpg(proc.pid, signal.SIGSTOP)
        assert _ds(sandbox, "clear-lock", "--yes").returncode == 0
        assert _ds(sandbox, "acquire", "--pr", "43", "--sha", SHA).returncode == 10
        second = _pipe(sandbox, "43", SHA, "--observe-min", "0")
        assert second.returncode == 10
        assert _state(sandbox)["lock"] is None
        out, err = _resume(proc, gate)
        assert proc.returncode != 0, out + err
        assert _state(sandbox)["deployed"] is None


def test_process_tree_exit_allows_stale_recovery(sandbox):
    with held_pipeline(sandbox) as (proc, _gate):
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate(timeout=10)
        assert proc.returncode == -signal.SIGKILL
    st = _state(sandbox)
    st["lock"]["heartbeat_at"] = 0
    (sandbox["state"] / "state.json").write_text(json.dumps(st))
    recovered = _pipe(sandbox, "43", "f" * 40, "--observe-min", "0")
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert _state(sandbox)["last_evicted"]["pr"] == 42
    assert _state(sandbox)["deployed"]["sha"] == "f" * 40


def test_release_real_anchor_io_error_is_failure(sandbox):
    python = _interpreter_port(
        sandbox,
        'if len(sys.argv) > 2 and Path(sys.argv[1]).name == "deploy_state.py" '
        'and sys.argv[2] == "release":\n'
        '    (Path(os.environ["DAILYCHECK_DEPLOY_STATE_DIR"]) / '
        'f".state.json.tmp-{os.getpid()}").mkdir()\n',
    )
    proc = _pipe(sandbox, "42", SHA, "--observe-min", "0", PYTHON=python)
    assert proc.returncode == 27, proc.stdout + proc.stderr
    assert "IsADirectoryError" in (sandbox["tmp"] / "pipeline.log").read_text()
    assert _state(sandbox)["lock"]["pr"] == 42
    assert _state(sandbox)["deployed"] is None
    assert "kind=release-failed" in (sandbox["state"] / "audit.log").read_text()
    assert "全链完成" not in proc.stdout


def test_emergency_rollback_and_blue_switch_ignore_normal_attempt_lock(sandbox):
    green = sandbox["tmp"] / "green-db"
    green.mkdir()
    with sqlite3.connect(green / "evidence.db") as db:
        db.execute("create table evidence (id integer)")
        db.execute("insert into evidence values (161)")
    evidence = sandbox["tmp"] / "evidence"
    shutil.copy2(BG_DIR / "rollback.sh", sandbox["dir"] / "rollback.sh")
    shutil.copy2(BG_DIR / "down.sh", sandbox["dir"] / "down.sh")
    conf = sandbox["tmp"] / "old-layout.conf"
    conf.write_text("server { location / { proxy_pass http://dailycheck-green; } }\n")
    with held_pipeline(sandbox) as (proc, gate):
        original = _state(sandbox)["lock"]["token"]
        env = _env(
            sandbox, DAILYCHECK_DEPLOY_ATTEMPT="invalid-original-identity",
            DAILYCHECK_BG_LOCK_HELD="1", NGINX_DAILYCHECK_CONF=conf,
            BACKUP_DIR=sandbox["tmp"] / "backups", GREEN_DB=green,
            EVIDENCE_BASE=evidence,
        )
        rollback = subprocess.run(
            ["bash", str(sandbox["dir"] / "rollback.sh")],
            env=env, capture_output=True, text=True, timeout=10,
        )
        assert rollback.returncode == 0, rollback.stdout + rollback.stderr
        snapshot = next(evidence.glob("rollback-*/db/evidence.db"))
        with sqlite3.connect(snapshot) as db:
            assert db.execute("select id from evidence").fetchone() == (161,)
        blue = subprocess.run(
            ["bash", str(BG_DIR / "switch.sh"), "blue", "100"],
            env=env, capture_output=True, text=True, timeout=10,
        )
        assert blue.returncode == 2, blue.stdout + blue.stderr  # layout stop, past lock guard
        down = subprocess.run(
            ["bash", str(sandbox["dir"] / "down.sh"), "--dry-run"],
            env=env, capture_output=True, text=True, timeout=10,
        )
        assert down.returncode == 0
        assert _state(sandbox)["lock"]["token"] == original
        out, err = _resume(proc, gate)
        assert proc.returncode == 0, out + err


@pytest.mark.parametrize("phase", ["status", "release", "writeback"])
def test_process_fence_spans_setup_release_and_writeback(sandbox, phase):
    if phase == "writeback":
        target = sandbox["dir"] / "deploy_report.py"
        target.write_text("import sys\n")
        _report_closure(sandbox)
    else:
        target = sandbox["dir"] / "deploy_state.py"
    python = _interpreter_port(
        sandbox,
        f'if len(sys.argv) > 2 and Path(sys.argv[1]).name == {target.name!r} '
        f'and sys.argv[2] == {("write" if phase == "writeback" else phase)!r} '
        'and os.environ.get("READY_FD"):\n'
        '    os.write(int(os.environ["READY_FD"]), b"R")\n'
        '    assert os.read(int(os.environ["GATE_FD"]), 1) == b"G"\n',
    )
    # Use a no-op held stage to share the pipe/process cleanup harness; only the
    # chosen state CLI or reporter consumes the gate in this test.
    placeholder = sandbox["dir"] / "unused.sh"
    placeholder.write_text("#!/bin/sh\nexit 0\n")
    with held_pipeline(sandbox, "unused.sh", DAILYCHECK_DEPLOY_REPORT="1",
                       PYTHON=python) as (proc, gate):
        if phase == "status":
            assert _state(sandbox) == {}
        if phase == "writeback":
            assert _state(sandbox)["lock"] is None
            assert _state(sandbox)["deployed"]["sha"] == SHA
        second = _pipe(sandbox, "43", "f" * 40, "--observe-min", "0")
        assert second.returncode == 10, second.stdout + second.stderr
        out, err = _resume(proc, gate)
        assert proc.returncode == 0, out + err
        assert _state(sandbox)["deployed"]["sha"] == SHA


def test_process_fence_covers_observation(sandbox):
    curl = sandbox["dir"] / "observe-curl"
    curl.write_text("#!/bin/sh\nprintf 302\n")
    curl.chmod(0o755)
    jctl = sandbox["tmp"] / "journalctl-empty"
    jctl.write_text("#!/bin/sh\nexit 0\n")
    jctl.chmod(0o755)
    with held_pipeline(
        sandbox, "observe-curl", observe_min="5",
        DAILYCHECK_PIPELINE_CURL=curl, DAILYCHECK_PIPELINE_JOURNALCTL=jctl,
    ) as (proc, gate):
        os.killpg(proc.pid, signal.SIGSTOP)
        st = _state(sandbox)
        st["lock"]["heartbeat_at"] = 0
        (sandbox["state"] / "state.json").write_text(json.dumps(st))
        second = _pipe(sandbox, "43", "f" * 40, "--observe-min", "0")
        assert second.returncode == 10
        out, err = _resume(proc, gate)
        assert proc.returncode == 0, out + err


def test_release_failure_keeps_successful_local_merge_facts(candidate_sandbox):
    sb = candidate_sandbox
    python = _interpreter_port(
        sb,
        'if len(sys.argv) > 2 and Path(sys.argv[1]).name == "deploy_state.py" '
        'and sys.argv[2] == "release": sys.exit(11)\n',
    )
    proc = _pipe(
        sb, "42", sb["sha"], "--candidate", "--merge-pr", "--observe-min", "0",
        **_cand_env(sb, PYTHON=python),
    )
    assert proc.returncode == 27, proc.stdout + proc.stderr
    assert _head(sb) == sb["sha"]
    remote = subprocess.run(
        ["git", "--git-dir", str(sb["origin"]), "rev-parse", "main"],
        capture_output=True, text=True, check=True,
    )
    assert remote.stdout.strip() == sb["sha"]
    assert "FF 合并完成" in (sb["tmp"] / "pipeline.log").read_text()
    assert "merge_helper merge" in "\n".join(_calls(sb))
    assert _state(sb)["deployed"] is None
    assert "全链完成" not in proc.stdout


def test_shared_token_file_change_alone_cannot_change_pipeline_identity(sandbox):
    with held_pipeline(sandbox) as (proc, gate):
        (sandbox["state"] / "lock.token").write_text("unrelated-token\n")
        out, err = _resume(proc, gate)
        assert proc.returncode == 0, out + err
        assert _state(sandbox)["deployed"]["sha"] == SHA
        assert _state(sandbox)["lock"] is None


def test_invalid_process_descriptor_is_not_a_lock_bypass(sandbox):
    proc = _pipe(
        sandbox, "42", SHA, "--observe-min", "0", DAILYCHECK_DEPLOY_ATTEMPT_FD="9999",
    )
    assert proc.returncode == 11
    assert _calls(sandbox) == []
    assert _state(sandbox) == {}


def test_corrupt_old_state_is_recovered_without_corrupting_acquire_identity(sandbox):
    (sandbox["state"] / "state.json").write_text("{broken")
    proc = _pipe(sandbox, "42", SHA, "--observe-min", "0")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _state(sandbox)["deployed"]["sha"] == SHA
    assert list(sandbox["state"].glob("state.json.corrupt-*"))
