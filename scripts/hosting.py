"""Hosting API boundary. GitHub is opt-in; credentials never cross roles or hosts."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit


class Provider(str, Enum):
    GITEA = "gitea"
    GITHUB = "github"


class AccessRole(str, Enum):
    READ = "read"
    OPS_READ = "ops_read"
    FEEDBACK = "feedback"
    REPORT = "report"
    FINALIZE = "finalize"


class HostingError(RuntimeError):
    def __init__(self, reason: str, status: int = 0):
        super().__init__(reason)
        self.status = status


class PayloadError(ValueError):
    """Invalid external body; distinct from unavailable/invalid hosting configuration."""


def validate_json_tree(value: Any, *, path: str = "$", depth: int = 0) -> None:
    """Reject values Python accepts but RFC JSON cannot represent faithfully."""
    if depth > 100:
        raise PayloadError(f"{path} is nested too deeply")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise PayloadError(f"{path} contains an invalid Unicode surrogate") from exc
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PayloadError(f"{path} contains a non-finite number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            validate_json_tree(item, path=f"{path}[{index}]", depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise PayloadError(f"{path} contains a non-string object key")
            try:
                key.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise PayloadError(f"{path} contains an invalid object key") from exc
            validate_json_tree(item, path=f"{path}.*", depth=depth + 1)
        return
    raise PayloadError(f"{path} contains a non-JSON value")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PayloadError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise PayloadError(f"non-RFC JSON number {value}")


def strict_json_loads(raw: bytes | str, *, max_bytes: int = 1_048_576):
    """Validate untrusted JSON without accepting Python-only values or duplicate keys."""
    try:
        if len(raw if isinstance(raw, bytes) else raw.encode("utf-8")) > max_bytes:
            raise PayloadError("payload_too_large")
        payload = json.loads(
            raw, parse_constant=_reject_json_constant, object_pairs_hook=_strict_object
        )
        validate_json_tree(payload)
        return payload
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise PayloadError(str(exc)) from exc


def parse_json_object(raw: bytes | str) -> dict:
    payload = strict_json_loads(raw)
    if not isinstance(payload, dict):
        raise PayloadError("JSON body must be an object")
    return payload


def provider(environ: Mapping[str, str] | None = None) -> Provider:
    env = os.environ if environ is None else environ
    try:
        return Provider(env.get("DAILYCHECK_HOSTING_PROVIDER", "gitea"))
    except ValueError as exc:
        raise HostingError("invalid_hosting_provider") from exc


def github_ordinary_enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    if provider(env) is not Provider.GITHUB:
        return False
    lane = env.get("DAILYCHECK_GITHUB_LANE", "shadow")
    if lane not in {"shadow", "ordinary"}:
        raise HostingError("invalid_github_lane")
    return lane == "ordinary"


GITHUB_REPOSITORY_NODE = "R_kgDOVl3HNQ"
GITHUB_FF_MUTATION = (
    "mutation DailyCheckFinalize($input: UpdateRefsInput!) { "
    "updateRefs(input: $input) { clientMutationId } }"
)


def github_ff_payload(base: str, head: str, head_ref: str, attempt_id: str) -> dict:
    if (any(not isinstance(s, str) or not re.fullmatch(r"[0-9a-f]{40}", s)
            or s == "0" * 40 for s in (base, head)) or base == head
            or not isinstance(head_ref, str)
            or not re.fullmatch(r"refs/heads/[A-Za-z0-9_/-][A-Za-z0-9_./-]*", head_ref)
            or head_ref == "refs/heads/main"
            or any(x in head_ref for x in ("..", "//", "/.", ".lock"))
            or head_ref.endswith(("/", "."))
            or not isinstance(attempt_id, str) or not re.fullmatch(r"[0-9a-f]{32}", attempt_id)):
        raise HostingError("invalid_finalization_binding")
    return {"query": GITHUB_FF_MUTATION, "variables": {"input": {
        "repositoryId": GITHUB_REPOSITORY_NODE,
        "clientMutationId": attempt_id,
        "refUpdates": [
            {"name": "refs/heads/main", "beforeOid": base, "afterOid": head, "force": False},
            {"name": head_ref, "beforeOid": head, "afterOid": head, "force": False},
        ],
    }}}


def _token(env: Mapping[str, str], names: tuple[str, ...], file_key: str) -> str:
    value = next((env[n] for n in names if env.get(n)), "")
    if not value and env.get(file_key):
        try:
            value = Path(env[file_key]).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise HostingError("credential_file_unavailable") from exc
    value = value.strip()
    if any(ord(c) < 33 or ord(c) == 127 for c in value):
        raise HostingError("invalid_credentials")
    return value


def is_shadow_event(payload: object) -> bool:
    """Identify delayed/untagged target events as well as explicit shadow provenance."""
    if not isinstance(payload, dict):
        return False
    if any(
        isinstance(payload.get(key), str) and payload[key].strip().casefold() == value
        for key, value in (("hosting_provider", "github"), ("intake_mode", "shadow"))
    ):
        return True
    repository = payload.get("repository")
    identities = [payload.get("repo_full_name"), payload.get("repo")]
    if isinstance(repository, dict):
        identities.append(repository.get("full_name"))
    if any(
        isinstance(repo, str) and repo.strip().casefold() == "duidui-studio/dailycheck"
        for repo in identities
    ):
        return True
    nested = payload.get("payload")
    return isinstance(nested, dict) and nested is not payload and is_shadow_event(nested)


def require_production_work(payload: object = None) -> None:
    if provider() is Provider.GITHUB or is_shadow_event(payload):
        raise HostingError("github_shadow_only")


def require_shadow_path(path: Path) -> Path:
    raw = os.environ.get("DAILYCHECK_GITHUB_SHADOW_DIR", "")
    root = Path(raw)
    protected = (
        Path("/var/lib/dailycheck-prod-agent").resolve(),
        Path("/var/lib/dailycheck-deploy").resolve(),
    )
    if (
        not root.is_absolute()
        or root.resolve() == Path("/")
        or any(
            root.resolve().is_relative_to(p) or p.is_relative_to(root.resolve()) for p in protected
        )
    ):
        raise HostingError("isolated_shadow_directory_required")
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise HostingError("production_intake_unsupported") from exc
    return root


@dataclass(frozen=True)
class HostingConfig:
    provider: Provider
    api_url: str
    repo: str
    role: AccessRole
    token: str = field(repr=False)
    web_url: str = "https://github.com"
    repo_id: int = 1448986421
    base_ref: str = "main"
    workflow_path: str = ".github/workflows/ci.yml"
    actions_app_id: int = 15368
    timeout: float = 15.0
    curl: str = "/usr/bin/curl"

    def __post_init__(self):
        parsed = urlsplit(self.api_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise HostingError("invalid_api_url")
        if self.provider is Provider.GITHUB and self.api_url != "https://api.github.com":
            raise HostingError("invalid_github_api_identity")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repo):
            raise HostingError("invalid_repository")
        if self.provider is Provider.GITHUB and (
            (self.repo, self.repo_id) != (
                ("DUIDUI-Studio/DailyCheck-ops", 1449125557)
                if self.role in {AccessRole.FEEDBACK, AccessRole.OPS_READ}
                else ("DUIDUI-Studio/DailyCheck", 1448986421)
            )
            or self.web_url != "https://github.com"
        ):
            raise HostingError("invalid_github_repository_identity")
        if not math.isfinite(self.timeout) or not 0 < self.timeout <= 60:
            raise HostingError("invalid_timeout")
        if any(ord(c) < 33 or ord(c) == 127 for c in self.token):
            raise HostingError("invalid_credentials")

    @classmethod
    def from_environment(
        cls, role: AccessRole = AccessRole.READ, environ: Mapping[str, str] | None = None
    ) -> HostingConfig:
        env = os.environ if environ is None else environ
        selected = provider(env)
        if selected is Provider.GITEA:
            return cls(
                selected,
                env.get("GITEA_API", "http://172.28.129.216:3000/api/v1").rstrip("/"),
                env.get("GITEA_REPO", "eric/dailycheck"),
                role,
                _token(env, ("GITEA_TOKEN",), "GITEA_TOKEN_FILE"),
                web_url=env.get("GITEA_WEB_BASE", ""),
            )
        names = {
            AccessRole.READ: ("DAILYCHECK_GITHUB_READ_TOKEN",),
            AccessRole.OPS_READ: ("DAILYCHECK_GITHUB_OPS_READ_TOKEN",),
            AccessRole.FEEDBACK: ("DAILYCHECK_GITHUB_FEEDBACK_TOKEN",),
            AccessRole.REPORT: ("DAILYCHECK_GITHUB_REPORT_TOKEN",),
            AccessRole.FINALIZE: (),
        }[role]
        if role is AccessRole.FINALIZE:
            if not github_ordinary_enabled(env):
                raise HostingError("github_finalization_unsupported")
            if not env.get("DAILYCHECK_GITHUB_FINALIZE_TOKEN_FILE"):
                raise HostingError("explicit_finalization_credential_required")
            final_token = _token(env, (), "DAILYCHECK_GITHUB_FINALIZE_TOKEN_FILE")
            shared = [env.get(k, "") for k in ("GH_TOKEN", "GITHUB_TOKEN", "GITEA_TOKEN")]
            for other in (AccessRole.READ, AccessRole.REPORT, AccessRole.FEEDBACK, AccessRole.OPS_READ):
                prefix = f"DAILYCHECK_GITHUB_{other.value.upper()}_TOKEN"
                shared.append(_token(env, (prefix,), prefix + "_FILE"))
            if not final_token or final_token in shared:
                raise HostingError("separate_finalization_credential_required")
        private = role in {AccessRole.FEEDBACK, AccessRole.OPS_READ}
        repo = env.get("DAILYCHECK_GITHUB_REPO", "DUIDUI-Studio/DailyCheck")
        repo_id = 1448986421
        if private:
            repo = env.get("DAILYCHECK_GITHUB_OPERATIONS_REPO", "")
            if env.get("DAILYCHECK_GITHUB_OPERATIONS_REPO_ID") != "1449125557":
                raise HostingError("explicit_operations_identity_required")
            if env.get("DAILYCHECK_GITHUB_OPERATIONS_EXPECT_PRIVATE") != "true":
                raise HostingError("explicit_private_destination_required")
            repo_id = 1449125557
        return cls(
            selected,
            env.get("DAILYCHECK_GITHUB_API", "https://api.github.com"),
            repo,
            role,
            final_token if role is AccessRole.FINALIZE else
            _token(env, names, f"DAILYCHECK_GITHUB_{role.value.upper()}_TOKEN_FILE"),
            repo_id=repo_id,
            curl=env.get("DAILYCHECK_CURL", "/usr/bin/curl"),
        )

    def pr_url(self, number: int) -> str:
        noun = "pull" if self.provider is Provider.GITHUB else "pulls"
        return f"{self.web_url}/{self.repo}/{noun}/{number}"


class HostingClient:
    """Small curl transport; no redirect or credential in argv, bounded complete pages."""

    PER_PAGE = 100
    MAX_PAGES = 50
    MAX_BYTES = 4 * 1024 * 1024

    def __init__(self, config: HostingConfig):
        self.config = config

    def request(self, method: str, path: str, payload: dict | None = None, *,
                guard_fd: int | None = None, attempt_fd: int | None = None) -> tuple[object, str]:
        c = self.config
        if not c.token:
            raise HostingError("credentials_unavailable")
        if method != "GET" and c.role in {AccessRole.READ, AccessRole.OPS_READ}:
            raise HostingError("read_role_cannot_write")
        # Only trusted publication/finalization mutators hand explicit
        # deployment-fence descriptors to their HTTP child. Read/feedback/Gitea
        # requests keep the ordinary close_fds default and inherit nothing.
        if guard_fd is not None:
            if type(guard_fd) is not int or guard_fd < 3 or method == "GET":
                raise HostingError("invalid_transport_guard")
            try:
                os.fstat(guard_fd)
            except OSError as exc:
                raise HostingError("invalid_transport_guard") from exc
        finalization = c.provider is Provider.GITHUB and c.role is AccessRole.FINALIZE
        if finalization and method != "GET":
            try:
                data = payload["variables"]["input"]
                main, head = data["refUpdates"]
                expected = github_ff_payload(main["beforeOid"], main["afterOid"],
                                             head["name"], data["clientMutationId"])
                valid = payload == expected
            except (KeyError, TypeError, ValueError, HostingError):
                valid = False
            if (not github_ordinary_enabled() or method != "POST" or path != "/graphql"
                    or not valid or guard_fd is None or attempt_fd is None):
                raise HostingError("unsupported_finalization_write")
        if attempt_fd is not None:
            if (not finalization or method == "GET" or type(attempt_fd) is not int
                    or attempt_fd < 3 or attempt_fd == guard_fd):
                raise HostingError("invalid_transport_attempt")
            try:
                os.fstat(attempt_fd)
            except OSError as exc:
                raise HostingError("invalid_transport_attempt") from exc
        routes = {
            AccessRole.FEEDBACK: rf"/repos/{re.escape(c.repo)}/(?:issues|labels)",
            AccessRole.REPORT: rf"/repos/{re.escape(c.repo)}/issues/(?:[1-9][0-9]*/comments|comments/[1-9][0-9]*)",
        }
        allowed = re.fullmatch(routes.get(c.role, r"(?!)"), path)
        if method != "GET" and not finalization and (not allowed or method not in {"POST", "PATCH"}):
            raise HostingError("unsupported_hosting_write")
        compare = re.fullmatch(
            rf"/repos/{re.escape(c.repo)}/compare/[0-9a-f]{{40}}\.\.\.[0-9a-f]{{40}}"
            r"(?:\?per_page=1&page=2)?", path
        )
        if (not (finalization and path == "/graphql")
                and path != f"/repos/{c.repo}" and not path.startswith(f"/repos/{c.repo}/")) or any(
            x in path for x in ("\r", "\n", "#")
        ) or ".." in path and not compare:
            raise HostingError("invalid_api_path")
        if c.provider is Provider.GITHUB and c.role is AccessRole.FEEDBACK and method != "GET":
            if method != "POST":
                raise HostingError("unsupported_private_feedback_write")
            if path.endswith("/labels"):
                valid = payload == {
                    "name": "user-feedback", "color": "0e8a16",
                    "description": "用户在站内提交的使用问题，由管理员确认后派发",
                }
            else:
                valid = (
                    isinstance(payload, dict)
                    and set(payload) == {"title", "body", "labels"}
                    and isinstance(payload["title"], str)
                    and isinstance(payload["body"], str)
                    and payload["labels"] == ["user-feedback"]
                )
            if not valid:
                raise HostingError("invalid_private_feedback_payload")
            # Re-read authenticated metadata immediately before EVERY private mutation.
            self.verify_repository()
        auth = "Bearer" if c.provider is Provider.GITHUB else "token"
        headers = {
            "Authorization": f"{auth} {c.token}",
            "Accept": "application/vnd.github+json"
            if c.provider is Provider.GITHUB
            else "application/json",
            "User-Agent": "dailycheck-hosting/1",
            "Content-Type": "application/json",
        }
        if c.provider is Provider.GITHUB:
            headers["X-GitHub-Api-Version"] = "2022-11-28"
        config = "".join(f"header = {json.dumps(k + ': ' + v)}\n" for k, v in headers.items())
        if payload is not None:
            config += f"data = {json.dumps(json.dumps(payload))}\n"
        try:
            with tempfile.NamedTemporaryFile() as dump:
                proc = subprocess.run(
                    [
                        c.curl,
                        "--disable",
                        "--silent",
                        "--max-time",
                        str(c.timeout),
                        "--max-redirs",
                        "0",
                        "--max-filesize",
                        str(self.MAX_BYTES),
                        "--config",
                        "-",
                        "--request",
                        method,
                        "--dump-header",
                        dump.name,
                        "--write-out",
                        "\n%{http_code}",
                        c.api_url + path,
                    ],
                    input=config,
                    text=True,
                    capture_output=True,
                    timeout=c.timeout + 5,
                    check=False,
                    **({"pass_fds": (guard_fd,) + ((attempt_fd,) if attempt_fd is not None else ())}
                       if guard_fd is not None else {}),
                )
                raw_headers = Path(dump.name).read_bytes().decode("latin1")
            raw, sep, status = proc.stdout.rpartition("\n")
            if proc.returncode or not sep or len(raw.encode()) > self.MAX_BYTES:
                raise HostingError("hosting_network_error")
            code = int(status)
            expected = {"GET": 200, "POST": 200 if finalization else 201, "PATCH": 200}
            if not 200 <= code < 300 or (
                c.provider is Provider.GITHUB and code != expected.get(method)
            ):
                raise HostingError("hosting_http_error", code)
            data = strict_json_loads(raw, max_bytes=4_194_304)
            return data, raw_headers
        except (OSError, ValueError, UnicodeError, subprocess.SubprocessError) as exc:
            raise HostingError("hosting_invalid_response") from exc

    def get(self, path: str) -> object:
        return self.request("GET", path)[0]

    def pages(self, path: str, *, key: str | None = None, params: dict | None = None) -> list[dict]:
        items: list[dict] = []
        seen: set[int] = set()
        total = None
        for page in range(1, self.MAX_PAGES + 1):
            query = {
                **(params or {}),
                "per_page" if self.config.provider is Provider.GITHUB else "limit": self.PER_PAGE,
                "page": page,
            }
            data, headers = self.request("GET", path + "?" + urlencode(query))
            if key is not None:
                if not isinstance(data, dict) or type(data.get("total_count")) is not int:
                    raise HostingError("invalid_page_envelope")
                if total is not None and total != data["total_count"]:
                    raise HostingError("pagination_changed")
                total = data["total_count"]
                data = data.get(key)
            if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
                raise HostingError("invalid_page_items")
            for row in data:
                ident = row.get("id")
                if type(ident) is not int or ident <= 0 or ident in seen:
                    raise HostingError("invalid_or_repeated_page_item")
                seen.add(ident)
            items.extend(data)
            links = re.findall(r'<([^>]+)>;\s*rel="next"', headers, flags=re.I)
            if links:
                link = urlsplit(links[0])
                expected = urlsplit(self.config.api_url + path)
                if (
                    len(links) != 1
                    or (link.scheme, link.netloc, link.path)
                    != (expected.scheme, expected.netloc, expected.path)
                    or parse_qs(link.query).get("page") != [str(page + 1)]
                    or not data
                ):
                    raise HostingError("invalid_pagination_link")
            if not links and len(data) < self.PER_PAGE:
                if total is not None and len(items) != total:
                    raise HostingError("incomplete_pagination")
                return items
        raise HostingError("pagination_exhausted")

    def verify_repository(self) -> dict:
        data = self.get(f"/repos/{self.config.repo}")
        if (
            not isinstance(data, dict)
            or type(data.get("id")) is not int
            or data.get("id") != self.config.repo_id
            or data.get("full_name") != self.config.repo
        ):
            raise HostingError("repository_identity_mismatch")
        if self.config.provider is Provider.GITHUB and self.config.role in {
            AccessRole.FEEDBACK, AccessRole.OPS_READ,
        }:
            if (
                data.get("private") is not True
                or data.get("visibility") != "private"
                or data.get("has_issues") is not True
                or data.get("archived") is not False
                or data.get("disabled") is not False
                or data.get("url") != f"{self.config.api_url}/repos/{self.config.repo}"
                or data.get("html_url") != f"{self.config.web_url}/{self.config.repo}"
            ):
                raise HostingError("private_repository_unavailable")
        return data
