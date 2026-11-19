#!/usr/bin/env python3
"""Root-installed GitHub App entrypoint for the DailyCheck PR poller."""

import base64
import json
import os
import re
import runpy
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

KEY = Path("/etc/dailycheck/github-prod-app.pem")
ENV = Path("/etc/dailycheck/github-production.env")
SETTINGS = Path("/etc/dailycheck/github-app-shim.env")
OWNER = "DUIDUI-Studio"
CODE = f"{OWNER}/DailyCheck"
OPS = f"{OWNER}/DailyCheck-ops"
CODE_ID = 1448986421
OPS_ID = 1449125557
APP_ID = 5674104
INSTALLATION_ID = 180015273
INSTALLATION = f"/app/installations/{INSTALLATION_ID}"
READ = {name: "read" for name in (
    "actions", "administration", "checks", "contents", "issues", "pull_requests"
)}
ROLES = {
    "read": (CODE_ID, READ),
    "report": (CODE_ID, {"pull_requests": "write"}),
    "finalize": (CODE_ID, {
        **{name: READ[name] for name in ("actions", "administration", "checks", "pull_requests")},
        "contents": "write", "workflows": "write",
    }),
    "ops_read": (OPS_ID, {"issues": "read"}),
    "ops_issue": (OPS_ID, {"issues": "write"}),
}
FILES = {role: f"DAILYCHECK_GITHUB_{role.upper()}_TOKEN_FILE" for role in ROLES}
FILES["ops_issue"] = "DAILYCHECK_GITHUB_FEEDBACK_TOKEN_FILE"
CODE_ROLES = ("read", "report", "finalize")
OPS_ROLES = ("ops_read", "ops_issue")
OPS_ENV = {
    "DAILYCHECK_GITHUB_OPERATIONS_REPO": OPS,
    "DAILYCHECK_GITHUB_OPERATIONS_REPO_ID": str(OPS_ID),
    "DAILYCHECK_GITHUB_OPERATIONS_EXPECT_PRIVATE": "true",
}
ENV_KEYS = {
    "DAILYCHECK_HOSTING_PROVIDER", "DAILYCHECK_GITHUB_LANE", "DAILYCHECK_GITHUB_REPO",
    "DAILYCHECK_PROD_REPO", "DAILYCHECK_DEPLOY_STATE_DIR", "DAILYCHECK_DEPLOY_EVENT_LOG",
    "DAILYCHECK_PROD_AGENT_INBOX", *(FILES[role] for role in CODE_ROLES),
}
SETTINGS_KEYS = {"DAILYCHECK_APP_OPS_ENABLED", "DAILYCHECK_APP_PROXY", "DAILYCHECK_APP_NO_PROXY"}
active_settings = {}


def private_file(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise ValueError("unsafe shim file custody")


def read_config(path, allowed, *, optional=False):
    try:
        private_file(path)
    except FileNotFoundError:
        if optional:
            return {}
        raise
    values = {}
    for line in path.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        if key not in allowed or key in values or not value or not value.isprintable():
            raise ValueError("unexpected shim configuration")
        values[key] = value
    return values


def configure_egress():
    # Retain the pinned release adapter's no-argument, egress-only ABI.
    settings = read_config(SETTINGS, SETTINGS_KEYS, optional=True)
    if settings.get("DAILYCHECK_APP_OPS_ENABLED", "0") not in {"0", "1"}:
        raise ValueError("invalid ops activation")
    proxy = settings.get("DAILYCHECK_APP_PROXY", "")
    if proxy and not re.fullmatch(r"http://127\.0\.0\.1:[1-9][0-9]{0,4}", proxy):
        raise ValueError("invalid egress proxy")
    for name in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        os.environ.pop(name, None)
        os.environ.pop(name.upper(), None)
    if proxy:
        os.environ["https_proxy"] = proxy
        inherited = os.environ.get("GIT_CONFIG_PARAMETERS", "")
        git_proxy = f"'http.https://github.com/{CODE}.git.proxy'='{proxy}'"
        os.environ["GIT_CONFIG_PARAMETERS"] = (inherited + " " + git_proxy).strip()
    os.environ["no_proxy"] = settings.get("DAILYCHECK_APP_NO_PROXY", "localhost,127.0.0.1,::1")
    active_settings.clear()
    active_settings.update(settings)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def request(method, route, bearer, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        "https://api.github.com" + route, data=data, method=method,
        headers={"Authorization": "Bearer " + bearer, "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28",
                 "User-Agent": "dailycheck-prod"},
    )
    proxy = active_settings.get("DAILYCHECK_APP_PROXY")
    handler = urllib.request.ProxyHandler({"https": proxy} if proxy else {})
    with urllib.request.build_opener(handler, NoRedirect()).open(req, timeout=25) as response:
        if response.status != (201 if method == "POST" else 200):
            raise ValueError("unexpected GitHub status")
        result = json.load(response)
        if not isinstance(result, dict):
            raise ValueError("unexpected GitHub response")
        return result


def jwt():
    private_file(KEY)

    def encode(raw):
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    now = int(datetime.now(timezone.utc).timestamp())
    claims = json.dumps({"iat": now - 30, "exp": now + 300, "iss": str(APP_ID)}).encode()
    message = encode(b'{"alg":"RS256","typ":"JWT"}') + b"." + encode(claims)
    signature = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(KEY)],
        input=message, capture_output=True, check=True,
    ).stdout
    return (message + b"." + encode(signature)).decode()


