#!/usr/bin/env python3
"""Fail-closed Gitea merge-readiness check for the Prod Agent reducer.

The helper is stdlib-only. It reads the same Gitea credential mechanism used by
the existing webhook receiver and pollers: ``GITEA_TOKEN`` first, then an
explicit token file, then the established DailyCheck token paths. Tokens are
never included in returned diagnostics or CLI output.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_REQUIRED_CONTEXTS = ("test", "agent-framework-reports")
DEFAULT_REQUIRED_CONTEXTS_ENV = ",".join(DEFAULT_REQUIRED_CONTEXTS)
DEFAULT_API_URL = "http://172.28.129.216:3000"
DEFAULT_REPO = "eric/dailycheck"
DEFAULT_TIMEOUT_S = 10.0
DEFAULT_CURL = "/usr/bin/curl"
MAX_RESPONSE_BYTES = 1_048_576
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_EVENT_SUFFIX_RE = re.compile(r"\s+\([^()]*\)\s*$")


def parse_required_contexts(value: str) -> tuple[str, ...]:
    """Parse exact, case-sensitive required context names from comma-separated text."""
    if not isinstance(value, str):
        raise ValueError("required contexts must be text")
    contexts = tuple(part.strip() for part in value.split(","))
    if not contexts or any(not part for part in contexts):
        raise ValueError("required contexts must not be empty")
    if len(set(contexts)) != len(contexts):
        raise ValueError("required contexts must be unique")
    return contexts


def _context_names(context: str) -> tuple[str, str]:
    """Return normalized full context and its exact leaf job name."""
    normalized = " ".join(context.strip().split())
    leaf = _EVENT_SUFFIX_RE.sub("", normalized).rsplit("/", 1)[-1].strip()
    return normalized, leaf


def _context_matches(context: str, required: str) -> bool:
    """Match a configured name without turning it into a permissive substring."""
    normalized, leaf = _context_names(context)
    required_normalized = " ".join(required.strip().split())
    if normalized == required_normalized:
        return True
    return "/" not in required_normalized and leaf == required_normalized


def _usable_token(value: str) -> str:
    token = value.strip()
    return "" if any(ord(char) < 32 or ord(char) == 127 for char in token) else token


def _token_from_environment(environ: dict[str, str]) -> str:
    direct = _usable_token(environ.get("GITEA_TOKEN", ""))
    if direct:
        return direct
    paths: list[str] = []
    explicit = environ.get("GITEA_TOKEN_FILE", "").strip()
    if explicit:
        paths.append(explicit)
    paths.extend(["/etc/dailycheck/gitea-token", "/root/.gitea-token"])
    for raw_path in dict.fromkeys(paths):
        try:
            token = _usable_token(Path(raw_path).read_text(encoding="utf-8"))
        except OSError:
            continue
        if token:
            return token
    return ""


@dataclass(frozen=True)
class ReadinessConfig:
    api_url: str
    repo: str
    token: str = field(repr=False)
    required_contexts: tuple[str, ...] = DEFAULT_REQUIRED_CONTEXTS
    timeout_s: float = DEFAULT_TIMEOUT_S
    curl_path: str = DEFAULT_CURL

    @classmethod
    def from_environment(cls, environ: dict[str, str] | None = None) -> ReadinessConfig:
        env = dict(os.environ if environ is None else environ)
        raw_contexts = env.get("DAILYCHECK_REQUIRED_CONTEXTS", DEFAULT_REQUIRED_CONTEXTS_ENV)
        api_url = (
            env.get("GITEA_API")
            or env.get("GITEA_URL")
            or DEFAULT_API_URL
        ).rstrip("/")
        if api_url.endswith("/api/v1"):
            api_url = api_url[: -len("/api/v1")]
        return cls(
            api_url=api_url,
            repo=env.get("GITEA_REPO", DEFAULT_REPO),
            token=_token_from_environment(env),
            required_contexts=parse_required_contexts(raw_contexts),
            timeout_s=float(env.get("GITEA_TIMEOUT", DEFAULT_TIMEOUT_S)),
            curl_path=env.get("DAILYCHECK_CURL", DEFAULT_CURL),
        )


def _not_ready(
    reason: str,
    *,
    pr_state: str | None = None,
    expected_head: str,
    actual_head: str | None = None,
    mergeable: bool | None = None,
    required_contexts: tuple[str, ...] = DEFAULT_REQUIRED_CONTEXTS,
    contexts: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "ready": False,
        "reason": reason,
        "pr_state": pr_state,
        "expected_head": expected_head.lower(),
        "actual_head": actual_head.lower() if isinstance(actual_head, str) else None,
        "mergeable": mergeable,
        "required_contexts": list(required_contexts),
        "contexts": dict(contexts or {}),
    }


class GiteaReadinessClient:
    """Query one PR and its commit statuses, returning a stable decision object."""

    def __init__(
        self,
        config: ReadinessConfig,
        *,
        get_json: Callable[[str], object] | None = None,
    ):
        self.config = config
        self._get_json_override = get_json

    def _get_json(self, path: str) -> object:
        if self._get_json_override is not None:
            return self._get_json_override(path)
        parsed = urllib.parse.urlsplit(self.config.api_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("invalid_gitea_api_url")
        if not _REPO_RE.fullmatch(self.config.repo):
            raise ValueError("invalid_gitea_repo")
        url = f"{self.config.api_url}/api/v1{path}"
        token = self.config.token.replace("\\", "\\\\").replace('"', '\\"')
        curl_config = (
            f'header = "Authorization: token {token}"\n'
            'header = "Accept: application/json"\n'
            'header = "User-Agent: dailycheck-merge-readiness/1"\n'
        )
        proc = subprocess.run(
            [
                self.config.curl_path,
                "--silent",
                "--show-error",
                "--max-time",
                str(self.config.timeout_s),
                "--max-redirs",
                "0",
                "--config",
                "-",
                "--write-out",
                "\n%{http_code}",
                url,
            ],
            input=curl_config,
            capture_output=True,
            text=True,
            timeout=self.config.timeout_s + 5,
            check=False,
        )
        if proc.returncode != 0:
            raise OSError("gitea_curl_failed")
        raw, separator, status_text = proc.stdout.rpartition("\n")
        if not separator:
            raise ValueError("gitea_response_missing_status")
        try:
            status = int(status_text.strip())
        except ValueError as exc:
            raise ValueError("gitea_response_invalid_status") from exc
        if not 200 <= status < 300:
            raise OSError("gitea_http_error")
        if len(raw.encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise ValueError("gitea_response_too_large")
        return json.loads(raw)

    def check(self, pr_number: int, expected_head_sha: str) -> dict[str, Any]:
        expected = expected_head_sha.lower()
        required = self.config.required_contexts
        if not self.config.token:
            return _not_ready(
                "credentials_unavailable",
                expected_head=expected,
                required_contexts=required,
            )
        repo_path = "/".join(
            urllib.parse.quote(part, safe="") for part in self.config.repo.split("/")
        )
        try:
            pr = self._get_json(f"/repos/{repo_path}/pulls/{pr_number}")
            if not isinstance(pr, dict):
                return _not_ready(
                    "invalid_response",
                    expected_head=expected,
                    required_contexts=required,
                )
            pr_state = pr.get("state")
            head = pr.get("head")
            mergeable = pr.get("mergeable")
            if not isinstance(pr_state, str) or not isinstance(head, dict):
                return _not_ready(
                    "invalid_response",
                    pr_state=pr_state if isinstance(pr_state, str) else None,
                    expected_head=expected,
                    mergeable=mergeable if isinstance(mergeable, bool) else None,
                    required_contexts=required,
                )
            actual_head = head.get("sha")
            if not isinstance(actual_head, str) or not actual_head:
                return _not_ready(
                    "invalid_response",
                    pr_state=pr_state,
                    expected_head=expected,
                    mergeable=mergeable if isinstance(mergeable, bool) else None,
                    required_contexts=required,
                )
            actual_head = actual_head.lower()
            if pr_state.lower() != "open":
                return _not_ready(
                    "pr_not_open",
                    pr_state=pr_state,
                    expected_head=expected,
                    actual_head=actual_head,
                    mergeable=mergeable if isinstance(mergeable, bool) else None,
                    required_contexts=required,
                )
            if actual_head != expected:
                return _not_ready(
                    "head_changed",
                    pr_state=pr_state,
                    expected_head=expected,
                    actual_head=actual_head,
                    mergeable=mergeable if isinstance(mergeable, bool) else None,
                    required_contexts=required,
                )
            if mergeable is not True:
                return _not_ready(
                    "not_mergeable",
                    pr_state=pr_state,
                    expected_head=expected,
                    actual_head=actual_head,
                    mergeable=mergeable if isinstance(mergeable, bool) else None,
                    required_contexts=required,
                )

            statuses_data = self._get_json(
                f"/repos/{repo_path}/commits/{urllib.parse.quote(expected, safe='')}/status"
            )
            if not isinstance(statuses_data, dict) or not isinstance(
                statuses_data.get("statuses"), list
            ):
                return _not_ready(
                    "invalid_response",
                    pr_state=pr_state,
                    expected_head=expected,
                    actual_head=actual_head,
                    mergeable=True,
                    required_contexts=required,
                )

            matched: dict[str, list[str]] = {name: [] for name in required}
            for raw_status in statuses_data["statuses"]:
                if not isinstance(raw_status, dict):
                    return _not_ready(
                        "invalid_response",
                        pr_state=pr_state,
                        expected_head=expected,
                        actual_head=actual_head,
                        mergeable=True,
                        required_contexts=required,
                    )
                context = raw_status.get("context")
                status = raw_status.get("status")
                if not isinstance(context, str) or not isinstance(status, str):
                    return _not_ready(
                        "invalid_response",
                        pr_state=pr_state,
                        expected_head=expected,
                        actual_head=actual_head,
                        mergeable=True,
                        required_contexts=required,
                    )
                for name in required:
                    if _context_matches(context, name):
                        matched[name].append(status.strip().lower())

            contexts: dict[str, str] = {}
            for name, values in matched.items():
                if not values:
                    continue
                if any(value in {"failure", "error"} for value in values):
                    contexts[name] = "failure"
                elif any(value != "success" for value in values):
                    contexts[name] = "pending"
                else:
                    contexts[name] = "success"

            missing = [name for name in required if name not in contexts]
            if missing:
                return _not_ready(
                    "checks_missing",
                    pr_state=pr_state,
                    expected_head=expected,
                    actual_head=actual_head,
                    mergeable=True,
                    required_contexts=required,
                    contexts=contexts,
                )
            if any(value == "failure" for value in contexts.values()):
                return _not_ready(
                    "checks_failed",
                    pr_state=pr_state,
                    expected_head=expected,
                    actual_head=actual_head,
                    mergeable=True,
                    required_contexts=required,
                    contexts=contexts,
                )
            if any(value != "success" for value in contexts.values()):
                return _not_ready(
                    "checks_running",
                    pr_state=pr_state,
                    expected_head=expected,
                    actual_head=actual_head,
                    mergeable=True,
                    required_contexts=required,
                    contexts=contexts,
                )
            return {
                "ready": True,
                "reason": "ready",
                "pr_state": pr_state,
                "expected_head": expected,
                "actual_head": actual_head,
                "mergeable": True,
                "required_contexts": list(required),
                "contexts": contexts,
            }
        except (
            OSError,
            TimeoutError,
            ValueError,
            UnicodeError,
            subprocess.SubprocessError,
        ):
            return _not_ready(
                "gitea_unavailable",
                expected_head=expected,
                required_contexts=required,
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument(
        "--required-contexts",
        default=os.environ.get(
            "DAILYCHECK_REQUIRED_CONTEXTS", DEFAULT_REQUIRED_CONTEXTS_ENV
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base = ReadinessConfig.from_environment()
    config = ReadinessConfig(
        api_url=base.api_url,
        repo=base.repo,
        token=base.token,
        required_contexts=parse_required_contexts(args.required_contexts),
        timeout_s=base.timeout_s,
        curl_path=base.curl_path,
    )
    result = GiteaReadinessClient(config).check(args.pr, args.head_sha)
    print(
        json.dumps(
            result,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ),
        flush=True,
    )
    return 0 if result["ready"] else 10


if __name__ == "__main__":
    raise SystemExit(main())
