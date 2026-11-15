#!/usr/bin/env python3
"""Exact adapter over the installed App shim; inert until separately adopted.

The three existing roles are unchanged. Dedicated Issue-only grants are minted
at startup and late reporting, with explicit operator-selected identities.
"""

import hashlib
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

CONFIG = None  # BUILD_CONFIG


def main():
    if sys.flags.optimize or CONFIG is None or not os.getuid() == os.geteuid() == CONFIG["uid"]:
        raise ValueError("refresh-runtime")
    path = Path(CONFIG["engine"]) / "scripts/blue_green/release_issues.py"
    if (
        hashlib.sha256(path.read_bytes()).hexdigest()
        != CONFIG["pins"]["scripts/blue_green/release_issues.py"]
    ):
        raise ValueError("refresh-helper-hash")
    sys.path.insert(0, CONFIG["engine"])
    from scripts.blue_green import deploy_state

    if deploy_state.process_lock_fd() is None:
        raise ValueError("refresh-process-fence")
    args = sys.argv[1:]
    allowed = {
        str(Path(CONFIG["root"]) / name) for name in ("entry.py", "selector.py", "review.py")
    }
    if (
        len(args) < 6
        or args[:3] != ["--exec", CONFIG["python"], "-B"]
        or args[3] not in allowed
        or args[4] not in {"--startup-return", "--renewal-return", "--review-return"}
    ):
        raise ValueError("refresh-continuation")
    from scripts.blue_green.release_issues import private

    raw = private(CONFIG["shim"])
    if hashlib.sha256(raw).hexdigest() != CONFIG["shim_sha"]:
        raise ValueError("refresh-shim-hash")
    spec = importlib.util.spec_from_loader("_accepted_app_shim", loader=None)
    shim = importlib.util.module_from_spec(spec)
    shim.__file__ = CONFIG["shim"]
    exec(compile(raw, shim.__file__, "exec"), shim.__dict__)
    accepted_refresh = shim.refresh

    def refresh():
        accepted_refresh()
        args = sys.argv[1:]
        late_report = any(Path(value).name == "deploy_report.py" for value in args)
        if "--startup-return" not in args and not late_report:
            return
        bearer = shim.jwt()
        pending = []
        for target in CONFIG["issue_protocol"]["targets"]:
            permissions = {"issues": "write"}
            data = shim.request(
                "POST",
                "/app/installations/180015273/access_tokens",
                bearer,
                {"repository_ids": [target["repository_id"]], "permissions": permissions},
            )
            if (
                data.get("permissions") != {**permissions, "metadata": "read"}
                or [row.get("id") for row in data.get("repositories", [])]
                != [target["repository_id"]]
                or not isinstance(data.get("token"), str)
                or not data["token"]
            ):
                raise ValueError("issue-grant-scope")
            expires = datetime.strptime(data["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
            if (expires - datetime.now(timezone.utc)).total_seconds() < 300:
                raise ValueError("issue-grant-expiry")
            token = data["token"]
            pending.append(
                (
                    target,
                    token,
                    {
                        "app_id": 5674104,
                        "installation_id": 180015273,
                        "repository_ids": [target["repository_id"]],
                        "permissions": data["permissions"],
                        "expires_at": data["expires_at"],
                        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                    },
                )
            )
        if len({token for _, token, _ in pending}) != 2:
            raise ValueError("issue-grants-distinct")
        for target, token, receipt in pending:
            for key, raw in (
                ("token_file", (token + "\n").encode()),
                ("grant_file", json.dumps(receipt, sort_keys=True).encode()),
            ):
                path = Path(target[key])
                if path.exists() or path.is_symlink():
                    private(str(path))
                temporary = path.with_name(path.name + ".new-" + str(os.getpid()))
                try:
                    fd = os.open(
                        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
                    )
                    with os.fdopen(fd, "wb") as output:
                        output.write(raw)
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)

    shim.refresh = refresh
    shim.main()  # Original environment/egress/exec ABI and inherited FDs remain in charge.


if __name__ == "__main__":
    main()
