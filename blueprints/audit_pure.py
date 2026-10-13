"""Admin action audit log — single implementation (Phase 3, AC-02).

Platform-admin actions (user/role/token/warehouse management) are appended
as one JSON line each to ``admin_audit.log`` at the repo root. master.db has
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
from datetime import datetime
from pathlib import Path

# Repo root: blueprints/audit_pure.py -> blueprints/ -> repo root.
ADMIN_AUDIT_LOG = Path(__file__).resolve().parent.parent / "admin_audit.log"


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
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "actor": actor,
            "detail": detail,
        }, ensure_ascii=False) + "\n")
