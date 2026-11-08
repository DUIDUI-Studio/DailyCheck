"""Focused source seams only; no deployment or genuine 600-second acceptance.

Portable children run as the caller. setpriv argv assertions do not prove a
Linux UID boundary. Synthetic durable records test the G writer parser/HTTP
context, never claim a newly observed runtime or a production grant.
"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import io
import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.ci.merge_group import REPOSITORY, digest
from scripts.prod_agent.native_gate import GitHubGProdGate
from scripts.prod_agent.native_pipeline import (
    HELPERS,
    TOOL_NAMES,
    ProtectedPipeline,
    database_snapshot,
)
from scripts.prod_agent.native_queue import NativeQueue, candidate, deploy_state
from tests import test_native_live_bridge as bridge_tests
from tests import test_native_queue_contract as queue_tests

bridge = bridge_tests.bridge
native_base = queue_tests.native_base
git = bridge_tests.git
fresh = queue_tests.fresh


def test_missing_executor_rejects_before_new_admission(bridge):
    b = bridge
    b["controller"].initialize()
    before = copy.deepcopy(deploy_state.load_state(b["root"] / "state"))
    with pytest.raises(ValueError, match="executor policy required"):
        b["controller"].deploy_front()
    assert deploy_state.load_state(b["root"] / "state") == before
    assert b["server"].posts == []
    assert git(b["policy"].location("repo"), "rev-parse", "HEAD") == b["head"]


def test_single_queue_dispatch_preserves_fence_and_attempt(native_base):
    b = native_base
    runner = fresh(b, "protected-dispatch")
    calls = []

    class Pipeline:
        def execute(self, value, *, guard_fd, attempt):
            calls.append((value, guard_fd, attempt))
            assert os.fstat(guard_fd)
            assert attempt == json.loads(os.environ[deploy_state.ATTEMPT_ENV])

    runner.pipeline = Pipeline()
    with runner.fence() as fd:
        runner.start(b["values"][0])
        assert len(calls) == 1 and calls[0][:2] == (b["values"][0], fd)
    state = deploy_state.load_state(runner.directory)
    assert state["native_queue"]["active"]["journal"] == []
    assert state["deployed"]["sha"] == b["base"] and state["outcomes"] == {}


def test_nonfixture_facts_have_no_candidate_shell_fallback(native_base):
    b = native_base
    runner = fresh(b, "missing-protected-port")
    facts = runner.facts
    runner.facts = SimpleNamespace(read=facts.read, read_head=facts.read_head)
    with runner.fence():
        with pytest.raises(RuntimeError, match="no checkout-shell fallback"):
            runner.start(b["values"][0])
    assert b["host"].posts == []
    assert deploy_state.load_state(runner.directory)["native_queue"]["active"]["journal"] == []


def test_changed_fixed_helper_rejects_before_tools_or_candidate_work(bridge):
    b = bridge
    p = b["policy"]
    keys = ("app_python", "source_db", "green_db", "scratch_root", "log_directory", "backup_directory",
            "unit_directory", "nginx_conf", "app_environment_file", "blue_access_log", "green_access_log")
    p.value["pipeline"] = {**dict.fromkeys(keys, str(b["root"])), "version": 1, "app_gid": 65532,
        "app_runtime": {}, "tools": dict.fromkeys(TOOL_NAMES, "0" * 64),
        "helpers": {name: hashlib.sha256((p.authority / name).read_bytes()).hexdigest() for name in HELPERS}}
    p.value["pipeline"]["helpers"][HELPERS[0]] = "f" * 64
    with pytest.raises(ValueError, match="helper changed"):
        ProtectedPipeline(p)
    assert b["server"].posts == []


def test_release_uses_literal_git_blobs_and_keeps_control_outside_candidate(native_base, tmp_path):
    b = native_base
    runner = fresh(b, "literal-release")
    release_root = tmp_path / "releases"
    release_root.mkdir()
    port = object.__new__(ProtectedPipeline)
    locations = {"repo": b["repo"], "directory": runner.directory, "release_root": release_root}
    port.policy = SimpleNamespace(location=locations.__getitem__)

    class ExportOnly:
        def execute(self, value, **kwargs):
            port._export_release(value)

    runner.pipeline = ExportOnly()
    with runner.fence():
        runner.start(b["values"][0])
    manifest = b["values"][0]["manifest"]
    release = release_root / manifest["group_sha"]
    assert json.loads((release / "release.json").read_text()) == {
        "sha": manifest["group_sha"], "tree": manifest["tree_sha"]}
    assert not (release / ".git").exists()
    assert (release.stat().st_mode & 0o222) == 0
    for row in git(b["repo"], "ls-tree", "-r", manifest["group_sha"]).splitlines():
        meta, name = row.split("\t", 1)
        oid = meta.split()[2]
        assert (release / name).read_bytes() == subprocess.check_output(["git", "-C", str(b["repo"]), "cat-file", "blob", oid])
    assert git(b["repo"], "rev-parse", "HEAD") == manifest["group_sha"]
    assert deploy_state.load_state(runner.directory)["native_queue"]["active"]["journal"] == []


def test_fixed_helpers_stop_on_actual_native_exit_without_observation(native_base, tmp_path, monkeypatch):
    b = native_base
    runner = fresh(b, "protected-stages")
    authority = tmp_path / "authority"
    helpers = authority / "scripts/blue_green"
    helpers.mkdir(parents=True)
    calls = tmp_path / "calls"
    for name in ("up", "verify", "promote", "switch"):
        (helpers / (name + ".sh")).write_text(
            f"printf '%s\\n' '{name}' >> '{calls}'\nexit {37 if name == 'switch' else 0}\n")
    port = object.__new__(ProtectedPipeline)
    port.policy = SimpleNamespace(authority=authority)
    monkeypatch.setattr(port, "_validate_installation", lambda: None)
    monkeypatch.setattr(port, "_owned", lambda value=None: b["values"][0])
    monkeypatch.setattr(port, "_control_env", lambda value: {"PATH": "/usr/bin:/bin"})
    monkeypatch.setattr(port, "_export_release", lambda value: None)
    monkeypatch.setattr(port, "_scratch", lambda: tmp_path / "candidate-scratch")
    (tmp_path / "candidate-scratch").mkdir()
    monkeypatch.setattr(port, "_lower", lambda *a, **kw: subprocess.CompletedProcess([], 0, ""))
    runner.pipeline = port
    with runner.fence():
        with pytest.raises(subprocess.CalledProcessError) as failure:
            runner.start(b["values"][0])
        assert failure.value.returncode == 37
    assert calls.read_text().splitlines() == ["up", "verify", "promote", "switch"]
    state = deploy_state.load_state(runner.directory)
    journal = state["native_queue"]["active"]["journal"]
    assert [r["stage"] for r in journal] == ["candidate", "up", "verify", "promote", "switch"]
    assert [r["completed"] for r in journal] == [True, True, True, True, False]
    assert state["deployed"]["sha"] == b["base"] and b["host"].posts == []


def test_lower_launch_sanitizes_env_and_descriptors_with_real_owned_child(tmp_path, monkeypatch):
    """Real local child; setpriv is intercepted because this is portable evidence."""
    port = object.__new__(ProtectedPipeline)
    port.policy = SimpleNamespace(value={"app_uid": 65532})
    port.value = {"app_gid": 65532}
    port.python = Path(sys.executable)
    monkeypatch.setattr(port, "_owned", lambda: {})
    monkeypatch.setattr(port, "_release", lambda value: tmp_path)
    monkeypatch.setenv("SIGNING_TOKEN", "synthetic-never-forward")
    monkeypatch.setenv(deploy_state.ATTEMPT_ENV, "synthetic-attempt")
    fd = os.open(tmp_path / "fence", os.O_CREAT | os.O_RDWR, 0o600)
    os.set_inheritable(fd, True)
    monkeypatch.setenv(deploy_state.ATTEMPT_FD_ENV, str(fd))
    original = subprocess.Popen
    captured = {}

    def portable(command, **kwargs):
        captured.update(command=command, options=kwargs)
        # Keep actual cwd/env/close_fds/stdio behavior, only remove unavailable
        # Linux identity setup. No production constructor permits this override.
        return original(command[command.index(str(sys.executable)):], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", portable)
    code = ("import json,os; "
            "print(json.dumps({'uid':os.geteuid(),'env':dict(os.environ),"
            f"'fence_open':os.path.exists('/dev/fd/{fd}')}}))")
    try:
        result = port._lower(["-c", code], db=tmp_path / "db", scratch=tmp_path)
    finally:
        os.close(fd)
    assert result.returncode == 0
    value = json.loads(result.stdout)
    assert value["uid"] == os.geteuid() and value["fence_open"] is False
    assert not {"SIGNING_TOKEN", deploy_state.ATTEMPT_ENV, deploy_state.ATTEMPT_FD_ENV} & value["env"].keys()
    command = captured["command"]
    assert command[:8] == ["/usr/bin/setpriv", "--reuid=65532", "--regid=65532", "--clear-groups",
                           "--bounding-set=-all", "--inh-caps=-all", "--ambient-caps=-all", "--no-new-privs"]
    assert captured["options"]["close_fds"] is True and "pass_fds" not in captured["options"]


def test_generated_service_recipe_drops_identity_before_Python_and_preserves_backup(native_base, tmp_path, monkeypatch):
    """Actual serialized service artifacts; systemd/chown are not executed here."""
    b = native_base
    locations = {name: tmp_path / name for name in ("directory", "review_directory", "key_file", "read_token_file", "runtime_file", "bootstrap_file")}
    locations["repo"] = b["repo"]
    paths = {name: tmp_path / name for name in ("unit_directory", "backup_directory", "source_db", "green_db")}
    paths.update(green_access_log=tmp_path / "green-access.log", app_environment_file=tmp_path / "application.env")
    for name in ("unit_directory", "backup_directory"):
        paths[name].mkdir()
    units = ("dailycheck-green.service", "dailycheck-green-mcp.service")
    for unit in units:
        (paths["unit_directory"] / unit).write_text("original protected service\n")
    port = object.__new__(ProtectedPipeline)
    port.policy = SimpleNamespace(location=locations.__getitem__, value={"app_uid": 65532},
                                  owner=os.geteuid(), anchor=tmp_path)
    port.value = {"app_gid": 65532}
    port.python = Path("/opt/approved-application/bin/python")
    monkeypatch.setattr(port, "path", paths.__getitem__)
    monkeypatch.setattr(port, "_owned", lambda: b["values"][0])
    monkeypatch.setattr(port, "_release", lambda value: tmp_path / value["manifest"]["group_sha"])
    monkeypatch.setattr(os, "fchown", lambda *args: None)
    real_run = subprocess.run
    commands = []

    def no_systemd(command, **kwargs):
        if command[0] == "/usr/bin/systemctl":
            commands.append(command)
            return subprocess.CompletedProcess(command, 0)
        return real_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", no_systemd)
    # The preceding literal-release case may move HEAD; make the trusted object
    # cursor explicit without any candidate hook/filter execution.
    git(b["repo"], "-c", "core.hooksPath=/dev/null", "update-ref", "--no-deref", "HEAD", b["g1"])
    port.prepare_services()
    assert commands == [["/usr/bin/systemctl", "daemon-reload"]]
    for unit in units:
        content = (paths["unit_directory"] / unit).read_text()
        start = next(line for line in content.splitlines() if line.startswith("ExecStart="))
        assert start.startswith("ExecStart=/usr/bin/env -i ")
        assert start.index("/usr/bin/setpriv --reuid=65532 --regid=65532 --clear-groups") < start.index(str(port.python))
        assert "--bounding-set=-all --inh-caps=-all --ambient-caps=-all --no-new-privs" in start
        assert "EnvironmentFile=" + str(paths["app_environment_file"]) in content
        assert "NoNewPrivileges=yes" in content and "DAILYCHECK_DEPLOY_ATTEMPT" not in content
        backup = paths["backup_directory"] / (unit + ".native-" + b["g1"])
        assert backup.read_text() == "original protected service\n"


@pytest.mark.parametrize("damage", ["isolated", "noop", "leak", "error", "missing-log"])
def test_green_update_collector_uses_actual_http_and_value_content(tmp_path, damage, monkeypatch):
    green = tmp_path / "green"
    green.mkdir()
    blue = tmp_path / "blue"
    blue.mkdir()
    db = green / "master.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE users(id INTEGER PRIMARY KEY, password_hash TEXT, username TEXT, is_admin INT, is_active INT)")
        conn.execute("INSERT INTO users VALUES(1,'original-hash','fixture',1,1)")
    (blue / "master.db").write_bytes(db.read_bytes())
    requests = []
    log = io.StringIO()
    handler = logging.StreamHandler(log)
    logging.getLogger("dryrun").addHandler(handler)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'<input name="csrf_token" value="synthetic">')
            if self.path == "/select-warehouse":
                self.wfile.write(b'<form action="/select-warehouse/1"></form>')
            if requests and requests[-1][0] == "/users/1/reset-password" and damage != "noop":
                self.wfile.write("已重置 fixture 的密码".encode())

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append((self.path, body))
            if self.path == "/login":
                self.send_response(302)
                self.send_header("Location", "/users")
                self.end_headers()
                return
            if self.path == "/select-warehouse/1":
                self.send_response(302)
                self.send_header("Location", "/land")
                self.end_headers()
                return
            assert self.path == "/users/1/reset-password" and b"password=dc" in body
            if damage == "leak":
                with sqlite3.connect(db) as conn:
                    conn.execute("UPDATE users SET password_hash='leaked' WHERE id=1")
            elif damage in {"isolated", "missing-log"}:
                from db.dryrun import DryRunConnection

                with sqlite3.connect(db) as conn:
                    wrapped = DryRunConnection(conn)
                    wrapped.execute("UPDATE users SET password_hash=? WHERE id=?", ("new-hash", 1))
                    wrapped.commit()
                    assert wrapped.writes_blocked == 1 and wrapped.writes_allowed == 0
            self.send_response(500 if damage == "error" else 302)
            self.send_header("Location", "/users")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    port = object.__new__(ProtectedPipeline)
    monkeypatch.setattr(port, "path", lambda key: {"green_db": green, "source_db": blue}[key])
    monkeypatch.setenv("SIM_USER_USER", "fixture")
    monkeypatch.setenv("SIM_USER_PASSWORD", "synthetic")

    def local_child(args, **kwargs):
        code = args[1].replace("http://127.0.0.1:8091", f"http://127.0.0.1:{server.server_port}")
        return subprocess.run([sys.executable, "-I", "-B", "-c", code], capture_output=True, text=True,
                              env=kwargs["extra"])

    monkeypatch.setattr(port, "_lower", local_child)
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **kw: "" if damage == "missing-log" else log.getvalue())
    try:
        if damage == "isolated":
            assert port._dryrun(tmp_path) == 0
        else:
            with pytest.raises(ValueError, match="probe failed|not proven isolated"):
                port._dryrun(tmp_path)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        logging.getLogger("dryrun").removeHandler(handler)
    assert requests and requests[-1][0] == "/users/1/reset-password"


def test_logical_snapshot_detects_update_with_unchanged_row_count(tmp_path):
    path = tmp_path / "master.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE fixture(value TEXT)")
        conn.execute("INSERT INTO fixture VALUES('old')")
    before = database_snapshot(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE fixture SET value='new'")
        assert conn.execute("SELECT count(*) FROM fixture").fetchone() == (1,)
    assert before != database_snapshot(tmp_path)


def _synthetic_intent(b, runner):
    """Parser fixture, not a seal() call or elapsed-time/observation claim."""
    repo = b["policy"].location("repo")
    tree = git(repo, "rev-parse", b["head"] + "^{tree}")
    group = git(repo, "commit-tree", tree, "-p", b["base"], "-p", b["head"], input="synthetic G\n")
    member = {**b["member"], "queue_entry_id": "entry-1", "position_at_creation": 1, "integration_sha": group}
    manifest = {"version": 1, "repository": REPOSITORY, "repository_id": 1448986421,
                "repository_node_id": "R_kgDOVl3HNQ", "queue_id": "queue", "subject_entry_id": "entry-1",
                "subject_pr_number": 1, "creation_base_sha": b["base"], "group_sha": group,
                "tree_sha": tree, "parents": [b["base"], b["head"]], "expected_predecessor_sha": b["base"],
                "group_ref": "refs/heads/gh-readonly-queue/main/fixture", "event_delivery_id": "41", "members": [member]}
    from scripts.prod_agent.native_queue import publication_authority

    binding = publication_authority.resolve_source_binding(runner.directory, 1)
    value = candidate(repo, manifest, [{"pr": 1, "source": dict(zip(publication_authority.SOURCE_KEYS, binding.source, strict=True)),
        "source_revision": binding.source_revision, "binding_sha256": binding.binding_sha256}], b["policy"].producer)
    rc = deploy_state._acquire(argparse.Namespace(pr=1, sha=group, session="native-parser-fixture", phase="native-deploy",
        deployment_provider="github", deployment_host="github.com", deployment_repository=REPOSITORY))
    assert rc == 0
    state = deploy_state.load_state(runner.directory)
    lock = state["lock"]
    os.environ[deploy_state.ATTEMPT_ENV] = json.dumps({k: lock[k] for k in ("token", "pr", "sha", "session")})
    journal = [{"stage": name, "started_monotonic": 0, "completed_monotonic": 600,
                "started_at": 0, "completed_at": 600, "completed": True, "rc": 0}
               for name in deploy_state.NATIVE_STAGES]
    proof = {"candidate_sha256": digest(value), "verification_sha256": digest(journal), "verification_result": "normal",
             "review_role": "independent_prod", "production_sha": group, "observation_seconds": 600}
    proof["evidence_sha256"] = digest(proof)
    payload = {"name": "dailycheck/prod-gate", "head_sha": group, "status": "completed",
               "conclusion": "success", "external_id": "1" * 32}
    grant = {"intent_id": payload["external_id"], "intent_at": 1, "issuer_app_id": 801, "installation_id": 802,
             "group_sha": group, "evidence_sha256": proof["evidence_sha256"], "success_request_sha256": digest(payload),
             "validity": "irrevocable_immutable_G", "last_http_result": "unknown", "check_run_id": None}
    state["native_queue"]["active"] = {"candidate": value, "origin_attempt_id": lock["attempt_id"],
        "origin_generation": lock["generation"], "phase": "grant_intent", "journal": journal,
        "verified_evidence": proof, "release_grant": grant}
    deploy_state.native_guard(runner.directory, {"version": 1, "initialization_id": state["native_queue"]["initialization_id"],
                                              "intent_id": payload["external_id"]})
    deploy_state.save_state(runner.directory, state)
    return value, payload


def test_G_dedicated_writer_actual_crypto_http_and_durable_binding(bridge):
    b = bridge
    b["controller"].initialize()
    runner = NativeQueue(b["policy"].location("repo"), b["policy"].location("directory"), b["policy"].producer, None)
    with runner.fence() as fd:
        value, payload = _synthetic_intent(b, runner)
        response = GitHubGProdGate(b["policy"], value).success(payload, guard_fd=fd)
        assert response["head_sha"] == value["manifest"]["group_sha"] and response["app_id"] == 801
    assert b["server"].posts == [payload]
    # Direct writer transport cannot anchor or relabel the synthetic proof.
    state = deploy_state.load_state(runner.directory)
    assert state["native_queue"]["active"]["phase"] == "grant_intent"
    assert state["deployed"]["sha"] == b["base"] and state["outcomes"] == {}


@pytest.mark.parametrize("damage", ["head", "request", "proof", "policy", "phase", "app"])
def test_G_writer_context_drift_never_posts(bridge, damage):
    b = bridge
    b["controller"].initialize()
    runner = NativeQueue(b["policy"].location("repo"), b["policy"].location("directory"), b["policy"].producer, None)
    with runner.fence() as fd:
        value, payload = _synthetic_intent(b, runner)
        state = deploy_state.load_state(runner.directory)
        active = state["native_queue"]["active"]
        if damage == "head":
            payload["head_sha"] = b["head"]
        elif damage == "request":
            active["release_grant"]["success_request_sha256"] = "f" * 64
        elif damage == "proof":
            active["verified_evidence"]["verification_sha256"] = "f" * 64
        elif damage == "policy":
            state["native_bootstrap"]["policy"] = "f" * 64
        elif damage == "phase":
            active["phase"] = "grant_uncertain"
        else:
            active["release_grant"]["issuer_app_id"] = 999
        deploy_state.save_state(runner.directory, state)
        with pytest.raises(ValueError):
            GitHubGProdGate(b["policy"], value).success(payload, guard_fd=fd)
    assert b["server"].posts == []


@pytest.mark.parametrize("damage", ["response-head", "lost-response"])
def test_G_actual_HTTP_response_failure_is_uncertain_without_repost(bridge, damage):
    b = bridge
    b["controller"].initialize()
    runner = NativeQueue(b["policy"].location("repo"), b["policy"].location("directory"), b["policy"].producer, None)
    with runner.fence() as fd:
        value, payload = _synthetic_intent(b, runner)
        b["server"].damage = damage
        with pytest.raises(ValueError, match="uncertain"):
            GitHubGProdGate(b["policy"], value).success(payload, guard_fd=fd)
    assert b["server"].posts == [payload]
    assert deploy_state.load_state(runner.directory)["deployed"]["sha"] == b["base"]


def test_controller_active_intent_reconciles_without_executor_or_repost(bridge, monkeypatch):
    b = bridge
    b["controller"].initialize()
    runner = NativeQueue(b["policy"].location("repo"), b["policy"].location("directory"), b["policy"].producer, None)
    with runner.fence():
        _synthetic_intent(b, runner)
    calls = []
    monkeypatch.setattr(NativeQueue, "reconcile", lambda self: calls.append("read-only") or "reconcile_required")
    assert b["controller"].deploy_front()["result"] == "reconcile_required"
    assert calls == ["read-only"] and b["server"].posts == []


def test_controller_recovers_original_manifest_from_authenticated_run(bridge, monkeypatch):
    b = bridge
    b["controller"].initialize()
    runner = NativeQueue(b["policy"].location("repo"), b["policy"].location("directory"), b["policy"].producer, None)
    with runner.fence():
        value, _ = _synthetic_intent(b, runner)
    manifest = value["manifest"]
    reports = {"name": "agent-framework-reports", "id": 55, "envelope": {"binding": {
        "CI_BASE_SHA": manifest["creation_base_sha"], "member_manifest_sha256": digest(manifest)}}}
    facts = b["controller"].facts
    monkeypatch.setattr(facts, "_run", lambda *args: {"jobs": [reports], "run": {"id": 41, "attempt": 1}})
    monkeypatch.setattr(facts, "job_log", lambda _: "UPSTREAM_MEMBER_MANIFEST_B64: " + base64.b64encode(json.dumps(manifest).encode()).decode())
    b["server"].routes[f"/repos/{REPOSITORY}/actions/runs/41"] = {
        "head_sha": manifest["group_sha"], "head_branch": manifest["group_ref"].removeprefix("refs/heads/"), "run_attempt": 1}
    calls = []
    b["controller"].queue_reader = SimpleNamespace(revalidate=lambda *a: calls.append(a[1]))
    assert b["controller"]._candidate(manifest["group_sha"]) == value
    assert calls == [manifest]
    reports["envelope"]["binding"]["member_manifest_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="digest mismatch"):
        b["controller"]._candidate(manifest["group_sha"])
