"""JSON access.log 写入，复用现有 `blueprints/agent_mpc.py` 逻辑。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from mcp_server.config import BASE_DIR

_ACCESS_LOG_PATH: Path = BASE_DIR / "access.log"


def write_mcp_access_log(
    token_id: int | None,
    method: str,
    path: str,
    status: int,
    duration_ms: int,
) -> None:
    """追加一条 JSON 记录到 access.log。异常静默吞掉。"""
    try:
        rec = {
            "ts": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "agent_token_id": token_id,
            "path": path,
            "method": method,
            "status": int(status),
            "duration_ms": int(duration_ms),
        }
        with open(_ACCESS_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
