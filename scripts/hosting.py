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
from urllib.parse import parse_qs, urlencode, urlsplit


class Provider(str, Enum):
    GITEA = "gitea"
    GITHUB = "github"


class AccessRole(str, Enum):
    READ = "read"
    FEEDBACK = "feedback"
    REPORT = "report"


class HostingError(RuntimeError):
    def __init__(self, reason: str, status: int = 0):
        super().__init__(reason)
        self.status = status


def _json_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise HostingError("duplicate_json_key")
        result[key] = value
    return result


def _invalid_constant(value: str):
    raise HostingError("invalid_json_constant")


def provider(environ: Mapping[str, str] | None = None) -> Provider:
    env = os.environ if environ is None else environ
    try:
        return Provider(env.get("DAILYCHECK_HOSTING_PROVIDER", "gitea"))
    except ValueError as exc:
        raise HostingError("invalid_hosting_provider") from exc


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
    if payload.get("hosting_provider") == "github" or payload.get("intake_mode") == "shadow":
        return True
    repository = payload.get("repository")
    identities = [payload.get("repo_full_name"), payload.get("repo")]
    if isinstance(repository, dict):
        identities.append(repository.get("full_name"))
    if any(
        isinstance(repo, str) and repo.casefold() == "duidui-studio/dailycheck"
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
            self.repo != "DUIDUI-Studio/DailyCheck"
            or self.repo_id != 1448986421
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
            AccessRole.READ: ("DAILYCHECK_GITHUB_READ_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"),
            AccessRole.FEEDBACK: ("DAILYCHECK_GITHUB_FEEDBACK_TOKEN",),
            AccessRole.REPORT: ("DAILYCHECK_GITHUB_REPORT_TOKEN",),
        }[role]
        return cls(
            selected,
            env.get("DAILYCHECK_GITHUB_API", "https://api.github.com"),
            env.get("DAILYCHECK_GITHUB_REPO", "DUIDUI-Studio/DailyCheck"),
            role,
            _token(env, names, f"DAILYCHECK_GITHUB_{role.value.upper()}_TOKEN_FILE"),
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

    def request(self, method: str, path: str, payload: dict | None = None) -> tuple[object, str]:
        c = self.config
        if not c.token:
            raise HostingError("credentials_unavailable")
        if method != "GET" and c.role is AccessRole.READ:
            raise HostingError("read_role_cannot_write")
        # This transport deliberately has no GitHub finalization primitive.
        routes = {
            AccessRole.FEEDBACK: rf"/repos/{re.escape(c.repo)}/(?:issues|labels)",
            AccessRole.REPORT: rf"/repos/{re.escape(c.repo)}/issues/(?:[1-9][0-9]*/comments|comments/[1-9][0-9]*)",
        }
        allowed = re.fullmatch(routes.get(c.role, r"(?!)"), path)
        if method != "GET" and (not allowed or method not in {"POST", "PATCH"}):
            raise HostingError("unsupported_hosting_write")
        if (path != f"/repos/{c.repo}" and not path.startswith(f"/repos/{c.repo}/")) or any(
            x in path for x in ("\r", "\n", "#", "..")
        ):
            raise HostingError("invalid_api_path")
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
                )
                raw_headers = Path(dump.name).read_bytes().decode("latin1")
            raw, sep, status = proc.stdout.rpartition("\n")
            if proc.returncode or not sep or len(raw.encode()) > self.MAX_BYTES:
                raise HostingError("hosting_network_error")
            code = int(status)
            expected = {"GET": 200, "POST": 201, "PATCH": 200}
            if not 200 <= code < 300 or (
                c.provider is Provider.GITHUB and code != expected.get(method)
            ):
                raise HostingError("hosting_http_error", code)
            data = json.loads(raw, object_pairs_hook=_json_object, parse_constant=_invalid_constant)
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
        return data
