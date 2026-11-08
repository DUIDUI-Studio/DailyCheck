"""Portable real HTTP/crypto/process controls, plus a separately invoked kernel proof.

OwnedFixture controls are synthetic transport/parser evidence, never cross-UID
acceptance. kernel_boundary() requires a separately authorized root Linux runtime
and fails, rather than skips, when that runtime is unavailable.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from scripts.ci.merge_group import REPOSITORY, digest
from scripts.ci.trusted_producer import attest, identity, make_test_envelope
from scripts.hosting import AccessRole, HostingClient, HostingConfig, Provider
from scripts.prod_agent.native_controller import NativeController, main
from scripts.prod_agent.native_gate import GitHubProdGate
from scripts.prod_agent.native_ports import (
    AUTHORITY_FILES,
    PERMISSIONS,
    AppSignerIdentity,
    InstalledPolicy,
    OwnedFixture,
    ProtectedInitializer,
    ProtectedReview,
    ProtectedRuntime,
    SystemdObserver,
    runtime_identity,
)
from scripts.prod_agent.native_queue import NativeQueue, ProducerPolicy, deploy_state

ROOT = Path(__file__).resolve().parents[1]


def git(repo, *args, input=None):
    return subprocess.check_output(["git", "-C", str(repo), *args], input=input, text=True).strip()


def private(path, value):
    path.write_text(json.dumps(value) if not isinstance(value, str) else value)
    path.chmod(0o600)


class AppHTTP:
    def __init__(self, public_key):
        self.public_key = public_key
        self.damage = None
        self.routes = {}
        self.posts = []
        self.health_headers = []
        self.health_handler = None
        self.jwt_verified = 0
        self.arrived = threading.Event()
        self.release = threading.Event()
        self.release.set()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.handle_request()

            def do_POST(self):
                self.handle_request()

            def handle_request(self):
                if self.path == "/health":
                    auth = self.headers.get("Authorization")
                    owner.health_headers.append(auth)
                    if owner.health_handler is not None:
                        status, raw = owner.health_handler(auth)
                        self.send_response(status)
                        self.send_header("Content-Length", str(len(raw)))
                        self.end_headers()
                        self.wfile.write(raw)
                        return
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                if self.path.startswith("/app"):
                    claims = jwt.decode(token, owner.public_key, algorithms=["RS256"], issuer="801")
                    assert claims["exp"] - claims["iat"] == 600
                    owner.jwt_verified += 1
                elif self.path != "/health":
                    assert token in {"synthetic-install-token", "synthetic-read-token"}
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
                status = 200
                if self.path == "/app":
                    value = {"id": 801 if owner.damage != "app" else 999}
                elif self.path == "/app/installations/802":
                    value = {"id": 802, "app_id": 801, "repository_selection": "selected",
                             "suspended_at": None, "permissions": dict(PERMISSIONS)}
                    if owner.damage == "permissions":
                        value["permissions"]["contents"] = "write"
                elif self.path == "/app/installations/802/access_tokens":
                    assert body == {"repository_ids": [1448986421], "permissions": PERMISSIONS}
                    status = 201
                    value = {"token": "synthetic-install-token", "permissions": PERMISSIONS,
                             "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=50)).isoformat()}
                elif self.path == "/installation/repositories?per_page=100":
                    value = {"total_count": 1, "repositories": [{"id": 1448986421,
                                "full_name": REPOSITORY if owner.damage != "repo" else "foreign/repo"}]}
                elif self.path == f"/repos/{REPOSITORY}/check-runs" and self.command == "POST":
                    owner.posts.append(body)
                    owner.arrived.set()
                    assert owner.release.wait(15)
                    if owner.damage == "lost-response":
                        self.connection.close()
                        return
                    status = 201
                    value = {**body, "id": 901, "app": {"id": 801}}
                    if owner.damage == "response-head":
                        value["head_sha"] = "f" * 40
                elif self.path.endswith("/logs"):
                    value = owner.routes[self.path]
                else:
                    value = owner.routes[self.path]
                raw = value.encode() if isinstance(value, str) else json.dumps(value).encode()
                if owner.damage == "redirect" and self.path == "/app":
                    status = 302
                if owner.damage == "oversize" and self.path == "/app":
                    raw = b" " * (1024 * 1024 + 1)
                self.send_response(status)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    (tmp_path / ".native-h-fixture").write_text("synthetic-owned-fixture\n")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server = AppHTTP(key.public_key())
    try:
        fixture = OwnedFixture(tmp_path, server.endpoint)
        repo = tmp_path / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.name", "Synthetic H fixture")
        git(repo, "config", "user.email", "fixture@example.invalid")
        (repo / ".github/workflows").mkdir(parents=True)
        (repo / ".github/workflows/ci.yml").write_text("name: synthetic-read-side-fixture\n")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "synthetic P")
        base = git(repo, "rev-parse", "HEAD")
        (repo / "member").write_text("owned fixture\n")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "synthetic H")
        head = git(repo, "rev-parse", "HEAD")
        raw = (ROOT / "scripts/ci/producer_manifest.json").read_text()
        pin = git(repo, "hash-object", "-w", "--stdin", input=raw)
        producer = ProducerPolicy(15368, 51, git(repo, "rev-parse", "HEAD:.github/workflows/ci.yml"),
                                  801, 802, producer_manifest_blob_sha=pin)
        for name in ("state", "reviews", "releases"):
            (tmp_path / name).mkdir()
        (tmp_path / "state").chmod(0o700)
        private(tmp_path / "key.pem", key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode())
        private(tmp_path / "read-token", "synthetic-read-token")
        private(tmp_path / "mcp-health-token", os.urandom(24).hex())
        app_uid = 65531 if os.geteuid() == 65532 else 65532
        value = {"version": 1, "repository": REPOSITORY, "repository_id": 1448986421,
                 "repository_node_id": "R_kgDOVl3HNQ", "producer": asdict(producer),
                 "authority": str(ROOT), "python": str(Path(sys.executable)),
                 "closure": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in AUTHORITY_FILES},
                 "app_uid": app_uid, "epoch": "synthetic-epoch", "repo": str(repo),
                 "directory": str(tmp_path / "state"), "key_file": str(tmp_path / "key.pem"),
                 "read_token_file": str(tmp_path / "read-token"),
                 "mcp_health_token_file": str(tmp_path / "mcp-health-token"),
                 "runtime_file": str(tmp_path / "runtime.json"),
                 "bootstrap_file": str(tmp_path / "bootstrap.json"), "review_directory": str(tmp_path / "reviews"),
                 "reviewers": {str(os.geteuid()): "synthetic-independent"}, "writer": "synthetic-writer",
                 "release_root": str(tmp_path / "releases"), "units": ["dailycheck.service", "dailycheck-mcp.service"],
                 "runtime": runtime_identity()}
        private(tmp_path / "policy.json", value)
        policy = InstalledPolicy(tmp_path / "policy.json", fixture=fixture)
        release = tmp_path / "releases" / base
        release.mkdir()
        (release / "release.json").write_text(json.dumps({"sha": base, "tree": git(repo, "rev-parse", base + "^{tree}")}))
        # Explicit synthetic parser input, not a kernel PID/UID/observation claim.
        processes = [{"unit": u, "uid": app_uid, "cwd": str(release), "pid": i + 100,
                      "exe": "/synthetic/python", "start_ticks": "1", "cgroup": "/synthetic"}
                     for i, u in enumerate(value["units"])]

        class ParserObserver:
            def observe(self, _):
                return processes

        runtime = ProtectedRuntime(policy, observer=ParserObserver())
        runtime_record = {"version": 1, "policy": policy.fingerprint, "epoch": value["epoch"],
                          "sha": base, "tree": git(repo, "rev-parse", base + "^{tree}"),
                          "processes": processes, "observed_at": time.time(), "observation_seconds": 600,
                          "incident": False, "routes": {"app": server.endpoint + "/health",
                                                        "mcp": server.endpoint + "/health"}}
        private(tmp_path / "runtime.json", runtime_record)
        server.routes["/health"] = {"ok": True}
        state = deploy_state.load_state(tmp_path / "state")
        state["deployed"] = {"sha": "e" * 40, "pr": 158, "at": 1}
        deploy_state.save_state(tmp_path / "state", state)
        receipt = {"version": 1, "policy": policy.fingerprint, "epoch": value["epoch"],
                   "source_sha": "e" * 40, "public_sha": base, "tree_sha": runtime_record["tree"],
                   "map_sha256": digest("synthetic-reviewed-map"), "source_outcome_sha256": digest(state["deployed"]),
                   "runtime_sha256": digest(runtime_record), "observed_at": time.time()}
        private(tmp_path / "bootstrap.json", receipt)
        server.routes[f"/repos/{REPOSITORY}"] = {"id": 1448986421, "node_id": "R_kgDOVl3HNQ",
            "full_name": REPOSITORY, "private": False, "visibility": "public", "has_issues": True,
            "archived": False, "disabled": False, "url": "https://api.github.com/repos/" + REPOSITORY,
            "html_url": "https://github.com/" + REPOSITORY}
        server.routes[f"/repos/{REPOSITORY}/branches/main"] = {"commit": {"sha": base}}
        shim = tmp_path / "curl"
        shim.write_text(f'''#!{sys.executable}
import os,sys
args=sys.argv[1:]
assert args[-1].startswith('https://api.github.com/')
args[-1]={server.endpoint!r}+args[-1][len('https://api.github.com'):]
os.execv('/usr/bin/curl',['/usr/bin/curl',*args])
''')
        shim.chmod(0o700)
        client = HostingClient(HostingConfig(Provider.GITHUB, "https://api.github.com", REPOSITORY,
                                             AccessRole.READ, "synthetic-read-token", curl=str(shim)))
        import urllib.request

        original = urllib.request.build_opener

        class LocalOpener:
            def __init__(self, handlers):
                self.opener = original(*handlers)

            def open(self, request, **kwargs):
                assert request.full_url.startswith("https://api.github.com/")
                request = urllib.request.Request(server.endpoint + request.full_url[len("https://api.github.com"):],
                    data=request.data, headers=dict(request.header_items()), method=request.get_method())
                return self.opener.open(request, **kwargs)

        monkeypatch.setattr(urllib.request, "build_opener", lambda *h: LocalOpener(h))
        controller = NativeController(policy, client=client, queue_reader=object(), runtime=runtime)
        body = "## 报告 1/4\n| R1 | synthetic | ✅ |\n## Checklist\n## 生产侧事项\nCloses #1\n"
        member = {"number": 1, "node_id": "PR_1", "head_sha": head, "base_sha": base,
                  "head_ref": "feat/issue-1-fixture", "body_sha256": digest(body)}
        server.routes[f"/repos/{REPOSITORY}/pulls/1"] = {"number": 1, "node_id": "PR_1", "state": "open", "draft": False,
            "body": body, "head": {"sha": head, "ref": member["head_ref"], "repo": {"id": 1448986421}},
            "base": {"sha": base, "ref": "main", "repo": {"id": 1448986421}}}
        bindings = {"version": 2, "bindings": [{"pr": 1, "public": deploy_state.PUBLIC_CODE_HOSTING,
            "source": {"provider": "gitea", "host": "fixture.invalid", "repository_id": 77,
                       "repository": "private/source", "kind": "issue", "number": 1},
            "source_revision": {"provider": "gitea", "host": "fixture.invalid", "repository_id": 77,
                                "repository": "private/source", "sha": "e" * 40,
                                "source_public_map_sha256": digest("synthetic-map")}}]}
        private(tmp_path / "state/bindings.json", bindings)
        _serve_ci(server, producer, member, json.loads(raw), pin)
        yield {"root": tmp_path, "policy": policy, "controller": controller, "server": server,
               "member": member, "runtime": runtime, "runtime_record": runtime_record, "receipt": receipt,
               "value": value, "base": base, "head": head}
    finally:
        server.close()


def _serve_ci(server, policy, member, manifest, pin):
    """Synthetic transport input from accepted producer schema; no new pytest proof."""
    head, base = member["head_sha"], member["base_sha"]
    binding = {"version": 3, "phase": "H", "CI_REPOSITORY": REPOSITORY, "CI_RUN_ID": "41",
               "CI_RUN_ATTEMPT": "1", "CI_EVENT": "pull_request", "CI_HEAD_SHA": head,
               "CI_EVENT_SHA": head, "CI_BASE_SHA": base, "tested_sha": head, "CI_GROUP_SHA": None,
               "member_manifest_sha256": None, "workflow_path": ".github/workflows/ci.yml",
               "workflow_blob_sha": policy.workflow_blob_sha}
    session = {"schema_version": 1, "pytest_exit": 0, "selected": 2, "selected_unique": 2, "completed": 2,
               "uncompleted": [], "unexpected": [], "failed_nodeids": [], "error_nodeids": [],
               "counts": {"passed": 2, "failed": 0, "skipped": 0, "xfailed": 0, "xpassed": 0, "error": 0}}
    raw = b".. [100%]\n2 passed in 0.01s\n"
    capture = {"version": 1, "container_id": "5" * 64, "image": manifest["runtime"]["image"],
               "kind": "pytest", "context": "test", "binding_sha256": digest(binding), "native_exit": 0,
               "stdout_sha256": hashlib.sha256(raw).hexdigest(), "session_sha256": digest(session),
               "exited": True, "removed": True, "oom_killed": False}
    envelope = make_test_envelope(raw, session, capture, binding)
    producer = {"identity": identity(manifest, pin), "test_capture": capture,
                "lint_capture": {**capture, "kind": "lint", "session_sha256": None}, "gates": []}
    root = f"/repos/{REPOSITORY}"
    server.routes[root + "/actions/workflows/ci.yml"] = {"id": 51, "state": "active", "path": ".github/workflows/ci.yml"}
    server.routes[root + "/contents/.github/workflows/ci.yml?ref=" + head] = {"sha": policy.workflow_blob_sha}
    server.routes[root + "/git/blobs/" + pin] = {"sha": pin, "encoding": "base64",
        "content": base64.b64encode(json.dumps(manifest, indent=2).encode()).decode()}
    # Preserve the exact manifest blob bytes (trailing newline included).
    server.routes[root + "/git/blobs/" + pin]["content"] = base64.b64encode(
        (ROOT / "scripts/ci/producer_manifest.json").read_bytes()).decode()
    run = {"id": 41, "run_number": 1, "run_attempt": 1, "workflow_id": 51, "path": ".github/workflows/ci.yml",
           "head_sha": head, "event": "pull_request", "status": "completed", "conclusion": "success",
           "repository": {"id": 1448986421, "node_id": "R_kgDOVl3HNQ"},
           "head_repository": {"full_name": REPOSITORY}, "check_suite_id": 410,
           "pull_requests": [{"number": 1, "head": {"sha": head}, "base": {"sha": base, "ref": "main"}}]}
    server.routes[root + "/actions/workflows/51/runs?event=pull_request&head_sha=" + head + "&per_page=100&page=1"] = {"total_count": 1, "workflow_runs": [run]}
    jobs, checks = [], []
    for i, context in enumerate(("test", "agent-framework-reports"), 1):
        if i == 2:
            producer = {**producer, "gates": [{**capture, "kind": k, "context": context, "session_sha256": None}
                                            for k in ("report-generate", "report-verify")]}
        record = attest(envelope, [member], context, producer)
        server.routes[root + f"/actions/jobs/{i}/logs"] = "DAILYCHECK_NATIVE_EVIDENCE=" + base64.b64encode(json.dumps(record).encode()).decode() + "\n"
        jobs.append({"id": i, "name": context, "run_id": 41, "run_attempt": 1, "head_sha": head,
                     "status": "completed", "conclusion": "success", "check_run_url": "https://api.github.com" + root + f"/check-runs/{i}"})
        checks.append({"id": i, "name": context, "head_sha": head, "check_suite": {"id": 410},
                       "app": {"id": 15368, "slug": "github-actions"}, "status": "completed", "conclusion": "success"})
    server.routes[root + "/actions/runs/41/attempts/1/jobs?per_page=100&page=1"] = {"total_count": 2, "jobs": jobs}
    server.routes[root + f"/commits/{head}/check-runs?filter=all&per_page=100&page=1"] = {"total_count": 2, "check_runs": checks}


def test_actual_H_reader_crypto_and_http_writer(bridge):
    b = bridge
    b["controller"].initialize()
    before = copy.deepcopy(deploy_state.load_state(b["root"] / "state"))
    receipt = b["controller"].admit_head(1)
    assert receipt["claim"] == "queue_eligible" and receipt["deployment"] is False
    assert len(b["server"].posts) == 1 and b["server"].posts[0]["head_sha"] == b["head"]
    assert b["server"].jwt_verified >= 6
    assert deploy_state.load_state(b["root"] / "state") == before


@pytest.mark.parametrize("damage", ["multi-parent", "attempt", "closure", "body"])
def test_H_context_drift_never_sends_check(bridge, damage):
    b = bridge
    b["controller"].initialize()
    root = f"/repos/{REPOSITORY}"
    if damage == "multi-parent":
        repo = b["policy"].location("repo")
        git(repo, "checkout", "-qb", "second", b["base"])
        (repo / "other").write_text("fixture other member")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "second")
        git(repo, "merge", "--no-ff", "-qm", "synthetic G", b["head"])
        b["server"].routes[root + "/pulls/1"]["head"]["sha"] = git(repo, "rev-parse", "HEAD")
    elif damage == "attempt":
        path = root + "/actions/runs/41/attempts/1/jobs?per_page=100&page=1"
        b["server"].routes[path]["jobs"][0]["run_attempt"] = 2
    elif damage == "closure":
        path = root + "/git/blobs/" + b["policy"].producer.producer_manifest_blob_sha
        b["server"].routes[path]["content"] = base64.b64encode(b"{}").decode()
    else:
        b["server"].routes[root + "/pulls/1"]["body"] += "changed candidate body"
    with pytest.raises(ValueError):
        b["controller"].admit_head(1)
    assert b["server"].posts == []


@pytest.mark.parametrize("damage", ["payload", "fence"])
def test_H_writer_rejects_unbound_caller_request(bridge, damage):
    b = bridge
    b["controller"].initialize()
    gate = GitHubProdGate(b["policy"], b["member"], b["controller"].facts)
    runner = NativeQueue(b["policy"].location("repo"), b["root"] / "state", b["policy"].producer, None)
    with runner.fence() as fd, pytest.raises(ValueError):
        gate.success({"head_sha": "f" * 40, "conclusion": "success"},
                     guard_fd=fd if damage == "payload" else None)
    assert b["server"].posts == []


def test_epoch_and_symlink_records_fail_closed(bridge):
    b = bridge
    b["controller"].initialize()
    state = deploy_state.load_state(b["root"] / "state")
    state["native_bootstrap"]["epoch"] = "foreign-epoch"
    deploy_state.save_state(b["root"] / "state", state)
    with pytest.raises(ValueError, match="epoch changed"):
        b["controller"].status()
    runtime = b["root"] / "runtime.json"
    runtime.unlink()
    runtime.symlink_to(b["root"] / "bootstrap.json")
    with pytest.raises(ValueError, match="ownership/mode"):
        b["runtime"].snapshot()


@pytest.mark.parametrize("damage", ["app", "permissions", "repo", "redirect", "oversize"])
def test_actual_App_identity_failures_send_no_check(bridge, damage):
    bridge["server"].damage = damage
    with pytest.raises((ValueError, KeyError)):
        AppSignerIdentity(bridge["policy"]).read()
    assert bridge["server"].posts == []


@pytest.mark.parametrize("damage", ["response-head", "lost-response"])
def test_actual_check_response_cannot_be_relabelled_success(bridge, damage):
    bridge["controller"].initialize()
    bridge["server"].damage = damage
    with pytest.raises(ValueError, match="uncertain"):
        bridge["controller"].admit_head(1)
    assert len(bridge["server"].posts) == 1
    assert deploy_state.load_state(bridge["root"] / "state")["outcomes"] == {}


@pytest.mark.parametrize("damage", ["mapping-only", "stale", "runtime", "live-writer", "partial",
                                     "orphan-receipt", "orphan-anchor", "runtime-digest", "source-outcome"])
def test_protected_bootstrap_denies_incomplete_transition(bridge, damage):
    b = bridge
    if damage == "mapping-only":
        private(b["root"] / "bootstrap.json", {"map_sha256": digest("map")})
    elif damage == "stale":
        private(b["root"] / "bootstrap.json", {**b["receipt"], "observed_at": 1})
    elif damage == "runtime":
        private(b["root"] / "runtime.json", {**b["runtime_record"], "sha": "d" * 40})
    elif damage == "live-writer":
        state = deploy_state.load_state(b["root"] / "state")
        state["lock"] = {"active": "synthetic old writer"}
        deploy_state.save_state(b["root"] / "state", state)
    elif damage in {"orphan-receipt", "orphan-anchor"}:
        state = deploy_state.load_state(b["root"] / "state")
        state["native_bootstrap" if damage == "orphan-receipt" else "native_source_anchor"] = b["receipt"]
        deploy_state.save_state(b["root"] / "state", state)
    elif damage in {"runtime-digest", "source-outcome"}:
        key = "runtime_sha256" if damage == "runtime-digest" else "source_outcome_sha256"
        private(b["root"] / "bootstrap.json", {**b["receipt"], key: "f" * 64})
    else:
        deploy_state.native_guard(b["root"] / "state", {"version": 1, "initialization_id": "partial", "intent_id": None})
    with pytest.raises((ValueError, FileNotFoundError)):
        ProtectedInitializer(b["policy"], b["runtime"], b["controller"].client).initialize()
    state = deploy_state.load_state(b["root"] / "state")
    assert state["deployed"]["sha"] == "e" * 40 and state["outcomes"] == {}


def test_protected_bootstrap_idempotence_preserves_original_anchor(bridge):
    b = bridge
    first = b["controller"].initialize()
    state = deploy_state.load_state(b["root"] / "state")
    assert b["controller"].initialize() == first
    assert state["native_source_anchor"]["sha"] == "e" * 40
    assert state["deployed"]["sha"] == b["base"] and state["outcomes"] == {}


def test_completed_bootstrap_survives_observation_refresh(bridge):
    b = bridge
    first = b["controller"].initialize()
    state = deploy_state.load_state(b["root"] / "state")
    guard = (b["root"] / "state" / deploy_state.NATIVE_GUARD_FILENAME).read_bytes()
    refreshed = {**b["runtime_record"], "observed_at": time.time()}
    assert refreshed != b["runtime_record"]
    private(b["root"] / "runtime.json", refreshed)
    assert b["runtime"].read()["sha"] == b["base"]
    assert b["controller"].initialize() == first
    receipt = {**b["receipt"], "runtime_sha256": digest(refreshed), "observed_at": time.time()}
    private(b["root"] / "bootstrap.json", receipt)
    assert b["controller"].initialize() == first
    # Completion consumes the original protected proof once; it is not a new installation.
    (b["root"] / "bootstrap.json").unlink()
    assert b["controller"].initialize() == first
    assert deploy_state.load_state(b["root"] / "state") == state
    assert (b["root"] / "state" / deploy_state.NATIVE_GUARD_FILENAME).read_bytes() == guard
    assert state["native_bootstrap"] == b["receipt"]
    assert state["native_source_anchor"]["sha"] == "e" * 40
    assert state["outcomes"] == {} and b["server"].posts == []


@pytest.mark.parametrize("damage", ["policy", "epoch", "source", "public", "tree", "anchor",
                                     "missing-receipt", "timestamp", "guard", "active", "lock", "deployed", "main", "runtime"])
def test_completed_bootstrap_rechecks_stable_identity_and_current_state(bridge, damage):
    b = bridge
    b["controller"].initialize()
    directory = b["root"] / "state"
    state = deploy_state.load_state(directory)
    if damage in {"policy", "epoch", "source", "public", "tree"}:
        key = {"source": "source_sha", "public": "public_sha", "tree": "tree_sha"}.get(damage, damage)
        state["native_bootstrap"][key] = "f" * (40 if damage in {"source", "public", "tree"} else 64)
    elif damage == "anchor":
        state["native_source_anchor"]["pr"] = 999
    elif damage == "missing-receipt":
        state.pop("native_bootstrap")
    elif damage == "timestamp":
        state["native_bootstrap"]["observed_at"] = "corrupt"
    elif damage == "guard":
        (directory / deploy_state.NATIVE_GUARD_FILENAME).unlink()
    elif damage == "active":
        state["native_queue"]["active"] = {"phase": "grant_uncertain"}
    elif damage == "lock":
        state["lock"] = {"synthetic": "active legacy writer"}
    elif damage == "deployed":
        state["deployed"]["sha"] = "f" * 40
    elif damage == "main":
        b["server"].routes[f"/repos/{REPOSITORY}/branches/main"] = {"commit": {"sha": "f" * 40}}
    else:
        private(b["root"] / "runtime.json", {**b["runtime_record"], "observed_at": 1})
    deploy_state.save_state(directory, state)
    before = (directory / deploy_state.STATE_FILENAME).read_bytes()
    with pytest.raises(ValueError):
        ProtectedInitializer(b["policy"], b["runtime"], b["controller"].client).initialize()
    assert (directory / deploy_state.STATE_FILENAME).read_bytes() == before
    assert b["server"].posts == []


@pytest.fixture
def mcp_health(bridge, monkeypatch):
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from mcp_server import main as actual
    from mcp_server.data import unit_of_work

    b = bridge
    token = (b["root"] / "mcp-health-token").read_text()
    monkeypatch.setenv("DAILYCHECK_MCP_TOKEN", token)
    # Wrong-token fallback can only open this disposable empty DB, never business data.
    monkeypatch.setattr(unit_of_work, "MASTER_DB", b["root"] / "synthetic-auth.db")
    with TestClient(actual._build_app()) as actual_app:
        assert actual_app.get("/health").status_code == 401
    leaf = Starlette(routes=[Route("/health", lambda _: JSONResponse({"synthetic_DB_free_healthy_leaf": True}))],
                     middleware=[Middleware(actual.AuthMiddleware)])
    with TestClient(leaf) as client:
        assert client.get("/health").status_code == 401
        assert client.get("/health", headers={"Authorization": "Bearer " + token}).status_code == 200

        def health(auth):
            # OwnedFixture has one loopback port: multiplex the public app probe
            # and authenticated MCP probe here, then run the real MCP middleware.
            if auth is None:
                return 200, b'{"public_app_health":true}'
            response = client.get("/health", headers={"Authorization": auth})
            return response.status_code, response.content

        b["server"].health_handler = health
        yield b


def test_runtime_health_uses_separate_mcp_bearer_only(mcp_health):
    b = mcp_health
    b["server"].health_headers.clear()
    assert b["runtime"].snapshot()["sha"] == b["base"]
    token = (b["root"] / "mcp-health-token").read_text()
    if b["server"].health_headers != [None, "Bearer " + token]:
        pytest.fail("Expected anonymous app probe followed by MCP-only Bearer probe")
    assert b["policy"].read_token() != token


def test_mcp_health_credential_never_leaves_fixed_route(bridge):
    b = bridge
    routes = {**b["runtime_record"]["routes"], "mcp": "http://foreign.invalid/health"}
    private(b["root"] / "runtime.json", {**b["runtime_record"], "routes": routes})
    with pytest.raises(ValueError, match="invalid fixed runtime probe"):
        b["runtime"].snapshot()
    assert b["server"].health_headers == [None]


@pytest.mark.parametrize("damage", ["missing", "wrong", "symlink", "public-mode", "reused-read-token", "read-path", "key-path"])
def test_failed_mcp_health_never_sends_H_success(mcp_health, damage):
    b = mcp_health
    b["controller"].initialize()
    path = b["root"] / "mcp-health-token"
    if damage == "missing":
        path.unlink()
    elif damage == "wrong":
        private(path, os.urandom(24).hex())
    elif damage == "symlink":
        path.unlink()
        path.symlink_to(b["root"] / "read-token")
    elif damage == "public-mode":
        path.chmod(0o644)
    elif damage == "reused-read-token":
        private(path, b["policy"].read_token())
    else:
        # Explicit fixture-only config mutation; installed policy remains protected.
        b["policy"].value["mcp_health_token_file"] = str(b["root"] / ("read-token" if damage == "read-path" else "key.pem"))
    before = deploy_state.load_state(b["root"] / "state")
    with pytest.raises((ValueError, OSError)):
        b["controller"].admit_head(1)
    assert b["server"].posts == []
    assert deploy_state.load_state(b["root"] / "state") == before


def mcp_credential_owner_boundary(root):
    """Tiny separately granted Linux control: genuine credential inode ownership only."""
    if sys.platform != "linux" or os.geteuid() != 0:
        raise RuntimeError("BLOCKED: separately authorized root Linux fixture required")
    patch = pytest.MonkeyPatch()
    generator = bridge.__wrapped__(root, patch)
    try:
        b = next(generator)
        path = b["policy"].location("mcp_health_token_file")
        assert path.stat().st_uid == 0 and b["policy"].mcp_health_token()
        assert b["runtime"].snapshot()["sha"] == b["base"]
        os.chown(path, 65532, 65532)
        assert path.stat().st_uid == 65532
        with pytest.raises(ValueError, match="ownership/mode"):
            b["policy"].mcp_health_token()
        with pytest.raises(ValueError, match="ownership/mode"):
            b["controller"].admit_head(1)
        assert b["server"].posts == []
        os.chown(path, 0, 0)
        assert b["policy"].mcp_health_token()
        return {"trusted_uid": 0, "foreign_credential_uid": 65532, "foreign_owner_denied": True,
                "no_H_success": True, "restored_owner_positive": True}
    finally:
        generator.close()
        patch.undo()


def test_review_origin_and_policy_closure_denial(bridge):
    b = bridge
    candidate = {"synthetic": "candidate with exact private bindings/CI identity"}
    review = ProtectedReview(b["policy"], fixture_ci=lambda _: "synthetic-CI-parser-input")
    with pytest.raises(ValueError, match="unavailable"):
        review.read(candidate)
    directory = b["root"] / "reviews" / str(os.geteuid())
    directory.mkdir()
    record = {"version": 1, "candidate_sha256": digest(candidate), "policy": b["policy"].fingerprint,
              "epoch": "synthetic-epoch", "reviewer": "synthetic-independent", "writer": "synthetic-writer",
              "role": "independent_prod", "verdict": "pass", "ci_sha256": digest("synthetic-CI-parser-input")}
    path = directory / (digest(candidate) + ".json")
    private(path, record)
    assert review.read(candidate)["reviewer"] == "synthetic-independent"  # Portable parser only.
    private(path, {**record, "reviewer": "synthetic-writer"})
    with pytest.raises(ValueError, match="mismatch"):
        review.read(candidate)
    private(b["root"] / "policy.json", {**b["value"], "closure": {**b["value"]["closure"],
            "scripts/prod_agent/native_gate.py": "0" * 64}})
    with pytest.raises(ValueError, match="closure changed"):
        InstalledPolicy(b["root"] / "policy.json", fixture=b["policy"].fixture)


@pytest.mark.parametrize("operation", ["grant", "release"])
def test_G_cli_hard_denies_before_any_policy_or_network(operation):
    assert main([operation]) == 2


def test_installed_policy_has_no_portable_UID_bypass(bridge):
    b = bridge
    assert b["policy"].value["app_uid"] > 0
    assert b["policy"].value["app_uid"] != os.geteuid()
    value = {**b["value"], "app_uid": os.geteuid()}
    private(b["policy"].path, value)
    with pytest.raises(ValueError, match="invalid installed H policy"):
        InstalledPolicy(b["policy"].path, fixture=b["policy"].fixture)
    with pytest.raises(ValueError, match="root isolated"):
        InstalledPolicy(bridge["root"] / "policy.json")
    if sys.platform != "linux":
        with pytest.raises(ValueError, match="Linux runtime boundary unavailable"):
            SystemdObserver().observe(bridge["policy"])


def signer_reporter(b, payload):
    script = '''import json,sys
from pathlib import Path
from scripts.prod_agent.native_ports import InstalledPolicy,OwnedFixture
from scripts.prod_agent.native_gate import GitHubProdGate
from scripts.prod_agent.native_queue import NativeQueue
p=InstalledPolicy(Path(sys.argv[1]),fixture=OwnedFixture(Path(sys.argv[2]),sys.argv[3]))
runner=NativeQueue(p.location('repo'),p.location('directory'),p.producer,None)
with runner.fence() as fd:
 GitHubProdGate(p,{},None)._send(json.loads(sys.argv[4]),fd)
'''
    return subprocess.Popen([sys.executable, "-c", script, str(b["policy"].path), str(b["root"]),
        b["server"].endpoint, json.dumps(payload)], cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def wait_for_signer_exit(pid, timeout=10):
    """Reap this exact orphan when PID1 adopts it; otherwise prove disappearance."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            waited, status = os.waitpid(pid, os.WNOHANG)
            if waited == pid:
                return {"proof": "waitpid", "native_exit": os.waitstatus_to_exitcode(status)}
        except ChildProcessError:
            pass
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return {"proof": "ProcessLookupError", "native_exit": None}
        assert time.monotonic() < deadline, "actual signer child exit not proven"
        time.sleep(0.05)


