#!/usr/bin/env python3
"""Deterministic, stdlib-only webhook reducer and queue for Prod Agent.

The reducer is the only writer to its state database.  It normalizes the flat
records emitted by ``webhook_receiver.py`` (and equivalent raw Gitea payloads),
deduplicates deliveries, coalesces bursts, and serializes provider calls with a
recoverable per-PR lease.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.hosting import (  # noqa: E402
    HostingError,
    Provider,
    is_shadow_event,
    provider,
    require_production_work,
    require_shadow_path,
)
from scripts.prod_agent.merge_readiness import (  # noqa: E402
    DEFAULT_REQUIRED_CONTEXTS,
    DEFAULT_REQUIRED_CONTEXTS_ENV,
    GiteaReadinessClient,
    ReadinessConfig,
    parse_required_contexts,
)

ROUTES = {
    "check_suite": "wdg-ci",
    "deploy_result": "dailycheck-deploy",
}
SUPPORTED_EVENTS = frozenset({"pull_request", "check_suite", "deploy_result"})
EVENT_PRIORITY = {"check_suite": 20, "deploy_result": 30}
ALLOWED_ACTIONS = {
    "pull_request": frozenset({"opened", "synchronize", "synchronized", "reopened"}),
    "check_suite": frozenset({"completed"}),
    "deploy_result": frozenset({"completed"}),
}
DEFAULT_DB = Path("/var/lib/dailycheck-prod-agent/event_reducer.sqlite3")
MAX_INPUT_BYTES = 1_048_576
MAX_PROVIDER_COOLDOWN_MS = 3_600_000
DEFAULT_LEASE_TTL_MS = 1_200_000
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_REDACTIONS = (
    (
        re.compile(r"(?i)\b(authorization\s*:\s*(?:bearer|basic)\s+)[^\s,;]+"),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)\b((?:api[_-]?key|access[_-]?token|token|secret|password|passwd)"
            r"\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
        ),
        r"\1[REDACTED]",
    ),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"), "[REDACTED]"),
    (re.compile(r"\b(?:gh[pousr]|github_pat)_[A-Za-z0-9_]{8,}\b"), "[REDACTED]"),
    (re.compile(r"\bwhsec_[A-Za-z0-9_=-]{8,}\b"), "[REDACTED]"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id TEXT PRIMARY KEY,
    event_key TEXT NOT NULL,
    trigger_id TEXT,
    received_at_ms INTEGER NOT NULL,
    disposition TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pr_state (
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    checks_status TEXT NOT NULL DEFAULT '',
    review_status TEXT NOT NULL DEFAULT '',
    deploy_status TEXT NOT NULL DEFAULT '',
    last_event TEXT NOT NULL,
    next_action TEXT NOT NULL DEFAULT '',
    updated_at_ms INTEGER NOT NULL,
    merge_ready_at_ms INTEGER,
    merge_ready_trigger_id TEXT,
    gate_reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (repo, pr_number)
);

CREATE TABLE IF NOT EXISTS trigger_queue (
    trigger_id TEXT PRIMARY KEY,
    event_key TEXT NOT NULL,
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    event TEXT NOT NULL,
    action TEXT NOT NULL,
    conclusion TEXT NOT NULL,
    route TEXT NOT NULL,
    priority INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'claimed', 'done')),
    first_seen_ms INTEGER NOT NULL,
    due_at_ms INTEGER NOT NULL,
    lease_token TEXT,
    lease_expires_ms INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    collapsed_count INTEGER NOT NULL DEFAULT 1,
    last_error TEXT NOT NULL DEFAULT '',
    completed_at_ms INTEGER,
    updated_at_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trigger_queue_due
    ON trigger_queue(state, due_at_ms, first_seen_ms, trigger_id);
CREATE INDEX IF NOT EXISTS idx_trigger_queue_pr
    ON trigger_queue(repo, pr_number, state);
CREATE UNIQUE INDEX IF NOT EXISTS one_claimed_trigger_per_pr
    ON trigger_queue(repo, pr_number) WHERE state = 'claimed';

CREATE TABLE IF NOT EXISTS provider_state (
    provider TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    cooldown_until_ms INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    updated_at_ms INTEGER NOT NULL
);
"""


class ValidationError(ValueError):
    """The input cannot be safely reduced."""


def _text(value: Any, *, field: str, limit: int = 512, required: bool = True) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValidationError(f"{field} is not valid UTF-8") from exc
    value = value.strip()
    if len(value) > limit:
        raise ValidationError(f"{field} exceeds {limit} characters")
    if required and not value:
        raise ValidationError(f"{field} is required")
    return value


def redact_sensitive(value: Any, *, limit: int = 400) -> str:
    """Return bounded diagnostic text with credential-shaped values removed."""
    text = "" if value is None else str(value)
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text[:limit]


