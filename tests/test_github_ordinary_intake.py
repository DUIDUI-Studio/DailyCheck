"""Signed receiver -> real adapter/reducer/sender -> owned HTTP sinks only."""

from __future__ import annotations

import hashlib
import hmac
import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.conftest import candidate_python_env
from tests.test_deploy_publication_wiring import (  # noqa: F401
    PRIVATE_CANARY,
    _pipe,
    _report,
    _state,
    loopback,
    sandbox,
)
from tests.test_github_transport import BASE, REPO, REPO_ID, ROOT, SHA, evidence
from tests.test_github_transport import proof as save_proof

SOURCE = Path(__file__).resolve().parents[1]


@pytest.fixture
def lane(tmp_path, request):
    table = evidence()
    table[ROOT + "/branches/main/protection"] = {"data": {
        "required_status_checks": {"strict": True, "checks": [
            {"context": name, "app_id": 15368}
            for name in ("test", "agent-framework-reports")]},
        "required_pull_request_reviews": {"required_approving_review_count": 1,
                                          "dismiss_stale_reviews": True},
        "enforce_admins": {"enabled": True},
    }}
    table[ROOT + "/pulls/160/reviews?page=1"] = {"data": []}
    posts, reads = [], []
    transport = {"drop": False}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            key = url.path + ("?page=" + query["page"][0] if "page" in query else "")
            reads.append(key)
            row = table.get(key, {"status": 404, "data": {}})
            self.respond(row.get("status", 200), row["data"])

        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            expected = "sha256=" + hmac.new(b"owned-route-secret", raw, hashlib.sha256).hexdigest()
            assert hmac.compare_digest(self.headers["X-Hub-Signature-256"], expected)
            posts.append({"path": self.path, "event": self.headers["X-GitHub-Event"],
                          "body": json.loads(raw)})
            if transport["drop"]:
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            self.respond(202, {})

        def respond(self, status, data):
            raw = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    shim = tmp_path / "curl"
    shim.write_text(f'''#!{sys.executable}
import os, sys
from urllib.parse import urlsplit
args = sys.argv[1:]
url = urlsplit(args[-1])
assert url.scheme == "https" and url.netloc == "api.github.com"
args[-1] = "http://127.0.0.1:{server.server_port}" + url.path + ("?" + url.query if url.query else "")
os.execv("/usr/bin/curl", ["/usr/bin/curl", *args])
''')
    shim.chmod(0o700)
    key = tmp_path / "intake.key"
    key.write_bytes(b"owned-test-intake-key-32-bytes!!!!")
    key.chmod(0o600)
    subscriptions = tmp_path / "subscriptions.json"
    subscriptions.write_text(json.dumps({r: {"secret": "owned-route-secret"}
                                       for r in ("wdg-ci", "dailycheck-deploy")}))
    env = {k: v for k, v in candidate_python_env(SOURCE).items()
           if not k.startswith(("DAILYCHECK_", "GITEA_", "GITHUB_", "GH_"))}
    env.update({
        "PYTHONDONTWRITEBYTECODE": "1", "WEBHOOK_SECRET": "owned-hook-secret",
        "DAILYCHECK_HOSTING_PROVIDER": "github", "DAILYCHECK_GITHUB_LANE": "ordinary",
        "DAILYCHECK_GITHUB_INTAKE_ENABLED": "1",
        "DAILYCHECK_GITHUB_INTAKE_SINCE_MS": str(int(time.time() * 1000) - 1000),
        "DAILYCHECK_GITHUB_INTAKE_TARGETS": json.dumps([{"pr": 160, "head": SHA}]),
        "DAILYCHECK_GITHUB_INTAKE_KEY_FILE": str(key),
        "DAILYCHECK_GITHUB_READ_TOKEN": "owned-fake-read",
        "DAILYCHECK_DEPLOY_STATE_DIR": str(tmp_path / "deploy-state"),
        "DAILYCHECK_CURL": str(shim), "DAILYCHECK_GITHUB_SHADOW_DIR": str(tmp_path),
        "INBOX_PATH": str(tmp_path / "inbox.jsonl"),
        "PROCESSED_PATH": str(tmp_path / "processed.jsonl"),
        "DAILYCHECK_PYTHON": sys.executable,
        "DAILYCHECK_EVENT_REDUCER": str(SOURCE / "scripts/prod_agent/event_reducer.py"),
        "DAILYCHECK_EVENT_REDUCER_DB": str(tmp_path / "state.sqlite3"),
        "DAILYCHECK_REDUCER_DEBOUNCE_MS": "0",
        "DAILYCHECK_HERMES_SENDER": str(SOURCE / "scripts/prod_agent/hermes_webhook_sender.py"),
        "DAILYCHECK_HERMES_SUBSCRIPTIONS": str(subscriptions),
        "DAILYCHECK_HERMES_WEBHOOK_URL": f"http://127.0.0.1:{server.server_port}",
        "TMPDIR": str(tmp_path),
    })
    initialized = subprocess.run([sys.executable, str(SOURCE / "scripts/blue_green/deploy_state.py"),
                                  "init", "--sha", BASE], env=env, capture_output=True, text=True)
    assert initialized.returncode == 0, initialized.stderr
    receiver = subprocess.Popen([sys.executable, "-c", '''
from werkzeug.serving import make_server
from scripts.blue_green.webhook_receiver import app
server = make_server("127.0.0.1", 0, app, threaded=True)
print(server.server_port, flush=True)
server.serve_forever()
'''], cwd=SOURCE, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    port = int(receiver.stdout.readline())

    class Lane:
        def post(self, data=None, event="check_suite", delivery="owned-1", signature=True):
            if data is None:
                data = {"repository": {"id": REPO_ID, "full_name": REPO},
                        "action": "completed", "check_suite": {
                            "id": 140, "head_sha": SHA, "conclusion": "success"}}
            raw = json.dumps(data).encode()
            sig = "sha256=" + hmac.new(b"owned-hook-secret", raw, hashlib.sha256).hexdigest()
            req = urllib.request.Request(f"http://127.0.0.1:{port}/webhook", data=raw,
                headers={"Content-Type": "application/json", "X-GitHub-Event": event,
                         "X-GitHub-Delivery": delivery,
                         "X-Hub-Signature-256": sig if signature else "sha256=wrong"})
            try:
                with urllib.request.urlopen(req, timeout=10) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as exc:
                code = exc.code
                exc.close()
                return code, {}

        def adapter(self, payload=None):
            return subprocess.run(["bash", str(SOURCE / "deploy/scripts/dc-notify-prod-agent.sh"),
                                   *([] if payload is not None else ["--drain"])],
                                  cwd=SOURCE, env=env, text=True, input=json.dumps(payload)
                                  if payload is not None else None,
                                  capture_output=True, timeout=30)

        def records(self):
            path = Path(env["INBOX_PATH"])
            return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    obj = Lane()
    obj.env, obj.table, obj.posts, obj.reads = env, table, posts, reads
    obj.transport = transport
    try:
        yield obj
    finally:
        receiver.terminate()
        receiver.wait(timeout=10)
        receiver.stdout.close()
        with pytest.raises(ChildProcessError):
            os.waitpid(receiver.pid, os.WNOHANG)
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
        assert not thread.is_alive()
        save_proof("ordinary-" + hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:16], {
            "test": request.node.nodeid, "receiver_native_rc": receiver.returncode,
            "receiver_wait_ECHILD": True, "owned_http_thread_stopped": True,
            "api_reads": reads, "hermes_posts": posts, "synthetic_only": True})


