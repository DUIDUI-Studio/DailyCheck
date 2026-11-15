"""Owner-operated binding CAS using the native publication configuration fence.

No credentials, registration, service action or admission is implicit. Install
and rollback both refuse drift; an existing receipt is never overwritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.blue_green import deploy_state, publication_authority  # noqa: E402
from scripts.hosting import strict_json_loads  # noqa: E402


def safe_directory(path: Path):
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("unsafe-config-path")
    for parent in [path, *path.parents]:
        meta = parent.lstat()
        if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != os.geteuid() or meta.st_mode & 0o022:
            raise ValueError("unsafe-config-parent")


def regular(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        meta = os.fstat(fd)
        if (
            not stat.S_ISREG(meta.st_mode)
            or meta.st_uid != os.geteuid()
            or meta.st_nlink != 1
            or meta.st_mode & 0o077
            or meta.st_size > 1048576
        ):
            raise ValueError("unsafe-config-file")
        return os.read(fd, 1048577)
    finally:
        os.close(fd)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def checked_document(raw: bytes):
    value = strict_json_loads(raw)
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "bindings"}
        or type(value["version"]) is not int
        or value["version"] < 1
        or not isinstance(value["bindings"], list)
    ):
        raise ValueError("invalid-bindings")
    entries = [publication_authority._validate_binding_entry(row) for row in value["bindings"]]
    if len({row["pr"] for row in entries}) != len(entries):
        raise ValueError("ambiguous-bindings")
    return value


def durable_create(path: Path, raw: bytes):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())


def sync(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def change(state: Path, receipt: Path, expected: str, new: bytes, operation: str):
    safe_directory(state)
    safe_directory(receipt.parent)
    checked_document(new)
    path = state / publication_authority.BINDINGS_FILENAME
    # All unsafe-path checks precede the native transaction's first write.
    before = regular(path)
    if digest(before) != expected:
        raise ValueError("binding-cas-drift")
    receipt.mkdir(mode=0o700)  # Exclusive; partial installation requires examination.
    with deploy_state.publication_config_transaction(state):
        before = regular(path)
        if digest(before) != expected:
            raise ValueError("binding-cas-drift")
        old, changed = checked_document(before), checked_document(new)
        # This finite installer only adds/removes PR20; unrelated mappings survive.
        if [r for r in old["bindings"] if r["pr"] != 20] != [
            r for r in changed["bindings"] if r["pr"] != 20
        ]:
            raise ValueError("unrelated-binding-change")
        durable_create(receipt / "before.json", before)
        durable_create(receipt / "after.json", new)
        durable_create(
            receipt / "intent.json",
            json.dumps(
                {
                    "operation": operation,
                    "before_sha256": digest(before),
                    "after_sha256": digest(new),
                    "state": str(state),
                    "no_automatic_retry": True,
                }
            ).encode(),
        )
        sync(receipt)
        temporary = state / (".bindings-pr20-" + uuid.uuid4().hex)
        try:
            durable_create(temporary, new)
            os.replace(temporary, path)
            sync(state)
        finally:
            temporary.unlink(missing_ok=True)
        durable_create(
            receipt / "installed.json", json.dumps({"sha256": digest(regular(path))}).encode()
        )
        sync(receipt)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("install", "rollback"))
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="New full binding document; rollback uses the original before.json",
    )
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    change(args.state, args.receipt, args.expected_sha256, regular(args.input), args.operation)


if __name__ == "__main__":
    if sys.flags.optimize:
        raise SystemExit("optimized-python")
    main()
