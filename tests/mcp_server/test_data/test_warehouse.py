"""Test warehouse.py data access layer."""
import datetime
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


def create_categories_table(conn: sqlite3.Connection) -> None:
    """Create categories table for list_items JOIN."""
    conn.execute(
        "CREATE TABLE categories ("
        "id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, "
        "description TEXT, created_at TEXT)"
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
    create_categories_table(conn)
    create_items_table(conn)
    assert warehouse.list_items(conn) == []


def test_list_items_returns_rows():
    """Test list_items returns items with correct columns, including category_name."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_categories_table(conn)
    create_items_table(conn)
    conn.execute(
        "INSERT INTO categories (id, name) VALUES (1, '调味酱')"
    )
    conn.execute(
        "INSERT INTO items (sku, name, category_id, quantity, unit, unit_cost) "
        "VALUES ('SKU1', 'Test Item', 1, 10, 'pcs', 5.50)"
    )
    items = warehouse.list_items(conn)
    assert len(items) == 1
    assert items[0]["sku"] == "SKU1"
    assert items[0]["name"] == "Test Item"
    assert items[0]["current_stock"] == 10
    assert items[0]["category_name"] == "调味酱"


def test_get_item_returns_none_for_missing():
    """Test get_item returns None when item not found."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_categories_table(conn)  # required by get_item's category subquery
    create_items_table(conn)
    assert warehouse.get_item(conn, 999) is None


def test_get_item_returns_item():
    """Test get_item returns item when found, including category_name + current_stock."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_categories_table(conn)
    create_items_table(conn)
    conn.execute(
        "INSERT INTO categories (id, name) VALUES (1, '调味酱')"
    )
    conn.execute(
        "INSERT INTO items (sku, name, category_id, quantity) "
        "VALUES ('SKU1', 'Test', 1, 10)"
    )
    item = warehouse.get_item(conn, 1)
    assert item is not None
    assert item["sku"] == "SKU1"
    assert item["current_stock"] == 10
    assert item["category_name"] == "调味酱"


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


# ---------------------------------------------------------------------------
# get_inventory_turnover — stocktake-anchored
# ---------------------------------------------------------------------------

def create_stocktake_batches_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE stocktake_batches ("
        "id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, note TEXT, "
        "rolled_back INTEGER NOT NULL DEFAULT 0, status TEXT, loss_req_ids TEXT)"
    )


def create_stocktakes_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE stocktakes ("
        "id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL, "
        "previous_quantity INTEGER NOT NULL, actual_quantity INTEGER NOT NULL, "
        "diff INTEGER NOT NULL, batch_id INTEGER, created_at TEXT NOT NULL, note TEXT)"
    )


def create_restock_requests_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE restock_requests ("
        "id INTEGER PRIMARY KEY, item_id INTEGER, requested_quantity INTEGER, "
        "reason TEXT, status TEXT, created_at TEXT)"
    )


def create_production_runs_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE production_runs ("
        "id INTEGER PRIMARY KEY, rolled_back INTEGER NOT NULL DEFAULT 0, "
        "created_at TEXT NOT NULL)"
    )


def create_production_run_items_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE production_run_items ("
        "id INTEGER PRIMARY KEY, run_id INTEGER, item_id INTEGER, "
        "actual_qty REAL)"
    )


def _setup_turnover_env(
    item_id: int = 1,
    qty: int = 100,
    unit_cost: float = 10.0,
) -> sqlite3.Connection:
    """Minimal schema + one item, suitable for turnover tests."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_items_table(conn)
    create_stocktake_batches_table(conn)
    create_stocktakes_table(conn)
    create_outbound_requests_table(conn)
    create_production_runs_table(conn)
    create_production_run_items_table(conn)
    conn.execute(
        "INSERT INTO items (id, sku, name, quantity, unit_cost) "
        "VALUES (?, 'SKU1', 'Test', ?, ?)",
        (item_id, qty, unit_cost),
    )
    return conn


def _insert_batch(
    conn: sqlite3.Connection,
    batch_id: int,
    created_at: str,
    rolled_back: int = 0,
) -> None:
    conn.execute(
        "INSERT INTO stocktake_batches (id, created_at, rolled_back) "
        "VALUES (?, ?, ?)",
        (batch_id, created_at, rolled_back),
    )


def _insert_stocktake(
    conn: sqlite3.Connection,
    item_id: int,
    batch_id: int,
    previous_quantity: int,
) -> None:
    conn.execute(
        "INSERT INTO stocktakes (item_id, batch_id, previous_quantity, "
        "actual_quantity, diff, created_at) "
        "VALUES (?, ?, ?, ?, 0, ?)",
        (item_id, batch_id, previous_quantity, previous_quantity, "2026-01-01 00:00:00"),
    )


def test_inventory_turnover_no_anchors_returns_none():
    """No stocktake anchors → data_quality='none', avg_inventory=None."""
    conn = _setup_turnover_env()
    now = datetime.datetime(2026, 7, 22, 12, 0, 0)
    result = warehouse.get_inventory_turnover(conn, item_id=1, days=30, _now=now)
    assert result is not None
    assert result["avg_inventory"] is None
    assert result["turnover_value"] is None
    assert result["anchors_in_window"] == 0
    assert result["anchors_total"] == 0
    assert result["data_quality"] == "none"
    assert result["method"] == "stocktake_weighted_avg"


def test_inventory_turnover_single_anchor_in_window():
    """1 anchor inside window → data_quality='medium', avg_inventory=None (can't average)."""
    conn = _setup_turnover_env(qty=80, unit_cost=5.0)
    now = datetime.datetime(2026, 7, 22, 12, 0, 0)
    # Anchor 5 days ago, qty=100
    _insert_batch(conn, 1, "2026-07-17 10:00:00")
    _insert_stocktake(conn, 1, 1, 100)

    result = warehouse.get_inventory_turnover(conn, item_id=1, days=30, _now=now)
    assert result["anchors_in_window"] == 1
    assert result["anchors_total"] == 1
    assert result["data_quality"] == "medium"
    assert result["avg_inventory"] is None  # can't average with 1
    assert result["turnover_value"] is None
    assert result["current_inventory"] == 80


def test_inventory_turnover_two_anchors_weighted_avg():
    """2 anchors → weighted-by-gap average.

    Setup (all times relative to pinned _now = 2026-07-22 12:00):
      - window = [2026-06-22 12:00, 2026-07-22 12:00] (30 days)
      - anchor A: 2026-07-02 12:00 (qty=100, 20 days back, inside window)
      - anchor B: 2026-07-12 12:00 (qty=50,  10 days back, inside window)
      - current qty at end = 50

    Boundary padding prepends (start_ts, qty=100 from earliest in-window anchor)
    since 2026-07-02 12:00 > start_ts 2026-06-22 12:00.

    Segments (qty × days → weight contribution):
      [2026-06-22 12:00, 2026-07-02 12:00] qty=100 × 10d = 1000
      [2026-07-02 12:00, 2026-07-12 12:00] qty=100 × 10d = 1000
      [2026-07-12 12:00, 2026-07-22 12:00] qty=50  × 10d = 500

    weighted sum = 2500, total gap = 30, avg = 83.33
    """
    conn = _setup_turnover_env(qty=50, unit_cost=5.0)
    now = datetime.datetime(2026, 7, 22, 12, 0, 0)
    _insert_batch(conn, 1, "2026-07-02 12:00:00")
    _insert_stocktake(conn, 1, 1, 100)
    _insert_batch(conn, 2, "2026-07-12 12:00:00")
    _insert_stocktake(conn, 1, 2, 50)

    result = warehouse.get_inventory_turnover(conn, item_id=1, days=30, _now=now)
    assert result["anchors_in_window"] == 2
    assert result["data_quality"] == "high"
    assert result["avg_inventory"] == 83.33
    # No consumption → COGS=0 → turnover=0.0 (mathematically meaningful)
    assert result["turnover_value"] == 0.0


def test_inventory_turnover_with_cogs_calculates_turnover():
    """With outbound consumption, turnover = cogs_value / avg_inventory.

    Setup:
      - now = 2026-07-22 12:00, current qty=50, unit_cost=10
      - anchor A: 2026-07-02 12:00 (qty=100)
      - anchor B: 2026-07-12 12:00 (qty=50)
      - outbound consume: 30 units at 2026-07-15 12:00 (inside window)

    avg_inventory = 83.33 (same as two-anchors test)
    cogs_value = 30 × 10 = 300
    turnover = 300 / 83.33 ≈ 3.6
    """
    conn = _setup_turnover_env(qty=50, unit_cost=10.0)
    now = datetime.datetime(2026, 7, 22, 12, 0, 0)
    _insert_batch(conn, 1, "2026-07-02 12:00:00")
    _insert_stocktake(conn, 1, 1, 100)
    _insert_batch(conn, 2, "2026-07-12 12:00:00")
    _insert_stocktake(conn, 1, 2, 50)
    conn.execute(
        "INSERT INTO outbound_requests "
        "(item_id, requested_quantity, reason, created_at, rolled_back) "
        "VALUES (1, 30, 'order#1', '2026-07-15 12:00:00', 0)"
    )

    result = warehouse.get_inventory_turnover(conn, item_id=1, days=30, _now=now)
    assert result["cogs_value"] == 300.0
    assert result["avg_inventory"] == 83.33
    assert result["turnover_value"] == 3.6
    assert result["data_quality"] == "high"


def test_inventory_turnover_excludes_rolled_back_batches():
    """Rolled-back batches' anchors must not count."""
    conn = _setup_turnover_env(qty=80, unit_cost=5.0)
    now = datetime.datetime(2026, 7, 22, 12, 0, 0)
    _insert_batch(conn, 1, "2026-07-02 12:00:00", rolled_back=1)
    _insert_stocktake(conn, 1, 1, 100)
    _insert_batch(conn, 2, "2026-07-12 12:00:00", rolled_back=0)
    _insert_stocktake(conn, 1, 2, 90)

    result = warehouse.get_inventory_turnover(conn, item_id=1, days=30, _now=now)
    assert result["anchors_total"] == 1
    assert result["anchors_in_window"] == 1
    assert result["data_quality"] == "medium"
    assert result["avg_inventory"] is None


def test_inventory_turnover_includes_production_consume_in_cogs():
    """Production-run consume is included in COGS via UNION ALL."""
    conn = _setup_turnover_env(qty=50, unit_cost=10.0)
    now = datetime.datetime(2026, 7, 22, 12, 0, 0)
    _insert_batch(conn, 1, "2026-07-02 12:00:00")
    _insert_stocktake(conn, 1, 1, 100)
    _insert_batch(conn, 2, "2026-07-12 12:00:00")
    _insert_stocktake(conn, 1, 2, 50)

    conn.execute(
        "INSERT INTO production_runs (id, rolled_back, created_at) "
        "VALUES (1, 0, '2026-07-15 12:00:00')"
    )
    conn.execute(
        "INSERT INTO production_run_items (run_id, item_id, actual_qty) "
        "VALUES (1, 1, 20)"
    )

    result = warehouse.get_inventory_turnover(conn, item_id=1, days=30, _now=now)
    assert result["cogs_value"] == 200.0  # 20 × 10
    assert result["avg_inventory"] == 83.33
    assert result["turnover_value"] == 2.4  # 200 / 83.33


def test_inventory_turnover_excludes_rolled_back_outbound_from_cogs():
    conn = _setup_turnover_env(qty=50, unit_cost=10.0)
    now = datetime.datetime(2026, 7, 22, 12, 0, 0)
    _insert_batch(conn, 1, "2026-07-02 12:00:00")
    _insert_stocktake(conn, 1, 1, 100)
    _insert_batch(conn, 2, "2026-07-12 12:00:00")
    _insert_stocktake(conn, 1, 2, 50)

    conn.execute(
        "INSERT INTO outbound_requests "
        "(item_id, requested_quantity, reason, created_at, rolled_back) "
        "VALUES (1, 30, 'order#1', '2026-07-15 12:00:00', 1)"
    )

    result = warehouse.get_inventory_turnover(conn, item_id=1, days=30, _now=now)
    assert result["cogs_value"] == 0.0
    # avg_inventory still computable (2 anchors); COGS=0 → turnover=0.0
    assert result["avg_inventory"] == 83.33
    assert result["turnover_value"] == 0.0


def test_inventory_turnover_returns_none_for_missing_item():
    conn = _setup_turnover_env()
    now = datetime.datetime(2026, 7, 22, 12, 0, 0)
    result = warehouse.get_inventory_turnover(conn, item_id=999, days=30, _now=now)
    assert result is None


# ---------------------------------------------------------------------------
# get_warehouse_inventory_turnover + list_consumption_summary (warehouse-level)
# ---------------------------------------------------------------------------

def _add_item(conn: sqlite3.Connection, item_id: int, qty: int, unit_cost: float) -> None:
    conn.execute(
        "INSERT INTO items (id, sku, name, category_id, quantity, unit_cost) "
        "VALUES (?, ?, ?, 1, ?, ?)",
        (item_id, f"SKU{item_id}", f"Item {item_id}", qty, unit_cost),
    )


def _add_anchor(
    conn: sqlite3.Connection,
    item_id: int,
    batch_id: int,
    created_at: str,
    previous_quantity: int,
    rolled_back: int = 0,
) -> None:
    _insert_batch(conn, batch_id, created_at, rolled_back=rolled_back)
    _insert_stocktake(conn, item_id, batch_id, previous_quantity)


def test_warehouse_turnover_aggregates_per_item():
    """Two items with anchors: Σ(cogs_value) / Σ(avg_inventory × unit_cost).

    Item 1: anchors [100, 50] over 30d, current=50, unit_cost=10
      → avg_inventory = 83.33, avg_value = 833.33
    Item 2: anchors [200, 100] over 30d, current=100, unit_cost=5
      → avg_inventory = 166.67, avg_value = 833.33
    Total avg_value = 1666.66
    No outbound for either item → warehouse_cogs = 0 → turnover = 0
    """
    conn = _setup_turnover_env(qty=50, unit_cost=10.0)
    _add_item(conn, 2, 100, 5.0)
    now = datetime.datetime(2026, 7, 22, 12, 0, 0)

    _add_anchor(conn, 1, 1, "2026-07-02 12:00:00", 100)
    _add_anchor(conn, 1, 2, "2026-07-12 12:00:00", 50)
    _add_anchor(conn, 2, 3, "2026-07-02 12:00:00", 200)
    _add_anchor(conn, 2, 4, "2026-07-12 12:00:00", 100)

    result = warehouse.get_warehouse_inventory_turnover(conn, days=30, _now=now)
    assert result["items_total"] == 2
    assert result["items_with_turnover"] == 2
    assert result["data_quality"] == "high"
    assert result["warehouse_cogs_value"] == 0.0
    assert result["turnover_value"] == 0.0


def test_warehouse_turnover_skips_items_with_no_anchors():
    """Items without anchors contribute to items_total but not to turnover."""
    conn = _setup_turnover_env(qty=50, unit_cost=10.0)
    _add_item(conn, 2, 50, 10.0)
    now = datetime.datetime(2026, 7, 22, 12, 0, 0)

    _add_anchor(conn, 1, 1, "2026-07-02 12:00:00", 100)
    _add_anchor(conn, 1, 2, "2026-07-12 12:00:00", 50)

    result = warehouse.get_warehouse_inventory_turnover(conn, days=30, _now=now)
    assert result["items_total"] == 2
    assert result["items_with_turnover"] == 1
    assert result["data_quality"] == "medium"


def test_warehouse_turnover_with_cogs_calculates_turnover():
    """Outbound consumption drives warehouse_cogs, giving a non-zero turnover.

    Setup:
      Item 1: avg_value = 83.33 × 10 = 833.33
      Item 2: avg_value = 166.67 × 5 = 833.33
      Total avg_value = 1666.66
      Outbound: 30 from item 1 (cogs=300), 20 from item 2 (cogs=100)
      turnover = 400 / 1666.66 ≈ 0.24
    """
    conn = _setup_turnover_env(qty=50, unit_cost=10.0)
    _add_item(conn, 2, 100, 5.0)
    now = datetime.datetime(2026, 7, 22, 12, 0, 0)

    _add_anchor(conn, 1, 1, "2026-07-02 12:00:00", 100)
    _add_anchor(conn, 1, 2, "2026-07-12 12:00:00", 50)
    _add_anchor(conn, 2, 3, "2026-07-02 12:00:00", 200)
    _add_anchor(conn, 2, 4, "2026-07-12 12:00:00", 100)

    conn.execute(
        "INSERT INTO outbound_requests "
        "(item_id, requested_quantity, reason, created_at, rolled_back) "
        "VALUES (1, 30, 'order#1', '2026-07-15 12:00:00', 0)"
    )
    conn.execute(
        "INSERT INTO outbound_requests "
        "(item_id, requested_quantity, reason, created_at, rolled_back) "
        "VALUES (2, 20, 'order#2', '2026-07-15 12:00:00', 0)"
    )

    result = warehouse.get_warehouse_inventory_turnover(conn, days=30, _now=now)
    assert result["warehouse_cogs_value"] == 400.0
    # 83.33 × 10 = 833.3, 166.67 × 5 = 833.35, sum = 1666.65
    assert result["warehouse_avg_inventory_value"] == 1666.65
    assert result["turnover_value"] == 0.24


def test_warehouse_turnover_skips_items_with_zero_unit_cost():
    """Items with unit_cost=0 can't contribute to value-based aggregation → skipped."""
    conn = _setup_turnover_env(qty=50, unit_cost=10.0)
    _add_item(conn, 2, 50, 0.0)  # zero unit_cost
    now = datetime.datetime(2026, 7, 22, 12, 0, 0)

    _add_anchor(conn, 1, 1, "2026-07-02 12:00:00", 100)
    _add_anchor(conn, 1, 2, "2026-07-12 12:00:00", 50)
    _add_anchor(conn, 2, 3, "2026-07-02 12:00:00", 200)
    _add_anchor(conn, 2, 4, "2026-07-12 12:00:00", 100)

    result = warehouse.get_warehouse_inventory_turnover(conn, days=30, _now=now)
    assert result["items_total"] == 2
    assert result["items_with_turnover"] == 1


def test_warehouse_turnover_no_anchors_anywhere_returns_none():
    """If no item has anchors, turnover is None with data_quality='none'."""
    conn = _setup_turnover_env()
    _add_item(conn, 2, 100, 5.0)
    now = datetime.datetime(2026, 7, 22, 12, 0, 0)

    result = warehouse.get_warehouse_inventory_turnover(conn, days=30, _now=now)
    assert result["turnover_value"] is None
    assert result["items_with_turnover"] == 0
    assert result["data_quality"] == "none"


def _setup_consumption_summary_env() -> sqlite3.Connection:
    """Schema for list_consumption_summary: items + categories + outbound + production."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_categories_table(conn)
    create_items_table(conn)
    create_outbound_requests_table(conn)
    create_restock_requests_table(conn)
    create_production_runs_table(conn)
    create_production_run_items_table(conn)
    create_stocktake_batches_table(conn)
    create_stocktakes_table(conn)
    conn.execute("INSERT INTO categories (id, name) VALUES (1, '调味酱')")
    conn.execute(
        "INSERT INTO items (id, sku, name, category_id, quantity, unit, unit_cost) "
        "VALUES (1, 'SKU1', 'Item A', 1, 50, 'pcs', 10.0)"
    )
    conn.execute(
        "INSERT INTO items (id, sku, name, category_id, quantity, unit, unit_cost) "
        "VALUES (2, 'SKU2', 'Item B', 1, 100, 'pcs', 5.0)"
    )
    return conn


def test_list_consumption_summary_returns_dict_with_items_and_warehouse_turnover():
    """The new return shape is {items: [...], warehouse_turnover: {...}}."""
    conn = _setup_consumption_summary_env()

    result = warehouse.list_consumption_summary(conn, days=7, sort_by="qty", limit=10)
    assert isinstance(result, dict)
    assert "items" in result
    assert "warehouse_turnover" in result
    assert isinstance(result["items"], list)
    assert len(result["items"]) == 2
    first = result["items"][0]
    for key in ("rank", "item_id", "sku", "name", "category_name", "unit",
                "current_stock", "safety_stock", "consume_qty",
                "active_days", "daily_avg", "turnover_rate",
                "consume_pct", "first_date", "last_date"):
        assert key in first, f"missing key {key} in item"
    wt = result["warehouse_turnover"]
    for key in ("window_days", "warehouse_cogs_value",
                "warehouse_avg_inventory_value", "turnover_value",
                "items_with_turnover", "items_total", "data_quality",
                "method"):
        assert key in wt, f"missing key {key} in warehouse_turnover"
    assert wt["window_days"] == 30
    assert wt["method"] == "stocktake_weighted_sum"
