"""Master v7 compatibility floor for order changes; no warehouse DDL or commits."""
from __future__ import annotations

import sqlite3

ORDER_COLUMNS = {
    "change_revision": "INTEGER NOT NULL DEFAULT 0 CHECK(change_revision >= 0)",
    "shipping_rule_snapshot": "TEXT CHECK(shipping_rule_snapshot IS NULL OR json_valid(shipping_rule_snapshot))",
}
ITEM_COLUMNS = {
    "original_quantity": "REAL",
    "line_revision": "INTEGER NOT NULL DEFAULT 0 CHECK(line_revision >= 0)",
    "unit_price_snapshot": "TEXT",
    "price_snapshot_origin": "TEXT CHECK(price_snapshot_origin IN ('selling_price','unit_cost','verified'))",
    "product_snapshot": "TEXT CHECK(product_snapshot IS NULL OR json_valid(product_snapshot))",
    "replaces_order_item_id": "INTEGER REFERENCES store_order_items(id)",
    "introduced_by_proposal_line_id": "INTEGER REFERENCES store_order_change_lines(id)",
}
DDL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_order_item_identity ON store_order_items(id, order_id)",
    """CREATE TABLE IF NOT EXISTS store_order_change_proposals (
        id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES store_orders(id),
        sequence INTEGER NOT NULL CHECK(sequence > 0),
        status TEXT NOT NULL CHECK(status IN ('pending','accepted','rejected','withdrawn','stale')),
        base_order_change_revision INTEGER NOT NULL CHECK(base_order_change_revision >= 0),
        proposed_by INTEGER NOT NULL REFERENCES users(id), reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
        snapshot_json TEXT NOT NULL CHECK(json_valid(snapshot_json)),
        required_warehouses_json TEXT NOT NULL CHECK(json_valid(required_warehouses_json)),
        created_at TEXT NOT NULL, decided_by INTEGER REFERENCES users(id), decided_at TEXT,
        decision_note TEXT, stale_reason TEXT,
        UNIQUE(order_id, sequence), UNIQUE(id, order_id)
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_order_change_pending ON store_order_change_proposals(order_id) WHERE status='pending'",
    """CREATE TABLE IF NOT EXISTS store_order_change_lines (
        id INTEGER PRIMARY KEY, proposal_id INTEGER NOT NULL, order_id INTEGER NOT NULL,
        order_item_id INTEGER NOT NULL,
        operation TEXT NOT NULL CHECK(operation IN ('cancel','reduce','replace')),
        base_line_revision INTEGER NOT NULL CHECK(base_line_revision >= 0),
        baseline_quantity REAL NOT NULL, baseline_shipped REAL NOT NULL,
        remove_quantity REAL NOT NULL CHECK(remove_quantity > 0 AND remove_quantity <= baseline_quantity-baseline_shipped),
        replacement_snapshot TEXT CHECK(replacement_snapshot IS NULL OR json_valid(replacement_snapshot)),
        UNIQUE(proposal_id, order_item_id),
        FOREIGN KEY(proposal_id, order_id) REFERENCES store_order_change_proposals(id, order_id),
        FOREIGN KEY(order_item_id, order_id) REFERENCES store_order_items(id, order_id),
        CHECK(baseline_shipped >= 0 AND baseline_quantity >= baseline_shipped),
        CHECK((operation='replace') = (replacement_snapshot IS NOT NULL))
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_order_change_introduced ON store_order_items(introduced_by_proposal_line_id) WHERE introduced_by_proposal_line_id IS NOT NULL",
    """CREATE TABLE IF NOT EXISTS store_order_change_commands (
        request_key TEXT PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES store_orders(id),
        proposal_id INTEGER REFERENCES store_order_change_proposals(id),
        actor_id INTEGER NOT NULL REFERENCES users(id),
        action TEXT NOT NULL CHECK(action IN ('create','accept','reject','withdraw')),
        payload_hash TEXT NOT NULL, result_json TEXT NOT NULL CHECK(json_valid(result_json)),
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS store_order_change_events (
        id INTEGER PRIMARY KEY, proposal_id INTEGER NOT NULL REFERENCES store_order_change_proposals(id),
        actor_id INTEGER NOT NULL REFERENCES users(id),
        from_status TEXT, to_status TEXT NOT NULL CHECK(to_status IN ('pending','accepted','rejected','withdrawn','stale')),
        note TEXT NOT NULL, created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS store_order_ship_claims (
        op_no TEXT PRIMARY KEY CHECK(length(op_no) > 0),
        order_id INTEGER NOT NULL REFERENCES store_orders(id),
        source_warehouse_code TEXT NOT NULL REFERENCES warehouses(code),
        payload_hash TEXT NOT NULL, payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
        state TEXT NOT NULL CHECK(state IN ('active','recovering','committed','aborted')),
        actor_id INTEGER NOT NULL REFERENCES users(id),
        delivery_id INTEGER REFERENCES store_order_deliveries(id),
        created_at TEXT NOT NULL, completed_at TEXT,
        UNIQUE(op_no, order_id), CHECK((state='committed') = (delivery_id IS NOT NULL))
    )""",
    """CREATE TABLE IF NOT EXISTS store_order_ship_claim_lines (
        op_no TEXT NOT NULL, order_id INTEGER NOT NULL, order_item_id INTEGER NOT NULL,
        quantity REAL NOT NULL CHECK(quantity > 0), base_line_revision INTEGER NOT NULL,
        baseline_quantity REAL NOT NULL, baseline_shipped REAL NOT NULL,
        canonical_id INTEGER NOT NULL REFERENCES canonical_items(id),
        dc_item_id INTEGER NOT NULL, unit TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('active','recovering','committed','aborted')),
        PRIMARY KEY(op_no, order_item_id),
        FOREIGN KEY(op_no, order_id) REFERENCES store_order_ship_claims(op_no, order_id),
        FOREIGN KEY(order_item_id, order_id) REFERENCES store_order_items(id, order_id)
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_order_ship_claim_occupied ON store_order_ship_claim_lines(order_item_id) WHERE state IN ('active','recovering')",
    """CREATE TRIGGER IF NOT EXISTS order_ship_claim_transition BEFORE UPDATE OF state ON store_order_ship_claims
       WHEN NOT (NEW.state=OLD.state OR (OLD.state='active' AND NEW.state IN ('recovering','committed')) OR
                 (OLD.state='recovering' AND NEW.state='aborted'))
       BEGIN SELECT RAISE(ABORT, 'invalid ship claim transition'); END""",
    """CREATE TRIGGER IF NOT EXISTS order_ship_claim_line_state AFTER UPDATE OF state ON store_order_ship_claims
       BEGIN UPDATE store_order_ship_claim_lines SET state=NEW.state WHERE op_no=NEW.op_no; END""",
    """CREATE TRIGGER IF NOT EXISTS order_item_original_insert AFTER INSERT ON store_order_items
       WHEN NEW.original_quantity IS NULL
       BEGIN UPDATE store_order_items SET original_quantity=NEW.quantity WHERE id=NEW.id; END""",
    """CREATE TRIGGER IF NOT EXISTS order_item_identity_immutable BEFORE UPDATE ON store_order_items
       WHEN NEW.original_quantity IS NOT OLD.original_quantity AND OLD.original_quantity IS NOT NULL
         OR NEW.canonical_id IS NOT OLD.canonical_id OR NEW.order_id IS NOT OLD.order_id
         OR NEW.source_warehouse_code IS NOT OLD.source_warehouse_code OR NEW.unit IS NOT OLD.unit
         OR (OLD.dc_item_id IS NOT NULL AND NEW.dc_item_id IS NOT OLD.dc_item_id)
         OR (OLD.product_snapshot IS NOT NULL AND NEW.product_snapshot IS NOT OLD.product_snapshot)
         OR (OLD.unit_price_snapshot IS NOT NULL AND NEW.unit_price_snapshot IS NOT OLD.unit_price_snapshot)
       BEGIN SELECT RAISE(ABORT, 'immutable order line snapshot'); END""",
    """CREATE TRIGGER IF NOT EXISTS order_item_quantity_claim_guard BEFORE UPDATE OF quantity, line_revision ON store_order_items
       WHEN EXISTS(SELECT 1 FROM store_order_ship_claim_lines WHERE order_item_id=OLD.id AND state IN ('active','recovering'))
       BEGIN SELECT RAISE(ABORT, 'order line occupied by ship claim'); END""",
    """CREATE TRIGGER IF NOT EXISTS order_change_line_claim_guard BEFORE INSERT ON store_order_change_lines
       WHEN EXISTS(SELECT 1 FROM store_order_ship_claim_lines WHERE order_item_id=NEW.order_item_id AND state IN ('active','recovering'))
       BEGIN SELECT RAISE(ABORT, 'order line occupied by ship claim'); END""",
)


def registered_up(conn: sqlite3.Connection) -> None:
    """One helper for init and runner, atomic even if called without a transaction."""
    conn.execute("SAVEPOINT order_change_schema")
    try:
        for table, additions in (("store_orders", ORDER_COLUMNS), ("store_order_items", ITEM_COLUMNS)):
            columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if not columns:
                continue
            for name, definition in additions.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        if conn.execute("PRAGMA table_info(store_order_items)").fetchone():
            # Quantity is known; historical price/rules/payment are deliberately unknown.
            conn.execute("UPDATE store_order_items SET original_quantity=quantity WHERE original_quantity IS NULL")
            for statement in DDL:
                conn.execute(statement)
        conn.execute("RELEASE order_change_schema")
    except Exception:
        conn.execute("ROLLBACK TO order_change_schema")
        conn.execute("RELEASE order_change_schema")
        raise


def registered_down(conn: sqlite3.Connection) -> None:
    raise RuntimeError("订单变更兼容底座不可破坏性down；保留proposal/claim/快照与活动占用，使用兼容代码回退")
