"""Real loopback and /proc under disposable Linux with a systemd/Nginx fixture."""
from __future__ import annotations

import contextlib
import http.client
import http.server
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.ci.relay_exact import offline
from scripts.prod_agent import relay_pipeline as port
from scripts.prod_agent.native_pipeline import literal_tree, write_literal_release
from scripts.prod_agent.relay_git import git
from scripts.prod_agent.relay_vps import Server, StageFailure

UID, GID = 4001, 4002
TOKEN = "synthetic_fixture_token_" + "a" * 32
ROOT = Path(__file__).resolve().parents[1]
APP = r"""
import http.server, os, sys
port, mcp, logfile = int(sys.argv[1]), sys.argv[2] == 'mcp', sys.argv[3]
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        with open(logfile, 'a') as log:
            log.write(self.path + '\n')
        status = 200
        if mcp and self.headers.get('Authorization') != 'Bearer ' + os.environ['TOKEN']:
            status = 401
        self.send_response(status)
        self.end_headers()
    def log_message(self, *_):
        pass
http.server.HTTPServer(('127.0.0.1', port), Handler).serve_forever()
"""


@pytest.mark.skipif(os.environ.get("DAILYCHECK_RELAY_SSH_INTEGRATION") != "1",
                    reason="requires the isolated Linux/root relay lane")
def test_live_route_real_proc_and_http(monkeypatch):
    assert sys.platform == "linux" and os.geteuid() == 0
    assert offline()
    with tempfile.TemporaryDirectory(prefix="relay-live-", dir="/var/lib") as tmp:
        root = Path(tmp)
        root.chmod(0o755)
        exercise_live_route(monkeypatch, root)


