"""JSON access.log 写入，复用现有 `blueprints/agent_mpc.py` 逻辑。"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from mcp_server.config import BASE_DIR, MASTER_DB

# 配置了非空 DAILYCHECK_DB_DIR 时，审计写进与 master.db 同一个中央
# `config.DB_DIR`（不可变代码发布下 BASE_DIR 只读）；未设置或显式空串时
# 保持旧位置 BASE_DIR/access.log。中央 config 定义
# MASTER_DB = DB_DIR / "master.db"，故其父目录就是同一份已选定的 DB 根；
# import 期选择，env 判空语义与 central config 相同，不引入第二个解析器。
_DB_DIR: Path = MASTER_DB.parent
_ACCESS_LOG_PATH: Path = (
    _DB_DIR / "access.log" if os.environ.get("DAILYCHECK_DB_DIR") else BASE_DIR / "access.log"
)


def write_mcp_access_log(
    token_id: int | None,
    method: str,
    path: str,
    status: int,
    duration_ms: int,
    tool_name: str | None = None,
) -> None:
    """追加一条 JSON 记录到 access.log。异常静默吞掉。

    `tool_name` 是 Issue #85 新增的可选字段，MCP `tools/call` 用它区分具体
    工具；既有调用方不传时记录形状保持不变。
    """
    try:
        rec = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "agent_token_id": token_id,
            "path": path,
            "method": method,
            "status": int(status),
            "duration_ms": int(duration_ms),
        }
        if tool_name is not None:
            rec["tool_name"] = tool_name
        with open(_ACCESS_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
