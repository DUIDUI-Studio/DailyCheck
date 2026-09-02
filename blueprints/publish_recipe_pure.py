"""Cross-warehouse recipe publishing: snapshot + apply.

Lifecycle:
  draft → published → superseded
- Every save of a recipe in the source warehouse calls
  `snapshot_recipe()` + `upsert_draft_version()`. If there's already
  a draft version, it's overwritten in place (so the version number
  stays stable while the admin edits). If the latest version is
  published, a new draft is started with version+1.
- Publishing calls `publish_version(version_id, warehouse_codes,
  user_id, summary)`. It marks the version as 'published',
  inserts a recipe_publish_events row, and per target warehouse
  inserts/updates the recipe + BOM + referenced items.
- A subsequent save starts a new draft on top of the latest
  version+1, leaving the published version immutable. The
  previously-published version transitions to 'superseded'.

The snapshot is JSON-serialisable so it can be replayed into any
warehouse db without touching the source warehouse.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

RECIPE_TYPES = ("ic_recipe", "recipe")


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _serialise(obj: Any) -> Any:
    """JSON can't handle Decimal / datetime — normalise to primitives."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, datetime):
        return obj.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(obj, dict):
        return {k: _serialise(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialise(v) for v in obj]
    return obj


def snapshot_recipe(conn, recipe_type: str, recipe_id: int) -> dict:
    """Capture current state of a recipe (in its warehouse db) as a dict.

    Includes the BOM lines with item details (name, unit, gram_per_unit,
    unit_cost, selling_price) so the snapshot is self-contained for
    cross-warehouse replay.
    """
    if recipe_type not in RECIPE_TYPES:
        raise ValueError(f"unknown recipe_type: {recipe_type!r}")
    conn.row_factory = sqlite3.Row

    table = "ic_recipes" if recipe_type == "ic_recipe" else "recipes"
    bom_table = "ic_recipe_items" if recipe_type == "ic_recipe" else "recipe_items"
    bom_id_col = "ic_recipe_id" if recipe_type == "ic_recipe" else "recipe_id"

    head = conn.execute(
        f"SELECT * FROM {table} WHERE id = ?", (recipe_id,)
    ).fetchone()
    if head is None:
        return None

    lines = conn.execute(
        f"""
        SELECT t.id AS line_id, t.qty_per_unit,
               t.item_id, t.ic_recipe_id,
               i.sku, i.name AS item_name, i.unit AS item_unit,
               i.gram_per_unit, i.unit_cost, i.selling_price,
               i.category_id, c.name AS category_name
        FROM {bom_table} t
        LEFT JOIN items i ON i.id = t.item_id
        LEFT JOIN categories c ON c.id = i.category_id
        WHERE t.{bom_id_col} = ?
        ORDER BY t.id
        """,
        (recipe_id,),
    ).fetchall()

    return _serialise({
        "recipe_type": recipe_type,
        "recipe_id": recipe_id,
        "head": dict(head),
        "lines": [dict(r) for r in lines],
        # Only meaningful for polymorphic 'recipe' type; 'ic_recipe'
        # always uses source_type='item' implicitly.
        "has_polymorphic_lines": any("source_type" in r.keys() for r in lines),
    })


def _next_version(conn, recipe_type: str, recipe_id: int) -> int:
    """Version+1, or 1 if no versions yet."""
    row = conn.execute(
        "SELECT MAX(version) AS v FROM recipe_versions "
        "WHERE recipe_type = ? AND recipe_id = ?",
        (recipe_type, recipe_id),
    ).fetchone()
    return int(row["v"] or 0) + 1


def _latest_version_row(conn, recipe_type: str, recipe_id: int):
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM recipe_versions "
        "WHERE recipe_type = ? AND recipe_id = ? "
        "ORDER BY version DESC LIMIT 1",
        (recipe_type, recipe_id),
    ).fetchone()


