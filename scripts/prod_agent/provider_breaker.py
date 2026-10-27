#!/usr/bin/env python3
"""External Hermes provider-quota breaker.

The breaker tails only complete, newly appended bytes of Hermes'
``errors.log``.  Three quota/429 events in five minutes open a fifteen-minute
reducer cooldown; a provider Retry-After/reset hint may extend that cooldown to
one hour.  Cursor and trigger state are persisted atomically so a restart never
replays already scanned log lines.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from event_reducer import (
    DEFAULT_DB,
    DEFAULT_LEASE_TTL_MS,
    MAX_PROVIDER_COOLDOWN_MS,
    EventReducer,
    ReducerConfig,
)

DEFAULT_LOG = Path("/root/.hermes/logs/errors.log")
DEFAULT_STATE = Path("/var/lib/dailycheck-prod-agent/provider_breaker_state.json")
WINDOW_MS = 300_000
THRESHOLD = 3
COOLDOWN_MS = 900_000
MAX_SCAN_BYTES = 16 * 1024 * 1024

_PROVIDER_CONTEXT = (
    r"(?:provider|model|api|llm|openai|anthropic|gemini|codex|nous|deepseek|minimax)"
)
_PROVIDER_CONTEXT_TOKEN = rf"(?<![A-Za-z0-9]){_PROVIDER_CONTEXT}(?![A-Za-z0-9])"
_QUOTA_MARKER = (
    r"(?:quota[ _-]?(?:exhausted|exceeded|limit|reached)|insufficient_quota)"
)
_QUOTA_PATTERNS = (
    re.compile(rf"(?i){_PROVIDER_CONTEXT_TOKEN}.{{0,120}}\b{_QUOTA_MARKER}\b"),
    re.compile(rf"(?i)\b{_QUOTA_MARKER}\b.{{0,120}}{_PROVIDER_CONTEXT_TOKEN}"),
    re.compile(
        rf"(?i){_PROVIDER_CONTEXT_TOKEN}.{{0,120}}"
        r"\b(?:rate[ _-]?limit|usage[ _-]?limit)\b"
    ),
    re.compile(
        rf"(?i)\b(?:rate[ _-]?limit|usage[ _-]?limit)\b.{{0,120}}"
        rf"{_PROVIDER_CONTEXT_TOKEN}"
    ),
    re.compile(rf"(?i){_PROVIDER_CONTEXT_TOKEN}.{{0,120}}\b429\b"),
    re.compile(
        r"(?i)\b(?:http(?:\s+status)?|status(?:_code)?|error(?:\s+code)?|code)"
        r"\s*[:=]?\s*429\b|\(429\)|\b429\s+(?:too many requests|client error)"
    ),
)
_RETRY_AFTER_PATTERNS = (
    re.compile(r"(?i)\bretry[ _-]?after\s*[:=]\s*(\d+(?:\.\d+)?)"),
    re.compile(r"(?i)\bretry_after_s\s*[:=]\s*(\d+(?:\.\d+)?)"),
    re.compile(r"(?i)\breset(?:s)?\s+in\s+(\d+(?:\.\d+)?)\s*(?:s|sec|seconds?)\b"),
)
_RESET_AT_PATTERNS = (
    re.compile(r"(?i)\bx-ratelimit-reset\s*[:=]\s*(\d+)"),
    re.compile(r"(?i)\breset(?:_at|[_ ]time)?\s*[:=]\s*(\d+)"),
)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        os.chmod(path, 0o600)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "cursor": None, "quota_events": [], "pending_trip": None}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {"version": 1, "cursor": None, "quota_events": [], "pending_trip": None}
    if not isinstance(raw, dict):
        return {"version": 1, "cursor": None, "quota_events": [], "pending_trip": None}
    cursor = raw.get("cursor")
    if not isinstance(cursor, dict):
        cursor = None
    raw_events = raw.get("quota_events")
    if not isinstance(raw_events, list):
        raw_events = []
    events: list[dict[str, int]] = []
    for value in raw_events:
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            events.append({"at_ms": value, "cooldown_until_ms": 0})
            continue
        if not isinstance(value, dict):
            continue
        at_ms = value.get("at_ms")
        cooldown_until_ms = value.get("cooldown_until_ms", 0)
        if (
            isinstance(at_ms, int)
            and not isinstance(at_ms, bool)
            and at_ms >= 0
            and isinstance(cooldown_until_ms, int)
            and not isinstance(cooldown_until_ms, bool)
            and cooldown_until_ms >= 0
        ):
            events.append({"at_ms": at_ms, "cooldown_until_ms": cooldown_until_ms})
    pending = raw.get("pending_trip")
    if not isinstance(pending, dict):
        pending = None
    return {
        "version": 1,
        "cursor": cursor,
        "quota_events": events,
        "pending_trip": pending,
    }


def _is_quota_line(line: str) -> bool:
    return any(pattern.search(line) for pattern in _QUOTA_PATTERNS)


def _cooldown_hint_ms(
    lines: list[str],
    *,
    now_ms: int,
    default_cooldown_ms: int,
    max_cooldown_ms: int,
) -> int:
    hint_ms = default_cooldown_ms
    for line in lines:
        for pattern in _RETRY_AFTER_PATTERNS:
            for match in pattern.finditer(line):
                try:
                    seconds = float(match.group(1))
                except (TypeError, ValueError, OverflowError):
                    continue
                if math.isfinite(seconds) and seconds > 0:
                    hint_ms = max(hint_ms, int(seconds * 1000))
        for pattern in _RESET_AT_PATTERNS:
            for match in pattern.finditer(line):
                try:
                    reset_value = int(match.group(1))
                except (TypeError, ValueError, OverflowError):
                    continue
                reset_ms = reset_value if reset_value >= 1_000_000_000_000 else reset_value * 1000
                hint_ms = max(hint_ms, reset_ms - now_ms)
    return max(0, min(hint_ms, max_cooldown_ms))


def scan_once(
    *,
    log_path: Path,
    state_path: Path,
    reducer: EventReducer,
    now_ms: int,
    window_ms: int = WINDOW_MS,
    threshold: int = THRESHOLD,
    cooldown_ms: int = COOLDOWN_MS,
    max_cooldown_ms: int = MAX_PROVIDER_COOLDOWN_MS,
) -> dict[str, Any]:
    """Scan new complete log lines and apply a durable provider cooldown."""
    state = _load_state(state_path)
    try:
        stat = log_path.stat()
    except FileNotFoundError:
        # Remember that the log did not exist. If it appears later, all of its
        # bytes are new relative to this observation and should be scanned.
        state["cursor"] = {"missing": True}
        _atomic_write_json(state_path, state)
        return {
            "scanned_bytes": 0,
            "new_quota_events": 0,
            "window_event_count": 0,
            "tripped": False,
        }

    cursor = state["cursor"]
    identity = {"dev": stat.st_dev, "ino": stat.st_ino}
    if isinstance(cursor, dict) and cursor.get("missing") is True:
        offset = 0
    elif not isinstance(cursor, dict) or not isinstance(cursor.get("offset"), int):
        # First observation starts at EOF. Historical errors are never replayed.
        state["cursor"] = {**identity, "offset": stat.st_size}
        _atomic_write_json(state_path, state)
        offset = stat.st_size
    else:
        same_file = cursor.get("dev") == stat.st_dev and cursor.get("ino") == stat.st_ino
        offset = int(cursor["offset"]) if same_file else 0
        if offset < 0 or offset > stat.st_size:
            offset = 0

    scanned_bytes = 0
    new_lines: list[str] = []
    if offset < stat.st_size:
        with log_path.open("rb") as fh:
            fh.seek(offset)
            chunk = fh.read(min(stat.st_size - offset, MAX_SCAN_BYTES))
        complete_bytes = chunk[: chunk.rfind(b"\n") + 1] if b"\n" in chunk else b""
        if complete_bytes:
            scanned_bytes = len(complete_bytes)
            offset += scanned_bytes
            new_lines = complete_bytes.decode("utf-8", "replace").splitlines()

    quota_lines = [line for line in new_lines if _is_quota_line(line)]
    cutoff_ms = now_ms - window_ms
    recent = [event for event in state["quota_events"] if event["at_ms"] >= cutoff_ms]
    for line in quota_lines:
        hint_ms = _cooldown_hint_ms(
            [line],
            now_ms=now_ms,
            default_cooldown_ms=0,
            max_cooldown_ms=max_cooldown_ms,
        )
        recent.append(
            {
                "at_ms": now_ms,
                "cooldown_until_ms": now_ms + hint_ms if hint_ms else 0,
            }
        )

    pending_trip = state.get("pending_trip")
    if len(recent) >= threshold and not isinstance(pending_trip, dict):
        hint_ms = max(
            cooldown_ms,
            max(event["cooldown_until_ms"] for event in recent) - now_ms,
        )
        hint_ms = max(0, min(hint_ms, max_cooldown_ms))
        pending_trip = {
            "cooldown_until_ms": now_ms + hint_ms,
            "reason": "provider_quota_threshold",
        }

    state["cursor"] = {**identity, "offset": offset}
    state["quota_events"] = recent
    state["pending_trip"] = pending_trip
    # Persist the cursor before touching the reducer. If the DB is unavailable,
    # pending_trip survives and a restart retries the cooldown without re-reading
    # these log lines.
    _atomic_write_json(state_path, state)

    applied_until_ms = None
    if isinstance(pending_trip, dict):
        try:
            until_ms = int(pending_trip.get("cooldown_until_ms", 0))
        except (TypeError, ValueError, OverflowError):
            until_ms = now_ms + cooldown_ms
        reducer.trip_provider(
            now_ms=now_ms,
            cooldown_ms=max(0, until_ms - now_ms),
            reason=str(pending_trip.get("reason", "provider_quota_threshold")),
        )
        applied_until_ms = until_ms
        state["pending_trip"] = None
        _atomic_write_json(state_path, state)

    return {
        "scanned_bytes": scanned_bytes,
        "new_quota_events": len(quota_lines),
        "window_event_count": len(recent),
        "tripped": applied_until_ms is not None,
        "cooldown_until_ms": applied_until_ms,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", default=str(DEFAULT_LOG))
    parser.add_argument("--state", default=str(DEFAULT_STATE))
    parser.add_argument(
        "--db", default=os.environ.get("DAILYCHECK_EVENT_REDUCER_DB", str(DEFAULT_DB))
    )
    parser.add_argument("--window-ms", type=int, default=WINDOW_MS)
    parser.add_argument("--threshold", type=int, default=THRESHOLD)
    parser.add_argument("--cooldown-ms", type=int, default=COOLDOWN_MS)
    parser.add_argument("--max-cooldown-ms", type=int, default=MAX_PROVIDER_COOLDOWN_MS)
    parser.add_argument("--lease-ttl-ms", type=int, default=DEFAULT_LEASE_TTL_MS)
    parser.add_argument("--now-ms", type=int, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    import sys
    import time

    args = build_parser().parse_args(argv)
    now_ms = args.now_ms if args.now_ms is not None else int(time.time() * 1000)
    reducer = EventReducer(
        ReducerConfig(
            db_path=Path(args.db),
            lease_ttl_ms=args.lease_ttl_ms,
        )
    )
    try:
        result = scan_once(
            log_path=Path(args.log),
            state_path=Path(args.state),
            reducer=reducer,
            now_ms=now_ms,
            window_ms=args.window_ms,
            threshold=args.threshold,
            cooldown_ms=args.cooldown_ms,
            max_cooldown_ms=args.max_cooldown_ms,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "decision": "retry",
                    "reason": "provider_breaker_unavailable",
                    "detail": type(exc).__name__,
                },
                separators=(",", ":"),
            ),
            file=sys.stderr,
            flush=True,
        )
        return 1
    print(json.dumps(result, separators=(",", ":"), sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
