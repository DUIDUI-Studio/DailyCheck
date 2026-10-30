"""多仓履约迁移：真实旧库、FK ON、回填门禁与注册表集成。"""

import sqlite3
from contextlib import closing

import pytest

from db import MASTER_SCHEMA, WAREHOUSE_SCHEMA, init_warehouse_db
from migrations import MIGRATIONS
from migrations_runner import migrate_up

LEGACY_SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY);
CREATE TABLE tenants (id INTEGER PRIMARY KEY);
CREATE TABLE warehouses (
    code TEXT PRIMARY KEY, db_path TEXT, tenant_id INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE canonical_items (id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL DEFAULT 1);
CREATE TABLE store_orders (
    id INTEGER PRIMARY KEY, dc_warehouse_code TEXT NOT NULL,
    FOREIGN KEY (dc_warehouse_code) REFERENCES warehouses(code)
);
CREATE TABLE store_order_items (
    id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL, canonical_id INTEGER NOT NULL,
    quantity REAL NOT NULL, shipped_quantity REAL NOT NULL DEFAULT 0,
    fulfilled_quantity REAL NOT NULL DEFAULT 0,
    FOREIGN KEY (order_id) REFERENCES store_orders(id)
);
CREATE TABLE store_order_deliveries (
    id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL,
    FOREIGN KEY (order_id) REFERENCES store_orders(id)
);
CREATE TABLE store_order_carts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
    store_warehouse_code TEXT NOT NULL, dc_warehouse_code TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE (user_id, store_warehouse_code),
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (store_warehouse_code) REFERENCES warehouses(code),
    FOREIGN KEY (dc_warehouse_code) REFERENCES warehouses(code)
);
CREATE INDEX idx_store_order_carts_user ON store_order_carts(user_id, store_warehouse_code);
CREATE TABLE store_order_cart_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT, cart_id INTEGER NOT NULL,
    canonical_id INTEGER NOT NULL, quantity REAL NOT NULL, unit TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    FOREIGN KEY (cart_id) REFERENCES store_order_carts(id) ON DELETE CASCADE,
    FOREIGN KEY (canonical_id) REFERENCES canonical_items(id)
);
CREATE INDEX idx_store_order_cart_items_cart ON store_order_cart_items(cart_id);
INSERT INTO users VALUES (1), (2);
INSERT INTO tenants VALUES (1);
INSERT INTO warehouses VALUES ('A', '', 1), ('B', '', 1), ('store', '', 1);
INSERT INTO canonical_items VALUES (1, 1);
INSERT INTO store_orders VALUES (1, 'A');
INSERT INTO store_order_items VALUES (1, 1, 1, 10, 4, 2);
INSERT INTO store_order_deliveries VALUES (1, 1);
INSERT INTO store_order_carts VALUES (7, 1, 'store', 'A', 'created', 'updated');
INSERT INTO store_order_cart_items VALUES (9, 7, 1, 3, '件', 'created', 'updated');
"""


@pytest.fixture
def legacy(tmp_path):
    with closing(sqlite3.connect(tmp_path / "master.db")) as master:
        master.execute("PRAGMA foreign_keys=ON")
        master.executescript(LEGACY_SCHEMA)
        with closing(sqlite3.connect(tmp_path / "A.db")) as warehouse:
            warehouse.execute("PRAGMA foreign_keys=ON")
            warehouse.executescript(
                "CREATE TABLE stock_movements (id INTEGER PRIMARY KEY, delta REAL);"
                "INSERT INTO stock_movements VALUES (1, -4);"
            )
            yield master, {"A": warehouse}


def _migrate(master, warehouses):
    # 延迟导入：先让全部行为用例因缺少约定接口而失败，避免 collection 中断。
    from migrations import migrate_multi_warehouse_fulfillment

    return migrate_multi_warehouse_fulfillment(master, warehouses)


def _columns(conn, table):
    return {r[1]: r for r in conn.execute(f"PRAGMA table_info({table})")}


def test_adds_schema_and_backfills_legacy_rows(legacy):
    master, warehouses = legacy
    result = _migrate(master, warehouses)
    assert result == {
        "backfilled_items": 1, "backfilled_deliveries": 1,
        "carts_rebuilt": True, "anomalies": [],
    }
    assert {"source_warehouse_code", "ship_op_no"} <= _columns(master, "store_order_items").keys()
    assert master.execute("SELECT source_warehouse_code FROM store_order_items").fetchone() == ("A",)
    assert master.execute("SELECT warehouse_code FROM store_order_deliveries").fetchone() == ("A",)
    assert "op_no" in _columns(warehouses["A"], "stock_movements")
    assert warehouses["A"].execute("SELECT id, delta, op_no FROM stock_movements").fetchone() == (
        1, -4, None,
    )
    assert {"delivery_id", "order_item_id", "warehouse_code", "item_id", "quantity",
            "op_no", "created_at"} <= _columns(master, "store_order_delivery_items").keys()
    assert {"tenant_id", "canonical_id", "fulfillment_warehouse_code", "updated_at",
            "updated_by"} == _columns(master, "item_routing").keys()


def test_rerun_preserves_frozen_sources_and_new_cross_warehouse_order(legacy):
    master, warehouses = legacy
    _migrate(master, warehouses)
    master.execute("UPDATE store_order_items SET source_warehouse_code='B', ship_op_no='frozen'")
    master.execute("UPDATE store_order_deliveries SET warehouse_code='B'")
    master.execute("INSERT INTO store_orders VALUES (2, 'A')")
    master.executemany(
        "INSERT INTO store_order_items "
        "(id, order_id, canonical_id, quantity, source_warehouse_code) VALUES (?, 2, 1, 1, ?)",
        [(2, "A"), (3, "B"), (4, None)],
    )
    result = _migrate(master, warehouses)
    assert result["backfilled_items"] == 1
    assert result["backfilled_deliveries"] == 0
    assert master.execute(
        "SELECT source_warehouse_code, ship_op_no FROM store_order_items ORDER BY id"
    ).fetchall() == [("B", "frozen"), ("A", None), ("B", None), ("A", None)]
    assert master.execute("SELECT warehouse_code FROM store_order_deliveries").fetchone() == ("B",)


def test_carts_rebuilt_with_fk_on_preserves_children_constraints_and_sequences(legacy):
    master, warehouses = legacy
    master.execute(
        "INSERT INTO store_order_carts VALUES (100, 2, 'store', 'A', 'c', 'u')"
    )
    master.execute("DELETE FROM store_order_carts WHERE id=100")
    master.commit()
    before = master.execute("SELECT * FROM store_order_carts").fetchall()
    children = master.execute("SELECT * FROM store_order_cart_items").fetchall()
    master.execute("BEGIN")  # runner 已在事务内；PRAGMA OFF 在这里无效。
    _migrate(master, warehouses)
    assert master.in_transaction
    assert master.execute("PRAGMA foreign_keys").fetchone() == (1,)
    assert _columns(master, "store_order_carts")["dc_warehouse_code"][3] == 0
    assert master.execute("SELECT * FROM store_order_carts").fetchall() == before
    assert master.execute("SELECT * FROM store_order_cart_items").fetchall() == children
    assert master.execute("PRAGMA foreign_key_check").fetchall() == []
    new_id = master.execute(
        "INSERT INTO store_order_carts (user_id, store_warehouse_code, created_at, updated_at) "
        "VALUES (2, 'store', 'c', 'u')"
    ).lastrowid
    assert new_id > 100
    with pytest.raises(sqlite3.IntegrityError):
        master.execute(
            "INSERT INTO store_order_carts (user_id, store_warehouse_code, created_at, updated_at) "
            "VALUES (2, 'store', 'c', 'u')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        master.execute("UPDATE store_order_carts SET dc_warehouse_code='missing' WHERE id=7")
    indexes = {r[0] for r in master.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"idx_store_order_carts_user", "idx_store_order_cart_items_cart"} <= indexes
    master.execute("DELETE FROM store_order_carts WHERE id=7")
    assert master.execute("SELECT * FROM store_order_cart_items").fetchall() == []


@pytest.mark.parametrize("quantity,shipped,fulfilled", [
    (10, -1, 0), (10, 4, -1), (10, 11, 2), (10, 4, 5), (-1, 0, 0),
])
def test_anomalous_rows_are_reported_and_never_modified(legacy, quantity, shipped, fulfilled):
    master, warehouses = legacy
    master.execute(
        "UPDATE store_order_items SET quantity=?, shipped_quantity=?, fulfilled_quantity=?",
        (quantity, shipped, fulfilled),
    )
    before = master.execute("SELECT * FROM store_order_items").fetchone()
    result = _migrate(master, warehouses)
    assert result["backfilled_items"] == 0
    assert len(result["anomalies"]) == 1
    anomaly = result["anomalies"][0]
    assert anomaly["order_item_id"] == 1
    assert anomaly["order_id"] == 1
    assert (anomaly["quantity"], anomaly["shipped_quantity"], anomaly["fulfilled_quantity"]) == (
        quantity, shipped, fulfilled,
    )
    assert master.execute(
        "SELECT id, order_id, canonical_id, quantity, shipped_quantity, fulfilled_quantity "
        "FROM store_order_items"
    ).fetchone() == before
    assert master.execute("SELECT source_warehouse_code FROM store_order_items").fetchone() == (None,)
    assert _migrate(master, warehouses)["anomalies"] == result["anomalies"]


def test_repeated_runs_are_noops_with_consistent_report(legacy):
    master, warehouses = legacy
    _migrate(master, warehouses)
    second = _migrate(master, warehouses)
    assert second == {
        "backfilled_items": 0, "backfilled_deliveries": 0,
        "carts_rebuilt": False, "anomalies": [],
    }
    assert _migrate(master, warehouses) == second


def test_new_ddl_has_nullable_cart_and_enforces_operation_and_routing_keys():
    with closing(sqlite3.connect(":memory:")) as master:
        master.executescript(MASTER_SCHEMA)
        assert "source_warehouse_code" in _columns(master, "store_order_items")
        assert "ship_op_no" in _columns(master, "store_order_items")
        assert "warehouse_code" in _columns(master, "store_order_deliveries")
        assert _columns(master, "store_order_carts")["dc_warehouse_code"][3] == 0
        master.execute("INSERT INTO item_routing VALUES (1, 1, 'A', 'now', NULL)")
        with pytest.raises(sqlite3.IntegrityError):
            master.execute("INSERT INTO item_routing VALUES (1, 1, 'B', 'now', NULL)")
        master.execute("INSERT INTO store_order_delivery_items VALUES (1, 1, 'A', 1, 2, 'op', 'now')")
        with pytest.raises(sqlite3.IntegrityError):
            master.execute(
                "INSERT INTO store_order_delivery_items VALUES (2, 2, 'B', 1, 2, 'op', 'now')"
            )
    with closing(sqlite3.connect(":memory:")) as warehouse:
        warehouse.executescript(WAREHOUSE_SCHEMA)
        assert "op_no" in _columns(warehouse, "stock_movements")


def test_warehouse_version_gate_upgrades_existing_stock_movements(tmp_path):
    path = tmp_path / "warehouse.db"
    init_warehouse_db(path)
    with closing(sqlite3.connect(path)) as conn:
        if "op_no" in _columns(conn, "stock_movements"):
            conn.execute("ALTER TABLE stock_movements DROP COLUMN op_no")
        conn.execute("PRAGMA user_version=5")
        conn.commit()
    from db import migrate_warehouse_db_columns

    migrate_warehouse_db_columns(path)
    with closing(sqlite3.connect(path)) as conn:
        assert "op_no" in _columns(conn, "stock_movements")


def test_registered_migration_upgrades_master_and_registered_warehouse(legacy, tmp_path):
    master, warehouses = legacy
    master.execute("UPDATE warehouses SET db_path=? WHERE code='A'", (str(tmp_path / "A.db"),))
    # 只有实际登记的仓文件需要迁移。
    master.execute("UPDATE warehouses SET db_path=? WHERE code IN ('B', 'store')",
                   (str(tmp_path / "A.db"),))
    master.commit()
    warehouses["A"].commit()
    assert migrate_up(tmp_path / "master.db", MIGRATIONS, tmp_path / "backups")
    assert master.execute("SELECT source_warehouse_code FROM store_order_items").fetchone() == ("A",)
    assert "op_no" in _columns(warehouses["A"], "stock_movements")
    assert migrate_up(tmp_path / "master.db", MIGRATIONS, tmp_path / "backups") == []


def test_cart_rebuild_can_be_rolled_back_by_caller(legacy):
    master, warehouses = legacy
    master.execute("BEGIN")
    _migrate(master, warehouses)
    master.rollback()
    assert _columns(master, "store_order_carts")["dc_warehouse_code"][3] == 1
    assert master.execute("SELECT id FROM store_order_cart_items").fetchall() == [(9,)]
    assert "source_warehouse_code" not in _columns(master, "store_order_items")
