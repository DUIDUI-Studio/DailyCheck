"""Host interlock shared by production routing writers and protected route changes."""

import fcntl
import os
import stat
from contextlib import contextmanager
from pathlib import Path

FENCE = Path("/etc/dailycheck-application/routing-writes.lock")


def _open_fence(path):
    for parent in path.parents:
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise OSError("unprotected routing fence directory")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) != 0o644 or info.st_nlink != 1):
            raise OSError("unprotected routing fence file")
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextmanager
def routing_writer_fence(*, switching=False):
    # The stable root-owned inode is shared across service namespaces. The
    # operator changes its contents in place only under an exclusive flock.
    fd = _open_fence(FENCE)
    try:
        if switching:
            # The existing blue/green shell uses low numbered descriptors.
            inherited = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 20)
            os.close(fd)
            fd = inherited
        mode = fcntl.LOCK_EX if switching else fcntl.LOCK_SH
        fcntl.flock(fd, mode | fcntl.LOCK_NB)
        required = b"disabled\n" if switching else b"enabled\n"
        if os.read(fd, len(required) + 1) != required:
            raise OSError("routing fence is in the wrong state")
        yield fd
    finally:
        # A route helper inherits this descriptor. Closing (rather than
        # unlocking) leaves the fence held if the controller dies first.
        os.close(fd)
