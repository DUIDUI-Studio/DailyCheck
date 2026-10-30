"""Fulfillment must resolve one eligible row in the explicitly supplied warehouse."""
from __future__ import annotations

import importlib
import importlib.util
import sqlite3

import pytest


def _module():
    assert importlib.util.find_spec("blueprints.fulfillment_pure") is not None, (
        "fulfillment resolver has not been implemented"
    )
    return importlib.import_module("blueprints.fulfillment_pure")


class NoFirstRowCursor(sqlite3.Cursor):
    def fetchone(self):
        raise AssertionError("fulfillment must explicitly check candidate uniqueness")


class NoFirstRowConnection(sqlite3.Connection):
    def cursor(self, factory=NoFirstRowCursor):
        return super().cursor(factory)

    def execute(self, sql, parameters=()):
        return self.cursor().execute(sql, parameters)


@pytest.fixture(params=[None, sqlite3.Row], ids=["tuples", "sqlite-rows"])
def databases(request):
    connections = [
        sqlite3.connect(":memory:", factory=NoFirstRowConnection) for _ in range(3)
    ]
    master, warehouse_a, warehouse_b = connections
    master.executescript(
        """CREATE TABLE canonical_items (id INTEGER PRIMARY KEY, unit TEXT NOT NULL);
        INSERT INTO canonical_items VALUES (101, '件');
        CREATE TABLE canonical_conflicts (
            id INTEGER PRIMARY KEY, canonical_id INTEGER, warehouse_code TEXT,
            local_item_id INTEGER, field TEXT, conflict_type TEXT, status TEXT
        );"""
    )
    for warehouse in (warehouse_a, warehouse_b):
        warehouse.execute(
            """CREATE TABLE items (
                id INTEGER PRIMARY KEY, canonical_id INTEGER, unit TEXT DEFAULT '件',
                quantity REAL DEFAULT 10, unit_cost REAL DEFAULT 3,
                selling_price REAL DEFAULT 5, order_multiple REAL DEFAULT 2,
                is_active INTEGER DEFAULT 1, is_alias INTEGER DEFAULT 0
            )"""
        )
    for connection in connections:
        connection.row_factory = request.param
    yield connections
    for connection in connections:
        connection.close()


def _item(warehouse, item_id=7, **overrides):
    values = {"id": item_id, "canonical_id": 101, **overrides}
    warehouse.execute(
        f"INSERT INTO items ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
        tuple(values.values()),
    )


def _resolve(fp, databases, warehouse_code="A", warehouse_index=1, canonical_id=101):
    master = databases[0]
    factory_before = (master.row_factory, databases[warehouse_index].row_factory)
    result = fp.resolve_fulfillment_item(
        master, databases[warehouse_index],
        canonical_id=canonical_id, warehouse_code=warehouse_code,
    )
    assert (master.row_factory, databases[warehouse_index].row_factory) == factory_before
    return result


def test_unique_active_binding_returns_exact_contract(databases):
    fp = _module()
    _item(databases[1])
    assert _resolve(fp, databases) == {
        "item_id": 7, "unit": "件", "unit_cost": 3.0,
        "order_multiple": 2.0, "available": 10.0,
    }


@pytest.mark.parametrize("ignored", [{"is_alias": 1}, {"is_active": 0}])
def test_ignored_row_does_not_make_valid_binding_ambiguous(databases, ignored):
    fp = _module()
    _item(databases[1], item_id=1, unit="箱", **ignored)
    _item(databases[1], item_id=7)
    assert _resolve(fp, databases)["item_id"] == 7


def test_two_active_bindings_fail_with_context(databases):
    fp = _module()
    _item(databases[1], item_id=1)
    _item(databases[1], item_id=7)
    with pytest.raises(fp.FulfillmentAmbiguousError) as error:
        _resolve(fp, databases)
    assert error.value.canonical_id == 101
    assert error.value.warehouse_code == "A"
    assert "101" in str(error.value) and "A" in str(error.value)


@pytest.mark.parametrize("ignored", [None, {"is_alias": 1}, {"is_active": 0}])
def test_no_eligible_binding_fails_with_context(databases, ignored):
    fp = _module()
    if ignored is not None:
        _item(databases[1], **ignored)
    _item(databases[1], item_id=9, canonical_id=102)
    with pytest.raises(fp.FulfillmentMissingError) as error:
        _resolve(fp, databases)
    assert error.value.canonical_id == 101
    assert error.value.warehouse_code == "A"
    assert "101" in str(error.value) and "A" in str(error.value)


