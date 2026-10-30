"""SQL participation uses line sources, without multiplying order counts."""
import sqlite3

import pytest

from blueprints import store_ordering_pure as sop


@pytest.fixture
def orders():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE store_orders (id INTEGER PRIMARY KEY, dc_warehouse_code TEXT);
        CREATE TABLE store_order_items (order_id INTEGER, source_warehouse_code TEXT);
        INSERT INTO store_orders VALUES (1, 'A'), (2, 'A'), (3, 'A'), (4, 'HEAD');
        INSERT INTO store_order_items VALUES
            (1, 'A'), (1, 'A'),
            (2, 'B'), (2, 'A'), (2, 'B'),
            (3, NULL), (3, NULL),
            (4, 'B');
    """)
    yield conn
    conn.close()


@pytest.mark.parametrize(
    ("order_id", "warehouse", "count"),
    [(1, 'A', 1), (1, 'B', 0), (2, 'A', 1), (2, 'B', 1),
     (3, 'A', 1), (3, 'B', 0), (4, 'HEAD', 0), (4, 'B', 1)],
)
def test_participation_counts_order_once(orders, order_id, warehouse, count):
    predicate = sop.order_participation_sql()
    actual = orders.execute(
        f"SELECT COUNT(*) FROM store_orders so WHERE so.id=? AND {predicate}",
        (order_id, warehouse),
    ).fetchone()[0]
    assert actual == count


def test_participation_custom_alias_and_total(orders):
    predicate = sop.order_participation_sql(alias="orders")
    assert orders.execute(
        f"SELECT COUNT(*) FROM store_orders orders WHERE {predicate}", ('A',),
    ).fetchone()[0] == 3


@pytest.mark.parametrize(
    ("order_id", "expected"),
    [(1, ['A']), (2, ['A', 'B']), (3, ['A']), (4, ['B']), (999, [])],
)
def test_participants_deduplicated_sorted_with_null_fallback(orders, order_id, expected):
    assert sop.order_participants(orders, order_id) == expected