def verify_installation(data):
    account = data.get("account") or {}
    if (data.get("id") != INSTALLATION_ID or data.get("app_id") != APP_ID
            or data.get("repository_selection") not in {"selected", "all"}
            or account.get("id") != 353610181 or account.get("login") != OWNER
            or account.get("type") != "Organization"
            or "suspended_at" not in data or data["suspended_at"] is not None):
        raise ValueError("unexpected App installation identity")


def mint(bearer, role):
    repo_id, permissions = ROLES[role]
    data = request("POST", INSTALLATION + "/access_tokens", bearer,
                   {"repository_ids": [repo_id], "permissions": permissions})
    repositories = data.get("repositories")
    token = data.get("token")
    if (data.get("permissions") != {**permissions, "metadata": "read"}
            or not isinstance(repositories, list) or len(repositories) != 1
            or not isinstance(repositories[0], dict) or type(repositories[0].get("id")) is not int
            or repositories[0]["id"] != repo_id
            or not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", token)):
        raise ValueError("unexpected role credential grant")
    expires = datetime.strptime(data["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    if not 300 <= (expires - datetime.now(timezone.utc)).total_seconds() <= 3660:
        raise ValueError("unexpected role credential expiry")
    return token


def token_file(role):
    return KEY.parent / f"github-{role}.token"


def save_token(path, token):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w") as out:
            out.write(token + "\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def refresh():
    enabled = active_settings.get("DAILYCHECK_APP_OPS_ENABLED") == "1"
    roles = CODE_ROLES + OPS_ROLES if enabled else CODE_ROLES
    bearer = jwt()
    app = request("GET", "/app", bearer)
    if app.get("id") != APP_ID or app.get("slug") != "dailycheck-prod-gate-duidui":
        raise ValueError("unexpected App identity")
    verify_installation(request("GET", INSTALLATION, bearer))
    for repo in (CODE, OPS) if enabled else (CODE,):
        verify_installation(request("GET", f"/repos/{repo}/installation", bearer))
    pending = {role: mint(bearer, role) for role in roles}
    if len(set(pending.values())) != len(roles):
        raise ValueError("role credentials must be distinct")
    paths = {role: token_file(role) for role in roles}
    for path in paths.values():
        if path.exists() or path.is_symlink():
            private_file(path)
    for role, token in pending.items():
        save_token(paths[role], token)


def main():
    args = sys.argv[1:]
    if args and args != ["--refresh"] and not (args[0] == "--exec" and len(args) > 1):
        raise ValueError("unsupported shim arguments")
    config = read_config(ENV, ENV_KEYS)
    if (config.keys() != ENV_KEYS or config["DAILYCHECK_HOSTING_PROVIDER"] != "github"
            or config["DAILYCHECK_GITHUB_REPO"] != CODE
            or any(config[FILES[role]] != str(token_file(role)) for role in CODE_ROLES)):
        raise ValueError("unexpected production environment")
    credential_keys = {key for role_file in FILES.values() for key in (
        role_file, role_file.removesuffix("_FILE"),
    )}
    credential_keys.update({"DAILYCHECK_GITHUB_OPS_ISSUE_TOKEN", "DAILYCHECK_GITHUB_OPS_ISSUE_TOKEN_FILE"})
    for key in credential_keys | OPS_ENV.keys() | {"GH_TOKEN", "GITHUB_TOKEN", "GITEA_TOKEN"}:
        os.environ.pop(key, None)
    os.environ.update(config)
    configure_egress()
    refresh()
    if args == ["--refresh"]:
        return
    if active_settings.get("DAILYCHECK_APP_OPS_ENABLED") == "1":
        os.environ.update(OPS_ENV)
        os.environ.update({FILES[role]: str(token_file(role)) for role in OPS_ROLES})
    if args:
        return os.execvp(args[1], args[1:])
    target = Path(config["DAILYCHECK_PROD_REPO"]) / "scripts/prod_agent/pr_poll.py"
    runpy.run_path(str(target), run_name="__main__")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        status = f" ({error.code})" if isinstance(error, urllib.error.HTTPError) else ""
        print(f"DailyCheck GitHub Prod entrypoint failed: {type(error).__name__}{status}")
        sys.exit(1)
