"""Explicit, process-level read-only policy for the MCP server.

The policy is opt-in: when no policy is loaded the server keeps its existing
behaviour exactly.  A policy is trusted operator configuration (a JSON file)
that pins one existing ``agent_tokens`` row, a finite UTC deadline, the
canonical master path, a trusted warehouse root and an explicit
``code -> canonical file`` mapping.  It never contains a secret: the caller
still presents its bearer token and it is authenticated against the pinned row.

This module also owns the guarded SQLite connection used in strict mode:
``mode=ro`` plus ``PRAGMA query_only`` plus an authorizer that denies writes,
DDL, ``ATTACH``/``DETACH`` and extension loading.  ``mode=ro`` alone does not
block ``ATTACH`` of a different, writable database.
"""
from __future__ import annotations

import contextvars
import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType

POLICY_SCHEMA_VERSION = 1

#: The only tools a strict policy exposes.  The registry itself is unchanged.
STRICT_TOOL_NAMES: tuple[str, ...] = ("warehouse_list", "items_list", "items_detail")

#: Fixed safe audit marker for a tool rejected by the strict allowlist.  A
#: caller-controlled name is never copied into strict logs or errors.
STRICT_REJECTED_TOOL_LOG_NAME = "restricted"

_REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "token_id",
        "expires_at",
        "master_path",
        "warehouse_root",
        "warehouses",
    }
)

#: Exact argument sets accepted by the three strict tools.  Anything else
#: (SQL, paths, data roots, read_only/profile overrides) is rejected before a
#: handler runs or a business database is opened.
_STRICT_ARGUMENT_TYPES: dict[str, dict[str, type]] = {
    "warehouse_list": {},
    "items_list": {"warehouse_code": str},
    "items_detail": {"item_id": int, "warehouse_code": str},
}

_DENIED_SQLITE_ACTION_NAMES = (
    "SQLITE_INSERT",
    "SQLITE_UPDATE",
    "SQLITE_DELETE",
    "SQLITE_CREATE_INDEX",
    "SQLITE_CREATE_TABLE",
    "SQLITE_CREATE_TEMP_INDEX",
    "SQLITE_CREATE_TEMP_TABLE",
    "SQLITE_CREATE_TEMP_TRIGGER",
    "SQLITE_CREATE_TEMP_VIEW",
    "SQLITE_CREATE_TRIGGER",
    "SQLITE_CREATE_VIEW",
    "SQLITE_DROP_INDEX",
    "SQLITE_DROP_TABLE",
    "SQLITE_DROP_TEMP_INDEX",
    "SQLITE_DROP_TEMP_TABLE",
    "SQLITE_DROP_TEMP_TRIGGER",
    "SQLITE_DROP_TEMP_VIEW",
    "SQLITE_DROP_TRIGGER",
    "SQLITE_DROP_VIEW",
    "SQLITE_ALTER_TABLE",
    "SQLITE_REINDEX",
    "SQLITE_ANALYZE",
    "SQLITE_ATTACH",
    "SQLITE_DETACH",
    "SQLITE_PRAGMA",
)
_DENIED_SQLITE_ACTIONS = frozenset(
    getattr(sqlite3, name)
    for name in _DENIED_SQLITE_ACTION_NAMES
    if hasattr(sqlite3, name)
)
_DENIED_FUNCTIONS = frozenset({"load_extension", "writefile", "readfile", "edit"})
_SQLITE_FUNCTION = getattr(sqlite3, "SQLITE_FUNCTION", None)


