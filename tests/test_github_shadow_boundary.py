"""Shadow isolation at real CLI, adapter, sender and SQLite write boundaries."""

from __future__ import annotations

import hashlib
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

ROOT = Path(os.environ.get("DAILYCHECK_TRANSPORT_TEST_ROOT", Path(__file__).resolve().parents[1]))
SHA = "a" * 40
REPO = "DUIDUI-Studio/DailyCheck"


def event():
    return {
        "delivery_id": "shadow-deploy",
        "event": "deploy_result",
        "action": "completed",
        "repo_full_name": REPO,
        "pr_number": 160,
        "pr_head_sha": SHA,
        "deploy_rc": 0,
        "deploy_stage": "done",
        "deploy_next": "closeout",
        "received_at_ms": 1000,
    }


@pytest.fixture
def lane(tmp_path, request):
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    subscriptions = tmp_path / "subscriptions.json"
    subscriptions.write_text(
        json.dumps({r: {"secret": "local-shadow-test"} for r in ("wdg-ci", "dailycheck-deploy")})
    )
    posts = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            posts.append(
                {
                    "path": self.path,
                    "body": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                }
            )
            self.send_response(202)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "DAILYCHECK_HOSTING_PROVIDER": "github",
        "DAILYCHECK_GITHUB_SHADOW_DIR": str(shadow),
        "DAILYCHECK_PYTHON": sys.executable,
        "DAILYCHECK_EVENT_REDUCER": str(ROOT / "scripts/prod_agent/event_reducer.py"),
        "DAILYCHECK_EVENT_REDUCER_DB": str(shadow / "state.sqlite3"),
        "DAILYCHECK_REDUCER_DEBOUNCE_MS": "0",
        "DAILYCHECK_HERMES_SENDER": str(ROOT / "scripts/prod_agent/hermes_webhook_sender.py"),
        "DAILYCHECK_HERMES_SUBSCRIPTIONS": str(subscriptions),
        "DAILYCHECK_HERMES_WEBHOOK_URL": f"http://127.0.0.1:{server.server_port}",
        "DAILYCHECK_PROD_AGENT_INBOX": str(shadow / "inbox.jsonl"),
        "TMPDIR": str(shadow),
    }
    for key in (
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "DAILYCHECK_GITHUB_READ_TOKEN",
        "DAILYCHECK_GITHUB_READ_TOKEN_FILE",
    ):
        env.pop(key, None)
    try:
        yield env, posts, shadow
    finally:
        evidence_dir = os.environ.get("DAILYCHECK_SHADOW_EVIDENCE_DIR")
        if evidence_dir:
            destination = Path(evidence_dir)
            destination.mkdir(parents=True, exist_ok=True)
            outside = tmp_path / "outside"
            outside_files = (
                [str(p) for p in outside.rglob("*") if p.is_file()] if outside.is_dir() else []
            )
            report_log = tmp_path / "outside-events.jsonl"
            if report_log.exists():
                outside_files.append(str(report_log))
            observed = {
                "test": request.node.nodeid,
                "posts": posts,
                "post_count": len(posts),
                "outside_files": outside_files,
                "source_sha": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
                ).strip(),
                "source_dirty": bool(
                    subprocess.check_output(
                        ["git", "status", "--porcelain"], cwd=ROOT, text=True
                    ).strip()
                ),
            }
            name = hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:16] + ".json"
            (destination / name).write_text(json.dumps(observed, indent=2))
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def invoke(args, env, payload=None):
    return subprocess.run(
        args,
        cwd=ROOT,
        env=env,
        input=json.dumps(payload) if payload is not None else None,
        text=True,
        capture_output=True,
        timeout=30,
    )


def cli(env, command="ingest", payload=None, db=None):
    return invoke(
        [
            sys.executable,
            str(ROOT / "scripts/prod_agent/event_reducer.py"),
            "--db",
            str(db or env["DAILYCHECK_EVENT_REDUCER_DB"]),
            command,
        ],
        env,
        payload,
    )


