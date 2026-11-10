"""Issue163: real pipeline outcome -> durable receipt -> trusted adapter -> loopback HTTP.

Native only: the real `deploy_pipeline.sh`, `deploy_state.py`, `deploy_report.py`,
`deploy_triage_pure.py` and the publication adapter are copied into a disposable
sandbox.  `systemctl`/`nginx`/journal traffic stages are tiny owned fakes; the
GitHub API is a real loopback HTTP server reached through a curl URL shim.  All
state, logs, shadow paths and bindings stay under `tmp_path`.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts.hosting import AccessRole, HostingClient, HostingConfig, HostingError

BG_DIR = Path(__file__).resolve().parent.parent / "scripts" / "blue_green"
REPO_ROOT = BG_DIR.parents[1]
SHA = "abcdef1234567890abcdef1234567890abcdef12"
OTHER_SHA = "f" * 40
PR = 42
CODE = "DUIDUI-Studio/DailyCheck"
OPS = "DUIDUI-Studio/DailyCheck-ops"
API = "https://api.github.com"
WEB = "https://github.com"
PRIVATE_CANARY = "SYNTHETIC_PRIVATE_OUTCOME_163"
PR_BODY = """## 报告 1/4：需求对齐

| ID | 需求 | 状态 |
|----|------|------|
| R-001 | 部署结果可信回写 | ✅ 已实现 |

## Checklist
"""


def bindings(number: int = 7) -> dict:
    return {
        "version": 1,
        "bindings": [{
            "pr": PR,
            "public": {"provider": "github", "host": "github.com",
                       "repository_id": 1448986421, "repository": CODE},
            "source": {"provider": "github", "host": "github.com",
                       "repository_id": 1449125557, "repository": OPS,
                       "kind": "issue", "number": number},
        }],
    }


class Loopback:
    """Minimal GitHub API; records every request and can drop a POST response."""

    def __init__(self):
        self.requests: list[dict] = []
        self.comments: list[dict] = []
        self.drop_comment = False
        self.comment_status = 201
        self.pr_body = PR_BODY
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def posts(self) -> list[dict]:
        return [row for row in self.requests if row["method"] != "GET"]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _handler(self):
        loop = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.respond()

            def do_POST(self):
                self.respond()

            def respond(self):
                parsed = urlsplit(self.path)
                path, query = parsed.path, parse_qs(parsed.query)
                raw = (self.rfile.read(int(self.headers["Content-Length"]))
                       if self.headers.get("Content-Length") else b"")
                payload = json.loads(raw) if raw else None
                loop.requests.append({
                    "method": self.command, "path": path, "query": query,
                    "payload": payload, "auth": self.headers.get("Authorization"),
                })
                status, data = 200, None
                if path in {f"/repos/{CODE}", f"/repos/{OPS}"}:
                    private = path.endswith(OPS)
                    repo = CODE if private is False else OPS
                    data = {"id": 1449125557 if private else 1448986421,
                            "full_name": repo, "private": private,
                            "visibility": "private" if private else "public",
                            "has_issues": True, "archived": False, "disabled": False,
                            "url": API + path, "html_url": WEB + "/" + repo}
                elif path == f"/repos/{CODE}/pulls/{PR}":
                    data = {"number": PR, "html_url": WEB + f"/{CODE}/pull/{PR}",
                            "head": {"sha": SHA},
                            "base": {"repo": {"id": 1448986421}},
                            "body": loop.pr_body}
                elif path == f"/repos/{CODE}/issues/{PR}/comments":
                    if self.command == "POST":
                        status = loop.comment_status
                        data = {"id": len(loop.comments) + 201,
                                "issue_url": API + f"/repos/{CODE}/issues/{PR}",
                                **payload}
                        loop.comments.append(data)
                        if loop.drop_comment:
                            self.connection.shutdown(socket.SHUT_RDWR)
                            self.connection.close()
                            return
                    else:
                        data = loop.comments
                else:
                    status, data = 404, {}
                if isinstance(data, list):
                    page = int(query.get("page", [1])[0])
                    size = int(query.get("per_page", [100])[0])
                    data = data[(page - 1) * size:page * size]
                encoded = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        return Handler


@pytest.fixture()
def loopback(tmp_path):
    api = Loopback()
    shim = tmp_path / "loopback-curl"
    shim.write_text(
        f"""#!{sys.executable}
import os, sys
from urllib.parse import urlsplit
args = sys.argv[1:]
url = urlsplit(args[-1])
assert url.scheme == "https" and url.netloc == "api.github.com", args[-1]
args[-1] = "http://127.0.0.1:{api.server.server_port}" + url.path + (
    "?" + url.query if url.query else "")
