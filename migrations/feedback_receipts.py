"""私有反馈送达回执表（Issue #162，master v8）。

这张表此前只写进 ``MASTER_SCHEMA``：新库/启动期幂等建表能补上，但
pre-162 的 ``user_version=7`` 库在显式迁移入口既看不到待应用项，也不会
建表或备份。本迁移复用 ``db.FEEDBACK_DELIVERY_RECEIPTS_DDL``，让
``init_master_db()`` 的幂等建表与显式迁移共用同一份 schema。
"""
from __future__ import annotations

import sqlite3


def registered_up(conn: sqlite3.Connection) -> None:
    # Lazy import: db imports the migrations package at module load.
    from db import FEEDBACK_DELIVERY_RECEIPTS_DDL

    conn.execute(FEEDBACK_DELIVERY_RECEIPTS_DDL)


def registered_down(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS feedback_delivery_receipts")
