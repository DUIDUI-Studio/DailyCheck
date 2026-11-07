"""Issue163: real pipeline outcome -> durable receipt -> trusted adapter -> loopback HTTP.

Native only: the real `deploy_pipeline.sh`, `deploy_state.py`, `deploy_report.py`,
`deploy_triage_pure.py` and the publication adapter are copied into a disposable
sandbox.  `systemctl`/`nginx`/journal traffic stages are tiny owned fakes; the
GitHub API is a real loopback HTTP server reached through a curl URL shim.  All
state, logs, shadow paths and bindings stay under `tmp_path`.
"""

from __future__ import annotations

import json
import os
import shutil
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
        "DAILYCHECK_GITHUB_SHADOW_DIR": str(shadow),
        "DAILYCHECK_DEPLOY_EVENT_LOG": str(shadow / "events.jsonl"),
        "DAILYCHECK_PROD_AGENT_INBOX": str(shadow / "inbox.jsonl"),
        "DAILYCHECK_CURL": str(shim),
        "DAILYCHECK_GITHUB_READ_TOKEN": "synthetic-read-token",
        "DAILYCHECK_GITHUB_REPORT_TOKEN": "synthetic-report-token",
        "DAILYCHECK_GITEA_TOKEN_FILE": str(tmp_path / "absent-token"),
    })
    return {"dir": sb, "repo": tmp_path, "state": state_dir, "shadow": shadow,
            "env": env, "calls": calls, "tmp": tmp_path, "api": api}


def _pipe(sb, *args: str, env: dict | None = None):
    return subprocess.run(
        ["bash", str(sb["dir"] / "deploy_pipeline.sh"), *args],
        capture_output=True, text=True, env=env or sb["env"], timeout=120)


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
    assert outcome["verification"] == "short-observe"
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


# ---------------------------------------------------------------- negative


def test_failed_pipeline_mints_no_outcome_and_publishes_nothing(sandbox):
    (sandbox["dir"] / "up.sh").write_text(
        f"#!/bin/sh\necho '{PRIVATE_CANARY} failed stage'\nexit 7\n")
    proc = _pipe(sandbox, str(PR), SHA, "--observe-min", "0")
    assert proc.returncode == 20, proc.stdout + proc.stderr
    state = _state(sandbox)
    assert not state.get("outcomes")
    assert state["deployed"] is None
    assert state["last_attempt"]["result"] == "failed"
    assert not sandbox["api"].posts
    assert PRIVATE_CANARY in _private_text(sandbox)


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

    retry = _report(sandbox, "--pr", str(PR), "--sha", SHA, "--rc", "0",
                    "--stage", "", "--log", str(sandbox["tmp"] / "pipeline.log"),
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
