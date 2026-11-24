"""Admin action audit log — single implementation (Phase 3, AC-02).

Platform-admin actions (user/role/token/warehouse management) are appended
as one JSON line each to the configured file, or to ``admin_audit.log`` at
the repo root in existing deployments. master.db has
no audit_log table, and the per-warehouse audit_log only covers business
writes, so admin actions live in this flat file.

Historically two byte-identical copies of this writer existed
(``blueprints/users.py`` and ``blueprints/agent_tokens.py``). This module is
the single implementation; both call sites delegate to it.

Kept free of Flask imports so it is trivially unit-testable: the caller
passes the actor explicitly (``g.user["username"]`` at the call site).
"""
from __future__ import annotations

import json
import os
import stat
from datetime import datetime
from pathlib import Path

_configured = os.environ.get("DAILYCHECK_ADMIN_AUDIT_LOG")
_CONFIGURED_AUDIT_LOG = Path(_configured) if _configured is not None else None
ADMIN_AUDIT_LOG = (
    _CONFIGURED_AUDIT_LOG if _CONFIGURED_AUDIT_LOG is not None
    else Path(__file__).resolve().parent.parent / "admin_audit.log"
)


def _open_configured_log(path: Path):
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("absolute admin audit path required")
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or (info.st_uid, info.st_gid) != (os.geteuid(), os.getegid())
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError("private application-owned admin audit file required")
        return os.fdopen(descriptor, "a", encoding="utf-8")
    except BaseException:
        os.close(descriptor)
        raise


def check_admin_audit_sink() -> None:
    if _CONFIGURED_AUDIT_LOG is not None:
        # Test the actual service principal and mount without adding a record.
        with _open_configured_log(_CONFIGURED_AUDIT_LOG):
            pass


def append_admin_audit(
    actor: str | None,
    detail: str,
    path: Path | None = None,
) -> None:
    """Append one admin-action line to the audit log (format-preserving).

    The on-disk format ``{"ts", "actor", "detail"}`` is unchanged from the
    original inline writers so existing log-parsers keep working.
    """
    log = path or ADMIN_AUDIT_LOG
    stream = (_open_configured_log(log) if log == _CONFIGURED_AUDIT_LOG
              else log.open("a", encoding="utf-8"))
    with stream as f:
        f.write(json.dumps({
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "actor": actor,
            "detail": detail,
        }, ensure_ascii=False) + "\n")