def upsert_draft_version(
    master_conn,
    recipe_type: str,
    recipe_id: int,
    source_warehouse_code: str,
    snapshot: dict,
    user_id: int | None,
    notes: str | None = None,
) -> int:
    """Create or update the current draft version. Returns version id.

    Rules:
    - If the latest version is 'draft', overwrite its snapshot_json,
      notes, updated_at (== created_at for new row). Version stays.
    - If the latest version is 'published' or 'superseded', insert a
      new draft with version = latest + 1.
    """
    master_conn.row_factory = sqlite3.Row
    latest = _latest_version_row(master_conn, recipe_type, recipe_id)
    snap_json = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)

    if latest is None:
        cur = master_conn.execute(
            """INSERT INTO recipe_versions
               (recipe_type, recipe_id, source_warehouse_code, version,
                status, snapshot_json, notes, created_by, created_at)
               VALUES (?, ?, ?, 1, 'draft', ?, ?, ?, ?)""",
            (recipe_type, recipe_id, source_warehouse_code, snap_json,
             notes, user_id, now_str()),
        )
        return int(cur.lastrowid)

    if latest["status"] == "draft":
        master_conn.execute(
            """UPDATE recipe_versions
               SET snapshot_json = ?, notes = ?, created_by = ?, created_at = ?
               WHERE id = ?""",
            (snap_json, notes, user_id, now_str(), latest["id"]),
        )
        return int(latest["id"])

    # Latest is published/superseded — start a new draft on top.
    new_version = int(latest["version"]) + 1
    cur = master_conn.execute(
        """INSERT INTO recipe_versions
           (recipe_type, recipe_id, source_warehouse_code, version,
            status, snapshot_json, notes, created_by, created_at)
           VALUES (?, ?, ?, ?, 'draft', ?, ?, ?, ?)""",
        (recipe_type, recipe_id, source_warehouse_code, new_version,
         snap_json, notes, user_id, now_str()),
    )
    return int(cur.lastrowid)


def list_versions(master_conn, recipe_type: str, recipe_id: int) -> list[dict]:
    master_conn.row_factory = sqlite3.Row
    rows = master_conn.execute(
        "SELECT * FROM recipe_versions "
        "WHERE recipe_type = ? AND recipe_id = ? "
        "ORDER BY version DESC",
        (recipe_type, recipe_id),
    ).fetchall()
    return [dict(r) for r in rows]


def get_published_version_for_recipe(master_conn, recipe_type: str, recipe_id: int):
    """Latest 'published' version, or None."""
    master_conn.row_factory = sqlite3.Row
    return master_conn.execute(
        "SELECT * FROM recipe_versions "
        "WHERE recipe_type = ? AND recipe_id = ? AND status = 'published' "
        "ORDER BY version DESC LIMIT 1",
        (recipe_type, recipe_id),
    ).fetchone()


def publish_version(
    master_conn,
    version_id: int,
    warehouse_codes: list[str],
    user_id: int | None,
    summary: str | None,
    apply_func,
) -> dict:
    """Mark a draft version as published and fan out to warehouses.

    `apply_func(master_conn, target_conn, target_warehouse_code, snapshot)`
    is supplied by the route layer — it opens the per-warehouse sqlite
    connection, copies the snapshot in, and returns True on success
    or False + error_message on failure. We don't import db helpers
    here so this module stays testable without a full Flask stack.

    Returns:
        {"event_id": int, "version_id": int,
         "per_warehouse": [{"warehouse_code": str, "status": str,
                           "error_message": str|None}, ...],
         "status": "complete" | "partial" | "failed"}
    """
    master_conn.row_factory = sqlite3.Row
    version = master_conn.execute(
        "SELECT * FROM recipe_versions WHERE id = ?", (version_id,)
    ).fetchone()
    if version is None:
        raise ValueError(f"version_id {version_id} not found")
    if version["status"] != "draft":
        raise ValueError(
            f"version {version_id} is {version['status']!r}, only drafts can be published"
        )

    snapshot = json.loads(version["snapshot_json"])
    target_codes_json = json.dumps(warehouse_codes, ensure_ascii=False)

    cur = master_conn.execute(
        """INSERT INTO recipe_publish_events
           (recipe_version_id, summary, status, started_by, started_at,
            target_warehouse_codes_json)
           VALUES (?, ?, 'pending', ?, ?, ?)""",
        (version_id, summary, user_id, now_str(), target_codes_json),
    )
    event_id = int(cur.lastrowid)

    per_warehouse: list[dict] = []
    any_failed = False
    for code in warehouse_codes:
        try:
            apply_func(master_conn, code, snapshot)
            master_conn.execute(
                """INSERT INTO recipe_publish_event_warehouses
                   (publish_event_id, warehouse_code, status, applied_at)
                   VALUES (?, ?, 'success', ?)""",
                (event_id, code, now_str()),
            )
            per_warehouse.append({"warehouse_code": code, "status": "success",
                                  "error_message": None})
        except Exception as exc:  # noqa: BLE001 — caller wants per-row status
            master_conn.execute(
                """INSERT INTO recipe_publish_event_warehouses
                   (publish_event_id, warehouse_code, status, error_message, applied_at)
                   VALUES (?, ?, 'failed', ?, ?)""",
                (event_id, code, str(exc), now_str()),
            )
            per_warehouse.append({"warehouse_code": code, "status": "failed",
                                  "error_message": str(exc)})
            any_failed = True

    overall = "complete" if not any_failed else "partial"
    if not per_warehouse:
        overall = "failed"

    master_conn.execute(
        "UPDATE recipe_publish_events SET status=?, completed_at=? WHERE id=?",
        (overall, now_str(), event_id),
    )
    # Mark version as published (no longer draft).
    master_conn.execute(
        "UPDATE recipe_versions SET status='published', published_at=? WHERE id=?",
        (now_str(), version_id),
    )
    # Mark prior published versions of the same recipe as superseded.
    master_conn.execute(
        """UPDATE recipe_versions SET status='superseded'
           WHERE recipe_type=? AND recipe_id=? AND status='published' AND id != ?""",
        (version["recipe_type"], version["recipe_id"], version_id),
    )

    return {
        "event_id": event_id,
        "version_id": version_id,
        "per_warehouse": per_warehouse,
        "status": overall,
    }


