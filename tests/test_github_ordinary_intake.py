"""Signed receiver -> real adapter/reducer/sender -> owned HTTP sinks only."""

from __future__ import annotations

import hashlib
import hmac
import http.server
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.conftest import candidate_python_env
from tests.test_github_transport import BASE, REPO, REPO_ID, ROOT, SHA, evidence, pr

SOURCE = Path(__file__).resolve().parents[1]


@pytest.fixture
def lane(tmp_path):
    table = evidence()
    table[ROOT + "/branches/main/protection"] = {"data": {
        "required_status_checks": {"strict": True, "checks": [
            {"context": name, "app_id": 15368}
            for name in ("test", "agent-framework-reports")]},
        "required_pull_request_reviews": {"required_approving_review_count": 1},
    }}
    table[ROOT + "/pulls/160/reviews?page=1"] = {"data": []}
    posts, reads = [], []

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
