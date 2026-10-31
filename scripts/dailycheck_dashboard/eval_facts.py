"""Worktree evaluation projection over immutable execution/usage facts (Issue #102)."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict

from scripts.model_eval.collect_pure import (
    BASE_KEYS,
    normalize_path,
    parse_ts,
    reconcile_legacy_overlap,
)
from scripts.model_eval.collect_pure import record_key as row_key

REQUIRED_CHECKS = ("CI / test (pull_request)", "CI / agent-framework-reports (pull_request)")
DEFAULT_REPO = "git:hub01/eric/dailycheck"


def token_sum(tokens):
    if not isinstance(tokens, dict):
        return None
    known = [
        tokens.get(k)
        for k in BASE_KEYS
        if isinstance(tokens.get(k), int) and not isinstance(tokens.get(k), bool) and tokens[k] >= 0
    ]
    return sum(known) if known else None


def complete_tokens(tokens):
    return isinstance(tokens, dict) and all(
        isinstance(tokens.get(k), int) and not isinstance(tokens.get(k), bool) and tokens[k] >= 0
        for k in BASE_KEYS
    )


def delivery_state(pr: dict | None, evidence: dict | None = None) -> str:
    """No green delivery badge without every independent, SHA-bound proof."""
    if not pr:
        return "unknown"
    state = pr.get("state", "UNKNOWN").upper()
    if state == "CLOSED":
        return "closed_unmerged"
    head = pr.get("head_sha")
    ev = evidence or pr.get("delivery_evidence") or {}
    sha_ok = bool(head and ev.get("head_sha") == head)
    checks = ev.get("required_checks") or {}
    ci = sha_ok and all(
        isinstance(checks.get(k), dict)
        and checks[k].get("sha") == head
        and checks[k].get("state") == "success"
        for k in REQUIRED_CHECKS
    )
    deploy = ev.get("deployment") or {}
    target = pr.get("merge_sha") or head
    deployment_matches = sha_ok and deploy.get("sha") == target
    if deployment_matches and deploy.get("result") == "failed":
        return "deploy_failed"
    if state == "MERGED":
        observed = parse_ts(deploy.get("observed_until"))
        verified = parse_ts(deploy.get("verified_at"))
        required = deploy.get("required_observation_seconds")
        observation_ok = (
            isinstance(required, (int, float))
            and not isinstance(required, bool)
            and required >= 1800
            and observed is not None
            and verified is not None
            and observed - verified >= required
        )
        if (
            ci
            and ev.get("linked_issues_closed") is True
            and deployment_matches
            and deploy.get("result") == "success"
            and deploy.get("verification") == "success"
            and deploy.get("observation") == "success"
            and observation_ok
            and deploy.get("evidence_ref")
        ):
            return "delivered"
        return "awaiting_deploy"
    return "awaiting_prod" if ci else "ci_unknown"


def prepare_facts(
    rows: list[dict],
    prs: list[dict],
    *,
    snapshot: str,
    pr_data_ok=True,
    unavailable=False,
    deploy_results=(),
) -> list[dict]:
    """Normalize once; the browser applies windows/filters to these same facts."""
    unique = {}
    for row in rows:
        unique[row_key(row)] = row
    facts, seen_events = [], set()
    pr_by_key = {(p.get("repo_id") or DEFAULT_REPO, int(p["number"])): p for p in prs}
    reconciled = reconcile_legacy_overlap(list(unique.values()))
    for row in reconciled:
        rid = row_key(row)
        repo, host = row.get("repo_id") or DEFAULT_REPO, row.get("host_id") or "unknown"
        path = normalize_path(row.get("worktree_path"))
        instance = row.get("worktree_instance_id")
        quality = "instance" if instance else "historical_path" if path else "unattributed"
        tree_key = json.dumps([repo, host, instance or path or "unattributed"], ensure_ascii=False)
        raw_events = row.get("usage_events")
        events = []
        if isinstance(raw_events, list):
            for e in raw_events:
                if not isinstance(e, dict) or not e.get("event_id") or e["event_id"] in seen_events:
                    continue
                seen_events.add(e["event_id"])
                model = e.get("model") or "unknown"
                events.append(
                    {
                        **e,
                        "at": parse_ts(e.get("event_ts")),
                        "model": model,
                        "total": token_sum(e.get("tokens")),
                        "complete": e.get("complete") is not False
                        and complete_tokens(e.get("tokens")),
                    }
                )
        elif token_sum(row.get("tokens")) is not None:
            model = row.get("model_effective") or "unknown"
            if "," in model or "multiple_models" in (row.get("warnings") or []):
                model = "unsplittable"
            events.append(
                {
                    "event_id": rid + ":legacy",
                    "at": None,
                    "model": model,
                    "tokens": row["tokens"],
                    "total": token_sum(row["tokens"]),
                    "complete": complete_tokens(row["tokens"]),
                    "attribution": "dispatch_time",
                }
            )
        models = sorted({e["model"] for e in events} or set(row.get("actual_models") or []))
        if not models:
            model = row.get("model_effective") or "unknown"
            models = ["unsplittable" if "," in model else model]
        start, end = parse_ts(row.get("started_at")), parse_ts(row.get("ended_at"))
        if end is None and row.get("running") is True:
            end = parse_ts(row.get("observed_at"))  # frozen observed snapshot, never arbitrary now
        interval = [start, end] if start is not None and end is not None and end >= start else None
        numbers = set()
        if row.get("pr"):
            numbers.add(int(row["pr"]))
        # Exact repository + branch association, never Issue-number substring inference.
        branch = str(row.get("branch") or "").removeprefix("refs/heads/")
        if branch:
            numbers.update(
                n for (r, n), p in pr_by_key.items() if r == repo and p.get("head_ref") == branch
            )
        links = []
        for number in sorted(numbers):
            pr = pr_by_key.get((repo, number)) if pr_data_ok else None
            evidence = row.get("delivery_evidence")
            link = {
                "key": f"{repo}#{number}",
                "number": number,
                "repo": repo,
                "state": pr.get("state", "UNKNOWN") if pr else "UNKNOWN",
                "delivery": delivery_state(pr, evidence),
                "evidence": evidence or {},
            }
            result = [
                e
                for e in deploy_results
                if e.get("pr") == number
                and pr
                and e.get("sha") in (pr.get("head_sha"), pr.get("merge_sha"))
            ]
            if result:
                latest = sorted(result, key=lambda e: parse_ts(e.get("ts")) or 0)[-1]
                link["deploy_result"] = latest
                if isinstance(latest.get("rc"), int) and latest["rc"] != 0:
                    link["delivery"] = "deploy_failed"
                # rc=0 only proves process success, not observation/check completion.
            links.append(link)
        collected = parse_ts(row.get("collected_at"))
        now = parse_ts(snapshot)
        fresh = (
            "unavailable"
            if unavailable
            else "unknown"
            if collected is None
            else ("stale" if now is not None and now - collected > 1800 else "fresh")
        )
        facts.append(
            {
                "id": rid,
                "tree": tree_key,
                "repo": repo,
                "host": host,
                "path": path,
                "instance": instance,
                "quality": quality,
                "name": row.get("worktree_name")
                or (path.rsplit("/", 1)[-1] if path else "未归属记录"),
                "branch": branch,
                "deleted_at": row.get("worktree_deleted_at"),
                "worker": row.get("worker_instance_id"),
                "dispatch": row.get("dispatch_id")
                or (
                    row.get("worker_id")
                    if str(row.get("worker_id", "")).startswith("ctx_")
                    else None
                ),
                "session": row.get("session_id"),
                "kind": row.get("record_kind", "execution"),
                "task": row.get("task_id"),
                "retry_of": row.get("retry_of"),
                "role": row.get("role") or "unknown",
                "agent": row.get("agent") or "unknown",
                "requested": row.get("model_requested"),
                "effort": row.get("effort"),
                "complexity": row.get("complexity_score"),
                "models": models,
                "events": events,
                "interval": interval,
                "started_at": row.get("started_at"),
                "ended_at": row.get("ended_at"),
                "running": row.get("running") is True,
                "at": parse_ts(row["fact_ts"] if "fact_ts" in row else row.get("ts")),
                "legacy_usage_audit": row.get("legacy_usage_audit"),
                "prs": links,
                "issue": row.get("issue"),
                "freshness": fresh,
                "collected_at": row.get("collected_at"),
                "warnings": (row.get("warnings") or []) + (row.get("source_warnings") or []),
            }
        )
    return facts


def interval_union(intervals):
    end, total = None, 0
    for a, b in sorted(intervals):
        total += max(0, b - max(a, end if end is not None else a))
        end = max(end, b) if end is not None else b
    return total


def select_facts(
    facts, *, start=None, end=None, role=None, model=None, status=None, query="", quality=None
):
    """Half-open UTC window. Historical totals explicitly use dispatch-time attribution."""
    lo, hi = start if start is not None else float("-inf"), end if end is not None else float("inf")
    out = []
    unbounded = start is None and end is None
    for f in facts:
        if role and f["role"] != role or model and model not in f["models"]:
            continue
        if status and not any(p["delivery"] == status for p in f["prs"]):
            if status != "active" or f["prs"]:
                continue
        if quality and f["freshness"] != quality:
            continue
        if (
            query
            and query.lower()
            not in (json.dumps(f, ensure_ascii=False) + f" #{f.get('issue')}").lower()
        ):
            continue
        events = [
            e
            for e in f["events"]
            if (not model or e["model"] == model)
            and (
                (e["at"] is not None and lo <= e["at"] < hi)
                or (e["at"] is None and (unbounded or f["at"] is not None and lo <= f["at"] < hi))
            )
        ]
        interval = f["interval"]
        clipped = [max(lo, interval[0]), min(hi, interval[1])] if interval else None
        if clipped and clipped[1] <= clipped[0]:
            clipped = None
        # A multi-model execution has no measured per-model runtime segments.
        if model and len(f["models"]) > 1:
            clipped = None
        if unbounded or events or clipped or (f["at"] is not None and lo <= f["at"] < hi):
            out.append({**f, "events": events, "interval": clipped})
    return out


def summarize(facts):
    workers, attempts, prs, times = set(), set(), {}, defaultdict(list)
    totals, coverage, known = dict.fromkeys((*BASE_KEYS, "reasoning"), 0), 0, False
    measured_fields = set()
    intervals, time_rows = [], 0
    for f in facts:
        if f["worker"]:
            workers.add(f["worker"])
        if f["kind"] == "execution":
            attempts.add(f["dispatch"] or f["id"])
        prs.update((p["key"], p) for p in f["prs"])
        if f["events"] and all(e["complete"] for e in f["events"]):
            coverage += 1
        for e in f["events"]:
            if e["total"] is not None:
                known = True
            for k in totals:
                value = (e.get("tokens") or {}).get(k)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    totals[k] += value
                    measured_fields.add(k)
        if f["interval"]:
            intervals.append(f["interval"])
            if f["worker"]:
                times[f["worker"]].append(f["interval"])
                time_rows += 1
    return {
        "workers": len(workers),
        "executions": len(attempts),
        "prs": list(prs.values()),
        "unknown_workers": sum(f["kind"] == "execution" and not f["worker"] for f in facts),
        "unattributed_sessions": sum(f["kind"] != "execution" for f in facts),
        "retries": len({f["dispatch"] or f["id"] for f in facts if f["retry_of"]}),
        "tokens": {k: v if k in measured_fields else None for k, v in totals.items()},
        "total": sum(totals[k] for k in BASE_KEYS) if known else None,
        "token_coverage": coverage,
        "time_coverage": time_rows,
        "rows": len(facts),
        "worker_seconds": sum(interval_union(v) for v in times.values()) if times else None,
        "span_seconds": max(i[1] for i in intervals) - min(i[0] for i in intervals)
        if intervals
        else None,
        "model_coverage": sum(
            "unknown" not in f["models"] and "unsplittable" not in f["models"] for f in facts
        ),
        "identity_coverage": sum(f["quality"] == "instance" for f in facts),
        "delivery": dict(Counter(p["delivery"] for p in prs.values())),
    }


def aggregate_worktrees(facts, **filters):
    groups = defaultdict(list)
    for f in select_facts(facts, **filters):
        groups[f["tree"]].append(f)
    return [{"tree": key, "facts": values, **summarize(values)} for key, values in groups.items()]


def compact_ui_facts(facts):
    """Exact for UI's CST-date windows; full audit events stay in ledger, never truncate.

    One bucket per execution/provider session/actual model/CST day. Timing intervals
    remain untouched. Timestamp-less totals keep dispatch-time attribution.
    """
    import math

    compact = []
    for fact in facts:
        buckets = {}
        for event in fact["events"]:
            at = event["at"]
            day_start = (
                math.floor((at + 8 * 3600) / 86400) * 86400 - 8 * 3600 if at is not None else None
            )
            key = (event.get("session_id"), event["model"], day_start)
            if key not in buckets:
                buckets[key] = {
                    "event_id": "bucket:"
                    + hashlib.sha256(json.dumps(key).encode()).hexdigest()[:16],
                    "at": day_start,
                    "model": event["model"],
                    "session_id": event.get("session_id"),
                    "tokens": dict.fromkeys((*BASE_KEYS, "reasoning"), None),
                    "total": None,
                    "complete": True,
                    "event_count": 0,
                    "first_at": at,
                    "last_at": at,
                    "first_event_id": event["event_id"],
                    "last_event_id": event["event_id"],
                    "counter_reset": False,
                }
            bucket = buckets[key]
            bucket["event_count"] += 1
            bucket["complete"] = bucket["complete"] and event["complete"]
            bucket["last_event_id"] = event["event_id"]
            bucket["counter_reset"] = bucket["counter_reset"] or event.get("counter_reset", False)
            if at is not None:
                bucket["first_at"] = min(bucket["first_at"], at)
                bucket["last_at"] = max(bucket["last_at"], at)
            if event["total"] is not None:
                bucket["total"] = (bucket["total"] or 0) + event["total"]
            for field in bucket["tokens"]:
                value = (event.get("tokens") or {}).get(field)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    bucket["tokens"][field] = (bucket["tokens"][field] or 0) + value
        compact.append({**fact, "events": list(buckets.values())})
    return compact
