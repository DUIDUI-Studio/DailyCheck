#!/usr/bin/env python3
"""Operator-bound release entry. No ambient environment or journal supplies admission."""

import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
from pathlib import Path

SELECTOR_SHA = None  # BUILD_SELECTOR_SHA


def selector():
    p = Path(__file__).absolute().with_name("selector.py")
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_mode & 0o022:
            raise ValueError("selector-custody")
        raw = os.read(fd, 1048577)
        if hashlib.sha256(raw).hexdigest() != SELECTOR_SHA:
            raise ValueError("selector-hash")
    finally:
        os.close(fd)
    spec = importlib.util.spec_from_loader("_release_selector", loader=None)
    m = importlib.util.module_from_spec(spec)
    m.__file__ = str(p)
    sys.modules[spec.name] = m
    exec(compile(raw, str(p), "exec"), m.__dict__)
    m.runtime()
    if os.geteuid() != st.st_uid:
        raise ValueError("selector-uid")
    return m


def startup_binding(s):
    p = s.PROFILE
    state = s.state_module()
    fd = s.fence(state)
    s.require("DAILYCHECK_DEPLOY_ATTEMPT" not in os.environ, "nested-attempt")
    s.require(
        json.loads(s.regular(Path(p["root"]) / "invocation.json"))
        == {
            "task": p["task"],
            "dispatch": p["dispatch"],
            "pr": p["pr"],
            "head": p["head"],
            "base": p["base"],
            "pid": os.getpid(),
            "pgid": os.getpgrp(),
            "stage": "invocation-before-standard-run",
        },
        "startup-invocation",
    )
    s.source_admission()
    current = state._load_state(Path(p["state"]))
    s.require(
        current.get("lock") is None
        and type(current.get("generation")) is int
        and current.get("generation") == p["generation"]
        and current.get("deployed", {}).get("sha") == p["base"]
        and type(current.get("deployed", {}).get("generation")) is int
        and current.get("deployed", {}).get("generation") == p["deployed_generation"]
        and not state.native_blocked(Path(p["state"]), current),
        "startup-baseline",
    )
    s.require(
        state.failed_head_status(
            current,
            p["pr"],
            p["head"],
            {
                "provider": "github",
                "host": "github.com",
                "repository": "DUIDUI-Studio/DailyCheck",
                "pr": p["pr"],
            },
        )
        is None,
        "previous-target-attempt",
    )
    s.require(
        not any(
            x.get("pr") == p["pr"] and x.get("sha") == p["head"]
            for x in current.get("outcomes", {}).values()
        ),
        "previous-target-attempt",
    )
    for relative, digest in p["pins"].items():
        s.regular(Path(p["repo"]) / relative, digest)
    for relative in ("scratch", "scratch/tmp"):
        s.private_dir(Path(p["root"]) / relative)
    st = os.fstat(fd)
    return {
        "generation": p["generation"],
        "deployed_generation": p["deployed_generation"],
        "base": p["base"],
        "fence_fd": fd,
        "fence_device": st.st_dev,
        "fence_inode": st.st_ino,
    }


def main():
    s = selector()
    p = s.PROFILE
    s.require(Path(__file__).absolute() == Path(p["root"]) / "entry.py", "entry-path")
    args = sys.argv[1:]
    if args == ["--execute-admitted"]:
        s.require(
            "DAILYCHECK_DEPLOY_ATTEMPT" not in os.environ
            and "DAILYCHECK_DEPLOY_ATTEMPT_FD" not in os.environ,
            "fresh-entry",
        )
        for relative, digest in p["pins"].items():
            s.regular(Path(p["repo"]) / relative, digest)
        s.regular(p["shim"], p["shim_sha"])
        # This invocation journal consumes no authority and is never a success receipt.
        intent = Path(p["root"]) / "invocation.json"
        fd = os.open(intent, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.write(
                fd,
                json.dumps(
                    {
                        "task": p["task"],
                        "dispatch": p["dispatch"],
                        "pr": p["pr"],
                        "head": p["head"],
                        "base": p["base"],
                        "pid": os.getpid(),
                        "pgid": os.getpgrp(),
                        "stage": "invocation-before-standard-run",
                    }
                ).encode(),
            )
            os.fsync(fd)
        finally:
            os.close(fd)
        s.sync_directory(p["root"])
        for relative in ("scratch", "scratch/tmp"):
            (Path(p["root"]) / relative).mkdir(mode=0o700)
        for key in (
            "PYTHONPATH",
            "PYTHONHOME",
            "PYTHONOPTIMIZE",
            "DAILYCHECK_DEPLOY_MERGE_BIN",
            "DC_NATIVE_REPO",
        ):
            os.environ.pop(key, None)
        os.environ.update(
            DAILYCHECK_DEPLOY_STATE_DIR=p["state"],
            GIT_TERMINAL_PROMPT="0",
            PYTHONDONTWRITEBYTECODE="1",
            PYTHON=str(Path(p["root"]) / "selector.py"),
            TMPDIR=p["tmp"],
            DAILYCHECK_PIPELINE_LOG=p["log"],
        )
        ds = str(Path(p["repo"]) / "scripts/blue_green/deploy_state.py")
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
                str(Path(__file__).absolute()),
                "--startup-fenced",
            ],
        )
    if args == ["--startup-fenced"]:
        binding = startup_binding(s)
        s.renew("startup", [], binding, str(Path(__file__).absolute()), "--startup-return")
    s.require(len(args) == 2 and args[0] == "--startup-return", "entry-arguments")
    binding = startup_binding(s)
    s.continuation(args[1], "startup", [], binding)
    argv = [
        "bash",
        str(Path(p["repo"]) / "scripts/blue_green/deploy_pipeline.sh"),
        str(p["pr"]),
        p["head"],
        "--candidate",
        "--merge-pr",
        "--session",
        p["session"],
        "--observe-min",
        "10",
    ]
    os.execvp("bash", argv)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            "Release entry refused: "
            + (
                str(exc)
                if isinstance(exc, ValueError) and re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", str(exc))
                else type(exc).__name__
            ),
            file=sys.stderr,
        )
        raise SystemExit(78) from None
