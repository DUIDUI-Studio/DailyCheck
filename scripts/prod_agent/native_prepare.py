"""Disabled protected native preparation and record publishers.

This module supplies the executable prerequisites the H/G consumers in
``native_ports`` require: an initial immutable baseline release, fixed baseline
unit recipes, a fixed isolated launcher, independent runtime and baseline
records, and an independent review request/result boundary. It never installs,
provisions, enables a service, signs an App request or changes repository/state
anchors. Every protected operation reads fixed protected records; caller JSON
cannot authorize a live publication.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from scripts.ci.merge_group import REPOSITORY, digest, sha
from scripts.hosting import AccessRole, HostingClient, HostingConfig, Provider
from scripts.prod_agent.native_pipeline import literal_tree, render_unit, write_literal_release
from scripts.prod_agent.native_ports import (
    InstalledPolicy,
    ProtectedRuntime,
    observe_processes,
    probe_health,
    protected_path,
    read_protected,
)
from scripts.prod_agent.native_queue import NativeQueue, deploy_state

POLICY_PATH = Path("/etc/dailycheck-native/policy.json")
RECORDS_ROOT = Path("/var/lib/dailycheck-native-records")
REVIEW_ROOT = Path("/var/lib/dailycheck-native-reviews")
LAUNCHER_PATH = Path("/usr/local/sbin/dailycheck-native")
UNIT_NAMES = ("dailycheck.service", "dailycheck-mcp.service",
              "dailycheck-green.service", "dailycheck-green-mcp.service")
GREEN_UNITS = {"dailycheck-green.service", "dailycheck-green-mcp.service"}
PROD_APP_ID = 5674104
PROD_INSTALLATION_ID = 180015273
MAX_INPUT = 1024 * 1024
CANONICAL_SHA40 = re.compile(r"[0-9a-f]{40}")
CANONICAL_SHA64 = re.compile(r"[0-9a-f]{64}")
EPOCH = re.compile(r"[a-zA-Z0-9_-]{1,80}")
UNRESOLVED = re.compile(r"REQUIRED_(?:UNOBSERVED|LATER|)")
ROUTE_PORTS = {"baseline": (8090, 5100), "green": (8091, 5101)}
RECORD_PLAN = "preparation-plan.json"
RECORD_EVIDENCE = "baseline-evidence.json"
RECORD_MAP = "source-public-map.json"
RECORD_LEDGER = "observation-ledger.json"
CONTROLLER_OPERATIONS = ("status", "initialize", "admit-head", "deploy-front", "recover",
                         "grant", "release")
INITIALIZED_KEYS = {"version", "policy", "epoch", "source_sha", "public_sha", "tree_sha",
                    "map_sha256", "source_outcome_sha256", "runtime_sha256", "observed_at"}


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or UNRESOLVED.search(value):
        raise ValueError(f"unresolved or invalid {name}")
    return value


def _sha40(value: object, name: str) -> str:
    if not isinstance(value, str) or not CANONICAL_SHA40.fullmatch(value):
        raise ValueError(f"invalid {name}")
    return value


def _sha64(value: object, name: str) -> str:
    if not isinstance(value, str) or not CANONICAL_SHA64.fullmatch(value):
        raise ValueError(f"invalid {name}")
    return value


def _positive(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"invalid {name}")
    return value


def _absolute(value: object, name: str) -> Path:
    path = Path(_text(value, name))
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"invalid {name}")
    return path


@dataclass(frozen=True)
class BaselineIdentity:
    source_sha: str
    source_outcome_sha256: str
    public_sha: str
    tree_sha: str
    map_sha256: str
    map_row: dict

    @classmethod
    def from_dict(cls, value: object) -> BaselineIdentity:
        keys = {"source_sha", "source_outcome_sha256", "public_sha", "tree_sha",
                "map_sha256", "map_row"}
        if not isinstance(value, dict) or set(value) != keys:
            raise ValueError("invalid baseline identity")
        row = value["map_row"]
        if not isinstance(row, dict) or set(row) != {"source_sha", "public_sha", "tree_sha"}:
            raise ValueError("invalid baseline map row")
        identity = cls(
            _sha40(value["source_sha"], "baseline source"),
            _sha64(value["source_outcome_sha256"], "baseline source outcome"),
            _sha40(value["public_sha"], "baseline public"),
            _sha40(value["tree_sha"], "baseline tree"),
            _sha64(value["map_sha256"], "baseline map"),
            {"source_sha": _sha40(row["source_sha"], "map source"),
             "public_sha": _sha40(row["public_sha"], "map public"),
             "tree_sha": _sha40(row["tree_sha"], "map tree")},
        )
        expected = {"source_sha": identity.source_sha, "public_sha": identity.public_sha,
                    "tree_sha": identity.tree_sha}
        if identity.map_row != expected:
            raise ValueError("baseline map row does not match identity")
        return identity


@dataclass(frozen=True)
class PreparationPlan:
    """Approved inert facts; unresolved runtime/epoch/principal values fail execution."""

    source_pin: str
    source_manifest: dict[str, str]
    authority: Path
    python: Path
    app_python: Path
    prod_app_id: int
    prod_installation_id: int
    app_uid: int
    app_gid: int
    reviewer_uid: int
    reviewer_gid: int
    reviewer: str
    writer: str
    epoch: str
    scope_approval: str
    baseline: BaselineIdentity
    source_db: Path
    source_db_device: int
    source_db_inode: int
    green_db: Path
    blue_access_log: Path
    green_access_log: Path
    app_environment_file: Path
    unit_directory: Path
    release_root: Path
    records_directory: Path
    review_directory: Path
    private_paths: tuple[Path, ...]

    @classmethod
    def from_dict(cls, value: object) -> PreparationPlan:
        keys = {"version", "source_pin", "source_manifest", "authority", "python",
                "app_python", "prod_app_id", "prod_installation_id", "app_uid", "app_gid",
                "reviewer_uid", "reviewer_gid", "reviewer", "writer", "epoch",
                "scope_approval", "baseline", "paths"}
        if not isinstance(value, dict) or set(value) != keys or value["version"] != 1:
            raise ValueError("invalid preparation plan")
        manifest = value["source_manifest"]
        if not isinstance(manifest, dict) or not manifest:
            raise ValueError("invalid preparation source manifest")
        parsed_manifest = {}
        for name, pin in manifest.items():
            relative = Path(_text(name, "manifest path"))
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("invalid preparation manifest path")
            parsed_manifest[str(relative)] = _sha64(pin, "manifest pin")
        if "scripts/prod_agent/native_prepare.py" not in parsed_manifest:
            raise ValueError("source manifest must pin the adapter")
        source_pin = _sha40(value["source_pin"], "source pin")
        authority = _absolute(value["authority"], "authority")
        if authority.name != source_pin:
            raise ValueError("authority must be the pinned source directory")
        python = _absolute(value["python"], "python")
        app_python = _absolute(value["app_python"], "app python")
        if str(python).startswith("/usr/bin/python"):
            raise ValueError("system Python cannot be the protected runtime")
        if app_python == python:
            raise ValueError("separate application runtime required")
        app_uid = _positive(value["app_uid"], "app uid")
        app_gid = _positive(value["app_gid"], "app gid")
        reviewer_uid = _positive(value["reviewer_uid"], "reviewer uid")
        reviewer_gid = _positive(value["reviewer_gid"], "reviewer gid")
        if reviewer_uid == app_uid:
            raise ValueError("reviewer must differ from the application identity")
        prod_app_id = _positive(value["prod_app_id"], "prod app id")
        prod_installation_id = _positive(value["prod_installation_id"], "prod installation id")
        if (prod_app_id, prod_installation_id) != (PROD_APP_ID, PROD_INSTALLATION_ID):
            raise ValueError("unapproved production App installation")
        epoch = _text(value["epoch"], "epoch")
        if not EPOCH.fullmatch(epoch):
            raise ValueError("invalid authority epoch")
        reviewer = _text(value["reviewer"], "reviewer")
        writer = _text(value["writer"], "writer")
        if reviewer == writer:
            raise ValueError("reviewer must differ from the original writer")
        paths = value["paths"]
        path_keys = {"source_db", "source_db_device", "source_db_inode", "green_db",
                     "blue_access_log", "green_access_log", "app_environment_file",
                     "unit_directory", "release_root", "records_directory",
                     "review_directory", "private_paths"}
        if not isinstance(paths, dict) or set(paths) != path_keys:
            raise ValueError("invalid preparation paths")
        source_db = _absolute(paths["source_db"], "source db")
        if source_db == Path("/root") or source_db.is_relative_to("/root"):
            raise ValueError("production DB must be an approved bind mount outside /root")
        green_db = _absolute(paths["green_db"], "green db")
        if green_db == source_db or green_db.is_relative_to(source_db):
            raise ValueError("green DB must be separate from production")
        private = paths["private_paths"]
        if not isinstance(private, list) or not private:
            raise ValueError("private control exclusions required")
        private_paths = tuple(_absolute(item, "private path") for item in private)
        release_root = _absolute(paths["release_root"], "release root")
        records_directory = _absolute(paths["records_directory"], "records directory")
        review_directory = _absolute(paths["review_directory"], "review directory")
        if len({release_root, records_directory, review_directory, source_db, green_db}) != 5:
            raise ValueError("preparation paths overlap")
        return cls(
            source_pin=source_pin, source_manifest=parsed_manifest, authority=authority,
            python=python, app_python=app_python, prod_app_id=prod_app_id,
            prod_installation_id=prod_installation_id, app_uid=app_uid, app_gid=app_gid,
            reviewer_uid=reviewer_uid, reviewer_gid=reviewer_gid,
            reviewer=reviewer,
            writer=writer,
            epoch=epoch, scope_approval=_text(value["scope_approval"], "scope approval"),
            baseline=BaselineIdentity.from_dict(value["baseline"]),
            source_db=source_db, source_db_device=_positive(paths["source_db_device"], "source db device"),
            source_db_inode=_positive(paths["source_db_inode"], "source db inode"),
            green_db=green_db,
            blue_access_log=_absolute(paths["blue_access_log"], "blue access log"),
            green_access_log=_absolute(paths["green_access_log"], "green access log"),
            app_environment_file=_absolute(paths["app_environment_file"], "application environment"),
            unit_directory=_absolute(paths["unit_directory"], "unit directory"),
            release_root=release_root, records_directory=records_directory,
            review_directory=review_directory, private_paths=private_paths,
        )


@dataclass(frozen=True)
class BaselineEvidence:
    source_sha: str
    source_outcome_sha256: str
    public_sha: str
    tree_sha: str
    map_sha256: str
    map_row: dict
    blob_manifest_sha256: str
    exclusions: tuple[str, ...]
    continuity: dict
    observed_at: float

    @classmethod
    def from_dict(cls, value: object) -> BaselineEvidence:
        keys = {"version", "source_sha", "source_outcome_sha256", "public_sha", "tree_sha",
                "map_sha256", "map_row", "blob_manifest_sha256", "exclusions",
                "continuity", "observed_at"}
        if not isinstance(value, dict) or set(value) != keys or value["version"] != 1:
            raise ValueError("invalid baseline evidence")
        row = value["map_row"]
        if not isinstance(row, dict) or set(row) != {"source_sha", "public_sha", "tree_sha"}:
            raise ValueError("invalid baseline evidence map row")
        identity = BaselineIdentity.from_dict({key: value[key] for key in (
            "source_sha", "source_outcome_sha256", "public_sha", "tree_sha", "map_sha256",
            "map_row")})
        exclusions = value["exclusions"]
        if (not isinstance(exclusions, list)
                or any(not isinstance(item, str) or not item for item in exclusions)):
            raise ValueError("invalid baseline exclusions")
        continuity = value["continuity"]
        if not isinstance(continuity, dict) or not continuity:
            raise ValueError("baseline continuity evidence required")
        observed_at = value["observed_at"]
        if type(observed_at) not in {int, float} or observed_at < 0:
            raise ValueError("invalid baseline observation time")
        return cls(
            source_sha=identity.source_sha,
            source_outcome_sha256=identity.source_outcome_sha256,
            public_sha=identity.public_sha, tree_sha=identity.tree_sha,
            map_sha256=identity.map_sha256, map_row=identity.map_row,
            blob_manifest_sha256=_sha64(value["blob_manifest_sha256"], "baseline blob manifest"),
            exclusions=tuple(exclusions), continuity=dict(continuity),
            observed_at=float(observed_at),
        )


def _launcher_template(root: Path | None = None) -> str:
    base = Path(__file__).resolve().parents[2] if root is None else Path(root)
    path = base / "deploy/native/dailycheck-native.in"
    raw = path.read_text()
    if raw.count("@PYTHON@") != 1 or raw.count("@AUTHORITY@") != 1:
        raise ValueError("invalid launcher template")
    return raw


def render_launcher(plan: PreparationPlan) -> str:
    return _launcher_template().replace(
        "@PYTHON@", str(plan.python)).replace("@AUTHORITY@", str(plan.authority))


def _stat_check(plan: PreparationPlan) -> str:
    return (f"{plan.python} -I -c \"import os,sys; s=os.stat(sys.argv[1]); "
            "sys.exit(0 if (s.st_dev,s.st_ino)==(int(sys.argv[2]),int(sys.argv[3])) else 1)\" "
            f"{plan.source_db} {plan.source_db_device} {plan.source_db_inode}")


def _baseline_units(plan: PreparationPlan) -> list[dict]:
    release = str(plan.release_root / plan.baseline.public_sha)
    private = " ".join(str(path) for path in plan.private_paths)
    recipes = (
        ("dailycheck.service", "DailyCheck production app (protected baseline)",
         8090, plan.source_db, "false", plan.blue_access_log, True, False),
        ("dailycheck-mcp.service", "DailyCheck production MCP (protected baseline)",
         5100, plan.source_db, "false", None, False, False),
        ("dailycheck-green.service", "DailyCheck green app (protected baseline)",
         8091, plan.green_db, "true", plan.green_access_log, True, True),
        ("dailycheck-green-mcp.service", "DailyCheck green MCP (protected baseline)",
         5101, plan.green_db, "true", None, False, True),
    )
    units = []
    for name, description, port, database, dry_run, access_log, is_app, green in recipes:
        module = (f"gunicorn --workers 2 --bind 127.0.0.1:{port} "
                  f"--access-logfile {access_log} app:create_app()") if is_app else (
            f"mcp_server --transport http --host 127.0.0.1 --port {port}")
        # The blue app and MCP units must both prove the approved DB mount before
        # any candidate import. Green units are dry-run copies and have no mount.
        content = render_unit(
            description=description, working_directory=release,
            environment_file=str(plan.app_environment_file),
            environment_lines=[("PATH", "/usr/bin:/bin"), ("PYTHONDONTWRITEBYTECODE", "1"),
                               ("DAILYCHECK_ENV", "production"), ("DRY_RUN", dry_run),
                               ("DAILYCHECK_DB_DIR", str(database))],
            read_write_paths=str(database) + (f" {access_log}" if access_log else ""),
            inaccessible_paths=private, module=module, python=str(plan.app_python),
            app_uid=plan.app_uid, app_gid=plan.app_gid,
            exec_start_pre=() if green else (_stat_check(plan),),
            unit_conditions=() if green else (f"ConditionPathIsMountPoint={plan.source_db}",),
        )
        units.append({"name": name, "path": str(plan.unit_directory / name),
                      "mode": "0644", "sha256": hashlib.sha256(content.encode()).hexdigest(),
                      "content": content})
    return units


def render_preparation(plan: PreparationPlan) -> dict:
    """Render inert manifests only; nothing on the host is created or changed."""
    launcher = render_launcher(plan)
    units = _baseline_units(plan)
    generated = {str(LAUNCHER_PATH): {"mode": "0755",
                                      "sha256": hashlib.sha256(launcher.encode()).hexdigest()}}
    for row in units:
        generated[row["path"]] = {"mode": row["mode"], "sha256": row["sha256"]}
    return {
        "version": 1,
        "status": "PREPARATION_ONLY_INERT",
        "source_pin": plan.source_pin,
        "source_manifest": dict(sorted(plan.source_manifest.items())),
        "scope_approval": plan.scope_approval,
        "launcher": {"path": str(LAUNCHER_PATH), "mode": "0755",
                     "sha256": generated[str(LAUNCHER_PATH)]["sha256"], "content": launcher},
        "units": units,
        "dropins_remove": [
            str(plan.unit_directory / "dailycheck.service.d" / "10-app-env.conf"),
            str(plan.unit_directory / "dailycheck-green.service.d" / "10-app-env.conf"),
        ],
        "green_conf_forbidden": [str(plan.unit_directory / "dailycheck-green.service.d" / "*.conf")],
        "generated_manifest": generated,
        "before_after": [
            {"target": row["path"], "before": "approved existing fragment/drop-in",
             "after_sha256": row["sha256"]} for row in units
        ] + [
            {"target": path, "before": "observed app-env drop-in",
             "after": "removed; folded into the complete unit"}
            for path in (
                str(plan.unit_directory / "dailycheck.service.d" / "10-app-env.conf"),
                str(plan.unit_directory / "dailycheck-green.service.d" / "10-app-env.conf"),
            )
        ],
        "state_mutation": False,
    }


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _ensure_directory(path: Path, owner: int, *, anchor: Path | None, mode: int,
                      group: int | None = None) -> Path:
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("absolute publication directory required")
    if anchor is not None and not path.is_relative_to(anchor):
        raise ValueError("publication directory outside owned anchor")
    if path.exists():
        info = path.lstat()
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != owner
                or os.geteuid() == 0 and group is not None and info.st_gid != group
                or info.st_mode & (0o022 if group is None else 0o002)):
            raise ValueError("unsafe publication directory")
        return path
    parent = path.parent
    info = parent.lstat()
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
            or info.st_uid not in {0, owner} or info.st_mode & 0o022):
        raise ValueError("unsafe publication parent")
    os.mkdir(path, mode)
    if os.geteuid() == 0:
        os.chown(path, owner, group if group is not None else -1)
    os.chmod(path, mode)
    return path


def _atomic_write(path: Path, raw: bytes, *, owner: int, mode: int,
                  group: int | None = None, anchor: Path | None = None) -> None:
    """One replace-only publication; a crash leaves the destination untouched."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("absolute publication path required")
    if anchor is not None and not path.is_relative_to(anchor):
        raise ValueError("publication path outside owned anchor")
    _ensure_directory(path.parent, owner, anchor=anchor, mode=0o700)
    if path.is_symlink():
        raise ValueError("symlinked publication destination")
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".tmp-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        if os.geteuid() == 0:
            os.fchown(fd, owner, group if group is not None else -1)
        info = os.fstat(fd)
        if (info.st_uid != owner
                or os.geteuid() == 0 and group is not None and info.st_gid != group):
            raise ValueError("publication ownership mismatch")
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
        _fsync_directory(path.parent)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _reject_fence() -> None:
    if deploy_state.process_lock_fd() is not None:
        raise ValueError("observer must not inherit the attempt fence")