os.execv("/usr/bin/curl", ["/usr/bin/curl", *args])
""")
    shim.chmod(0o755)
    try:
        yield api, shim
    finally:
        api.close()


@pytest.fixture()
def sandbox(tmp_path, loopback):
    api, shim = loopback
    sb = tmp_path / "scripts" / "blue_green"
    sb.mkdir(parents=True)
    for name in ("deploy_state.py", "deploy_pipeline.sh", "deploy_report.py",
                 "deploy_triage_pure.py", "publication_authority.py"):
        shutil.copy2(BG_DIR / name, sb / name)
        (sb / name).chmod(0o755)
    calls = tmp_path / "calls.txt"
    for name in ("up.sh", "verify.sh", "promote.sh", "switch.sh"):
        path = sb / name
        path.write_text(
            "#!/usr/bin/env bash\n"
            f'echo "{name} $*" >> "{calls}"\n'
            f"echo '{PRIVATE_CANARY} log line'\n"
            "exit 0\n")
        path.chmod(0o755)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "bindings.json").write_text(json.dumps(bindings()))
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("DAILYCHECK_GITHUB_") or key.startswith("DAILYCHECK_DEPLOY_"):
            env.pop(key, None)
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "GITEA_TOKEN", "GITEA_TOKEN_FILE"):
        env.pop(key, None)
    env.update({
        "PYTHON": sys.executable,
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": str(REPO_ROOT),
        "DAILYCHECK_DEPLOY_STATE_DIR": str(state_dir),
        "DAILYCHECK_PIPELINE_LOG": str(tmp_path / "pipeline.log"),
        "DAILYCHECK_PIPELINE_TICK_SEC": "1",
        "DAILYCHECK_DEPLOY_REPORT": "1",
        "DAILYCHECK_DASHBOARD_HOOK": "0",
        "DAILYCHECK_BG_LOCK_FILE": str(tmp_path / "bg.lock"),
        "DAILYCHECK_HOSTING_PROVIDER": "github",
        "DAILYCHECK_GITHUB_LANE": "ordinary",
        "DAILYCHECK_GITHUB_REPO": CODE,
        "DAILYCHECK_GITHUB_SHADOW_DIR": str(shadow),
        "DAILYCHECK_DEPLOY_EVENT_LOG": str(shadow / "events.jsonl"),
        "DAILYCHECK_PROD_AGENT_INBOX": str(shadow / "inbox.jsonl"),
        "DAILYCHECK_CURL": str(shim),
        "DAILYCHECK_GITHUB_READ_TOKEN": "synthetic-read-token",
        "DAILYCHECK_GITHUB_REPORT_TOKEN": "synthetic-report-token",
        "DAILYCHECK_GITEA_TOKEN_FILE": str(tmp_path / "absent-token"),
    })
    # Report tests exercise the real fenced pipeline with owned Git/clock/merge ports.
    # Exact Git ancestry and atomic hosting finalization have separate real-caller tests.
    tools = tmp_path / "bin"
    tools.mkdir()
    git = tools / "git"
    git.write_text(f'''#!{sys.executable}
import sys
args = sys.argv[1:]
if args[:1] == ["-C"]: args = args[2:]
if args == ["remote", "get-url", "origin"]:
    print("https://github.com/{CODE}.git")
elif args and args[0] == "rev-parse": print({OTHER_SHA!r} if args[-1] == "origin/main" else {SHA!r})
elif args and args[0] in {{"fetch", "checkout", "merge-base", "diff-index"}}: pass
else: sys.exit(128)
''')
    git.chmod(0o755)
    sleeper = tools / "sleep"
    sleeper.write_text("#!/bin/sh\nexit 0\n")
    sleeper.chmod(0o755)
    clock = tmp_path / "clock"
    python = tools / "python"
    python.write_text(f'''#!{sys.executable}
import os, sys
from pathlib import Path
if sys.argv[1:] == ["-c", "import time; print(int(time.monotonic()))"]:
    p = Path({str(clock)!r})
    value = int(p.read_text()) if p.exists() else 0
    print(value)
    p.write_text(str(value + 300))
else: os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])
''')
    python.chmod(0o755)
    helper = tools / "merge"
    helper.write_text(f'''#!{sys.executable}
import json, sys
args = sys.argv[1:]
base = args[args.index("--base") + 1]
print(json.dumps({{"merged": args[0] == "merge", "sha": {SHA!r}, "merged_sha": {SHA!r},
                  "main_sha": {SHA!r}, "base": base}}))
''')
    helper.chmod(0o755)
    journal = tools / "journalctl"
    journal.write_text("#!/bin/sh\nexit 0\n")
    journal.chmod(0o755)
    probe = tools / "probe"
    probe.write_text("#!/bin/sh\nprintf 200\n")
    probe.chmod(0o755)
    env.update(PATH=str(tools) + os.pathsep + env["PATH"], PYTHON=str(python),
               DAILYCHECK_DEPLOY_MERGE_BIN=str(helper),
               DAILYCHECK_PIPELINE_JOURNALCTL=str(journal), DAILYCHECK_PIPELINE_CURL=str(probe))
    return {"dir": sb, "repo": tmp_path, "state": state_dir, "shadow": shadow,
            "env": env, "calls": calls, "tmp": tmp_path, "api": api}


def _pipe(sb, *args: str, env: dict | None = None):
    selected = env or sb["env"]
    if selected.get("DAILYCHECK_HOSTING_PROVIDER") == "github":
        args = list(args)
        if "--observe-min" in args:
            args[args.index("--observe-min") + 1] = "10"
        args += ["--candidate", "--merge-pr"]
    return subprocess.run(
        ["bash", str(sb["dir"] / "deploy_pipeline.sh"), *args],
        capture_output=True, text=True, env=selected, timeout=120)


def _report(sb, *args: str, env: dict | None = None):
    return subprocess.run(
        [sys.executable, str(sb["dir"] / "deploy_report.py"), "write", *args],
        capture_output=True, text=True, env=env or sb["env"], timeout=60)


def _state(sb) -> dict:
    path = sb["state"] / "state.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _outcome(sb):
    state = _state(sb)
    outcome_id = (state.get("deployed") or {}).get("outcome_id")
    return state, outcome_id, (state.get("outcomes") or {}).get(outcome_id)


def _success(sb):
    proc = _pipe(sb, str(PR), SHA, "--observe-min", "0")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc


def _private_text(sb) -> str:
    return "\n".join(p.read_text() for p in (sb["shadow"] / "reports").glob("*.md"))


def _prepare(sb):
    """Real owned success outcome with GitHub reporting intentionally disabled."""
    env = dict(sb["env"], DAILYCHECK_DEPLOY_REPORT="0")
    proc = _pipe(sb, str(PR), SHA, "--observe-min", "0", env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    state, outcome_id, outcome = _outcome(sb)
    assert outcome_id and outcome is not None
    assert not sb["api"].posts
    return state, outcome_id, outcome


def wait_file(path: Path, proc=None, timeout: float = 15.0) -> None:
    end = time.monotonic() + timeout
    while not path.exists():
        if proc is not None and proc.poll() is not None:
            raise AssertionError(f"process exited {proc.returncode} before {path}")
        assert time.monotonic() < end, f"barrier timed out: {path}"
        time.sleep(0.02)


def open_gate(gate: Path) -> None:
    with gate.open("wb", buffering=0) as fh:
        fh.write(b"G")


def gated_report(sb, *, rc: int = 0, stage: str = ""):
    """Real report CLI whose actual curl child waits immediately before POST."""
    ready = sb["tmp"] / "post-ready.json"
    gate = sb["tmp"] / "post-gate"
    done = sb["tmp"] / "post-done.json"
    for path in (ready, gate, done):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    os.mkfifo(gate, 0o600)
    shim = sb["tmp"] / "gated-curl"
    shim.write_text(
        f"""#!{sys.executable}
import json, os, subprocess, sys
from pathlib import Path
from urllib.parse import urlsplit
args = sys.argv[1:]
url = urlsplit(args[-1])
assert url.scheme == "https" and url.netloc == "api.github.com"
args[-1] = "http://127.0.0.1:{sb['api'].server.server_port}" + url.path + (
    "?" + url.query if url.query else "")
