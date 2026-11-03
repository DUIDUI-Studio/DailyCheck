#!/usr/bin/env python3
"""Deprecated project-level Orca routing resolver.

This module used to resolve the DailyCheck project routing table. That table is
retired; the user-global policy at
``~/.agents/skills/orca-model-routing/routing.json`` is authoritative. The
loader fails closed on a deprecated routing document so this script cannot
generate new launches.

Historical note: Orca's ``worker-start`` accepts one model at a time, so this
module used to expose a declared fallback order and build ``--retry-of``
commands after an infrastructure failure settled.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ROUTING_PATH = ROOT / "docs" / "agent-framework" / "orca-model-routing.json"

FALLBACK_FAILURES = frozenset({"infrastructure_failure", "effective_model_mismatch"})
SEMANTIC_FAILURES = frozenset(
    {
        "review_blocker",
        "ci_failure",
        "quality_failure",
        "semantic_failure",
    }
)
VALID_FAILURE_KINDS = frozenset(
    {"success", "unknown_failure", *FALLBACK_FAILURES, *SEMANTIC_FAILURES}
)


class RoutingError(ValueError):
    """The routing contract is invalid or cannot resolve the requested role."""


@dataclass(frozen=True)
class RouteAttempt:
    role: str
    attempt_index: int
    agent: str
    model: str
    effort: str | None


@dataclass(frozen=True)
class LaunchResult:
    exit_code: int
    payload: Mapping[str, Any]


def load_routing(path: Path = DEFAULT_ROUTING_PATH) -> dict[str, Any]:
    """Load a routing JSON object from disk."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RoutingError(f"cannot read routing file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RoutingError(f"invalid JSON in routing file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RoutingError("routing document must be a JSON object")
    if data.get("deprecated") is True:
        superseded = data.get("superseded_by") or "the user-global routing policy"
        raise RoutingError(
            f"routing file {path} is deprecated; use {superseded}. "
            "This project-level table must not generate launches."
        )
    return data


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_routing(config: Mapping[str, Any]) -> None:
    """Validate the project-level role/model fallback contract."""
    issues: list[str] = []
    if config.get("schema_version") != 2:
        issues.append("schema_version must be 2")

    policy = config.get("fallback_policy")
    if not isinstance(policy, Mapping):
        issues.append("fallback_policy must be an object")
        policy = {}

    max_fallbacks = policy.get("max_fallbacks_per_task")
    if not isinstance(max_fallbacks, int) or isinstance(max_fallbacks, bool) or max_fallbacks < 0:
        issues.append("fallback_policy.max_fallbacks_per_task must be a non-negative integer")
        max_fallbacks = 0

    fallback_on = policy.get("fallback_on")
    if not isinstance(fallback_on, list) or not fallback_on:
        issues.append("fallback_policy.fallback_on must be a non-empty list")
        fallback_on = []
    for kind in fallback_on:
        if kind not in VALID_FAILURE_KINDS:
            issues.append(f"unknown fallback trigger: {kind}")
        elif kind in SEMANTIC_FAILURES or kind in {"success", "unknown_failure"}:
            issues.append(f"fallback trigger must be an infrastructure condition, got: {kind}")

    roles = config.get("roles")
    if not isinstance(roles, Mapping) or not roles:
        issues.append("roles must be a non-empty object")
        roles = {}

    for role, route in roles.items():
        if not isinstance(route, Mapping):
            issues.append(f"{role}: route must be an object")
            continue
        fallbacks = route.get("fallbacks")
        if not isinstance(fallbacks, list):
            issues.append(f"{role}: fallbacks must be a list")
            continue
        if len(fallbacks) > max_fallbacks:
            issues.append(f"{role}: declares {len(fallbacks)} fallbacks, limit is {max_fallbacks}")

        if role == "release":
            if route.get("model") is not None or route.get("effort") is not None:
                issues.append("release: deterministic release must not define a model or effort")
            if fallbacks:
                issues.append("release: deterministic release must not declare model fallbacks")
            continue

        candidates = [
            {
                "agent": route.get("agent"),
                "model": route.get("model"),
                "effort": route.get("effort"),
            },
            *fallbacks,
        ]
        if not fallbacks:
            issues.append(f"{role}: at least one fallback is required")

        seen: set[tuple[str, str, str | None]] = set()
        for index, candidate in enumerate(candidates):
            label = "primary" if index == 0 else f"fallback[{index - 1}]"
            if not isinstance(candidate, Mapping):
                issues.append(f"{role} {label}: must be an object")
                continue
            agent = candidate.get("agent")
            model = candidate.get("model")
            effort = candidate.get("effort")
            if not _nonempty_string(agent):
                issues.append(f"{role} {label}: agent is required")
            if not _nonempty_string(model):
                issues.append(f"{role} {label}: model is required")
            if effort is not None and not _nonempty_string(effort):
                issues.append(f"{role} {label}: effort must be a string or null")
            key = (
                str(agent or "").strip().casefold(),
                str(model or "").strip().casefold(),
                str(effort or "").strip().casefold() or None,
            )
            if key in seen:
                issues.append(f"{role}: duplicate route candidate {key}")
            seen.add(key)

    if issues:
        raise RoutingError("\n".join(issues))


def _attempt(role: str, index: int, candidate: Mapping[str, Any]) -> RouteAttempt:
    return RouteAttempt(
        role=role,
        attempt_index=index,
        agent=str(candidate["agent"]).strip(),
        model=str(candidate["model"]).strip(),
        effort=(str(candidate["effort"]).strip() if candidate.get("effort") else None),
    )


def resolve_role_route(config: Mapping[str, Any], role: str) -> list[RouteAttempt]:
    """Return the primary and declared fallbacks for one launchable role."""
    validate_routing(config)
    roles = config["roles"]
    if role not in roles:
        raise RoutingError(f"unknown role: {role}")
    route = roles[role]
    if role == "release" or route.get("model") is None:
        raise RoutingError(f"role {role} is deterministic and cannot launch an LLM worker")
    candidates = [
        {
            "agent": route.get("agent"),
            "model": route.get("model"),
            "effort": route.get("effort"),
        },
        *route["fallbacks"],
    ]
    return [_attempt(role, index, candidate) for index, candidate in enumerate(candidates)]


def next_attempt(config: Mapping[str, Any], role: str, failed_model: str) -> RouteAttempt | None:
    """Return the next model after ``failed_model``, or None at the end."""
    attempts = resolve_role_route(config, role)
    normalized = failed_model.strip().casefold()
    for index, attempt in enumerate(attempts):
        if attempt.model.casefold() == normalized:
            return attempts[index + 1] if index + 1 < len(attempts) else None
    raise RoutingError(f"model {failed_model!r} is not in role {role}'s route")


def _normalized_model(value: Any) -> str:
    return str(value or "").strip().casefold()


def _find_value(value: Any, keys: frozenset[str]) -> Any:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key) in keys and item not in (None, ""):
                return item
        for item in value.values():
            found = _find_value(item, keys)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_value(item, keys)
            if found not in (None, ""):
                return found
    return None


