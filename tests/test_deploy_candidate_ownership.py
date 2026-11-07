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
def candidate_paused(sb, phase):
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
            (sb["state"] / "state.json").write_text(json.dumps(st))
            (sb["state"] / "lock.token").write_text("successor-token\n")
    _git(sb, "checkout", "-q", "successor-tree")
    return _state(sb)


@pytest.mark.parametrize("phase", ["check", "merge"])
@pytest.mark.parametrize("replacement", ["clear-only", "same-pr-successor"])
def test_revoked_candidate_preserves_successor_checkout(candidate_sandbox, phase, replacement):
    sb = candidate_sandbox
    successor = _successor_tree(sb)
    with candidate_paused(sb, phase) as (proc, gate):
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
        assert _state(sb)["deployed"] == expected_state["deployed"] is None
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
