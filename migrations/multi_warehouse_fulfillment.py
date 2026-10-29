"""多仓履约 schema 与历史来源回填（master v3 / warehouse v6）。"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Mapping
from contextlib import ExitStack, closing, contextmanager

DELIVERY_ITEMS_DDL = """
CREATE TABLE IF NOT EXISTS store_order_delivery_items (
    delivery_id INTEGER NOT NULL,
    order_item_id INTEGER NOT NULL,
    warehouse_code TEXT NOT NULL,
    item_id INTEGER NOT NULL,
    quantity REAL NOT NULL,
    op_no TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    FOREIGN KEY (delivery_id) REFERENCES store_order_deliveries(id) ON DELETE CASCADE,
    FOREIGN KEY (order_item_id) REFERENCES store_order_items(id) ON DELETE CASCADE,
    FOREIGN KEY (warehouse_code) REFERENCES warehouses(code)
)
"""

ITEM_ROUTING_DDL = """
CREATE TABLE IF NOT EXISTS item_routing (
    tenant_id INTEGER NOT NULL,
    canonical_id INTEGER NOT NULL,
    fulfillment_warehouse_code TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by INTEGER,
    PRIMARY KEY (tenant_id, canonical_id),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id),
    FOREIGN KEY (canonical_id) REFERENCES canonical_items(id),
    FOREIGN KEY (fulfillment_warehouse_code) REFERENCES warehouses(code),
    FOREIGN KEY (updated_by) REFERENCES users(id)
)
"""


def _columns(conn, table):
    return {r[1]: r for r in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn, table, column):
    columns = _columns(conn, table)
    if columns and column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")


@contextmanager
def _migration_savepoint(conn):
    # 不 commit 调用方事务；即使直接调用也保留外层事务，供调用方统一提交。
    if not conn.in_transaction:
        conn.execute("BEGIN")
    conn.execute("SAVEPOINT multi_warehouse_fulfillment")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK TO multi_warehouse_fulfillment")
        conn.execute("RELEASE multi_warehouse_fulfillment")
        raise
    else:
        conn.execute("RELEASE multi_warehouse_fulfillment")


def _rebuild_carts(conn):
    columns = _columns(conn, "store_order_carts")
    if not columns or not columns["dc_warehouse_code"][3]:
        return False

    # runner 已 BEGIN，不能在事务内关 FK。先保存并移走子表，防止 DROP 父表
    # 隐式 ON DELETE CASCADE 清空明细，再按 SQLite create/copy/drop/rename 重建。
    # 原始 DDL、索引、触发器和 AUTOINCREMENT 高水位均保留。
    tables = ["store_order_carts"]
    if _columns(conn, "store_order_cart_items"):
        tables.append("store_order_cart_items")
    definitions = {}
    objects = []
    sequences = {}
    for table in tables:
        definitions[table] = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()[0]
        objects.extend(r[0] for r in conn.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name=? "
            "AND type IN ('index', 'trigger') AND sql IS NOT NULL", (table,)
        ))
        seq = conn.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone()
        if seq:
            sequences[table] = seq[0]
        conn.execute(f"CREATE TEMP TABLE {table}_mw_backup AS SELECT * FROM {table}")

    cart_ddl = re.sub(
        r"(\bdc_warehouse_code\s+TEXT)\s+NOT\s+NULL", r"\1",
        definitions["store_order_carts"], flags=re.IGNORECASE,
    )
    if cart_ddl == definitions["store_order_carts"]:
        raise RuntimeError("无法识别 store_order_carts.dc_warehouse_code 的旧 DDL")
    cart_ddl = cart_ddl.replace("store_order_carts", "store_order_carts_mw_new", 1)
    conn.execute(cart_ddl)
    conn.execute(
        "INSERT INTO store_order_carts_mw_new SELECT * FROM store_order_carts_mw_backup"
    )
    if "store_order_cart_items" in tables:
        conn.execute("DROP TABLE store_order_cart_items")
    conn.execute("DROP TABLE store_order_carts")
    conn.execute("ALTER TABLE store_order_carts_mw_new RENAME TO store_order_carts")
    if "store_order_cart_items" in tables:
        conn.execute(definitions["store_order_cart_items"])
        conn.execute(
            "INSERT INTO store_order_cart_items SELECT * FROM store_order_cart_items_mw_backup"
        )
    for sql in objects:
        conn.execute(sql)
    for table, seq in sequences.items():
        conn.execute("UPDATE sqlite_sequence SET seq=? WHERE name=?", (seq, table))
    for table in tables:
        conn.execute(f"DROP TABLE temp.{table}_mw_backup")
    return True


def migrate_multi_warehouse_fulfillment(
    master_conn: sqlite3.Connection,
    warehouse_conns: Mapping[str, sqlite3.Connection],
) -> dict:
    """幂等补列/表、仅回填合法历史空来源；所有连接由调用方 commit/rollback。

    warehouse_conns 是仓 code → 已打开连接；不改仓库 user_version（v6 请求迁移
    还包含其他历史列）。返回本次写入数、购物车是否重建及待人工处置的异常行。
    """
    result = {"backfilled_items": 0, "backfilled_deliveries": 0,
              "carts_rebuilt": False, "anomalies": []}
    with ExitStack() as stack:
        stack.enter_context(_migration_savepoint(master_conn))
        for conn in dict.fromkeys(warehouse_conns.values()):
            stack.enter_context(_migration_savepoint(conn))
            _add_column(conn, "stock_movements", "op_no")

        _add_column(master_conn, "store_order_items", "source_warehouse_code")
        _add_column(master_conn, "store_order_items", "ship_op_no")
        _add_column(master_conn, "store_order_deliveries", "warehouse_code")
        master_conn.execute(DELIVERY_ITEMS_DDL)
        master_conn.execute(ITEM_ROUTING_DDL)
        if _columns(master_conn, "store_order_items"):
            # 回填前先做数量门禁，异常行完全不写（包括来源字段）。
            valid = (
                "0 <= fulfilled_quantity AND fulfilled_quantity <= shipped_quantity "
                "AND shipped_quantity <= quantity"
            )
            rows = master_conn.execute(
                "SELECT id, order_id, quantity, shipped_quantity, fulfilled_quantity "
                f"FROM store_order_items WHERE NOT ({valid}) ORDER BY id"
            )
            keys = ("order_item_id", "order_id", "quantity", "shipped_quantity", "fulfilled_quantity")
            result["anomalies"] = [dict(zip(keys, row, strict=True)) for row in rows]
            result["backfilled_items"] = master_conn.execute(
                "UPDATE store_order_items SET source_warehouse_code = "
                "(SELECT dc_warehouse_code FROM store_orders o WHERE o.id=order_id) "
                f"WHERE source_warehouse_code IS NULL AND ({valid}) "
                "AND EXISTS (SELECT 1 FROM store_orders o "
                "WHERE o.id=order_id AND o.dc_warehouse_code IS NOT NULL)"
            ).rowcount
        if _columns(master_conn, "store_order_deliveries"):
            result["backfilled_deliveries"] = master_conn.execute(
                "UPDATE store_order_deliveries SET warehouse_code = "
                "(SELECT dc_warehouse_code FROM store_orders o WHERE o.id=order_id) "
                "WHERE warehouse_code IS NULL AND EXISTS (SELECT 1 FROM store_orders o "
                "WHERE o.id=order_id AND o.dc_warehouse_code IS NOT NULL)"
            ).rowcount
        result["carts_rebuilt"] = _rebuild_carts(master_conn)
    return result


def registered_up(conn: sqlite3.Connection) -> None:
    """注册表适配：master 由 runner 备份/提交，仓文件以原路径打开并幂等升级。"""
    from config import BASE_DIR

    with ExitStack() as stack:
        warehouses = {}
        paths = {}
        if "db_path" in _columns(conn, "warehouses"):
            for code, db_path in conn.execute("SELECT code, db_path FROM warehouses"):
                path = (BASE_DIR / db_path).resolve()
                if not path.is_file():
                    raise FileNotFoundError(f"仓库 {code} 数据库不存在: {path}")
                if path not in paths:
                    wh = stack.enter_context(closing(sqlite3.connect(path)))
                    wh.execute("PRAGMA foreign_keys=ON")
                    stack.enter_context(wh)
                    paths[path] = wh
                warehouses[code] = paths[path]
        result = migrate_multi_warehouse_fulfillment(conn, warehouses)
        logging.getLogger(__name__).warning("多仓履约迁移报告: %s", result)


def registered_down(conn: sqlite3.Connection) -> None:
    raise RuntimeError("多仓履约迁移保留固化来源与批次事实，不可 down；请还原备份")
