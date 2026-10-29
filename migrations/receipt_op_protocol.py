"""收货镜像协议：``store_order_receipts.op_no`` 幂等键 + ``applied`` 应用标记。

Task 5 把「请求级 op_no」落成行级派生键 ``f'{op_no}:{order_item_id}'``；
收货沿用同一约定，存进 ``store_order_receipts.op_no``（UNIQUE）。
``applied=0`` 表示 master 已落 receipt、但门店库加库尚未回写完成，
由重放或 ``reconcile_orphan_receipts`` 补齐后置 1。

存量行一律回填 ``applied=1``（旧路径写入即已入库），因此该迁移可直接重跑。
"""
from __future__ import annotations

import sqlite3

RECEIPT_OP_NO_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_store_order_receipts_op_no"
    " ON store_order_receipts(op_no)"
)


def _columns(conn: sqlite3.Connection, table: str) -> dict[str, tuple]:
    return {row[1]: row for row in conn.execute(f"PRAGMA table_info({table})")}


def registered_up(conn: sqlite3.Connection) -> None:
    columns = _columns(conn, "store_order_receipts")
    if not columns:
        # 存量老库可能还没有收货表；由 init_master_db 的 DDL 兜底新建。
        return
    added_op_no = False
    if "op_no" not in columns:
        conn.execute("ALTER TABLE store_order_receipts ADD COLUMN op_no TEXT")
        added_op_no = True
    if "applied" not in columns:
        # DEFAULT 1 让存量行直接成为「已应用」。
        conn.execute(
            "ALTER TABLE store_order_receipts ADD COLUMN"
            " applied INTEGER NOT NULL DEFAULT 1"
        )
    conn.execute(
        "UPDATE store_order_receipts SET applied=1 WHERE applied IS NULL"
    )
    if added_op_no:
        conn.execute(RECEIPT_OP_NO_INDEX)


def registered_down(conn: sqlite3.Connection) -> None:
    """尽力回退：先删索引再删列（SQLite 3.35+ 支持 DROP COLUMN）。"""
    conn.execute("DROP INDEX IF EXISTS idx_store_order_receipts_op_no")
    columns = _columns(conn, "store_order_receipts")
    for column in ("applied", "op_no"):
        if column in columns:
            conn.execute(f"ALTER TABLE store_order_receipts DROP COLUMN {column}")