def adapter(env, payload=None, drain=False):
    return invoke(
        [
            "bash",
            str(ROOT / "deploy/scripts/dc-notify-prod-agent.sh"),
            *(["--drain"] if drain else []),
        ],
        env,
        payload,
    )


def seed_delayed_row(db, payload):
    # Fixture a pre-cutover row directly; transport isolation must not trust tags or new ingress.
    source = (ROOT / "scripts/prod_agent/event_reducer.py").read_text()
    schema = re.search(r'SCHEMA = """(.*?)"""', source, re.S).group(1)
    with sqlite3.connect(db) as conn:
        conn.executescript(schema)
        conn.execute(
            """INSERT INTO trigger_queue
            (trigger_id,event_key,repo,pr_number,head_sha,event,action,conclusion,route,priority,payload_json,state,first_seen_ms,due_at_ms,updated_at_ms)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "trig_delayed",
                "old-key",
                REPO.lower(),
                160,
                SHA,
                "deploy_result",
                "completed",
                "success",
                "dailycheck-deploy",
                30,
                json.dumps(payload),
                "pending",
                1,
                1,
                1,
            ),
        )


@pytest.mark.parametrize("command", ["status", "ingest"])
@pytest.mark.parametrize(
    "kind", ["outside", "file-link", "parent-link", "wal-link", "shm-link", "journal-link"]
)
def test_cli_rejects_outside_and_symlink_state_before_any_sqlite_write(
    lane, tmp_path, kind, command
):
    env, posts, shadow = lane
    outside = tmp_path / "outside"
    db = shadow / "state.sqlite3"
    if kind == "outside":
        db = outside / "state.sqlite3"
    elif kind == "file-link":
        outside.mkdir()
        db.symlink_to(outside / "state.sqlite3")
    elif kind == "parent-link":
        outside.mkdir()
        (shadow / "link").symlink_to(outside, target_is_directory=True)
        db = shadow / "link" / "state.sqlite3"
    else:
        outside.mkdir()
        suffix = {"wal-link": "-wal", "shm-link": "-shm", "journal-link": "-journal"}[kind]
        Path(str(db) + suffix).symlink_to(outside / "sidecar")
    result = cli(env, command, event() if command == "ingest" else None, db=db)
    assert result.returncode != 0
    assert "shadow" in result.stdout + result.stderr
    assert not outside.exists() or list(outside.iterdir()) == []
    assert not (shadow / "state.sqlite3").is_file()
    assert not posts


def test_report_event_has_explicit_shadow_provenance(lane):
    env, _, _ = lane
    code = """
from scripts.blue_green import deploy_report
import json
assert deploy_report.push_inbox_event(160, 'a'*40, {'rc':0,'kind':'success','stage':'done'}, {'verdict':'aligned'}, 'closeout')
print(json.dumps(json.loads(deploy_report.inbox_path().read_text().splitlines()[-1])))
"""
    result = invoke([sys.executable, "-c", code], env)
    assert result.returncode == 0, result.stderr
    row = json.loads(result.stdout)
    assert row["hosting_provider"] == "github" and row["intake_mode"] == "shadow"


@pytest.mark.parametrize("mode", [None, "gitea", "github"])
def test_actual_adapter_ingest_and_drain_never_post_untagged_github_deploy_result(lane, mode):
    env, posts, _ = lane
    env.pop("DAILYCHECK_HOSTING_PROVIDER")
    if mode:
        env["DAILYCHECK_HOSTING_PROVIDER"] = mode
    first = adapter(env, event())
    second = adapter(env, drain=True)
    assert first.returncode == second.returncode == 0, first.stderr + second.stderr
    assert not posts


@pytest.mark.parametrize("mode", [None, "gitea", "github"])
def test_actual_adapter_never_posts_preexisting_untagged_delayed_rows(lane, mode):
    env, posts, _ = lane
    env.pop("DAILYCHECK_HOSTING_PROVIDER")
    if mode:
        env["DAILYCHECK_HOSTING_PROVIDER"] = mode
    seed_delayed_row(env["DAILYCHECK_EVENT_REDUCER_DB"], event())
    result = adapter(env, drain=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not posts
    with sqlite3.connect(env["DAILYCHECK_EVENT_REDUCER_DB"]) as conn:
        assert conn.execute("SELECT state,attempts FROM trigger_queue").fetchone() == (
            "pending" if mode == "github" else "done", 0
        )
        if mode != "github":
            assert conn.execute("SELECT last_error FROM trigger_queue").fetchone()[0] == "shadow_rejected"


@pytest.mark.parametrize("mode", [None, "gitea", "github"])
def test_sender_directly_blocks_shadow_target_even_without_markers(lane, mode):
    env, posts, shadow = lane
    env.pop("DAILYCHECK_HOSTING_PROVIDER")
    if mode:
        env["DAILYCHECK_HOSTING_PROVIDER"] = mode
    body = shadow / "body.json"
    body.write_text(json.dumps(event()))
    result = invoke(
        [
            sys.executable,
            str(ROOT / "scripts/prod_agent/hermes_webhook_sender.py"),
            "--subscriptions",
            env["DAILYCHECK_HERMES_SUBSCRIPTIONS"],
            "--base-url",
            env["DAILYCHECK_HERMES_WEBHOOK_URL"],
            "--route",
            "dailycheck-deploy",
            "--event",
            "deploy_result",
            "--delivery-id",
            "sender-shadow",
            "--body-file",
            str(body),
        ],
        env,
    )
    assert result.returncode != 0 and json.loads(result.stdout)["accepted"] is False
    assert not posts


def test_github_mode_blocks_sender_with_preexisting_gitea_named_payload(lane):
    env, posts, shadow = lane
    row = event()
    row["repo_full_name"] = "eric/dailycheck"
    seed_delayed_row(env["DAILYCHECK_EVENT_REDUCER_DB"], row)
    result = adapter(env, drain=True)
    assert result.returncode == 0 and not posts
    body = shadow / "old-body.json"
    body.write_text(json.dumps(row))
    direct = invoke(
        [
            sys.executable,
            str(ROOT / "scripts/prod_agent/hermes_webhook_sender.py"),
            "--subscriptions",
            env["DAILYCHECK_HERMES_SUBSCRIPTIONS"],
            "--base-url",
            env["DAILYCHECK_HERMES_WEBHOOK_URL"],
            "--route",
            "dailycheck-deploy",
            "--event",
            "deploy_result",
            "--delivery-id",
            "old-sender-shadow",
            "--body-file",
            str(body),
        ],
        env,
    )
    assert direct.returncode != 0 and not posts


def test_gitea_real_adapter_positive_control_is_unchanged(lane):
    env, posts, _ = lane
    env["DAILYCHECK_HOSTING_PROVIDER"] = "gitea"
    row = event()
    row["repo_full_name"] = "eric/dailycheck"
    assert adapter(env, row).returncode == 75
    result = adapter(env, drain=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(posts) == 1 and posts[0]["path"] == "/webhooks/dailycheck-deploy"


def test_ready_for_review_receiver_to_cli_roundtrip(lane, tmp_path):
    env, _, _ = lane
    # Use the existing executable fake network at the true receiver boundary.
    code = r"""
import hashlib,hmac,json,os,runpy,sys
from pathlib import Path
fixtures=runpy.run_path('tests/test_github_transport.py')
d=Path(os.environ['DAILYCHECK_GITHUB_SHADOW_DIR'])
fake=d/'fake-curl'
fake.write_text(fixtures['FAKE_CURL'].replace('PYTHON',sys.executable)); fake.chmod(0o755)
os.environ['DAILYCHECK_CURL']=str(fake)
os.environ['DAILYCHECK_GITHUB_READ_TOKEN']='test-credential'
for key in ('TABLE','STATE','LOG'): os.environ['FAKE_'+key]=str(d/key.lower())
(d/'table').write_text(json.dumps(fixtures['evidence']()))
from scripts.blue_green import webhook_receiver as receiver
receiver.WEBHOOK_SECRET='fixture-secret'
receiver.INBOX_PATH=d/'inbox.jsonl'; receiver.PROCESSED_PATH=d/'processed.jsonl'
data=fixtures['native']('pull_request'); data['action']='ready_for_review'
raw=json.dumps(data).encode(); sig='sha256='+hmac.new(b'fixture-secret',raw,hashlib.sha256).hexdigest()
r=receiver.app.test_client().post('/webhook',data=raw,headers={'X-GitHub-Event':'pull_request','X-GitHub-Delivery':'ready-fixture','X-Hub-Signature-256':sig})
assert r.status_code==202,r.data
print(receiver.INBOX_PATH.read_text().splitlines()[-1])
"""
    received = invoke([sys.executable, "-c", code], env)
    assert received.returncode == 0, received.stderr
    record = json.loads(received.stdout)
    reduced = cli(env, payload=record)
    assert reduced.returncode == 0, reduced.stderr
    assert json.loads(reduced.stdout)["reason"] != "invalid_payload"
    assert record["source_action"] == "ready_for_review" and record["action"] == "reopened"


def test_gitea_receiver_keeps_inbox_only_legacy_deduplication(lane):
    env, _, _ = lane
    env["DAILYCHECK_HOSTING_PROVIDER"] = "gitea"
    code = r"""
import hashlib,hmac,json,os,runpy
from pathlib import Path
fixtures=runpy.run_path('tests/test_github_transport.py')
from scripts.blue_green import webhook_receiver as receiver
d=Path(os.environ['DAILYCHECK_GITHUB_SHADOW_DIR'])
receiver.WEBHOOK_SECRET='fixture-secret'
receiver.INBOX_PATH=d/'inbox.jsonl'; receiver.PROCESSED_PATH=d/'processed.jsonl'
receiver.PROCESSED_PATH.write_text(json.dumps({'delivery_id':'legacy-id'})+'\n')
data=fixtures['native']('pull_request'); data['repository']={'full_name':'eric/dailycheck'}
raw=json.dumps(data).encode(); sig='sha256='+hmac.new(b'fixture-secret',raw,hashlib.sha256).hexdigest()
r=receiver.app.test_client().post('/webhook',data=raw,headers={'X-GitHub-Event':'pull_request','X-GitHub-Delivery':'legacy-id','X-Hub-Signature-256':sig})
assert r.status_code==202,r.data
print('legacy queued as before')
"""
    result = invoke([sys.executable, "-c", code], env)
    assert result.returncode == 0, result.stderr


def test_shadow_report_log_rejects_outside_path_and_marks_inside(lane, tmp_path):
    env, _, shadow = lane
    outside = tmp_path / "outside-events.jsonl"
    env["DAILYCHECK_DEPLOY_EVENT_LOG"] = str(outside)
    code = "from scripts.blue_green import deploy_report as d; print(d.append_event({'event':'deploy_result'}))"
    rejected = invoke([sys.executable, "-c", code], env)
    assert rejected.returncode == 0 and rejected.stdout.strip() == "False"
    assert not outside.exists()
    env["DAILYCHECK_DEPLOY_EVENT_LOG"] = str(shadow / "events.jsonl")
    accepted = invoke([sys.executable, "-c", code], env)
    assert accepted.stdout.strip() == "True"
    row = json.loads((shadow / "events.jsonl").read_text())
    assert row["hosting_provider"] == "github" and row["intake_mode"] == "shadow"