class ReadonlyPolicyError(RuntimeError):
    """Policy load/validation failure.

    Messages name the offending field, never a configured value, so a startup
    failure does not echo a path or credential into logs.
    """


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_deadline(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ReadonlyPolicyError("expires_at must be a non-empty ISO-8601 string")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ReadonlyPolicyError("expires_at is not valid ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ReadonlyPolicyError("expires_at must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _path_string(value: object, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ReadonlyPolicyError(f"{field} must be a non-empty path string")
    path = Path(value)
    if not path.is_absolute():
        raise ReadonlyPolicyError(f"{field} must be an absolute path")
    return path


def _canonical_root(value: object) -> Path:
    path = _path_string(value, "warehouse_root")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ReadonlyPolicyError("warehouse_root does not exist") from exc
    if not resolved.is_dir():
        raise ReadonlyPolicyError("warehouse_root is not a directory")
    return resolved


def _canonical_file(value: object, field: str) -> Path:
    path = _path_string(value, field)
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ReadonlyPolicyError(f"{field} does not exist") from exc
    if not resolved.is_file():
        raise ReadonlyPolicyError(f"{field} is not a file")
    return resolved


@dataclass(frozen=True)
class ReadonlyPolicy:
    """Immutable strict-mode policy.  See module docstring for the trust model."""

    token_id: int
    expires_at: datetime
    master_path: Path
    warehouse_root: Path
    warehouses: Mapping[str, Path]
    schema_version: int = POLICY_SCHEMA_VERSION

    # ------------------------------------------------------------------ load

    @classmethod
    def load(cls, path: str | Path) -> ReadonlyPolicy:
        """Load and validate a policy.  Any failure raises, never falls back."""
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ReadonlyPolicyError("readonly policy file could not be read") from exc
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise ReadonlyPolicyError("readonly policy is not valid JSON") from exc
        import config as root_config

        return cls.from_mapping(data, configured_master_path=root_config.MASTER_DB)

    @classmethod
    def from_mapping(
        cls,
        data: object,
        *,
        configured_master_path: str | Path,
    ) -> ReadonlyPolicy:
        if not isinstance(data, dict):
            raise ReadonlyPolicyError("readonly policy must be a JSON object")
        keys = set(data)
        missing = _REQUIRED_FIELDS - keys
        if missing:
            raise ReadonlyPolicyError(f"missing policy field: {sorted(missing)[0]}")
        unknown = keys - _REQUIRED_FIELDS
        if unknown:
            raise ReadonlyPolicyError(f"unknown policy field: {sorted(unknown)[0]}")

        if not _is_int(data["schema_version"]) or data["schema_version"] != POLICY_SCHEMA_VERSION:
            raise ReadonlyPolicyError("unsupported policy schema_version")
        if not _is_int(data["token_id"]) or data["token_id"] <= 0:
            raise ReadonlyPolicyError("token_id must be a positive integer")

        deadline = _parse_deadline(data["expires_at"])
        if deadline <= datetime.now(timezone.utc):
            raise ReadonlyPolicyError("policy expires_at is already in the past")

        root = _canonical_root(data["warehouse_root"])
        master = _canonical_file(data["master_path"], "master_path")
        try:
            configured_master = Path(configured_master_path).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ReadonlyPolicyError("configured master does not exist") from exc
        if master != configured_master:
            raise ReadonlyPolicyError("master_path does not match the configured master")

        raw_warehouses = data["warehouses"]
        if not isinstance(raw_warehouses, dict) or not raw_warehouses:
            raise ReadonlyPolicyError("warehouses must be a non-empty object")
        warehouses: dict[str, Path] = {}
        seen_files: set[Path] = set()
        for code, value in raw_warehouses.items():
            if (
                not isinstance(code, str)
                or not code
                or code.strip() != code
                or any(ch in code for ch in ("/", "\\", "\x00"))
                or code == "*"
            ):
                raise ReadonlyPolicyError("warehouses keys must be explicit warehouse codes")
            file_path = _canonical_file(value, f"warehouses[{code}]")
            if not file_path.is_relative_to(root):
                raise ReadonlyPolicyError(f"warehouses[{code}] escapes warehouse_root")
            if file_path in seen_files:
                raise ReadonlyPolicyError("duplicate warehouse file alias")
            seen_files.add(file_path)
            warehouses[code] = file_path

        return cls(
            token_id=data["token_id"],
            expires_at=deadline,
            master_path=master,
            warehouse_root=root,
            warehouses=MappingProxyType(warehouses),
            schema_version=POLICY_SCHEMA_VERSION,
        )

    # --------------------------------------------------------------- runtime

    def is_expired(self, now: datetime | None = None) -> bool:
        moment = datetime.now(timezone.utc) if now is None else now
        return moment >= self.expires_at

    def ensure_active(self, now: datetime | None = None) -> None:
        if self.is_expired(now):
            raise ReadonlyPolicyError("readonly policy has expired")

    def approved_file(self, code: str) -> Path | None:
        return self.warehouses.get(code)


# ---------------------------------------------------------------------------
# Per-call policy context
#
# build_server() captures its policy in a closure and publishes it here for the
# duration of each tools/call.  The stdio path has no request-scoped auth, so
# resolve_ctx() uses this to authenticate the process token against the table
# instead of trusting an environment identity.  It is never an import-time
# global: a process without an explicit policy never sets it.
# ---------------------------------------------------------------------------

_current_policy: contextvars.ContextVar[ReadonlyPolicy | None] = contextvars.ContextVar(
    "dailycheck_readonly_policy", default=None
)


def set_current_policy(policy: ReadonlyPolicy):
    return _current_policy.set(policy)


def reset_current_policy(token) -> None:
    _current_policy.reset(token)


def get_current_policy() -> ReadonlyPolicy | None:
    return _current_policy.get()


def validate_strict_arguments(name: str, arguments: object) -> None:
    """Reject anything but the exact declared arguments for a strict tool.

    Runs before the handler and before any business database is opened.  Error
    messages are fixed and never echo caller-supplied keys or values.
    """
    from mcp_server.infra.errors import ForbiddenError, ValidationError

    expected = _STRICT_ARGUMENT_TYPES.get(name)
    if expected is None:
        raise ForbiddenError("forbidden_tool")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ValidationError("invalid_arguments")
    if set(arguments) != set(expected):
        raise ValidationError("invalid_arguments")
    for key, expected_type in expected.items():
        value = arguments[key]
        if expected_type is int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValidationError("invalid_arguments")
        elif isinstance(value, bool) or not isinstance(value, str):
            raise ValidationError("invalid_arguments")


# ---------------------------------------------------------------------------
# Guarded read-only SQLite connection
# ---------------------------------------------------------------------------


def _authorizer(action: int, arg1, arg2, dbname, source) -> int:
    if action in _DENIED_SQLITE_ACTIONS:
        return sqlite3.SQLITE_DENY
    if action == _SQLITE_FUNCTION and str(arg2 or "").lower() in _DENIED_FUNCTIONS:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


@contextmanager
def readonly_connection(path: str | Path) -> Iterator[sqlite3.Connection]:
    """Open an existing SQLite file read-only and deny writes/DDL/ATTACH.

    ``Path.as_uri`` percent-encodes ``?`` and ``#`` so a filename cannot turn
    into SQLite URI flags.  No file is created.  ``immutable=1`` is deliberately
    not used: it hides WAL contents and can return stale data.
    """
    target = Path(path)
    if not target.is_file():
        raise ReadonlyPolicyError("readonly source does not exist")
    uri = target.as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        setter = getattr(conn, "set_authorizer", None)
        if setter is None:  # pragma: no cover - Python < 3.11 fails closed
            raise ReadonlyPolicyError("SQLite authorizer is unavailable")
        setter(_authorizer)
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Strict-mode reads
# ---------------------------------------------------------------------------


def ensure_token_active(policy: ReadonlyPolicy, now: datetime | None = None) -> None:
    """Enforce the deadline and re-check revocation for a new operation."""
    policy.ensure_active(now=now)
    try:
        with readonly_connection(policy.master_path) as conn:
            row = conn.execute(
                "SELECT revoked_at FROM agent_tokens WHERE id = ?",
                (policy.token_id,),
            ).fetchone()
    except (sqlite3.Error, ReadonlyPolicyError) as exc:
        raise ReadonlyPolicyError("pinned token state is unavailable") from exc
    if row is None or row[0] is not None:
        raise ReadonlyPolicyError("pinned token is missing or revoked")


def read_warehouse_metadata(policy: ReadonlyPolicy, code: str) -> dict | None:
    try:
        with readonly_connection(policy.master_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT code, name, db_path, warehouse_type FROM warehouses WHERE code = ?",
                (code,),
            ).fetchone()
            return dict(row) if row else None
    except sqlite3.Error as exc:
        raise ReadonlyPolicyError("warehouse metadata is unavailable") from exc


def read_all_warehouses(policy: ReadonlyPolicy) -> list[dict]:
    try:
        with readonly_connection(policy.master_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT code, name, db_path, warehouse_type FROM warehouses ORDER BY code"
            ).fetchall()
            return [dict(row) for row in rows]
    except sqlite3.Error as exc:
        raise ReadonlyPolicyError("warehouse metadata is unavailable") from exc
