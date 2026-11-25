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


def test_relay_write_paths_include_only_the_pinned_external_warehouse_parent(tmp_path):
    root = tmp_path.resolve()
    source = root / "source"
    warehouses = source / "warehouses"
    warehouses.mkdir(parents=True)
    (source / "master.db").touch()
    external = root / "warehouse-volume" / "wh_002.db"
    external.parent.mkdir()
    external.touch()
    (warehouses / external.name).symlink_to(external)
    green = root / "green"
    registration = {external.name: str(external)}
    pipeline = object.__new__(port.RelayPipeline)
    pipeline.value = {"source_db": str(source), "green_db": str(green)}
    pipeline.server = SimpleNamespace(
        authority=root / "authority", directory=root / "state", seed=root / "seed",
        profile={"external_warehouse_dbs": registration,
                 "source_db_identity": port.database_identity(source, registration)},
    )
    assert pipeline._database_write_paths() == (green, source, external.parent)
    pipeline.server.seed = external.parent
    with pytest.raises(ValueError, match="overlaps control paths"):
        pipeline._database_write_paths()
    pipeline.server.seed = root / "seed"
    external.rename(external.with_suffix(".previous"))
    external.touch()
    with pytest.raises(ValueError, match="approved external warehouse identity changed"):
        pipeline._database_write_paths()


