#!/usr/bin/env python3
"""Operator-bound finite credential phase selector; rendered outside the candidate."""

import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
import time
from pathlib import Path

PROFILE = None  # BUILD_PROFILE


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def private_dir(path):
    p = Path(path)
    s = p.lstat()
    require(
        stat.S_ISDIR(s.st_mode) and s.st_uid == PROFILE["uid"] and stat.S_IMODE(s.st_mode) == 0o700,
        "directory-custody",
    )
    return p


def regular(path, expected=None, executable=False):
    p = Path(path)
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        s = os.fstat(fd)
        require(
            stat.S_ISREG(s.st_mode)
            and s.st_uid == PROFILE["uid"]
            and s.st_nlink == 1
            and not s.st_mode & 0o022
            and s.st_size <= 1048576,
            "file-custody",
        )
        data = os.read(fd, 1048577)
        require(len(data) == s.st_size, "file-size")
        if executable:
            require(bool(s.st_mode & 0o100), "file-executable")
        if expected:
            require(hashlib.sha256(data).hexdigest() == expected, "file-hash")
        return data
    finally:
        os.close(fd)


def runtime():
    require(sys.flags.optimize == 0, "optimized-python")
    require(os.geteuid() == PROFILE["uid"], "runtime-uid")
    require(os.path.abspath(sys.executable) == PROFILE["python"], "runtime-python")
    private_dir(PROFILE["root"])
    require(PROFILE["uid"] == os.getuid(), "runtime-real-uid")
    require(Path(__file__).absolute() == Path(PROFILE["root"]) / "selector.py", "selector-path")