def test_signed_current_exact_h_reaches_existing_consumer_once(lane):
    assert lane.post()[0] == 202
    rows = lane.records()
    assert len(rows) == 1
    result = lane.adapter(rows[0])
    assert result.returncode in (0, 75), result.stderr
    drained = lane.adapter()
    assert drained.returncode == 0, drained.stderr
    assert len(lane.posts) == 1, (result.stdout, drained.stdout)
    assert lane.posts[0]["path"] == "/webhooks/wdg-ci"
    assert lane.posts[0]["event"] == "check_suite"
    assert lane.posts[0]["body"]["trigger_name"] == "MERGE_READY"
    assert lane.post()[1]["status"] == "duplicate"
    assert lane.adapter(rows[0]).returncode == 0
    assert lane.adapter().returncode == 0
    assert len(lane.posts) == 1


@pytest.mark.parametrize("fault", ["pending", "partial", "failed", "wrong-app", "forged-check",
                                   "closed", "draft", "head-change", "protection", "review"])
def test_event_success_never_substitutes_for_live_truth(lane, fault):
    if fault in {"pending", "failed"}:
        value = "in_progress" if fault == "pending" else "completed"
        conclusion = None if fault == "pending" else "failure"
        for row in lane.table[ROOT + "/actions/runs/40/jobs?page=1"]["data"]["jobs"]:
            row.update(status=value, conclusion=conclusion)
        for row in lane.table[ROOT + f"/commits/{SHA}/check-runs?page=1"]["data"]["check_runs"]:
            row.update(status=value, conclusion=conclusion)
    elif fault == "partial":
        lane.table[ROOT + "/actions/runs/40/jobs?page=1"]["data"]["jobs"].pop()
    elif fault in {"wrong-app", "forged-check"}:
        checks = lane.table[ROOT + f"/commits/{SHA}/check-runs?page=1"]["data"]["check_runs"]
        if fault == "wrong-app":
            checks[0]["app"]["id"] = 99
        else:
            checks[0]["id"] = 99  # Same name, but not the job-bound check ID.
    elif fault == "protection":
        lane.table[ROOT + "/branches/main/protection"]["data"]["enforce_admins"]["enabled"] = False
    elif fault == "review":
        lane.table[ROOT + "/pulls/160/reviews?page=1"]["data"] = [
            {"id": 8, "state": "CHANGES_REQUESTED", "user": {"id": 9}, "commit_id": SHA}]
    else:
        pull = lane.table[ROOT + "/pulls/160"]["data"]
        if fault == "head-change":
            pull["head"]["sha"] = "c" * 40
        else:
            pull["state" if fault == "closed" else "draft"] = "closed" if fault == "closed" else True
    assert lane.post()[0] == 202
    result = lane.adapter(lane.records()[0])
    assert result.returncode == 0, result.stderr
    assert lane.adapter().returncode == 0
    assert lane.posts == []