def list_publish_events_for_recipe(
    master_conn, recipe_type: str, recipe_id: int
) -> list[dict]:
    """All publish events that touched this recipe, newest first."""
    master_conn.row_factory = sqlite3.Row
    rows = master_conn.execute(
        """SELECT pe.* FROM recipe_publish_events pe
           JOIN recipe_versions v ON v.id = pe.recipe_version_id
           WHERE v.recipe_type = ? AND v.recipe_id = ?
           ORDER BY pe.started_at DESC""",
        (recipe_type, recipe_id),
    ).fetchall()
    return [dict(r) for r in rows]


def get_event_warehouses(master_conn, event_id: int) -> list[dict]:
    master_conn.row_factory = sqlite3.Row
    rows = master_conn.execute(
        "SELECT * FROM recipe_publish_event_warehouses "
        "WHERE publish_event_id = ? ORDER BY warehouse_code",
        (event_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Item / category cross-warehouse sync (used by publish blueprint + tests)
# ---------------------------------------------------------------------------

ITEM_DEFAULT_ACTION = "overwrite"  # default if not specified


def snapshot_item(conn, item_id: int) -> dict:
    """Capture a single item (in its warehouse db) with category info."""
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """SELECT i.*, c.name AS category_name
           FROM items i JOIN categories c ON c.id = i.category_id
           WHERE i.id = ?""",
        (item_id,),
    ).fetchone()
    return _serialise(dict(row)) if row else None


def apply_item_to_warehouse(
    target_conn,
    source_snapshot: dict,
    action: str = ITEM_DEFAULT_ACTION,
) -> str:
    """Apply an item snapshot to a target warehouse db. Returns final action.

    `action`:
    - 'overwrite': UPDATE if item exists (matched by sku), else INSERT.
                   Always sets name/category/unit/gram_per_unit/unit_cost/selling_price.
    - 'keep':      if item exists by sku, do nothing. else INSERT.
    - 'merge':     UPDATE non-null snapshot fields on existing item,
                   else INSERT.
    Category is auto-created on first use if missing.
    """
    target_conn.row_factory = sqlite3.Row

    # 1. Ensure category exists.
    cat_name = source_snapshot["category_name"]
    row = target_conn.execute(
        "SELECT id FROM categories WHERE name = ?", (cat_name,)
    ).fetchone()
    if row is None:
        target_conn.execute(
            "INSERT INTO categories (name, description, created_at) "
            "VALUES (?, '系统固定品类', ?)",
            (cat_name, now_str()),
        )
        cat_id = int(target_conn.execute(
            "SELECT id FROM categories WHERE name = ?", (cat_name,)
        ).fetchone()["id"])
    else:
        cat_id = int(row["id"])

    sku = source_snapshot["sku"]
    existing = target_conn.execute(
        "SELECT id FROM items WHERE sku = ?", (sku,)
    ).fetchone()

    if existing is None:
        target_conn.execute(
            """INSERT INTO items
               (sku, name, category_id, quantity, safety_stock,
                unit, unit_cost, gram_per_unit, aux_unit, aux_rate,
                selling_price, updated_at)
               VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                sku, source_snapshot["name"], cat_id,
                float(source_snapshot["safety_stock"] or 0),
                source_snapshot["unit"], float(source_snapshot["unit_cost"] or 0),
                float(source_snapshot["gram_per_unit"] or 0),
                source_snapshot["aux_unit"], float(source_snapshot["aux_rate"] or 0),
                float(source_snapshot["selling_price"] or 0),
                now_str(),
            ),
        )
        return "inserted"

    if action == "keep":
        return "kept"

    if action == "overwrite":
        target_conn.execute(
            """UPDATE items SET
               name=?, category_id=?, safety_stock=?, unit=?,
               unit_cost=?, gram_per_unit=?, aux_unit=?, aux_rate=?,
               selling_price=?, updated_at=?
               WHERE sku=?""",
            (
                source_snapshot["name"], cat_id,
                float(source_snapshot["safety_stock"] or 0),
                source_snapshot["unit"], float(source_snapshot["unit_cost"] or 0),
                float(source_snapshot["gram_per_unit"] or 0),
                source_snapshot["aux_unit"], float(source_snapshot["aux_rate"] or 0),
                float(source_snapshot["selling_price"] or 0),
                now_str(), sku,
            ),
        )
        return "overwritten"

    # merge: only fill fields that are non-null/zero in target but set in source.
    if action == "merge":
        cur = target_conn.execute(
            "SELECT * FROM items WHERE sku=?", (sku,)
        ).fetchone()
        updates = {}
        if not cur["unit_cost"]:
            updates["unit_cost"] = float(source_snapshot["unit_cost"] or 0)
        if not cur["selling_price"]:
            updates["selling_price"] = float(source_snapshot["selling_price"] or 0)
        if not cur["gram_per_unit"]:
            updates["gram_per_unit"] = float(source_snapshot["gram_per_unit"] or 0)
        if not cur["aux_unit"]:
            updates["aux_unit"] = source_snapshot["aux_unit"]
        if not cur["aux_rate"]:
            updates["aux_rate"] = float(source_snapshot["aux_rate"] or 0)
        updates["updated_at"] = now_str()
        if updates:
            set_clause = ", ".join(f"{k}=?" for k in updates)
            target_conn.execute(
                f"UPDATE items SET {set_clause} WHERE sku=?",
                (*updates.values(), sku),
            )
        return "merged"

    raise ValueError(f"unknown action: {action!r}")


def publish_items(
    master_conn,
    source_conn,
    source_warehouse_code: str,
    item_ids: list[int],
    warehouse_codes: list[str],
    user_id: int | None,
    summary: str | None,
    default_action: str = ITEM_DEFAULT_ACTION,
) -> dict:
    """Publish items from a source warehouse to target warehouses.

    Returns event summary dict.
    """
    master_conn.row_factory = sqlite3.Row

    target_codes_json = json.dumps(warehouse_codes, ensure_ascii=False)
    cur = master_conn.execute(
        """INSERT INTO item_publish_events
           (summary, status, started_by, started_at,
            target_warehouse_codes_json, item_count)
           VALUES (?, 'pending', ?, ?, ?, ?)""",
        (summary, user_id, now_str(), target_codes_json, len(item_ids)),
    )
    event_id = int(cur.lastrowid)

    # Snapshot each source item.
    snapshots = []
    for item_id in item_ids:
        snap = snapshot_item(source_conn, item_id)
        if snap is None:
            master_conn.execute(
                """INSERT INTO item_publish_event_items
                   (publish_event_id, item_id, source_warehouse_code,
                    target_warehouse_code, status, error_message, action)
                   VALUES (?, ?, ?, '', 'failed', ?, ?)""",
                (event_id, item_id, source_warehouse_code,
                 f"item {item_id} not found in source", default_action),
            )
            continue
        snapshots.append((item_id, snap))

    any_failed = False

    # Per warehouse, per item: apply.
    for wh_code in warehouse_codes:
        target_path = _resolve_warehouse_db_path(master_conn, wh_code)
        if target_path is None or not Path(target_path).exists():
            master_conn.execute(
                """INSERT INTO item_publish_event_warehouses
                   (publish_event_id, warehouse_code, status, error_message, applied_at)
                   VALUES (?, ?, 'failed', ?, ?)""",
                (event_id, wh_code, f"warehouse db not found at {target_path}",
                 now_str()),
            )
            any_failed = True
            continue

        wh_failed = False
        with sqlite3.connect(target_path) as tc:
            tc.execute("PRAGMA foreign_keys = ON")
            for item_id, snap in snapshots:
                try:
                    action = apply_item_to_warehouse(tc, snap, default_action)
                    master_conn.execute(
                        """INSERT INTO item_publish_event_items
                           (publish_event_id, item_id, source_warehouse_code,
                            target_warehouse_code, status, action)
                           VALUES (?, ?, ?, ?, 'success', ?)""",
                        (event_id, item_id, source_warehouse_code,
                         wh_code, action),
                    )
                except Exception as exc:  # noqa: BLE001
                    master_conn.execute(
                        """INSERT INTO item_publish_event_items
                           (publish_event_id, item_id, source_warehouse_code,
                            target_warehouse_code, status, error_message, action)
                           VALUES (?, ?, ?, ?, 'failed', ?, ?)""",
                        (event_id, item_id, source_warehouse_code,
                         wh_code, str(exc), default_action),
                    )
                    wh_failed = True

        master_conn.execute(
            """INSERT INTO item_publish_event_warehouses
               (publish_event_id, warehouse_code, status, error_message, applied_at)
               VALUES (?, ?, ?, NULL, ?)""",
            (event_id, wh_code, "failed" if wh_failed else "success",
             now_str()),
        )
        if wh_failed:
            any_failed = True

    overall = "complete" if not any_failed else "partial"
    if not snapshots or not warehouse_codes:
        overall = "failed"

    master_conn.execute(
        "UPDATE item_publish_events SET status=?, completed_at=? WHERE id=?",
        (overall, now_str(), event_id),
    )
    return {"event_id": event_id, "status": overall,
            "item_count": len(snapshots), "warehouse_count": len(warehouse_codes)}


def _resolve_warehouse_db_path(master_conn, warehouse_code: str) -> str | None:
    """Resolve a warehouse db path from master.db.

    master.db stores paths like 'db/warehouses/wh_001.db' (relative to
    the repo, BASE_DIR). Try resolving against:
      1. BASE_DIR + raw  (production: e.g. /opt/dailycheck/db/warehouses/wh_001.db)
      2. cwd + raw        (tests where path is relative to test cwd)
      3. absolute path    (already absolute → return as-is)

    Returns the first candidate that exists on disk. If none match,
    returns the BASE_DIR-relative one (so the FileNotFoundError message
    points at the production-expected location).
    """
    from config import BASE_DIR
    master_conn.row_factory = sqlite3.Row
    row = master_conn.execute(
        "SELECT db_path FROM warehouses WHERE code = ?", (warehouse_code,)
    ).fetchone()
    if row is None:
        return None
    raw = row["db_path"]
    if Path(raw).is_absolute() and Path(raw).exists():
        return raw
    for base in (Path(BASE_DIR), Path.cwd()):
        cand = base / raw
        if cand.exists():
            return str(cand)
    return str(Path(BASE_DIR) / raw)


def list_item_publish_events(master_conn, limit: int = 50) -> list[dict]:
    master_conn.row_factory = sqlite3.Row
    rows = master_conn.execute(
        "SELECT * FROM item_publish_events ORDER BY started_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_item_event_details(master_conn, event_id: int) -> dict:
    """Combined view of an item_publish_event + per-warehouse + per-item rows."""
    master_conn.row_factory = sqlite3.Row
    event = master_conn.execute(
        "SELECT * FROM item_publish_events WHERE id = ?", (event_id,)
    ).fetchone()
    if event is None:
        return None
    warehouses = master_conn.execute(
        "SELECT * FROM item_publish_event_warehouses WHERE publish_event_id = ?",
        (event_id,),
    ).fetchall()
    items = master_conn.execute(
        "SELECT * FROM item_publish_event_items WHERE publish_event_id = ?",
        (event_id,),
    ).fetchall()
    return {
        "event": dict(event),
        "warehouses": [dict(r) for r in warehouses],
        "items": [dict(r) for r in items],
    }