def _payload_text(payload: Mapping[str, Any]) -> str:
    try:
        return json.dumps(payload, ensure_ascii=True, sort_keys=True).casefold()
    except (TypeError, ValueError):
        return str(payload).casefold()


def classify_launch_result(result: LaunchResult, requested_model: str) -> str:
    """Classify a launch receipt for fallback decisions.

    Unknown failures deliberately do not fall back. The coordinator must inspect
    the receipt and residual resources before starting another writer.
    """
    effective = _find_value(result.payload, frozenset({"effective", "effectiveModel"}))
    ok = result.payload.get("ok", True)
    if result.exit_code == 0 and ok is not False:
        if effective is not None and _normalized_model(effective) != _normalized_model(
            requested_model
        ):
            return "effective_model_mismatch"
        return "success"

    explicit = str(result.payload.get("failure_class") or "").strip().casefold()
    if explicit in SEMANTIC_FAILURES:
        return "semantic_failure"
    if explicit in FALLBACK_FAILURES:
        return explicit

    status = _find_value(
        result.payload,
        frozenset({"status_code", "http_status", "httpStatusCode"}),
    )
    try:
        status_code = int(status) if status is not None else None
    except (TypeError, ValueError):
        status_code = None
    if status_code in {401, 403, 408, 425, 429} or (
        status_code is not None and 500 <= status_code <= 599
    ):
        return "infrastructure_failure"

    text = _payload_text(result.payload)
    if any(marker in text for marker in SEMANTIC_FAILURES):
        return "semantic_failure"
    if any(
        marker in text
        for marker in (
            "http_401",
            "http_403",
            "http_429",
            "http_500",
            "status_code=401",
            "status_code=403",
            "status_code=429",
            "status_code=500",
            "timeout",
            "timed_out",
            "connection",
            "connect_failed",
            "provider_unavailable",
            "overloaded",
            "rate_limit",
            "quota",
            "authentication",
            "authorization",
            "provider_launch",
        )
    ):
        return "infrastructure_failure"
    return "unknown_failure"


def should_fallback(config: Mapping[str, Any], failure_kind: str) -> bool:
    """Whether the configured policy authorizes a model change."""
    return failure_kind in set(config["fallback_policy"]["fallback_on"])


