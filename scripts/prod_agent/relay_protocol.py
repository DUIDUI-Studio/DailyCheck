"""Private exact-H Worker/relay wire and protected local files."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from scripts.hosting import github_ff_payload, strict_json_loads
from scripts.prod_agent.native_ports import protected_path

REPO = "DUIDUI-Studio/DailyCheck"
REMOTE = f"https://github.com/{REPO}.git"
COMMAND = "dailycheck-release-relay-v1"
SIGNER = "dailycheck-local-release-worker"
MAX_CONTROL = 8192
MAX_BUNDLE = 16 * 1024 * 1024
STATE_PATH = Path("/etc/dailycheck-relay/vps.json")
WORKER_PATH = Path("/private/etc" if sys.platform == "darwin" else "/etc") / "dailycheck-relay/worker.json"
SYSTEM_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


class StageFailure(Exception):
    """A positively waited stage exit or a failed observation probe."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def hex_id(value, width):
    return isinstance(value, str) and re.fullmatch(f"[0-9a-f]{{{width}}}", value) is not None


def positive(value):
    return type(value) is int and value > 0


def target(value):
    require(isinstance(value, dict) and set(value) == {
        "pr", "head", "base", "ref", "attempt", "source", "generation", "pr_id", "pr_node", "tree"
    }, "target shape")
    github_ff_payload(value["base"], value["head"], value["ref"], value["attempt"])
    require(positive(value["pr"]) and positive(value["generation"])
            and positive(value["pr_id"]) and hex_id(value["tree"], 40) and hex_id(value["source"], 64)
            and isinstance(value["pr_node"], str)
            and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value["pr_node"]), "target identity")
    return value


def lock_file(directory, name):
    path = directory / name
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_nlink != 1 or info.st_mode & 0o022):
        os.close(fd)
        raise ValueError("unprotected lock inode")
    return os.fdopen(fd, "rb")


def checked_file(path, *, anchor=None, private=True, pin=None):
    path = protected_path(Path(path), os.geteuid(), anchor=anchor, private=private)
    info = path.stat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "protected regular file")
    if pin is not None:
        with path.open("rb") as stream:
            require(hashlib.sha256(stream.read()).hexdigest() == pin,
                    "installed file pin changed")
    return path


def read_file(path, *, anchor=None):
    if anchor is not None:
        checked_file(path, anchor=anchor)
    with Path(path).open("rb") as stream:
        return strict_json_loads(stream.read(1024 * 1024 + 1))


def secure_profile(path, fixture_root):
    root = None
    if fixture_root is not None:
        root = Path(fixture_root)
        require(root.is_absolute() and root == root.resolve(), "fixture root")
        protected_path(root, os.geteuid(), anchor=root, private=True)
    path = checked_file(path, anchor=root)
    return read_file(path), root


def sync_dir(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save_file(path, value):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".relay-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_dir(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def sign(envelope, key):
    result = subprocess.run(
        ["/usr/bin/ssh-keygen", "-Y", "sign", "-f", str(key), "-n", COMMAND],
        input=canonical(envelope), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=SYSTEM_ENV, timeout=8, check=True)
    return result.stdout.decode("ascii")


def verify(envelope, signers, directory):
    signature = envelope.pop("signature", "")
    require(isinstance(signature, str) and 0 < len(signature) < 1024, "Worker signature absent")
    with tempfile.NamedTemporaryFile(dir=directory) as stream:
        stream.write(signature.encode("ascii"))
        stream.flush()
        subprocess.run(
            ["/usr/bin/ssh-keygen", "-Y", "verify", "-f", str(signers), "-I", SIGNER,
             "-n", COMMAND, "-s", stream.name],
            input=canonical(envelope), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=SYSTEM_ENV, timeout=8, check=True)