@pytest.mark.parametrize("fault", ["signature", "repo", "repo-float", "unadmitted", "off",
                                   "shadow", "tampered", "epoch", "unsigned"])
def test_untrusted_or_paused_work_never_reaches_hermes(lane, fault):
    if fault in {"signature", "repo", "repo-float"}:
        data = {"repository": {"id": REPO_ID, "full_name": REPO}, "action": "completed",
                "check_suite": {"head_sha": SHA, "conclusion": "success"}}
        if fault == "repo":
            data["repository"]["full_name"] = "other/repo"
        if fault == "repo-float":
            data["repository"]["id"] = float(REPO_ID)
        status, _ = lane.post(data, signature=fault != "signature")
        assert status == (401 if fault == "signature" else 400)
        assert lane.records() == []
    else:
        assert lane.post()[0] == 202
        payload = lane.records()[0]
        if fault == "unadmitted":
            lane.env["DAILYCHECK_GITHUB_INTAKE_TARGETS"] = "[]"
        elif fault == "off":
            lane.env["DAILYCHECK_GITHUB_INTAKE_ENABLED"] = "0"
        elif fault == "shadow":
            payload["intake_mode"] = "shadow"
        elif fault == "tampered":
            payload["pr_number"] = 15
        elif fault == "epoch":
            lane.env["DAILYCHECK_GITHUB_INTAKE_SINCE_MS"] = str(int(time.time() * 1000))
        elif fault == "unsigned":
            payload.pop("ordinary_provenance")
        assert lane.adapter(payload).returncode == 0
    assert lane.adapter().returncode == 0
    assert not lane.posts


def test_prod_approval_is_after_wake_and_ready_for_review_is_a_hint(lane):
    lane.table[ROOT + "/pulls/160"]["data"]["mergeable_state"] = "blocked"
    data = {"number": 160, "action": "ready_for_review",
            "repository": {"id": REPO_ID, "full_name": REPO},
            "pull_request": lane.table[ROOT + "/pulls/160"]["data"]}
    assert lane.post(data, event="pull_request")[0] == 202
    assert lane.adapter(lane.records()[0]).returncode == 75
    assert lane.adapter().returncode == 0
    assert len(lane.posts) == 1
    assert lane.posts[0]["body"]["review_phase"] == "independent_prod_after_wake"