def _validate_json_tree(value: Any, *, path: str = "$", depth: int = 0) -> None:
    """Reject values Python accepts but RFC JSON cannot represent faithfully."""
    if depth > 100:
        raise ValidationError(f"{path} is nested too deeply")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValidationError(f"{path} contains an invalid Unicode surrogate") from exc
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValidationError(f"{path} contains a non-finite number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_tree(item, path=f"{path}[{index}]", depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValidationError(f"{path} contains a non-string object key")
            try:
                key.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValidationError(f"{path} contains an invalid object key") from exc
            _validate_json_tree(item, path=f"{path}.*", depth=depth + 1)
        return
    raise ValidationError(f"{path} contains a non-JSON value")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValidationError(f"non-RFC JSON number {value}")


def strict_json_loads(raw: bytes | str) -> Any:
    """Parse JSON while rejecting NaN, Infinity, overflow and duplicate keys."""
    payload = json.loads(
        raw,
        parse_constant=_reject_json_constant,
        object_pairs_hook=_strict_object,
    )
    _validate_json_tree(payload)
    return payload


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_value(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _positive_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise ValidationError(f"{field} must be a positive integer")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.isdecimal():
        result = int(value)
    else:
        raise ValidationError(f"{field} must be a positive integer")
    if result <= 0 or result > 2_147_483_647:
        raise ValidationError(f"{field} is out of range")
    return result


def event_key(event: dict[str, Any]) -> str:
    canonical = [
        event["repo_full_name"].lower(),
        event["pr_number"],
        event["pr_head_sha"].lower(),
        event["event"],
        event["action"],
        event["conclusion"].lower(),
    ]
    encoded = json.dumps(canonical, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ReducerConfig:
    db_path: Path
    debounce_ms: int = 2_000
    max_debounce_ms: int = 10_000
    lease_ttl_ms: int = DEFAULT_LEASE_TTL_MS
    provider_cooldown_ms: int = 60_000
    required_contexts: tuple[str, ...] = DEFAULT_REQUIRED_CONTEXTS


class EventReducer:
    def __init__(
        self,
        config: ReducerConfig,
        *,
        readiness_checker: Any | None = None,
    ):
        self.config = config
        self._validate_state_path()
        if readiness_checker is None:
            base = ReadinessConfig.from_environment()
            readiness_config = ReadinessConfig(
                api_url=base.api_url,
                repo=base.repo,
                token=base.token,
                required_contexts=config.required_contexts,
                timeout_s=base.timeout_s,
                curl_path=base.curl_path,
                hosting=base.hosting,
            )
            client = GiteaReadinessClient(readiness_config)

            def check_readiness(repo: str, pr_number: int, head_sha: str) -> dict[str, Any]:
                result = client.check(pr_number, head_sha, repo=repo)
                if base.hosting is not None:
                    return {**result, "ready": False, "reason": "github_shadow_only"}
                return result

            self._readiness_checker = check_readiness
        else:
            self._readiness_checker = readiness_checker

    def _validate_state_path(self) -> None:
        if provider() is Provider.GITHUB:
            for suffix in ("", "-wal", "-shm", "-journal"):
                require_shadow_path(Path(str(self.config.db_path) + suffix))

    def _connect(self) -> sqlite3.Connection:
        self._validate_state_path()
        self.config.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.config.db_path), timeout=10, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.executescript(SCHEMA)
            conn.execute("BEGIN IMMEDIATE")
            pr_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(pr_state)").fetchall()
            }
            for name, ddl in (
                ("merge_ready_at_ms", "ALTER TABLE pr_state ADD COLUMN merge_ready_at_ms INTEGER"),
                (
                    "merge_ready_trigger_id",
                    "ALTER TABLE pr_state ADD COLUMN merge_ready_trigger_id TEXT",
                ),
                (
                    "gate_reason",
                    "ALTER TABLE pr_state ADD COLUMN gate_reason TEXT NOT NULL DEFAULT ''",
                ),
            ):
                if name not in pr_columns:
                    conn.execute(ddl)
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            conn.close()
            raise
        return conn

    def normalize(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValidationError("payload must be a JSON object")
        _validate_json_tree(payload)
        event_name = _text(payload.get("event"), field="event", limit=64)
        if event_name not in SUPPORTED_EVENTS:
            raise ValidationError("event is not supported")
        action = _text(payload.get("action"), field="action", limit=64)
        if action not in ALLOWED_ACTIONS[event_name]:
            raise ValidationError(f"action {action!r} is not valid for {event_name}")

        repo_obj = _mapping(payload.get("repository"))
        pr_obj = _mapping(payload.get("pull_request"))
        check_obj = _mapping(payload.get("check_suite"))
        check_prs = check_obj.get("pull_requests")
        check_pr = _mapping(check_prs[0]) if isinstance(check_prs, list) and check_prs else {}
        check_head = _mapping(check_pr.get("head"))
        pr_head = _mapping(pr_obj.get("head"))

        repo = _text(
            _first_value(payload.get("repo_full_name"), repo_obj.get("full_name")),
            field="repo_full_name",
            limit=256,
        )
        pr_number = _positive_int(
            _first_value(
                payload.get("pr_number"),
                payload.get("number"),
                pr_obj.get("number"),
                check_pr.get("number"),
            ),
            field="pr_number",
        )
        sha = _text(
            _first_value(
                payload.get("pr_head_sha"),
                payload.get("head_sha"),
                payload.get("sha"),
                pr_head.get("sha"),
                check_obj.get("head_sha"),
                check_head.get("sha"),
            ),
            field="pr_head_sha",
            limit=64,
        )
        if not _SHA_RE.fullmatch(sha):
            raise ValidationError("pr_head_sha must be 7-64 hexadecimal characters")
        delivery_id = _text(payload.get("delivery_id"), field="delivery_id", limit=256)

        if event_name == "check_suite":
            conclusion = _text(
                _first_value(
                    payload.get("check_conclusion"),
                    check_obj.get("conclusion"),
                    payload.get("state"),
                    payload.get("status"),
                ),
                field="check_conclusion",
                limit=64,
            )
        elif event_name == "deploy_result":
            explicit = payload.get("deploy_conclusion")
            if explicit:
                conclusion = _text(explicit, field="deploy_conclusion", limit=64)
            elif payload.get("deploy_rc") is not None:
                conclusion = "success" if str(payload["deploy_rc"]) == "0" else "failure"
            else:
                conclusion = _text(
                    _first_value(payload.get("conclusion"), payload.get("deploy_verdict")),
                    field="conclusion",
                    limit=64,
                )
        else:
            conclusion = _text(
                payload.get("conclusion", ""),
                field="conclusion",
                limit=64,
                required=False,
            )

        normalized = dict(payload)
        for field in (
            "provider_error",
            "error",
            "deploy_reason",
            "reason",
            "last_error",
        ):
            if field in normalized:
                normalized[field] = redact_sensitive(normalized[field], limit=512)
        normalized.update(
            {
                "delivery_id": delivery_id,
                "event": event_name,
                "action": action,
                "repo_full_name": repo,
                "pr_number": pr_number,
                "pr_head_sha": sha.lower(),
                "conclusion": conclusion,
            }
        )
        return normalized

    @staticmethod
    def _trigger_id(key: str) -> str:
        return "trig_" + hashlib.sha256(("trigger|" + key).encode()).hexdigest()[:24]

    @staticmethod
    def _merge_ready_key(repo: str, pr_number: int, head_sha: str) -> str:
        canonical = [repo.lower(), pr_number, head_sha.lower(), "MERGE_READY"]
        return hashlib.sha256(
            json.dumps(canonical, separators=(",", ":"), ensure_ascii=True).encode()
        ).hexdigest()

    @staticmethod
    def _state_values(event: dict[str, Any]) -> tuple[str, str, str, str]:
        checks = ""
        review = ""
        deploy = ""
        if event["event"] == "check_suite":
            checks = str(event.get("_checks_status") or event["conclusion"]).lower()
            next_action = "merge" if checks == "success" else "wait_ci"
        elif event["event"] == "deploy_result":
            deploy = event["conclusion"]
            next_action = _text(
                event.get("deploy_next", ""),
                field="deploy_next",
                limit=64,
                required=False,
            ) or ("closeout" if event["conclusion"].lower() == "success" else "inspect_deploy")
        else:
            review = _text(
                _first_value(event.get("review_status"), event.get("pr_state")),
                field="review_status",
                limit=64,
                required=False,
            )
            next_action = "review_pr"
            checks = "pending"
            next_action = "wait_ci"
        return checks, review, deploy, next_action

    @staticmethod
    def _quota_details(event: dict[str, Any], *, default_cooldown_ms: int) -> tuple[bool, int, str]:
        status_values = (
            event.get("provider_status"),
            event.get("http_status"),
            event.get("status_code"),
        )
        exhausted = any(
            not isinstance(value, bool) and str(value).strip() == "429" for value in status_values
        )
        exhausted = exhausted or any(
            event.get(field) is True for field in ("quota_exhausted", "provider_quota_exhausted")
        )
        text = " ".join(
            str(event.get(field, "")).lower()
            for field in ("provider_error", "error", "deploy_reason", "reason")
        )
        exhausted = exhausted or any(
            marker in text
            for marker in ("quota exhausted", "quota_exceeded", "rate limit", "rate_limit")
        )
        if not exhausted:
            return False, 0, ""

        retry_after_ms = default_cooldown_ms
        for field in ("retry_after_s", "provider_retry_after_s"):
            value = event.get(field)
            if isinstance(value, bool):
                continue
            try:
                seconds = float(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(seconds) and seconds > 0:
                retry_after_ms = min(int(seconds * 1000), 86_400_000)
                break
        error = _text(
            _first_value(event.get("provider_error"), event.get("error")),
            field="provider_error",
            limit=512,
            required=False,
        )
        return True, retry_after_ms, error or "provider quota exhausted"

    @staticmethod
    def _record_delivery(
        conn: sqlite3.Connection,
        event: dict[str, Any],
        key: str,
        trigger_id: str | None,
        disposition: str,
        now_ms: int,
    ) -> None:
        conn.execute(
            """
            INSERT OR IGNORE INTO deliveries (
                delivery_id, event_key, trigger_id, received_at_ms, disposition
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (event["delivery_id"], key, trigger_id, now_ms, disposition),
        )

    @staticmethod
    def _claim_specific(
        conn: sqlite3.Connection,
        *,
        trigger_id: str,
        now_ms: int,
        lease_ttl_ms: int,
    ) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT * FROM trigger_queue WHERE trigger_id = ?",
            (trigger_id,),
        ).fetchone()
        if row is None or row["state"] != "pending" or row["due_at_ms"] > now_ms:
            return None
        active = conn.execute(
            """
            SELECT 1 FROM trigger_queue
            WHERE repo = ? AND pr_number = ? AND state = 'claimed'
            """,
            (row["repo"], row["pr_number"]),
        ).fetchone()
        if active is not None:
            return None
        require_production_work({"repo": row["repo"], "payload": json.loads(row["payload_json"])})
        attempts = int(row["attempts"]) + 1
        lease = hashlib.sha256(f"{trigger_id}|{attempts}|{now_ms}|lease".encode()).hexdigest()[:40]
        conn.execute(
            """
            UPDATE trigger_queue
            SET state = 'claimed', attempts = ?, lease_token = ?,
                lease_expires_ms = ?, updated_at_ms = ?
            WHERE trigger_id = ? AND state = 'pending'
            """,
            (attempts, lease, now_ms + lease_ttl_ms, now_ms, trigger_id),
        )
        row = conn.execute(
            "SELECT * FROM trigger_queue WHERE trigger_id = ?",
            (trigger_id,),
        ).fetchone()
        return EventReducer._trigger_decision(row)

    @staticmethod
    def _trigger_decision(
        row: sqlite3.Row,
        *,
        recovered: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "decision": "trigger",
            "reason": "due",
            "trigger_id": row["trigger_id"],
            "route": row["route"],
            "lease": row["lease_token"],
            "lease_expires_ms": row["lease_expires_ms"],
            "attempts": row["attempts"],
            "due_at_ms": row["due_at_ms"],
            "collapsed_count": row["collapsed_count"],
            "recovered": recovered or [],
            "payload": json.loads(row["payload_json"]),
        }

    @staticmethod
    def _pending_decision(
        row: sqlite3.Row,
        *,
        reason: str,
        delivery_id: str,
        now_ms: int,
        due_at_ms: int | None = None,
    ) -> dict[str, Any]:
        due = int(row["due_at_ms"] if due_at_ms is None else due_at_ms)
        return {
            "decision": "defer",
            "reason": reason,
            "delivery_id": delivery_id,
            "event_key": row["event_key"],
            "trigger_id": row["trigger_id"],
            "route": row["route"],
            "due_at_ms": due,
            "wait_ms": max(0, due - now_ms),
            "payload": json.loads(row["payload_json"]),
        }

    def _recover_expired(self, conn: sqlite3.Connection, *, now_ms: int) -> list[str]:
        rows = conn.execute(
            """
            SELECT trigger_id FROM trigger_queue
            WHERE state = 'claimed' AND lease_expires_ms <= ?
            ORDER BY trigger_id
            """,
            (now_ms,),
        ).fetchall()
        trigger_ids = [row["trigger_id"] for row in rows]
        if trigger_ids:
            conn.execute(
                """
                UPDATE trigger_queue
                SET state = 'pending', lease_token = NULL, lease_expires_ms = NULL,
                    due_at_ms = ?, last_error = 'lease_expired', updated_at_ms = ?
                WHERE state = 'claimed' AND lease_expires_ms <= ?
                """,
                (now_ms, now_ms, now_ms),
            )
        return trigger_ids

    def _provider_gate(self, conn: sqlite3.Connection, *, now_ms: int) -> sqlite3.Row | None:
        conn.execute(
            """
            INSERT INTO provider_state (
                provider, state, consecutive_failures, cooldown_until_ms,
                last_error, updated_at_ms
            ) VALUES ('hermes', 'closed', 0, 0, '', ?)
            ON CONFLICT(provider) DO NOTHING
            """,
            (now_ms,),
        )
        row = conn.execute("SELECT * FROM provider_state WHERE provider = 'hermes'").fetchone()
        if row["state"] == "open" and row["cooldown_until_ms"] > now_ms:
            return row
        if row["state"] == "open":
            conn.execute(
                """
                UPDATE provider_state SET state = 'half_open', updated_at_ms = ?
                WHERE provider = 'hermes'
                """,
                (now_ms,),
            )
        return None

    def _open_provider_cooldown(
        self,
        conn: sqlite3.Connection,
        *,
        now_ms: int,
        retry_after_ms: int,
        error: str,
    ) -> int:
        retry_after_ms = max(0, min(retry_after_ms, MAX_PROVIDER_COOLDOWN_MS))
        until_ms = now_ms + retry_after_ms
        conn.execute(
            """
            INSERT INTO provider_state (
                provider, state, consecutive_failures, cooldown_until_ms,
                last_error, updated_at_ms
            ) VALUES ('hermes', 'open', 1, ?, ?, ?)
            ON CONFLICT(provider) DO UPDATE SET
                state = 'open',
                consecutive_failures = provider_state.consecutive_failures + 1,
                cooldown_until_ms = MAX(
                    provider_state.cooldown_until_ms, excluded.cooldown_until_ms
                ),
                last_error = excluded.last_error,
                updated_at_ms = excluded.updated_at_ms
            """,
            (until_ms, redact_sensitive(error), now_ms),
        )
        return until_ms

    def trip_provider(
        self,
        *,
        now_ms: int,
        cooldown_ms: int,
        reason: str,
    ) -> dict[str, Any]:
        """Open the provider breaker from an external quota monitor."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cooldown_until_ms = self._open_provider_cooldown(
                conn,
                now_ms=now_ms,
                retry_after_ms=cooldown_ms,
                error=reason,
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return {
            "decision": "silent",
            "reason": "provider_cooldown_set",
            "provider": "hermes",
            "state": "open",
            "cooldown_until_ms": cooldown_until_ms,
        }

    def _upsert_pr_state(
        self,
        conn: sqlite3.Connection,
        event: dict[str, Any],
        *,
        now_ms: int,
        head_changed: bool,
    ) -> None:
        checks, review, deploy, next_action = self._state_values(event)
        last_event = f"{event['event']}:{event['action']}:{event['conclusion']}"
        gate_reason = str(event.get("_gate_reason", ""))
        conn.execute(
            """
            INSERT INTO pr_state (
                repo, pr_number, head_sha, checks_status, review_status,
                deploy_status, last_event, next_action, updated_at_ms,
                merge_ready_at_ms, merge_ready_trigger_id, gate_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
            ON CONFLICT(repo, pr_number) DO UPDATE SET
                head_sha = excluded.head_sha,
                checks_status = CASE
                    WHEN ? = 1 AND excluded.last_event LIKE 'pull_request:%'
                    THEN 'pending'
                    WHEN excluded.checks_status = '' THEN pr_state.checks_status
                    ELSE excluded.checks_status END,
                review_status = CASE
                    WHEN excluded.review_status = '' THEN pr_state.review_status
                    ELSE excluded.review_status END,
                deploy_status = CASE
                    WHEN ? = 1 AND excluded.last_event LIKE 'pull_request:%'
                    THEN ''
                    WHEN excluded.deploy_status = '' THEN pr_state.deploy_status
                    ELSE excluded.deploy_status END,
                last_event = excluded.last_event,
                next_action = excluded.next_action,
                updated_at_ms = excluded.updated_at_ms,
                merge_ready_at_ms = CASE
                    WHEN ? = 1 AND excluded.last_event LIKE 'pull_request:%'
                    THEN NULL ELSE pr_state.merge_ready_at_ms END,
                merge_ready_trigger_id = CASE
                    WHEN ? = 1 AND excluded.last_event LIKE 'pull_request:%'
                    THEN NULL ELSE pr_state.merge_ready_trigger_id END,
                gate_reason = CASE
                    WHEN excluded.gate_reason = '' THEN pr_state.gate_reason
                    ELSE excluded.gate_reason END
            """,
            (
                event["repo_full_name"].lower(),
                event["pr_number"],
                event["pr_head_sha"],
                checks,
                review,
                deploy,
                last_event,
                next_action,
                now_ms,
                gate_reason,
                1 if head_changed else 0,
                1 if head_changed else 0,
                1 if head_changed else 0,
                1 if head_changed else 0,
            ),
        )

    def _merge_pending(
        self,
        conn: sqlite3.Connection,
        pending: sqlite3.Row,
        event: dict[str, Any],
        *,
        key: str,
        payload_json: str,
        route: str,
        now_ms: int,
    ) -> sqlite3.Row:
        due = min(
            max(int(pending["due_at_ms"]), now_ms + self.config.debounce_ms),
            int(pending["first_seen_ms"]) + self.config.max_debounce_ms,
        )
        use_new = EVENT_PRIORITY[event["event"]] >= pending["priority"]
        conn.execute(
            """
            UPDATE trigger_queue
            SET event_key = CASE WHEN ? THEN ? ELSE event_key END,
                event = CASE WHEN ? THEN ? ELSE event END,
                action = CASE WHEN ? THEN ? ELSE action END,
                conclusion = CASE WHEN ? THEN ? ELSE conclusion END,
                route = CASE WHEN ? THEN ? ELSE route END,
                priority = MAX(priority, ?),
                payload_json = CASE WHEN ? THEN ? ELSE payload_json END,
                collapsed_count = collapsed_count + 1,
                due_at_ms = ?, updated_at_ms = ?
            WHERE trigger_id = ?
            """,
            (
                1 if use_new else 0,
                key,
                1 if use_new else 0,
                event["event"],
                1 if use_new else 0,
                event["action"],
                1 if use_new else 0,
                event["conclusion"],
                1 if use_new else 0,
                route,
                EVENT_PRIORITY[event["event"]],
                1 if use_new else 0,
                payload_json,
                due,
                now_ms,
                pending["trigger_id"],
            ),
        )
        return conn.execute(
            "SELECT * FROM trigger_queue WHERE trigger_id = ?",
            (pending["trigger_id"],),
        ).fetchone()

    def ingest(self, payload: dict[str, Any], *, now_ms: int) -> dict[str, Any]:
        event = self.normalize(payload)
        if provider() is Provider.GITHUB or is_shadow_event(event):
            return {"decision": "silent", "reason": "github_shadow_only", "delivery_id": event["delivery_id"]}
        if event["event"] == "pull_request":
            return self._ingest_pull_request(event, now_ms=now_ms)
        if event["event"] == "check_suite":
            return self._ingest_check_suite(event, now_ms=now_ms)
        return self._ingest_deploy_result(event, now_ms=now_ms)

    def _ingest_pull_request(
        self, event: dict[str, Any], *, now_ms: int
    ) -> dict[str, Any]:
        """Record PR lifecycle without creating any provider work."""
        key = event_key(event)
        repo = event["repo_full_name"].lower()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            previous_delivery = conn.execute(
                "SELECT event_key FROM deliveries WHERE delivery_id = ?",
                (event["delivery_id"],),
            ).fetchone()
            if previous_delivery is not None:
                conn.execute("COMMIT")
                return {
                    "decision": "silent",
                    "reason": "delivery_already_recorded",
                    "delivery_id": event["delivery_id"],
                    "event_key": previous_delivery["event_key"],
                    "payload": event,
                }

            state = conn.execute(
                "SELECT * FROM pr_state WHERE repo = ? AND pr_number = ?",
                (repo, event["pr_number"]),
            ).fetchone()
            head_changed = state is None or state["head_sha"] != event["pr_head_sha"]
            reset_existing_head = state is not None and head_changed
            if reset_existing_head:
                old_trigger_ids = [
                    row["trigger_id"]
                    for row in conn.execute(
                        """
                        SELECT trigger_id FROM trigger_queue
                        WHERE repo = ? AND pr_number = ?
                          AND state IN ('pending', 'claimed')
                        """,
                        (repo, event["pr_number"]),
                    ).fetchall()
                ]
                conn.execute(
                    """
                    UPDATE trigger_queue
                    SET state = 'done', lease_token = NULL,
                        lease_expires_ms = NULL, completed_at_ms = ?,
                        last_error = 'superseded_head', updated_at_ms = ?
                    WHERE repo = ? AND pr_number = ?
                      AND state IN ('pending', 'claimed')
                    """,
                    (now_ms, now_ms, repo, event["pr_number"]),
                )
                for trigger_id in old_trigger_ids:
                    conn.execute(
                        "UPDATE deliveries SET disposition = 'superseded_head' WHERE trigger_id = ?",
                        (trigger_id,),
                    )

            self._upsert_pr_state(
                conn,
                event,
                now_ms=now_ms,
                head_changed=head_changed,
            )
            self._record_delivery(
                conn,
                event,
                key,
                None,
                "head_reset" if reset_existing_head else "state_updated",
                now_ms,
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

        return {
            "decision": "silent",
            "reason": "head_reset" if reset_existing_head else "pr_state_updated",
            "delivery_id": event["delivery_id"],
            "event_key": key,
            "payload": event,
        }

    @staticmethod
    def _checks_status_from_gate(gate: dict[str, Any], conclusion: str) -> str:
        reason = str(gate.get("reason", ""))
        is_ready = gate.get("ready") is True
        if reason == "ready":
            return "success" if is_ready else "unknown"
        if reason == "checks_failed":
            return "failure"
        if reason == "checks_running":
            return "pending"
        if reason == "checks_missing":
            return "partial"
        if reason in {"invalid_response", "gitea_unavailable", "credentials_unavailable"}:
            return "unknown"
        normalized = conclusion.lower()
        if normalized in {"success", "failure", "error"}:
            if normalized == "success":
                return "success" if is_ready else "unknown"
            return "failure" if normalized == "error" else normalized
        if normalized in {
            "pending",
            "queued",
            "requested",
            "in_progress",
            "running",
            "waiting",
        }:
            return "pending"
        return "unknown"

    def _record_check_state(
        self,
        event: dict[str, Any],
        *,
        key: str,
        reason: str,
        checks_status: str,
        now_ms: int,
    ) -> dict[str, Any]:
        repo = event["repo_full_name"].lower()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM deliveries WHERE delivery_id = ?",
                (event["delivery_id"],),
            ).fetchone():
                conn.execute("COMMIT")
                return {
                    "decision": "silent",
                    "reason": "delivery_already_recorded",
                    "delivery_id": event["delivery_id"],
                    "event_key": key,
                }
            state = conn.execute(
                "SELECT * FROM pr_state WHERE repo = ? AND pr_number = ?",
                (repo, event["pr_number"]),
            ).fetchone()
            if state is not None and state["head_sha"] != event["pr_head_sha"]:
                self._record_delivery(conn, event, key, None, "stale_head", now_ms)
                conn.execute("COMMIT")
                return {
                    "decision": "suppress",
                    "reason": "stale_head",
                    "delivery_id": event["delivery_id"],
                    "event_key": key,
                    "route": ROUTES["check_suite"],
                    "current_head": state["head_sha"],
                }
            state_event = dict(event)
            state_event["_checks_status"] = checks_status
            state_event["_gate_reason"] = reason
            self._upsert_pr_state(
                conn,
                state_event,
                now_ms=now_ms,
                head_changed=state is None or state["head_sha"] != event["pr_head_sha"],
            )
            self._record_delivery(conn, event, key, None, reason, now_ms)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return {
            "decision": "silent",
            "reason": reason,
            "delivery_id": event["delivery_id"],
            "event_key": key,
            "route": ROUTES["check_suite"],
        }

    def _ingest_check_suite(
        self, event: dict[str, Any], *, now_ms: int
    ) -> dict[str, Any]:
        conclusion = event["conclusion"].lower()
        key = event_key(event)
        if conclusion != "success":
            busy = {
                "pending",
                "queued",
                "requested",
                "in_progress",
                "running",
                "waiting",
            }
            status = "pending" if conclusion in busy else (
                "failure" if conclusion in {"failure", "error"} else "unknown"
            )
            return self._record_check_state(
                event,
                key=key,
                reason=f"checks_{conclusion}",
                checks_status=status,
                now_ms=now_ms,
            )

        repo = event["repo_full_name"].lower()
        conn = self._connect()
        try:
            state = conn.execute(
                "SELECT * FROM pr_state WHERE repo = ? AND pr_number = ?",
                (repo, event["pr_number"]),
            ).fetchone()
        finally:
            conn.close()
        if state is not None and state["head_sha"] != event["pr_head_sha"]:
            return self._record_check_state(
                event,
                key=key,
                reason="stale_head",
                checks_status="unknown",
                now_ms=now_ms,
            )
        if state is not None and state["merge_ready_at_ms"] is not None:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                delivery = conn.execute(
                    "SELECT trigger_id FROM deliveries WHERE delivery_id = ?",
                    (event["delivery_id"],),
                ).fetchone()
                queued = (
                    conn.execute(
                        "SELECT * FROM trigger_queue WHERE trigger_id = ?",
                        (delivery["trigger_id"],),
                    ).fetchone()
                    if delivery is not None and delivery["trigger_id"]
                    else None
                )
                blocked = self._provider_gate(conn, now_ms=now_ms)
                if (
                    delivery is not None
                    and queued is not None
                    and queued["state"] == "pending"
                ):
                    if blocked is not None:
                        conn.execute("COMMIT")
                        return self._pending_decision(
                            queued,
                            reason="provider_cooldown",
                            delivery_id=event["delivery_id"],
                            now_ms=now_ms,
                            due_at_ms=max(
                                int(queued["due_at_ms"]),
                                int(blocked["cooldown_until_ms"]),
                            ),
                        )
                    if queued["due_at_ms"] > now_ms:
                        conn.execute("COMMIT")
                        return self._pending_decision(
                            queued,
                            reason="debounce",
                            delivery_id=event["delivery_id"],
                            now_ms=now_ms,
                        )
                    triggered = self._claim_specific(
                        conn,
                        trigger_id=queued["trigger_id"],
                        now_ms=now_ms,
                        lease_ttl_ms=self.config.lease_ttl_ms,
                    )
                    if triggered is not None:
                        conn.execute(
                            "UPDATE deliveries SET disposition = 'triggered' WHERE delivery_id = ?",
                            (event["delivery_id"],),
                        )
                        conn.execute("COMMIT")
                        return triggered
                self._record_delivery(
                    conn,
                    event,
                    key,
                    state["merge_ready_trigger_id"],
                    "already_merge_ready",
                    now_ms,
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()
            return {
                "decision": "suppress",
                "reason": "already_merge_ready",
                "delivery_id": event["delivery_id"],
                "event_key": key,
                "trigger_id": state["merge_ready_trigger_id"],
                "route": ROUTES["check_suite"],
            }

        try:
            gate = self._readiness_checker(
                event["repo_full_name"], event["pr_number"], event["pr_head_sha"]
            )
        except Exception:
            gate = {"ready": False, "reason": "gitea_unavailable"}
        if not isinstance(gate, dict):
            gate = {"ready": False, "reason": "invalid_response"}
        gate_reason = str(gate.get("reason") or "invalid_response")
        if gate.get("ready") is not True:
            return self._record_check_state(
                event,
                key=key,
                reason=gate_reason,
                checks_status=self._checks_status_from_gate(gate, conclusion),
                now_ms=now_ms,
            )

        ready_event = dict(event)
        ready_event["trigger_name"] = "MERGE_READY"
        ready_event["merge_ready_contexts"] = gate.get("contexts", {})
        ready_key = self._merge_ready_key(
            event["repo_full_name"], event["pr_number"], event["pr_head_sha"]
        )
        trigger_id = self._trigger_id(ready_key)
        payload_json = json.dumps(
            ready_event,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        quota, retry_after_ms, provider_error = self._quota_details(
            ready_event, default_cooldown_ms=self.config.provider_cooldown_ms
        )

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cooldown_until_ms = (
                self._open_provider_cooldown(
                    conn,
                    now_ms=now_ms,
                    retry_after_ms=retry_after_ms,
                    error=provider_error,
                )
                if quota
                else 0
            )
            blocked = None if quota else self._provider_gate(conn, now_ms=now_ms)
            provider_blocked = blocked is not None or quota
            if blocked is not None:
                cooldown_until_ms = int(blocked["cooldown_until_ms"])

            if conn.execute(
                "SELECT 1 FROM deliveries WHERE delivery_id = ?",
                (event["delivery_id"],),
            ).fetchone():
                conn.execute("COMMIT")
                return {
                    "decision": "suppress",
                    "reason": "delivery_already_recorded",
                    "delivery_id": event["delivery_id"],
                    "route": ROUTES["check_suite"],
                }

            state = conn.execute(
                "SELECT * FROM pr_state WHERE repo = ? AND pr_number = ?",
                (repo, event["pr_number"]),
            ).fetchone()
            if state is not None and state["head_sha"] != event["pr_head_sha"]:
                self._record_delivery(conn, event, ready_key, None, "stale_head", now_ms)
                conn.execute("COMMIT")
                return {
                    "decision": "suppress",
                    "reason": "stale_head",
                    "delivery_id": event["delivery_id"],
                    "route": ROUTES["check_suite"],
                    "current_head": state["head_sha"],
                }
            if state is not None and state["merge_ready_at_ms"] is not None:
                self._record_delivery(
                    conn, event, ready_key, state["merge_ready_trigger_id"],
                    "already_merge_ready", now_ms,
                )
                conn.execute("COMMIT")
                return {
                    "decision": "suppress",
                    "reason": "already_merge_ready",
                    "delivery_id": event["delivery_id"],
                    "trigger_id": state["merge_ready_trigger_id"],
                    "route": ROUTES["check_suite"],
                }

            queued = conn.execute(
                "SELECT * FROM trigger_queue WHERE trigger_id = ?",
                (trigger_id,),
            ).fetchone()
            if queued is not None:
                self._record_delivery(
                    conn, event, ready_key, trigger_id, "already_merge_ready", now_ms
                )
                conn.execute("COMMIT")
                return {
                    "decision": "suppress",
                    "reason": "already_merge_ready",
                    "delivery_id": event["delivery_id"],
                    "trigger_id": trigger_id,
                    "route": ROUTES["check_suite"],
                }

            state_event = dict(ready_event)
            state_event["_checks_status"] = "success"
            state_event["_gate_reason"] = "ready"
            self._upsert_pr_state(
                conn,
                state_event,
                now_ms=now_ms,
                head_changed=state is None or state["head_sha"] != event["pr_head_sha"],
            )
            conn.execute(
                """
                INSERT INTO trigger_queue (
                    trigger_id, event_key, repo, pr_number, head_sha, event,
                    action, conclusion, route, priority, payload_json, state,
                    first_seen_ms, due_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, 'check_suite', 'completed', 'success',
                          'wdg-ci', ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    trigger_id,
                    ready_key,
                    repo,
                    event["pr_number"],
                    event["pr_head_sha"],
                    EVENT_PRIORITY["check_suite"],
                    payload_json,
                    now_ms,
                    now_ms + self.config.debounce_ms,
                    now_ms,
                ),
            )
            conn.execute(
                """
                UPDATE pr_state
                SET merge_ready_at_ms = ?, merge_ready_trigger_id = ?,
                    checks_status = 'success', next_action = 'merge',
                    gate_reason = 'ready', updated_at_ms = ?
                WHERE repo = ? AND pr_number = ? AND head_sha = ?
                """,
                (now_ms, trigger_id, now_ms, repo, event["pr_number"], event["pr_head_sha"]),
            )
            self._record_delivery(
                conn, event, ready_key, trigger_id, "deferred", now_ms
            )
            queued = conn.execute(
                "SELECT * FROM trigger_queue WHERE trigger_id = ?",
                (trigger_id,),
            ).fetchone()
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

        return self._pending_decision(
            queued,
            reason="provider_cooldown" if provider_blocked else "debounce",
            delivery_id=event["delivery_id"],
            now_ms=now_ms,
            due_at_ms=(
                max(int(queued["due_at_ms"]), cooldown_until_ms)
                if provider_blocked
                else None
            ),
        )

    def _ingest_deploy_result(
        self, event: dict[str, Any], *, now_ms: int
    ) -> dict[str, Any]:
        key = event_key(event)
        route = ROUTES[event["event"]]
        try:
            payload_json = json.dumps(
                event,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValidationError("payload is not RFC-compatible JSON") from exc
        quota, retry_after_ms, provider_error = self._quota_details(
            event, default_cooldown_ms=self.config.provider_cooldown_ms
        )

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cooldown_until_ms = (
                self._open_provider_cooldown(
                    conn,
                    now_ms=now_ms,
                    retry_after_ms=retry_after_ms,
                    error=provider_error,
                )
                if quota
                else 0
            )
            blocked = None if quota else self._provider_gate(conn, now_ms=now_ms)
            provider_blocked = blocked is not None or quota
            if blocked is not None:
                cooldown_until_ms = int(blocked["cooldown_until_ms"])

            delivery = conn.execute(
                """
                SELECT event_key, trigger_id FROM deliveries
                WHERE delivery_id = ?
                """,
                (event["delivery_id"],),
            ).fetchone()
            if delivery is not None:
                queued = (
                    conn.execute(
                        "SELECT * FROM trigger_queue WHERE trigger_id = ?",
                        (delivery["trigger_id"],),
                    ).fetchone()
                    if delivery["trigger_id"]
                    else None
                )
                if queued is not None and queued["state"] == "pending":
                    if provider_blocked:
                        conn.execute("COMMIT")
                        return self._pending_decision(
                            queued,
                            reason="provider_cooldown",
                            delivery_id=event["delivery_id"],
                            now_ms=now_ms,
                            due_at_ms=max(int(queued["due_at_ms"]), cooldown_until_ms),
                        )
                    triggered = self._claim_specific(
                        conn,
                        trigger_id=queued["trigger_id"],
                        now_ms=now_ms,
                        lease_ttl_ms=self.config.lease_ttl_ms,
                    )
                    if triggered is not None:
                        conn.execute(
                            "UPDATE deliveries SET disposition = 'triggered' WHERE delivery_id = ?",
                            (event["delivery_id"],),
                        )
                        conn.execute("COMMIT")
                        return triggered
                    if queued["due_at_ms"] > now_ms:
                        conn.execute("COMMIT")
                        return self._pending_decision(
                            queued,
                            reason="debounce",
                            delivery_id=event["delivery_id"],
                            now_ms=now_ms,
                        )
                    conn.execute("COMMIT")
                    return self._pending_decision(
                        queued,
                        reason="active_lease",
                        delivery_id=event["delivery_id"],
                        now_ms=now_ms,
                    )
                conn.execute("COMMIT")
                return {
                    "decision": "suppress",
                    "reason": (
                        "equivalent_active"
                        if queued is not None and queued["state"] == "claimed"
                        else "already_processed"
                    ),
                    "delivery_id": event["delivery_id"],
                    "event_key": delivery["event_key"],
                    "route": route,
                }

            state = conn.execute(
                """
                SELECT * FROM pr_state WHERE repo = ? AND pr_number = ?
                """,
                (event["repo_full_name"].lower(), event["pr_number"]),
            ).fetchone()
            if (
                state is not None
                and event["event"] != "pull_request"
                and state["head_sha"] != event["pr_head_sha"]
            ):
                self._record_delivery(conn, event, key, None, "stale_head", now_ms)
                conn.execute("COMMIT")
                return {
                    "decision": "suppress",
                    "reason": "stale_head",
                    "delivery_id": event["delivery_id"],
                    "event_key": key,
                    "route": route,
                    "current_head": state["head_sha"],
                }

            equivalent = conn.execute(
                """
                SELECT * FROM trigger_queue WHERE event_key = ?
                ORDER BY first_seen_ms, trigger_id LIMIT 1
                """,
                (key,),
            ).fetchone()
            if equivalent is not None:
                self._record_delivery(
                    conn, event, key, equivalent["trigger_id"], "coalesced", now_ms
                )
                if equivalent["state"] == "pending":
                    merged = self._merge_pending(
                        conn,
                        equivalent,
                        event,
                        key=key,
                        payload_json=payload_json,
                        route=route,
                        now_ms=now_ms,
                    )
                    conn.execute("COMMIT")
                    return self._pending_decision(
                        merged,
                        reason=("provider_cooldown" if provider_blocked else "coalesced"),
                        delivery_id=event["delivery_id"],
                        now_ms=now_ms,
                        due_at_ms=(
                            max(int(merged["due_at_ms"]), cooldown_until_ms)
                            if provider_blocked
                            else None
                        ),
                    )
                conn.execute("COMMIT")
                return {
                    "decision": "suppress",
                    "reason": (
                        "equivalent_active"
                        if equivalent["state"] == "claimed"
                        else "equivalent_completed"
                    ),
                    "delivery_id": event["delivery_id"],
                    "event_key": key,
                    "trigger_id": equivalent["trigger_id"],
                    "route": route,
                }

            self._upsert_pr_state(
                conn,
                event,
                now_ms=now_ms,
                head_changed=state is None or state["head_sha"] != event["pr_head_sha"],
            )

            pending = conn.execute(
                """
                SELECT * FROM trigger_queue
                WHERE repo = ? AND pr_number = ? AND head_sha = ?
                  AND state = 'pending'
                ORDER BY first_seen_ms, trigger_id LIMIT 1
                """,
                (
                    event["repo_full_name"].lower(),
                    event["pr_number"],
                    event["pr_head_sha"],
                ),
            ).fetchone()
            if pending is not None:
                merged = self._merge_pending(
                    conn,
                    pending,
                    event,
                    key=key,
                    payload_json=payload_json,
                    route=route,
                    now_ms=now_ms,
                )
                self._record_delivery(conn, event, key, merged["trigger_id"], "coalesced", now_ms)
                conn.execute("COMMIT")
                return self._pending_decision(
                    merged,
                    reason="provider_cooldown" if provider_blocked else "coalesced",
                    delivery_id=event["delivery_id"],
                    now_ms=now_ms,
                    due_at_ms=(
                        max(int(merged["due_at_ms"]), cooldown_until_ms)
                        if provider_blocked
                        else None
                    ),
                )

            trigger_id = self._trigger_id(key)
            conn.execute(
                """
                INSERT INTO trigger_queue (
                    trigger_id, event_key, repo, pr_number, head_sha, event,
                    action, conclusion, route, priority, payload_json, state,
                    first_seen_ms, due_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    trigger_id,
                    key,
                    event["repo_full_name"].lower(),
                    event["pr_number"],
                    event["pr_head_sha"],
                    event["event"],
                    event["action"],
                    event["conclusion"],
                    route,
                    EVENT_PRIORITY[event["event"]],
                    payload_json,
                    now_ms,
                    now_ms + self.config.debounce_ms,
                    now_ms,
                ),
            )
            queued = conn.execute(
                "SELECT * FROM trigger_queue WHERE trigger_id = ?",
                (trigger_id,),
            ).fetchone()
            self._record_delivery(conn, event, key, trigger_id, "deferred", now_ms)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

        return self._pending_decision(
            queued,
            reason="provider_cooldown" if provider_blocked else "debounce",
            delivery_id=event["delivery_id"],
            now_ms=now_ms,
            due_at_ms=(
                max(int(queued["due_at_ms"]), cooldown_until_ms) if provider_blocked else None
            ),
        )

    def drain(self, *, now_ms: int) -> dict[str, Any]:
        if provider() is Provider.GITHUB:
            return {"decision": "silent", "reason": "github_shadow_only"}
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            recovered = self._recover_expired(conn, now_ms=now_ms)
            blocked = self._provider_gate(conn, now_ms=now_ms)
            if blocked is not None:
                conn.execute("COMMIT")
                return {
                    "decision": "silent",
                    "reason": "provider_cooldown",
                    "cooldown_until_ms": blocked["cooldown_until_ms"],
                    "recovered": recovered,
                }
            candidate = conn.execute(
                """
                SELECT q.trigger_id FROM trigger_queue q
                WHERE q.state = 'pending' AND q.due_at_ms <= ?
                  AND NOT EXISTS (
                    SELECT 1 FROM trigger_queue active
                    WHERE active.repo = q.repo
                      AND active.pr_number = q.pr_number
                      AND active.state = 'claimed'
                  )
                ORDER BY q.due_at_ms, q.first_seen_ms, q.trigger_id
                LIMIT 1
                """,
                (now_ms,),
            ).fetchone()
            if candidate is None:
                active_count = conn.execute(
                    "SELECT COUNT(*) FROM trigger_queue WHERE state = 'claimed'"
                ).fetchone()[0]
                pending_count = conn.execute(
                    "SELECT COUNT(*) FROM trigger_queue WHERE state = 'pending'"
                ).fetchone()[0]
                conn.execute("COMMIT")
                return {
                    "decision": "silent",
                    "reason": "active_lease" if active_count else "no_due",
                    "active_count": active_count,
                    "pending_count": pending_count,
                    "recovered": recovered,
                }
            decision = self._claim_specific(
                conn,
                trigger_id=candidate["trigger_id"],
                now_ms=now_ms,
                lease_ttl_ms=self.config.lease_ttl_ms,
            )
            if decision is None:
                conn.execute("COMMIT")
                return {
                    "decision": "silent",
                    "reason": "active_lease",
                    "recovered": recovered,
                }
            decision["recovered"] = recovered
            conn.execute(
                "UPDATE deliveries SET disposition = 'triggered' WHERE trigger_id = ?",
                (candidate["trigger_id"],),
            )
            conn.execute("COMMIT")
            return decision
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def complete(self, trigger_id: str, lease: str, *, now_ms: int) -> dict[str, Any]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM trigger_queue WHERE trigger_id = ?",
                (trigger_id,),
            ).fetchone()
            if row is None or row["state"] != "claimed" or row["lease_token"] != lease:
                conn.execute("COMMIT")
                return {
                    "decision": "suppress",
                    "reason": "stale_lease",
                    "trigger_id": trigger_id,
                    "completed": False,
                }
            conn.execute(
                """
                UPDATE trigger_queue
                SET state = 'done', lease_token = NULL,
                    lease_expires_ms = NULL, completed_at_ms = ?,
                    last_error = '', updated_at_ms = ?
                WHERE trigger_id = ?
                """,
                (now_ms, now_ms, trigger_id),
            )
            conn.execute(
                "UPDATE deliveries SET disposition = 'triggered' WHERE trigger_id = ?",
                (trigger_id,),
            )
            conn.execute(
                """
                INSERT INTO provider_state (
                    provider, state, consecutive_failures, cooldown_until_ms,
                    last_error, updated_at_ms
                ) VALUES ('hermes', 'closed', 0, 0, '', ?)
                ON CONFLICT(provider) DO UPDATE SET
                    state = CASE
                        WHEN provider_state.state = 'half_open' THEN 'closed'
                        ELSE provider_state.state END,
                    consecutive_failures = CASE
                        WHEN provider_state.state = 'half_open' THEN 0
                        ELSE provider_state.consecutive_failures END,
                    cooldown_until_ms = CASE
                        WHEN provider_state.state = 'half_open' THEN 0
                        ELSE provider_state.cooldown_until_ms END,
                    last_error = CASE
                        WHEN provider_state.state = 'half_open' THEN ''
                        ELSE provider_state.last_error END,
                    updated_at_ms = excluded.updated_at_ms
                """,
                (now_ms,),
            )
            conn.execute("COMMIT")
            return {
                "decision": "suppress",
                "reason": "trigger_completed",
                "trigger_id": trigger_id,
                "completed": True,
            }
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def fail(
        self,
        trigger_id: str,
        lease: str,
        *,
        now_ms: int,
        reason: str = "",
        quota_exhausted: bool = False,
        retry_after_ms: int | None = None,
    ) -> dict[str, Any]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM trigger_queue WHERE trigger_id = ?",
                (trigger_id,),
            ).fetchone()
            if row is None or row["state"] != "claimed" or row["lease_token"] != lease:
                conn.execute("COMMIT")
                return {
                    "decision": "suppress",
                    "reason": "stale_lease",
                    "trigger_id": trigger_id,
                    "failed": False,
                }
            safe_reason = redact_sensitive(_text(reason, field="reason", limit=400, required=False))
            if quota_exhausted:
                delay = (
                    retry_after_ms
                    if retry_after_ms is not None and retry_after_ms >= 0
                    else self.config.provider_cooldown_ms
                )
                delay = max(0, min(delay, MAX_PROVIDER_COOLDOWN_MS))
                due_at_ms = self._open_provider_cooldown(
                    conn,
                    now_ms=now_ms,
                    retry_after_ms=delay,
                    error=safe_reason or "provider quota exhausted",
                )
            else:
                delay = max(500, self.config.debounce_ms * min(int(row["attempts"]), 30))
                due_at_ms = now_ms + delay
                conn.execute(
                    """
                    INSERT INTO provider_state (
                        provider, state, consecutive_failures, cooldown_until_ms,
                        last_error, updated_at_ms
                    ) VALUES ('hermes', 'closed', 1, 0, ?, ?)
                    ON CONFLICT(provider) DO UPDATE SET
                        consecutive_failures = consecutive_failures + 1,
                        last_error = excluded.last_error,
                        updated_at_ms = excluded.updated_at_ms
                    """,
                    (safe_reason, now_ms),
                )
            conn.execute(
                """
                UPDATE trigger_queue
                SET state = 'pending', lease_token = NULL,
                    lease_expires_ms = NULL, due_at_ms = ?,
                    last_error = ?, updated_at_ms = ?
                WHERE trigger_id = ?
                """,
                (due_at_ms, safe_reason, now_ms, trigger_id),
            )
            conn.execute("COMMIT")
            return {
                "decision": "defer",
                "reason": "provider_cooldown" if quota_exhausted else "provider_failure",
                "trigger_id": trigger_id,
                "failed": True,
                "due_at_ms": due_at_ms,
            }
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def inspect(self, *, pr: int) -> dict[str, Any]:
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT * FROM pr_state WHERE pr_number = ?
                ORDER BY updated_at_ms DESC LIMIT 1
                """,
                (pr,),
            ).fetchone()
            queue = conn.execute(
                """
                SELECT trigger_id, event_key, head_sha, event, route, state,
                       due_at_ms, lease_expires_ms, attempts, collapsed_count,
                       last_error
                FROM trigger_queue WHERE pr_number = ?
                ORDER BY first_seen_ms, trigger_id
                """,
                (pr,),
            ).fetchall()
        finally:
            conn.close()
        return {
            "pr": pr,
            "pr_state": dict(row) if row is not None else None,
            "queue": [dict(item) for item in queue],
        }

    def status(self, *, now_ms: int) -> dict[str, Any]:
        conn = self._connect()
        try:
            counts = {
                state: conn.execute(
                    "SELECT COUNT(*) FROM trigger_queue WHERE state = ?",
                    (state,),
                ).fetchone()[0]
                for state in ("pending", "claimed", "done")
            }
            next_row = conn.execute(
                """
                SELECT trigger_id, pr_number, route, due_at_ms
                FROM trigger_queue WHERE state = 'pending'
                ORDER BY due_at_ms, first_seen_ms, trigger_id LIMIT 1
                """
            ).fetchone()
            provider = conn.execute(
                "SELECT * FROM provider_state WHERE provider = 'hermes'"
            ).fetchone()
            merge_ready = conn.execute(
                """
                SELECT repo, pr_number, head_sha, merge_ready_at_ms AS ready_at_ms,
                       merge_ready_trigger_id AS trigger_id
                FROM pr_state
                WHERE merge_ready_at_ms IS NOT NULL
                ORDER BY merge_ready_at_ms DESC, pr_number
                """
            ).fetchall()
        finally:
            conn.close()
        provider_data = (
            dict(provider)
            if provider is not None
            else {
                "provider": "hermes",
                "state": "closed",
                "consecutive_failures": 0,
                "cooldown_until_ms": 0,
                "last_error": "",
            }
        )
        return {
            "decision": "silent",
            "reason": "status",
            "now_ms": now_ms,
            "queue": counts,
            "next_due": dict(next_row) if next_row is not None else None,
            "provider": provider_data,
            "merge_ready": [dict(row) for row in merge_ready],
        }


def _emit(decision: dict[str, Any]) -> None:
    if decision.get("decision") in {"trigger", "defer"} and (provider() is Provider.GITHUB or is_shadow_event(decision)):
        decision = {"decision": "suppress", "reason": "github_shadow_only"}
    print(
        json.dumps(
            decision,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ),
        flush=True,
    )


def _config_from_args(args: Any) -> ReducerConfig:
    return ReducerConfig(
        db_path=Path(args.db),
        debounce_ms=args.debounce_ms,
        max_debounce_ms=args.max_debounce_ms,
        lease_ttl_ms=args.lease_ttl_ms,
        provider_cooldown_ms=args.provider_cooldown_ms,
        required_contexts=parse_required_contexts(args.required_contexts),
    )


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


def build_parser() -> Any:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=os.environ.get("DAILYCHECK_EVENT_REDUCER_DB", str(DEFAULT_DB)),
    )
    parser.add_argument("--debounce-ms", type=int, default=2_000)
    parser.add_argument("--max-debounce-ms", type=int, default=10_000)
    parser.add_argument("--lease-ttl-ms", type=int, default=DEFAULT_LEASE_TTL_MS)
    parser.add_argument("--provider-cooldown-ms", type=int, default=60_000)
    parser.add_argument(
        "--required-contexts",
        default=os.environ.get(
            "DAILYCHECK_REQUIRED_CONTEXTS", DEFAULT_REQUIRED_CONTEXTS_ENV
        ),
        help="comma-separated exact required CI context names",
    )
    parser.add_argument("--now-ms", type=int, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ingest", help="read one event JSON object from stdin")
    sub.add_parser("drain", help="claim one due trigger, or report silent")

    complete = sub.add_parser("complete", help="mark a claimed trigger delivered")
    complete.add_argument("--trigger-id", required=True)
    complete.add_argument("--lease", required=True)

    fail = sub.add_parser("fail", help="return a claimed trigger to the queue")
    fail.add_argument("--trigger-id", required=True)
    fail.add_argument("--lease", required=True)
    fail.add_argument("--reason", default="")
    fail.add_argument("--quota-exhausted", action="store_true")
    fail.add_argument("--retry-after-ms", type=int)

    trip = sub.add_parser("provider-trip", help="open the provider cooldown breaker")
    trip.add_argument("--cooldown-ms", type=int, required=True)
    trip.add_argument("--reason", default="provider quota threshold")

    sub.add_parser("status", help="show queue and provider state")
    inspect = sub.add_parser("inspect", help="show one PR state and queue history")
    inspect.add_argument("--pr", type=int, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    now_ms = args.now_ms if args.now_ms is not None else _now_ms()
    try:
        reducer = EventReducer(_config_from_args(args))
        if args.command == "ingest":
            raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
            if len(raw) > MAX_INPUT_BYTES:
                _emit(
                    {
                        "decision": "suppress",
                        "reason": "invalid_payload",
                        "detail": "payload exceeds 1 MiB",
                    }
                )
                return 0
            try:
                payload = strict_json_loads(raw)
                decision = reducer.ingest(payload, now_ms=now_ms)
            except (ValidationError, ValueError, RecursionError, UnicodeError) as exc:
                _emit(
                    {
                        "decision": "suppress",
                        "reason": "invalid_payload",
                        "detail": str(exc)[:240],
                    }
                )
                return 0
        elif args.command == "drain":
            decision = reducer.drain(now_ms=now_ms)
        elif args.command == "complete":
            decision = reducer.complete(args.trigger_id, args.lease, now_ms=now_ms)
        elif args.command == "fail":
            decision = reducer.fail(
                args.trigger_id,
                args.lease,
                now_ms=now_ms,
                reason=args.reason,
                quota_exhausted=args.quota_exhausted,
                retry_after_ms=args.retry_after_ms,
            )
        elif args.command == "provider-trip":
            decision = reducer.trip_provider(
                now_ms=now_ms,
                cooldown_ms=args.cooldown_ms,
                reason=args.reason,
            )
        elif args.command == "inspect":
            decision = reducer.inspect(pr=args.pr)
        else:
            decision = reducer.status(now_ms=now_ms)
    except HostingError as exc:
        if str(exc) == "github_shadow_only":
            _emit({"decision": "suppress", "reason": "github_shadow_only"})
            return 0
        _emit({"decision": "retry", "reason": "invalid_shadow_configuration"})
        return 1
    except ValueError:
        print(
            json.dumps(
                {
                    "decision": "retry",
                    "reason": "invalid_configuration",
                },
                separators=(",", ":"),
            ),
            file=sys.stderr,
            flush=True,
        )
        return 1
    except (OSError, sqlite3.Error) as exc:
        print(
            json.dumps(
                {
                    "decision": "retry",
                    "reason": "state_unavailable",
                    "detail": f"{type(exc).__name__}: {exc}"[:240],
                },
                ensure_ascii=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
            flush=True,
        )
        return 1

    _emit(decision)
    if decision.get("decision") == "defer":
        return 75
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