config = sys.stdin.read()
post = args[args.index("--request") + 1] == "POST"
if post:
    Path({str(ready)!r}).write_text(json.dumps({{
        "pid": os.getpid(), "ppid": os.getppid(),
        "fd_env": os.getenv("DAILYCHECK_DEPLOY_ATTEMPT_FD")}}))
    with open({str(gate)!r}, "rb", buffering=0) as fh:
        assert fh.read(1) == b"G"
proc = subprocess.run(["/usr/bin/curl", *args], input=config, text=True,
                      capture_output=True)
if post:
    Path({str(done)!r}).write_text(json.dumps(
        {{"pid": os.getpid(), "curl_exit": proc.returncode}}))
sys.stdout.write(proc.stdout)
sys.stderr.write(proc.stderr)
sys.exit(proc.returncode)
""")
    shim.chmod(0o700)
    env = dict(sb["env"], DAILYCHECK_CURL=str(shim))
    stdout = (sb["tmp"] / "retry.stdout").open("w")
    stderr = (sb["tmp"] / "retry.stderr").open("w")
    proc = subprocess.Popen(
        [sys.executable, str(sb["dir"] / "deploy_report.py"), "write",
         "--pr", str(PR), "--sha", SHA, "--rc", str(rc), "--stage", stage,
         "--log", str(sb["tmp"] / "pipeline.log"), "--repo", str(sb["tmp"])],
        env=env, stdout=stdout, stderr=stderr)
    stdout.close()
    stderr.close()
    wait_file(ready, proc)
    return proc, gate, done


def _acquire_process(sb):
    return subprocess.run(
        [sys.executable, str(sb["dir"] / "deploy_state.py"), "acquire",
         "--pr", "43", "--sha", OTHER_SHA],
        env=sb["env"], capture_output=True, text=True, timeout=30)


# ---------------------------------------------------------------- positive


def test_pipeline_owned_release_mints_receipt_and_publishes_typed_payload(sandbox):
    _success(sandbox)
    state, outcome_id, outcome = _outcome(sandbox)

    assert outcome_id and outcome, state
    assert state["generation"] == 1
    assert outcome["generation"] == 1
    assert outcome["result"] == "success" and outcome["rc"] == 0
    assert outcome["stage"] == "complete"
    assert outcome["sha"] == SHA and outcome["pr"] == PR
    assert outcome["verification"] == "normal"
    assert uuid.UUID(outcome["public_receipt"])
    assert outcome["public_receipt"] != outcome_id
    assert outcome["public_receipt"] != outcome["attempt_token"]
    assert outcome["hosting"] == {
        "provider": "github", "host": "github.com",
        "repository_id": 1448986421, "repository": CODE}
    assert outcome["facts"]["rc"] == 0 and outcome["facts"]["stage"] == "complete"
    assert outcome["payload_sha256"]
    assert state["deployed"]["outcome_id"] == outcome_id
    assert state["lock"] is None

    publication = state["publications"][outcome_id]
    assert publication["state"] == "linked"
    assert publication["comment_id"] == 201

    posts = sandbox["api"].posts
    assert len(posts) == 1, posts
    assert posts[0]["path"] == f"/repos/{CODE}/issues/{PR}/comments"
    body = posts[0]["payload"]["body"]
    assert "<!-- deploy-report:v1 -->" in body
    assert f"repo=1448986421 pr={PR} head={SHA} receipt={outcome['public_receipt']}" in body
    assert outcome["outcome_at"] in body
    assert "| RC | 0 |" in body and "| 阶段 | `complete` |" in body
    assert PRIVATE_CANARY not in body
    assert outcome["attempt_token"] not in body
    assert outcome_id not in body

    # Private diagnostics, events and inbox survive on the shadow lane.
    private = _private_text(sandbox)
    assert PRIVATE_CANARY in private
    assert outcome["verification"] in private
    assert (sandbox["shadow"] / "events.jsonl").is_file()
    assert (sandbox["shadow"] / "inbox.jsonl").is_file()


def test_idempotent_skip_reuses_receipt_without_new_outcome_or_post(sandbox):
    _success(sandbox)
    _, outcome_id, outcome = _outcome(sandbox)
    frozen = json.dumps(outcome["facts"], sort_keys=True)
    assert len(sandbox["api"].posts) == 1

    sandbox["calls"].unlink()
    again = _pipe(sandbox, str(PR), SHA, "--observe-min", "0")
    assert again.returncode == 0, again.stdout + again.stderr
    assert "已部署" in again.stdout
    assert not sandbox["calls"].exists()
    state, same_id, same = _outcome(sandbox)
    assert same_id == outcome_id
    assert json.dumps(same["facts"], sort_keys=True) == frozen
    assert same["public_receipt"] == outcome["public_receipt"]
    assert len(sandbox["api"].posts) == 1, "idempotent skip must not re-POST"


def test_ordinary_report_cli_private_sinks_and_durable_public_projection(sandbox):
    from tests.test_github_transport import proof

    sb = sandbox
    _prepare(sb)
    private = sb["tmp"] / "ordinary-private"
    env = dict(sb["env"], DAILYCHECK_DEPLOY_EVENT_LOG=str(private / "events.jsonl"),
               DAILYCHECK_PROD_AGENT_INBOX=str(private / "inbox.jsonl"))
    result = _report(sb, "--pr", str(PR), "--sha", SHA, "--rc", "0",
                     "--log", env["DAILYCHECK_PIPELINE_LOG"], "--repo", str(sb["repo"]), env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(sb["api"].posts) == 1
    body = sb["api"].posts[0]["payload"]["body"]
    assert PRIVATE_CANARY not in body and str(sb["tmp"]) not in body
    assert "synthetic" not in body and "| RC | 0 |" in body
    assert PRIVATE_CANARY in "".join(p.read_text() for p in (private / "reports").glob("*.md"))
    event = json.loads((private / "events.jsonl").read_text())
    inbox = json.loads((private / "inbox.jsonl").read_text())
    assert event["publication_rc"] == 0 and inbox["hosting_provider"] == "github"
    assert inbox["intake_mode"] == "shadow"
    proof("ordinary-report", {"native_exit": result.returncode,
          "stderr": result.stderr.replace(str(sb["tmp"]), "<owned-fixture>"),
          "post_path": sb["api"].posts[0]["path"], "public_body": body,
          "private_diagnostic_retained": True, "inbox_keeps_shadow_provenance": True})


def test_ordinary_unlinked_receipt_is_polled_even_without_event_or_inbox(sandbox):
    from tests.test_github_transport import proof

    sb = sandbox
    _prepare(sb)
    assert not (sb["shadow"] / "events.jsonl").exists()
    poll = subprocess.run([sys.executable, "-B", str(REPO_ROOT / "scripts/prod_agent/pr_poll.py")],
                          capture_output=True, text=True, env=sb["env"], timeout=30)
    assert poll.returncode == 0 and "report publication failed or unconfirmed" in poll.stdout
    assert f"PR #{PR} head={SHA}" in poll.stdout
    assert not sb["api"].posts
    proof("poll-unlinked-receipt", {"native_exit": poll.returncode, "stdout": poll.stdout,
                                   "post_count": 0, "event_file_absent": True})


def test_live_reporter_fence_blocks_successor_then_links_once(sandbox):
    _prepare(sandbox)
    proc, gate, done = gated_report(sandbox)
    try:
        successor = _acquire_process(sandbox)
        assert successor.returncode == 10, successor.stdout + successor.stderr
        open_gate(gate)
        wait_file(done)
        assert proc.wait(timeout=15) == 0
        state = _state(sandbox)
        outcome_id = next(iter(state["outcomes"]))
        assert state["publications"][outcome_id]["state"] == "linked"
        assert state["publications"][outcome_id]["comment_id"] == 201
        assert len(sandbox["api"].posts) == 1
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_http_mutator_retains_fence_after_reporter_death(sandbox):
    _prepare(sandbox)
    proc, gate, done = gated_report(sandbox)
    try:
        proc.kill()
        assert proc.wait(timeout=10) == -signal.SIGKILL
        # The reporter Python is gone, but the actual HTTP child still holds the
        # inherited/serialized process fence until it exits.
        successor = _acquire_process(sandbox)
        assert successor.returncode == 10, (
            "successor acquired while the actual HTTP mutator still held the fence")
        open_gate(gate)
        wait_file(done)
        late = None
        for _ in range(100):
            late = _acquire_process(sandbox)
            if late.returncode == 0:
                break
            time.sleep(0.05)
        assert late is not None and late.returncode == 0
        state = _state(sandbox)
        assert state["generation"] == 2
        assert len(sandbox["api"].posts) == 1
        outcome_id = next(iter(state["outcomes"]))
        assert state["publications"][outcome_id]["state"] == "uncertain"
        assert state["publications"][outcome_id]["comment_id"] == 0
        assert state["lock"]["pr"] == 43
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


# ---------------------------------------------------------------- negative


def test_owned_failed_deployment_retains_typed_failure_and_publishes_once(sandbox):
    (sandbox["dir"] / "up.sh").write_text(
        f"#!/bin/sh\necho '{PRIVATE_CANARY} failed stage'\nexit 7\n")
    proc = _pipe(sandbox, str(PR), SHA, "--observe-min", "0")
    assert proc.returncode == 20, proc.stdout + proc.stderr
    state = _state(sandbox)
    assert state["lock"] is None
    assert state["last_attempt"]["result"] == "failed"
    assert state["last_attempt"]["note"] == "up rc=20"
    assert state["last_attempt"]["outcome_id"]
    assert len(state["outcomes"]) == 1, "owned FAILED deployment must have a durable outcome"
    outcome = next(iter(state["outcomes"].values()))
    assert outcome["result"] == "failed" and outcome["rc"] == 20
    assert outcome["stage"] == "up"
    assert uuid.UUID(outcome["public_receipt"])
    assert outcome["deployment"] == {
        "provider": "github", "host": "github.com", "repository": CODE, "pr": PR}

    posts = sandbox["api"].posts
    assert len(posts) == 1, posts
    body = posts[0]["payload"]["body"]
    assert "| RC | 20 |" in body and "| 阶段 | `up` |" in body
    assert "部署结果 · 失败（rc=20）" in body
    events = [json.loads(line) for line in
              (sandbox["shadow"] / "events.jsonl").read_text().splitlines()]
    assert events[-1]["rc"] == 20 and events[-1]["stage"] == "up"
    assert events[-1]["report_comment"] == 201


def test_dry_run_mints_no_outcome_and_publishes_nothing(sandbox):
    proc = _pipe(sandbox, str(PR), SHA, "--dry-run")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not _state(sandbox).get("outcomes")
    assert not sandbox["api"].requests


def test_missing_binding_blocks_publication_but_keeps_private_outcome(sandbox):
    (sandbox["state"] / "bindings.json").unlink()
    proc = _success(sandbox)
    assert "PR 回写失败" in proc.stdout or "public publication blocked" in proc.stderr + proc.stdout
    _, _, outcome = _outcome(sandbox)
    assert outcome is not None, "the durable receipt must survive a blocked publication"
    assert not sandbox["api"].posts
    assert PRIVATE_CANARY in _private_text(sandbox)


def test_gitea_deployment_cannot_authorize_github_pr(sandbox):
    env = dict(sandbox["env"], DAILYCHECK_HOSTING_PROVIDER="gitea",
               GITEA_REPO="private-example/production-source",
               FEEDBACK_GITEA_REPO="private-example/production-source",
               DAILYCHECK_DEPLOY_REPORT="0")
    proc = _pipe(sandbox, str(PR), SHA, "--observe-min", "0", env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    _, _, outcome = _outcome(sandbox)
    assert outcome["deployment"]["provider"] == "gitea"
    assert outcome["deployment"]["repository"] == "private-example/production-source"

    retry = _report(sandbox, "--pr", str(PR), "--sha", SHA, "--rc", "0",
                    "--stage", "", "--log", str(sandbox["tmp"] / "pipeline.log"),
                    "--repo", str(sandbox["tmp"]))
    assert retry.returncode == 1
    assert "publication blocked" in retry.stderr
    assert not sandbox["api"].posts, "Gitea provenance must not become GitHub authority"


def test_missing_deployment_provenance_cannot_publish(sandbox):
    _success(sandbox)
    state = _state(sandbox)
    outcome_id = state["deployed"]["outcome_id"]
    del state["outcomes"][outcome_id]["deployment"]
    (sandbox["state"] / "state.json").write_text(json.dumps(state))

    retry = _report(sandbox, "--pr", str(PR), "--sha", SHA, "--rc", "0",
                    "--stage", "", "--log", str(sandbox["tmp"] / "pipeline.log"),
                    "--repo", str(sandbox["tmp"]))
    assert retry.returncode == 1
    assert "publication blocked" in retry.stderr
    assert len(sandbox["api"].posts) == 1


def test_gitea_private_issue_binding_allowed_for_github_deployment(sandbox):
    document = bindings()
    document["bindings"][0]["source"] = {
        "provider": "gitea", "host": "gitea.example", "repository_id": 12345,
        "repository": "private/source", "kind": "issue", "number": 7}
    (sandbox["state"] / "bindings.json").write_text(json.dumps(document))
    _success(sandbox)
    posts = sandbox["api"].posts
    assert len(posts) == 1
    body = posts[0]["payload"]["body"]
    assert "gitea.example" not in body and "private/source" not in body


def test_foreign_outcome_hosting_identity_cannot_publish(sandbox):
    _success(sandbox)
    state = _state(sandbox)
    outcome_id = state["deployed"]["outcome_id"]
    state["outcomes"][outcome_id]["hosting"]["repository_id"] = 1449125557
    (sandbox["state"] / "state.json").write_text(json.dumps(state))

    proc = _report(sandbox, "--pr", str(PR), "--sha", SHA, "--rc", "0",
                   "--stage", "", "--log", str(sandbox["tmp"] / "pipeline.log"),
                   "--repo", str(sandbox["tmp"]))
    assert proc.returncode == 1
    assert "publication blocked" in proc.stderr
    assert len(sandbox["api"].posts) == 1


def test_modified_cli_rc_cannot_override_durable_outcome(sandbox):
    _success(sandbox)
    before = json.dumps(_state(sandbox), sort_keys=True)
    proc = _report(sandbox, "--pr", str(PR), "--sha", SHA, "--rc", "21",
                   "--stage", "verify", "--log", str(sandbox["tmp"] / "pipeline.log"),
                   "--repo", str(sandbox["tmp"]))
    assert proc.returncode == 1
    assert "publication blocked" in proc.stderr
    assert json.dumps(_state(sandbox), sort_keys=True) == before
    assert len(sandbox["api"].posts) == 1


def test_handwritten_context_json_cannot_bootstrap_authority(sandbox):
    fake = sandbox["tmp"] / "fake-context.json"
    fake.write_text(json.dumps({
        "provider": "github", "host": "github.com", "repository_id": 1448986421,
        "repository": CODE, "pr": PR, "sha": SHA,
        "attempt_id": "approved", "generation": 1,
        "outcome_at": "2026-11-07T08:00:00Z",
        "public_receipt": str(uuid.uuid4()),
        "private_source": {"provider": "github", "host": "github.com",
                           "repository_id": 1449125557, "repository": OPS,
                           "kind": "issue", "number": 7},
        "payload_sha256": "0" * 64, "approved": True, "valid": True,
    }))
    proc = _report(sandbox, "--pr", str(PR), "--sha", SHA, "--rc", "0",
                   "--stage", "", "--log", str(sandbox["tmp"] / "pipeline.log"),
                   "--repo", str(sandbox["tmp"]), "--publication-context", str(fake))
    assert proc.returncode == 1
    assert not sandbox["api"].posts
    assert not _state(sandbox).get("outcomes")


def test_direct_cli_release_and_failed_result_mint_no_outcome(sandbox):
    env = dict(sandbox["env"])
    state_cli = [sys.executable, str(sandbox["dir"] / "deploy_state.py")]

    def run(*args):
        return subprocess.run([*state_cli, *args], capture_output=True, text=True,
                              env=env, timeout=30)

    assert run("acquire", "--pr", str(PR), "--sha", SHA).returncode == 0
    assert run("release", "--result", "success", "--sha", SHA).returncode == 0
    assert not _state(sandbox).get("outcomes")

    assert run("acquire", "--pr", str(PR), "--sha", SHA).returncode == 0
    assert run("release", "--result", "failed", "--sha", SHA).returncode == 0
    assert not _state(sandbox).get("outcomes")
    assert _state(sandbox)["last_attempt"]["result"] == "failed"


def test_identity_without_owned_process_descriptor_cannot_release_or_mint(sandbox):
    env = dict(sandbox["env"])
    state_cli = [sys.executable, str(sandbox["dir"] / "deploy_state.py")]
    acquired = subprocess.run([*state_cli, "acquire", "--pr", str(PR), "--sha", SHA],
                              capture_output=True, text=True, env=env, timeout=30)
    assert acquired.returncode == 0
    identity = json.loads(acquired.stdout)
    env["DAILYCHECK_DEPLOY_ATTEMPT"] = json.dumps({
        "token": identity["token"], "pr": PR, "sha": SHA, "session": "",
    })
    released = subprocess.run(
        [*state_cli, "release", "--token", identity["token"],
         "--result", "success", "--sha", SHA],
        capture_output=True, text=True, env=env, timeout=30)
    assert released.returncode == 11, released.stdout + released.stderr
    assert not _state(sandbox).get("outcomes")


def test_release_state_io_failure_mints_no_outcome(sandbox):
    target = sandbox["dir"] / "deploy_state.py"
    target.rename(sandbox["dir"] / "real_state.py")
    target.write_text(
        "import os, runpy, sys\n"
        "from pathlib import Path\n"
        "if len(sys.argv) > 1 and sys.argv[1] == 'release':\n"
        "    (Path(os.environ['DAILYCHECK_DEPLOY_STATE_DIR']) / "
        "f'.state.json.tmp-{os.getpid()}').mkdir()\n"
        "runpy.run_path(str(Path(__file__).with_name('real_state.py')), run_name='__main__')\n")
    proc = _pipe(sandbox, str(PR), SHA, "--observe-min", "0")
    assert proc.returncode == 27, proc.stdout + proc.stderr
    state = _state(sandbox)
    assert not state.get("outcomes")
    assert state["deployed"] is None
    assert state["lock"]["pr"] == PR
    assert not sandbox["api"].posts


# ---------------------------------------------------------------- restart/recovery


def _drop_first_response(sandbox):
    sandbox["api"].drop_comment = True
    proc = _pipe(sandbox, str(PR), SHA, "--observe-min", "0")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    sandbox["api"].drop_comment = False
    state, outcome_id, outcome = _outcome(sandbox)
    assert state["publications"][outcome_id]["state"] == "uncertain"
    assert len(sandbox["api"].posts) == 1
    return state, outcome_id, outcome


def test_lost_response_reconciles_exactly_once_without_duplicate_post(sandbox):
    _, outcome_id, outcome = _drop_first_response(sandbox)
    frozen = json.dumps(outcome["facts"], sort_keys=True)

    # A different CLI stage must not replace the durable outcome projection.
    retry = _report(sandbox, "--pr", str(PR), "--sha", SHA, "--rc", "0",
                    "--stage", "verify", "--log", str(sandbox["tmp"] / "pipeline.log"),
                    "--repo", str(sandbox["tmp"]))
    assert retry.returncode == 0, retry.stdout + retry.stderr
    state = _state(sandbox)
    publication = state["publications"][outcome_id]
    assert publication["state"] == "linked"
    assert publication["comment_id"] == 201
    assert len(sandbox["api"].posts) == 1, "recovery must not duplicate the POST"
    assert state["outcomes"][outcome_id]["public_receipt"] == outcome["public_receipt"]
    assert state["outcomes"][outcome_id]["outcome_at"] == outcome["outcome_at"]
    assert json.dumps(state["outcomes"][outcome_id]["facts"], sort_keys=True) == frozen
    assert "| 阶段 | `complete` |" in sandbox["api"].comments[0]["body"]


@pytest.mark.parametrize("change", ["zero", "multiple", "body"])
def test_uncertain_or_changed_comment_never_reposts(sandbox, change):
    _, outcome_id, _ = _drop_first_response(sandbox)
    if change == "zero":
        sandbox["api"].comments = []
    elif change == "multiple":
        sandbox["api"].comments.append({**sandbox["api"].comments[0], "id": 999})
    else:
        sandbox["api"].comments[0]["body"] += PRIVATE_CANARY

    retry = _report(sandbox, "--pr", str(PR), "--sha", SHA, "--rc", "0",
                    "--stage", "", "--log", str(sandbox["tmp"] / "pipeline.log"),
                    "--repo", str(sandbox["tmp"]))
    assert retry.returncode == 1
    assert len(sandbox["api"].posts) == 1
    assert _state(sandbox)["publications"][outcome_id]["state"] == "uncertain"


def test_changed_binding_invalidates_an_existing_publication_claim(sandbox):
    _, outcome_id, _ = _drop_first_response(sandbox)
    (sandbox["state"] / "bindings.json").write_text(json.dumps(bindings(number=8)))

    retry = _report(sandbox, "--pr", str(PR), "--sha", SHA, "--rc", "0",
                    "--stage", "", "--log", str(sandbox["tmp"] / "pipeline.log"),
                    "--repo", str(sandbox["tmp"]))
    assert retry.returncode == 1
    assert len(sandbox["api"].posts) == 1
    assert _state(sandbox)["publications"][outcome_id]["state"] == "uncertain"


def test_ambiguous_binding_blocks_publication_and_keeps_private_outcome(sandbox):
    document = bindings()
    document["bindings"].append(json.loads(json.dumps(document["bindings"][0])))
    document["bindings"][1]["source"]["number"] = 8
    (sandbox["state"] / "bindings.json").write_text(json.dumps(document))
    _success(sandbox)
    _, _, outcome = _outcome(sandbox)
    assert outcome is not None
    assert not sandbox["api"].posts
    assert PRIVATE_CANARY in _private_text(sandbox)


def test_retry_requires_real_process_exclusion(sandbox):
    _success(sandbox)
    before = json.loads((sandbox["state"] / "state.json").read_text())
    sentinel = sandbox["tmp"] / "process-lock-held"
    holder = subprocess.Popen(
        [sys.executable, str(sandbox["dir"] / "deploy_state.py"), "run", "--",
         sys.executable, "-c",
         f"import pathlib, time; pathlib.Path({str(sentinel)!r}).write_text('ready'); time.sleep(30)"],
        env=sandbox["env"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            if sentinel.exists():
                break
            time.sleep(0.05)
        assert sentinel.exists(), "holder never took the attempt lock"

        retry = _report(sandbox, "--pr", str(PR), "--sha", SHA, "--rc", "0",
                        "--stage", "", "--log", str(sandbox["tmp"] / "pipeline.log"),
                        "--repo", str(sandbox["tmp"]))
        assert retry.returncode == 1
        assert "publication blocked" in retry.stderr
        assert len(sandbox["api"].posts) == 1
        after = json.loads((sandbox["state"] / "state.json").read_text())
        assert after["generation"] == before["generation"]
        assert after["lock"] == before["lock"]
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_tampered_frozen_payload_hash_blocks_without_repost(sandbox):
    _success(sandbox)
    state = _state(sandbox)
    outcome_id = state["deployed"]["outcome_id"]
    state["outcomes"][outcome_id]["payload_sha256"] = "0" * 64
    (sandbox["state"] / "state.json").write_text(json.dumps(state))

    retry = _report(sandbox, "--pr", str(PR), "--sha", SHA, "--rc", "0",
                    "--stage", "", "--log", str(sandbox["tmp"] / "pipeline.log"),
                    "--repo", str(sandbox["tmp"]))
    assert retry.returncode == 1
    assert len(sandbox["api"].posts) == 1


def test_missing_receipt_outcome_cannot_publish(sandbox):
    _success(sandbox)
    state = _state(sandbox)
    outcome_id = state["deployed"]["outcome_id"]
    del state["outcomes"][outcome_id]["public_receipt"]
    (sandbox["state"] / "state.json").write_text(json.dumps(state))

    retry = _report(sandbox, "--pr", str(PR), "--sha", SHA, "--rc", "0",
                    "--stage", "", "--log", str(sandbox["tmp"] / "pipeline.log"),
                    "--repo", str(sandbox["tmp"]))
    assert retry.returncode == 1
    assert "publication blocked" in retry.stderr
    assert len(sandbox["api"].posts) == 1


# ---------------------------------------------------------------- failure/recovery


def test_failed_outcome_recovery_is_exact_and_does_not_duplicate(sandbox):
    (sandbox["dir"] / "up.sh").write_text("#!/bin/sh\nexit 7\n")
    sandbox["api"].drop_comment = True
    proc = _pipe(sandbox, str(PR), SHA, "--observe-min", "0")
    assert proc.returncode == 20, proc.stdout + proc.stderr
    sandbox["api"].drop_comment = False

    state = _state(sandbox)
    outcome = next(iter(state["outcomes"].values()))
    outcome_id = outcome["attempt_id"]
    assert outcome["result"] == "failed" and outcome["rc"] == 20 and outcome["stage"] == "up"
    assert state["publications"][outcome_id]["state"] == "uncertain"
    assert len(sandbox["api"].posts) == 1
    frozen = json.dumps(outcome["facts"], sort_keys=True)

    retry = _report(sandbox, "--pr", str(PR), "--sha", SHA, "--rc", "20",
                    "--stage", "up", "--log", str(sandbox["tmp"] / "pipeline.log"),
                    "--repo", str(sandbox["tmp"]))
    assert retry.returncode == 0, retry.stdout + retry.stderr
    state = _state(sandbox)
    assert state["publications"][outcome_id]["state"] == "linked"
    assert len(sandbox["api"].posts) == 1, "failure recovery must not duplicate the POST"
    assert state["outcomes"][outcome_id]["rc"] == 20
    assert state["outcomes"][outcome_id]["stage"] == "up"
    assert state["outcomes"][outcome_id]["public_receipt"] == outcome["public_receipt"]
    assert json.dumps(state["outcomes"][outcome_id]["facts"], sort_keys=True) == frozen


# ---------------------------------------------------------------- binding ordering


WRITER_CODE = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import deploy_state
directory = Path(sys.argv[2])
with deploy_state.publication_config_transaction(directory):
    path = directory / "bindings.json"
    value = json.loads(path.read_text())
    value["version"] += 1
    value["bindings"][0]["source"]["number"] = 8
    path.write_text(json.dumps(value))
Path(sys.argv[3]).write_text(json.dumps({"version": value["version"]}))
"""


