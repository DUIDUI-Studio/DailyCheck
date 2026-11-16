"""Prepared only. Retain one real child through metadata/audit failure and wait."""

import sys

# This must execute even under -O/-OO, before preparation, intents or Popen.
if sys.flags.optimize != 0:
    raise SystemExit("optimized-python")

import hashlib
import json
import os
import signal
import stat
import subprocess
import time
from pathlib import Path

PROFILE = None  # BUILD_PROFILE
ENTRY_SHA = None
SELECTOR_SHA = None
ROOT = Path(PROFILE["root"]) if PROFILE else None
PYTHON = PROFILE["python"] if PROFILE else None
PYTHON_TARGET = PROFILE["python_target"] if PROFILE else None
PYTHON_SHA = PROFILE["python_sha256"] if PROFILE else None


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def start(pid):
    return Path("/proc/" + str(pid) + "/stat").read_text().rpartition(")")[2].split()[19]


def write(name, obj):
    fd = os.open(ROOT / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as file:
        json.dump(obj, file, sort_keys=True)
        file.flush()
        os.fsync(file.fileno())
    fd = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def wait_child(child):
    interruptions = []
    while True:
        try:
            rc = child.wait()
            break
        except (InterruptedError, KeyboardInterrupt) as exc:
            # Keep the same handle; interruption is not process exit.
            interruptions.append(type(exc).__name__)
    try:
        os.waitpid(child.pid, os.WNOHANG)
        echild = False
    except ChildProcessError:
        echild = True
    require(echild, "child-reap-unproved")
    return {
        "pid": child.pid,
        "native_rc": rc,
        "positive_wait": True,
        "actual_ECHILD": echild,
        "wait_interruptions": interruptions,
        "no_retry": True,
    }


def error_fact(error, stage):
    return {
        "error_class": type(error).__name__,
        "stage": stage,
        "errno": getattr(error, "errno", None),
    }


def main():
    require(sys.flags.optimize == 0, "optimized-python")
    require(sys.argv[1:] == ["--execute-admitted"], "arguments")
    require(PROFILE is not None and os.getuid() == os.geteuid() == PROFILE["uid"], "runtime-uid")
    require(
        all(PROFILE[k] is not None for k in ("capability", "binding_sha256", "issue_protocol")),
        "live-inputs-missing",
    )
    for path in [ROOT, *ROOT.parents]:
        metadata = path.lstat()
        require(
            stat.S_ISDIR(metadata.st_mode)
            and metadata.st_uid == PROFILE["uid"]
            and not metadata.st_mode & 0o022,
            "parent-custody",
        )
    for name, digest in [("entry.py", ENTRY_SHA), ("selector.py", SELECTOR_SHA)]:
        path = ROOT / name
        metadata = path.lstat()
        require(
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_uid == PROFILE["uid"]
            and metadata.st_nlink == 1
            and stat.S_IMODE(metadata.st_mode) == 0o700
            and hashlib.sha256(path.read_bytes()).hexdigest() == digest,
            "bundle-custody",
        )
    for path in [*Path(PYTHON).parents, *Path(PYTHON_TARGET).parents]:
        metadata = path.lstat()
        require(
            stat.S_ISDIR(metadata.st_mode)
            and metadata.st_uid == PROFILE["uid"]
            and not metadata.st_mode & 0o022,
            "interpreter-parent-custody",
        )
    metadata = Path(PYTHON_TARGET).lstat()
    require(
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == PROFILE["uid"]
        and not metadata.st_mode & 0o022,
        "interpreter-custody",
    )
    require(
        str(Path(PYTHON).resolve()) == PYTHON_TARGET
        and hashlib.sha256(Path(PYTHON_TARGET).read_bytes()).hexdigest() == PYTHON_SHA,
        "interpreter-identity",
    )
    require(
        not any(
            key in os.environ
            for key in [
                "DAILYCHECK_DEPLOY_ATTEMPT",
                "DAILYCHECK_DEPLOY_ATTEMPT_FD",
                "DAILYCHECK_DEPLOY_UNLOCKED",
                "DAILYCHECK_BG_LOCK_HELD",
            ]
        ),
        "ambient-fence",
    )
    # Consumption only: native deploy_state.run/acquire remains the authority.
    write(
        "execution-attempt-intent.json",
        {
            "pr": PROFILE["pr"],
            "head": PROFILE["head"],
            "attempt": 1,
            "preparation_task": PROFILE["task"],
            "preparation_dispatch": PROFILE["dispatch"],
            "separate_concrete_live_admission_required": True,
            "at": time.time(),
        },
    )
    write(
        "retained-parent-start.json",
        {
            "pid": os.getpid(),
            "start_ticks": start(os.getpid()),
            "pgid": os.getpgrp(),
            "at": time.time(),
        },
    )
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    fd = os.open(
        ROOT / "retained-child.private.log",
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    child = None
    result = None
    primary = None
    stage = "Popen"
    log = os.fdopen(fd, "wb", buffering=0)
    log_close_error = None
    try:
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONOPTIMIZE"):
            env.pop(key, None)
        child = subprocess.Popen(
            [PYTHON, "-I", "-B", str(ROOT / "entry.py"), "--execute-admitted"],
            cwd=PROFILE["repo"],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        # Everything after returned Popen is inside this unconditional wait scope.
        stage = "child-start-metadata"
        identity = {
            "pid": child.pid,
            "start_ticks": start(child.pid),
            "pgid": os.getpgid(child.pid),
            "before_child_IO_and_wait": True,
        }
        stage = "child-start-audit"
        write("retained-child-start.json", identity)
    except BaseException as exc:
        primary = (exc, exc.__traceback__, stage)
    finally:
        try:
            if child is not None:
                result = wait_child(child)
        finally:
            try:
                log.close()
            except BaseException as exc:
                log_close_error = error_fact(exc, "child-log-close")
                if primary is None:
                    primary = (exc, exc.__traceback__, "child-log-close")
    if result is not None:
        if log_close_error is not None:
            result["log_close_error"] = log_close_error
        if primary is not None:
            result["postlaunch_error"] = error_fact(primary[0], primary[2])
        try:
            write("retained-child-wait.json", result)
        except BaseException as exc:
            result["settlement_audit_error"] = error_fact(exc, "child-wait-audit")
            if primary is None:
                primary = (exc, exc.__traceback__, "child-wait-audit")
        # A full/unwritable audit directory must never be a reason to skip wait.
        # This closed, secret-free fallback occurs only AFTER positive settlement.
        try:
            os.write(
                2,
                (json.dumps({"retained_child_settlement": result}, sort_keys=True) + "\n").encode(),
            )
        except BaseException:
            pass
    if primary is not None:
        raise primary[0].with_traceback(primary[1])
    require(result is not None, "child-not-launched")
    raise SystemExit(result["native_rc"] if result["native_rc"] >= 0 else 128 - result["native_rc"])


if __name__ == "__main__":
    main()
