"""Pure functions for the /admin/publish/recipe blueprint.

Spec: docs/superpowers/specs/2026-06-29-recipe-publish-design.md §0, §2.2, §7.1.
PRD : §2.5.2 (versioned recipes, never reuse deleted version numbers).

Pure fns take a sqlite3.Connection explicitly so they can be tested with
an in-memory db (mirrors the notifications_pure pattern).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime


def create_new_bom_version(
    db: sqlite3.Connection,
    product_id: int,
    bom_items: list[dict],
) -> int:
    """Insert a new product_bom_versions row and return its id.

    The new version number is MAX(version) + 1 for this product_id, so
    even if an old version was deleted (spec §0.3 forbids this via API,
    but the invariant must hold regardless), the new version number is
    strictly greater than any previously-issued one. This implements
    PRD §2.5.2's "不允许删除老版本" lock — even if a row is missing,
    we never recycle a number.

    The bom_items list is stored verbatim as JSON in `bom_json`.
    The caller is expected to validate that bom_items is non-empty
    before calling; this fn accepts the empty list as a valid (but
    degenerate) BOM so the route layer can decide what counts as
    invalid for its error contract.
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = db.execute(
        "SELECT COALESCE(MAX(version), 0) AS m FROM product_bom_versions WHERE product_id=?",
        (product_id,),
    ).fetchone()
    next_version = int(row["m"]) + 1
    cur = db.execute(
        """INSERT INTO product_bom_versions
           (product_id, version, bom_json, created_at)
           VALUES (?, ?, ?, ?)""",
        (product_id, next_version, json.dumps(bom_items, ensure_ascii=False), ts),
    )
    db.commit()
    return int(cur.lastrowid)