@pytest.mark.parametrize("unit", ["箱", "", None])
def test_incomparable_unit_fails_with_context(databases, unit):
    fp = _module()
    _item(databases[1], unit=unit)
    with pytest.raises(fp.FulfillmentConflictError) as error:
        _resolve(fp, databases)
    assert error.value.canonical_id == 101
    assert error.value.warehouse_code == "A"
    assert "101" in str(error.value) and "A" in str(error.value)


def test_missing_canonical_cannot_supply_a_comparable_unit(databases):
    fp = _module()
    _item(databases[1], canonical_id=999)
    with pytest.raises(fp.FulfillmentMissingError):
        _resolve(fp, databases, canonical_id=999)


@pytest.mark.parametrize("conflict_type", ["unit_conversion", "double_bind"])
def test_open_blocking_conflict_rejects_matching_binding(databases, conflict_type):
    fp = _module()
    _item(databases[1])
    databases[0].execute(
        "INSERT INTO canonical_conflicts VALUES (1, 101, 'A', 7, 'unit', ?, 'open')",
        (conflict_type,),
    )
    with pytest.raises(fp.FulfillmentConflictError):
        _resolve(fp, databases)


@pytest.mark.parametrize(
    ("canonical_id", "warehouse_code", "item_id", "status"),
    [
        (102, "A", 7, "open"),
        (101, "B", 7, "open"),
        (101, "A", 9, "open"),
        (101, "A", 7, "resolved_keep_local"),
        (101, "A", 7, "resolved_accept_canonical"),
        (101, "A", 7, "waived"),
    ],
)
def test_unrelated_or_resolved_conflict_does_not_block(
    databases, canonical_id, warehouse_code, item_id, status,
):
    fp = _module()
    _item(databases[1])
    databases[0].execute(
        "INSERT INTO canonical_conflicts VALUES (1, ?, ?, ?, 'unit', 'unit_conversion', ?)",
        (canonical_id, warehouse_code, item_id, status),
    )
    assert _resolve(fp, databases)["item_id"] == 7


def test_same_local_id_in_independent_warehouses_does_not_leak(databases):
    fp = _module()
    _item(databases[1], unit_cost=3, order_multiple=2, quantity=10)
    _item(databases[2], unit_cost=8, order_multiple=5, quantity=70)
    assert _resolve(fp, databases) == {
        "item_id": 7, "unit": "件", "unit_cost": 3.0,
        "order_multiple": 2.0, "available": 10.0,
    }
    assert _resolve(fp, databases, "B", 2) == {
        "item_id": 7, "unit": "件", "unit_cost": 8.0,
        "order_multiple": 5.0, "available": 70.0,
    }
    databases[2].execute("UPDATE items SET is_active=0 WHERE id=7")
    with pytest.raises(fp.FulfillmentMissingError):
        _resolve(fp, databases, "B", 2)
    assert _resolve(fp, databases)["available"] == 10.0


@pytest.mark.parametrize(
    ("selling_price", "unit_cost", "multiple", "expected"),
    [
        (8, 3, 2, (8.0, 2.0)),
        (0, 3, None, (3.0, 1.0)),
        (-8, 3, 0, (3.0, 1.0)),
        (None, None, -2, (0.0, 1.0)),
        ("8.25", "3.5", "2.555", (8.25, 2.56)),
        (None, -3, "bad", (-3.0, 1.0)),
        (2, 3, 0.001, (2.0, 0.0)),
    ],
)
def test_price_and_multiple_preserve_existing_cart_rules(
    selling_price, unit_cost, multiple, expected,
):
    fp = _module()
    row = {"selling_price": selling_price, "unit_cost": unit_cost, "order_multiple": multiple}
    assert fp.parse_price_and_multiple(row) == expected


def test_missing_item_uses_existing_price_and_multiple_defaults():
    fp = _module()
    assert fp.parse_price_and_multiple(None) == (0.0, 1.0)


def test_price_parser_accepts_sqlite_row_without_flask_context(databases):
    fp = _module()
    _item(databases[1])
    cursor = databases[1].cursor()
    cursor.row_factory = sqlite3.Row
    row = cursor.execute("SELECT * FROM items WHERE id=7").fetchall()[0]
    assert fp.parse_price_and_multiple(row) == (5.0, 2.0)
