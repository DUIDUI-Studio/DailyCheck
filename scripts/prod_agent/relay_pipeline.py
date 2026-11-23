"""Installed relay port for DailyCheck's protected blue/green shell helpers."""
from __future__ import annotations

import http.client
import os
import re
import secrets
import stat
import subprocess
import tempfile
import time
from pathlib import Path

from scripts.blue_green import deploy_state as ds
from scripts.prod_agent import relay_git
from scripts.prod_agent.native_pipeline import (
    RELEASE_MODES,
    ProtectedPipeline,
    literal_tree,
    parse_application_environment,
    verify_literal_release,
    write_literal_release,
)
from scripts.prod_agent.native_ports import SystemdObserver, protected_path
from scripts.prod_agent.relay_protocol import STATE_PATH, SYSTEM_ENV, digest, require

UNITS = {
    "blue": ["dailycheck.service", "dailycheck-mcp.service"],
    "green": ["dailycheck-green.service", "dailycheck-green-mcp.service"],
}


def _audit_inode(path, uid, gid):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
            and (info.st_uid, info.st_gid) == (uid, gid) and stat.S_IMODE(info.st_mode) == 0o600,
            "private application-owned admin audit file required")
    require(not os.statvfs(path).f_flag & os.ST_RDONLY, "admin audit mount is read-only")
    return info.st_dev, info.st_ino


