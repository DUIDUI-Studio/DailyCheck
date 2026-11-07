"""Raw boundary agreement and durable permanent-rejection progress through real entrypoints."""

from __future__ import annotations

import hashlib
import hmac
import http.server
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TARGET = "DUIDUI-Studio/DailyCheck"
SHA = "a" * 40
SECRET = b"local-boundary-repair-fixture"


def deployment(pr=160, **extra):
    return {
        "delivery_id": f"delivery-{pr}",
        "event": "deploy_result",
        "action": "completed",
        "repo_full_name": "eric/dailycheck",
        "pr_number": pr,
        "pr_head_sha": SHA,
        "conclusion": "success",
        "deploy_rc": 0,
        "deploy_next": "closeout",
        **extra,
    }


@pytest.fixture
def rig(tmp_path, request):
    posts = []
    responses = [202]

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            signature = "sha256=" + hmac.new(SECRET, raw, hashlib.sha256).hexdigest()
            posts.append(
                {
                    "raw": raw.decode("utf-8", errors="backslashreplace"),
                    "hmac_valid": hmac.compare_digest(
                        signature, self.headers.get("X-Hub-Signature-256", "")
                    ),
                    "path": self.path,
                }
            )
            status = responses.pop(0) if len(responses) > 1 else responses[0]
            self.send_response(status)
            if status == 429:
                self.send_header("Retry-After", "0.01")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    secret_file = tmp_path / "subscriptions.json"
    secret_file.write_text(
        json.dumps({r: {"secret": SECRET.decode()} for r in ("wdg-ci", "dailycheck-deploy")})
    )
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "GITEA_TOKEN": "local-unused-read-token",
        "DAILYCHECK_PYTHON": sys.executable,
        "DAILYCHECK_EVENT_REDUCER": str(ROOT / "scripts/prod_agent/event_reducer.py"),
        "DAILYCHECK_EVENT_REDUCER_DB": str(tmp_path / "state.sqlite3"),
        "DAILYCHECK_REDUCER_DEBOUNCE_MS": "0",
        "DAILYCHECK_HERMES_SENDER": str(ROOT / "scripts/prod_agent/hermes_webhook_sender.py"),
        "DAILYCHECK_HERMES_SUBSCRIPTIONS": str(secret_file),
        "DAILYCHECK_HERMES_WEBHOOK_URL": f"http://127.0.0.1:{server.server_port}",
        "TMPDIR": str(tmp_path),
    }
    env.pop("DAILYCHECK_HOSTING_PROVIDER", None)
    calls = []

    def run(args, raw=None):
        proc = subprocess.run(args, cwd=ROOT, env=env, input=raw, capture_output=True, timeout=30)
        calls.append(
            {
                "args": [str(x) for x in args],
                "exit": proc.returncode,
                "stdout": proc.stdout.decode(errors="backslashreplace"),
                "stderr": proc.stderr.decode(errors="backslashreplace"),
            }
        )
        return proc

    class Rig:
        db = tmp_path / "state.sqlite3"
        directory = tmp_path

        def sender(self, raw):
            body = tmp_path / "body.json"
            body.write_bytes(raw)
            return run(
                [
                    sys.executable,
                    env["DAILYCHECK_HERMES_SENDER"],
                    "--subscriptions",
                    str(secret_file),
                    "--base-url",
                    env["DAILYCHECK_HERMES_WEBHOOK_URL"],
                    "--route",
                    "dailycheck-deploy",
                    "--event",
                    "deploy_result",
                    "--delivery-id",
                    "direct-fixture",
                    "--body-file",
                    str(body),
                ]
            )

        def guard(self, raw=b""):
            return run([sys.executable, env["DAILYCHECK_HERMES_SENDER"], "--production-guard"], raw)

        def adapter(self, raw=None):
            return run(
                [
                    "bash",
                    str(ROOT / "deploy/scripts/dc-notify-prod-agent.sh"),
                    *(["--drain"] if raw is None else []),
                ],
                raw,
            )

        def cli(self, command, raw=None, now=10000, *extra):
            return run(
                [
                    sys.executable,
                    env["DAILYCHECK_EVENT_REDUCER"],
                    "--db",
                    str(self.db),
                    "--now-ms",
                    str(now),
                    command,
                    *extra,
                ],
                raw,
            )

    obj = Rig()
    obj.posts, obj.responses, obj.env, obj.calls = posts, responses, env, calls
    try:
        yield obj
    finally:
        dest = os.environ.get("DAILYCHECK_REPAIR_EVIDENCE")
        if dest:
            target = Path(dest)
            target.mkdir(parents=True, exist_ok=True)
            name = hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:16]
            observed = {"test": request.node.nodeid, "posts": posts, "calls": calls}
            if obj.db.exists():
                with sqlite3.connect(obj.db) as conn:
                    conn.row_factory = sqlite3.Row
                    observed["tables"] = {
                        table: [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]
                        for table in ("trigger_queue", "deliveries", "provider_state")
                    }
            (target / f"{name}.json").write_text(json.dumps(observed, indent=2))
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def seed(rig, rows):
    schema = re.search(
        r'SCHEMA = """(.*?)"""', (ROOT / "scripts/prod_agent/event_reducer.py").read_text(), re.S
    ).group(1)
    with sqlite3.connect(rig.db) as conn:
        conn.executescript(schema)
        for index, row in enumerate(rows):
            payload = row.get("payload", deployment(160 + index))
            raw = row.get("raw", json.dumps(payload))
            conn.execute(
                """INSERT INTO trigger_queue
              (trigger_id,event_key,repo,pr_number,head_sha,event,action,conclusion,route,priority,payload_json,state,first_seen_ms,due_at_ms,attempts,updated_at_ms)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    f"q-{index}",
                    row.get("key", f"key-{index}"),
                    row.get("repo", payload.get("repo_full_name", "eric/dailycheck")),
                    payload.get("pr_number", 160 + index),
                    SHA,
                    payload.get("event", "deploy_result"),
                    "completed",
                    "success",
                    "dailycheck-deploy",
                    30,
                    raw,
                    "pending",
                    index + 1,
                    1,
                    row.get("attempts", 2),
                    1,
                ),
            )
            conn.execute(
                "INSERT INTO deliveries VALUES (?,?,?,?,?)",
                (
                    payload["delivery_id"],
                    row.get("key", f"key-{index}"),
                    f"q-{index}",
                    1,
                    "deferred",
                ),
            )


def state(rig):
    with sqlite3.connect(rig.db) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(r)
            for r in conn.execute("SELECT * FROM trigger_queue ORDER BY first_seen_ms,trigger_id")
        ]


def test_original_mixed_queue_progress_without_cleanup(rig):
    rows = [{"repo": TARGET}, {"raw": '{"broken":'}, {}]
    seed(rig, rows)
    before = state(rig)
    for _ in range(3):
        rig.adapter()
    after = state(rig)
    assert len(rig.posts) == 1 and rig.posts[0]["hmac_valid"]
    assert [r["state"] for r in after] == ["done"] * 3
    assert [r["last_error"] for r in after[:2]] == ["shadow_rejected", "invalid_stored_payload"]
    for old, new in zip(before[:2], after[:2], strict=True):
        assert new["payload_json"] == old["payload_json"] and new["attempts"] == old["attempts"]
        assert new["completed_at_ms"] and new["updated_at_ms"] > old["updated_at_ms"]
        assert new["lease_token"] is None and new["lease_expires_ms"] is None
    with sqlite3.connect(rig.db) as conn:
        assert [
            r[0] for r in conn.execute("SELECT disposition FROM deliveries ORDER BY delivery_id")
        ][:2] == ["shadow_rejected", "invalid_stored_payload"]


@pytest.mark.parametrize(
    "raw",
    [
        b'{"hosting_provider":"github",',
        json.dumps(deployment(repo_full_name=f" {TARGET} ")).encode(),
    ],
)
def test_original_direct_sender_rejects_invalid_or_padded_target_before_post(rig, raw):
    guard = rig.guard(raw)
    result = rig.sender(raw)
    assert guard.returncode != 0
    assert result.returncode != 0 and json.loads(result.stdout)["accepted"] is False
    assert not rig.posts


@pytest.mark.parametrize("alias", ["repository", "repo"])
def test_original_padded_conflicting_alias_through_actual_shell(rig, alias):
    supplied = (
        {"repository": {"full_name": f" {TARGET} "}}
        if alias == "repository"
        else {"repo": f" {TARGET} "}
    )
    raw = json.dumps(deployment(**supplied)).encode()
    rig.adapter(raw)
    rig.adapter()
    assert not rig.posts and not rig.db.exists()
