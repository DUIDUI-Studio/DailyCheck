"""The relay's Linux stage port invokes the real switch helper and native lock."""
from __future__ import annotations

import fcntl
import http.server
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.blue_green import deploy_state as ds
from scripts.prod_agent.relay_pipeline import RelayPipeline, _route
from scripts.prod_agent.relay_protocol import lock_file
from scripts.prod_agent.relay_vps import Server, StageFailure
from tests.blue_green_testenv import install_fake_systemctl

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(sys.platform != "linux", reason="real Linux error and shell semantics")
def test_protected_relay_runs_real_switch_with_native_identity(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture",
                    "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty",
                    "-qm", "candidate"], check=True)
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                   text=True).strip()
    tree = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"],
                                   text=True).strip()
    vps = object.__new__(Server)
    vps.directory = tmp_path / "state"
    vps.directory.mkdir(mode=0o700)
    vps.work = tmp_path / "stages"
    vps.work.mkdir()
    vps.expected = {"attempt": "9" * 32, "pr": 123, "head": head, "tree": tree, "generation": 4}
    monkeypatch.setenv("DAILYCHECK_DEPLOY_STATE_DIR", str(vps.directory))

    state = ds._empty_state()
    deployed = {"sha": "b" * 40, "generation": 3}
    state.update(generation=4, deployed=deployed,
                 lock={"attempt_id": vps.expected["attempt"], "sha": head, "generation": 4,
                       "session": "test-relay", "pr": 123, "token": "synthetic-lock",
                       "heartbeat_at": ds.now_epoch()},
                 worker_relay={"target": vps.expected, "previous_deployed": deployed, "phase": "staging"})
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
                    port.stage("switch")
                    assert _route(conf.read_text()) == "green"
                    vps._set(phase="rollback_started")
                    port.stage("rollback")
                    assert _route(conf.read_text()) == "blue"
                except StageFailure as failure:
                    pytest.fail((vps.work / (str(failure) + ".log")).read_text())
            assert all("dc_probe_" in path.read_text() for path in logs.values())
            assert ds.native_blocked(vps.directory, vps.state())
        finally:
            endpoint.shutdown()
            thread.join(timeout=5)