def admin_audit_path(server):
    profile = server.profile
    pipeline = profile["pipeline"]
    path = Path(profile["admin_audit_log"])
    require(path.name == "admin_audit.log" and path == path.resolve() and ".." not in path.parts
            and re.fullmatch(r"/[a-zA-Z0-9_./-]+", str(path)), "canonical admin audit path required")
    protected_path(path.parent, 0)
    other = [server.authority, server.directory, server.seed, STATE_PATH.parent,
             Path("/root"), Path("/etc/dailycheck-native"), Path("/etc/dailycheck-release"),
             Path("/var/lib/dailycheck-release")]
    other += [Path(profile[key]) for key in ("release_root", "baseline_blue", "baseline_green",
              "worker_signers", "control_python")
              if key in profile]
    other += [Path(pipeline[key]) for key in ("source_db", "green_db", "scratch_root",
              "unit_directory", "backup_directory", "log_directory") if key in pipeline]
    other += [Path(pipeline[key]) for key in ("app_environment_file", "app_python",
              "nginx_conf", "blue_access_log", "green_access_log") if key in pipeline]
    external = profile.get("external_warehouse_dbs", {})
    require(isinstance(external, dict) and all(isinstance(value, str) for value in external.values()),
            "invalid external warehouse registration")
    other += [Path(value).parent for value in external.values()]
    require(not any(path.parent.is_relative_to(item) or item.is_relative_to(path.parent)
                    for item in other), "admin audit directory overlaps a protected or data domain")
    _audit_inode(path, profile["app_uid"], pipeline["app_gid"])
    return path


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
        return {**super()._control_env(candidate), "DC_RELAY_MODE": "1",
                "BLUE_SERVICE_UNIT": UNITS["blue"][0], "GREEN_SERVICE_UNIT": UNITS["green"][0]}

    def _admin_audit_log(self):
        return admin_audit_path(self.server)

    def _pytest(self, scratch):
        self._owned()
        audit = scratch / "admin_audit.log"
        fd = os.open(audit, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as sink:
            os.fchown(sink.fileno(), self.policy.value["app_uid"], self.value["app_gid"])
        # This sibling of the UID-owned scratch is root-prepared and root-owned.
        # Runtime and control paths, including the repo's seed alternate, stay private.
        with tempfile.TemporaryDirectory(prefix="relay-check1-", dir=self.path("scratch_root")) as tmp:
            parent = Path(tmp)
            checkout = parent / "source"
            prepare_test_checkout(self.policy.location("repo"), self.server.expected, checkout)
            parent.chmod(0o711)
            return super()._pytest(scratch, cwd=checkout, extra={
                "DAILYCHECK_ADMIN_AUDIT_LOG": str(audit),
                "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory",
                "GIT_CONFIG_VALUE_0": str(checkout),
            })

    def prepare(self, repo):
        candidate = self._owned()
        self._admin_audit_log()
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


def prepare_test_checkout(repo, expected, checkout):
    """Build an independent local Git view using only literal bytes and Git plumbing."""
    head, base = expected["head"], expected["base"]
    relay_git.check_objects(repo, expected)
    tree, entries = literal_tree(repo, head)
    require(tree == expected["tree"], "test checkout candidate tree changed")
    write_literal_release(repo, head, tree, checkout, entries)
    checkout.chmod(0o755)
    (checkout / "release.json").unlink()
    # Preserve usual Git working-tree modes on the test view. The root owner
    # is still the only writer, and tests can mutate their own copies of blobs.
    for path in checkout.rglob("*"):
        if path.is_dir():
            path.chmod(0o755)
    for row in entries:
        (checkout / row["path"]).chmod(RELEASE_MODES[row["mode"]] | stat.S_IWUSR)
    relay_git.git(checkout, "init", "--template=")
    metadata = checkout / ".git"
    # No local clone, external alternate, hardlinks, transport, hooks or candidate
    # checkout/filter execution. Copy H's object graph, including exact ancestor B.
    relay_git.git(repo, "pack-objects", "--revs", str(metadata / "objects/pack/pack"),
                  input=(head + "\n").encode())
    for key, value in (("core.hooksPath", "/dev/null"), ("core.fsmonitor", "false"),
                       ("credential.helper", ""), ("protocol.allow", "never")):
        relay_git.git(checkout, "config", key, value)
    relay_git.git(checkout, "update-ref", "--no-deref", "HEAD", head)
    relay_git.git(checkout, "update-ref", "refs/remotes/origin/main", base)
    relay_git.git(checkout, "read-tree", head)
    relay_git.check_objects(checkout, expected)
    for path in metadata.rglob("*"):
        path.chmod(0o555 if path.is_dir() else 0o444)
    metadata.chmod(0o555)


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
    config = re.sub(r"(?m)#[^\n]*", "", config)
    for name, blue, green in (("dailycheck", 8090, 8091), ("dailycheck-mcp", 5100, 5101)):
        body = re.findall(r"(?m)^\s*upstream " + name + r"\s*\{([^{}]+)\}", config)
        require(len(body) == 1, "unknown Nginx upstream")
        server_line = r"server\s+127\.0\.0\.1:(\d+)\s+(down|weight=100)"
        directives = body[0].strip().split(";")
        matches = [re.fullmatch(server_line, line.strip()) for line in directives[:-1]]
        require(not directives[-1].strip() and all(matches), "unexpected upstream directive")
        matches = [match.groups() for match in matches]
        require(len(matches) == 2 and {p for p, _ in matches} == {str(blue), str(green)}
                and {mode for _, mode in matches} == {"down", "weight=100"},
                "upstream must be pinned at 100 percent")
        active = next(p for p, mode in matches if mode == "weight=100")
        result.append("blue" if active == str(blue) else "green")
    require(result[0] == result[1], "app/MCP routes diverged")
    return result[0]


def database_identity(path, external=None, *, view=Path("/")):
    """Physical identity of the approved directory, Master and each warehouse DB."""
    def entry(file, file_type):
        info = file.lstat()
        require(stat.S_IFMT(info.st_mode) == file_type, "unexpected DB layout or symlink")
        return [info.st_dev, info.st_ino, info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)]

    external = external if external is not None else {}
    require(isinstance(external, dict), "invalid external warehouse registration")
    path = view / path.relative_to("/")
    for parent in path.parents:
        if parent == view:
            break
        entry(parent, stat.S_IFDIR)
    warehouses = path / "warehouses"
    for directory in (path, warehouses):
        require(not os.statvfs(directory).f_flag & os.ST_RDONLY, "runtime DB mount is read-only")
    master = path / "master.db"
    require(not os.statvfs(master).f_flag & os.ST_RDONLY, "master DB mount is read-only")
    identity = {".": entry(path, stat.S_IFDIR), "master.db": entry(master, stat.S_IFREG),
                "warehouses": entry(warehouses, stat.S_IFDIR)}
    physical = {tuple(identity["master.db"][:2])}
    files = {file.name: file for file in warehouses.glob("*.db")}
    require(external.keys() <= files.keys(), "registered external warehouse absent")
    for name, file in sorted(files.items()):
        key = "warehouses/" + name
        if name in external:
            target = Path(external[name])
            require(target.is_absolute() and ".." not in target.parts,
                    "canonical external warehouse path required")
            lexical = os.readlink(file)
            declared = Path(lexical)
            if not declared.is_absolute():
                declared = Path(os.path.normpath(str(Path("/") / warehouses.relative_to(view) / declared)))
            require(declared == target, "external warehouse target changed")
            actual = view / target.relative_to("/")
            for parent in actual.parents:
                if parent == view:
                    break
                entry(parent, stat.S_IFDIR)
            info = entry(actual, stat.S_IFREG)
            require(not os.statvfs(actual).f_flag & os.ST_RDONLY
                    and not os.statvfs(actual.parent).f_flag & os.ST_RDONLY,
                    "external warehouse mount is read-only")
            identity[key] = [entry(file, stat.S_IFLNK), lexical, str(target), info]
        else:
            info = entry(file, stat.S_IFREG)
            require(not os.statvfs(file).f_flag & os.ST_RDONLY, "warehouse DB mount is read-only")
            identity[key] = info
        device_inode = tuple(info[:2])
        require(device_inode not in physical, "duplicate physical database")
        physical.add(device_inode)
    return digest(identity)


def _journal(unit, since):
    result = subprocess.run(
        ["/usr/bin/journalctl", "-q", "-u", unit, "--since", "@" + str(since),
         "--lines=5000", "--no-pager"], env=SYSTEM_ENV, timeout=15, capture_output=True,
         check=True)
    require(not result.stderr, "runtime journal unavailable")
    require(len(result.stdout.splitlines()) < 5000, "runtime journal window overflow")
    return result.stdout


