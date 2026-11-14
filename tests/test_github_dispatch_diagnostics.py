"""Actual failing child evidence; fixed operator argv coverage lives in its separate artifact."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import stat
import subprocess
import sys

import pytest

from tests import test_github_ordinary_intake as ordinary

SOURCE = ordinary.SOURCE
lane = ordinary.lane

CANARY = "SYNTHETIC_PRIVATE_DISPATCH_DIAGNOSTIC"


def run_dispatch(lane, adapter):
    code = ("import sys; from pathlib import Path; "
            "from scripts.prod_agent import github_dispatch as d; "
            f"d.ADAPTER=Path({str(adapter)!r}); sys.exit(d.main())")
    child = subprocess.Popen([sys.executable, "-B", "-c", code], cwd=SOURCE, env=lane.env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = child.communicate(timeout=30)
    assert child.wait() == 1
    with pytest.raises(ChildProcessError):
        os.waitpid(child.pid, os.WNOHANG)
    return json.loads(out), out, err


@pytest.mark.parametrize("fault", ["ingest", "drain", "malformed", "binary", "large", "write"])
def test_actual_child_evidence_precedes_parsing_and_failure(lane, tmp_path, fault):
    audit = tmp_path / "child-audit"
    audit.mkdir(mode=0o700)
    lane.env["DAILYCHECK_GITHUB_DISPATCH_AUDIT_DIR"] = str(audit.resolve())
    calls = tmp_path / "child-calls"
    stdout = (json.dumps({"decision": "defer", "reason": CANARY}) + "\n").encode()
    stderr = (CANARY + "\n").encode()
    if fault == "malformed":
        stdout = b"not-json\n"
    if fault == "binary":
        stdout = b"\xff\n"
    if fault == "large":
        stderr += b"x" * 70000
    adapter = tmp_path / "failing-adapter.sh"
    emit = (f"printf '%s' {shlex.quote(stdout.decode())}\n" if fault != "binary"
            else "printf '\\377\\n'\n")
    emit += f"printf '%s' {shlex.quote(stderr.decode())} >&2\n"
    if fault == "drain":
        body = (f"if [ \"${{1:-}}\" != --drain ]; then "
                f"printf '%s' {shlex.quote(json.dumps({'decision': 'defer', 'reason': CANARY}))}; "
                "exit 75; fi\n")
    else:
        body = ""
    if fault == "write":
        body += f"rmdir {shlex.quote(str(audit))}\n"
    adapter.write_text("#!/bin/bash\n" + f"printf '%s\\n' \"${{1:-ingest}}\" >>{shlex.quote(str(calls))}\n"
                       + body + emit + f"exit {0 if fault in {'malformed', 'binary'} else 23}\n")
    adapter.chmod(0o700)
    status, public, outer_stderr = run_dispatch(lane, adapter)
    assert status["intake"] == "reconcile_required"
    expected_phase = "drain" if fault == "drain" else "ingest"
    expected_rc = 0 if fault in {"malformed", "binary"} else 23
    if fault == "write":
        assert status["error"] == "ordinary_cron_diagnostic_unavailable"
        assert status["child"] == {"phase": "ingest", "native_rc": 23}
    else:
        refs = status["diagnostics"]
        assert len(refs) == (2 if fault == "drain" else 1)
        ref = refs[-1]
        assert ref["phase"] == expected_phase and ref["native_rc"] == expected_rc
        path = audit / ref["evidence"]
        assert path.parent == audit and path.name == ref["evidence"]
        info = path.lstat()
        assert stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
        assert info.st_uid == os.geteuid() and info.st_nlink == 1
        raw = path.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == ref["sha256"]
        doc = json.loads(raw)
        assert doc["phase"] == expected_phase and doc["native_rc"] == expected_rc
        for label, original in [("stdout", stdout), ("stderr", stderr)]:
            stream = doc[label]
            retained = base64.b64decode(stream["base64"], validate=True)
            assert retained == original[:65536]
            assert stream["raw_bytes"] == len(original)
            assert stream["sha256"] == hashlib.sha256(original).hexdigest()
            assert stream["retained_bytes"] == len(retained)
            assert stream["truncated"] is (len(original) > 65536)
        assert len(raw) < 180000
    assert CANARY not in public + outer_stderr
    assert calls.read_text().splitlines() == (["ingest", "--drain"] if fault == "drain" else ["ingest"])
    assert lane.posts == []


@pytest.mark.parametrize("fault", ["missing", "symlink", "public"])
def test_unsafe_audit_directory_stops_before_provider_or_child(lane, tmp_path, fault):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    if fault == "missing":
        lane.env.pop("DAILYCHECK_GITHUB_DISPATCH_AUDIT_DIR", None)
    elif fault == "symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(directory, target_is_directory=True)
        lane.env["DAILYCHECK_GITHUB_DISPATCH_AUDIT_DIR"] = str(alias)
    else:
        directory.chmod(0o777)
        lane.env["DAILYCHECK_GITHUB_DISPATCH_AUDIT_DIR"] = str(directory.resolve())
    status, public, err = run_dispatch(lane, tmp_path / "must-not-run")
    assert status["error"] == "ordinary_cron_diagnostic_unavailable"
    assert not lane.reads and not lane.posts
    assert CANARY not in public + err
