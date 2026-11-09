"""Protected installed H/G inputs; application execution lives in native_pipeline.

Publishers are privileged, external prerequisites. This module cannot turn caller
JSON into a review, observation, or baseline receipt. Missing records deny use.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import stat
import subprocess
import sys
import time
from copy import copy
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from scripts.ci.merge_group import REPOSITORY, REPOSITORY_ID, REPOSITORY_NODE, digest, sha
from scripts.prod_agent.native_queue import NativeQueue, ProducerPolicy, deploy_state

MAX_INPUT = 1024 * 1024
AUTHORITY_FILES = (
    "scripts/prod_agent/native_ports.py", "scripts/prod_agent/native_gate.py",
    "scripts/prod_agent/native_pipeline.py", "scripts/prod_agent/native_prepare.py",
    "scripts/prod_agent/native_controller.py", "scripts/prod_agent/native_queue.py",
    "scripts/prod_agent/github_readiness.py", "scripts/hosting.py",
    "scripts/ci/merge_group.py", "scripts/ci/candidate_binding.py",
    "scripts/ci/pytest_summary.py", "scripts/ci/trusted_producer.py",
    "scripts/check_known_failures.py", "scripts/blue_green/deploy_state.py",
    "scripts/blue_green/publication_authority.py",
)
PERMISSIONS = {"checks": "write", "actions": "read", "contents": "read",
               "pull_requests": "read", "merge_queues": "read", "metadata": "read"}


def runtime_identity():
    """Installed policy, not a candidate claim, approves these executed inputs."""
    import cryptography
    import jwt

    packages = {}
    for module in (jwt, cryptography):
        root = Path(module.__file__).resolve().parent
        files = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted(root.rglob("*")) if p.is_file()
                 and p.suffix in {".py", ".so", ".pyd"}}
        packages[module.__name__] = {"version": module.__version__, "root": str(root),
                                     "files_sha256": digest(files)}
    return {"python_sha256": hashlib.sha256(Path(sys.executable).resolve().read_bytes()).hexdigest(),
            "packages": packages}


@dataclass(frozen=True)
class OwnedFixture:
    """Explicit test constructor capability, never accepted by the installed CLI.

    This permits portable transport/parser tests, not production ownership proof.
    Secrets/records must stay in its owned directory and HTTP on owned loopback.
    """
    root: Path
    endpoint: str

    def __post_init__(self):
        root = self.root.resolve()
        u = urlsplit(self.endpoint)
        if (root == Path("/") or root.stat().st_uid != os.geteuid()
                or (root / ".native-h-fixture").read_text() != "synthetic-owned-fixture\n"
                or u.scheme != "http" or u.hostname != "127.0.0.1" or u.port is None
                or u.username or u.password or u.path or u.query or u.fragment):
            raise ValueError("explicit owned H fixture required")


def protected_path(path: Path, owner: int, *, anchor: Path | None = None,
                   private: bool = False) -> Path:
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("absolute protected path required")
    if anchor is not None and not path.is_relative_to(anchor):
        raise ValueError("record outside owned fixture")
    current = path
    while True:
        info = current.lstat()
        if (stat.S_ISLNK(info.st_mode) or info.st_uid not in {0, owner}
                or info.st_mode & 0o022):
            raise ValueError("unprotected path ownership/mode")
        if current == anchor or current == current.parent:
            break
        current = current.parent
    info = path.stat()
    if info.st_uid != owner or private and info.st_mode & 0o077:
        raise ValueError("private publisher ownership/mode required")
    return path


def read_protected(path: Path, owner: int, *, anchor: Path | None = None,
                   private: bool = True) -> bytes:
    protected_path(path, owner, anchor=anchor, private=private)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner
                or info.st_mode & (0o077 if private else 0o022)
                or info.st_size > MAX_INPUT):
            raise ValueError("invalid protected file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise ValueError("protected input too large")
        return raw
    finally:
        os.close(fd)


class InstalledPolicy:
    def __init__(self, path: Path, *, fixture: OwnedFixture | None = None):
        self.fixture = fixture
        self.owner = os.geteuid() if fixture else 0
        self.anchor = fixture.root.resolve() if fixture else None
        if fixture is None and (os.geteuid() != 0 or not sys.flags.isolated):
            raise ValueError("installed root isolated Python required")
        self.path = Path(path)
        self.raw = read_protected(self.path, self.owner, anchor=self.anchor)
        value = json.loads(self.raw)
        keys = {"version", "repository", "repository_id", "repository_node_id", "producer",
                "authority", "python", "closure", "app_uid", "epoch", "repo", "directory",
                "key_file", "read_token_file", "mcp_health_token_file", "runtime_file", "bootstrap_file",
                "review_directory", "reviewers", "writer", "release_root", "units", "runtime"}
        if (set(value) not in (keys, keys | {"pipeline"}) or value["version"] != 1
                or (value["repository"], value["repository_id"], value["repository_node_id"])
                    != (REPOSITORY, REPOSITORY_ID, REPOSITORY_NODE)
                or set(value["producer"]) != set(ProducerPolicy.__dataclass_fields__)
                or type(value["app_uid"]) is not int or value["app_uid"] <= 0
                or value["app_uid"] == self.owner
                or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", value["epoch"])
                or not isinstance(value["writer"], str) or not value["writer"]
                or set(value["closure"]) != set(AUTHORITY_FILES)):
            raise ValueError("invalid installed H policy")
        self.value = value
        self.producer = ProducerPolicy(**value["producer"])
        if self.producer.producer_manifest_blob_sha is None:
            raise ValueError("approved producer pin required")
        self.authority = Path(value["authority"])
        # Lower-UID pytest reads an immutable root-owned release/control copy.
        # This fixture capability still confines every credential/record to its
        # owned synthetic root and every HTTP request to its owned loopback.
        code_owner = self.authority.stat().st_uid if fixture else 0
        if code_owner not in {0, self.owner}:
            raise ValueError("untrusted fixture code owner")
        for name, expected in value["closure"].items():
            if not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise ValueError("invalid installed code pin")
            raw = read_protected(self.authority / name, code_owner,
                                 anchor=self.authority if fixture else None, private=False)
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError("installed executed closure changed")
            imported = sys.modules.get(name.removesuffix(".py").replace("/", "."))
            if name.startswith("scripts/blue_green/"):
                imported = sys.modules.get(Path(name).stem)
            if imported is not None and Path(imported.__file__).resolve() != self.authority / name:
                raise ValueError("executing import outside approved closure")
        for package in ("scripts", "scripts/prod_agent", "scripts/ci", "scripts/blue_green"):
            init = self.authority / package / "__init__.py"
            if init.exists() and read_protected(init, code_owner,
                    anchor=self.authority if fixture else None, private=False) != b"":
                raise ValueError("unapproved package initializer")
        if (self.authority / "scripts.py").exists():
            raise ValueError("unapproved authority package alias")
        if Path(__file__).resolve() != self.authority / "scripts/prod_agent/native_ports.py":
            raise ValueError("policy does not pin executing authority")
        python_owner = Path(value["python"]).resolve().stat().st_uid if fixture else 0
        if python_owner not in {0, self.owner}:
            raise ValueError("untrusted interpreter owner")
        executable = Path(value["python"])
        protected_path(executable.resolve(), python_owner,
                       anchor=executable.resolve() if fixture else None)
        parent_owner = executable.parent.stat().st_uid if fixture else 0
        if parent_owner not in {0, self.owner}:
            raise ValueError("untrusted interpreter directory owner")
        protected_path(executable.parent, parent_owner,
                       anchor=executable.parent if fixture else None)
        self.python = executable
        if self.python != Path(sys.executable):
            raise ValueError("unapproved executing interpreter")
        if value["runtime"] != runtime_identity():
            raise ValueError("executed crypto/interpreter runtime changed")
        if fixture is None:
            for package in value["runtime"]["packages"].values():
                for path in Path(package["root"]).rglob("*"):
                    if path.is_file() and path.suffix in {".py", ".so", ".pyd"}:
                        protected_path(path, 0)
        for key in ("repo", "directory", "release_root", "review_directory"):
            protected_path(self.location(key), self.owner, anchor=self.anchor, private=key == "directory")
        allowed = {"dailycheck.service", "dailycheck-mcp.service", "dailycheck-green.service",
                   "dailycheck-green-mcp.service"}
        if (not isinstance(value["units"], list) or len(value["units"]) != 2
                or len(set(value["units"])) != 2 or not set(value["units"]) <= allowed
                or sum("mcp" in unit for unit in value["units"]) != 1
                or not isinstance(value["reviewers"], dict) or not value["reviewers"]
                or any(not k.isdigit() or int(k) == value["app_uid"]
                       or fixture is None and int(k) == self.owner
                       or not isinstance(v, str) or not v or v == value["writer"]
                       for k, v in value["reviewers"].items())):
            raise ValueError("invalid protected principal/unit binding")

    @property
    def fingerprint(self):
        return hashlib.sha256(self.raw).hexdigest()

    def location(self, key):
        path = Path(self.value[key])
        if not path.is_absolute() or self.anchor and not path.is_relative_to(self.anchor):
            raise ValueError("invalid installed location")
        return path

    def record(self, key):
        return json.loads(read_protected(self.location(key), self.owner, anchor=self.anchor))

    def read_token(self):
        token = read_protected(self.location("read_token_file"), self.owner,
                               anchor=self.anchor).decode().strip()
        if not token or any(ord(c) < 33 or ord(c) == 127 for c in token):
            raise ValueError("invalid read credential")
        return token

    def mcp_health_token(self):
        path = self.location("mcp_health_token_file")
        if path in {self.location("key_file"), self.location("read_token_file")}:
            raise ValueError("separate MCP health credential required")
        token = read_protected(path, self.owner, anchor=self.anchor).decode("ascii").strip()
        if (not token or any(not 33 <= ord(c) <= 126 for c in token)
                or token == self.read_token()):
            raise ValueError("invalid MCP health credential")
        return token


class GitHubAPI:
    """Bounded, no-redirect API; production target cannot come from environment."""
    def __init__(self, *, fixture: OwnedFixture | None = None):
        self.fixture = fixture
        self.endpoint = fixture.endpoint if fixture else "https://api.github.com"

    def request(self, method, path, token, body=None, *, expected=200):
        if (method not in {"GET", "POST"} or not path.startswith("/")
                or any(c in path for c in ("\r", "\n", "#", ".."))
                or not token or any(ord(c) < 33 or ord(c) == 127 for c in token)):
            raise ValueError("invalid authenticated request")
        target = urlsplit(self.endpoint)
        connection = (http.client.HTTPConnection if self.fixture else http.client.HTTPSConnection)(
            target.hostname, target.port, timeout=20)
        try:
            raw = json.dumps(body).encode() if body is not None else None
            connection.request(method, path, raw, {"Authorization": "Bearer " + token,
                "Accept": "application/vnd.github+json", "Content-Type": "application/json",
                "User-Agent": "dailycheck-protected-h/1", "X-GitHub-Api-Version": "2022-11-28"})
            response = connection.getresponse()
            raw = response.read(MAX_INPUT + 1)
            if response.status != expected or len(raw) > MAX_INPUT:
                raise ValueError("untrusted App HTTP response")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("App object response required")
            return value
        finally:
            connection.close()


class AppSignerIdentity:
    def __init__(self, policy: InstalledPolicy):
        self.policy = policy

    def authenticate(self, key: bytes):
        import jwt

        p = self.policy.producer
        now = int(time.time())
        signed = jwt.encode({"iat": now - 60, "exp": now + 540, "iss": str(p.prod_app_id)},
                            key, algorithm="RS256")
        api = GitHubAPI(fixture=self.policy.fixture)
        app = api.request("GET", "/app", signed)
        install = api.request("GET", f"/app/installations/{p.prod_installation_id}", signed)
        if (app.get("id") != p.prod_app_id or install.get("app_id") != p.prod_app_id
                or install.get("id") != p.prod_installation_id
                or install.get("repository_selection") != "selected"
                or install.get("suspended_at") is not None or install.get("permissions") != PERMISSIONS):
            raise ValueError("unapproved App installation/permissions")
        issued = api.request("POST", f"/app/installations/{p.prod_installation_id}/access_tokens",
                             signed, {"repository_ids": [REPOSITORY_ID], "permissions": PERMISSIONS},
                             expected=201)
        token = issued.get("token")
        from datetime import datetime, timezone

        expiry = datetime.fromisoformat(issued.get("expires_at", "").replace("Z", "+00:00"))
        if (not isinstance(token, str) or issued.get("permissions") != PERMISSIONS
                or expiry.tzinfo is None or not 30 < (expiry - datetime.now(timezone.utc)).total_seconds() <= 3700):
            raise ValueError("invalid scoped installation token")
        repos = api.request("GET", "/installation/repositories?per_page=100", token)
        if (repos.get("total_count") != 1 or len(repos.get("repositories", [])) != 1
                or repos["repositories"][0].get("id") != REPOSITORY_ID
                or repos["repositories"][0].get("full_name") != REPOSITORY):
            raise ValueError("installation repository mismatch")
        return api, token, {"app_id": p.prod_app_id, "installation_id": p.prod_installation_id}

    def read(self):
        key = read_protected(self.policy.location("key_file"), self.policy.owner,
                             anchor=self.policy.anchor)
        _, _, identity = self.authenticate(key)
        return identity


def observed_units(policy, sha_value: str) -> list[str]:
    """Fixed app/MCP pair selected from the actual protected state, never caller JSON."""
    state = deploy_state.load_state(policy.location("directory"))
    active = (state.get("native_queue") or {}).get("active")
    native_runtime = (active and active["candidate"]["manifest"]["group_sha"] == sha_value
                      or any(o.get("sha") == sha_value and o.get("native_evidence_sha256")
                             and o.get("result") == "success" for o in state.get("outcomes", {}).values()))
    if native_runtime:
        return ["dailycheck-green.service", "dailycheck-green-mcp.service"]
    return list(policy.value["units"])


def observe_processes(policy, observer, sha_value: str) -> list[dict]:
    observed = copy(policy)
    observed.value = {**policy.value, "units": observed_units(policy, sha_value)}
    return observer.observe(observed)


def probe_health(policy, role: str, url: str) -> None:
    """Exact fixed loopback health probe; production target cannot come from a caller."""
    u = urlsplit(url)
    ports = {8090, 8091} if role == "app" else {5100, 5101}
    if policy.fixture:
        ports = {urlsplit(policy.fixture.endpoint).port}
    if (u.scheme != "http" or u.hostname != "127.0.0.1" or u.port not in ports
            or u.path != "/health" or u.username or u.password or u.query or u.fragment):
        raise ValueError("invalid fixed runtime probe")
    connection = http.client.HTTPConnection(u.hostname, u.port, timeout=5)
    try:
        headers = {"Authorization": "Bearer " + policy.mcp_health_token()} if role == "mcp" else {}
        connection.request("GET", "/health", headers=headers)
        response = connection.getresponse()
        if response.status != 200 or len(response.read(65537)) > 65536:
            raise ValueError("runtime route unhealthy")
    finally:
        connection.close()


class SystemdObserver:
    def observe(self, policy):
        if sys.platform != "linux":
            raise ValueError("Linux runtime boundary unavailable")
        result = []
        for unit in policy.value["units"]:
            raw = subprocess.check_output(["/usr/bin/systemctl", "show", unit, "--no-pager",
                "--property=MainPID,ActiveState,SubState,WorkingDirectory,ControlGroup,NoNewPrivileges"],
                env={"PATH": "/usr/bin:/bin", "LANG": "C"}, text=True, timeout=10)
            fields = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
            pid = int(fields["MainPID"])
            if fields.get("ActiveState") != "active" or fields.get("SubState") != "running" or pid <= 1:
                raise ValueError("runtime unit not running")
            proc = Path("/proc") / str(pid)
            status = (proc / "status").read_text()
            uids = [int(x) for x in re.search(r"^Uid:\s+(.+)$", status, re.M)[1].split()]
            cwd, exe = (os.readlink(proc / name) for name in ("cwd", "exe"))
            cgroup = (proc / "cgroup").read_text().splitlines()
            start = (proc / "stat").read_text().rsplit(")", 1)[1].split()[19]
            if (set(uids) != {policy.value["app_uid"]} or fields["WorkingDirectory"] != cwd
                    or fields.get("NoNewPrivileges") != "yes"
                    or not re.search(r"^NoNewPrivs:\s+1$", status, re.M)
                    or not re.search(r"^CapEff:\s+0+$", status, re.M)
                    or not re.search(r"^Groups:[ \t]*$", status, re.M)
                    or not any(row.endswith(":" + fields["ControlGroup"]) for row in cgroup)):
                raise ValueError("runtime UID/cgroup/checkout mismatch")
            result.append({"unit": unit, "pid": pid, "uid": uids[0], "cwd": cwd,
                           "exe": exe, "start_ticks": start, "cgroup": fields["ControlGroup"]})
        return result


class ProtectedRuntime:
    def __init__(self, policy, *, observer=None):
        if observer is not None and policy.fixture is None:
            raise ValueError("installed runtime observer cannot be replaced")
        self.policy, self.observer = policy, observer or SystemdObserver()

    def snapshot(self):
        policy = self.policy
        record = policy.record("runtime_file")
        if (set(record) != {"version", "policy", "epoch", "sha", "tree", "processes",
                           "observed_at", "observation_seconds", "incident", "routes"}
                or record["version"] != 1 or record["policy"] != policy.fingerprint
                or record["epoch"] != policy.value["epoch"] or record["incident"] is not False
                or not 0 <= time.time() - record["observed_at"] <= 120
                or type(record["observation_seconds"]) not in {int, float}
                or record["observation_seconds"] < policy.producer.minimum_observation_seconds):
            raise ValueError("protected runtime publisher unavailable/stale")
        sha(record["sha"])
        sha(record["tree"])
        state = deploy_state.load_state(policy.location("directory"))
        active = (state.get("native_queue") or {}).get("active")
        processes = observe_processes(policy, self.observer, record["sha"])
        if processes != record["processes"] or len(processes) != 2:
            raise ValueError("runtime process identity changed")
        release = policy.location("release_root") / record["sha"]
        protected_path(release, policy.owner, anchor=policy.anchor)
        for path in release.rglob("*"):
            protected_path(path, policy.owner, anchor=policy.anchor)
        build = json.loads(read_protected(release / "release.json", policy.owner,
                                         anchor=policy.anchor, private=False))
        if build != {"sha": record["sha"], "tree": record["tree"]} or any(
                row["cwd"] != str(release) or row["uid"] != policy.value["app_uid"] for row in processes):
            raise ValueError("immutable runtime release mismatch")
        if set(record["routes"]) != {"app", "mcp"}:
            raise ValueError("protected route observations required")
        for role, url in record["routes"].items():
            probe_health(policy, role, url)
        if observe_processes(policy, self.observer, record["sha"]) != processes:
            raise ValueError("runtime drift during collection")
        if active and active["candidate"]["manifest"]["group_sha"] == record["sha"]:
            journal = active["journal"]
            if (len(journal) != len(deploy_state.NATIVE_STAGES)
                    or [row["stage"] for row in journal] != list(deploy_state.NATIVE_STAGES)
                    or any(row.get("completed") is not True or row.get("rc") != 0 for row in journal)
                    or journal[-1]["completed_monotonic"] - journal[-1]["started_monotonic"]
                        < policy.producer.minimum_observation_seconds
                    or record["observed_at"] < journal[-1]["completed_at"]
                    or record["tree"] != active["candidate"]["manifest"]["tree_sha"]):
                raise ValueError("G runtime publisher is not bound to completed actual stages")
        return record

    def read(self):
        record = self.snapshot()
        return {"sha": record["sha"], "incident": False, "observation_complete": True}


class ProtectedReview:
    def __init__(self, policy, *, fixture_ci=None):
        if fixture_ci is not None and policy.fixture is None:
            raise ValueError("installed review CI cannot be replaced")
        self.policy, self.fixture_ci, self.facts = policy, fixture_ci, None

    def read(self, candidate):
        policy = self.policy
        if self.fixture_ci is not None:
            ci = self.fixture_ci(candidate)
        else:
            if self.facts is None:
                raise ValueError("authenticated independent review CI publisher unavailable")
            from scripts.prod_agent.github_readiness import validate_native_ci

            manifest = candidate["manifest"]
            runs = [self.facts._run("G", manifest["group_sha"], None)]
            runs += [self.facts._run("H", m["head_sha"], m["number"]) for m in manifest["members"]]
            ci = validate_native_ci(policy.location("repo"), manifest, policy.producer, runs)
        records = []
        for uid, principal in policy.value["reviewers"].items():
            path = policy.location("review_directory") / uid / (digest(candidate) + ".json")
            if not path.exists():
                continue
            record = json.loads(read_protected(path, int(uid), anchor=policy.anchor))
            expected = {"version": 1, "candidate_sha256": digest(candidate),
                        "policy": policy.fingerprint, "epoch": policy.value["epoch"],
                        "reviewer": principal, "writer": policy.value["writer"],
                        "role": "independent_prod", "verdict": "pass", "ci_sha256": digest(ci)}
            if record != expected:
                raise ValueError("independent protected review mismatch")
            records.append({k: record[k] for k in ("candidate_sha256", "reviewer", "writer", "role", "verdict")})
            records[-1]["record_sha256"] = digest(record)
        if len(records) != 1:
            raise ValueError("independent review publisher unavailable/ambiguous")
        return records[0]


class ProtectedInitializer:
    def __init__(self, policy, runtime, client):
        self.policy, self.runtime, self.client = policy, runtime, client

    def initialize(self):
        policy = self.policy
        directory = policy.location("directory")
        runner = NativeQueue(policy.location("repo"), directory, policy.producer, None)
        with runner.fence(), deploy_state.state_transaction(directory):
            state = deploy_state.load_state(directory)
            if not deploy_state.native_consistent(directory, state):
                raise ValueError("partial native initialization; operator reconciliation required")
            expected_keys = {"version", "policy", "epoch", "source_sha", "public_sha", "tree_sha",
                             "map_sha256", "source_outcome_sha256", "runtime_sha256", "observed_at"}
            runtime = self.runtime.snapshot()
            main = self.client.get(f"/repos/{REPOSITORY}/branches/main")["commit"]["sha"]
            if "native_queue" in state:
                bootstrap = state.get("native_bootstrap")
                anchor = state.get("native_source_anchor")
                if (not isinstance(bootstrap, dict) or set(bootstrap) != expected_keys
                        or bootstrap["version"] != 1 or bootstrap["policy"] != policy.fingerprint
                        or bootstrap["epoch"] != policy.value["epoch"]
                        or bootstrap["public_sha"] != main or runtime["sha"] != main
                        or bootstrap["tree_sha"] != runtime["tree"]
                        or not isinstance(anchor, dict) or anchor.get("sha") != bootstrap["source_sha"]
                        or digest(anchor) != bootstrap["source_outcome_sha256"]
                        or type(bootstrap["observed_at"]) not in {int, float}
                        or not 0 <= bootstrap["observed_at"] <= time.time()
                        or state.get("lock") is not None or state["native_queue"]["active"] is not None
                        or not isinstance(state.get("deployed"), dict)
                        or (state.get("deployed") or {}).get("sha") != main
                        or any(not isinstance(bootstrap[k], str)
                               or not re.fullmatch(r"[0-9a-f]{64}", bootstrap[k])
                               for k in ("map_sha256", "source_outcome_sha256", "runtime_sha256"))):
                    raise ValueError("different/active initialized epoch")
                sha(bootstrap["source_sha"])
                sha(bootstrap["public_sha"])
                sha(bootstrap["tree_sha"])
                return state["native_queue"]["initialization_id"]
            if "native_bootstrap" in state or "native_source_anchor" in state:
                raise ValueError("partial native initialization; operator reconciliation required")
            receipt = policy.record("bootstrap_file")
            if (set(receipt) != expected_keys or receipt["version"] != 1
                    or receipt["policy"] != policy.fingerprint or receipt["epoch"] != policy.value["epoch"]
                    or receipt["public_sha"] != main or runtime["sha"] != main
                    or runtime["tree"] != receipt["tree_sha"] or receipt["runtime_sha256"] != digest(runtime)
                    or not 0 <= time.time() - receipt["observed_at"] <= 120
                    or not re.fullmatch(r"[0-9a-f]{64}", receipt["map_sha256"])):
                raise ValueError("fresh protected baseline transition required")
            sha(receipt["source_sha"])
            sha(receipt["public_sha"])
            sha(receipt["tree_sha"])
            if (state.get("lock") is not None or not isinstance(state.get("deployed"), dict)
                    or state["deployed"].get("sha") != receipt["source_sha"]
                    or digest(state["deployed"]) != receipt["source_outcome_sha256"]):
                raise ValueError("old deployment/writer not reconciled")
            initialization = os.urandom(16).hex()
            deploy_state.native_guard(directory, {"version": 1, "initialization_id": initialization,
                                                   "intent_id": None})
            state["native_bootstrap"] = receipt
            state["native_source_anchor"] = dict(state["deployed"])
            state["deployed"] = {**state["deployed"], "sha": main}
            state["native_queue"] = {"version": 1, "initialization_id": initialization,
                                     "active": None, "last_reconciled": None}
            deploy_state.save_state(directory, state)
            return initialization