def test_queued_head_invalidated_before_drain_and_native_failure_never_cleared(lane):
    assert lane.post()[0] == 202
    assert lane.adapter(lane.records()[0]).returncode == 75
    lane.table[ROOT + "/pulls/160"]["data"]["head"]["sha"] = "c" * 40
    assert lane.adapter().returncode == 0
    assert not lane.posts
    state_path = Path(lane.env["DAILYCHECK_DEPLOY_STATE_DIR"]) / "state.json"
    state = json.loads(state_path.read_text())
    state["last_attempt"] = {"pr": 160, "sha": SHA, "result": "failed"}
    state_path.write_text(json.dumps(state))
    lane.table[ROOT + "/pulls/160"]["data"]["head"]["sha"] = SHA
    assert lane.post(delivery="owned-2")[0] == 202
    before = state_path.read_bytes()
    assert lane.adapter(lane.records()[-1]).returncode == 0
    assert lane.adapter().returncode == 0
    assert state_path.read_bytes() == before
    assert not lane.posts


@pytest.mark.parametrize("publication_available", [True, False])
def test_real_durable_failure_reaches_feedback_and_unavailable_publication_reconciles(
    lane, request, publication_available
):
    from tests.test_deploy_publication_wiring import PR
    from tests.test_deploy_publication_wiring import SHA as OUTCOME_SHA

    sb = request.getfixturevalue("sandbox")
    targets = json.dumps([{"pr": PR, "head": OUTCOME_SHA}])
    for key, value in lane.env.items():
        if key.startswith("DAILYCHECK_GITHUB_INTAKE_"):
            sb["env"][key] = value
    sb["env"]["DAILYCHECK_GITHUB_INTAKE_TARGETS"] = targets
    sb["env"]["DAILYCHECK_GITHUB_INTAKE_SINCE_MS"] = str(int(time.time() * 1000) - 3000)
    lane.env.update({key: value for key, value in sb["env"].items()
                     if key.startswith("DAILYCHECK_GITHUB_INTAKE_")})
    lane.env["DAILYCHECK_DEPLOY_STATE_DIR"] = str(sb["state"])
    sb["api"].comment_status = 201 if publication_available else 503
    (sb["dir"] / "up.sh").write_text(f"#!/bin/sh\necho '{PRIVATE_CANARY}'\nexit 7\n")
    result = _pipe(sb, str(PR), OUTCOME_SHA, "--observe-min", "0")
    assert result.returncode == 20, result.stdout + result.stderr
    assert (sb["shadow"] / "inbox.jsonl").exists(), result.stdout + result.stderr
    rows = [json.loads(line) for line in (sb["shadow"] / "inbox.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["intake_mode"] == "ordinary"
    before = _state(sb)
    assert lane.adapter(rows[0]).returncode == 75
    assert lane.adapter().returncode == 0
    assert len(lane.posts) == 1
    posted = lane.posts[0]
    assert posted["path"] == "/webhooks/dailycheck-deploy" and posted["event"] == "deploy_result"
    body = posted["body"]
    assert body["deploy_rc"] == 20 and body["deployment_actionable"] is False
    assert body["generation"] == before["generation"] and body["public_receipt"]
    assert PRIVATE_CANARY not in json.dumps(body) and str(sb["tmp"]) not in json.dumps(body)
    assert body["publication_state"] == ("linked" if publication_available else "unconfirmed")
    assert lane.adapter(rows[0]).returncode == 0
    assert lane.adapter().returncode == 0 and len(lane.posts) == 1
    assert _state(sb) == before  # No deploy, grant, failed-H clearing or generation advance.
    if publication_available:
        public = sb["api"].comments[0]["body"]
        assert "| RC | 20 |" in public and body["public_receipt"] in public
        assert PRIVATE_CANARY not in public
    else:
        poll = subprocess.run([sys.executable, "-c", '''
from scripts.prod_agent.pr_poll import _report_block
print(_report_block())
'''], cwd=SOURCE, env=sb["env"], text=True, capture_output=True, timeout=15)
        assert poll.returncode == 0, poll.stderr
        assert "publication failed or unconfirmed" in poll.stdout and f"PR #{PR}" in poll.stdout
    private = "".join(path.read_text() for path in (sb["shadow"] / "reports").glob("*.md"))
    assert PRIVATE_CANARY in private
    # Re-running a report from before activation cannot re-label/re-send a historical outcome.
    lane.env["DAILYCHECK_GITHUB_INTAKE_SINCE_MS"] = str(int(time.time() * 1000))
    retry_env = dict(sb["env"], DAILYCHECK_GITHUB_INTAKE_SINCE_MS=lane.env["DAILYCHECK_GITHUB_INTAKE_SINCE_MS"])
    public_attempts = len(sb["api"].posts)
    _report(sb, "--pr", str(PR), "--sha", OUTCOME_SHA, "--rc", "20", "--stage", "up", env=retry_env)
    assert len(sb["api"].posts) == public_attempts, "Historical activation must not resend a report"
    assert len((sb["shadow"] / "inbox.jsonl").read_text().splitlines()) == 1
    assert lane.adapter(rows[0]).returncode == 0 and len(lane.posts) == 1


def dispatch(lane, *args):
    return subprocess.run([sys.executable, str(SOURCE / "scripts/prod_agent/github_dispatch.py"), *args],
                          cwd=SOURCE, env=lane.env, text=True, capture_output=True, timeout=30)


def test_policy_preflight_is_before_network_claim_or_refresh(lane, tmp_path):
    result = dispatch(lane, "--policy-preflight")
    assert result.returncode == 0 and json.loads(result.stdout)["admitted"] is True
    assert not lane.reads and not lane.posts
    assert not Path(lane.env["DAILYCHECK_EVENT_REDUCER_DB"]).exists()
    lane.env["DAILYCHECK_GITHUB_INTAKE_TARGETS"] = "[]"
    assert dispatch(lane, "--policy-preflight").returncode == 78
    assert dispatch(lane).returncode == 0
    assert not lane.reads and not lane.posts


def test_cron_and_signed_receiver_share_one_durable_admission(lane):
    assert lane.post()[0] == 202
    assert lane.adapter(lane.records()[0]).returncode == 75
    result = dispatch(lane)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(lane.posts) == 1
    assert lane.posts[0]["body"]["dispatch_phase"] == "prod_review"
    assert dispatch(lane).returncode == 0
    assert lane.adapter(lane.records()[0]).returncode == 0
    assert len(lane.posts) == 1
    status = json.loads(result.stdout)
    assert status["dispatch_reconciliation"][0]["status"] == "awaiting_agent_receipt"


def test_reviewed_automatic_range_admits_future_pr_without_exact_h_permit(lane, tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"version": 1, "repository_id": REPO_ID, "repository": REPO,
        "mode": "automatic", "since_ms": int(lane.env["DAILYCHECK_GITHUB_INTAKE_SINCE_MS"]),
        "review_targets": [], "automatic_pr_range": {"min": 19, "max": 200},
        "excluded_open_prs": [15, 17, 18], "review_actor_ids": [9001]}))
    path.chmod(0o600)
    lane.env["DAILYCHECK_GITHUB_INTAKE_POLICY_FILE"] = str(path)
    assert dispatch(lane, "--policy-preflight").returncode == 0
    result = dispatch(lane)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(lane.posts) == 1
    body = lane.posts[0]["body"]
    assert body["policy_sha256"] and body["admission_id"] and body["observed_ci_run_id"] == 40
    assert body["observed_ci_attempt"] == 2 and body["trigger_id"]


