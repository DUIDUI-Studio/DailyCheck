"""master.db 数据访问：warehouse 元信息查询。"""
from __future__ import annotations

from mcp_server.data.unit_of_work import master_connection


def resolve_warehouse(code: str) -> dict | None:
    """根据 code 查询 warehouse 元信息，返回 Row 或 None。"""
    with master_connection() as conn:
        row = conn.execute(
            "SELECT code, name, db_path FROM warehouses WHERE code = ?",
            (code,),
        ).fetchone()
        return dict(row) if row else None


def list_all_warehouses() -> list[dict]:
    """返回所有 warehouse 元信息。"""
    with master_connection() as conn:
        rows = conn.execute(
            "SELECT code, name, db_path FROM warehouses ORDER BY code"
        ).fetchall()
        return [dict(r) for r in rows]