def state_module():
    path = Path(PROFILE["engine"]) / "scripts/blue_green/deploy_state.py"
    data = regular(path, PROFILE["pins"]["scripts/blue_green/deploy_state.py"])
    # Execute the exact bytes just hashed, not a second path read.
    spec = importlib.util.spec_from_loader("_release_owned_state", loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__file__ = str(path)
    sys.modules[spec.name] = module
    exec(compile(data, str(path), "exec"), module.__dict__)
    return module


def source_admission():
    state = state_module()
    hosting_path = Path(PROFILE["engine"]) / "scripts/hosting.py"
    hosting_raw = regular(hosting_path, PROFILE["pins"]["scripts/hosting.py"])
    authority_path = Path(PROFILE["engine"]) / "scripts/blue_green/publication_authority.py"
    authority_raw = regular(
        authority_path, PROFILE["pins"]["scripts/blue_green/publication_authority.py"]
    )
    import types

    package = types.ModuleType("scripts")
    package.__path__ = [str(Path(PROFILE["engine"]) / "scripts")]
    sys.modules["scripts"] = package
    sys.modules["deploy_state"] = state
    for name, path, raw in [
        ("scripts.hosting", hosting_path, hosting_raw),
        ("_release_authority", authority_path, authority_raw),
    ]:
        spec = importlib.util.spec_from_loader(name, loader=None)
        module = importlib.util.module_from_spec(spec)
        module.__file__ = str(path)
        sys.modules[name] = module
        exec(compile(raw, str(path), "exec"), module.__dict__)
    try:
        with state.state_transaction(Path(PROFILE["state"])):
            binding = module.resolve_source_binding(Path(PROFILE["state"]), PROFILE["pr"])
        require(binding.binding_sha256 == PROFILE["binding_sha256"], "source-binding-changed")
    except sys.modules["scripts.hosting"].HostingError as exc:
        # Only these closed local schema reasons may enter the diagnostic.
        reason = str(exc)
        allowed = {
            "private_source_binding_missing",
            "private_source_binding_invalid",
            "private_source_binding_ambiguous",
            "private_revision_binding_invalid",
        }
        raise ValueError(reason if reason in allowed else "source-admission-failed") from None


def fence(state):
    require(os.environ.get("DAILYCHECK_DEPLOY_STATE_DIR") == PROFILE["state"], "state-selector")
    fd = state._process_lock_fd()
    require(fd is not None, "process-fence")
    return fd


def intake_environment():
    os.environ.update(
        DAILYCHECK_GITHUB_INTAKE_ENABLED="1",
        DAILYCHECK_GITHUB_INTAKE_POLICY_FILE=PROFILE["intake_policy"],
        DAILYCHECK_GITHUB_INTAKE_KEY_FILE=PROFILE["intake_key"],
        DAILYCHECK_EVENT_REDUCER_DB=PROFILE["reducer_db"],
    )


def review_admission(*, require_approval=True):
    """A single operator capability narrows existing authenticated intake authority."""
    import sqlite3

    from scripts.prod_agent import github_intake

    intake_environment()
    capability = PROFILE["capability"]
    require(
        isinstance(capability, dict)
        and set(capability)
        == {"version", "owner", "ci_run_id", "ci_attempt", "policy_sha256", "since_ms"}
        and capability["version"] == 1
        and isinstance(capability["owner"], str)
        and bool(capability["owner"]),
        "whole-run-capability-missing",
    )
    require(
        all(
            type(capability[k]) is int and capability[k] > 0
            for k in ("ci_run_id", "ci_attempt", "since_ms")
        ),
        "whole-run-capability-invalid",
    )
    rule = github_intake.policy()
    require(
        rule.digest == capability["policy_sha256"]
        and rule.epoch == capability["since_ms"]
        and rule.targets == frozenset({(PROFILE["pr"], PROFILE["head"])})
        and rule.pr_range is None
        and rule.review_actors,
        "whole-run-policy-changed",
    )
    with sqlite3.connect(Path(PROFILE["reducer_db"]).as_uri() + "?mode=ro", uri=True) as connection:
        rows = connection.execute(
            """SELECT payload_json FROM trigger_queue
            WHERE repo=? AND pr_number=? AND head_sha=? AND event='check_suite'
            AND (state='claimed' OR (state='done' AND last_error IN
                ('review_approved','awaiting_agent_receipt','delivery_unknown')))""",
            (github_intake.REPO.lower(), PROFILE["pr"], PROFILE["head"]),
        ).fetchall()
    require(len(rows) == 1, "single-review-claim-required")
    payload = json.loads(rows[0][0])
    require(
        payload.get("pr_base_sha") == PROFILE["base"]
        and payload.get("observed_ci_run_id") == capability["ci_run_id"]
        and payload.get("observed_ci_attempt") == capability["ci_attempt"],
        "review-ci-changed",
    )
    if require_approval:
        observed = github_intake.review_completion(payload)
        require(
            observed is not None and observed["state"] == "APPROVED", "formal-review-not-approved"
        )
    gate = github_intake.readiness(payload)
    require(
        gate.get("ready") is True
        and gate.get("base_sha") == PROFILE["base"]
        and gate.get("run_id") == capability["ci_run_id"]
        and gate.get("run_attempt") == capability["ci_attempt"],
        "current-ci-not-ready",
    )
    if require_approval:
        from scripts.blue_green import publication_authority
        from scripts.blue_green.release_issues import grant_client, issue, validate_protocol

        binding = publication_authority.resolve_source_binding(
            Path(PROFILE["state"]), PROFILE["pr"]
        )
        for target in validate_protocol(PROFILE["issue_protocol"], binding):
            issue(grant_client(target), target)
    audit("CANDIDATE_ADMITTED" if require_approval else "REVIEW_WRITE_ADMITTED", "review")
    return payload


def command(args):
    name = Path(args[0]).name if args else ""
    if name == "deploy_merge.py" and len(args) >= 2 and args[1] == "merge":
        require(
            args[1:]
            == [
                "merge",
                "--pr",
                str(PROFILE["pr"]),
                "--sha",
                PROFILE["head"],
                "--base",
                PROFILE["base"],
            ],
            "merge-arguments",
        )
        return "finalizer"
    if name == "deploy_report.py" and len(args) >= 2 and args[1] == "write":
        require(
            len(args) == 14
            and args[1:6] == ["write", "--pr", str(PROFILE["pr"]), "--sha", PROFILE["head"]]
            and args[6] == "--rc"
            and re.fullmatch(r"0|[1-9][0-9]{0,2}", args[7])
            and 0 <= int(args[7]) <= 255
            and args[8] == "--stage"
            and args[10:] == ["--log", PROFILE["log"], "--repo", PROFILE["repo"]],
            "report-arguments",
        )
        return "typed-report"
    return None


def late_binding(kind, args):
    state = state_module()
    fd = fence(state)
    identity = state.AttemptIdentity.from_env()
    require(
        identity is not None
        and type(identity.pr) is int
        and identity.pr == PROFILE["pr"]
        and all(isinstance(v, str) and v for v in (identity.token, identity.sha, identity.session))
        and identity.sha == PROFILE["head"]
        and identity.session == PROFILE["session"],
        "attempt-identity",
    )
    p = Path(args[0]).absolute()
    tmp = Path(PROFILE["tmp"])
    require(
        p.parent.name == "blue_green"
        and p.parent.parent.name == "scripts"
        and p.parent.parent.parent.parent == tmp
        and p.parent.parent.parent.name.startswith("dailycheck-pipeline-helpers-"),
        "helper-path",
    )
    private_dir(tmp)
    private_dir(p.parent.parent.parent)
    private_dir(p.parent.parent)
    private_dir(p.parent)
    for relative, digest in PROFILE["pins"].items():
        if relative not in {
            "scripts/blue_green/deploy_pipeline.sh",
            "scripts/blue_green/release_finish.py",
            "scripts/blue_green/release_issues.py",
            "scripts/blue_green/release_completion.py",
        }:
            regular(p.parent.parent.parent / relative, digest)
    current = state._load_state(Path(PROFILE["state"]))
    require(
        type(current.get("generation")) is int
        and current["generation"] == PROFILE["generation"] + 1,
        "attempt-generation",
    )
    expected = {
        "provider": "github",
        "host": "github.com",
        "repository": "DUIDUI-Studio/DailyCheck",
        "pr": PROFILE["pr"],
    }
    if kind == "finalizer":
        lock = current.get("lock")
        require(
            identity.owns(lock)
            and lock.get("phase") == "merge-start"
            and lock.get("deployment") == expected
            and type(lock.get("generation")) is int
            and lock.get("generation") == current.get("generation")
            and not state.native_blocked(Path(PROFILE["state"]), current),
            "active-attempt",
        )
        binding = {"attempt_id": lock["attempt_id"], "generation": lock["generation"]}
    else:
        matches = [
            v
            for v in current.get("outcomes", {}).values()
            if v.get("attempt_token") == identity.token
            and v.get("session") == identity.session
            and v.get("pr") == PROFILE["pr"]
            and v.get("sha") == PROFILE["head"]
            and type(v.get("generation")) is int
            and v.get("generation") == current.get("generation")
        ]
        require(current.get("lock") is None and len(matches) == 1, "released-attempt")
        outcome = matches[0]
        require(
            outcome.get("deployment") == expected
            and type(outcome.get("rc")) is int
            and outcome.get("rc") == int(args[7])
            and args[9] in ("", outcome.get("stage")),
            "report-outcome",
        )
        binding = {"attempt_id": outcome["attempt_id"], "generation": outcome["generation"]}
    require(re.fullmatch(r"[0-9a-f]{32}", binding["attempt_id"]) is not None, "attempt-id")
    s = os.fstat(fd)
    source_admission()
    return {**binding, "fence_fd": fd, "fence_device": s.st_dev, "fence_inode": s.st_ino}


def audit(phase, kind):
    p = private_dir(PROFILE["root"]) / "phase-refresh-events.jsonl"
    fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        s = os.fstat(fd)
        require(
            stat.S_ISREG(s.st_mode)
            and s.st_uid == PROFILE["uid"]
            and s.st_nlink == 1
            and stat.S_IMODE(s.st_mode) == 0o600
            and s.st_size < 65536,
            "audit-custody",
        )
        row = {
            "phase": phase,
            "kind": kind,
            "pid": os.getpid(),
            "pgid": os.getpgrp(),
            "monotonic_ns": time.monotonic_ns(),
        }
        os.write(fd, (json.dumps(row, sort_keys=True) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def ticket(kind, args, binding):
    return {
        "kind": kind,
        "args": args,
        "binding": binding,
        "pid": os.getpid(),
        "pgid": os.getpgrp(),
        "uid": os.geteuid(),
    }


def renew(kind, args, binding, script, marker):
    regular(PROFILE["shim"], PROFILE["shim_sha"])
    refresh = Path(PROFILE["root"]) / "refresh.py"
    regular(refresh, PROFILE["refresh_sha"])
    # A crash/failed refresh consumes this phase slot; never renew again implicitly.
    claim = Path(PROFILE["root"]) / ("renewal-intent-" + kind + ".json")
    fd = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, json.dumps(ticket(kind, args, binding), sort_keys=True).encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    sync_directory(PROFILE["root"])
    r, w = os.pipe()
    try:
        raw = json.dumps(ticket(kind, args, binding), sort_keys=True).encode()
        require(len(raw) <= 3500, "continuation-size")
        os.write(w, raw)
    finally:
        os.close(w)
    os.set_inheritable(r, True)
    audit("REFRESH_BEGIN", kind)
    os.execv(
        PROFILE["python"],
        [
            PROFILE["python"],
            "-B",
            str(refresh),
            "--exec",
            PROFILE["python"],
            "-B",
            script,
            marker,
            str(r),
            *args,
        ],
    )


def continuation(fd_arg, kind, args, binding):
    claim = Path(PROFILE["root"]) / ("renewal-intent-" + kind + ".json")
    require(json.loads(regular(claim)) == ticket(kind, args, binding), "continuation-intent")
    require(re.fullmatch(r"[0-9]+", fd_arg) is not None, "continuation-fd")
    fd = int(fd_arg)
    require(fd >= 3 and stat.S_ISFIFO(os.fstat(fd).st_mode), "continuation-pipe")
    try:
        os.set_blocking(fd, False)
        raw = os.read(fd, 4097)
        require(len(raw) <= 3500 and os.read(fd, 1) == b"", "continuation-bound")
        require(json.loads(raw) == ticket(kind, args, binding), "continuation-binding")
    finally:
        os.close(fd)
    audit("REFRESH_RETURN", kind)


def main():
    runtime()
    args = sys.argv[1:]
    resumed = args[:1] == ["--renewal-return"]
    if resumed:
        require(len(args) >= 4, "continuation-arguments")
        fd_arg, args = args[1], args[2:]
    kind = command(args)
    if kind is None:
        require(
            not resumed
            and (not args or Path(args[0]).name != "deploy_report.py" or args[1:2] != ["write"]),
            "continuation-kind",
        )
        os.execv(PROFILE["python"], [PROFILE["python"], *args])
    binding = late_binding(kind, args)
    if resumed:
        continuation(fd_arg, kind, args, binding)
        if kind == "typed-report":
            intake_environment()
            finish = Path(PROFILE["engine"]) / "scripts/blue_green/release_finish.py"
            regular(finish, PROFILE["pins"]["scripts/blue_green/release_finish.py"])
            for name in ("release_issues.py", "release_completion.py"):
                regular(finish.with_name(name), PROFILE["pins"]["scripts/blue_green/" + name])
            os.execv(
                PROFILE["python"],
                [
                    PROFILE["python"],
                    "-B",
                    str(finish),
                    str(Path(PROFILE["root"]) / "selector.py"),
                    *args,
                ],
            )
        os.execv(PROFILE["python"], [PROFILE["python"], *args])
    renew(kind, args, binding, str(Path(__file__).absolute()), "--renewal-return")


def safe_reason(exc):
    reason = str(exc)
    if isinstance(exc, ValueError) and re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", reason):
        return reason
    return type(exc).__name__


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Closed local codes only; no token, environment or raw exception text.
        print("Release selector refused: " + safe_reason(exc), file=sys.stderr)
        raise SystemExit(78) from None
