"""Finite Issue reconciliation under the native outcome/process fence.

Dedicated least-privilege grants are operator inputs, never minted here. A lost
PATCH acknowledgement permits GET reconciliation only, never a second PATCH.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from scripts.blue_green import deploy_state, publication_authority
from scripts.hosting import (
    AccessRole,
    HostingClient,
    HostingConfig,
    HostingError,
    strict_json_loads,
)

CODE = "DUIDUI-Studio/DailyCheck"
OPS = "DUIDUI-Studio/DailyCheck-ops"


def private(path: str) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
            or metadata.st_size > 65536
        ):
            raise HostingError("issue_credential_custody")
        return os.read(fd, 65537)
    finally:
        os.close(fd)


def grant_client(target: dict, *, now: float | None = None) -> HostingClient:
    grant = strict_json_loads(private(target["grant_file"]))
    token = private(target["token_file"]).decode().strip()
    expected = {
        "app_id": 5674104,
        "installation_id": 180015273,
        "repository_ids": [target["repository_id"]],
        "permissions": {"issues": "write", "metadata": "read"},
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
    }
    if not isinstance(grant, dict) or set(grant) != {*expected, "expires_at"}:
        raise HostingError("issue_grant_invalid")
    if any(grant.get(key) != value for key, value in expected.items()):
        raise HostingError("issue_scope_forbidden")
    if any(type(grant.get(key)) is not int for key in ("app_id", "installation_id")):
        raise HostingError("issue_scope_forbidden")
    expiry = datetime.strptime(grant["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    if expiry.timestamp() <= (time.time() if now is None else now) + 60:
        raise HostingError("issue_grant_expired")
    role = AccessRole.READ if target["repository"] == CODE else AccessRole.OPS_READ
    config = HostingConfig.from_environment(
        role,
        dict(
            os.environ,
            DAILYCHECK_GITHUB_OPERATIONS_REPO=OPS,
            DAILYCHECK_GITHUB_OPERATIONS_REPO_ID="1449125557",
            DAILYCHECK_GITHUB_OPERATIONS_EXPECT_PRIVATE="true",
        ),
    )
    if (config.repo, config.repo_id) != (target["repository"], target["repository_id"]):
        raise HostingError("issue_repository_mismatch")
    return HostingClient(replace(config, token=token))


def validate_protocol(protocol: object, binding) -> list[dict]:
    if (
        not isinstance(protocol, dict)
        or set(protocol) != {"version", "targets"}
        or protocol["version"] != 1
    ):
        raise HostingError("issue_protocol_missing")
    targets = protocol["targets"]
    source = dict(zip(publication_authority.SOURCE_KEYS, binding.source, strict=True))
    if (source["provider"], source["host"], source["repository_id"], source["repository"]) != (
        "github",
        "github.com",
        1449125557,
        OPS,
    ):
        raise HostingError("issue_source_transport_unapproved")
    identities = {(CODE, 1448986421, 19, 6391796319), (OPS, 1449125557, source["number"], None)}
    if not isinstance(targets, list) or len(targets) != 2:
        raise HostingError("issue_targets_missing")
    actual = set()
    for row in targets:
        if (
            not isinstance(row, dict)
            or set(row)
            != {"repository", "repository_id", "number", "issue_id", "token_file", "grant_file"}
            or any(
                type(row[k]) is not int or row[k] <= 0
                for k in ("repository_id", "number", "issue_id")
            )
            or any(
                not isinstance(row[k], str)
                or not Path(row[k]).is_absolute()
                or ".." in Path(row[k]).parts
                for k in ("token_file", "grant_file")
            )
        ):
            raise HostingError("issue_target_invalid")
        actual.add(
            (
                row["repository"],
                row["repository_id"],
                row["number"],
                row["issue_id"] if row["repository"] == CODE else None,
            )
        )
    if actual != identities:
        raise HostingError("issue_target_mismatch")
    return targets


def issue(client: HostingClient, target: dict) -> dict:
    client.verify_repository()
    row = client.get(f"/repos/{target['repository']}/issues/{target['number']}")
    root = f"https://api.github.com/repos/{target['repository']}"
    if (
        not isinstance(row, dict)
        or type(row.get("id")) is not int
        or row["id"] != target["issue_id"]
        or type(row.get("number")) is not int
        or row["number"] != target["number"]
        or "pull_request" in row
        or row.get("url") != f"{root}/issues/{target['number']}"
        or row.get("repository_url") != root
        or row.get("html_url")
        != f"https://github.com/{target['repository']}/issues/{target['number']}"
        or row.get("state") not in {"open", "closed"}
    ):
        raise HostingError("issue_identity_changed")
    return row


def close_once(client: HostingClient, target: dict, fence_fd: int) -> None:
    # This dedicated transport exposes only this exact mutation; existing read,
    # report and finalizer roles gain no Issue-writing capability.
    headers = {
        "Authorization": "Bearer " + client.config.token,
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    config = "".join(f"header = {json.dumps(k + ': ' + v)}\n" for k, v in headers.items())
    config += (
        "data = " + json.dumps(json.dumps({"state": "closed", "state_reason": "completed"})) + "\n"
    )
    result = subprocess.run(
        [
            client.config.curl,
            "--disable",
            "--silent",
            "--max-time",
            "15",
            "--max-redirs",
            "0",
            "--max-filesize",
            "1048576",
            "--config",
            "-",
            "--request",
            "PATCH",
            "--write-out",
            "\n%{http_code}",
            f"https://api.github.com/repos/{target['repository']}/issues/{target['number']}",
        ],
        input=config,
        text=True,
        capture_output=True,
        pass_fds=(fence_fd,),
        check=False,
    )
    raw, separator, status = result.stdout.rpartition("\n")
    if result.returncode != 0 or not separator or status != "200":
        raise HostingError("issue_close_unknown")
    data = strict_json_loads(raw)
    if (
        not isinstance(data, dict)
        or data.get("id") != target["issue_id"]
        or data.get("state") != "closed"
    ):
        raise HostingError("issue_close_unknown")


def reconcile(profile: dict) -> list[dict]:
    directory = Path(profile["state"])
    fd = deploy_state.process_lock_fd()
    if fd is None:
        raise HostingError("issue_process_fence_missing")

    def current():
        ident, outcome, generation = publication_authority.load_durable_outcome(
            directory, profile["pr"], profile["head"]
        )
        binding = publication_authority.resolve_source_binding(directory, profile["pr"])
        state = deploy_state.load_state(directory, strict=True)
        publication = state.get("publications", {}).get(ident, {})
        if (
            outcome["rc"] != 0
            or outcome["verification"] != "normal"
            or binding.binding_sha256 != profile["binding_sha256"]
            or publication.get("state") != "linked"
            or publication.get("generation") != generation
            or publication.get("binding_sha256") != binding.binding_sha256
            or type(publication.get("comment_id")) is not int
            or publication["comment_id"] <= 0
            or state.get("deployed", {}).get("sha") != profile["head"]
            or state.get("deployed", {}).get("outcome_id") != ident
        ):
            raise HostingError("issue_delivery_not_proved")
        return state, ident, binding

    with deploy_state.state_transaction(directory):
        _, _, binding = current()
        targets = validate_protocol(profile["issue_protocol"], binding)
    reader = HostingClient(HostingConfig.from_environment())
    pr = reader.get(f"/repos/{CODE}/pulls/{profile['pr']}")
    main = reader.get(f"/repos/{CODE}/git/ref/heads/main")
    if (
        not isinstance(pr, dict)
        or pr.get("merged") is not True
        or pr.get("head", {}).get("sha") != profile["head"]
        or main.get("object", {}).get("sha") != profile["head"]
    ):
        raise HostingError("issue_merge_not_proved")
    # Validate BOTH real grants/targets before either mutation.
    clients = [grant_client(target) for target in targets]
    for client, target in zip(clients, targets, strict=True):
        issue(client, target)
    results = []
    for client, target in zip(clients, targets, strict=True):
        key = f"{target['repository_id']}:{target['number']}"
        observed = issue(client, target)
        with deploy_state.state_transaction(directory):
            state, ident, _ = current()
            resolutions = state["publications"][ident].setdefault("issue_resolutions", {})
            previous = resolutions.get(key)
            if observed["state"] == "closed":
                resolutions[key] = {"state": "closed", "issue_id": target["issue_id"]}
                deploy_state.save_state(directory, state)
            elif previous is not None:
                raise HostingError("issue_close_uncertain_no_replay")
            else:
                resolutions[key] = {"state": "uncertain", "issue_id": target["issue_id"]}
                deploy_state.save_state(directory, state)  # Durable intent BEFORE PATCH.
        if observed["state"] != "closed":
            close_once(client, target, fd)
            if issue(client, target)["state"] != "closed":
                raise HostingError("issue_close_unknown")
            with deploy_state.state_transaction(directory):
                state, ident, _ = current()
                state["publications"][ident]["issue_resolutions"][key]["state"] = "closed"
                deploy_state.save_state(directory, state)
        results.append(
            {
                "repository_id": target["repository_id"],
                "number": target["number"],
                "state": "closed",
            }
        )
    return results