class _FixtureProcessObserver:
    """TEST ONLY: replay an owned synthetic process list, never a kernel claim.

    The installed path cannot construct this class (``NativePreparer`` rejects a
    replaced observer when ``policy.fixture is None``). The list lives in a
    0600 file inside the owned fixture root and cannot authorize a live record.
    """

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.path = self.root / "fixture-processes.json"

    def observe(self, _policy) -> list[dict]:
        raw = read_protected(self.path, os.geteuid(), anchor=self.root)
        value = json.loads(raw)
        if not isinstance(value, list) or len(value) != 2:
            raise ValueError("invalid owned fixture process list")
        return value


class NativePreparer:
    """Fixed protected operations; installed mode cannot replace observers/clients."""

    def __init__(self, policy: InstalledPolicy, *, records: Path | None = None,
                 repo: Path | None = None, client=None, observer=None,
                 clock=time.time, monotonic=time.monotonic, sleep=time.sleep):
        if policy.fixture is None and any(x is not None for x in (records, repo, client, observer)):
            raise ValueError("installed preparation inputs cannot be replaced")
        self.policy = policy
        self.records = (Path(records) if records is not None else
                        policy.fixture.root.resolve() / "records" if policy.fixture
                        else RECORDS_ROOT)
        self.repo = Path(repo) if repo is not None else policy.location("repo")
        self._client = client
        if observer is None:
            if policy.fixture is not None:
                observer = _FixtureProcessObserver(policy.fixture.root)
            else:
                from scripts.prod_agent.native_ports import SystemdObserver
                observer = SystemdObserver()
        self.observer = observer
        self.clock, self.monotonic, self.sleep = clock, monotonic, sleep

    @property
    def client(self):
        """Read credentials only for the operations that need remote main."""
        if self._client is None:
            if self.policy.fixture is not None:
                # TEST ONLY: the owned fixture supplies a curl shim for its
                # loopback server; never fall back to a real network target.
                shim = self.policy.fixture.root.resolve() / "curl"
                if not shim.is_file():
                    raise ValueError("owned fixture read transport required")
                curl = str(shim)
            else:
                curl = "/usr/bin/curl"
            self._client = HostingClient(HostingConfig(
                Provider.GITHUB, "https://api.github.com", REPOSITORY, AccessRole.READ,
                self.policy.read_token(), actions_app_id=self.policy.producer.actions_app_id,
                curl=curl))
        return self._client

    def _record(self, name: str) -> dict:
        return json.loads(read_protected(self.records / name, self.policy.owner,
                                         anchor=self.policy.anchor))

    def _write_record(self, path: Path, value: object) -> None:
        _atomic_write(Path(path), json.dumps(value).encode(), owner=self.policy.owner,
                      mode=0o600, anchor=self.policy.anchor)

    def plan(self) -> PreparationPlan:
        plan = PreparationPlan.from_dict(self._record(RECORD_PLAN))
        policy = self.policy
        if (plan.release_root != policy.location("release_root")
                or plan.review_directory != policy.location("review_directory")
                or plan.records_directory != self.records
                or plan.epoch != policy.value["epoch"]
                or plan.app_uid != policy.value["app_uid"]
                or plan.prod_app_id != policy.producer.prod_app_id
                or plan.prod_installation_id != policy.producer.prod_installation_id
                or plan.reviewer != policy.value["reviewers"].get(str(plan.reviewer_uid))
                or plan.writer != policy.value["writer"]):
            raise ValueError("preparation plan does not match installed policy")
        return plan

    # ── initial immutable baseline export ─────────────────────────────────

    def _validate_map(self, document: object, evidence: BaselineEvidence) -> dict:
        if (not isinstance(document, dict) or set(document) != {"version", "rows"}
                or document["version"] != 1 or not isinstance(document["rows"], list)):
            raise ValueError("invalid approved source/public map")
        rows = []
        for row in document["rows"]:
            if not isinstance(row, dict) or set(row) != {"source_sha", "public_sha", "tree_sha"}:
                raise ValueError("invalid approved map row")
            rows.append({"source_sha": _sha40(row["source_sha"], "map source"),
                         "public_sha": _sha40(row["public_sha"], "map public"),
                         "tree_sha": _sha40(row["tree_sha"], "map tree")})
        if digest(document) != evidence.map_sha256:
            raise ValueError("approved source/public map changed")
        matches = [row for row in rows if row["source_sha"] == evidence.source_sha]
        if len(matches) != 1 or matches[0] != evidence.map_row:
            raise ValueError("approved map row missing or ambiguous")
        return matches[0]

    def _validate_uninitialized(self, state: dict, evidence: BaselineEvidence) -> dict:
        directory = self.policy.location("directory")
        if not deploy_state.native_consistent(directory, state):
            raise ValueError("native state/guard mismatch; operator reconciliation required")
        if state.get("lock") is not None:
            raise ValueError("legacy writer drain not proved")
        if any(key in state for key in ("native_queue", "native_bootstrap", "native_source_anchor")):
            raise ValueError("baseline operation requires uninitialized native state")
        deployed = state.get("deployed")
        if (not isinstance(deployed, dict) or digest(deployed) != evidence.source_outcome_sha256
                or deployed.get("sha") != evidence.source_sha):
            raise ValueError("original successful source outcome changed")
        return deployed

    def _baseline_outcome(self, state: dict, evidence: BaselineEvidence) -> tuple[dict, bool]:
        """Return the preserved original source outcome and whether native is initialized.

        Initial export/transition remain uninitialized-only. A runtime refresh is
        also allowed after the unchanged initializer completed on the same P, but
        only while idle and while every preserved identity fact still matches.
        """
        directory = self.policy.location("directory")
        if not deploy_state.native_consistent(directory, state):
            raise ValueError("native state/guard mismatch; operator reconciliation required")
        if state.get("lock") is not None:
            raise ValueError("legacy writer drain not proved")
        queue = state.get("native_queue")
        if queue is None:
            if "native_bootstrap" in state or "native_source_anchor" in state:
                raise ValueError("partial native initialization; operator reconciliation required")
            deployed = state.get("deployed")
            if (not isinstance(deployed, dict)
                    or digest(deployed) != evidence.source_outcome_sha256
                    or deployed.get("sha") != evidence.source_sha):
                raise ValueError("original successful source outcome changed")
            return deployed, False
        if (not isinstance(queue, dict) or queue.get("version") != 1
                or queue.get("active") is not None):
            raise ValueError("baseline refresh requires an idle initialized native state")
        bootstrap = state.get("native_bootstrap")
        anchor = state.get("native_source_anchor")
        deployed = state.get("deployed")
        if (not isinstance(bootstrap, dict) or set(bootstrap) != INITIALIZED_KEYS
                or bootstrap["version"] != 1
                or bootstrap["policy"] != self.policy.fingerprint
                or bootstrap["epoch"] != self.policy.value["epoch"]
                or bootstrap["source_sha"] != evidence.source_sha
                or bootstrap["public_sha"] != evidence.public_sha
                or bootstrap["tree_sha"] != evidence.tree_sha
                or bootstrap["source_outcome_sha256"] != evidence.source_outcome_sha256
                or bootstrap["map_sha256"] != evidence.map_sha256
                or not isinstance(anchor, dict)
                or anchor.get("sha") != bootstrap["source_sha"]
                or digest(anchor) != bootstrap["source_outcome_sha256"]
                or not isinstance(deployed, dict)
                or deployed.get("sha") != bootstrap["public_sha"]):
            raise ValueError("initialized baseline identity mismatch")
        _sha64(bootstrap["runtime_sha256"], "initialized runtime digest")
        if type(bootstrap["observed_at"]) not in {int, float}:
            raise ValueError("initialized baseline identity mismatch")
        return anchor, True

    def export_baseline(self) -> dict:
        """Create or verify the immutable literal P release under the original fence."""
        if deploy_state.process_lock_fd() is None:
            raise ValueError("original attempt fence required")
        evidence = BaselineEvidence.from_dict(self._record(RECORD_EVIDENCE))
        document = self._record(RECORD_MAP)
        row = self._validate_map(document, evidence)
        state = deploy_state.load_state(self.policy.location("directory"))
        self._validate_uninitialized(state, evidence)
        tree, entries = literal_tree(self.repo, evidence.public_sha)
        if tree != evidence.tree_sha or tree != row["tree_sha"]:
            raise ValueError("public P tree changed")
        if digest(entries) != evidence.blob_manifest_sha256:
            raise ValueError("retained literal blob identity changed")
        for exclusion in evidence.exclusions:
            prefix = exclusion.rstrip("/") + "/"
            if any(item["path"] == exclusion or item["path"].startswith(prefix)
                   for item in entries):
                raise ValueError("excluded path present in baseline release")
        release = self.policy.location("release_root") / evidence.public_sha
        created = write_literal_release(self.repo, evidence.public_sha, tree, release,
                                        entries, existing="verify")
        return {"result": "created" if created else "verified",
                "release": str(release), "sha": evidence.public_sha, "tree": tree,
                "manifest_sha256": evidence.blob_manifest_sha256,
                "state_mutation": False}

    # ── independent runtime publication ───────────────────────────────────

    def default_routes(self, kind: str) -> dict:
        if self.policy.fixture is not None:
            endpoint = self.policy.fixture.endpoint
            return {"app": endpoint + "/health", "mcp": endpoint + "/health"}
        app_port, mcp_port = ROUTE_PORTS[kind]
        return {"app": f"http://127.0.0.1:{app_port}/health",
                "mcp": f"http://127.0.0.1:{mcp_port}/health"}

    def _probe(self, routes: object) -> dict:
        if not isinstance(routes, dict) or set(routes) != {"app", "mcp"}:
            raise ValueError("protected route observations required")
        for role, url in routes.items():
            probe_health(self.policy, role, url)
        return dict(routes)

    def _validate_ledger(self, ledger: object, kind: str) -> dict:
        shared = {"version", "policy", "epoch", "kind", "sha", "tree", "processes",
                  "routes", "started_at", "finished_at", "started_monotonic",
                  "finished_monotonic", "observation_seconds", "samples", "incident"}
        outcome_key = "source_outcome_sha256" if kind == "baseline" else "journal_sha256"
        if (not isinstance(ledger, dict) or set(ledger) != shared | {outcome_key}
                or ledger["version"] != 1 or ledger["policy"] != self.policy.fingerprint
                or ledger["epoch"] != self.policy.value["epoch"] or ledger["kind"] != kind
                or ledger["incident"] is not False):
            raise ValueError("invalid protected observation ledger")
        sha(ledger["sha"])
        sha(ledger["tree"])
        if (not isinstance(ledger["processes"], list) or len(ledger["processes"]) != 2
                or set(ledger["routes"]) != {"app", "mcp"}):
            raise ValueError("invalid protected observation identity")
        minimum = self.policy.producer.minimum_observation_seconds
        started = ledger["started_monotonic"]
        finished = ledger["finished_monotonic"]
        if (type(started) not in {int, float} or type(finished) not in {int, float}
                or type(ledger["observation_seconds"]) not in {int, float}
                or finished < started or ledger["observation_seconds"] < minimum
                or finished - started < minimum):
            raise ValueError("observation window too short or not actual")
        if ledger["finished_at"] > self.clock() + 1:
            raise ValueError("observation freshness metadata is in the future")
        samples = ledger["samples"]
        if not isinstance(samples, list) or len(samples) < 2:
            raise ValueError("continuity samples required")
        for sample in samples:
            if (not isinstance(sample, dict)
                    or set(sample) != {"at", "processes_sha256", "routes_sha256"}
                    or sample["processes_sha256"] != digest(ledger["processes"])
                    or sample["routes_sha256"] != digest(ledger["routes"])):
                raise ValueError("observation continuity changed")
        _sha64(ledger[outcome_key], "observation outcome binding")
        return ledger

    def _validate_green(self, state: dict, ledger: dict) -> None:
        directory = self.policy.location("directory")
        if not deploy_state.native_consistent(directory, state):
            raise ValueError("native state/guard mismatch; operator reconciliation required")
        active = (state.get("native_queue") or {}).get("active")
        if not isinstance(active, dict) or active.get("phase") != "deploy":
            raise ValueError("completed actual G stages required")
        journal = active["journal"]
        minimum = self.policy.producer.minimum_observation_seconds
        if (len(journal) != len(deploy_state.NATIVE_STAGES)
                or [row["stage"] for row in journal] != list(deploy_state.NATIVE_STAGES)
                or any(row.get("completed") is not True or row.get("rc") != 0 for row in journal)
                or journal[-1]["completed_monotonic"] - journal[-1]["started_monotonic"] < minimum):
            raise ValueError("completed >=" + str(minimum) + "s actual stage journal required")
        manifest = active["candidate"]["manifest"]
        if (ledger["sha"] != manifest["group_sha"] or ledger["tree"] != manifest["tree_sha"]
                or ledger["journal_sha256"] != digest(journal)
                or ledger["finished_at"] < journal[-1]["completed_at"]):
            raise ValueError("runtime record is not bound to the actual G journal")

    def _assert_green_unchanged(self, ledger: dict) -> None:
        state = deploy_state.load_state(self.policy.location("directory"))
        active = (state.get("native_queue") or {}).get("active")
        if (not isinstance(active, dict) or digest(active.get("journal")) != ledger["journal_sha256"]
                or active["candidate"]["manifest"]["group_sha"] != ledger["sha"]
                or active["candidate"]["manifest"]["tree_sha"] != ledger["tree"]):
            raise ValueError("active G identity/journal changed during observation")

    def _assert_baseline_unchanged(self, ledger: dict, evidence: BaselineEvidence,
                                   initial: dict) -> None:
        state = deploy_state.load_state(self.policy.location("directory"))
        outcome, _ = self._baseline_outcome(state, evidence)
        if (digest(outcome) != ledger["source_outcome_sha256"]
                or any(state.get(key) != initial.get(key) for key in (
                    "native_queue", "native_bootstrap", "native_source_anchor", "deployed"))):
            raise ValueError("baseline identity changed during observation")

    def _write_runtime(self, ledger: dict, *, verify_state) -> dict:
        processes = observe_processes(self.policy, self.observer, ledger["sha"])
        if processes != ledger["processes"]:
            raise ValueError("runtime identity changed; new observation window required")
        routes = self._probe(ledger["routes"])
        record = {"version": 1, "policy": self.policy.fingerprint,
                  "epoch": self.policy.value["epoch"], "sha": ledger["sha"],
                  "tree": ledger["tree"], "processes": processes,
                  "observed_at": self.clock(),
                  "observation_seconds": ledger["observation_seconds"],
                  "incident": False, "routes": routes}
        # Same short lock as NativeQueue.start/_acquire and the initializer.
        # os.replace inside _write_record is the publication linearization point.
        # The observer never takes the attempt fence; probes/readback stay outside.
        with deploy_state.state_transaction(self.policy.location("directory")):
            verify_state(ledger)
            self._write_record(self.policy.location("runtime_file"), record)
        ProtectedRuntime(self.policy, observer=self.observer
                         if self.policy.fixture else None).read()
        return record

    def publish_runtime_from_ledger(self, ledger: object, *, kind: str) -> dict:
        if kind not in {"baseline", "green"}:
            raise ValueError("baseline or green runtime kind required")
        _reject_fence()
        ledger = self._validate_ledger(ledger, kind)
        state = deploy_state.load_state(self.policy.location("directory"))
        if kind == "baseline":
            evidence = BaselineEvidence.from_dict(self._record(RECORD_EVIDENCE))
            outcome, _ = self._baseline_outcome(state, evidence)
            if ledger["source_outcome_sha256"] != digest(outcome):
                raise ValueError("original successful source outcome changed")
            if ledger["sha"] != evidence.public_sha or ledger["tree"] != evidence.tree_sha:
                raise ValueError("baseline runtime is not the approved public P")
        else:
            self._validate_green(state, ledger)
        release = self.policy.location("release_root") / ledger["sha"]
        protected_path(release, self.policy.owner, anchor=self.policy.anchor)
        for row in ledger["processes"]:
            if row.get("cwd") != str(release) or row.get("uid") != self.policy.value["app_uid"]:
                raise ValueError("runtime process is not the immutable protected release")
        verify_state = ((lambda ledger: self._assert_baseline_unchanged(ledger, evidence, state))
                        if kind == "baseline"
                        else self._assert_green_unchanged)
        return self._write_runtime(ledger, verify_state=verify_state)

    def observe_baseline(self) -> dict:
        """Real bounded >=600s continuity ledger; never shortened by a caller."""
        _reject_fence()
        evidence = BaselineEvidence.from_dict(self._record(RECORD_EVIDENCE))
        state = deploy_state.load_state(self.policy.location("directory"))
        outcome, _ = self._baseline_outcome(state, evidence)
        minimum = self.policy.producer.minimum_observation_seconds
        processes = observe_processes(self.policy, self.observer, evidence.public_sha)
        routes = self.default_routes("baseline")
        started_monotonic, started_at = self.monotonic(), self.clock()
        samples = []
        while True:
            current = observe_processes(self.policy, self.observer, evidence.public_sha)
            if current != processes:
                raise ValueError("runtime identity changed; observation reset")
            self._probe(routes)
            samples.append({"at": self.clock(), "processes_sha256": digest(current),
                            "routes_sha256": digest(routes)})
            elapsed = self.monotonic() - started_monotonic
            if elapsed >= minimum:
                break
            self.sleep(min(30, max(0, minimum - elapsed)))
        finished_monotonic, finished_at = self.monotonic(), self.clock()
        ledger = {"version": 1, "policy": self.policy.fingerprint,
                  "epoch": self.policy.value["epoch"], "kind": "baseline",
                  "sha": evidence.public_sha, "tree": evidence.tree_sha,
                  "processes": processes, "routes": routes,
                  "source_outcome_sha256": digest(outcome),
                  "started_at": started_at, "finished_at": finished_at,
                  "started_monotonic": started_monotonic,
                  "finished_monotonic": finished_monotonic,
                  "observation_seconds": finished_monotonic - started_monotonic,
                  "samples": samples, "incident": False}
        self._write_record(self.records / RECORD_LEDGER, ledger)
        return self._validate_ledger(ledger, "baseline")

    def publish_runtime_baseline(self) -> dict:
        path = self.records / RECORD_LEDGER
        try:
            protected_path(path, self.policy.owner, anchor=self.policy.anchor, private=True)
            ledger = self._record(RECORD_LEDGER)
        except FileNotFoundError:
            ledger = self.observe_baseline()
        return self.publish_runtime_from_ledger(ledger, kind="baseline")

    def publish_runtime_green(self) -> dict:
        _reject_fence()
        state = deploy_state.load_state(self.policy.location("directory"))
        active = (state.get("native_queue") or {}).get("active")
        if not isinstance(active, dict) or active.get("phase") != "deploy":
            raise ValueError("completed actual G stages required")
        journal = active["journal"]
        minimum = self.policy.producer.minimum_observation_seconds
        if (len(journal) != len(deploy_state.NATIVE_STAGES)
                or [row["stage"] for row in journal] != list(deploy_state.NATIVE_STAGES)
                or any(row.get("completed") is not True or row.get("rc") != 0 for row in journal)
                or journal[-1]["completed_monotonic"] - journal[-1]["started_monotonic"] < minimum):
            raise ValueError("completed >=" + str(minimum) + "s actual stage journal required")
        manifest = active["candidate"]["manifest"]
        duration = journal[-1]["completed_monotonic"] - journal[-1]["started_monotonic"]
        ledger = {"version": 1, "policy": self.policy.fingerprint,
                  "epoch": self.policy.value["epoch"], "kind": "green",
                  "sha": manifest["group_sha"], "tree": manifest["tree_sha"],
                  "processes": observe_processes(self.policy, self.observer, manifest["group_sha"]),
                  "routes": self.default_routes("green"),
                  "journal_sha256": digest(journal),
                  "started_at": journal[-1]["started_at"],
                  "finished_at": max(self.clock(), journal[-1]["completed_at"]),
                  "started_monotonic": journal[-1]["started_monotonic"],
                  "finished_monotonic": journal[-1]["completed_monotonic"],
                  "observation_seconds": duration, "samples": [], "incident": False}
        # The real journal is the continuity proof; it already spans >=600s.
        ledger["samples"] = [{"at": journal[-1]["completed_at"],
                              "processes_sha256": digest(ledger["processes"]),
                              "routes_sha256": digest(ledger["routes"])},
                             {"at": self.clock(),
                              "processes_sha256": digest(ledger["processes"]),
                              "routes_sha256": digest(ledger["routes"])}]
        self._validate_green(state, self._validate_ledger(ledger, "green"))
        return self._write_runtime(ledger, verify_state=self._assert_green_unchanged)

    def publish_runtime_process(self, kind: str, *, timeout: int = 900) -> dict:
        """Start the fixed bounded publisher child and require its positive exit."""
        if kind not in {"baseline", "green"}:
            raise ValueError("baseline or green runtime kind required")
        policy = self.policy
        bootstrap = ("import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); "
                     "runpy.run_module('scripts.prod_agent.native_prepare',run_name='__main__')")
        command = [str(policy.python), "-I", "-B", "-c", bootstrap, str(policy.authority),
                   "publish-runtime", "--kind", kind]
        if policy.fixture is not None:
            command += ["--fixture-root", str(policy.fixture.root),
                        "--fixture-endpoint", policy.fixture.endpoint,
                        "--fixture-policy", str(policy.path)]
        # The observer never receives the attempt descriptor, attempt identity or
        # the inherited application environment.
        env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
        child = subprocess.Popen(command, env=env, close_fds=True, cwd="/",
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        try:
            stdout, _ = child.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            child.kill()
            child.communicate()
            raise ValueError("runtime publisher uncertain after timeout") from exc
        if child.returncode != 0 or len(stdout) > MAX_INPUT:
            raise ValueError("independent runtime publisher failed")
        return json.loads(stdout)

    # ── genuine baseline transition ───────────────────────────────────────

    def publish_baseline(self) -> dict:
        """Emit only the fresh transition file; never initialize or sign."""
        if deploy_state.process_lock_fd() is None:
            raise ValueError("existing attempt fence required")
        evidence = BaselineEvidence.from_dict(self._record(RECORD_EVIDENCE))
        row = self._validate_map(self._record(RECORD_MAP), evidence)
        state = deploy_state.load_state(self.policy.location("directory"))
        deployed = self._validate_uninitialized(state, evidence)
        runtime = self.policy.record("runtime_file")
        if ProtectedRuntime(self.policy, observer=self.observer
                            if self.policy.fixture else None).snapshot() != runtime:
            raise ValueError("independent P runtime record changed")
        if runtime["sha"] != evidence.public_sha or runtime["tree"] != row["tree_sha"]:
            raise ValueError("runtime is not the approved public P")
        main = self.client.get(f"/repos/{REPOSITORY}/branches/main")["commit"]["sha"]
        if main != runtime["sha"]:
            raise ValueError("remote main does not equal the protected P runtime")
        transition = {"version": 1, "policy": self.policy.fingerprint,
                      "epoch": self.policy.value["epoch"],
                      "source_sha": evidence.source_sha, "public_sha": main,
                      "tree_sha": runtime["tree"], "map_sha256": evidence.map_sha256,
                      "source_outcome_sha256": digest(deployed),
                      "runtime_sha256": digest(runtime), "observed_at": self.clock()}
        self._write_record(self.policy.location("bootstrap_file"), transition)
        return transition

    # ── independent review request / result ───────────────────────────────

    def prepare_review_request(self, candidate: dict, ci: dict, *,
                               reviewer_uid: int | None = None) -> dict:
        policy = self.policy
        reviewers = policy.value["reviewers"]
        if reviewer_uid is None:
            if len(reviewers) != 1:
                raise ValueError("exactly one approved reviewer is required")
            reviewer_uid = int(next(iter(reviewers)))
        principal = reviewers.get(str(reviewer_uid))
        if principal is None:
            raise ValueError("unapproved reviewer identity")
        try:
            reviewer_gid = pwd.getpwuid(reviewer_uid).pw_gid
        except KeyError as exc:
            raise ValueError("reviewer account does not exist") from exc
        expected = {"version": 1, "candidate_sha256": digest(candidate),
                    "policy": policy.fingerprint, "epoch": policy.value["epoch"],
                    "reviewer": principal, "writer": policy.value["writer"],
                    "role": "independent_prod", "verdict": "pass",
                    "ci_sha256": digest(ci)}
        request = {"version": 1, "review_uid": str(reviewer_uid), "reviewer": principal,
                   "requested_at": self.clock(), "expected": expected,
                   "candidate": candidate, "ci": ci}
        root = policy.location("review_directory")
        requests = _ensure_directory(root / "requests", policy.owner,
                                     anchor=policy.anchor, mode=0o750, group=reviewer_gid)
        directory = _ensure_directory(requests / str(reviewer_uid), policy.owner,
                                      anchor=policy.anchor, mode=0o750, group=reviewer_gid)
        target = directory / (digest(candidate) + ".json")
        _atomic_write(target, json.dumps(request).encode(), owner=policy.owner,
                      mode=0o640, group=reviewer_gid, anchor=policy.anchor)
        return {"request": str(target), "candidate_sha256": expected["candidate_sha256"],
                "policy": expected["policy"], "epoch": expected["epoch"],
                "reviewer_uid": reviewer_uid, "reviewer": principal}


def publish_review_result(review_directory: Path, candidate_digest: str, *,
                          decision: str) -> dict:
    """Reviewer-owned result only; root/app paths cannot supply a verdict string."""
    uid = os.geteuid()
    if uid == 0:
        raise ValueError("reviewer publication is denied for root")
    if not isinstance(candidate_digest, str) or not CANONICAL_SHA64.fullmatch(candidate_digest):
        raise ValueError("candidate digest required")
    if decision != "pass":
        raise ValueError("explicit independent pass decision required")
    directory = Path(review_directory)
    request_path = directory / "requests" / str(uid) / (candidate_digest + ".json")
    try:
        fd = os.open(request_path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ValueError("protected review request unavailable") from exc
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, uid}
                or info.st_mode & 0o007 or info.st_size > MAX_INPUT):
            raise ValueError("unsafe protected review request")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(MAX_INPUT + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_INPUT:
        raise ValueError("review request too large")
    request = json.loads(raw)
    expected = request.get("expected") if isinstance(request, dict) else None
    record_keys = {"version", "candidate_sha256", "policy", "epoch", "reviewer",
                   "writer", "role", "verdict", "ci_sha256"}
    if (not isinstance(request, dict) or set(request) != {
            "version", "review_uid", "reviewer", "requested_at", "expected",
            "candidate", "ci"}
            or request["version"] != 1 or request["review_uid"] != str(uid)
            or not isinstance(expected, dict) or set(expected) != record_keys
            or expected["candidate_sha256"] != candidate_digest
            or expected["candidate_sha256"] != digest(request["candidate"])
            or expected["ci_sha256"] != digest(request["ci"])
            or expected["reviewer"] != request["reviewer"] or expected["verdict"] != "pass"
            or _sha64(expected["policy"], "review policy") != expected["policy"]
            or _sha64(expected["ci_sha256"], "review CI") != expected["ci_sha256"]
            or not EPOCH.fullmatch(expected["epoch"])):
        raise ValueError("review request binding mismatch")
    record = {key: expected[key] for key in record_keys}
    result_directory = directory / str(uid)
    try:
        info = result_directory.lstat()
    except OSError as exc:
        raise ValueError("reviewer-owned result directory required") from exc
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != uid or info.st_mode & 0o077):
        raise ValueError("unsafe reviewer result directory")
    target = result_directory / (candidate_digest + ".json")
    if target.exists():
        raise ValueError("review result already published")
    _atomic_write(target, json.dumps(record).encode(), owner=uid, mode=0o600, anchor=directory)
    return record


