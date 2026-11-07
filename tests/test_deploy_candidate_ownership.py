"""Candidate checkout must be ordered with supported deployment revocation."""
from __future__ import annotations

import fcntl
import json
import os
import select
import shutil
import signal
import subprocess
import sys
from contextlib import contextmanager, suppress

import pytest

from tests.test_deploy_attempt_fencing import isolated as isolated
from tests.test_deploy_pipeline import _calls, _cand_env, _ds, _env, _head, _state
from tests.test_deploy_pipeline import candidate_sandbox as candidate_sandbox
from tests.test_deploy_pipeline import sandbox as sandbox


def _git(sb, *args):
    return subprocess.check_output(["git", "-C", str(sb["repo"]), *args], text=True).strip()


@contextmanager
def candidate_paused(sb, phase, **overrides):
    """Gate a real helper return or the restore fetch, outside any state mutation."""
    ready_r, ready_w = os.pipe()
    gate_r, gate_w = os.pipe()
    extra = _cand_env(sb)
    gate = (
        'os.write(int(os.environ["READY_FD"]), b"R")\n'
        'assert os.read(int(os.environ["GATE_FD"]), 1) == b"G"\n'
    )
    if phase in ("check", "merge"):
        real_helper = sb["helper"].with_name("real_merge.py")
        sb["helper"].rename(real_helper)
        sb["helper"].write_text(
            'import os, runpy, sys\n'
            'rc = 0\n'
            'try:\n'
            f'    runpy.run_path({str(real_helper)!r}, run_name="__main__")\n'
            'except SystemExit as exc:\n'
            '    rc = exc.code\n'
            f'if sys.argv[1] == {phase!r}:\n'
            + ''.join('    ' + line + '\n' for line in gate.splitlines())
            + 'sys.exit(rc)\n'
        )
    else:
        bindir = sb["tmp"] / "git-fixture"
        bindir.mkdir()
        real_git = shutil.which("git")
        shim = bindir / "git"
        shim.write_text(
            f'#!{sys.executable}\n'
            'import os, subprocess, sys\nfrom pathlib import Path\n'
            'args = sys.argv[1:]\n'
            'if "fetch" in args and args[-1] == "main":\n'
            f'    counter = Path({str(sb["tmp"] / "fetch-count")!r})\n'
            '    count = int(counter.read_text()) + 1 if counter.exists() else 1\n'
            '    counter.write_text(str(count))\n'
            '    if count == 2:\n'
            + ''.join('        ' + line + '\n' for line in gate.splitlines())
            + f'sys.exit(subprocess.call([{real_git!r}, *args]))\n'
        )
        shim.chmod(0o755)
        extra["PATH"] = f"{bindir}:{os.environ['PATH']}"
        if phase == "cleanup-fetch":
            (sb["dir"] / "up.sh").write_text('#!/bin/sh\nexit 7\n')
    extra.update(overrides)
    proc = subprocess.Popen(
        ["bash", str(sb["dir"] / "deploy_pipeline.sh"), "42", sb["sha"],
         "--candidate", "--merge-pr", "--observe-min", "0", "--heartbeat-interval", "1"],
        env=_env(sb, **extra, READY_FD=ready_w, GATE_FD=gate_r),
        pass_fds=(ready_w, gate_r), start_new_session=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    os.close(ready_w)
    os.close(gate_r)
    try:
        assert select.select([ready_r], [], [], 15)[0], "candidate helper did not reach gate"
        assert os.read(ready_r, 1) == b"R"
        yield proc, gate_w
    finally:
        if proc.poll() is None:
            with suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate(timeout=10)
        os.close(ready_r)
        os.close(gate_w)


def _successor_tree(sb):
    _git(sb, "checkout", "-qb", "successor-tree")
    (sb["repo"] / "app.py").write_text("successor\n")
    _git(sb, "commit", "-qam", "successor")
    head = _head(sb)
    _git(sb, "checkout", "-q", "main")
    return head


def _revoke_and_move(sb, successor, replacement):
    # Use the supported revocation command, with no arbitrary lock-inode changes.
    assert _ds(sb, "clear-lock", "--yes").returncode == 0
    if replacement == "same-pr-successor":
        with (sb["state"] / ".state.lock").open("a+") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            st = _state(sb)
            st["lock"] = {
                "pr": 42, "sha": successor, "session": "successor-session",
                "token": "successor-token", "heartbeat_at": 2000000000,
                "acquired_at": 2000000000,
            }
            st["deployed"] = {"sha": sb["base"], "pr": 41, "at": 2000000000}
            (sb["state"] / "state.json").write_text(json.dumps(st))
            (sb["state"] / "lock.token").write_text("successor-token\n")
    _git(sb, "checkout", "-q", "successor-tree")
    return _state(sb)


@pytest.mark.parametrize("phase", ["check", "merge"])
@pytest.mark.parametrize("replacement", ["clear-only", "same-pr-successor"])
def test_revoked_candidate_preserves_successor_checkout(candidate_sandbox, phase, replacement):
    sb = candidate_sandbox
    successor = _successor_tree(sb)
    report_args = sb["tmp"] / "report-args"
    (sb["dir"] / "deploy_report.py").write_text(
        'import json, sys\nfrom pathlib import Path\n'
        f'Path({str(report_args)!r}).write_text(json.dumps(sys.argv))\n'
    )
    with candidate_paused(sb, phase, DAILYCHECK_DEPLOY_REPORT="1") as (proc, gate):
        expected_state = _revoke_and_move(sb, successor, replacement)
        before = _head(sb)
        before_calls = _calls(sb)
        os.write(gate, b"G")
        out, err = proc.communicate(timeout=15)
        after = _head(sb)
        remote = _git(sb, "--git-dir", str(sb["origin"]), "rev-parse", "main")
        print(json.dumps({"phase": phase, "replacement": replacement, "before": before,
                          "after": after, "remote": remote, "candidate": sb["sha"],
                          "rc": proc.returncode}))
        assert proc.returncode != 0, out + err
        assert after == before == successor, "revoked candidate replaced successor checkout"
        assert _git(sb, "branch", "--show-current") == "successor-tree"
        assert (sb["repo"] / "app.py").read_text() == "successor\n"
        assert _calls(sb) == before_calls, "revoked candidate entered a later normal stage"
        assert _state(sb)["lock"] == expected_state["lock"]
        assert _state(sb)["deployed"] == expected_state["deployed"]
        args = json.loads(report_args.read_text())
        assert args[args.index("--rc") + 1] == "28"
        assert "kind=ownership-lost" in (sb["state"] / "audit.log").read_text()
        assert remote == (sb["sha"] if phase == "merge" else sb["base"])
        if phase == "merge":
            log = (sb["tmp"] / "pipeline.log").read_text()
            assert "FF 合并完成" in log, "completed remote merge must remain a recorded fact"
            assert "全链完成" not in out


@pytest.mark.parametrize("phase", ["restore-fetch", "cleanup-fetch"])
def test_revocation_during_restore_fetch_preserves_successor_checkout(candidate_sandbox, phase):
    sb = candidate_sandbox
    successor = _successor_tree(sb)
    with candidate_paused(sb, phase) as (proc, gate):
        expected_state = _revoke_and_move(sb, successor, "clear-only")
        os.write(gate, b"G")
        out, err = proc.communicate(timeout=15)
        print(json.dumps({"phase": phase, "successor": successor,
                          "after": _head(sb), "rc": proc.returncode}))
        assert proc.returncode != 0, out + err
        assert _head(sb) == successor
        assert _git(sb, "branch", "--show-current") == "successor-tree"
        assert _state(sb)["lock"] == expected_state["lock"]
        assert _state(sb)["deployed"] is None
        if phase == "cleanup-fetch":
            assert proc.returncode == 20, "cleanup revocation must preserve the up failure"


@pytest.mark.parametrize("phase,helper_rc,expected_rc", [("check", "31", 25), ("merge", "30", 26)])
def test_revoked_failed_helper_keeps_original_failure_without_stale_recovery(
    candidate_sandbox, phase, helper_rc, expected_rc,
):
    sb = candidate_sandbox
    successor = _successor_tree(sb)
    override = {"FAKE_CHECK_RC" if phase == "check" else "FAKE_MERGE_RC": helper_rc}
    with candidate_paused(sb, phase, **override) as (proc, gate):
        expected_state = _revoke_and_move(sb, successor, "clear-only")
        before_calls = _calls(sb)
        os.write(gate, b"G")
        out, err = proc.communicate(timeout=15)
        assert proc.returncode == expected_rc, out + err
        assert _head(sb) == successor
        assert _calls(sb) == before_calls, "revoked failure must not start stale recovery"
        assert _state(sb)["lock"] == expected_state["lock"]
        assert _state(sb)["deployed"] is None
        assert _git(sb, "--git-dir", str(sb["origin"]), "rev-parse", "main") == sb["base"]


@pytest.mark.parametrize("mode", ["candidate", "main"])
@pytest.mark.parametrize("kill_boundary_owner", [False, True])
def test_checkout_winner_finishes_before_supported_clear_lock_returns(
    candidate_sandbox, kill_boundary_owner, mode,
):
    """A pipe inside local checkout forces clear-lock to wait for that effect."""
    sb = candidate_sandbox
    if mode == "main":
        _git(sb, "checkout", "-q", "--detach", sb["sha"])
    expected_before = sb["base"] if mode == "candidate" else sb["sha"]
    expected_after = sb["sha"] if mode == "candidate" else sb["base"]
    ready_fifo = sb["tmp"] / "checkout-ready"
    gate_fifo = sb["tmp"] / "checkout-gate"
    os.mkfifo(ready_fifo)
    os.mkfifo(gate_fifo)
    ready_r = os.open(ready_fifo, os.O_RDWR | os.O_NONBLOCK)
    gate_w = os.open(gate_fifo, os.O_RDWR | os.O_NONBLOCK)
    clear_r, clear_w = os.pipe()
    order = sb["tmp"] / "effect-order"
    real_git = shutil.which("git")
    bindir = sb["tmp"] / "checkout-git"
    bindir.mkdir()
    shim = bindir / "git"
    shim.write_text(
        f'#!{sys.executable}\n'
        'import os, subprocess, sys\nfrom pathlib import Path\n'
        f'Path({str(sb["tmp"] / "boundary-pid")!r}).write_text(str(os.getppid()))\n'
        'with open(os.environ["READY_FIFO"], "wb", buffering=0) as ready: ready.write(b"R")\n'
        'with open(os.environ["GATE_FIFO"], "rb", buffering=0) as gate: assert gate.read(1) == b"G"\n'
        f'rc = subprocess.call([{real_git!r}, *sys.argv[1:]])\n'
        f'with Path({str(order)!r}).open("a") as out: out.write("checkout\\n")\n'
        'sys.exit(rc)\n'
    )
    shim.chmod(0o755)
    ds = str(sb["dir"] / "deploy_state.py")
    runner = sb["tmp"] / "owned-checkout.sh"
    runner.write_text(
        '#!/bin/bash\nset -eu\n'
        'identity="$("$PYTHON" "$FENCE_DS" acquire --pr 42 --sha "$FENCE_SHA" --session boundary)"\n'
        'export DAILYCHECK_DEPLOY_ATTEMPT="$identity"\n'
        '"$PYTHON" "$FENCE_DS" owned-checkout --repo "$FENCE_REPO" '
        + ('--candidate "$FENCE_SHA"\n' if mode == "candidate" else '--main\n')
    )
    env = _env(
        sb, READY_FIFO=ready_fifo, GATE_FIFO=gate_fifo,
        FENCE_DS=ds, FENCE_SHA=sb["sha"], FENCE_REPO=sb["repo"],
        PATH=f"{bindir}:{os.environ['PATH']}",
    )
    checkout = subprocess.Popen(
        [sys.executable, ds, "run", "--", "bash", str(runner)], env=env,
        start_new_session=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    clear = beat = None
    try:
        assert select.select([ready_r], [], [], 10)[0], "local checkout did not reach gate"
        assert os.read(ready_r, 1) == b"R"
        assert _head(sb) == expected_before
        token = _state(sb)["lock"]["token"]
        if kill_boundary_owner:
            boundary_pid = int((sb["tmp"] / "boundary-pid").read_text())
            os.kill(boundary_pid, signal.SIGKILL)
            assert checkout.wait(timeout=10) == 137
            # Parent is positively reaped; the still-gated Git mutator must retain
            # the normal attempt fence as well as the state mutation boundary.
            assert _ds(sb, "acquire", "--pr", "43", "--sha", sb["sha"]).returncode == 10
        clear_code = (
            'import os, subprocess, sys\nfrom pathlib import Path\n'
            'os.write(int(os.environ["CLEAR_READY"]), b"Q")\n'
            f'rc = subprocess.call([{sys.executable!r}, {ds!r}, "clear-lock", "--yes"])\n'
            f'with Path({str(order)!r}).open("a") as out: out.write("clear\\n")\n'
            'sys.exit(rc)\n'
        )
        clear = subprocess.Popen(
            [sys.executable, "-c", clear_code], env=_env(sb, CLEAR_READY=clear_w),
            pass_fds=(clear_w,), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        os.close(clear_w)
        clear_w = None
        assert select.select([clear_r], [], [], 10)[0]
        assert os.read(clear_r, 1) == b"Q"
        # Independent kernel probe: the real state mutation boundary is occupied,
        # so supported revocation cannot return while the checkout effect is gated.
        with (sb["state"] / ".state.lock").open("a+") as observer:
            with pytest.raises(BlockingIOError):
                fcntl.flock(observer, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert clear.poll() is None
        beat = subprocess.Popen(
            [sys.executable, ds, "heartbeat", "--token", token], env=_env(sb),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        os.write(gate_w, b"G")
        out, err = checkout.communicate(timeout=15)
        assert checkout.returncode == (137 if kill_boundary_owner else 0), out + err
        out, err = clear.communicate(timeout=10)
        assert clear.returncode == 0, out + err
        beat.communicate(timeout=10)
        assert beat.returncode in (0, 11), "queued heartbeat must settle without deadlock"
        assert order.read_text().splitlines() == ["checkout", "clear"]
        assert _head(sb) == expected_after
        assert _state(sb)["lock"] is None
        assert _state(sb)["deployed"] is None
        print(json.dumps({"mode": mode, "ordering": order.read_text().splitlines(), "head": _head(sb),
                          "clear_rc": clear.returncode, "heartbeat_rc": beat.returncode}))
    finally:
        if checkout.poll() is None:
            with suppress(ProcessLookupError):
                os.killpg(checkout.pid, signal.SIGKILL)
        for proc in (checkout, clear, beat):
            if proc is not None:
                if proc.poll() is None:
                    proc.kill()
                proc.communicate(timeout=10)
        for fd in (ready_r, gate_w, clear_r, clear_w):
            if fd is not None:
                os.close(fd)
