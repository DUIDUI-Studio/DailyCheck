"""One-shot Lucy read runner for Issue #16 (default off; no production activation).

Two narrow entry points:

``consume``
    Host-local, production-Linux only (synthetic fixtures are allowed anywhere).
    Consumes one protected operator plan exactly once: claim -> preflight ->
    one temporary ``agent_tokens`` identity -> a real MCP stdio child using the
    unchanged strict ``ReadonlyPolicy`` -> one approved item -> revoke/cleanup.

``relay``
    Mac-side. Reads one protected relay request and executes exactly one fixed
    remote ``consume`` command through the existing SSH alias, with a supplied
    trusted host public key, per-invocation strict host checking and no
    fallback to weaker settings.

This module is not a gateway, scheduler, credential service or query client.
Plans cannot choose tools, SQL, ACLs or credentials; the tool sequence and the
token ACL are constants here.  A plan is operator-owned and hash-pinned, not a
self-authenticating Owner approval.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

PLAN_SCHEMA = "dailycheck.read-once.plan"
RELAY_SCHEMA = "dailycheck.read-once.relay"
RESULT_SCHEMA = "dailycheck.read-once.result"
SCHEMA_VERSION = 1

STRICT_TOOL_NAMES = ("warehouse_list", "items_list", "items_detail")
CONSUME_TOOLS = ("warehouse_list", "items_detail")
TOKEN_READ_PATHS = ("/api/v1/warehouses", "/api/v1/items")
TOKEN_NAME_PREFIX = "lucy-read-once:"
MCP_MODULE = "mcp_server"
RELAY_MODULE = "scripts.mcp_lucy_read_once"
RELAY_HOST_ALIAS = "wdg_vps"

ITEM_DETAIL_FIELDS = (
    "id",
    "sku",
    "name",
    "category_id",
    "current_stock",
    "safety_stock",
    "unit",
    "unit_cost",
    "gram_per_unit",
    "updated_at",
    "category_name",
)

MAX_PLAN_BYTES = 64 * 1024
MAX_ANCHOR_BYTES = 64 * 1024
MAX_REMOTE_BYTES = 1024 * 1024
MAX_POLICY_SECONDS = 300
MAX_RELAY_TIMEOUT = 600

_CORRELATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_WAREHOUSE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FINGERPRINT_RE = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_REMOTE_PATH_RE = re.compile(r"^/[A-Za-z0-9._/@+:-]{1,4000}$")
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9.:_-]{1,255}$")
_KEY_TYPE_RE = re.compile(r"^[a-z0-9-]{1,64}$")

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)

_STATES = (
    "claimed",
    "preflight-rejected",
    "reading",
    "cleaning",
    "succeeded",
    "reconcile_required",
)
_TRANSITIONS = {
    "claimed": {"preflight-rejected", "reading"},
    "reading": {"cleaning"},
    "cleaning": {"succeeded", "reconcile_required"},
    "preflight-rejected": set(),
    "succeeded": set(),
    "reconcile_required": set(),
}

EXIT_OK = 0
EXIT_PREFLIGHT = 2
EXIT_CLAIM = 3
EXIT_TRANSPORT = 4
EXIT_RECONCILE = 5
EXIT_RELAY = 6


class ReadOnceError(Exception):
    """A fail-closed condition with a fixed, non-secret code."""

    def __init__(self, code: str, *, stage: str, exit_code: int = EXIT_PREFLIGHT) -> None:
        super().__init__(code)
        self.code = code
        self.stage = stage
        self.exit_code = exit_code


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: object, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ReadOnceError(f"{field_name}_invalid", stage="plan")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ReadOnceError(f"{field_name}_invalid", stage="plan") from exc
    if parsed.tzinfo is None:
        raise ReadOnceError(f"{field_name}_invalid", stage="plan")
    return parsed.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Strict schema helpers
# ---------------------------------------------------------------------------


def _mapping(value: object, field_name: str) -> dict:
    if not isinstance(value, dict):
        raise ReadOnceError(f"{field_name}_invalid", stage="plan")
    return value


def _exact_keys(value: dict, expected: set[str], field_name: str) -> None:
    if set(value) != expected:
        raise ReadOnceError(f"{field_name}_keys_invalid", stage="plan")


def _text(value: object, field_name: str, *, max_len: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise ReadOnceError(f"{field_name}_invalid", stage="plan")
    if _CONTROL_RE.search(value):
        raise ReadOnceError(f"{field_name}_invalid", stage="plan")
    return value


def _boolean(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ReadOnceError(f"{field_name}_invalid", stage="plan")
    return value


def _integer(value: object, field_name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReadOnceError(f"{field_name}_invalid", stage="plan")
    if value < minimum or value > maximum:
        raise ReadOnceError(f"{field_name}_invalid", stage="plan")
    return value


def _correlation(value: object, field_name: str = "correlation") -> str:
    text = _text(value, field_name, max_len=64)
    if not _CORRELATION_RE.match(text):
        raise ReadOnceError(f"{field_name}_invalid", stage="plan")
    return text


def _absolute_path(value: object, field_name: str) -> Path:
    text = _text(value, field_name)
    path = Path(text)
    if not path.is_absolute():
        raise ReadOnceError(f"{field_name}_not_absolute", stage="plan")
    return path


def _remote_path(value: object, field_name: str) -> Path:
    """A fixed remote POSIX path: no shell metacharacters or whitespace."""
    path = _absolute_path(value, field_name)
    if not _REMOTE_PATH_RE.match(str(path)):
        raise ReadOnceError(f"{field_name}_invalid", stage="relay")
    return path


def _sha1(value: object, field_name: str) -> str:
    text = _text(value, field_name, max_len=40)
    if not _SHA1_RE.match(text):
        raise ReadOnceError(f"{field_name}_invalid", stage="plan")
    return text


def _sha256(value: object, field_name: str) -> str:
    text = _text(value, field_name, max_len=64)
    if not _SHA256_RE.match(text):
        raise ReadOnceError(f"{field_name}_invalid", stage="plan")
    return text


def _within(child: Path, parent: Path) -> bool:
    try:
        return child.resolve(strict=True).is_relative_to(parent.resolve(strict=True))
    except (OSError, RuntimeError):
        return False


# ---------------------------------------------------------------------------
# Protected files and the claim ledger
# ---------------------------------------------------------------------------


def _read_protected_bytes(path: Path, *, max_bytes: int, private: bool) -> bytes:
    """Read a single regular file through one no-follow descriptor."""
    flags = os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ReadOnceError("protected_file_unreadable", stage="plan") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ReadOnceError("protected_file_not_regular", stage="plan")
        if info.st_size > max_bytes:
            raise ReadOnceError("protected_file_too_large", stage="plan")
        if private:
            if info.st_mode & 0o077:
                raise ReadOnceError("protected_file_permissions", stage="plan")
            if info.st_uid != os.geteuid() and info.st_uid != 0:
                raise ReadOnceError("protected_file_owner", stage="plan")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > max_bytes:
            raise ReadOnceError("protected_file_too_large", stage="plan")
        return data
    except OSError as exc:
        raise ReadOnceError("protected_file_unreadable", stage="plan") from exc
    finally:
        os.close(fd)


def _parse_json_bytes(data: bytes, field_name: str) -> object:
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ReadOnceError(f"{field_name}_invalid_json", stage="plan") from exc


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | _O_CLOEXEC)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        view = view[written:]


class ClaimLedger:
    """One durable, non-secret receipt per correlation; never removed."""

    def __init__(self, directory: Path, correlation: str, plan_sha256: str) -> None:
        self.directory = directory
        self.correlation = correlation
        self.plan_sha256 = plan_sha256
        self.path = directory / f"{correlation}.claim.json"
        self.data: dict = {}

    def claim(self) -> None:
        try:
            info = self.directory.lstat()
        except OSError as exc:
            raise ReadOnceError("claim_dir_unavailable", stage="claim") from exc
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise ReadOnceError("claim_dir_untrusted", stage="claim")
        payload = {
            "schema": PLAN_SCHEMA,
            "version": SCHEMA_VERSION,
            "correlation": self.correlation,
            "plan_sha256": self.plan_sha256,
            "state": "claimed",
            "created_at": _iso(_utcnow()),
            "updated_at": _iso(_utcnow()),
            "stage": None,
            "token_id": None,
            "token_name": None,
            "history": [{"state": "claimed", "at": _iso(_utcnow())}],
        }
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC
        try:
            fd = os.open(self.path, flags, 0o600)
        except FileExistsError as exc:
            raise ReadOnceError(
                "already_claimed", stage="claim", exit_code=EXIT_CLAIM
            ) from exc
        except OSError as exc:
            raise ReadOnceError("claim_write_failed", stage="claim") from exc
        try:
            _write_all(fd, (json.dumps(payload, sort_keys=True) + "\n").encode())
            os.fsync(fd)
        except OSError as exc:
            raise ReadOnceError("claim_write_failed", stage="claim") from exc
        finally:
            os.close(fd)
        _fsync_dir(self.directory)
        self.data = payload

    def load_existing(self) -> dict:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def update(self, **changes: object) -> None:
        if not self.data:
            self.data = self.load_existing()
        self.data.update(changes)
        self.data["updated_at"] = _iso(_utcnow())
        self._write()

    def advance(self, state: str, *, stage: str | None = None) -> None:
        if state not in _STATES:
            raise ReadOnceError("state_invalid", stage="claim")
        if not self.data:
            self.data = self.load_existing()
        current = self.data.get("state")
        if state not in _TRANSITIONS.get(current, set()):
            raise ReadOnceError("state_transition_invalid", stage="claim")
        history = list(self.data.get("history") or [])
        history.append({"state": state, "at": _iso(_utcnow())})
        self.data["state"] = state
        self.data["stage"] = stage
        self.data["history"] = history
        self.data["updated_at"] = _iso(_utcnow())
        self._write()

    def _write(self) -> None:
        temp = self.directory / f".{self.path.name}.{os.getpid()}.{secrets.token_hex(4)}"
        payload = (json.dumps(self.data, sort_keys=True) + "\n").encode()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC
        try:
            fd = os.open(temp, flags, 0o600)
            try:
                _write_all(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temp, self.path)
            _fsync_dir(self.directory)
        except OSError as exc:
            with contextlib.suppress(OSError):
                os.unlink(temp)
            raise ReadOnceError("claim_write_failed", stage="claim") from exc


# ---------------------------------------------------------------------------
# Plan model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceSpec:
    repository: Path
    git_sha: str
    python: Path
    module: str


@dataclass(frozen=True)
class AnchorSpec:
    path: Path
    source_sha: str


@dataclass(frozen=True)
class DatabaseSpec:
    master_path: Path
    warehouse_root: Path
    warehouse_code: str
    warehouse_file: Path


@dataclass(frozen=True)
class ActiveBusinessSpec:
    pid: int
    start_time: str


@dataclass(frozen=True)
class ReadOncePlan:
    correlation: str
    identity: str
    created_at: datetime
    expires_at: datetime
    budget_seconds: int
    source: SourceSpec
    deploy_anchor: AnchorSpec
    database: DatabaseSpec
    active_business: ActiveBusinessSpec
    fence_path: Path
    claim_dir: Path
    item_id: int | None
    created_by_user_id: int
    rate_limit: int
    plan_sha256: str


def _build_plan(data: object, *, plan_sha256: str) -> ReadOncePlan:
    plan = _mapping(data, "plan")
    _exact_keys(
        plan,
        {
            "schema",
            "version",
            "correlation",
            "identity",
            "created_at",
            "expires_at",
            "budget_seconds",
            "source",
            "deploy_anchor",
            "database",
            "active_business",
            "fence",
            "claim_dir",
            "item",
            "field_consent",
            "controls",
            "token",
        },
        "plan",
    )
    if plan["schema"] != PLAN_SCHEMA:
        raise ReadOnceError("plan_schema_invalid", stage="plan")
    if plan["version"] != SCHEMA_VERSION or isinstance(plan["version"], bool):
        raise ReadOnceError("plan_version_invalid", stage="plan")
    correlation = _correlation(plan["correlation"])
    identity = plan["identity"]
    if identity not in ("production", "synthetic"):
        raise ReadOnceError("plan_identity_invalid", stage="plan")
    created_at = _parse_utc(plan["created_at"], "created_at")
    expires_at = _parse_utc(plan["expires_at"], "expires_at")
    if expires_at <= _utcnow():
        raise ReadOnceError("plan_expired", stage="plan")
    budget_seconds = _integer(plan["budget_seconds"], "budget_seconds", minimum=1, maximum=60)

    source = _mapping(plan["source"], "source")
    _exact_keys(source, {"repository", "git_sha", "python", "module"}, "source")
    module = _text(source["module"], "source.module", max_len=64)
    if module != MCP_MODULE:
        raise ReadOnceError("source_module_invalid", stage="plan")
    source_sha = _sha1(source["git_sha"], "source.git_sha")

    anchor = _mapping(plan["deploy_anchor"], "deploy_anchor")
    _exact_keys(anchor, {"path", "source_sha"}, "deploy_anchor")
    anchor_sha = _sha1(anchor["source_sha"], "deploy_anchor.source_sha")
    if anchor_sha != source_sha:
        raise ReadOnceError("deploy_anchor_source_mismatch", stage="plan")

    database = _mapping(plan["database"], "database")
    _exact_keys(
        database,
        {"master_path", "warehouse_root", "warehouse_code", "warehouse_file"},
        "database",
    )
    warehouse_code = _text(database["warehouse_code"], "database.warehouse_code", max_len=64)
    if not _WAREHOUSE_RE.match(warehouse_code):
        raise ReadOnceError("database.warehouse_code_invalid", stage="plan")

    active = _mapping(plan["active_business"], "active_business")
    _exact_keys(active, {"pid", "start_time"}, "active_business")
    start_time = _text(active["start_time"], "active_business.start_time", max_len=64)

    fence = _mapping(plan["fence"], "fence")
    _exact_keys(fence, {"path"}, "fence")

    item = _mapping(plan["item"], "item")
    _exact_keys(item, {"id"}, "item")
    item_id = item["id"]
    if item_id is not None:
        item_id = _integer(item_id, "item.id", minimum=1, maximum=2**63 - 1)

    consent = _mapping(plan["field_consent"], "field_consent")
    _exact_keys(consent, {"item_detail", "unit_cost"}, "field_consent")
    if not _boolean(consent["item_detail"], "field_consent.item_detail"):
        raise ReadOnceError("field_consent_missing", stage="plan")
    if not _boolean(consent["unit_cost"], "field_consent.unit_cost"):
        raise ReadOnceError("field_consent_missing", stage="plan")

    controls = _mapping(plan["controls"], "controls")
    _exact_keys(controls, {"issue_identity"}, "controls")
    if not _boolean(controls["issue_identity"], "controls.issue_identity"):
        raise ReadOnceError("identity_issuance_not_permitted", stage="plan")

    token = _mapping(plan["token"], "token")
    _exact_keys(token, {"created_by_user_id", "rate_limit"}, "token")
    created_by = _integer(token["created_by_user_id"], "token.created_by_user_id", minimum=1, maximum=2**63 - 1)
    rate_limit = _integer(token["rate_limit"], "token.rate_limit", minimum=1, maximum=600)

    return ReadOncePlan(
        correlation=correlation,
        identity=identity,
        created_at=created_at,
        expires_at=expires_at,
        budget_seconds=budget_seconds,
        source=SourceSpec(
            repository=_absolute_path(source["repository"], "source.repository"),
            git_sha=source_sha,
            python=_absolute_path(source["python"], "source.python"),
            module=module,
        ),
        deploy_anchor=AnchorSpec(
            path=_absolute_path(anchor["path"], "deploy_anchor.path"),
            source_sha=anchor_sha,
        ),
        database=DatabaseSpec(
            master_path=_absolute_path(database["master_path"], "database.master_path"),
            warehouse_root=_absolute_path(database["warehouse_root"], "database.warehouse_root"),
            warehouse_code=warehouse_code,
            warehouse_file=_absolute_path(database["warehouse_file"], "database.warehouse_file"),
        ),
        active_business=ActiveBusinessSpec(
            pid=_integer(active["pid"], "active_business.pid", minimum=1, maximum=2**31 - 1),
            start_time=start_time,
        ),
        fence_path=_absolute_path(fence["path"], "fence.path"),
        claim_dir=_absolute_path(plan["claim_dir"], "claim_dir"),
        item_id=item_id,
        created_by_user_id=created_by,
        rate_limit=rate_limit,
        plan_sha256=plan_sha256,
    )


def load_plan(path: str, *, expected_sha256: str, expected_correlation: str) -> ReadOncePlan:
    expected_hash = _sha256(expected_sha256, "plan_sha256")
    expected_corr = _correlation(expected_correlation)
    data = _read_protected_bytes(Path(path), max_bytes=MAX_PLAN_BYTES, private=True)
    actual_hash = hashlib.sha256(data).hexdigest()
    if actual_hash != expected_hash:
        raise ReadOnceError("plan_hash_mismatch", stage="plan")
    plan = _build_plan(_parse_json_bytes(data, "plan"), plan_sha256=actual_hash)
    if plan.correlation != expected_corr:
        raise ReadOnceError("plan_correlation_mismatch", stage="plan")
    return plan


# ---------------------------------------------------------------------------
# Preflight evidence
# ---------------------------------------------------------------------------


def _tool_env() -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
        "HOME": os.environ.get("HOME", ""),
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    sock = os.environ.get("SSH_AUTH_SOCK")
    if sock:
        env["SSH_AUTH_SOCK"] = sock
    return env


def _run_checked(
    argv: list[str],
    *,
    cwd: Path | None,
    env: dict[str, str] | None,
    timeout: float,
) -> tuple[int, int, bytes, bytes]:
    """Run one local tool/child and positively wait for it."""
    process = subprocess.Popen(
        argv,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            os.killpg(process.pid, signal.SIGKILL)
        out, err = process.communicate()
        raise ReadOnceError("child_timeout", stage="preflight") from None
    finally:
        if process.poll() is None:
            with contextlib.suppress(OSError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    return process.pid, process.returncode, out, err


def _verify_source(plan: ReadOncePlan) -> dict:
    repository = plan.source.repository
    try:
        resolved_repo = repository.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ReadOnceError("source_repository_missing", stage="source") from exc
    if not resolved_repo.is_dir():
        raise ReadOnceError("source_repository_missing", stage="source")
    if not plan.source.python.is_file():
        raise ReadOnceError("source_python_missing", stage="source")

    git = shutil.which("git", path=_tool_env()["PATH"])
    if git is None:
        raise ReadOnceError("git_unavailable", stage="source")
    _git_pid, git_rc, git_out, _ = _run_checked(
        [git, "-C", str(resolved_repo), "rev-parse", "HEAD"],
        cwd=None,
        env=_tool_env(),
        timeout=15,
    )
    head = git_out.decode("ascii", "replace").strip() if git_rc == 0 else ""
    if head != plan.source.git_sha:
        raise ReadOnceError("source_sha_mismatch", stage="source")
    _status_pid, status_rc, status_out, _ = _run_checked(
        [
            git,
            "-C",
            str(resolved_repo),
            "status",
            "--porcelain",
            "--untracked-files=no",
            "--",
            "mcp_server",
            "config.py",
            "db",
            "permissions.py",
        ],
        cwd=None,
        env=_tool_env(),
        timeout=15,
    )
    if status_rc != 0 or status_out.strip():
        raise ReadOnceError("source_tree_dirty", stage="source")

    code = (
        "import json, mcp_server\n"
        "print(json.dumps({'file': mcp_server.__file__}, sort_keys=True))\n"
    )
    env = _tool_env()
    env["PYTHONPATH"] = str(resolved_repo)
    pid, rc, out, _ = _run_checked(
        [str(plan.source.python), "-B", "-c", code],
        cwd=resolved_repo,
        env=env,
        timeout=30,
    )
    if rc != 0:
        raise ReadOnceError("source_import_failed", stage="source")
    try:
        imported = json.loads(out.decode("utf-8"))
        import_file = Path(imported["file"]).resolve(strict=True)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise ReadOnceError("source_import_failed", stage="source") from exc
    if not import_file.is_relative_to(resolved_repo):
        raise ReadOnceError("source_import_outside_repository", stage="source")
    return {
        "git_sha": plan.source.git_sha,
        "repository": str(resolved_repo),
        "import_file": str(import_file),
        "import_pid": pid,
        "import_returncode": rc,
    }


def _read_anchor(path: Path) -> dict:
    data = _read_protected_bytes(path, max_bytes=MAX_ANCHOR_BYTES, private=False)
    payload = _parse_json_bytes(data, "deploy_anchor")
    if not isinstance(payload, dict):
        raise ReadOnceError("deploy_anchor_invalid", stage="anchor")
    return payload


def _verify_anchor(plan: ReadOncePlan) -> tuple[dict, Path]:
    payload = _read_anchor(plan.deploy_anchor.path)
    deployed = payload.get("deployed") if isinstance(payload.get("deployed"), dict) else {}
    source_sha = payload.get("source_sha") or deployed.get("sha")
    release_id = payload.get("release_id") or deployed.get("pr")
    root_value = payload.get("deployment_root")
    if not isinstance(source_sha, str) or source_sha != plan.deploy_anchor.source_sha:
        raise ReadOnceError("deploy_anchor_source_mismatch", stage="anchor")
    if not isinstance(release_id, (str, int)) or isinstance(release_id, bool):
        raise ReadOnceError("deploy_anchor_release_missing", stage="anchor")
    if not isinstance(root_value, str):
        raise ReadOnceError("deploy_anchor_root_missing", stage="anchor")
    root = Path(root_value)
    if not root.is_absolute():
        raise ReadOnceError("deploy_anchor_root_invalid", stage="anchor")
    for child in (
        plan.database.master_path,
        plan.database.warehouse_root,
        plan.fence_path,
    ):
        if not _within(child, root):
            raise ReadOnceError("deploy_anchor_scope_mismatch", stage="anchor")
    if not _within(plan.database.warehouse_file, plan.database.warehouse_root):
        raise ReadOnceError("warehouse_file_outside_root", stage="anchor")
    return {
        "path": str(plan.deploy_anchor.path),
        "source_sha": source_sha,
        "release_id": str(release_id),
    }, root


def _stat_identity(path: Path) -> dict:
    try:
        info = path.stat()
    except OSError as exc:
        raise ReadOnceError("data_file_missing", stage="binding") from exc
    return {"dev": info.st_dev, "ino": info.st_ino}


def _process_start_time(pid: int) -> str | None:
    if sys.platform.startswith("linux"):
        try:
            stat_line = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        except OSError:
            return None
        close = stat_line.rfind(")")
        if close < 0:
            return None
        fields = stat_line[close + 2 :].split()
        if len(fields) < 20:
            return None
        return fields[19]
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
            env=_tool_env(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return value or None


def _binding_linux(plan: ReadOncePlan, *, kind: str) -> dict:
    pid = plan.active_business.pid
    actual_start = _process_start_time(pid)
    if actual_start is None or actual_start != plan.active_business.start_time:
        raise ReadOnceError("active_business_start_mismatch", stage="binding")
    try:
        master = plan.database.master_path.resolve(strict=True)
        master_stat = master.stat()
    except (OSError, RuntimeError) as exc:
        raise ReadOnceError("active_business_master_missing", stage="binding") from exc
    fd_dir = Path(f"/proc/{pid}/fd")
    try:
        entries = list(fd_dir.iterdir())
    except OSError as exc:
        raise ReadOnceError("active_business_fds_unavailable", stage="binding") from exc
    for entry in entries:
        try:
            target = Path(os.readlink(entry))
            target_stat = os.stat(entry)
        except OSError:
            continue
        if target == master and (target_stat.st_dev, target_stat.st_ino) == (
            master_stat.st_dev,
            master_stat.st_ino,
        ):
            return {"kind": kind, "pid": pid, "start_time": actual_start, "master_fd_bound": True}
    raise ReadOnceError("active_business_not_holder", stage="binding")


def _binding_surrogate(plan: ReadOncePlan) -> dict:
    """Labeled macOS synthetic surrogate; never used for a production plan."""
    if plan.identity != "synthetic":
        raise ReadOnceError("unsupported_platform", stage="binding")
    pid = plan.active_business.pid
    actual_start = _process_start_time(pid)
    if actual_start is None or actual_start != plan.active_business.start_time:
        raise ReadOnceError("active_business_start_mismatch", stage="binding")
    lsof = shutil.which("lsof", path=_tool_env()["PATH"])
    if lsof is None:
        raise ReadOnceError("active_business_probe_unavailable", stage="binding")
    try:
        result = subprocess.run(
            [lsof, "-p", str(pid), "-Fn"],
            capture_output=True,
            text=True,
            timeout=15,
            env=_tool_env(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReadOnceError("active_business_probe_unavailable", stage="binding") from exc
    try:
        master = plan.database.master_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ReadOnceError("active_business_master_missing", stage="binding") from exc
    for line in result.stdout.splitlines():
        if not line.startswith("n"):
            continue
        try:
            if Path(line[1:]).resolve(strict=True) == master:
                return {
                    "kind": "synthetic-surrogate",
                    "pid": pid,
                    "start_time": actual_start,
                    "master_fd_bound": True,
                }
        except (OSError, RuntimeError):
            continue
    raise ReadOnceError("active_business_not_holder", stage="binding")


def _verify_active_business(plan: ReadOncePlan) -> dict:
    if sys.platform.startswith("linux"):
        return _binding_linux(plan, kind="linux-proc")
    if plan.identity != "synthetic":
        raise ReadOnceError("unsupported_platform", stage="binding")
    return _binding_surrogate(plan)


def _acquire_fence(plan: ReadOncePlan) -> tuple[int, dict]:
    path = plan.fence_path
    try:
        info = path.stat()
    except OSError as exc:
        raise ReadOnceError("fence_missing", stage="fence") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ReadOnceError("fence_not_regular", stage="fence")
    if info.st_uid not in (0, os.geteuid()):
        raise ReadOnceError("fence_untrusted", stage="fence")
    if info.st_mode & 0o022:
        raise ReadOnceError("fence_untrusted", stage="fence")
    try:
        fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC)
    except OSError as exc:
        raise ReadOnceError("fence_open_failed", stage="fence") from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(fd)
        raise ReadOnceError("fence_busy", stage="fence") from exc
    except OSError as exc:
        os.close(fd)
        raise ReadOnceError("fence_lock_failed", stage="fence") from exc
    return fd, {"path": str(path), "shared": True}


def _release_fence(fd: int | None) -> bool:
    if fd is None:
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
        return True
    except OSError:
        return False


def _select_item(plan: ReadOncePlan) -> int:
    from mcp_server.readonly import ReadonlyPolicyError, readonly_connection

    try:
        with readonly_connection(plan.database.warehouse_file) as conn:
            if plan.item_id is None:
                row = conn.execute("SELECT id FROM items ORDER BY id LIMIT 1").fetchone()
            else:
                row = conn.execute("SELECT id FROM items WHERE id = ?", (plan.item_id,)).fetchone()
    except (ReadonlyPolicyError, sqlite3.Error) as exc:
        raise ReadOnceError("item_selection_failed", stage="item") from exc
    if row is None:
        raise ReadOnceError("item_not_found", stage="item")
    item_id = row[0]
    if isinstance(item_id, bool) or not isinstance(item_id, int):
        raise ReadOnceError("item_selection_failed", stage="item")
    return item_id


# ---------------------------------------------------------------------------
# Identity issuance / revocation (one row, exact correlation binding)
# ---------------------------------------------------------------------------


def _token_name(correlation: str) -> str:
    return f"{TOKEN_NAME_PREFIX}{correlation}"


def _issue_identity(plan: ReadOncePlan, raw_token: str) -> int:
    from werkzeug.security import generate_password_hash

    token_hash = generate_password_hash(raw_token, method="pbkdf2:sha256")
    now = _utcnow().strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = sqlite3.connect(plan.database.master_path, timeout=10)
    except sqlite3.Error as exc:
        raise ReadOnceError("identity_issue_failed", stage="identity") from exc
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        cursor = conn.execute(
            "INSERT INTO agent_tokens "
            "(name, token_hash, token_prefix, created_by, created_at, "
            "allowed_read_paths_json, allowed_write_paths_json, "
            "allowed_warehouse_codes_json, rate_limit) "
            "VALUES (?, ?, ?, ?, ?, ?, '[]', ?, ?)",
            (
                _token_name(plan.correlation),
                token_hash,
                raw_token[:8],
                plan.created_by_user_id,
                now,
                json.dumps(list(TOKEN_READ_PATHS)),
                json.dumps([plan.database.warehouse_code]),
                plan.rate_limit,
            ),
        )
        token_id = cursor.lastrowid
        conn.commit()
    except sqlite3.Error as exc:
        raise ReadOnceError("identity_issue_failed", stage="identity") from exc
    finally:
        conn.close()
    if isinstance(token_id, bool) or not isinstance(token_id, int) or token_id <= 0:
        raise ReadOnceError("identity_issue_failed", stage="identity")
    return token_id


def _revoke_identity(plan: ReadOncePlan, token_id: int) -> bool:
    """Revoke only the row created for this correlation, then prove it."""
    name = _token_name(plan.correlation)
    now = _utcnow().strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = sqlite3.connect(plan.database.master_path, timeout=10)
        try:
            cursor = conn.execute(
                "UPDATE agent_tokens SET revoked_at = ? "
                "WHERE id = ? AND name = ? AND revoked_at IS NULL "
                "AND allowed_write_paths_json = '[]' "
                "AND allowed_read_paths_json = ? "
                "AND allowed_warehouse_codes_json = ?",
                (
                    now,
                    token_id,
                    name,
                    json.dumps(list(TOKEN_READ_PATHS)),
                    json.dumps([plan.database.warehouse_code]),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    if cursor.rowcount != 1:
        return False
    try:
        read = sqlite3.connect(f"file:{plan.database.master_path}?mode=ro", uri=True, timeout=10)
        try:
            row = read.execute(
                "SELECT revoked_at FROM agent_tokens WHERE id = ? AND name = ?",
                (token_id, name),
            ).fetchone()
        finally:
            read.close()
    except sqlite3.Error:
        return False
    return bool(row and row[0])


def _recover_uniquely_bound_identity(plan: ReadOncePlan, receipt: dict) -> int | None:
    """Crash-gap cleanup: only the row uniquely named for this correlation."""
    if receipt.get("token_id") is not None:
        return None
    name = receipt.get("token_name")
    if name != _token_name(plan.correlation):
        return None
    try:
        conn = sqlite3.connect(plan.database.master_path, timeout=10)
        try:
            rows = conn.execute(
                "SELECT id FROM agent_tokens WHERE name = ? AND revoked_at IS NULL "
                "AND allowed_write_paths_json = '[]' "
                "AND allowed_read_paths_json = ? "
                "AND allowed_warehouse_codes_json = ?",
                (
                    name,
                    json.dumps(list(TOKEN_READ_PATHS)),
                    json.dumps([plan.database.warehouse_code]),
                ),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if len(rows) != 1:
        return None
    token_id = rows[0][0]
    if _revoke_identity(plan, token_id):
        return token_id
    return None


# ---------------------------------------------------------------------------
# Policy plus the real MCP stdio job
# ---------------------------------------------------------------------------


def _write_policy(plan: ReadOncePlan, token_id: int) -> tuple[Path, Path]:
    try:
        directory = Path(tempfile.mkdtemp(prefix="dailycheck-read-once-"))
    except OSError as exc:
        raise ReadOnceError("policy_write_failed", stage="policy") from exc
    try:
        os.chmod(directory, 0o700)
        now = _utcnow()
        expiry = min(plan.expires_at, now + timedelta(seconds=MAX_POLICY_SECONDS))
        if expiry <= now:
            raise ReadOnceError("policy_deadline_exhausted", stage="policy")
        payload = {
            "schema_version": 1,
            "token_id": token_id,
            "expires_at": _iso(expiry),
            "master_path": str(plan.database.master_path.resolve(strict=True)),
            "warehouse_root": str(plan.database.warehouse_root.resolve(strict=True)),
            "warehouses": {
                plan.database.warehouse_code: str(plan.database.warehouse_file.resolve(strict=True))
            },
        }
        path = directory / "policy.json"
        fd = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC, 0o600
        )
        try:
            _write_all(fd, json.dumps(payload, sort_keys=True).encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        return directory, path
    except (OSError, RuntimeError) as exc:
        shutil.rmtree(directory, ignore_errors=True)
        raise ReadOnceError("policy_write_failed", stage="policy") from exc
    except ReadOnceError:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def _child_env(plan: ReadOncePlan, raw_token: str) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", ""),
        "LANG": "C.UTF-8",
        "PYTHONPATH": str(plan.source.repository),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "DAILYCHECK_DB_DIR": str(plan.database.master_path.parent),
        "DAILYCHECK_MCP_TOKEN": raw_token,
        "DAILYCHECK_SECRET_KEY": secrets.token_urlsafe(32),
    }


def _validate_sdk() -> None:
    """Validate the SDK seam before any identity is created."""
    try:
        import mcp.client.stdio as sdk_stdio  # noqa: F401
        from mcp import ClientSession  # noqa: F401
    except ImportError as exc:
        raise ReadOnceError("mcp_sdk_unavailable", stage="sdk") from exc
    import inspect

    seam = getattr(sdk_stdio, "_create_platform_compatible_process", None)
    if seam is None or not inspect.iscoroutinefunction(seam):
        raise ReadOnceError("mcp_sdk_incompatible", stage="sdk")
    parameters = inspect.signature(seam).parameters
    if "command" not in parameters or "args" not in parameters:
        raise ReadOnceError("mcp_sdk_incompatible", stage="sdk")


class _SpawnObserver:
    """Scoped observer around the SDK spawn seam; returns the real process."""

    def __init__(self, module) -> None:
        self.module = module
        self.original = module.__dict__["_create_platform_compatible_process"]
        self.process = None

    def install(self) -> None:
        import functools

        original = self.original

        async def observed(*args, **kwargs):
            process = await original(*args, **kwargs)
            self.process = process
            return process

        functools.update_wrapper(observed, original)
        self.module._create_platform_compatible_process = observed

    def restore(self) -> None:
        self.module._create_platform_compatible_process = self.original


@dataclass
class ChildState:
    pid: int | None = None
    start_time: str | None = None
    returncode: int | None = None
    waited: bool = False
    echild: bool = False
    forced: bool = False
    timed_out: bool = False
    cleaned: bool = False


async def _finalize_child(process, state: ChildState) -> None:
    import anyio

    if process is None:
        state.cleaned = True
        return
    state.pid = process.pid
    state.start_time = _process_start_time(process.pid)
    with anyio.CancelScope(shield=True):
        stdin = getattr(process, "stdin", None)
        if stdin is not None:
            with contextlib.suppress(Exception):
                await stdin.aclose()
        if process.returncode is None:
            state.forced = True
            with anyio.move_on_after(2.0):
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                try:
                    await process.wait()
                except (ProcessLookupError, ChildProcessError):
                    state.echild = True
        if process.returncode is None:
            state.forced = True
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            try:
                await process.wait()
            except (ProcessLookupError, ChildProcessError):
                state.echild = True
        state.returncode = process.returncode
        state.waited = process.returncode is not None or state.echild
        state.cleaned = state.waited


def _decode_tool_result(result) -> object:
    if getattr(result, "isError", None) is True:
        raise ReadOnceError("mcp_tool_error", stage="mcp", exit_code=EXIT_TRANSPORT)
    content = getattr(result, "content", None)
    if not isinstance(content, list) or len(content) != 1:
        raise ReadOnceError("mcp_payload_invalid", stage="mcp", exit_code=EXIT_TRANSPORT)
    text = getattr(content[0], "text", None)
    if not isinstance(text, str):
        raise ReadOnceError("mcp_payload_invalid", stage="mcp", exit_code=EXIT_TRANSPORT)
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise ReadOnceError("mcp_payload_invalid", stage="mcp", exit_code=EXIT_TRANSPORT) from exc
    if isinstance(payload, dict) and "error" in payload:
        raise ReadOnceError("mcp_business_error", stage="mcp", exit_code=EXIT_TRANSPORT)
    return payload


async def _mcp_job(
    plan: ReadOncePlan,
    policy_path: Path,
    raw_token: str,
    item_id: int,
    deadline: float,
    child: ChildState,
) -> tuple[dict, list[dict]]:
    import anyio
    import mcp.client.stdio as sdk_stdio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    observer = _SpawnObserver(sdk_stdio)
    calls: list[dict] = []
    item: dict | None = None
    params = StdioServerParameters(
        command=str(plan.source.python),
        args=[
            "-B",
            "-m",
            plan.source.module,
            "--transport",
            "stdio",
            "--readonly-policy",
            str(policy_path),
        ],
        env=_child_env(plan, raw_token),
        cwd=str(plan.source.repository),
    )
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ReadOnceError("budget_exhausted", stage="mcp", exit_code=EXIT_TRANSPORT)
    try:
        observer.install()
        with anyio.fail_after(remaining):
            async with stdio_client(params, errlog=subprocess.DEVNULL) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    names = tuple(tool.name for tool in listed.tools)
                    if names != STRICT_TOOL_NAMES:
                        raise ReadOnceError(
                            "mcp_toolset_mismatch", stage="mcp", exit_code=EXIT_TRANSPORT
                        )

                    started = _iso(_utcnow())
                    warehouses = _decode_tool_result(
                        await session.call_tool("warehouse_list", {})
                    )
                    if (
                        not isinstance(warehouses, list)
                        or len(warehouses) != 1
                        or set(warehouses[0]) != {"code", "name"}
                        or warehouses[0]["code"] != plan.database.warehouse_code
                    ):
                        raise ReadOnceError(
                            "mcp_warehouse_scope_mismatch", stage="mcp", exit_code=EXIT_TRANSPORT
                        )
                    calls.append(
                        {
                            "tool": "warehouse_list",
                            "arguments": {},
                            "started_at": started,
                            "records": 1,
                        }
                    )

                    started = _iso(_utcnow())
                    detail = _decode_tool_result(
                        await session.call_tool(
                            "items_detail",
                            {
                                "item_id": item_id,
                                "warehouse_code": plan.database.warehouse_code,
                            },
                        )
                    )
                    if not isinstance(detail, list) or len(detail) != 1:
                        raise ReadOnceError(
                            "mcp_item_shape_mismatch", stage="mcp", exit_code=EXIT_TRANSPORT
                        )
                    candidate = detail[0]
                    if not isinstance(candidate, dict) or set(candidate) != set(ITEM_DETAIL_FIELDS):
                        raise ReadOnceError(
                            "mcp_item_shape_mismatch", stage="mcp", exit_code=EXIT_TRANSPORT
                        )
                    if candidate.get("id") != item_id:
                        raise ReadOnceError(
                            "mcp_item_identity_mismatch", stage="mcp", exit_code=EXIT_TRANSPORT
                        )
                    calls.append(
                        {
                            "tool": "items_detail",
                            "arguments": {
                                "item_id": item_id,
                                "warehouse_code": plan.database.warehouse_code,
                            },
                            "started_at": started,
                            "records": 1,
                        }
                    )
                    item = dict(candidate)
    except TimeoutError as exc:
        child.timed_out = True
        raise ReadOnceError("mcp_timeout", stage="mcp", exit_code=EXIT_TRANSPORT) from exc
    except ReadOnceError:
        raise
    except Exception as exc:  # noqa: BLE001 - transport failures are fail-closed
        raise ReadOnceError("mcp_transport_failed", stage="mcp", exit_code=EXIT_TRANSPORT) from exc
    finally:
        observer.restore()
        await _finalize_child(observer.process, child)
    if item is None:
        raise ReadOnceError("mcp_no_item", stage="mcp", exit_code=EXIT_TRANSPORT)
    if child.returncode != 0 or not child.waited:
        raise ReadOnceError("mcp_child_not_clean", stage="mcp", exit_code=EXIT_TRANSPORT)
    return item, calls


# ---------------------------------------------------------------------------
# Result shaping
# ---------------------------------------------------------------------------


def _safe_corr(value: str | None) -> str | None:
    return value if isinstance(value, str) and _CORRELATION_RE.match(value) else None


def _safe_hash(value: str | None) -> str | None:
    return value if isinstance(value, str) and _SHA256_RE.match(value) else None


def _failure_payload(
    *,
    status: str,
    stage: str,
    code: str,
    correlation: str | None,
    plan_sha256: str | None,
    token_id: int | None = None,
    token_name: str | None = None,
    child: ChildState | None = None,
    cleanup: dict | None = None,
    receipt: str | None = None,
    state: str | None = None,
) -> dict:
    payload = {
        "schema": RESULT_SCHEMA,
        "version": SCHEMA_VERSION,
        "status": status,
        "stage": stage,
        "code": code,
        "correlation": _safe_corr(correlation),
        "plan_sha256": _safe_hash(plan_sha256),
        "claim_receipt": receipt,
        "claim_state": state,
        "token": {"id": token_id, "name": token_name} if token_id or token_name else None,
        "child": _child_payload(child) if child is not None else None,
        "cleanup": cleanup,
    }
    return payload


def _child_payload(child: ChildState | None) -> dict | None:
    if child is None:
        return None
    return {
        "pid": child.pid,
        "start_time": child.start_time,
        "returncode": child.returncode,
        "waited": child.waited,
        "echild": child.echild,
        "forced": child.forced,
        "timed_out": child.timed_out,
    }


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# consume
# ---------------------------------------------------------------------------


def _consume(args: argparse.Namespace) -> int:
    started = time.monotonic()
    try:
        plan = load_plan(
            args.plan,
            expected_sha256=args.plan_sha256,
            expected_correlation=args.correlation,
        )
    except ReadOnceError as exc:
        _emit(
            _failure_payload(
                status="failed",
                stage=exc.stage,
                code=exc.code,
                correlation=args.correlation,
                plan_sha256=args.plan_sha256,
            )
        )
        return exc.exit_code

    deadline = started + plan.budget_seconds
    ledger = ClaimLedger(plan.claim_dir, plan.correlation, plan.plan_sha256)
    token_name = _token_name(plan.correlation)
    token_id: int | None = None
    raw_token: str | None = None
    policy_dir: Path | None = None
    fence_fd: int | None = None
    child = ChildState()
    calls: list[dict] = []
    item: dict | None = None
    failure: ReadOnceError | None = None
    stage = "claim"
    cleanup = {
        "fence_released": False,
        "policy_removed": False,
        "token_revoked": False,
        "child_waited": False,
    }

    try:
        ledger.claim()
    except ReadOnceError as exc:
        receipt = ledger.load_existing()
        if receipt:
            recovered = _recover_uniquely_bound_identity(plan, receipt)
            state = receipt.get("state")
            _emit(
                _failure_payload(
                    status="replay_rejected",
                    stage="claim",
                    code=exc.code,
                    correlation=plan.correlation,
                    plan_sha256=plan.plan_sha256,
                    token_id=recovered,
                    token_name=receipt.get("token_name"),
                    receipt=str(ledger.path),
                    state=state,
                )
            )
        else:
            _emit(
                _failure_payload(
                    status="failed",
                    stage="claim",
                    code=exc.code,
                    correlation=plan.correlation,
                    plan_sha256=plan.plan_sha256,
                )
            )
        return exc.exit_code

    try:
        ledger.update(token_name=token_name)
        stage = "fence"
        fence_fd, fence_evidence = _acquire_fence(plan)
        stage = "source"
        source_evidence = _verify_source(plan)
        stage = "anchor"
        anchor_evidence, _deployment_root = _verify_anchor(plan)
        stage = "binding"
        binding_evidence = _verify_active_business(plan)
        stage = "item"
        item_id = _select_item(plan)
        stage = "reading"
        ledger.advance("reading", stage="reading")
        _validate_sdk()
        raw_token = secrets.token_urlsafe(32)
        stage = "identity"
        token_id = _issue_identity(plan, raw_token)
        ledger.update(token_id=token_id, token_name=token_name)
        stage = "policy"
        policy_dir, policy_path = _write_policy(plan, token_id)
        stage = "mcp"
        item, calls = _run_mcp(plan, policy_path, raw_token, item_id, deadline, child)
    except ReadOnceError as exc:
        failure = exc
    except Exception:  # noqa: BLE001 - never leak details
        failure = ReadOnceError("internal_error", stage=stage, exit_code=EXIT_PREFLIGHT)

    # ----- cleanup, in the fixed order: revoke, child, temp, fence -----
    if token_id is not None and raw_token is not None:
        stage = "revoke"
        try:
            cleanup["token_revoked"] = _revoke_identity(plan, token_id)
        except Exception:  # noqa: BLE001
            cleanup["token_revoked"] = False
    else:
        cleanup["token_revoked"] = True
    cleanup["child_waited"] = child.cleaned and child.waited
    stage = "temp"
    cleanup["policy_removed"] = _remove_policy_dir(policy_dir)
    stage = "fence"
    cleanup["fence_released"] = _release_fence(fence_fd)
    fence_fd = None

    all_clean = all(cleanup.values())
    try:
        current_state = ledger.data.get("state")
        final_state = "succeeded" if failure is None and all_clean else "reconcile_required"
        if current_state == "claimed":
            ledger.advance("preflight-rejected", stage=failure.stage if failure else "preflight")
            final_state = "preflight-rejected"
        elif current_state == "reading":
            ledger.advance("cleaning", stage="cleanup")
            current_state = "cleaning"
        if current_state == "cleaning":
            ledger.advance(final_state, stage="cleanup")
    except ReadOnceError:
        if failure is None:
            failure = ReadOnceError("claim_state_failed", stage="cleanup", exit_code=EXIT_RECONCILE)

    if failure is not None:
        _emit(
            _failure_payload(
                status="failed",
                stage=failure.stage,
                code=failure.code,
                correlation=plan.correlation,
                plan_sha256=plan.plan_sha256,
                token_id=token_id,
                token_name=token_name if token_id is not None else None,
                child=child,
                cleanup=cleanup,
                receipt=str(ledger.path),
                state=ledger.data.get("state"),
            )
        )
        return failure.exit_code
    if not all_clean:
        _emit(
            _failure_payload(
                status="reconcile_required",
                stage="cleanup",
                code="cleanup_incomplete",
                correlation=plan.correlation,
                plan_sha256=plan.plan_sha256,
                token_id=token_id,
                token_name=token_name if token_id is not None else None,
                child=child,
                cleanup=cleanup,
                receipt=str(ledger.path),
                state=ledger.data.get("state"),
            )
        )
        return EXIT_RECONCILE

    result = {
        "schema": RESULT_SCHEMA,
        "version": SCHEMA_VERSION,
        "status": "succeeded",
        "correlation": plan.correlation,
        "plan_sha256": plan.plan_sha256,
        "identity": plan.identity,
        "source": source_evidence,
        "anchor": anchor_evidence,
        "data": {
            "master_path": str(plan.database.master_path),
            "warehouse_code": plan.database.warehouse_code,
            "warehouse_file": str(plan.database.warehouse_file),
            "master_identity": _stat_identity(plan.database.master_path),
            "warehouse_identity": _stat_identity(plan.database.warehouse_file),
        },
        "binding": binding_evidence,
        "mcp": {
            "server_pid": child.pid,
            "server_start_time": child.start_time,
            "server_returncode": child.returncode,
            "child_waited": child.waited,
            "echild": child.echild,
            "forced": child.forced,
            "tools": list(STRICT_TOOL_NAMES),
            "calls": calls,
        },
        "declared_fields": list(ITEM_DETAIL_FIELDS),
        "item": item,
        "token": {
            "id": token_id,
            "revoked": cleanup["token_revoked"],
            "revocation_verified": cleanup["token_revoked"],
        },
        "cleanup": cleanup,
        "claim": {"receipt": str(ledger.path), "state": ledger.data.get("state")},
        "limitations": _limitations(plan, binding_evidence),
    }
    _emit(result)
    return EXIT_OK


def _run_mcp(
    plan: ReadOncePlan,
    policy_path: Path,
    raw_token: str,
    item_id: int,
    deadline: float,
    child: ChildState,
) -> tuple[dict, list[dict]]:
    import anyio

    return anyio.run(_mcp_job, plan, policy_path, raw_token, item_id, deadline, child)


def _remove_policy_dir(directory: Path | None) -> bool:
    if directory is None:
        return True
    try:
        shutil.rmtree(directory)
    except OSError:
        return False
    return not directory.exists()


def _limitations(plan: ReadOncePlan, binding: dict) -> list[str]:
    items = [
        "The existing agent_tokens schema has no universal expiry: the strict policy "
        "deadline plus verified revocation bound this identity; parent death before "
        "revocation is an operator recovery requirement, not a guaranteed global TTL.",
        "The deployment fence/anchor and active-source/data locators must be supplied "
        "and revalidated by the future authorized Prod operator against the real "
        "deployment contract; this run does not establish a production mapping.",
    ]
    if plan.identity == "synthetic":
        items.append("identity=synthetic: this result is local proof, not production authority.")
    if binding.get("kind") == "synthetic-surrogate":
        items.append(
            "Non-Linux active-business binding uses a labeled synthetic surrogate; "
            "actual /proc binding is covered only on Linux."
        )
    return items


# ---------------------------------------------------------------------------
# relay
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RelayHost:
    alias: str
    hostname: str
    port: int
    public_key: str
    fingerprint: str


@dataclass(frozen=True)
class RelayRequest:
    correlation: str
    plan_sha256: str
    host: RelayHost
    python: Path
    repository: Path
    plan_path: Path
    timeout_seconds: int


def _build_relay_request(data: object, *, request_sha256: str) -> RelayRequest:
    request = _mapping(data, "relay")
    _exact_keys(
        request,
        {"schema", "version", "correlation", "plan_sha256", "host", "remote", "timeout_seconds"},
        "relay",
    )
    if request["schema"] != RELAY_SCHEMA:
        raise ReadOnceError("relay_schema_invalid", stage="relay")
    if request["version"] != SCHEMA_VERSION or isinstance(request["version"], bool):
        raise ReadOnceError("relay_version_invalid", stage="relay")
    correlation = _correlation(request["correlation"])
    plan_sha = _sha256(request["plan_sha256"], "plan_sha256")
    timeout_seconds = _integer(request["timeout_seconds"], "timeout_seconds", minimum=1, maximum=MAX_RELAY_TIMEOUT)

    host = _mapping(request["host"], "host")
    _exact_keys(host, {"alias", "hostname", "port", "public_key", "fingerprint"}, "host")
    alias = _text(host["alias"], "host.alias", max_len=64)
    if alias != RELAY_HOST_ALIAS:
        raise ReadOnceError("host_alias_mismatch", stage="relay")
    hostname = _text(host["hostname"], "host.hostname", max_len=255)
    if not _HOSTNAME_RE.match(hostname):
        raise ReadOnceError("host.hostname_invalid", stage="relay")
    port = _integer(host["port"], "host.port", minimum=1, maximum=65535)
    public_key = _text(host["public_key"], "host.public_key", max_len=4096)
    fingerprint = _text(host["fingerprint"], "host.fingerprint", max_len=64)
    if not _FINGERPRINT_RE.match(fingerprint):
        raise ReadOnceError("host.fingerprint_invalid", stage="relay")
    if _fingerprint(public_key) != fingerprint:
        raise ReadOnceError("host.fingerprint_mismatch", stage="relay")

    remote = _mapping(request["remote"], "remote")
    _exact_keys(
        remote,
        {"python", "repository", "plan_path", "module", "correlation", "plan_sha256"},
        "remote",
    )
    module = _text(remote["module"], "remote.module", max_len=64)
    if module != RELAY_MODULE:
        raise ReadOnceError("remote_module_invalid", stage="relay")
    if _correlation(remote["correlation"], "remote.correlation") != correlation:
        raise ReadOnceError("remote_correlation_mismatch", stage="relay")
    if _sha256(remote["plan_sha256"], "remote.plan_sha256") != plan_sha:
        raise ReadOnceError("remote_plan_hash_mismatch", stage="relay")
    return RelayRequest(
        correlation=correlation,
        plan_sha256=plan_sha,
        host=RelayHost(
            alias=alias,
            hostname=hostname,
            port=port,
            public_key=public_key,
            fingerprint=fingerprint,
        ),
        python=_remote_path(remote["python"], "remote.python"),
        repository=_remote_path(remote["repository"], "remote.repository"),
        plan_path=_remote_path(remote["plan_path"], "remote.plan_path"),
        timeout_seconds=timeout_seconds,
    )


def _fingerprint(public_key: str) -> str:
    parts = public_key.split()
    if len(parts) < 2:
        raise ReadOnceError("host.public_key_invalid", stage="relay")
    key_type, encoded = parts[0], parts[1]
    if not _KEY_TYPE_RE.match(key_type):
        raise ReadOnceError("host.public_key_invalid", stage="relay")
    try:
        blob = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise ReadOnceError("host.public_key_invalid", stage="relay") from exc
    if len(blob) < 4 or blob[4 : 4 + len(key_type)].decode("ascii", "ignore") != key_type:
        raise ReadOnceError("host.public_key_invalid", stage="relay")
    digest = hashlib.sha256(blob).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def _known_hosts_line(host: RelayHost) -> str:
    parts = host.public_key.split()
    key_type, encoded = parts[0], parts[1]
    pattern = host.hostname if host.port == 22 else f"[{host.hostname}]:{host.port}"
    return f"{pattern} {key_type} {encoded}\n"


def _ssh_resolve(ssh: str, alias: str) -> tuple[str, int]:
    try:
        result = subprocess.run(
            [ssh, "-G", alias],
            capture_output=True,
            text=True,
            timeout=15,
            env=_tool_env(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReadOnceError("ssh_resolve_failed", stage="relay", exit_code=EXIT_RELAY) from exc
    if result.returncode != 0:
        raise ReadOnceError("ssh_resolve_failed", stage="relay", exit_code=EXIT_RELAY)
    resolved: dict[str, str] = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            resolved[parts[0].lower()] = parts[1].strip()
    hostname = resolved.get("hostname")
    port_text = resolved.get("port")
    if not hostname or not port_text:
        raise ReadOnceError("ssh_resolve_failed", stage="relay", exit_code=EXIT_RELAY)
    for key in ("proxycommand", "proxyjump"):
        value = resolved.get(key, "")
        if value and value.lower() != "none":
            raise ReadOnceError("ssh_proxy_override_refused", stage="relay", exit_code=EXIT_RELAY)
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ReadOnceError("ssh_resolve_failed", stage="relay", exit_code=EXIT_RELAY) from exc
    return hostname, port


def _remote_command(request: RelayRequest) -> str:
    for value in (str(request.python), str(request.repository), str(request.plan_path)):
        if _CONTROL_RE.search(value) or not Path(value).is_absolute():
            raise ReadOnceError("remote_command_unsafe", stage="relay")
    command = [
        str(request.python),
        "-B",
        "-m",
        RELAY_MODULE,
        "consume",
        "--plan",
        str(request.plan_path),
        "--plan-sha256",
        request.plan_sha256,
        "--correlation",
        request.correlation,
    ]
    quoted = " ".join(shlex.quote(part) for part in command)
    return f"cd {shlex.quote(str(request.repository))} && exec {quoted}"


def _relay(args: argparse.Namespace) -> int:
    try:
        expected_hash = _sha256(args.request_sha256, "request_sha256")
        expected_corr = _correlation(args.correlation)
        data = _read_protected_bytes(Path(args.request), max_bytes=MAX_PLAN_BYTES, private=True)
        actual_hash = hashlib.sha256(data).hexdigest()
        if actual_hash != expected_hash:
            raise ReadOnceError("request_hash_mismatch", stage="relay")
        request = _build_relay_request(_parse_json_bytes(data, "relay"), request_sha256=actual_hash)
        if request.correlation != expected_corr:
            raise ReadOnceError("request_correlation_mismatch", stage="relay")

        ssh = shutil.which("ssh", path=_tool_env()["PATH"])
        if ssh is None:
            raise ReadOnceError("ssh_unavailable", stage="relay", exit_code=EXIT_RELAY)
        resolved_host, resolved_port = _ssh_resolve(ssh, request.host.alias)
        if resolved_host.lower() != request.host.hostname.lower() or resolved_port != request.host.port:
            raise ReadOnceError("ssh_destination_mismatch", stage="relay", exit_code=EXIT_RELAY)

        try:
            directory = Path(tempfile.mkdtemp(prefix="dailycheck-read-once-relay-"))
        except OSError as exc:
            raise ReadOnceError(
                "relay_temp_failed", stage="relay", exit_code=EXIT_RELAY
            ) from exc
        known_hosts = directory / "known_hosts"
        try:
            try:
                os.chmod(directory, 0o700)
                fd = os.open(
                    known_hosts,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC,
                    0o600,
                )
                try:
                    _write_all(fd, _known_hosts_line(request.host).encode())
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError as exc:
                raise ReadOnceError(
                    "known_hosts_write_failed", stage="relay", exit_code=EXIT_RELAY
                ) from exc
            argv = [
                ssh,
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                f"UserKnownHostsFile={known_hosts}",
                "-o",
                "GlobalKnownHostsFile=/dev/null",
                "-o",
                "UpdateHostKeys=no",
                "-o",
                "CheckHostIP=no",
                "-o",
                "ForwardAgent=no",
                "-o",
                "ForwardX11=no",
                "-o",
                "ForwardX11Trusted=no",
                "-o",
                "ClearAllForwardings=yes",
                "-o",
                "PermitLocalCommand=no",
                "-o",
                "RequestTTY=no",
                "-o",
                "ControlMaster=no",
                "-o",
                "ControlPath=none",
                "-o",
                f"ConnectTimeout={min(request.timeout_seconds, 20)}",
                "-o",
                f"HostName={request.host.hostname}",
                "-p",
                str(request.host.port),
                request.host.alias,
                _remote_command(request),
            ]
            try:
                process = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                    env=_tool_env(),
                )
            except OSError as exc:
                raise ReadOnceError(
                    "ssh_spawn_failed", stage="relay", exit_code=EXIT_RELAY
                ) from exc
            try:
                out, _err = process.communicate(timeout=request.timeout_seconds)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                _emit(
                    _failure_payload(
                        status="unknown",
                        stage="ssh",
                        code="transport_timeout",
                        correlation=request.correlation,
                        plan_sha256=request.plan_sha256,
                    )
                )
                return EXIT_TRANSPORT
            if process.returncode != 0:
                code = (
                    "ssh_hostkey_rejected"
                    if b"host key verification failed" in (_err or b"").lower()
                    else "ssh_failed"
                )
                _emit(
                    _failure_payload(
                        status="failed",
                        stage="ssh",
                        code=code,
                        correlation=request.correlation,
                        plan_sha256=request.plan_sha256,
                    )
                )
                return EXIT_RELAY
            if len(out) > MAX_REMOTE_BYTES:
                raise ReadOnceError("remote_result_too_large", stage="result", exit_code=EXIT_RELAY)
            remote = _decode_remote_result(out, request)
        finally:
            shutil.rmtree(directory, ignore_errors=True)
        _emit(remote)
        if remote.get("status") == "succeeded":
            return EXIT_OK
        if remote.get("status") == "reconcile_required":
            return EXIT_RECONCILE
        return EXIT_TRANSPORT
    except ReadOnceError as exc:
        _emit(
            _failure_payload(
                status="failed",
                stage=exc.stage,
                code=exc.code,
                correlation=args.correlation,
                plan_sha256=args.request_sha256,
            )
        )
        return exc.exit_code


def _decode_remote_result(output: bytes, request: RelayRequest) -> dict:
    try:
        text = output.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReadOnceError("remote_result_invalid", stage="result", exit_code=EXIT_RELAY) from exc
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise ReadOnceError("remote_result_invalid", stage="result", exit_code=EXIT_RELAY)
    try:
        payload = json.loads(lines[0])
    except ValueError as exc:
        raise ReadOnceError("remote_result_invalid", stage="result", exit_code=EXIT_RELAY) from exc
    if not isinstance(payload, dict) or payload.get("schema") != RESULT_SCHEMA:
        raise ReadOnceError("remote_result_invalid", stage="result", exit_code=EXIT_RELAY)
    if payload.get("correlation") != request.correlation:
        raise ReadOnceError("remote_correlation_mismatch", stage="result", exit_code=EXIT_RELAY)
    if payload.get("plan_sha256") != request.plan_sha256:
        raise ReadOnceError("remote_plan_hash_mismatch", stage="result", exit_code=EXIT_RELAY)
    if payload.get("status") != "succeeded":
        return payload
    cleanup = payload.get("cleanup")
    token = payload.get("token")
    mcp = payload.get("mcp")
    item = payload.get("item")
    if (
        not isinstance(cleanup, dict)
        or not all(cleanup.get(key) is True for key in ("token_revoked", "child_waited", "policy_removed", "fence_released"))
        or not isinstance(token, dict)
        or token.get("revoked") is not True
        or token.get("revocation_verified") is not True
        or not isinstance(mcp, dict)
        or mcp.get("server_returncode") != 0
        or mcp.get("child_waited") is not True
        or not isinstance(item, dict)
        or not isinstance(item.get("id"), int)
    ):
        raise ReadOnceError("remote_cleanup_unproven", stage="result", exit_code=EXIT_RELAY)
    return payload


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp_lucy_read_once",
        description="One-shot Lucy read runner (Issue #16, default off).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    consume = subparsers.add_parser("consume", help="host-local protected plan")
    consume.add_argument("--plan", required=True, help="protected plan path")
    consume.add_argument("--plan-sha256", required=True, help="expected plan SHA-256")
    consume.add_argument("--correlation", required=True, help="expected correlation")

    relay = subparsers.add_parser("relay", help="Mac-SSH relay for one consume")
    relay.add_argument("--request", required=True, help="protected relay request path")
    relay.add_argument("--request-sha256", required=True, help="expected request SHA-256")
    relay.add_argument("--correlation", required=True, help="expected correlation")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "consume":
        return _consume(args)
    return _relay(args)


if __name__ == "__main__":
    raise SystemExit(main())