def live_route(server, role):
    policy = Policy(server)
    pipeline = policy.value["pipeline"]
    config = protected_path(Path(pipeline["nginx_conf"]), 0)
    active = _route(config.read_text())
    candidate = role in {"green", "candidate-standby"}
    color = {"previous": active, "standby": "blue", "candidate-standby": "green"}.get(role, role)
    if role == "candidate-standby":
        require(active == "blue", "candidate standby requires blue traffic")
    else:
        require(role == "standby" or active == color, "wrong live route")
    expected = server.expected
    sha = expected["head"] if candidate else expected["base"]
    if candidate:
        repo, release = policy.location("repo"), policy.location("release_root") / sha
    else:
        repo = server.seed
        release = server.path("baseline_" + color)
    protected_path(release, 0)
    tree, entries = literal_tree(repo, sha)
    if candidate:
        require(tree == expected["tree"], "live candidate tree changed")
    verify_literal_release(repo, sha, tree, release, entries)
    require(all(path.lstat().st_uid == 0 for path in (release, *release.rglob("*")))
            and not release.stat().st_mode & 0o222, "live release ownership or mode changed")
    policy.value = {**policy.value, "units": UNITS[color]}
    require(type(policy.value["app_uid"]) is int and policy.value["app_uid"] > 0
            and type(pipeline["app_gid"]) is int and pipeline["app_gid"] > 0,
            "nonroot runtime principal required")
    observer = SystemdObserver(exact_unit=True)
    processes = observer.observe(policy)
    require(len({row["pid"] for row in processes}) == 2
            and all(row["cwd"] == str(release) for row in processes),
            "serving processes are not the exact protected release")
    source_db = Path(pipeline["source_db"])
    require(source_db.is_absolute() and source_db.resolve() == source_db
            and not source_db.is_relative_to(release), "canonical external DB required")
    db_identity = policy.value["source_db_identity"]
    external = policy.value.get("external_warehouse_dbs")
    audit = admin_audit_path(server) if candidate else None
    audit_id = _audit_inode(audit, policy.value["app_uid"], pipeline["app_gid"]) if audit else None

    def verify_data():
        require(database_identity(source_db, external) == db_identity, "approved DB identity changed")
        for row in processes:
            proc = Path("/proc") / str(row["pid"])
            environment = (proc / "environ").read_bytes().split(b"\0")
            options = {"DRY_RUN": "false", "DAILYCHECK_ENV": "production",
                       "DAILYCHECK_DB_DIR": str(source_db)}
            if audit is not None and row["unit"] == UNITS[color][0]:
                options["DAILYCHECK_ADMIN_AUDIT_LOG"] = str(audit)
                uid, gid = policy.value["app_uid"], pipeline["app_gid"]
                require(_audit_inode(proc / "root" / audit.relative_to("/"), uid, gid) == audit_id,
                        "process admin audit inode differs")
            elif audit is not None:
                require(not any(field.startswith(b"DAILYCHECK_ADMIN_AUDIT_LOG=")
                                for field in environment), "admin audit input reached MCP")
            for key, value in options.items():
                prefix = os.fsencode(key + "=")
                require([x for x in environment if x.startswith(prefix)] ==
                        [prefix + os.fsencode(value)], "runtime is not on the approved write route")
            require(database_identity(source_db, external, view=proc / "root") == db_identity,
                    "process mount DB identity changed")

    verify_data()
    app_port, mcp_port = (8090, 5100) if color == "blue" else (8091, 5101)
    env_path = protected_path(Path(pipeline["app_environment_file"]), 0, private=True)
    app_env = parse_application_environment(env_path.read_bytes())
    token = app_env["DAILYCHECK_MCP_TOKEN"]
    for port, credential in ((app_port, None), (mcp_port, token)):
        code, body = _http(port, "/health", credential)
        require(code == 200 and len(body) <= 65536, "direct app/MCP health failed")
    require(_http(mcp_port, "/health")[0] == 401, "direct MCP auth exclusion failed")
    if role not in {"standby", "candidate-standby"}:
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
        since = int(time.time())
        path = "/api/mcp/?" + nonce.decode() + "=1"
        require(_http(8080, path)[0] == 401, "public MCP auth exclusion failed")
        code, body = _http(8080, path, token)
        require(code in {200, 400, 405, 406} and len(body) <= 65536, "public MCP route unhealthy")
        for _ in range(10):
            if nonce in _journal(UNITS[color][1], since):
                break
            time.sleep(0.2)
        else:
            raise ValueError("public MCP route landing unproven")
        require(nonce not in _journal(UNITS[other][1], since), "public MCP reached standby")
    if role == "green":
        row = server.state()["worker_relay"]
        switch = next(s for s in row["journal"] if s["stage"] == "switch")
        require(b"[dry_run] BLOCKED" not in _journal(UNITS["green"][0], switch["started_at"]),
                "live writes blocked")
    require(observer.observe(policy) == processes and _route(config.read_text()) == active,
            "runtime changed during observation")
    verify_data()
    return {"sha": sha, "route": color,
            "identity": digest({"tree": tree, "processes": processes, "database": db_identity,
                                "admin_audit": audit_id})}
