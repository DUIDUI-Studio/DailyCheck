"""Shared fixture builder for multi-warehouse fulfillment Task 7 tests.

Two DCs (``dc_alpha`` / ``dc_beta``) and one storefront (``store_one``).
Assignment is explicit so catalog/submit expectations are readable:

===========  ==============  ===========  ==========  =============
canonical    name            routed to    DC price    notes
===========  ==============  ===========  ==========  =============
201          品项A           dc_alpha     9.9         beta mirror @99.0
202          品项B           dc_beta      1.5         multiple 5
203          品项C           dc_alpha     3.0         beta mirror @3.3
204          品项D           dc_alpha     4.0         store row exclusive
205          品项E           (none)       5.0         unrouted
206          品项F           dc_alpha     6.0
207          品项G           dc_beta      7.0
===========  ==============  ===========  ==========  =============
"""
from __future__ import annotations

import sqlite3

TS = "2026-10-29 12:00:00"

CANONICALS = (
    (201, "品项A"),
    (202, "品项B"),
    (203, "品项C"),
    (204, "品项D"),
    (205, "品项E"),
    (206, "品项F"),
    (207, "品项G"),
)

# (sku, canonical_id, price, order_multiple)
DC_ALPHA_ITEMS = (
    ("DCA-A", 201, 9.9, 1),
    ("DCA-C", 203, 3.0, 1),
    ("DCA-D", 204, 4.0, 1),
    ("DCA-E", 205, 5.0, 1),
    ("DCA-F", 206, 6.0, 1),
)
DC_BETA_ITEMS = (
    ("DCB-A", 201, 99.0, 1),  # mirror: routing must win over this price
    ("DCB-B", 202, 1.5, 5),
    ("DCB-C", 203, 3.3, 1),
    ("DCB-G", 207, 7.0, 1),
)
ROUTING = ((201, "dc_alpha"), (202, "dc_beta"), (203, "dc_alpha"),
           (204, "dc_alpha"), (206, "dc_alpha"), (207, "dc_beta"))


def enable(monkeypatch) -> None:
    """Flip the rollout flag for one test (raising=False keeps the red run honest)."""
    import config as config_module

    monkeypatch.setattr(config_module, "MULTI_WAREHOUSE_FULFILLMENT", True, raising=False)


def _insert_items(conn: sqlite3.Connection, rows) -> dict[int, int]:
    category_id = conn.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()[0]
    ids: dict[int, int] = {}
    for sku, canonical_id, price, multiple in rows:
        cur = conn.execute(
            "INSERT INTO items (sku, name, category_id, quantity, unit, canonical_id, "
            "selling_price, order_multiple, updated_at) VALUES (?, ?, ?, ?, '件', ?, ?, ?, ?)",
            (sku, sku, category_id, 100.0, canonical_id, price, multiple, TS),
        )
        ids[canonical_id] = int(cur.lastrowid)
    return ids


def build(tmp_path, monkeypatch) -> dict:
    import config as config_module
    import db as db_module

    master_path = tmp_path / "master.db"
    wh_dir = tmp_path / "warehouses"
    wh_dir.mkdir()
    paths = {
        "dc_alpha": wh_dir / "dc_alpha.db",
        "dc_beta": wh_dir / "dc_beta.db",
        "store_one": wh_dir / "store_one.db",
    }
    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_dir)
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", wh_dir)

    db_module.init_master_db()
    for path in paths.values():
        db_module.init_warehouse_db(path)

    master = sqlite3.connect(str(master_path))
    master.row_factory = sqlite3.Row
    for uid, name in ((1, "store_staff"), (2, "alpha_mgr"), (3, "beta_mgr")):
        master.execute(
            "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
            "VALUES (?, ?, 'x', 0, ?)", (uid, name, TS),
        )
    for wid, code, kind, path in (
        (1, "dc_alpha", "distribution_center", paths["dc_alpha"]),
        (2, "dc_beta", "distribution_center", paths["dc_beta"]),
        (3, "store_one", "storefront", paths["store_one"]),
    ):
        master.execute(
            "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)", (wid, code, code, str(path), kind, TS),
        )
    master.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (1, 3, 'staff')")
    master.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (2, 1, 'manager')")
    master.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (3, 2, 'manager')")
    master.execute(
        "INSERT INTO canonical_categories (code, name, description, created_at, updated_at) "
        "VALUES ('PACKAGING', '包材', '', ?, ?)", (TS, TS),
    )
    for cid, name in CANONICALS:
        master.execute(
            "INSERT INTO canonical_items (id, canonical_sku, name, category_code, unit, "
            "gram_per_unit, aux_unit, aux_rate, status, created_from, created_at, updated_at) "
            "VALUES (?, ?, ?, 'PACKAGING', '件', 0, NULL, 0, 'active', 'rd_manual', ?, ?)",
            (cid, f"IC-{cid:06d}", name, TS, TS),
        )
    for cid, dc_code in ROUTING:
        master.execute(
            "INSERT INTO item_routing (tenant_id, canonical_id, fulfillment_warehouse_code, "
            "updated_at, updated_by) VALUES (1, ?, ?, ?, NULL)", (cid, dc_code, TS),
        )
    master.commit()

    alpha_conn = sqlite3.connect(str(paths["dc_alpha"]))
    alpha_ids = _insert_items(alpha_conn, DC_ALPHA_ITEMS)
    alpha_conn.commit()
    alpha_conn.close()
    beta_conn = sqlite3.connect(str(paths["dc_beta"]))
    beta_ids = _insert_items(beta_conn, DC_BETA_ITEMS)
    beta_conn.commit()
    beta_conn.close()

    store = sqlite3.connect(str(paths["store_one"]))
    store.row_factory = sqlite3.Row
    category_id = store.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()[0]
    for cid, _ in CANONICALS:
        store.execute(
            "INSERT INTO items (sku, name, category_id, quantity, unit, canonical_id, "
            "is_store_exclusive, updated_at) VALUES (?, ?, ?, ?, '件', ?, ?, ?)",
            (f"ST-{cid}", f"门店{cid}", category_id, 7.0 if cid == 201 else 0.0,
             cid, 1 if cid == 204 else 0, TS),
        )
    store.commit()
    store.close()
    master.close()

    from app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = 1
        session["warehouse_id"] = 3

    return {
        "app": app,
        "client": client,
        "master_path": master_path,
        "paths": paths,
        "alpha_item_ids": alpha_ids,
        "beta_item_ids": beta_ids,
    }


def connect(path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn
