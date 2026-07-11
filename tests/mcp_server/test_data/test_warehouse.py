"""Test warehouse.py data access layer."""
import pytest
import sqlite3

from mcp_server.data import warehouse


def create_items_table(conn: sqlite3.Connection) -> None:
    """Create items table with required schema."""
    conn.execute(
        "CREATE TABLE items ("
        "id INTEGER PRIMARY KEY, sku TEXT, name TEXT, category_id INTEGER, "
        "quantity INTEGER, safety_stock INTEGER, unit TEXT, "
        "unit_cost REAL, gram_per_unit REAL, updated_at TEXT)"
    )


def create_stock_movements_table(conn: sqlite3.Connection) -> None:
    """Create stock_movements table with required schema."""
    conn.execute(
        "CREATE TABLE stock_movements ("
        "id INTEGER PRIMARY KEY, item_id INTEGER, delta INTEGER, "
        "action TEXT, created_at TEXT)"
    )


def create_outbound_requests_table(conn: sqlite3.Connection) -> None:
    """Create outbound_requests table with required schema."""
    conn.execute(
        "CREATE TABLE outbound_requests ("
        "id INTEGER PRIMARY KEY, item_id INTEGER, requested_quantity INTEGER, "
        "reason TEXT, created_at TEXT, rolled_back INTEGER DEFAULT 0)"
    )


def test_list_items_empty():
    """Test list_items returns empty list when no items."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_items_table(conn)
    assert warehouse.list_items(conn) == []


def test_list_items_returns_rows():
    """Test list_items returns items with correct columns."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_items_table(conn)
    conn.execute(
        "INSERT INTO items (sku, name, quantity, unit, unit_cost) "
        "VALUES ('SKU1', 'Test Item', 10, 'pcs', 5.50)"
    )
    items = warehouse.list_items(conn)
    assert len(items) == 1
    assert items[0]["sku"] == "SKU1"
    assert items[0]["name"] == "Test Item"
    assert items[0]["quantity"] == 10


def test_get_item_returns_none_for_missing():
    """Test get_item returns None when item not found."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_items_table(conn)
    assert warehouse.get_item(conn, 999) is None


def test_get_item_returns_item():
    """Test get_item returns item when found."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_items_table(conn)
    conn.execute(
        "INSERT INTO items (sku, name, quantity) VALUES ('SKU1', 'Test', 10)"
    )
    item = warehouse.get_item(conn, 1)
    assert item is not None
    assert item["sku"] == "SKU1"


def test_item_exists_returns_false():
    """Test item_exists returns False when item not found."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_items_table(conn)
    assert warehouse.item_exists(conn, 999) is False


def test_item_exists_returns_true():
    """Test item_exists returns True when item exists."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_items_table(conn)
    conn.execute("INSERT INTO items (sku, name) VALUES ('SKU1', 'Test')")
    assert warehouse.item_exists(conn, 1) is True


def test_create_restock_increases_quantity():
    """Test create_restock adds restock record and updates quantity."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_items_table(conn)
    create_stock_movements_table(conn)

    # Insert item with initial quantity
    conn.execute(
        "INSERT INTO items (sku, name, quantity) VALUES ('SKU1', 'Test', 10)"
    )

    # Create restock (use None to get default action='restock')
    restock_id = warehouse.create_restock(conn, 1, 5, None)

    assert restock_id is not None

    # Check item quantity updated
    item = warehouse.get_item(conn, 1)
    assert item["quantity"] == 15

    # Check stock_movements record
    movements = warehouse.list_restock_movements(conn)
    assert len(movements) == 1
    assert movements[0]["qty"] == 5


def test_list_restock_movements_empty():
    """Test list_restock_movements returns empty when no restocks."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_items_table(conn)
    create_stock_movements_table(conn)
    assert warehouse.list_restock_movements(conn) == []


def test_list_movements_empty():
    """Test list_movements returns empty when no movements."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_items_table(conn)
    create_stock_movements_table(conn)
    create_outbound_requests_table(conn)
    assert warehouse.list_movements(conn) == []


def test_list_movements_includes_stock_movements():
    """Test list_movements includes stock movements."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_items_table(conn)
    create_stock_movements_table(conn)
    create_outbound_requests_table(conn)

    # Insert item
    conn.execute("INSERT INTO items (sku, name) VALUES ('SKU1', 'Test')")

    # Insert stock movement
    conn.execute(
        "INSERT INTO stock_movements (item_id, delta, action, created_at) "
        "VALUES (1, 10, 'restock', '2024-01-01 10:00:00')"
    )

    movements = warehouse.list_movements(conn)
    assert len(movements) == 1
    assert movements[0]["type"] == "stock_movement"
    assert movements[0]["qty"] == 10


def test_list_movements_includes_outbound():
    """Test list_movements includes outbound requests."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_items_table(conn)
    create_stock_movements_table(conn)
    create_outbound_requests_table(conn)

    # Insert item
    conn.execute("INSERT INTO items (sku, name) VALUES ('SKU1', 'Test')")

    # Insert outbound request
    conn.execute(
        "INSERT INTO outbound_requests "
        "(item_id, requested_quantity, reason, created_at, rolled_back) "
        "VALUES (1, 5, 'order #123', '2024-01-01 10:00:00', 0)"
    )

    movements = warehouse.list_movements(conn)
    assert len(movements) == 1
    assert movements[0]["type"] == "outbound"
    assert movements[0]["qty"] == 5


def test_list_movements_excludes_rolled_back():
    """Test list_movements excludes rolled back outbound requests."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_items_table(conn)
    create_stock_movements_table(conn)
    create_outbound_requests_table(conn)

    # Insert item
    conn.execute("INSERT INTO items (sku, name) VALUES ('SKU1', 'Test')")

    # Insert rolled back outbound request
    conn.execute(
        "INSERT INTO outbound_requests "
        "(item_id, requested_quantity, reason, created_at, rolled_back) "
        "VALUES (1, 5, 'order #123', '2024-01-01 10:00:00', 1)"
    )

    movements = warehouse.list_movements(conn)
    assert len(movements) == 0
