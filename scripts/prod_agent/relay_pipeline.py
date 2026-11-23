"""Installed relay port for DailyCheck's protected blue/green shell helpers."""
from __future__ import annotations

import http.client
import os
import re
import secrets
import stat
import subprocess
import time
from pathlib import Path

from scripts.blue_green import deploy_state as ds
from scripts.prod_agent import relay_git
from scripts.prod_agent.native_pipeline import (
    ProtectedPipeline,
    literal_tree,
    parse_application_environment,
    verify_literal_release,
    write_literal_release,
)
from scripts.prod_agent.native_ports import SystemdObserver, protected_path
from scripts.prod_agent.relay_protocol import STATE_PATH, SYSTEM_ENV, digest, require

UNITS = {
    "blue": ["dailycheck-app.service", "dailycheck-mcp.service"],
    "green": ["dailycheck-green.service", "dailycheck-green-mcp.service"],
}


class Policy:
    fixture = anchor = None
    owner = 0
    path = STATE_PATH

    def __init__(self, server):
        self.authority = server.authority
        self.python = Path(server.profile["control_python"])
        self.value = server.profile
        self.server = server
        protected_path(self.python.resolve(), 0)

    def location(self, key):
        if key == "repo":
            row = self.server.state()["worker_relay"]
            lane = {"anonymous_https": "direct.git", "incremental_bundle": "bundle.git"}
            return self.server.work / lane[row["source_method"]]
        if key == "directory":
            return self.server.directory
        if key == "release_root":
            return self.server.path(key)
        # The relay reads no release App credentials or native issuer state.
        return self.path.parent / key


class RelayPipeline(ProtectedPipeline):
    def __init__(self, server):
        self.server = server
        super().__init__(Policy(server))

    def _application_environment(self, raw):
        return parse_application_environment(raw)

    def _private_paths(self):
        return [str(self.server.directory), str(STATE_PATH.parent),
                "-/etc/dailycheck-release", "-/etc/dailycheck-native",
                "-/root", "-/var/lib/dailycheck-release"]

    def _owned(self, candidate=None):
        row, lock = self.server.owned(self.server.state())
        identity = ds.AttemptIdentity.from_env()
        require(ds.process_lock_fd() is not None and identity is not None and identity.owns(lock)
                and row["phase"] in {"staging", "rollback_started"}, "relay pipeline owner lost")
        expected = self.server.expected
        return {"manifest": {"group_sha": expected["head"], "tree_sha": expected["tree"],
                             "subject_pr_number": expected["pr"]}}

    def _control_env(self, candidate):
        return {**super()._control_env(candidate), "DC_RELAY_MODE": "1"}

    def prepare(self, repo):
        candidate = self._owned()
        expected = self.server.expected
        require(repo == self.policy.location("repo"), "wrong protected source store")
        tree, entries = literal_tree(repo, expected["head"])
        require(tree == expected["tree"], "literal candidate tree changed")
        write_literal_release(repo, expected["head"], tree, self._release(candidate), entries)
        relay_git.git(repo, "update-ref", "--no-deref", "HEAD", expected["head"])

    def stage(self, name):
        from scripts.prod_agent.relay_vps import StageFailure

        candidate = self._owned()
        expected = self.server.expected
        if name != "rollback":
            repo = self.policy.location("repo")
            require(relay_git.git(repo, "rev-parse", "HEAD").strip().decode() == expected["head"],
                    "protected stage cursor changed")
        args = {"up": ("up.sh", str(expected["pr"]), expected["head"]),
                "verify": ("verify.sh", str(expected["pr"])), "promote": ("promote.sh",),
                "switch": ("switch.sh", "green", "100"),
                "rollback": ("switch.sh", "blue", "100")}[name]
        script, *argv = args
        log = self.server.work / (name + ".log")
        with log.open("xb") as stream:
            result = subprocess.run(["/bin/bash", str(self.policy.authority / "scripts/blue_green" / script),
                                     *argv], env=self._control_env(candidate), cwd=self.policy.authority,
                                    pass_fds=(ds.process_lock_fd(),), stdin=subprocess.DEVNULL,
                                    stdout=stream, stderr=subprocess.STDOUT, check=False)
        if result.returncode > 0:
            raise StageFailure(name)
        require(result.returncode == 0, "stage died without a normal exit")


def candidate_operation(operation):
    from scripts.prod_agent.relay_vps import Server

    fd = ds.process_lock_fd()
    require(os.geteuid() == 0 and fd is not None, "private protected helper")
    probe = {k: os.environ[k] for k in ("SIM_USER_USER", "SIM_USER_PASSWORD")}
    server = Server(STATE_PATH)
    server._bind_owner(fd)
    os.environ.update(probe)
    pipeline = RelayPipeline(server)
    pipeline._owned()
    if operation == "prepare-services":
        pipeline.prepare_services()
    elif operation == "copy-db":
        pipeline.copy_db()
    else:
        return pipeline.operation(operation)
    return 0