def test_binding_writer_waits_for_inflight_publication(sandbox):
    _prepare(sandbox)
    proc, gate, done = gated_report(sandbox)
    writer_done = sandbox["tmp"] / "writer-done.json"
    writer = subprocess.Popen(
        [sys.executable, "-c", WRITER_CODE, str(sandbox["dir"]),
         str(sandbox["state"]), str(writer_done)],
        env=sandbox["env"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        time.sleep(0.5)
        assert writer.poll() is None, "writer must wait for the in-flight publication"
        assert json.loads((sandbox["state"] / "bindings.json").read_text())["version"] == 1

        open_gate(gate)
        wait_file(done)
        assert proc.wait(timeout=15) == 0
        writer.wait(timeout=15)
        assert writer.returncode == 0, writer.stderr.read() if writer.stderr else ""
        assert json.loads(writer_done.read_text())["version"] == 2
        assert len(sandbox["api"].posts) == 1
    finally:
        if writer.poll() is None:
            writer.kill()
            writer.wait(timeout=10)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_binding_change_before_claim_blocks_stale_context(sandbox):
    _prepare(sandbox)
    sys.path.insert(0, str(BG_DIR))
    import deploy_report as report_mod
    import deploy_state as state_mod
    import publication_authority as authority_mod

    state_dir = sandbox["state"]
    attempt_id, outcome, generation = authority_mod.load_durable_outcome(
        state_dir, PR, SHA)
    facts = report_mod.PublicDeployFacts(
        PR, SHA, outcome["public_receipt"], outcome["outcome_at"], outcome["rc"],
        "complete", "success", "out-of-scope", "aligned", "none")
    frozen, digest = authority_mod.freeze_public_facts(
        state_dir, attempt_id, generation, dataclasses.asdict(facts),
        report_mod._payload_digest(report_mod.render_public(facts)))
    facts = report_mod.PublicDeployFacts(**frozen)
    binding = authority_mod.resolve_source_binding(state_dir, PR)
    context = report_mod._publication_context(outcome, binding, digest)

    with state_mod.publication_config_transaction(state_dir):
        document = json.loads((state_dir / "bindings.json").read_text())
        document["version"] += 1
        document["bindings"][0]["source"]["number"] = 8
        (state_dir / "bindings.json").write_text(json.dumps(document))

    adapter = authority_mod.DurablePublicationAuthority(
        state_dir=state_dir, outcome_id=attempt_id, generation=generation,
        context=context, payload_sha256=digest,
        binding_sha256=binding.binding_sha256)
    with pytest.raises(HostingError, match="source_binding_changed"):
        with adapter.claim(context, digest):
            pass
    assert not sandbox["api"].posts


# ---------------------------------------------------------------- transport guard


def test_publication_mutator_guard_fd_is_explicit_and_narrow(tmp_path, monkeypatch):
    observed = tmp_path / "fd-observed.jsonl"
    shim = tmp_path / "fd-curl"
    shim.write_text(
        f"""#!{sys.executable}
import json, os, sys
sys.stdin.read()
fd = int(os.environ["GUARD_FD"])
try:
    os.fstat(fd)
    opened = True
except OSError:
    opened = False
method = sys.argv[sys.argv.index("--request") + 1]
with open({str(observed)!r}, "a") as fh:
    fh.write(json.dumps({{"opened": opened, "method": method}}) + "\\n")
sys.stdout.write("{{}}\\n" + ("201" if method == "POST" else "200"))
""")
    shim.chmod(0o755)
    for key in ("GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DAILYCHECK_HOSTING_PROVIDER", "github")
    monkeypatch.setenv("DAILYCHECK_GITHUB_REPORT_TOKEN", "synthetic-report")
    monkeypatch.setenv("DAILYCHECK_GITHUB_READ_TOKEN", "synthetic-read")
    monkeypatch.setenv("DAILYCHECK_GITHUB_API", "https://api.github.com")
    monkeypatch.setenv("DAILYCHECK_GITHUB_REPO", CODE)
    monkeypatch.setenv("DAILYCHECK_CURL", str(shim))

    with (tmp_path / "guard").open("w") as guard:
        fd = guard.fileno()
        monkeypatch.setenv("GUARD_FD", str(fd))
        client = HostingClient(HostingConfig.from_environment(AccessRole.REPORT))
        client.request("GET", f"/repos/{CODE}")
        client.request("POST", f"/repos/{CODE}/issues/{PR}/comments",
                       {"body": "synthetic"}, guard_fd=fd)
        with pytest.raises(HostingError):
            client.request("GET", f"/repos/{CODE}", guard_fd=fd)

    rows = [json.loads(line) for line in observed.read_text().splitlines()]
    assert rows == [{"opened": False, "method": "GET"},
                    {"opened": True, "method": "POST"}]


# ---------------------------------------------------------------- target resolver


TARGET_KEYS = (
    "GITEA_API", "GITEA_REPO", "GITEA_TOKEN", "GITEA_TOKEN_FILE",
    "FEEDBACK_GITEA_API", "FEEDBACK_GITEA_REPO", "FEEDBACK_GITEA_TOKEN",
    "DAILYCHECK_HOSTING_PROVIDER", "DAILYCHECK_GITHUB_REPO",
)

TARGET_CASES = {
    "default": {},
    "feedback_only": {
        "FEEDBACK_GITEA_API": "http://feedback.invalid:3010/api/v1",
        "FEEDBACK_GITEA_REPO": "feedback/source"},
    "gitea_only": {
        "GITEA_API": "http://primary.invalid:3020/api/v1",
        "GITEA_REPO": "primary/source"},
    "both_same": {
        "FEEDBACK_GITEA_API": "http://same.invalid:3000/api/v1",
        "FEEDBACK_GITEA_REPO": "same/source",
        "GITEA_API": "http://same.invalid:3000/api/v1",
        "GITEA_REPO": "same/source"},
    "conflicting_values": {
        "FEEDBACK_GITEA_API": "http://feedback.invalid:3010/api/v1",
        "FEEDBACK_GITEA_REPO": "feedback/source",
        "GITEA_API": "http://primary.invalid:3020/api/v1",
        "GITEA_REPO": "primary/source"},
    "feedback_blank_primary_set": {
        "FEEDBACK_GITEA_API": "", "FEEDBACK_GITEA_REPO": "",
        "GITEA_API": "http://primary.invalid:3020/api/v1",
        "GITEA_REPO": "primary/source"},
    "feedback_whitespace": {"FEEDBACK_GITEA_REPO": " feedback/source "},
    "feedback_trailing_slash": {
        "FEEDBACK_GITEA_API": "http://feedback.invalid:3010/api/v1/",
        "FEEDBACK_GITEA_REPO": "feedback/source"},
    "github_default": {"DAILYCHECK_HOSTING_PROVIDER": "github"},
    "github_ignores_legacy_aliases": {
        "DAILYCHECK_HOSTING_PROVIDER": "github",
        "GITEA_API": "http://unused.invalid:9876/api/v1",
        "GITEA_REPO": "other/other", "FEEDBACK_GITEA_REPO": "other/other"},
}


def _target_case_env(sb, overrides: dict) -> dict:
    env = dict(sb["env"])
    for key in TARGET_KEYS:
        env.pop(key, None)
    env.update({"DAILYCHECK_HOSTING_PROVIDER": "gitea",
                "DAILYCHECK_GITHUB_REPO": CODE,
                "DAILYCHECK_DEPLOY_REPORT": "0"})
    env.update({key: str(value) for key, value in overrides.items()})
    return env


def _actual_target(monkeypatch, overrides: dict) -> dict:
    """The actual report/candidate resolver, not a reimplementation."""
    sys.path.insert(0, str(BG_DIR))
    import deploy_report as report_mod

    with monkeypatch.context() as ctx:
        for key in TARGET_KEYS:
            ctx.delenv(key, raising=False)
        ctx.setenv("DAILYCHECK_HOSTING_PROVIDER", "gitea")
        ctx.setenv("DAILYCHECK_GITHUB_REPO", CODE)
        for key, value in overrides.items():
            ctx.setenv(key, str(value))
        if report_mod.provider() is report_mod.Provider.GITHUB:
            return {"provider": "github", "host": "github.com",
                    "repository": report_mod._repo()}
        return {"provider": "gitea",
                "host": urlsplit(report_mod._api_base()).netloc,
                "repository": report_mod._repo()}


@pytest.mark.parametrize("case", list(TARGET_CASES))
def test_pipeline_captures_the_actual_target_resolver(sandbox, monkeypatch, case):
    overrides = TARGET_CASES[case]
    expected = _actual_target(monkeypatch, overrides)
    proc = _pipe(sandbox, str(PR), SHA, "--observe-min", "0",
                 env=_target_case_env(sandbox, overrides))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    _, _, outcome = _outcome(sandbox)
    assert outcome is not None, f"no outcome for {case}"
    assert outcome["deployment"] == {**expected, "pr": PR}, (
        f"captured deployment provenance must follow the actual target resolver for {case}")


# ---------------------------------------------------------------- successor/fence


def test_successor_acquire_retires_old_outcome_without_mutating_successor(sandbox):
    _success(sandbox)
    env = dict(sandbox["env"])
    state_cli = [sys.executable, str(sandbox["dir"] / "deploy_state.py")]
    successor = subprocess.run(
        [*state_cli, "acquire", "--pr", "43", "--sha", OTHER_SHA],
        capture_output=True, text=True, env=env, timeout=30)
    assert successor.returncode == 0
    before = json.loads((sandbox["state"] / "state.json").read_text())
    assert before["generation"] == 2 and before["lock"]["pr"] == 43

    retry = _report(sandbox, "--pr", str(PR), "--sha", SHA, "--rc", "0",
                    "--stage", "", "--log", str(sandbox["tmp"] / "pipeline.log"),
                    "--repo", str(sandbox["tmp"]))
    assert retry.returncode == 1
    assert len(sandbox["api"].posts) == 1
    after = json.loads((sandbox["state"] / "state.json").read_text())
    assert after["generation"] == 2
    assert after["lock"] == before["lock"]
    assert after["publications"] == before["publications"]


def test_clear_lock_then_successor_keeps_old_attempt_unable_to_publish(sandbox):
    _success(sandbox)
    env = dict(sandbox["env"])
    state_cli = [sys.executable, str(sandbox["dir"] / "deploy_state.py")]
    assert subprocess.run([*state_cli, "clear-lock", "--yes"], capture_output=True,
                          text=True, env=env, timeout=30).returncode == 0
    assert subprocess.run([*state_cli, "acquire", "--pr", "43", "--sha", OTHER_SHA],
                          capture_output=True, text=True, env=env, timeout=30).returncode == 0
    before = json.loads((sandbox["state"] / "state.json").read_text())

    retry = _report(sandbox, "--pr", str(PR), "--sha", SHA, "--rc", "0",
                    "--stage", "", "--log", str(sandbox["tmp"] / "pipeline.log"),
                    "--repo", str(sandbox["tmp"]))
    assert retry.returncode == 1
    assert len(sandbox["api"].posts) == 1
    assert json.loads((sandbox["state"] / "state.json").read_text())["lock"] == before["lock"]