def _fixture_policy(args) -> InstalledPolicy:
    values = (args.fixture_root, args.fixture_endpoint, args.fixture_policy)
    if not any(values):
        return InstalledPolicy(POLICY_PATH)
    if not all(values):
        raise ValueError("complete owned fixture capability required")
    from scripts.prod_agent.native_ports import OwnedFixture

    root = Path(args.fixture_root)
    policy_path = Path(args.fixture_policy)
    if not policy_path.is_relative_to(root.resolve()) or policy_path == POLICY_PATH:
        raise ValueError("fixture policy must live inside its owned root")
    return InstalledPolicy(policy_path, fixture=OwnedFixture(root, args.fixture_endpoint))


def _controller_main(args) -> int:
    """Fixed H/G lifecycle routing behind the same allowlisted launcher."""
    if args.operation in {"grant", "release"}:
        print("standalone grant/release unavailable; use protected deploy-front/recover",
              file=sys.stderr)
        return 2
    try:
        policy = _fixture_policy(args)
    except (FileNotFoundError, ValueError):
        print("installed native controller policy unavailable", file=sys.stderr)
        return 1
    except Exception:
        print("protected H/G prerequisites unavailable or inconsistent; reconcile active attempts",
              file=sys.stderr)
        return 1
    try:
        from scripts.prod_agent.native_controller import NativeController

        controller = NativeController(policy)
        if args.operation == "admit-head":
            value = controller.admit_head(args.pr)
        else:
            value = getattr(controller, args.operation.replace("-", "_"))()
        print(json.dumps(value))
        return 0
    except Exception:
        print("protected H/G prerequisites unavailable or inconsistent; reconcile active attempts",
              file=sys.stderr)
        return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=(
        "render-preparation", "export-baseline", "publish-runtime", "publish-baseline",
        "prepare-review-request", "publish-review", *CONTROLLER_OPERATIONS))
    parser.add_argument("--kind", choices=("baseline", "green"))
    parser.add_argument("--candidate-digest")
    parser.add_argument("--decision", choices=("pass",))
    parser.add_argument("--pr", type=int)
    parser.add_argument("--fixture-root")
    parser.add_argument("--fixture-endpoint")
    parser.add_argument("--fixture-policy")
    args = parser.parse_args(argv)
    try:
        if args.operation == "publish-review":
            record = publish_review_result(REVIEW_ROOT, args.candidate_digest or "",
                                           decision=args.decision or "")
            print(json.dumps(record))
            return 0
        os.environ.clear()
        os.environ.update(PATH="/usr/bin:/bin", LANG="C.UTF-8")
        if args.operation in CONTROLLER_OPERATIONS:
            return _controller_main(args)
        policy = _fixture_policy(args)
        preparer = NativePreparer(policy)
        if args.operation == "render-preparation":
            value = render_preparation(preparer.plan())
        elif args.operation in {"export-baseline", "publish-baseline"}:
            # The trusted baseline branches self-acquire the original attempt
            # fence. The observer path never enters this branch, never receives
            # this descriptor and never inherits the application environment.
            runner = NativeQueue(policy.location("repo"), policy.location("directory"),
                                 policy.producer, None)
            with runner.fence():
                if args.operation == "export-baseline":
                    value = preparer.export_baseline()
                else:
                    value = preparer.publish_baseline()
        elif args.operation == "publish-runtime":
            if args.kind == "green":
                value = preparer.publish_runtime_green()
            elif args.kind == "baseline":
                value = preparer.publish_runtime_baseline()
            else:
                raise ValueError("baseline or green runtime kind required")
        else:
            from scripts.prod_agent.native_controller import NativeController
            controller = NativeController(policy)
            state = deploy_state.load_state(policy.location("directory"))
            active = (state.get("native_queue") or {}).get("active")
            if not isinstance(active, dict):
                raise ValueError("active protected G candidate required")
            value = preparer.prepare_review_request(active["candidate"],
                                                    controller.review_ci(active["candidate"]))
        print(json.dumps(value))
        return 0
    except Exception:
        print("protected native preparation/publisher unavailable or inconsistent",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