def _http(port, path, token=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        headers = {"Authorization": "Bearer " + token} if token else {}
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        return response.status, response.read(65537)
    finally:
        connection.close()


def _tail(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "instance log identity")
        stream.seek(max(0, info.st_size - 512 * 1024))
        return stream.read(1024 * 1024)


def _route(config):
    result = []
    for name, blue, green in (("dailycheck", 8090, 8091), ("dailycheck-mcp", 5100, 5101)):
        body = re.findall(r"(?m)^\s*upstream " + name + r"\s*\{([^{}]+)\}", config)
        require(len(body) == 1, "unknown Nginx upstream")
        matches = re.findall(r"(?m)^\s*server\s+127\.0\.0\.1:(\d+)\s+(down|weight=100);", body[0])
        require(len(matches) == 2 and {p for p, _ in matches} == {str(blue), str(green)}
                and {mode for _, mode in matches} == {"down", "weight=100"},
                "upstream must be pinned at 100 percent")
        active = next(p for p, mode in matches if mode == "weight=100")
        result.append("blue" if active == str(blue) else "green")
    require(result[0] == result[1], "app/MCP routes diverged")
    return result[0]


def live_route(server, role):
    policy = Policy(server)
    pipeline = policy.value["pipeline"]
    config = protected_path(Path(pipeline["nginx_conf"]), 0)
    active = _route(config.read_text())
    color = active if role == "previous" else "blue" if role == "standby" else role
    require(role == "standby" or active == color, "wrong live route")
    expected = server.expected
    sha = expected["head"] if role == "green" else expected["base"]
    if role == "green":
        repo, release = policy.location("repo"), policy.location("release_root") / sha
    else:
        repo = server.seed
        release = server.path("baseline_" + color)
    protected_path(release, 0)
    tree, entries = literal_tree(repo, sha)
    verify_literal_release(repo, sha, tree, release, entries)
    policy.value = {**policy.value, "units": UNITS[color]}
    observer = SystemdObserver()
    processes = observer.observe(policy)
    require(len(processes) == 2 and all(row["cwd"] == str(release) for row in processes),
            "serving processes are not the exact protected release")
    if role == "green":
        for row in processes:
            environment = (Path("/proc") / str(row["pid"]) / "environ").read_bytes().split(b"\0")
            require(b"DRY_RUN=false" in environment and
                    os.fsencode("DAILYCHECK_DB_DIR=" + pipeline["source_db"]) in environment,
                    "green runtime was not promoted to the approved write route")
    app_port, mcp_port = (8090, 5100) if color == "blue" else (8091, 5101)
    env_path = protected_path(Path(pipeline["app_environment_file"]), 0, private=True)
    app_env = parse_application_environment(env_path.read_bytes())
    token = app_env["DAILYCHECK_MCP_TOKEN"]
    for port, credential in ((app_port, None), (mcp_port, token)):
        code, body = _http(port, "/health", credential)
        require(code == 200 and len(body) <= 65536, "direct app/MCP health failed")
    if role != "standby":
        nonce = ("dc_relay_" + secrets.token_hex(16)).encode()
        code, body = _http(8080, "/health?" + nonce.decode() + "=1")
        require(code == 200 and len(body) <= 65536, "public app route unhealthy")
        for _ in range(10):
            if nonce in _tail(Path(pipeline[color + "_access_log"])):
                break
            time.sleep(0.2)
        other = "blue" if color == "green" else "green"
        require(nonce in _tail(Path(pipeline[color + "_access_log"]))
                and nonce not in _tail(Path(pipeline[other + "_access_log"])),
                "public route landing unproven")
        code, body = _http(8080, "/api/mcp/", token)
        require(code in {200, 400, 405, 406} and len(body) <= 65536, "public MCP route unhealthy")
    if role == "green":
        row = server.state()["worker_relay"]
        switch = next(s for s in row["journal"] if s["stage"] == "switch")
        journal = subprocess.check_output(
            ["/usr/bin/journalctl", "-u", UNITS["green"][0], "--since",
             "@" + str(switch["started_at"]), "--no-pager"], env=SYSTEM_ENV, timeout=15)
        require(b"[dry_run] BLOCKED" not in journal, "live writes blocked")
    require(observer.observe(policy) == processes and _route(config.read_text()) == active,
            "runtime changed during observation")
    return {"sha": sha, "route": color, "identity": digest({"tree": tree, "processes": processes})}