def test_lost_http_response_is_unknown_and_never_automatically_replayed(lane):
    lane.transport["drop"] = True
    assert lane.post()[0] == 202
    assert lane.adapter(lane.records()[0]).returncode == 75
    assert lane.adapter().returncode == 0
    assert len(lane.posts) == 1
    assert lane.adapter().returncode == 0
    result = dispatch(lane)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(lane.posts) == 1
    assert "delivery_unknown" in result.stdout


def test_live_native_process_lock_blocks_review_wake(lane):
    holder = subprocess.Popen([sys.executable, "-c", '''
from scripts.blue_green import deploy_state
with deploy_state.attempt_lock(deploy_state.state_dir()):
    print("held", flush=True)
    input()
'''], cwd=SOURCE, env=lane.env, text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        assert holder.stdout.readline().strip() == "held"
        assert lane.post()[0] == 202
        assert lane.adapter(lane.records()[0]).returncode == 0
        assert lane.adapter().returncode == 0
        assert not lane.posts
    finally:
        holder.communicate("release\n", timeout=10)
        with pytest.raises(ChildProcessError):
            os.waitpid(holder.pid, os.WNOHANG)


@pytest.mark.parametrize("review_state", ["APPROVED", "CHANGES_REQUESTED"])
def test_formal_pinned_review_completes_only_this_review_phase(lane, tmp_path, review_state):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"version": 1, "repository_id": REPO_ID, "repository": REPO,
        "mode": "automatic", "since_ms": int(lane.env["DAILYCHECK_GITHUB_INTAKE_SINCE_MS"]),
        "review_targets": [], "automatic_pr_range": {"min": 19, "max": 200},
        "excluded_open_prs": [15, 17, 18], "review_actor_ids": [9001]}))
    path.chmod(0o600)
    lane.env["DAILYCHECK_GITHUB_INTAKE_POLICY_FILE"] = str(path)
    lane.table[ROOT + "/pulls/160"]["data"]["user"] = {"id": 8001}
    assert dispatch(lane).returncode == 0
    assert len(lane.posts) == 1
    time.sleep(1.05)  # GitHub formal review timestamps have second precision.
    row = {"id": 7001, "user": {"id": 9001, "type": "Bot"}, "commit_id": SHA,
           "state": review_state, "submitted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    lane.table[ROOT + "/pulls/160/reviews?page=1"]["data"] = [row]
    result = dispatch(lane)
    assert result.returncode == 0 and len(lane.posts) == 1, result.stdout + result.stderr
    import sqlite3

    with sqlite3.connect(lane.env["DAILYCHECK_EVENT_REDUCER_DB"]) as conn:
        status, raw = conn.execute("SELECT last_error,payload_json FROM trigger_queue").fetchone()
    assert status == ("review_approved" if review_state == "APPROVED" else "review_blocker")
    assert json.loads(raw)["review_observation"]["review_id"] == 7001
    # A later fixed H can be reviewed automatically; the old H is never re-enqueued.
    changed = json.loads(json.dumps(lane.table).replace(SHA, "c" * 40))
    changed[ROOT + "/pulls/160/reviews?page=1"]["data"][0]["commit_id"] = SHA
    lane.table.clear()
    lane.table.update(changed)
    assert dispatch(lane).returncode == 0
    assert len(lane.posts) == 2 and lane.posts[-1]["body"]["pr_head_sha"] == "c" * 40


@pytest.mark.parametrize("fault", ["actor", "author", "head", "old-time", "commented", "dismissed", "pending", "malformed", "ci-version"])
def test_review_receipt_negative_controls_remain_unknown(lane, tmp_path, fault):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"version": 1, "repository_id": REPO_ID, "repository": REPO,
        "mode": "automatic", "since_ms": int(lane.env["DAILYCHECK_GITHUB_INTAKE_SINCE_MS"]),
        "review_targets": [], "automatic_pr_range": {"min": 19, "max": 200},
        "excluded_open_prs": [15, 17, 18], "review_actor_ids": [9001]}))
    path.chmod(0o600)
    lane.env["DAILYCHECK_GITHUB_INTAKE_POLICY_FILE"] = str(path)
    lane.table[ROOT + "/pulls/160"]["data"]["user"] = {"id": 8001}
    assert dispatch(lane).returncode == 0
    time.sleep(1.05)
    row = {"id": 7001, "user": {"id": 9001, "type": "Bot"}, "commit_id": SHA,
           "state": "APPROVED", "submitted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if fault == "actor":
        row["user"]["id"] = 9999
    elif fault == "author":
        lane.table[ROOT + "/pulls/160"]["data"]["user"]["id"] = 9001
    elif fault == "head":
        row["commit_id"] = "c" * 40
    elif fault == "old-time":
        row["submitted_at"] = "2020-01-01T00:00:00Z"
    elif fault in {"commented", "dismissed", "pending"}:
        row["state"] = fault.upper()
    elif fault == "malformed":
        row["id"] = 7001.0
    elif fault == "ci-version":
        lane.table[ROOT + "/actions/workflows/12/runs?page=1"]["data"]["workflow_runs"][0]["run_attempt"] += 1
    lane.table[ROOT + "/pulls/160/reviews?page=1"]["data"] = [row]
    result = dispatch(lane)
    assert result.returncode == 0 and "awaiting_agent_receipt" in result.stdout
    assert len(lane.posts) == 1


def test_two_real_drainers_claim_once_and_completed_payload_cannot_be_resent(lane, tmp_path):
    assert lane.post()[0] == 202
    assert lane.adapter(lane.records()[0]).returncode == 75
    command = ["bash", str(SOURCE / "deploy/scripts/dc-notify-prod-agent.sh"), "--drain"]
    children = [subprocess.Popen(command, cwd=SOURCE, env=lane.env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
    for child in children:
        stdout, stderr = child.communicate(timeout=30)
        assert child.returncode == 0, stdout + stderr
        with pytest.raises(ChildProcessError):
            os.waitpid(child.pid, os.WNOHANG)
    assert len(lane.posts) == 1
    body = tmp_path / "completed-payload.json"
    body.write_text(json.dumps(lane.posts[0]["body"]))
    replay = subprocess.run([sys.executable, lane.env["DAILYCHECK_HERMES_SENDER"],
        "--subscriptions", lane.env["DAILYCHECK_HERMES_SUBSCRIPTIONS"],
        "--base-url", lane.env["DAILYCHECK_HERMES_WEBHOOK_URL"],
        "--route", "wdg-ci", "--event", "check_suite", "--delivery-id", "different-replay",
        "--body-file", str(body)], cwd=SOURCE, env=lane.env, text=True, capture_output=True, timeout=30)
    assert replay.returncode == 5 and "ordinary_dispatch_not_claimed" in replay.stdout
    assert len(lane.posts) == 1


def test_activation_does_not_upgrade_a_preexisting_shadow_queue(lane):
    from tests.test_github_shadow_boundary import event, seed_delayed_row

    seed_delayed_row(Path(lane.env["DAILYCHECK_EVENT_REDUCER_DB"]), event())
    result = lane.adapter()
    assert result.returncode == 0 and "shadow_rejected" in result.stdout
    assert lane.posts == []


def test_pending_ci_after_enqueue_waits_and_claim_binds_fresh_attempt(lane):
    assert lane.post()[0] == 202
    assert lane.adapter(lane.records()[0]).returncode == 75
    jobs = lane.table[ROOT + "/actions/runs/40/jobs?page=1"]["data"]["jobs"]
    checks = lane.table[ROOT + f"/commits/{SHA}/check-runs?page=1"]["data"]["check_runs"]
    run = lane.table[ROOT + "/actions/workflows/12/runs?page=1"]["data"]["workflow_runs"][0]
    run.update(run_attempt=3, status="in_progress", conclusion=None)
    for row in jobs:
        row.update(run_attempt=3, status="in_progress", conclusion=None)
    for row in checks:
        row.update(status="in_progress", conclusion=None)
    result = lane.adapter()
    assert result.returncode == 0 and "ordinary_readiness_wait" in result.stdout
    assert not lane.posts
    run.update(status="completed", conclusion="success")
    for row in [*jobs, *checks]:
        row.update(status="completed", conclusion="success")
    assert lane.adapter().returncode == 0
    assert len(lane.posts) == 1 and lane.posts[0]["body"]["observed_ci_attempt"] == 3


def test_known_unsent_startup_failure_has_a_finite_claim_budget(lane):
    assert lane.post()[0] == 202
    assert lane.adapter(lane.records()[0]).returncode == 75
    lane.env["DAILYCHECK_HERMES_SUBSCRIPTIONS"] += ".absent"
    assert lane.adapter().returncode == 75
    time.sleep(0.6)
    assert lane.adapter().returncode == 75
    time.sleep(0.6)
    assert lane.adapter().returncode == 0
    assert lane.adapter().returncode == 0
    import sqlite3

    with sqlite3.connect(lane.env["DAILYCHECK_EVENT_REDUCER_DB"]) as conn:
        attempts, state, reason = conn.execute("SELECT attempts,state,last_error FROM trigger_queue").fetchone()
    assert (attempts, state, reason) == (3, "done", "delivery_unknown")
    assert lane.posts == []


def test_pinned_hosting_guard_stays_closed_for_a_valid_new_ordinary_envelope(lane):
    assert lane.post()[0] == 202
    legacy = subprocess.run([sys.executable, "-c", '''
import json,sys
from scripts.hosting import HostingError,require_production_work
try:
    require_production_work(json.load(sys.stdin))
except HostingError as exc:
    print(json.dumps({"reason":str(exc),"new_intake_imported":"scripts.prod_agent.github_intake" in sys.modules}))
else:
    raise AssertionError("legacy guard unexpectedly admitted ordinary work")
'''], cwd=SOURCE, env=lane.env, input=json.dumps(lane.records()[0]), text=True,
                            capture_output=True, timeout=15)
    assert legacy.returncode == 0, legacy.stderr
    assert json.loads(legacy.stdout) == {"reason": "github_shadow_only", "new_intake_imported": False}
    assert not lane.posts
