"""Tests for db.clone.clone_warehouse_catalog."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from db import init_warehouse_db
from db.clone import clone_warehouse_catalog


def _seed_minimal_warehouse(path: Path) -> None:
    """Create a warehouse DB with 1 cat, 2 items, 1 product, 1 BOM row."""
    init_warehouse_db(path)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(path) as conn:
        cat_id = conn.execute(
            "INSERT INTO categories (name, description, created_at) VALUES (?, ?, ?)",
            ("测试品类", "seeded", ts),
        ).lastrowid
        i1 = conn.execute(
            """INSERT INTO items (sku, name, category_id, quantity, safety_stock, unit, unit_cost, gram_per_unit, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("SKU001", "原料甲", cat_id, 5.0, 0.0, "罐", 10.0, 500.0, ts),
        ).lastrowid
        i2 = conn.execute(
            """INSERT INTO items (sku, name, category_id, quantity, safety_stock, unit, unit_cost, gram_per_unit, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("SKU002", "原料乙", cat_id, 3.0, 0.0, "罐", 20.0, 250.0, ts),
        ).lastrowid
        prod_id = conn.execute(
            "INSERT INTO products (name, unit, note, created_at) VALUES (?, ?, ?, ?)",
            ("测试产品", "件", "", ts),
        ).lastrowid
        conn.execute(
            "INSERT INTO product_bom (product_id, item_id, qty_per_unit) VALUES (?, ?, ?)",
            (prod_id, i1, 0.5),
        )
        conn.commit()


def test_clone_copies_all_tables(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    dst = tmp_path / "dst.db"
    init_warehouse_db(dst)  # dst must exist with schema first
    _seed_minimal_warehouse(src)

    # src has 9 FIXED_CATEGORIES + 1 seeded 测试品类 = 10; dst has 9 FIXED_CATEGORIES
    # After clone, dst's categories count = 10 (FIXED + 测试品类).
    with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
        src_cats = s.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
        src_items = s.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        src_products = s.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        src_bom = s.execute("SELECT COUNT(*) FROM product_bom").fetchone()[0]

    result = clone_warehouse_catalog(src, dst)

    assert result == {
        "categories": src_cats,
        "items": src_items,
        "products": src_products,
        "product_bom": src_bom,
    }
    with sqlite3.connect(dst) as conn:
        assert conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == src_cats
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == src_items
        assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == src_products
        assert conn.execute("SELECT COUNT(*) FROM product_bom").fetchone()[0] == src_bom


def test_clone_resets_item_quantity_to_zero(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    dst = tmp_path / "dst.db"
    init_warehouse_db(dst)
    _seed_minimal_warehouse(src)

    clone_warehouse_catalog(src, dst)

    with sqlite3.connect(dst) as conn:
        qtys = [r[0] for r in conn.execute("SELECT quantity FROM items").fetchall()]
    assert qtys == [0.0, 0.0]


def test_clone_remaps_foreign_keys(tmp_path: Path) -> None:
    """After clone, every FK must resolve to a row that exists in dst."""
    src = tmp_path / "src.db"
    dst = tmp_path / "dst.db"
    init_warehouse_db(dst)
    _seed_minimal_warehouse(src)

    clone_warehouse_catalog(src, dst)

    with sqlite3.connect(dst) as conn:
        orphans_items = conn.execute(
            """SELECT COUNT(*) FROM items i
               WHERE NOT EXISTS (SELECT 1 FROM categories c WHERE c.id = i.category_id)"""
        ).fetchone()[0]
        orphans_bom_prod = conn.execute(
            """SELECT COUNT(*) FROM product_bom b
               WHERE NOT EXISTS (SELECT 1 FROM products p WHERE p.id = b.product_id)"""
        ).fetchone()[0]
        orphans_bom_item = conn.execute(
            """SELECT COUNT(*) FROM product_bom b
               WHERE NOT EXISTS (SELECT 1 FROM items i WHERE i.id = b.item_id)"""
        ).fetchone()[0]
    assert orphans_items == 0
    assert orphans_bom_prod == 0
    assert orphans_bom_item == 0


def test_clone_preserves_item_fields_except_quantity(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    dst = tmp_path / "dst.db"
    init_warehouse_db(dst)
    _seed_minimal_warehouse(src)

    clone_warehouse_catalog(src, dst)

    with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
        s.row_factory = sqlite3.Row
        d.row_factory = sqlite3.Row
        src_item = s.execute("SELECT * FROM items WHERE sku='SKU001'").fetchone()
        dst_item = d.execute("SELECT * FROM items WHERE sku='SKU001'").fetchone()
    assert src_item["name"] == dst_item["name"]
    assert src_item["unit"] == dst_item["unit"]
    assert src_item["unit_cost"] == dst_item["unit_cost"]
    assert src_item["gram_per_unit"] == dst_item["gram_per_unit"]
    assert src_item["safety_stock"] == dst_item["safety_stock"]
    assert dst_item["quantity"] == 0.0  # reset


def test_clone_handles_categories_only_in_source(tmp_path: Path) -> None:
    """Categories present only in source must be created in dst (not just FIXED_CATEGORIES)."""
    src = tmp_path / "src.db"
    dst = tmp_path / "dst.db"
    init_warehouse_db(dst)  # dst has FIXED_CATEGORIES only
    _seed_minimal_warehouse(src)  # adds "测试品类"

    clone_warehouse_catalog(src, dst)

    with sqlite3.connect(dst) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM categories").fetchall()}
    assert "测试品类" in names