def exercise_live_route(monkeypatch, root):
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "--template=")
    for name in ("base", "head"):
        (repo / "version").write_text(name)
        git(repo, "add", "version")
        git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "commit", "-qm", name)
    head = git(repo, "rev-parse", "HEAD").decode().strip()
    base = git(repo, "rev-parse", "HEAD^").decode().strip()
    release = root / head
    baseline = root / base
    for sha, dest in ((head, release), (base, baseline)):
        tree, entries = literal_tree(repo, sha)
        write_literal_release(repo, sha, tree, dest, entries)
    tree = literal_tree(repo, head)[0]
    db = root / "db"
    (db / "warehouses").mkdir(parents=True)
    for path in (db / "master.db", db / "warehouses/store.db"):
        with contextlib.closing(sqlite3.connect(path)) as conn:
            conn.execute("CREATE TABLE fixture(id INTEGER PRIMARY KEY)")
        os.chown(path, UID, GID)
        path.chmod(0o600)
    for directory in (db, db / "warehouses"):
        os.chown(directory, UID, GID)
        directory.chmod(0o700)
    external = root / "external.db"
    (db / "warehouses/store.db").rename(external)
    link = db / "warehouses/store.db"
    link.symlink_to(external)
    registration = {link.name: str(external)}
    config = root / "nginx.conf"
    blue = (ROOT / "scripts/blue_green/nginx-site.conf").read_text()
    green = blue.replace("weight=100;", "TMP;").replace("down;", "weight=100;").replace("TMP;", "down;")
    config.write_text(blue)
    env = root / "application.env"
    env.write_text("DAILYCHECK_SECRET_KEY=" + "k" * 32 + "\nDAILYCHECK_MCP_TOKEN=" + TOKEN + "\n")
    env.chmod(0o600)
    log = {unit: root / (unit + ".log") for pair in port.UNITS.values() for unit in pair}
    for file in log.values():
        file.touch(mode=0o600)
        os.chown(file, UID, GID)
    server = object.__new__(Server)
    server.fixture = None
    server.directory = root / "state"
    server.directory.mkdir(mode=0o700)
    server.seed = repo
    server.authority = ROOT
    server.expected = {"head": head, "base": base, "tree": tree}
    server.profile = {"control_python": "/usr/bin/python3", "app_uid": UID,
                      "baseline_blue": str(baseline), "baseline_green": str(baseline),
                      "source_db_identity": port.database_identity(db, registration),
                      "external_warehouse_dbs": registration,
                      "pipeline": {"source_db": str(db), "app_gid": GID, "nginx_conf": str(config),
                                   "app_environment_file": str(env),
                                   "blue_access_log": str(log[port.UNITS["blue"][0]]),
                                   "green_access_log": str(log[port.UNITS["green"][0]])}}
    monkeypatch.setattr(port.Policy, "location", lambda self, key: repo if key == "repo" else root)
    monkeypatch.setattr(server, "state", lambda: {"worker_relay": {"journal": [
        {"stage": "switch", "started_at": int(time.time()) - 1}]}})
    processes, override = {}, {}
    real_output = subprocess.check_output
    real_run = subprocess.run
    journal_error = {}

    def system_output(argv, **kwargs):
        if argv[0] == "/usr/bin/systemctl":
            unit = argv[2]
            proc, cwd = processes[unit]
            cgroup = Path("/proc", str(proc.pid), "cgroup").read_text().splitlines()[0].split(":", 2)[2]
            fields = {"Id": unit, "LoadState": "loaded", "ActiveState": "active",
                      "SubState": "running", "MainPID": proc.pid, "WorkingDirectory": cwd,
                      "ControlGroup": cgroup, "NoNewPrivileges": "yes"}
            fields.update(override.get(unit, {}))
            return "".join(f"{k}={v}\n" for k, v in fields.items())
        return real_output(argv, **kwargs)

    def journal_output(argv, **kwargs):
        if argv[0] == "/usr/bin/journalctl":
            unit = argv[3]
            return subprocess.CompletedProcess(argv, 0, log[unit].read_bytes(),
                                               journal_error.get(unit, b""))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "check_output", system_output)
    monkeypatch.setattr(subprocess, "run", journal_output)
    force_route = {}
    suppress_landing = set()

    class Proxy(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            role = "mcp" if self.path.startswith("/api/mcp/") else "app"
            color = force_route.get(role) or port._route(config.read_text())
            index = int(role == "mcp")
            target = {"blue": (8090, 5100), "green": (8091, 5101)}[color][index]
            connection = http.client.HTTPConnection("127.0.0.1", target, timeout=3)
            try:
                headers = {"Authorization": self.headers["Authorization"]} if self.headers.get("Authorization") else {}
                path = self.path.split("?")[0] if role in suppress_landing else self.path
                connection.request("GET", path, headers=headers)
                response = connection.getresponse()
                self.send_response(response.status)
                self.end_headers()
                self.wfile.write(response.read())
            finally:
                connection.close()

        def log_message(self, *_):
            pass

    class Endpoint(http.server.HTTPServer):
        allow_reuse_address = True

    def stop(unit):
        process, _ = processes.pop(unit)
        process.terminate()
        process.wait(timeout=5)

    def launch(color, role, cwd, *, user=UID, group=GID, dry="false", mode="production",
               data=db, token=TOKEN):
        index = int(role == "mcp")
        unit = port.UNITS[color][index]
        if unit in processes:
            stop(unit)
        number = ((8090, 5100) if color == "blue" else (8091, 5101))[index]
        args = ["/usr/bin/setpriv", f"--reuid={user}", f"--regid={group}", "--clear-groups",
                "--bounding-set=-all", "--inh-caps=-all", "--ambient-caps=-all",
                "--no-new-privs", sys.executable, "-I", "-B", "-c", APP,
                str(number), role, str(log[unit])]
        child = subprocess.Popen(args, env={"DAILYCHECK_ENV": mode, "DRY_RUN": dry,
                                             "DAILYCHECK_DB_DIR": str(data), "TOKEN": token}, cwd=cwd)
        processes[unit] = child, cwd
        end = time.monotonic() + 5
        while time.monotonic() < end and child.poll() is None:
            try:
                with socket.create_connection(("127.0.0.1", number), timeout=.1):
                    return
            except OSError:
                time.sleep(.05)
        pytest.fail(f"fixture process failed to listen on {number}")

    with Endpoint(("127.0.0.1", 8080), Proxy) as endpoint:
        server_thread = threading.Thread(target=endpoint.serve_forever, daemon=True)
        server_thread.start()
        try:
            launch("blue", "app", baseline)
            launch("blue", "mcp", baseline)
            launch("green", "app", baseline)
            launch("green", "mcp", baseline)
            ledger = server.directory / "state.json"
            ledger.write_bytes(b'{"generation":20,"last_attempt":{"pr":43},"lock":null}')
            before = ledger.read_bytes(), tuple(server.directory.iterdir())
            prior = server.runtime("previous")
            assert prior == server.runtime("standby") == server.runtime("blue")
            assert prior["route"] == "blue" and prior["sha"] == base
            config.write_text(green)
            assert server.runtime("previous")["sha"] == base
            assert server.runtime("standby") == prior
            assert before == (ledger.read_bytes(), tuple(server.directory.iterdir()))
            config.write_text(blue)
            launch("green", "app", release)
            launch("green", "mcp", release)
            standby = server.observe_green("candidate-standby")
            assert standby["sha"] == head and standby["route"] == "green"
            real_statvfs = os.statvfs

            def readonly_external(path):
                if str(path).startswith("/proc/") and path.name == external.name:
                    return SimpleNamespace(f_flag=os.ST_RDONLY)
                return real_statvfs(path)

            with monkeypatch.context() as patch:
                patch.setattr(os, "statvfs", readonly_external)
                with pytest.raises(StageFailure, match="observation read failed") as failure:
                    server.observe_green("candidate-standby")
                assert "external warehouse mount is read-only" in str(failure.value.__cause__)
                assert port._route(config.read_text()) == "blue"
            config.write_text(green)
            observed = server.observe_green()
            assert observed == server.observe_green()
            assert observed == standby
            assert observed["sha"] == head and observed["route"] == "green"
            green_unit = port.UNITS["green"][0]
            for fields, reason in (
                ({"ActiveState": "inactive", "MainPID": 0}, "not running"),
                ({"ActiveState": "failed", "SubState": "failed"}, "not running"),
                ({"Id": "dailycheck-app.service"}, "canonical unit"),
                ({"WorkingDirectory": baseline}, "checkout mismatch"),
            ):
                override[green_unit] = fields
                with pytest.raises(ValueError, match=reason):
                    server.runtime("green")
            override.clear()
            for changes, reason in (
                ({"user": 0}, "UID"),
                ({"group": 0}, "GID"),
                ({"cwd": baseline}, "exact protected release"),
                ({"dry": "true"}, "approved write route"),
                ({"mode": "development"}, "approved write route"),
                ({"data": root}, "approved write route"),
            ):
                launch("green", "app", **({"cwd": release} | changes))
                with pytest.raises(ValueError, match=reason):
                    server.runtime("green")
            launch("green", "app", release)
            with pytest.raises(ValueError, match="routes diverged"):
                port._route(green.replace("5100 down;", "5100 weight=100;")
                            .replace("5101 weight=100;", "5101 down;"))
            with pytest.raises(ValueError, match="unexpected upstream"):
                port._route(green.replace("server 127.0.0.1:8090 down;",
                                          "server 127.0.0.1:8090 down; server example.invalid;"))
            config.write_text(blue)
            with pytest.raises(StageFailure):
                server.observe_green()
            config.write_text(green)
            force_route["mcp"] = "blue"
            with pytest.raises(ValueError, match="MCP route landing"):
                server.runtime("green")
            force_route.clear()
            suppress_landing.add("app")
            with pytest.raises(ValueError, match="public route landing"):
                server.runtime("green")
            suppress_landing.clear()
            launch("green", "mcp", release, token="wrong")
            with pytest.raises(ValueError, match="direct app/MCP health"):
                server.runtime("green")
            launch("green", "mcp", release)
            assert server.observe_green() != observed  # a restart breaks continuity
            journal_error[green_unit] = b"journal access denied"
            with pytest.raises(ValueError, match="journal unavailable"):
                server.runtime("green")
            journal_error.clear()
            log[green_unit].write_bytes(b"[dry_run] BLOCKED\n" + b"line\n" * 5000)
            with pytest.raises(ValueError, match="journal window overflow"):
                server.runtime("green")
            log[green_unit].write_bytes(b"[dry_run] BLOCKED\n")
            with pytest.raises(ValueError, match="live writes blocked"):
                server.runtime("green")
            log[green_unit].write_bytes(b"")
            link.unlink()
            link.symlink_to(db / "master.db")
            with pytest.raises(ValueError, match="warehouse target changed"):
                server.runtime("green")
            link.unlink()
            link.symlink_to(external)
            duplicate = db / "warehouses/duplicate.db"
            os.link(external, duplicate)
            with pytest.raises(ValueError, match="duplicate physical database"):
                server.runtime("green")
            duplicate.unlink()
            external.rename(external.with_suffix(".old"))
            external.touch()
            with pytest.raises(ValueError, match="DB identity changed"):
                server.runtime("green")
        finally:
            endpoint.shutdown()
            server_thread.join(timeout=5)
            for unit in list(processes):
                stop(unit)