def test_delayed_real_signer_child_retains_fence_after_parent_kill(bridge):
    b = bridge
    b["controller"].initialize()
    server = b["server"]
    server.release.clear()
    # Private child entry drives the actual signer/HTTP implementation, not a mock POST.
    payload = {"name": "dailycheck/prod-gate", "head_sha": b["head"], "status": "completed",
               "conclusion": "success", "external_id": "H:" + "a" * 64}
    proc = signer_reporter(b, payload)
    child_pid = None
    try:
        assert server.arrived.wait(15)
        child_pid = int(subprocess.check_output(["pgrep", "-P", str(proc.pid)], text=True).strip())
        proc.kill()
        proc.wait(10)
        assert proc.returncode == -signal.SIGKILL
        os.kill(child_pid, 0)
        runner = NativeQueue(b["policy"].location("repo"), b["root"] / "state", b["policy"].producer, None)
        with pytest.raises(BlockingIOError), runner.fence():
            pass
    finally:
        server.release.set()
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=10)
        if child_pid is not None:
            wait_for_signer_exit(child_pid)
    assert len(server.posts) == 1


def kernel_boundary(root):
    """Invoke only with this Dispatch's READY root Linux grant; no skip fallback."""
    if sys.platform != "linux" or os.geteuid() != 0:
        raise RuntimeError("BLOCKED: READY root Linux cross-UID runtime required")
    import shutil

    root = Path(root)
    root.mkdir(mode=0o755)
    fixture_root = root / "fixture"
    fixture_root.mkdir(mode=0o755)
    patch = pytest.MonkeyPatch()
    generator = bridge.__wrapped__(fixture_root, patch)
    reporter = None
    signer_pid = None
    b = None
    try:
        b = next(generator)
        # A distinct real publisher UID owns this record. Its semantic CI input
        # remains explicitly synthetic; G release is never exercised here.
        value = dict(b["value"])
        value["reviewers"] = {"65531": "synthetic-independent"}
        private(b["policy"].path, value)
        policy = InstalledPolicy(b["policy"].path, fixture=b["policy"].fixture)
        b["policy"] = policy
        review_dir = fixture_root / "reviews/65531"
        review_dir.mkdir(mode=0o700)
        os.chown(review_dir, 65531, 65531)
        candidate = {"synthetic": "OS publisher origin control"}
        review = review_dir / (digest(candidate) + ".json")
        record = {"version": 1, "candidate_sha256": digest(candidate), "policy": policy.fingerprint,
                  "epoch": value["epoch"], "reviewer": "synthetic-independent", "writer": value["writer"],
                  "role": "independent_prod", "verdict": "pass", "ci_sha256": digest("synthetic-CI")}

        def drop_reviewer():
            os.setgroups([])
            os.setgid(65531)
            os.setuid(65531)

        published = subprocess.run([sys.executable, "-I", "-c",
            "import os,sys;f=os.open(sys.argv[1],os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.write(f,sys.argv[2].encode());os.close(f)",
            str(review), json.dumps(record)], env={"PATH": "/usr/bin:/bin"}, preexec_fn=drop_reviewer,
            capture_output=True, text=True, timeout=10, check=False)
        assert published.returncode == 0, published.stderr
        reader = ProtectedReview(policy, fixture_ci=lambda _: "synthetic-CI")
        assert reader.read(candidate)["reviewer"] == "synthetic-independent"
        os.chown(review, 65532, 65532)
        with pytest.raises(ValueError, match="ownership/mode|publisher ownership"):
            reader.read(candidate)
        os.chown(review, 65531, 65531)
        paths = [b["policy"].location(k) for k in ("key_file", "read_token_file", "runtime_file", "bootstrap_file")]
        paths += [b["policy"].path, fixture_root / "state/state.json", review]
        write_only = [ROOT / p for p in AUTHORITY_FILES] + [Path(sys.executable).resolve()]
        expected = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths + write_only}
        work = root / "candidate"
        work.mkdir()
        os.chown(work, 65532, 65532)
        b["server"].release.clear()
        payload = {"name": "dailycheck/prod-gate", "head_sha": b["head"], "status": "completed",
                   "conclusion": "success", "external_id": "H:" + "a" * 64}
        reporter = signer_reporter(b, payload)
        assert b["server"].arrived.wait(15), "actual signer HTTP did not arrive"
        children = Path(f"/proc/{reporter.pid}/task/{reporter.pid}/children").read_text().split()
        assert len(children) == 1
        signer_pid = int(children[0])
        code = r'''import ctypes,json,os,sys
assert os.getuid()==65532 and os.geteuid()==65532
libc=ctypes.CDLL(None,use_errno=True)
assert libc.prctl(38,1,0,0,0)==0
status=open('/proc/self/status').read()
assert 'CapEff:\t0000000000000000' in status and 'NoNewPrivs:\t1' in status
paths=json.loads(sys.argv[1]);write_only=json.loads(sys.argv[2]);denied={}
for path in paths+write_only:
 for mode in ((os.O_RDONLY,os.O_WRONLY) if path in paths else (os.O_WRONLY,)):
  try:
   fd=os.open(path,mode);os.close(fd);denied[path+':'+str(mode)]=False
  except PermissionError: denied[path+':'+str(mode)]=True
try: os.kill(int(sys.argv[3]),0);denied['signal-real-signer']=False
except PermissionError: denied['signal-real-signer']=True
class Vec(ctypes.Structure): _fields_=[('base',ctypes.c_void_p),('size',ctypes.c_size_t)]
buf=ctypes.create_string_buffer(24);local=Vec(ctypes.addressof(buf),24);remote=Vec(1,24)
rc=libc.process_vm_readv(int(sys.argv[3]),ctypes.byref(local),1,ctypes.byref(remote),1,0)
denied['ptrace-real-signer-memory']=rc==-1 and ctypes.get_errno()==1
denied['no-inherited-authority-FD']=not os.path.exists('/proc/self/fd/3')
sys.path.insert(0,sys.argv[5])
from scripts.prod_agent.native_queue import deploy_state
fake=os.open('/dev/null',os.O_RDONLY)
os.environ[deploy_state.ATTEMPT_FD_ENV]=str(fake)
os.environ['DAILYCHECK_DEPLOY_STATE_DIR']=sys.argv[6]
denied['forged-fence-rejected']=deploy_state.process_lock_fd() is None
with open(sys.argv[4]+'/allowed','w') as f:f.write('candidate actually ran')
print(json.dumps(denied));assert all(denied.values())
'''

        def drop_uid():
            os.setgroups([])
            os.setgid(65532)
            os.setuid(65532)

        result = subprocess.run([sys.executable, "-I", "-c", code, json.dumps([str(p) for p in paths]),
            json.dumps([str(p) for p in write_only]), str(signer_pid), str(work), str(ROOT),
            str(fixture_root / "state")], env={"PATH": "/usr/bin:/bin"}, preexec_fn=drop_uid,
            capture_output=True, text=True, timeout=20, check=False)
        if result.returncode != 0:
            raise RuntimeError("kernel boundary failed: " + result.stdout + result.stderr)
        assert (work / "allowed").read_text() == "candidate actually ran"
        for path, wanted in expected.items():
            assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == wanted
        reporter.kill()
        reporter.wait(10)
        assert reporter.returncode == -signal.SIGKILL
        os.kill(signer_pid, 0)
        runner = NativeQueue(b["policy"].location("repo"), fixture_root / "state", b["policy"].producer, None)
        with pytest.raises(BlockingIOError), runner.fence():
            pass
        outcome = {"native_exit": result.returncode, "denials": json.loads(result.stdout),
                   "candidate_uid": 65532, "actual_signer_uid": 0, "positive_candidate_control": True,
                   "review_publisher_uid": 65531, "wrong_UID_review_rejected": True,
                   "executed_helper_and_private_record_hashes_unchanged": True,
                   "actual_signer_HTTP": True, "reporter_native_exit": reporter.returncode}
    finally:
        if b is not None:
            b["server"].release.set()
        if reporter is not None:
            if reporter.poll() is None:
                reporter.kill()
            reporter.communicate(timeout=10)
        if signer_pid is not None:
            signer_exit = wait_for_signer_exit(signer_pid)
        generator.close()
        patch.undo()
        shutil.rmtree(root)
    outcome["signer_exit"] = signer_exit
    outcome["actual_signer_exited"] = True
    outcome["scratch_removed"] = not root.exists()
    return outcome