def test_relay_preview_and_live_units_use_separate_database_and_access_log(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    source, green, external = (root / name for name in ("source", "green", "warehouse-volume"))
    audit, access = root / "logs" / "admin_audit.log", root / "logs" / "green-access.log"
    live_env = root / "control" / "live.env"
    pipeline = object.__new__(port.RelayPipeline)
    pipeline.server = SimpleNamespace(work=root / "control" / "attempt", profile={})
    pipeline.policy = SimpleNamespace(value={"app_uid": 996}, owner=0, anchor=None)
    pipeline.python = Path("/usr/bin/python3")
    pipeline.value = {"source_db": str(source), "green_db": str(green), "app_gid": 988,
                      "green_access_log": str(access), "app_environment_file": str(live_env)}
    monkeypatch.setattr(pipeline, "_release", lambda _: root / "literal-release")
    monkeypatch.setattr(pipeline, "_preview_uid", lambda: 993)
    monkeypatch.setattr(pipeline, "_admin_audit_log", lambda: audit)
    monkeypatch.setattr(pipeline, "_private_paths", lambda: [str(root / "control")])
    monkeypatch.setattr(pipeline, "_database_write_paths", lambda: (green, source, external))
    preview = dict(pipeline._candidate_units({}))
    live = dict(pipeline._candidate_units({}, promoted=True))
    for key, content in preview.items():
        assert "ProtectProc=invisible" in content
        assert f"InaccessiblePaths={root / 'control'} {source} {external}" in content
        assert f"Environment=DAILYCHECK_DB_DIR={green}" in content
        assert "Environment=DRY_RUN=true" in content and "--reuid=993" in content
        assert f"EnvironmentFile={pipeline.server.work / 'preview.env'}" in content
        assert f"ReadWritePaths={green} {access.with_name('dailycheck-relay-preview-access.log')}" in content
        assert str(live_env) not in content and str(audit) not in content
        if key == port.UNITS["green"][0]:
            assert "--access-logfile " + str(pipeline._candidate_access_log(promoted=False)) in content
    for key, content in live.items():
        assert f"EnvironmentFile={live_env}" in content
        assert f"Environment=DAILYCHECK_DB_DIR={source}" in content
        assert "Environment=DRY_RUN=false" in content and "--reuid=996" in content
        assert f"ReadWritePaths={source} {external} {access}" in content
        assert str(green) not in content
        assert (str(audit) in content) == (key == port.UNITS["green"][0])

    assert all("LoadCredential=" not in content for content in (*preview.values(), *live.values()))
    pipeline.policy.python = Path("/usr/bin/python3")
    monkeypatch.setattr(pipeline, "_candidate_routing_proof", lambda: b"synthetic-proof")
    opted_in = dict(pipeline._candidate_units({}, promoted=True))
    assert dict(pipeline._candidate_units({})) == preview
    for unit, content in opted_in.items():
        assert f"LoadCredential={port.ROUTING_PROOF_CREDENTIAL}:{port.ROUTING_PROOF_FILE}" in content
        assert "synthetic-proof" not in content
        assert "DAILYCHECK_MULTI_WAREHOUSE=true" in content
        assert f"DAILYCHECK_DB_DIR={source}" in content
        assert (f"DAILYCHECK_ROUTING_WEB_PORT={port.GREEN_WEB_PORT}" in content) == (
            unit == port.UNITS["green"][1]
        )


def test_relay_requires_the_confirmed_blue_runtime_before_stopping_green(monkeypatch):
    pipeline = object.__new__(port.RelayPipeline)
    calls = []
    state = {}
    standby = {"sha": "base", "route": "blue", "identity": "expected"}
    actual = {**standby, "identity": "different"}
    pipeline.server = SimpleNamespace(
        state=lambda: state,
        owned=lambda record: ({"rollback_runtime": standby}, None),
        runtime=lambda role: calls.append(role) or actual,
    )
    monkeypatch.setattr(pipeline, "_stop_green", lambda: calls.append("stop green"))
    with pytest.raises(ValueError, match="confirmed Web and MCP traffic"):
        pipeline._stop_green_on_blue()
    assert calls == ["blue"]
    actual.update(standby)
    pipeline._stop_green_on_blue()
    assert calls == ["blue", "blue", "stop green"]


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
    release_root = root / "releases"
    release_root.mkdir()
    release = release_root / head
    baseline = release_root / base
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
    external = root / "external" / "store.db"
    external.parent.mkdir()
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
    audit = root / "audit" / "admin_audit.log"
    audit.parent.mkdir()
    audit.touch(mode=0o600)
    os.chown(audit, UID, GID)
    server = object.__new__(Server)
    server.fixture = None
    server.directory = root / "state"
    server.directory.mkdir(mode=0o700)
    server.seed = repo
    server.authority = ROOT
    server.expected = {"head": head, "base": base, "tree": tree}
    server.profile = {"control_python": "/usr/bin/python3", "app_uid": UID,
                      "baseline_blue": str(baseline), "baseline_green": str(baseline),
                      "release_root": str(release_root), "admin_audit_log": str(audit),
                      "source_db_identity": port.database_identity(db, registration),
                      "external_warehouse_dbs": registration,
                      "pipeline": {"source_db": str(db), "app_gid": GID, "nginx_conf": str(config),
                                   "app_environment_file": str(env),
                                   "blue_access_log": str(log[port.UNITS["blue"][0]]),
                                   "green_access_log": str(log[port.UNITS["green"][0]])}}
    monkeypatch.setattr(port.Policy, "location", lambda self, key: repo if key == "repo" else release_root)
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
               data=db, token=TOKEN, audit_path=audit):
        index = int(role == "mcp")
        unit = port.UNITS[color][index]
        if unit in processes:
            stop(unit)
        number = ((8090, 5100) if color == "blue" else (8091, 5101))[index]
        args = ["/usr/bin/setpriv", f"--reuid={user}", f"--regid={group}", "--clear-groups",
                "--bounding-set=-all", "--inh-caps=-all", "--ambient-caps=-all",
                "--no-new-privs", sys.executable, "-I", "-B", "-c", APP,
                str(number), role, str(log[unit])]
        env = {"DAILYCHECK_ENV": mode, "DRY_RUN": dry, "DAILYCHECK_DB_DIR": str(data), "TOKEN": token}
        if color == "green" and role == "app" and cwd == release:
            env["DAILYCHECK_ADMIN_AUDIT_LOG"] = str(audit_path)
        child = subprocess.Popen(args, env=env, cwd=cwd)
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
            audit.chmod(0o644)
            with pytest.raises(ValueError, match="private application-owned admin audit"):
                server.runtime("candidate-standby")
            audit.chmod(0o600)
            server.profile["admin_audit_log"] = str(release / audit.name)
            with pytest.raises(ValueError, match="overlaps a protected or data domain"):
                server.runtime("candidate-standby")
            server.profile["admin_audit_log"] = str(audit)
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
                ({"audit_path": root / "wrong.log"}, "approved write route"),
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