def build_worker_start_command(
    attempt: RouteAttempt,
    *,
    task: str | None = None,
    spec: str | None = None,
    retry_of: str | None = None,
    worktree: str | None = None,
    repo: str | None = None,
    base_branch: str | None = None,
    orca_command: str = "orca",
) -> list[str]:
    """Build one ``worker-start`` command without executing it."""
    if bool(task) == bool(spec):
        raise RoutingError("exactly one of task or spec is required")
    if retry_of and not task:
        raise RoutingError("retry_of requires an existing task id")

    command = [orca_command, "orchestration", "worker-start"]
    if task:
        command.extend(["--task", task])
    else:
        command.extend(["--spec", spec or ""])
    if retry_of:
        command.extend(["--retry-of", retry_of])
    if worktree:
        command.extend(["--worktree", worktree])
    if repo:
        command.extend(["--repo", repo])
    if base_branch:
        command.extend(["--base-branch", base_branch])
    command.extend(["--agent", attempt.agent, "--model", attempt.model])
    if attempt.effort:
        command.extend(["--effort", attempt.effort])
    command.append("--json")
    return command


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DailyCheck Orca role/model fallback routing")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="Validate the routing contract")
    validate.add_argument("--config", default=str(DEFAULT_ROUTING_PATH))

    show = subparsers.add_parser("show", help="Show a role's ordered route")
    show.add_argument("--config", default=str(DEFAULT_ROUTING_PATH))
    show.add_argument("--role", required=True)
    show.add_argument("--json", action="store_true")

    classify = subparsers.add_parser(
        "classify", help="Classify a worker-start receipt for fallback eligibility"
    )
    classify.add_argument("--config", default=str(DEFAULT_ROUTING_PATH))
    classify.add_argument("--result-file", required=True)
    classify.add_argument("--exit-code", type=int, required=True)
    classify.add_argument("--requested-model", required=True)
    classify.add_argument("--json", action="store_true")

    next_parser = subparsers.add_parser(
        "next", help="Emit the next model's worker-start retry command"
    )
    next_parser.add_argument("--config", default=str(DEFAULT_ROUTING_PATH))
    next_parser.add_argument("--role", required=True)
    next_parser.add_argument("--failed-model", required=True)
    next_parser.add_argument("--task", required=True)
    next_parser.add_argument("--retry-of", required=True)
    next_parser.add_argument("--worktree")
    next_parser.add_argument("--repo")
    next_parser.add_argument("--base-branch")
    next_parser.add_argument("--orca-command", default="orca")
    next_parser.add_argument("--json", action="store_true")
    return parser


def _attempt_payload(attempt: RouteAttempt) -> dict[str, Any]:
    return {
        "role": attempt.role,
        "attempt_index": attempt.attempt_index,
        "agent": attempt.agent,
        "model": attempt.model,
        "effort": attempt.effort,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_routing(Path(args.config))
        if args.command == "validate":
            validate_routing(config)
            print("routing valid")
            return 0
        if args.command == "show":
            attempts = resolve_role_route(config, args.role)
            if args.json:
                print(json.dumps([_attempt_payload(item) for item in attempts], ensure_ascii=False))
            else:
                for item in attempts:
                    print(f"{item.attempt_index}: {item.agent} {item.model} {item.effort or '-'}")
            return 0
        if args.command == "classify":
            try:
                payload = json.loads(Path(args.result_file).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RoutingError(f"cannot read worker-start receipt: {exc}") from exc
            if not isinstance(payload, dict):
                raise RoutingError("worker-start receipt must be a JSON object")
            kind = classify_launch_result(
                LaunchResult(exit_code=args.exit_code, payload=payload),
                args.requested_model,
            )
            result = {
                "failure_kind": kind,
                "fallback_allowed": should_fallback(config, kind),
            }
            if args.json:
                print(json.dumps(result, ensure_ascii=False))
            else:
                print(
                    f"{kind}: "
                    f"{'fallback allowed' if result['fallback_allowed'] else 'do not switch model'}"
                )
            return 0

        attempt = next_attempt(config, args.role, args.failed_model)
        if attempt is None:
            print(
                f"no fallback remains for role {args.role} after {args.failed_model}",
                file=sys.stderr,
            )
            return 1
        command = build_worker_start_command(
            attempt,
            task=args.task,
            retry_of=args.retry_of,
            worktree=args.worktree,
            repo=args.repo,
            base_branch=args.base_branch,
            orca_command=args.orca_command,
        )
        payload = {**_attempt_payload(attempt), "command": command}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(" ".join(command))
        return 0
    except RoutingError as exc:
        print(f"model_routing: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
