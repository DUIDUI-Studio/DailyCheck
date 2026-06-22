"""Clone a source warehouse's catalog into a destination warehouse DB.

Catalog = categories, items, products, product_bom. Stock quantities
are reset to zero (a new store has no inventory to inherit).
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from typing import Union
from pathlib import Path


def clone_warehouse_catalog(
    src_path: Union[str, Path],
    dst_path: Union[str, Path],
) -> dict:
    """Copy categories, items (qty=0), products, and product_bom
    from src_path into dst_path. Returns row counts copied.

    The caller is responsible for ensuring dst_path already has the
    warehouse schema (use init_warehouse_db).
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with closing(sqlite3.connect(str(src_path))) as src, \
         closing(sqlite3.connect(str(dst_path))) as dst:
        src.row_factory = sqlite3.Row

        # 1. categories — match by name, create missing ones in dst
        src_cats = src.execute("SELECT * FROM categories").fetchall()
        dst_cat_names = {r[0] for r in dst.execute("SELECT name FROM categories").fetchall()}
        cat_map: dict = {}
        for r in src_cats:
            if r["name"] in dst_cat_names:
                existing = dst.execute(
                    "SELECT id FROM categories WHERE name=?", (r["name"],)
                ).fetchone()
                cat_map[r["id"]] = existing[0]
            else:
                cur = dst.execute(
                    "INSERT INTO categories (name, description, created_at) VALUES (?, ?, ?)",
                    (r["name"], r["description"] or "", ts),
                )
                cat_map[r["id"]] = cur.lastrowid

        # 2. items — reset quantity to 0, remap category_id
        src_items = src.execute("SELECT * FROM items").fetchall()
        item_map: dict = {}
        for r in src_items:
            cur = dst.execute(
                """INSERT INTO items
                   (sku, name, category_id, quantity, safety_stock,
                    unit, unit_cost, gram_per_unit, updated_at)
                   VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?)""",
                (r["sku"], r["name"], cat_map[r["category_id"]],
                 r["safety_stock"], r["unit"], r["unit_cost"],
                 r["gram_per_unit"], ts),
            )
            item_map[r["id"]] = cur.lastrowid

        # 3. products
        src_prods = src.execute("SELECT * FROM products").fetchall()
        prod_map: dict = {}
        for r in src_prods:
            cur = dst.execute(
                "INSERT INTO products (name, unit, note, created_at) VALUES (?, ?, ?, ?)",
                (r["name"], r["unit"], r["note"], ts),
            )
            prod_map[r["id"]] = cur.lastrowid

        # 4. product_bom — remap both FKs
        src_bom = src.execute("SELECT * FROM product_bom").fetchall()
        bom_count = 0
        for r in src_bom:
            new_prod = prod_map.get(r["product_id"])
            new_item = item_map.get(r["item_id"])
            if new_prod is None or new_item is None:
                continue
            dst.execute(
                "INSERT INTO product_bom (product_id, item_id, qty_per_unit) VALUES (?, ?, ?)",
                (new_prod, new_item, r["qty_per_unit"]),
            )
            bom_count += 1

        dst.commit()

    return {
        "categories": len(src_cats),
        "items": len(src_items),
        "products": len(src_prods),
        "product_bom": bom_count,
    }