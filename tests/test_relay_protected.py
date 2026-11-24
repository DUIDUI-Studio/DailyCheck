"""The relay's Linux stage port invokes the real switch helper and native lock."""
from __future__ import annotations

import fcntl
import hashlib
import http.server
import os
import sqlite3
import subprocess
import sys
import threading
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from urllib.request import urlopen

import pytest

import routing_writer_fence as writer
from scripts.blue_green import deploy_state as ds
from scripts.prod_agent import relay_git
from scripts.prod_agent.native_pipeline import ProtectedPipeline
from scripts.prod_agent.relay_pipeline import RelayPipeline, _route, database_identity
from scripts.prod_agent.relay_protocol import RoutingSwitchBlocked, lock_file
from scripts.prod_agent.relay_vps import Server, StageFailure, observation
from tests.blue_green_testenv import install_fake_systemctl

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("pipeline_type", [ProtectedPipeline, RelayPipeline])
def test_sim_user_uses_single_preview_transport_after_environment_reset(tmp_path, monkeypatch, pipeline_type):
    pipeline = object.__new__(pipeline_type)
    pipeline.policy = SimpleNamespace(authority=tmp_path)
    monkeypatch.delenv("DC_RELAY_MODE", raising=False)
    monkeypatch.setattr(pipeline, "path", lambda key: tmp_path / key)
    calls = []

    def lower(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(pipeline, "_lower", lower)
    assert pipeline._sim_user(tmp_path, "fixture", "synthetic") == 0
    assert calls[1] == [str(tmp_path / "scripts/playwright/sim_user.py"),
                        "--base-url", "http://localhost:8091"]


def test_incremental_bundle_does_not_pack_base_delta_objects(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    relay_git.git(repo, "init", "--template=")
    payload = hashlib.shake_256(b"baseline payload").digest(128 * 1024)
    path = repo / "payload.bin"

    def commit(data, message):
        path.write_bytes(data)
        relay_git.git(repo, "add", path.name)
        relay_git.git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                      "commit", "-qm", message)
        return relay_git.git(repo, "rev-parse", "HEAD").strip().decode()

    base = commit(payload, "base")
    head = commit(payload[:-5] + b"newer", "candidate")
    expected = {"base": base, "head": head, "ref": "refs/heads/candidate",
                "tree": relay_git.git(repo, "rev-parse", "HEAD^{tree}").strip().decode()}
    seed = relay_git.make_store(tmp_path / "seed.git")
    relay_git.git(repo, "pack-objects", "--revs", str(seed / "objects/pack/pack"),
                  input=(base + "\n").encode())
    receiver = relay_git.make_store(tmp_path / "receiver.git", seed)
    data = relay_git.make_bundle(repo, tmp_path / "outgoing.bundle", expected)

    relay_git.import_bundle(receiver, tmp_path / "incoming.bundle", data, expected,
                            hashlib.sha256(data).hexdigest())


@pytest.mark.skipif(sys.platform != "linux", reason="real Linux error and shell semantics")
def test_protected_relay_runs_real_switch_with_native_identity(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture",
                    "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty",
                    "-qm", "base"], check=True)
    base = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    (repo / "migrations").mkdir()
    (repo / "migrations/routing_stockout.py").touch()
    subprocess.run(["git", "-C", str(repo), "add", "migrations"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture",
                    "-c", "user.email=fixture@example.invalid", "commit", "-qm", "candidate"], check=True)
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                   text=True).strip()
    tree = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"],
                                   text=True).strip()
    vps = object.__new__(Server)
    vps.directory = tmp_path / "state"
    vps.directory.mkdir(mode=0o700)
    vps.work = tmp_path / "stages"
    vps.work.mkdir()
    vps.fixture = None
    vps.expected = {"attempt": "9" * 32, "pr": 123, "pr_id": 456, "pr_node": "synthetic123",
                    "head": head, "base": base, "tree": tree, "generation": 4}
    monkeypatch.setenv("DAILYCHECK_DEPLOY_STATE_DIR", str(vps.directory))

    state = ds._empty_state()
    deployed = {"sha": "b" * 40, "generation": 3}
    standby = {"sha": "base", "route": "blue", "identity": "a" * 64}
    observed = {"sha": head, "route": "green", "identity": "c" * 64}
    state.update(generation=4, deployed=deployed,
                 lock={"attempt_id": vps.expected["attempt"], "sha": head, "generation": 4,
                       "session": "test-relay", "pr": 123, "token": "synthetic-lock",
                       "deployment": ds._deployment_identity("github", "github.com", "DUIDUI-Studio/DailyCheck", 123),
                       "heartbeat_at": ds.now_epoch()},
                 worker_relay={"target": vps.expected, "previous_deployed": deployed, "phase": "staging",
                               "previous_last_attempt": None, "rollback_runtime": standby})
    ds.native_guard(vps.directory, vps.marker)
    ds.save_state(vps.directory, state)

    conf = tmp_path / "nginx.conf"
    conf.write_bytes((ROOT / "scripts/blue_green/nginx-site.conf").read_bytes())
    logs = {color: tmp_path / (color + ".access") for color in ("blue", "green")}
    for path in logs.values():
        path.touch()
    bin_dir = install_fake_systemctl(tmp_path / "bin")
    nginx = bin_dir / "nginx"
    nginx.write_text("#!/bin/sh\n[ \"$1\" = -t ]\n")
    nginx.chmod(0o755)

    source_db = tmp_path / "source"
    (source_db / "warehouses").mkdir(parents=True)
    master = source_db / "master.db"
    stockout_schema = """
            CREATE TABLE item_routing_stockout_state(pause_id, tenant_id, canonical_id);
            CREATE TABLE item_routing_stockout_events(id, operation, pause_id, tenant_id, canonical_id, warehouse_code);
    """
    with closing(sqlite3.connect(master)) as conn:
        conn.executescript("""
            PRAGMA user_version=9;
            CREATE TABLE item_routing(id);
            CREATE TABLE mcp_policy_presets(id);
            CREATE TABLE mcp_policy_bindings(id);
        """)
    clone_db = tmp_path / "preview"
    clone_db.mkdir()
    (clone_db / "master.db").write_bytes(master.read_bytes())
    fence = tmp_path / "routing-writes.lock"
    monkeypatch.setattr(writer, "_open_fence", lambda _: os.open(fence, os.O_RDONLY))
    vps.profile = {"source_db_identity": database_identity(source_db)}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            with logs[_route(conf.read_text())].open("a") as stream:
                stream.write(self.path + "\n")
            self.send_response(200)
            self.end_headers()
        def log_message(self, *_):
            pass

    with http.server.HTTPServer(("127.0.0.1", 0), Handler) as endpoint:
        thread = threading.Thread(target=endpoint.serve_forever, daemon=True)
        thread.start()
        try:
            port = object.__new__(RelayPipeline)
            port.server = vps
            port.policy = SimpleNamespace(authority=ROOT, location=lambda _: repo)
            port.value = {"source_db": str(source_db), "green_db": str(clone_db), "nginx_conf": str(conf)}
            monkeypatch.setattr(vps, "stage", port.stage)
            calls = []

            def blue_runtime(role):
                assert _route(conf.read_text()) == role
                if role == "green":
                    return observed
                for route in ("/health", "/api/mcp/"):
                    marker = route + "?relay-verified-blue"
                    with urlopen(f"http://127.0.0.1:{endpoint.server_port}" + marker, timeout=2) as response:
                        assert response.status == 200
                    assert marker in logs["blue"].read_text()
                    assert marker not in logs["green"].read_text()
                calls.append("blue observed")
                return standby

            monkeypatch.setattr(vps, "runtime", blue_runtime)
            monkeypatch.setattr(vps, "source", lambda: None)
            monkeypatch.setattr(port, "_stop_green", lambda: calls.append("green stopped"))
            env = {"PATH": f"{bin_dir}:{Path(sys.executable).parent}:/usr/bin:/bin",
                   "BACKUP_DIR": str(tmp_path / "backups"),
                   "BLUE_ACCESS_LOG": str(logs["blue"]), "GREEN_ACCESS_LOG": str(logs["green"]),
                   "NGINX_DAILYCHECK_CONF": str(conf),
                   "NGINX_ENTRY": f"http://127.0.0.1:{endpoint.server_port}",
                   "DAILYCHECK_BG_LOCK_FILE": str(tmp_path / "bluegreen.lock"),
                   "DAILYCHECK_DEPLOY_STATE_DIR": str(vps.directory),
                   "DC_CONTROL_PYTHON": sys.executable}
            with lock_file(vps.directory, ds.ATTEMPT_LOCK_FILENAME) as owner:
                fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                # Let monkeypatch restore the two process settings populated by the real bridge.
                monkeypatch.setenv(ds.ATTEMPT_FD_ENV, "")
                monkeypatch.setenv(ds.ATTEMPT_ENV, "")
                vps._bind_owner(owner.fileno())
                env.update({k: os.environ[k] for k in (ds.ATTEMPT_FD_ENV, ds.ATTEMPT_ENV)})
                monkeypatch.setattr(port, "_control_env", lambda _: env)
                try:
                    with monkeypatch.context() as unrelated:
                        unrelated.setitem(vps.expected, "head", base)
                        with port._routing_switch_guard("candidate") as fence_fd:
                            assert fence_fd is None
                    fence.write_text("disabled\n")
                    with port._routing_switch_guard("candidate"):
                        pass
                    with pytest.raises(RoutingSwitchBlocked, match="hold_state_unavailable"):
                        port.stage("switch")
                    with closing(sqlite3.connect(master)) as conn:
                        conn.executescript(stockout_schema)
                    port.stage("switch")
                    assert _route(conf.read_text()) == "green"
                    vps._set(phase="verified", observed=observed, ci="d" * 64, journal=[],
                             observation_seconds=600, samples=20, source_method="incremental_bundle",
                             fixture=False)
                    before = conf.read_bytes()
                    identity = vps.profile["source_db_identity"]

                    def blocked(reason):
                        with pytest.raises(RoutingSwitchBlocked, match=reason):
                            vps.rollback("preflight", owner.fileno())
                        saved = vps.state()
                        assert saved["worker_relay"]["routing_block"]["reason"] == reason
                        assert saved["worker_relay"]["phase"] == "verified"
                        assert conf.read_bytes() == before and calls == []
                        assert saved["lock"]["attempt_id"] == vps.expected["attempt"]
                        assert not (vps.work / "rollback.log").exists()
                        assert saved["outcomes"] == {} and saved["deployed"] == deployed

                    fence.write_text("enabled\n")
                    blocked("writer_unfenced")
                    with writer.routing_writer_fence():
                        blocked("writer_unfenced")
                    fence.unlink()
                    blocked("writer_unfenced")
                    fence.write_text("disabled\n")
                    with closing(sqlite3.connect(master)) as conn, conn:
                        conn.execute("PRAGMA user_version=10")
                        conn.execute("ALTER TABLE item_routing_stockout_state RENAME TO temporarily_missing")
                    blocked("hold_state_unavailable")
                    with closing(sqlite3.connect(master)) as conn, conn:
                        conn.execute("ALTER TABLE temporarily_missing RENAME TO item_routing_stockout_state")
                        conn.execute("INSERT INTO item_routing_stockout_events VALUES ('p','pause',NULL,1,201,'dc_alpha')")
                    blocked("hold_state_unavailable")
                    with closing(sqlite3.connect(master)) as conn, conn:
                        conn.execute("INSERT INTO item_routing_stockout_state VALUES ('p',1,201)")
                    blocked("active_holds")
                    vps.profile["source_db_identity"] = "f" * 64
                    blocked("hold_state_unavailable")
                    vps.profile["source_db_identity"] = identity
                    database_bytes = master.read_bytes()
                    master.write_bytes(b"unreadable database")
                    blocked("hold_state_unavailable")
                    master.write_bytes(database_bytes)
                    with closing(sqlite3.connect(master)) as conn, conn:
                        conn.execute("INSERT INTO item_routing_stockout_events VALUES ('r','restore','p',1,201,'dc_alpha')")
                        conn.execute("UPDATE item_routing_stockout_state SET pause_id = NULL")

                except StageFailure as failure:
                    pytest.fail((vps.work / (str(failure) + ".log")).read_text())
            receipt = observation(vps.status())
            remote = {"pr_id": 456, "pr_node": "synthetic123", "head": head, "main": base, "merged": False}
            abort = {"op": "abort", "remote": remote, "observation": receipt}
            fence.write_text("enabled\n")
            with pytest.raises(RoutingSwitchBlocked, match="writer_unfenced"):
                vps.action(abort)
            assert observation(vps.status()) == receipt
            with pytest.raises(ValueError, match="rollback is blocked"):
                vps.action({"op": "arm", "ci": "d" * 64, "observation": receipt})
            fence.write_text("disabled\n")
            result = vps.action(abort)
            assert result["phase"] == "rolled_back" and result["live"] is True and result["receipt"]
            assert _route(conf.read_text()) == "blue"
            assert calls == ["blue observed", "green stopped", "blue observed", "blue observed"]
            assert vps.state()["deployed"] == deployed
            assert all("dc_probe_" in path.read_text() for path in logs.values())
            assert ds.native_blocked(vps.directory, vps.state())
        finally:
            endpoint.shutdown()
            thread.join(timeout=5)
