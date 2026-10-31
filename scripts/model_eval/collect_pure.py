"""Auditable usage facts. No filesystem time is used for attribution (Issue #102)."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone

UNKNOWN_MODEL = "unknown"
UNSPLIT = "unsplittable"
BASE_KEYS = ("input", "output", "cache_read", "cache_write")


@dataclass(frozen=True)
class Usage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(**{k: getattr(self, k) + getattr(other, k) for k in self.as_dict()})

    def as_dict(self) -> dict[str, int]:
        return {k: getattr(self, k) for k in (*BASE_KEYS, "reasoning")}


@dataclass(frozen=True)
class Transcript:
    source: str
    path: str
    cwd: str | None
    mtime: float  # discovery only; never an execution or usage timestamp
    usage_by_model: dict[str, Usage]
    warnings: tuple[str, ...] = ()
    session_id: str | None = None
    events: tuple[dict, ...] = ()
    dispatch_ids: tuple[str, ...] = ()

    def models(self) -> list[str]:
        return sorted(self.usage_by_model)


def normalize_path(path: object) -> str | None:
    if not isinstance(path, str) or not path.strip():
        return None
    out = path.strip().replace("\\", "/")
    while "//" in out:
        out = out.replace("//", "/")
    return out.rstrip("/") or "/"


def parse_ts(value: object) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, OverflowError):
        return None


def iso_ts(value: object) -> str | None:
    stamp = parse_ts(value)
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat() if stamp is not None else None


def _as_int(value: object) -> int:
    try:
        return (
            max(0, int(value)) if not isinstance(value, bool) and math.isfinite(float(value)) else 0
        )
    except (ValueError, TypeError, OverflowError):
        return 0


def _obj(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _records(lines: Iterable[str]) -> tuple[list[dict], int]:
    out, bad = [], 0
    for raw in lines:
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                out.append(obj)
            else:
                bad += 1
        except (ValueError, RecursionError):
            bad += 1
    return out, bad


def _preamble(record: dict) -> tuple[str, str] | None:
    """Extract identifiers only from user records; transcript text is never executable."""
    msg = _obj(record.get("message")) or _obj(record.get("payload"))
    if record.get("type") != "user" and msg.get("role") != "user":
        return None
    content = msg.get("content", "")
    if isinstance(content, list):
        content = "\n".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    if "You are a dispatched worker." not in str(content) or "=== CLI COMMANDS ===" not in str(
        content
    ):
        return None
    dispatches = set(re.findall(r"--dispatch-id\s+(ctx_[A-Za-z0-9]+)\b", str(content)))
    tasks = set(re.findall(r"--task-id\s+(task_[A-Za-z0-9]+)\b", str(content)))
    return (
        (next(iter(dispatches)), next(iter(tasks))) if len(dispatches) == len(tasks) == 1 else None
    )


def _source_tokens(usage, raw, provider):
    def valid(value):
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value >= 0
            and int(value) == value
        )

    result = usage.as_dict()
    measured = any(valid(raw.get(k)) for k in ("input_tokens", "output_tokens"))
    for field, key in (("input", "input_tokens"), ("output", "output_tokens")):
        if not valid(raw.get(key)):
            result[field] = None
    cache = (
        (("cache_read", "cached_input_tokens"), ("cache_write", "cache_write_input_tokens"))
        if provider == "codex"
        else (
            ("cache_read", "cache_read_input_tokens"),
            ("cache_write", "cache_creation_input_tokens"),
        )
    )
    for field, key in cache:
        if (
            not measured
            or (provider == "claude" and key not in raw)
            or key in raw
            and not valid(raw[key])
        ):
            result[field] = None
            if provider == "codex":
                result["input"] = None
    reasoning = (
        raw.get("reasoning_output_tokens")
        if provider == "codex"
        else _obj(raw.get("output_tokens_details")).get(
            "thinking_tokens", raw.get("reasoning_tokens")
        )
    )
    if not valid(reasoning):
        result["reasoning"] = None
    return result


def _event(key, ts, model, usage, session, link, **extra):
    raw, provider = extra.pop("raw_usage", None), extra.pop("provider", None)
    tokens = _source_tokens(usage, raw, provider) if raw is not None else usage.as_dict()
    extra["complete"] = all(tokens[k] is not None for k in BASE_KEYS)
    return {
        "event_id": key,
        "event_ts": iso_ts(ts),
        "model": model,
        "tokens": tokens,
        "session_id": session,
        "dispatch_id": link[0] if link else None,
        "task_id": link[1] if link else None,
        **extra,
    }


def parse_claude_transcript(lines: Iterable[str], path: str, mtime: float) -> Transcript:
    records, bad = _records(lines)
    cwd, session, link = None, None, None
    events, seen, links, totals = [], set(), set(), {}
    for index, rec in enumerate(records):
        cwd = normalize_path(rec.get("cwd")) or cwd
        session = rec.get("sessionId") or session
        link = _preamble(rec) or link
        if link:
            links.add(link[0])
        msg = _obj(rec.get("message"))
        raw = msg.get("usage")
        if not isinstance(raw, dict):
            continue
        key = str(msg.get("id") or rec.get("uuid") or f"line:{index}")
        if key in seen:
            continue
        seen.add(key)
        model = msg.get("model") or UNKNOWN_MODEL
        usage = Usage(
            input=_as_int(raw.get("input_tokens")),
            output=_as_int(raw.get("output_tokens")),
            cache_read=_as_int(raw.get("cache_read_input_tokens")),
            cache_write=_as_int(raw.get("cache_creation_input_tokens")),
            reasoning=_as_int(
                _obj(raw.get("output_tokens_details")).get("thinking_tokens")
                or raw.get("reasoning_tokens")
            ),
        )
        # A file's name is only a source identity when provider metadata is absent.
        sid = str(session or path)
        events.append(
            _event(
                f"claude:{sid}:{key}",
                rec.get("timestamp"),
                model,
                usage,
                sid,
                link,
                raw_usage=raw,
                provider="claude",
            )
        )
        totals[model] = totals.get(model, Usage()) + usage
    warnings = (f"bad_lines:{path}:{bad}",) if bad else ()
    return Transcript(
        "claude",
        path,
        cwd,
        mtime,
        totals,
        warnings,
        str(session or path),
        tuple(events),
        tuple(sorted(links)),
    )


def _codex_usage_from_total(total: dict) -> Usage:
    cached = _as_int(total.get("cached_input_tokens"))
    written = _as_int(total.get("cache_write_input_tokens"))
    return Usage(
        input=max(0, _as_int(total.get("input_tokens")) - cached - written),
        output=_as_int(total.get("output_tokens")),
        cache_read=cached,
        cache_write=written,
        reasoning=_as_int(total.get("reasoning_output_tokens")),
    )


def parse_codex_transcript(lines: Iterable[str], path: str, mtime: float) -> Transcript:
    records, bad = _records(lines)
    cwd, session, model, link = None, None, UNKNOWN_MODEL, None
    snapshots, legacy, links = [], [], set()
    for index, rec in enumerate(records):
        raw = _obj(rec.get("payload"))
        kind = rec.get("type")
        link = _preamble(rec) or link
        if link:
            links.add(link[0])
        if kind in ("session_meta", "turn_context"):
            cwd = normalize_path(raw.get("cwd")) or cwd
            if kind == "session_meta":
                session = raw.get("id") or session
            model = raw.get("model") or model
        if kind == "event_msg" and raw.get("type") == "token_count":
            kind = "token_count"
        total = (
            raw.get("thread_token_usage")
            if kind == "token_usage_record"
            else (_obj(raw.get("info")).get("total_token_usage") if kind == "token_count" else None)
        )
        if isinstance(total, dict):
            target = snapshots if kind == "token_usage_record" else legacy
            target.append((rec, raw, total, model, link, index, session))
    samples = snapshots or legacy  # never add two representations of the same counter
    previous, events, totals, warnings = {}, [], {}, []
    if bad:
        warnings.append(f"bad_lines:{path}:{bad}")
    # Missing timestamps cannot support model/window deltas. Preserve a final session total.
    timed = bool(samples) and all(parse_ts(s[0].get("timestamp")) is not None for s in samples)
    if not timed:
        latest = {}
        for s in samples:
            latest[str(s[1].get("thread_id") or s[1].get("session_id") or session or path)] = s
        models = {s[3] for s in samples}
        for thread, s in latest.items():
            rec, raw, total, actual, at_link, index, sid = s
            actual = actual if len(models) == 1 else UNSPLIT
            usage = _codex_usage_from_total(total)
            sid = str(sid or raw.get("session_id") or path)
            events.append(
                _event(
                    f"codex:{sid}:total:{thread}",
                    None,
                    actual,
                    usage,
                    sid,
                    at_link if len(links) <= 1 else None,
                    raw_usage=total,
                    provider="codex",
                    attribution="dispatch_time",
                    counter="final_snapshot",
                )
            )
            totals[actual] = totals.get(actual, Usage()) + usage
        if len(models) > 1:
            warnings.append(f"model_unsplittable:{path}")
    else:
        keys = (
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "cache_write_input_tokens",
            "reasoning_output_tokens",
        )
        for rec, raw, total, actual, at_link, index, sid in samples:
            thread = str(raw.get("thread_id") or raw.get("session_id") or sid or path)
            if not all(
                isinstance(total.get(k), int) and not isinstance(total[k], bool) and total[k] >= 0
                for k in ("input_tokens", "output_tokens")
            ):
                warnings.append(f"incomplete_cumulative_snapshot:{thread}:{index}")
                continue
            prev = previous.get(thread, dict.fromkeys(keys, 0))
            reset = any(total[k] < prev[k] for k in ("input_tokens", "output_tokens"))
            current = {
                k: _as_int(total[k]) if k in total else (0 if reset else prev[k]) for k in keys
            }
            delta = current if reset else {k: max(0, current[k] - prev[k]) for k in keys}
            previous[thread] = current
            if not any(delta.values()):
                continue
            usage = _codex_usage_from_total(delta)
            sid = str(sid or raw.get("session_id") or path)
            key = raw.get("response_id") or rec.get("ordinal") or index
            events.append(
                _event(
                    f"codex:{sid}:{thread}:{key}",
                    rec.get("timestamp"),
                    actual,
                    usage,
                    sid,
                    at_link,
                    raw_usage=total,
                    provider="codex",
                    counter_reset=reset,
                )
            )
            totals[actual] = totals.get(actual, Usage()) + usage
            if reset:
                warnings.append(f"counter_reset:{thread}:{key}")
    return Transcript(
        "codex",
        path,
        cwd,
        mtime,
        totals,
        tuple(warnings),
        str(session or path),
        tuple(events),
        tuple(sorted(links)),
    )


def compute_cost(usage: Usage, price: dict) -> float:
    def rate(key):
        try:
            value = float(price.get(key, 0))
            return value if math.isfinite(value) and value >= 0 else 0
        except (ValueError, TypeError):
            return 0

    return sum(getattr(usage, k) * rate(k) for k in BASE_KEYS) / 1_000_000


def event_worktree(event: dict) -> str | None:
    return normalize_path(_obj(event.get("data")).get("worktree_path"))


def dispatch_id(event: dict) -> str | None:
    data = _obj(event.get("data"))
    value = data.get("dispatch_id") or data.get("dispatch")
    if not value and str(data.get("worker_id", "")).startswith("ctx_"):
        value = data["worker_id"]  # historical automation mislabeled the Dispatch ID
    return str(value) if value else None


def _matches(event, transcript, usage):
    data = _obj(event.get("data"))
    did = dispatch_id(event)
    if usage.get("dispatch_id"):
        return did == usage["dispatch_id"] and (
            not data.get("task_id") or data["task_id"] == usage.get("task_id")
        )
    if data.get("session_id") and data["session_id"] == usage.get("session_id"):
        return True
    if event_worktree(event) != transcript.cwd or not transcript.cwd:
        return False
    at, start = parse_ts(usage.get("event_ts")), parse_ts(data.get("started_at"))
    end = parse_ts(data.get("ended_at"))
    if end is None and data.get("running") is True:
        end = parse_ts(data.get("observed_at"))
    return at is not None and start is not None and end is not None and start <= at < end


def assign_transcripts(
    dispatch_events: list[dict], transcripts: list[Transcript]
) -> dict[int, list[Transcript]]:
    """Compatibility helper: only whole transcripts with a unique proven owner."""
    out = {}
    for transcript in transcripts:
        owners = [
            {i for i, d in enumerate(dispatch_events) if _matches(d, transcript, u)}
            for u in transcript.events
        ]
        if owners and all(len(o) == 1 and o == owners[0] for o in owners):
            out.setdefault(next(iter(owners[0])), []).append(transcript)
    return out


def build_ledger(
    dispatch_events: list[dict], transcripts: list[Transcript], pricing: dict | None = None
) -> list[dict]:
    prices = _obj(_obj(pricing).get("prices"))
    # One dispatch fact even when an automation event was replayed.
    unique = {}
    for event in dispatch_events:
        key = dispatch_id(event) or json.dumps(event, sort_keys=True)
        unique[key] = event
    dispatch_events = list(unique.values())
    assigned = {i: [] for i in range(len(dispatch_events))}
    unassigned = []
    seen = set()
    for transcript in transcripts:
        for usage in transcript.events:
            if usage["event_id"] in seen:
                continue
            seen.add(usage["event_id"])
            candidates = [
                i for i, d in enumerate(dispatch_events) if _matches(d, transcript, usage)
            ]
            if len(candidates) == 1:
                assigned[candidates[0]].append((transcript, usage))
            else:
                unassigned.append((transcript, usage))
    rows = []
    for i, event in enumerate(dispatch_events):
        data = _obj(event.get("data"))
        row = {
            k: data.get(k)
            for k in (
                "role",
                "agent",
                "effort",
                "complexity_score",
                "worker_id",
                "task_id",
                "worktree_path",
                "pr",
                "worker_instance_id",
                "run_id",
                "session_id",
                "repo_id",
                "host_id",
                "worktree_id",
                "worktree_instance_id",
                "worktree_name",
                "branch",
                "worktree_created_at",
                "worktree_deleted_at",
                "started_at",
                "ended_at",
                "running",
                "observed_at",
                "collected_at",
                "retry_of",
                "outcome",
                "lifecycle_source",
                "source_warnings",
            )
        }
        row["row_id"] = (
            "event:"
            + hashlib.sha256(
                json.dumps(
                    [
                        event.get("ts"),
                        event.get("issue"),
                        data.get("worker_id"),
                        data.get("task_id"),
                        data.get("worktree_path"),
                    ],
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            if not dispatch_id(event)
            else None
        )
        row.update(
            schema_version=2,
            ts=iso_ts(event.get("ts")) or event.get("ts"),
            issue=event.get("issue"),
            dispatch_id=dispatch_id(event),
            record_kind="execution",
            worktree_path=event_worktree(event),
            model_requested=data.get("model") or data.get("model_requested"),
            model_effective=data.get("model_effective"),
            usage_events=[],
            tokens=None,
            cost=None,
            currency="CNY",
            usage_schema_version=2,
            warnings=[],
        )
        for transcript, usage in assigned[i]:
            row["usage_events"].append(usage)
            row["warnings"].extend(transcript.warnings)
        if parse_ts(event.get("ts")) is None:
            row["warnings"].append("bad_ts")
        if not row["worktree_path"]:
            row["warnings"].append("no_worktree_path")
        elif not assigned[i]:
            row["warnings"].append("no_transcripts")
        if not row.get("agent") and assigned[i]:
            sources = {t.source for t, _ in assigned[i]}
            row["agent"] = next(iter(sources)) if len(sources) == 1 else "mixed"
        _finish_usage(row, prices)
        rows.append(row)
    # Keep unmatched sessions as facts, not fabricated workers or dispatches.
    by_session = {}
    for transcript, usage in unassigned:
        by_session.setdefault(
            (transcript.source, transcript.session_id, transcript.cwd), []
        ).append((transcript, usage))
    for (source, sid, cwd), items in by_session.items():
        times = [u["event_ts"] for _, u in items if u.get("event_ts")]
        row = {
            "schema_version": 2,
            "record_kind": "unattributed_session",
            "session_id": sid,
            "row_id": "session:" + hashlib.sha256(f"{source}:{sid}:{cwd}".encode()).hexdigest(),
            "ts": min(times) if times else None,
            "worktree_path": cwd,
            "agent": source,
            "usage_events": [u for _, u in items],
            "warnings": ["usage_unattributed"],
            "tokens": None,
            "cost": None,
            "model_effective": None,
            "currency": "CNY",
        }
        metadata = [_obj(e.get("data")) for e in dispatch_events if event_worktree(e) == cwd]
        for k in (
            "repo_id",
            "host_id",
            "worktree_instance_id",
            "worktree_name",
            "branch",
            "collected_at",
        ):
            values = {m.get(k) for m in metadata if m.get(k)}
            row[k] = next(iter(values)) if len(values) == 1 else None
        _finish_usage(row, prices)
        rows.append(row)
    rows.sort(key=lambda row: (parse_ts(row.get("ts")) or float("inf"), row.get("row_id") or ""))
    return rows


def _finish_usage(row, prices):
    events = row["usage_events"]
    if not events:
        return
    models = sorted({u["model"] for u in events})
    row["tokens"] = {}
    for field in (*BASE_KEYS, "reasoning"):
        measured = [u["tokens"][field] for u in events if u["tokens"].get(field) is not None]
        row["tokens"][field] = sum(measured) if measured else None
    row["actual_models"] = models
    if len(models) == 1 and models[0] not in (UNKNOWN_MODEL, UNSPLIT):
        row["model_effective"] = models[0]
    elif len(models) > 1:
        row["model_effective"] = None
        row["warnings"].append("multiple_models")
    if not row.get("agent"):
        row["agent"] = "mixed"
    priced = all(isinstance(prices.get(m), dict) and any(prices[m].values()) for m in models)
    if priced and all(u["complete"] for u in events):
        row["cost"] = round(
            sum(
                compute_cost(
                    Usage(**{k: v or 0 for k, v in u["tokens"].items()}), prices[u["model"]]
                )
                for u in events
            ),
            6,
        )
    else:
        row["warnings"].extend(
            f"no_pricing:{m}" for m in models if not prices.get(m) or not any(prices[m].values())
        )
    row["warnings"] = list(dict.fromkeys(row["warnings"]))


def dumps_ledger(rows: Iterable[dict]) -> str:
    return "".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows
    )


def preserve_history(rows: list[dict], previous: list[dict]) -> list[dict]:
    """Retain archived identities and usage when sources disappear; never refresh old usage."""
    prior = {record_key(r): r for r in previous}
    out = []
    for row in rows:
        key = record_key(row)
        old = prior.pop(key, {})
        if old.get("legacy_usage_audit") and not row.get("legacy_usage_audit"):
            row["legacy_usage_audit"] = old["legacy_usage_audit"]
        for field in (
            "worktree_instance_id",
            "worktree_id",
            "worktree_created_at",
            "worktree_deleted_at",
            "worktree_path",
            "worktree_name",
            "branch",
            "repo_id",
            "host_id",
            "worker_instance_id",
            "started_at",
            "ended_at",
        ):
            if not row.get(field) and old.get(field):
                row[field] = old[field]
        if not row.get("usage_events") and old.get("tokens") is not None:
            for field in (
                "usage_events",
                "tokens",
                "actual_models",
                "model_effective",
                "cost",
                "collected_at",
            ):
                if field in old:
                    row[field] = old[field]
            if "usage_events" not in old:
                row.pop("usage_events", None)
            row.setdefault("warnings", []).append("usage_source_unavailable:retained_history")
        out.append(row)
    claimed = {
        e["event_id"]
        for r in out
        if r.get("record_kind", "execution") == "execution"
        for e in r.get("usage_events") or []
    }
    for old in prior.values():
        if old.get("record_kind") == "unattributed_session" and old.get("usage_events"):
            old = dict(old)
            old["usage_events"] = [e for e in old["usage_events"] if e["event_id"] not in claimed]
            if not old["usage_events"]:
                continue
            _finish_usage(old, {})
        out.append(old)
    return reconcile_legacy_overlap(out)


def record_key(row: dict) -> str:
    did = row.get("dispatch_id")
    if not did and str(row.get("worker_id", "")).startswith("ctx_"):
        did = row["worker_id"]
    if did:
        return "dispatch:" + str(did)
    if row.get("row_id"):
        return str(row["row_id"])
    return (
        "legacy:"
        + hashlib.sha256(
            json.dumps(
                [row.get("worker_id"), parse_ts(row.get("ts")), row.get("session_id")],
                sort_keys=True,
            ).encode()
        ).hexdigest()
    )


def reconcile_legacy_overlap(rows: list[dict]) -> list[dict]:
    """Exclude only possibly overlapping ID-less totals; retain their values as audit facts.

    An old whole-dispatch total has no event identity. It cannot safely be added to new
    event-based usage in the same possible session/worktree. Disjoint projects, hosts,
    instances, paths or explicit provider sessions still retain their known history.
    """

    def positive(tokens):
        return isinstance(tokens, dict) and any(
            isinstance(tokens.get(k), int) and not isinstance(tokens[k], bool) and tokens[k] > 0
            for k in BASE_KEYS
        )

    measured = [
        r
        for r in rows
        if isinstance(r.get("usage_events"), list)
        and any(positive(e.get("tokens")) for e in r["usage_events"])
    ]

    def may_overlap(old, new):
        for key in ("repo_id", "host_id", "worktree_instance_id"):
            if old.get(key) and new.get(key) and old[key] != new[key]:
                return False
        a, b = normalize_path(old.get("worktree_path")), normalize_path(new.get("worktree_path"))
        if a and b and a != b:
            return False
        sessions = {e.get("session_id") for e in new["usage_events"] if e.get("session_id")}
        if old.get("session_id") and sessions and old["session_id"] not in sessions:
            return False
        return True

    result = []
    for row in rows:
        if (
            not isinstance(row.get("usage_events"), list)
            and positive(row.get("tokens"))
            and any(may_overlap(row, new) for new in measured)
        ):
            audit = row.get("legacy_usage_audit") or {
                key: row.get(key)
                for key in ("tokens", "model_effective", "collected_at", "ts", "cost", "currency")
            }
            audit = {
                **audit,
                "attribution_status": "overlap_unknown",
                "reason": "ID-less historical total may overlap measured events",
            }
            row = {
                **row,
                "legacy_usage_audit": audit,
                "tokens": None,
                "usage_events": [],
                "cost": None,
                "warnings": list(
                    dict.fromkeys(
                        (row.get("warnings") or [])
                        + ["legacy_usage_overlap_unknown:not_added_to_known_total"]
                    )
                ),
            }
        result.append(row)
    return result
