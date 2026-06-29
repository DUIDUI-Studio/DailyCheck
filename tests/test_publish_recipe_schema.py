"""Schema tests for subproject 5 (recipe publish).

Spec: docs/superpowers/specs/2026-06-29-recipe-publish-design.md §2.
PRD §2.5.5 data contract.

Two new master.db tables (recipe_publish_events, recipe_publish_event_warehouses)
plus two new warehouse.db tables (product_bom_versions, product_bom_store_versions)
plus a `products.current_version_id` column.
"""
from __future__ import annotations

import sqlite3


def test_master_db_has_recipe_publish_events_table(logged_client):
    """After fixture setup, recipe_publish_events must exist in master.db."""
    _, _ = logged_client
    import config as config_module
    conn = sqlite3.connect(config_module.MASTER_DB)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='recipe_publish_events'"
        ).fetchone()
        assert row is not None, "recipe_publish_events missing from master.db"
    finally:
        conn.close()


def test_master_db_has_recipe_publish_event_warehouses_table(logged_client):
    """Per-warehouse status table must exist in master.db."""
    _, _ = logged_client
    import config as config_module
    conn = sqlite3.connect(config_module.MASTER_DB)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='recipe_publish_event_warehouses'"
        ).fetchone()
        assert row is not None, "recipe_publish_event_warehouses missing from master.db"
    finally:
        conn.close()


def test_master_db_recipe_publish_events_columns(logged_client):
    """Columns per spec §2.1: id, product_id, bom_version_id, started_by,
    started_at, completed_at, summary, warehouse_codes_json."""
    _, _ = logged_client
    import config as config_module
    conn = sqlite3.connect(config_module.MASTER_DB)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(recipe_publish_events)").fetchall()}
        expected = {
            "id", "product_id", "bom_version_id", "started_by",
            "started_at", "completed_at", "summary", "warehouse_codes_json",
        }
        missing = expected - cols
        assert not missing, f"recipe_publish_events missing columns: {missing}"
    finally:
        conn.close()


def test_master_db_recipe_publish_event_warehouses_columns(logged_client):
    """Columns per spec §2.1: id, publish_event_id, warehouse_code, status, error_message."""
    _, _ = logged_client
    import config as config_module
    conn = sqlite3.connect(config_module.MASTER_DB)
    try:
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(recipe_publish_event_warehouses)"
        ).fetchall()}
        expected = {"id", "publish_event_id", "warehouse_code", "status", "error_message"}
        missing = expected - cols
        assert not missing, f"recipe_publish_event_warehouses missing columns: {missing}"
    finally:
        conn.close()


def test_warehouse_db_has_product_bom_versions_table(logged_client):
    _, wh_path = logged_client
    conn = sqlite3.connect(wh_path)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='product_bom_versions'"
        ).fetchone()
        assert row is not None, "product_bom_versions missing from warehouse.db"
    finally:
        conn.close()


def test_warehouse_db_has_product_bom_store_versions_table(logged_client):
    _, wh_path = logged_client
    conn = sqlite3.connect(wh_path)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='product_bom_store_versions'"
        ).fetchone()
        assert row is not None, "product_bom_store_versions missing from warehouse.db"
    finally:
        conn.close()


def test_warehouse_db_product_bom_versions_columns(logged_client):
    """Columns: id, product_id, version, bom_json, created_at. UNIQUE(product_id,version)."""
    _, wh_path = logged_client
    conn = sqlite3.connect(wh_path)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(product_bom_versions)").fetchall()}
        expected = {"id", "product_id", "version", "bom_json", "created_at"}
        missing = expected - cols
        assert not missing, f"product_bom_versions missing columns: {missing}"
    finally:
        conn.close()


def test_warehouse_db_product_bom_store_versions_columns(logged_client):
    """Columns: product_id, warehouse_code, bom_version_id, effective_at.
    PK (product_id, warehouse_code)."""
    _, wh_path = logged_client
    conn = sqlite3.connect(wh_path)
    try:
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(product_bom_store_versions)"
        ).fetchall()}
        expected = {"product_id", "warehouse_code", "bom_version_id", "effective_at"}
        missing = expected - cols
        assert not missing, f"product_bom_store_versions missing columns: {missing}"
    finally:
        conn.close()


def test_warehouse_db_products_has_current_version_id_column(logged_client):
    """products.current_version_id column must exist (spec §2.2)."""
    _, wh_path = logged_client
    conn = sqlite3.connect(wh_path)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(products)").fetchall()}
        assert "current_version_id" in cols, (
            f"products.current_version_id missing, columns are: {sorted(cols)}"
        )
    finally:
        conn.close()
