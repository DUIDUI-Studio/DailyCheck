"""SYNTHETIC Hermes control plane; real CLI/HTTP/SQLite/flock/process waits.

No production database, provider, credentials or real session is used. Forged
caller receipts are not an input surface. Root/Hermes-store compromise is outside
the same authority boundary as the existing root-owned process registry.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from scripts.blue_green.release_automatic import render_bootstrap
from tests.private_audit_testenv import private_audit_fixture  # noqa: F401
from tests.test_github_ordinary_intake import dispatch
from tests.test_github_ordinary_intake import lane as ordinary_lane
from tests.test_github_transport import REPO, ROOT, SHA
from tests.test_release_bundle import automatic_bootstrap, make_bundle, put, run

lane = ordinary_lane
SOURCE = Path(__file__).resolve().parents[1]
CONFIG = Path("/etc/dailycheck/github-review-feedback.json")


def owned_terminal(lane, tmp):
    lane.port = int(lane.env["DAILYCHECK_HERMES_WEBHOOK_URL"].rsplit(":", 1)[1])
    profile, env = make_bundle(
        tmp, lane, "happy", work=(160, SHA, 19, 6391796319, 7, 7001), render_now=False
    )
    staged, _ = automatic_bootstrap(profile, env, tmp)
    config = ast.literal_eval(
        next(
            n
            for n in ast.parse(staged.read_text()).body
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "CONFIG" for t in n.targets)
        ).value
    )
    state = Path(profile["state"])
    roots = state / ("automatic-work-" + SHA)
    roots.mkdir(mode=0o700)
    config["roots"] = str(roots)
    entry = state / ("automatic-" + SHA + ".py")
    render_bootstrap(config, entry)
    code = Path(profile["engine"]) / "scripts/blue_green/release_automatic.py"
    code.chmod(0o400)
    Path(profile["engine"]).chmod(0o500)
    result = dispatch(lane)
    assert result.returncode == 0 and len(lane.posts) == 1, result.stdout + result.stderr
    payload = lane.posts[0]["body"]
    trigger = payload["trigger_id"]
    before = datetime.now()
    original = lane.table[ROOT + "/pulls/160"]
    lane.table[ROOT + "/pulls/160"] = {"status": 503, "data": {"message": "SYNTHETIC private body"}}
    argv = [sys.executable, "-I", "-B", str(entry), "inspect", trigger]
    rc, out, err = run(argv, env)
    lane.table[ROOT + "/pulls/160"] = original
    assert rc == 78 and json.loads(err)["status"] == "BLOCKER"
    # This external Hermes fixture is populated from that ACTUAL child result.
    # run() itself asserts the original child is reaped/ECHILD, not just absent.
    end = datetime.now()
    sid = before.strftime("%Y%m%d_%H%M%S_") + "1234abcd"
    home = tmp / "hermes"
    home.mkdir(mode=0o700)
    (home / "sessions").mkdir(mode=0o700)
    (home / "logs").mkdir(mode=0o700)
    chat = "webhook:wdg-ci:dailycheck-" + trigger
    key = "agent:main:webhook:webhook:" + chat + ":webhook:wdg-ci"
    transcript = {
        "session_id": sid,
        "platform": "webhook",
        "session_start": before.isoformat(),
        "last_updated": end.isoformat(),
        "message_count": 4,
        "messages": [
            {"role": "user", "content": "SYNTHETIC authenticated webhook event"},
            {
                "role": "assistant",
                "content": "",
                "finish_reason": "tool_calls",
                "tool_calls": [
                    {
                        "id": "call_synthetic",
                        "type": "function",
                        "function": {
                            "name": "terminal",
                            "arguments": json.dumps({"command": shlex.join(argv), "timeout": 180}),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_synthetic",
                "content": json.dumps({"output": out + err, "exit_code": rc, "error": None}),
            },
            {"role": "assistant", "finish_reason": "stop", "content": "SYNTHETIC stopped BLOCKER"},
        ],
    }
    session = home / "sessions" / ("session_" + sid + ".json")
    put(session, json.dumps(transcript))
    mapping = {
        "session_key": key,
        "session_id": sid,
        "platform": "webhook",
        "chat_type": "webhook",
        "created_at": before.isoformat(),
        "updated_at": (end + timedelta(milliseconds=1)).isoformat(),
        "origin": {
            "platform": "webhook",
            "chat_type": "webhook",
            "chat_id": chat,
            "user_id": "webhook:wdg-ci",
            "thread_id": None,
        },
    }
    put(home / "sessions/sessions.json", json.dumps({key: mapping}))
    put(home / "processes.json", "[]")
    stamp = end.strftime("%Y-%m-%d %H:%M:%S,") + "000 INFO "
    log = (
        stamp
        + "gateway.run: inbound message: platform=webhook user=wdg-ci chat="
        + chat
        + " msg=synthetic\n"
        + stamp
        + "gateway.run: response ready: platform=webhook chat="
        + chat
        + " time=1s\n"
        + stamp
        + "gateway.platforms.base: [Webhook] Sending response (25 chars) to "
        + chat
        + "\n"
    )
    put(home / "logs/agent.log", log)
    return env, payload, home, session, transcript, state, roots, entry, code


@pytest.mark.skipif(sys.platform != "linux" or os.getuid() != 0, reason="owned root/Linux protocol")
@pytest.mark.parametrize("lane", ["two-heads"], indirect=True)
@pytest.mark.parametrize(
    "case",
    [
        "settled",
        "claimed",
        "unknown",
        "forged-mac",
        "cross-head",
        "cross-session",
        "running",
        "missing-exit",
        "unsafe",
        "candidate",
        "late-input",
        "code-drift",
        "orphan-receipt",
    ],
)
def test_terminal_blocker_feedback_requires_native_settlement(lane, tmp_path, case):
    prior = CONFIG.read_bytes() if CONFIG.exists() else None
    CONFIG.parent.mkdir(mode=0o700, exist_ok=True)
    child = None
    release = tmp_path / "release-child"
    try:
        env, payload, home, session, transcript, state, roots, entry, code = owned_terminal(
            lane, tmp_path
        )
        trigger = payload["trigger_id"]
        put(CONFIG, json.dumps({"version": 1, "state_dir": str(state), "hermes_home": str(home)}))
        db = Path(env["DAILYCHECK_EVENT_REDUCER_DB"])
        if case in {"claimed", "unknown", "forged-mac", "cross-head"}:
            # Only owned test data; this is not a production recovery interface.
            with sqlite3.connect(db) as conn:
                if case == "claimed":
                    conn.execute(
                        "UPDATE trigger_queue SET state='claimed',last_error='' WHERE trigger_id=?",
                        (trigger,),
                    )
                elif case == "unknown":
                    conn.execute(
                        "UPDATE trigger_queue SET last_error='delivery_unknown' WHERE trigger_id=?",
                        (trigger,),
                    )
                elif case == "cross-head":
                    conn.execute(
                        "UPDATE trigger_queue SET head_sha=? WHERE trigger_id=?",
                        ("c" * 40, trigger),
                    )
                else:
                    forged = dict(payload)
                    forged["ordinary_provenance"] = dict(
                        payload["ordinary_provenance"], mac="0" * 64
                    )
                    conn.execute(
                        "UPDATE trigger_queue SET payload_json=? WHERE trigger_id=?",
                        (json.dumps(forged), trigger),
                    )
        if case == "cross-session":
            transcript["session_id"] = "SYNTHETIC_OTHER_SESSION"
        if case == "missing-exit":
            data = json.loads(transcript["messages"][2]["content"])
            data["exit_code"] = None
            transcript["messages"][2]["content"] = json.dumps(data)
        put(session, json.dumps(transcript))
        if case == "unsafe":
            session.chmod(0o666)
        if case == "candidate":
            put(roots / (payload["admission_id"] + ".intent.json"), "SYNTHETIC consumed")
        if case == "late-input":
            with (home / "logs/agent.log").open("a") as stream:
                stream.write(
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S,")
                    + "000 INFO gateway.run: inbound message: platform=webhook user=wdg-ci chat=webhook:wdg-ci:dailycheck-"
                    + trigger
                    + " msg=synthetic-new-work\n"
                )
        if case == "code-drift":
            code.chmod(0o600)
            code.write_text(code.read_text() + "\n# SYNTHETIC code drift\n")
            code.chmod(0o400)
        if case == "orphan-receipt":
            (home / "sessions/sessions.json").unlink()
            put(
                state / "forged-blocker-receipt.json",
                json.dumps({"status": "BLOCKER", "exit_code": 78}),
            )
        if case == "running":
            ready = tmp_path / "child-ready"
            program = (
                f"import fcntl,time;from pathlib import Path;f=open({str(state / '.attempt.lock')!r},'a+');"
                f"fcntl.flock(f,fcntl.LOCK_EX);Path({str(ready)!r}).touch();\n"
                f"while not Path({str(release)!r}).exists():time.sleep(.02)"
            )
            child = subprocess.Popen([sys.executable, "-I", "-B", "-c", program])
            until = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < until:
                time.sleep(0.01)
            assert ready.exists()
        with sqlite3.connect(db) as conn:
            conn.row_factory = sqlite3.Row
            before = dict(
                conn.execute(
                    "SELECT * FROM trigger_queue WHERE trigger_id=?", (trigger,)
                ).fetchone()
            )
        command = [
            sys.executable,
            "-I",
            "-B",
            str(SOURCE / "scripts/prod_agent/event_reducer.py"),
            "--db",
            str(db),
            "reconcile-reviews",
            "--repo",
            REPO,
            "--pr",
            "160",
        ]
        rc, out, err = run(command, env)
        assert rc == 0, (out, err)
        result = json.loads(out)
        with sqlite3.connect(db) as conn:
            conn.row_factory = sqlite3.Row
            after = dict(
                conn.execute(
                    "SELECT * FROM trigger_queue WHERE trigger_id=?", (trigger,)
                ).fetchone()
            )
            if case == "settled":
                assert result["terminal_review_receipts"][0]["kind"] == "pre_candidate_BLOCKER"
                assert after["last_error"] == "review_terminal_blocker"
                for key in set(before) - {"last_error", "updated_at_ms"}:
                    assert after[key] == before[key], key
                record = conn.execute(
                    "SELECT receipt_json FROM review_terminal_receipts WHERE trigger_id=?",
                    (trigger,),
                ).fetchone()
                receipt = json.loads(record[0])
                assert receipt["formal_review"] is False and receipt["no_candidate"] is True
                assert receipt["session_sha256"] == hashlib.sha256(session.read_bytes()).hexdigest()
            else:
                assert after == before and result["terminal_review_receipts"] == []
        if case == "settled":
            rc, out, err = run(command, env)
            assert rc == 0 and json.loads(out)["terminal_review_receipts"] == []
            new_head = "c" * 40
            table = json.loads(json.dumps(lane.table).replace(SHA, new_head))
            workflow = table[ROOT + "/actions/workflows/12/runs?page=1"]["data"]["workflow_runs"][0]
            workflow.update(id=41, run_number=41, run_attempt=1, check_suite_id=141)
            checks = table[ROOT + f"/commits/{new_head}/check-runs?page=1"]["data"]["check_runs"]
            jobs = table.pop(ROOT + "/actions/runs/40/jobs?page=1")
            for index, (check, job) in enumerate(zip(checks, jobs["data"]["jobs"], strict=True)):
                check.update(id=61 + index, check_suite={"id": 141})
                job.update(
                    id=161 + index,
                    run_id=41,
                    run_attempt=1,
                    check_run_url=f"https://api.github.com{ROOT}/check-runs/{61 + index}",
                )
            table[ROOT + "/actions/runs/41/jobs?page=1"] = jobs
            lane.table.clear()
            lane.table.update(table)
            # make_bundle adopts a protected automatic policy after the receiver
            # fixture has started. The real cron producer uses that current
            # policy; a receiver still carrying its initial fixture env cannot.
            result = dispatch(lane)
            assert result.returncode == 0, (result.stdout, result.stderr)
            assert len(lane.posts) == 2 and lane.posts[-1]["body"]["pr_head_sha"] == new_head
            with sqlite3.connect(db) as conn:
                conn.row_factory = sqlite3.Row
                assert (
                    dict(
                        conn.execute(
                            "SELECT * FROM trigger_queue WHERE trigger_id=?", (trigger,)
                        ).fetchone()
                    )
                    == after
                )
        print(
            json.dumps(
                {
                    "case": case,
                    "synthetic_only": True,
                    "actual_terminal_exit": 78,
                    "actual_wait_ECHILD": True,
                    "native_transition": case == "settled",
                    "original_history_retained": True,
                }
            )
        )
    finally:
        if child is not None:
            release.touch()
            assert child.wait() == 0
            with pytest.raises(ChildProcessError):
                os.waitpid(child.pid, os.WNOHANG)
        if prior is None:
            CONFIG.unlink(missing_ok=True)
        else:
            put(CONFIG, prior)
