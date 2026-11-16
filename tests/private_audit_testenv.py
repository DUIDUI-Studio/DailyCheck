"""Private diagnostic data only; executable fixtures stay on pytest's disk scratch."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

import pytest


def _audit_parent() -> Path:
    for candidate in (Path("/dev/shm"), Path(tempfile.gettempdir())):
        try:
            parent = candidate.resolve(strict=True)
            for directory in (parent, *parent.parents):
                info = directory.lstat()
                if (not stat.S_ISDIR(info.st_mode)
                        or info.st_uid not in {0, os.geteuid()}
                        or info.st_mode & 0o022 and not
                        (info.st_uid == 0 and info.st_mode & stat.S_ISVTX)):
                    raise ValueError("unsafe audit ancestor")
            return parent
        except (OSError, ValueError):
            continue
    raise RuntimeError("no safe temporary ancestor for private audit data")


@pytest.fixture(name="private_audit")
def private_audit_fixture():
    # TemporaryDirectory provides a unique0700 leaf. Dependants reap their children
    # before this fixture tears down; /dev/shm never holds an executable or DB.
    with tempfile.TemporaryDirectory(prefix="dailycheck-private-audit-", dir=_audit_parent()) as raw:
        directory = Path(raw)
        info = directory.lstat()
        assert info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700
        yield directory
