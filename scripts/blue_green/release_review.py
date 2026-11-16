#!/usr/bin/env python3
"""Fixed exact-H formal review write, once; then the same Agent runs parent.py.

The reviewer supplies only APPROVED or CHANGES_REQUESTED. Detailed diagnostics
stay private; this tool emits a fixed declassified formal review body.
"""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

ENTRY_SHA = None  # BUILD_ENTRY_SHA


def selector():
    path = Path(__file__).with_name("entry.py")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != ENTRY_SHA:
        raise ValueError("review-entry-hash")
    spec = importlib.util.spec_from_loader("_review_entry", loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__file__ = str(path)
    exec(compile(raw, str(path), "exec"), module.__dict__)
    return module.selector()


def binding(s):
    state = s.state_module()
    fd = s.fence(state)
    current = state.load_state(Path(s.PROFILE["state"]), strict=True)
    s.require(
        current.get("lock") is None
        and current.get("generation") == s.PROFILE["generation"]
        and current.get("deployed", {}).get("sha") == s.PROFILE["base"],
        "review-baseline-changed",
    )
    s.source_admission()
    metadata = os.fstat(fd)
    return dict(
        generation=current["generation"],
        fence_fd=fd,
        fence_device=metadata.st_dev,
        fence_inode=metadata.st_ino,
    )


def main():
    s = selector()
    p = s.PROFILE
    args = sys.argv[1:]
    s.require(
        p["capability"] is not None
        and p["binding_sha256"] is not None
        and p["issue_protocol"] is not None,
        "live-inputs-missing",
    )
    s.require(Path(__file__).absolute() == Path(p["root"]) / "review.py", "review-path")
    if len(args) == 1 and args[0] in {"APPROVED", "CHANGES_REQUESTED"}:
        s.require(
            "DAILYCHECK_DEPLOY_ATTEMPT_FD" not in os.environ
            and "DAILYCHECK_DEPLOY_ATTEMPT" not in os.environ,
            "fresh-review",
        )
        for relative, digest in p["pins"].items():
            s.regular(Path(p["engine"]) / relative, digest)
        os.environ["DAILYCHECK_DEPLOY_STATE_DIR"] = p["state"]
        ds = str(Path(p["engine"]) / "scripts/blue_green/deploy_state.py")
        os.execv(
            p["python"],
            [
                p["python"],
                "-B",
                ds,
                "run",
                "--",
                p["python"],
                "-B",
                __file__,
                "--review-fenced",
                args[0],
            ],
        )
    s.require(
        len(args) in {2, 3} and args[-1] in {"APPROVED", "CHANGES_REQUESTED"}, "review-arguments"
    )
    decision = args[-1]
    bound = binding(s)
    if args[0] == "--review-fenced" and len(args) == 2:
        s.renew("review", [decision], bound, __file__, "--review-return")
    s.require(len(args) == 3 and args[0] == "--review-return", "review-return")
    s.continuation(args[1], "review", [decision], bound)
    payload = s.review_admission(require_approval=False)
    from scripts.hosting import AccessRole, HostingConfig
    from scripts.prod_agent import github_intake

    config = HostingConfig.from_environment(AccessRole.REPORT)
    body = {
        "commit_id": p["head"],
        "event": "APPROVE" if decision == "APPROVED" else "REQUEST_CHANGES",
        "body": f"Independent Prod review: {decision} for exact head {p['head']}. Diagnostics retained privately.",
    }
    headers = {
        "Authorization": "Bearer " + config.token,
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    curl_config = "".join(f"header = {json.dumps(k + ': ' + v)}\n" for k, v in headers.items())
    curl_config += "data = " + json.dumps(json.dumps(body)) + "\n"
    result = subprocess.run(
        [
            config.curl,
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
            "POST",
            "--write-out",
            "\n%{http_code}",
            f"https://api.github.com/repos/DUIDUI-Studio/DailyCheck/pulls/{p['pr']}/reviews",
        ],
        input=curl_config,
        text=True,
        capture_output=True,
        pass_fds=(bound["fence_fd"],),
        check=False,
    )
    _, separator, status = result.stdout.rpartition("\n")
    s.require(
        result.returncode == 0 and separator and status == "200", "formal-review-write-unknown"
    )
    observed = github_intake.review_completion(payload)
    s.require(observed is not None and observed["state"] == decision, "formal-review-unconfirmed")
    output = dict(
        pr=p["pr"],
        head=p["head"],
        state=decision,
        review=observed,
        no_replay=True,
        next="fixed-parent" if decision == "APPROVED" else "STOP",
    )
    raw = (json.dumps(output, sort_keys=True) + "\n").encode()
    fd = os.open(
        Path(p["root"]) / "review-receipt.json",
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(fd, "wb") as file:
        file.write(raw)
        file.flush()
        os.fsync(file.fileno())
    s.sync_directory(p["root"])
    os.write(1, raw)
    return 0 if decision == "APPROVED" else 31


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print("Formal review unconfirmed: " + type(exc).__name__, file=sys.stderr)
        raise SystemExit(78) from None
