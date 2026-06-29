"""/admin/publish/items blueprint: admin-driven item publish from a
template version to one or more warehouses.

Spec: docs/superpowers/specs/2026-06-29-item-publish-design.md
PRD : §2.4 (item publish, item set not store set)

Routes:
  POST /admin/publish/items/preview
       body: {template_id, template_version, warehouse_codes}
       200  → per-warehouse diff (add/skip/conflict)
       400  → unknown warehouse
       404  → unknown template_version
  POST /admin/publish/items/confirm
       body: {template_id, template_version, warehouse_codes,
              resolutions: [{template_item_idx, warehouse_code, action}]}
       200  → {"publish_event_id": N}
       400  → missing_resolutions | unresolved_conflicts

Pure math lives in blueprints/publish_items_pure; this module is the
HTTP/DB glue only. Per spec §3, per-warehouse writes are wrapped in
try/except so one failure does not abort the whole batch.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime

from flask import Blueprint, abort, g, jsonify, request

import config
from db import get_master_db, init_master_db
from permissions import require_platform_admin
from .publish_items_pure import compute_publish_diff


bp = Blueprint("publish_items", __name__)


# Fields that get copied from a template item to a store items row on
# add/overwrite/merge (and that participate in diff comparison).
# Mirrors _DIFF_FIELDS in publish_items_pure.
_COPY_FIELDS = ("category", "unit", "unit_cost", "gram_per_unit", "safety_stock")


# ---------------------------------------------------------------------------
# Helpers — all master.db reads go through config.MASTER_DB at call time
# so the conftest monkeypatch works (same pattern as forecast/procurement).
# ---------------------------------------------------------------------------


def _load_template_version(template_id: int, template_version: int) -> tuple[int, list[dict]] | None:
    """Return (template_version_id, parsed items_json) or None if missing."""
    init_master_db()
    with closing(sqlite3.connect(config.MASTER_DB)) as m:
        m.row_factory = sqlite3.Row
        row = m.execute(
            "SELECT id, items_json FROM template_versions "
            "WHERE template_id=? AND version=?",
            (template_id, template_version),
        ).fetchone()
    if row is None:
        return None
    return row["id"], json.loads(row["items_json"])


def _resolve_warehouses(codes: list[str]) -> list[dict]:
    """Look up warehouse rows by code; return the list (missing codes dropped)."""
    init_master_db()
    with closing(sqlite3.connect(config.MASTER_DB)) as m:
        m.row_factory = sqlite3.Row
        placeholders = ",".join("?" * len(codes)) if codes else "''"
        rows = m.execute(
            f"SELECT id, code, name, db_path FROM warehouses WHERE code IN ({placeholders})",
            codes,
        ).fetchall()
    return [dict(r) for r in rows]


def _existing_items(wh_db_path: str) -> list[dict]:
    """Read all items from one warehouse db, return [{id, name, ...fields}].

    Joins items → categories so the result has 'category' (name), matching
    the template_items shape used by compute_publish_diff.
    """
    with closing(sqlite3.connect(wh_db_path)) as w:
        w.row_factory = sqlite3.Row
        rows = w.execute(
            "SELECT i.id, i.name, c.name AS category, i.unit, "
            "       i.unit_cost, i.gram_per_unit, i.safety_stock "
            "FROM items i LEFT JOIN categories c ON c.id = i.category_id"
        ).fetchall()
    return [dict(r) for r in rows]


def _find_item_by_name(wh_db_path: str, name: str) -> dict | None:
    """Lookup a single store item by name. Includes the joined category name."""
    with closing(sqlite3.connect(wh_db_path)) as w:
        w.row_factory = sqlite3.Row
        row = w.execute(
            "SELECT i.id, i.name, c.name AS category, i.unit, "
            "       i.unit_cost, i.gram_per_unit, i.safety_stock "
            "FROM items i LEFT JOIN categories c ON c.id = i.category_id "
            "WHERE i.name=?",
            (name,),
        ).fetchone()
    return dict(row) if row else None


def _insert_item(wh_db_path: str, template_item: dict, publish_event_id: int) -> int:
    """INSERT a new items row using template fields. Return the new id."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with closing(sqlite3.connect(wh_db_path)) as w:
        w.row_factory = sqlite3.Row
        cat_name = template_item.get("category")
        cat_row = None
        if cat_name:
            cat_row = w.execute(
                "SELECT id FROM categories WHERE name=?", (cat_name,)
            ).fetchone()
        if cat_row is None:
            cat_row = w.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()
        if cat_row is None:
            raise RuntimeError("warehouse has no categories")
        cat_id = cat_row["id"]
        sku = f"P-{template_item['name']}-{publish_event_id}"
        cur = w.execute(
            "INSERT INTO items "
            "(sku, name, category_id, quantity, safety_stock, unit, unit_cost, "
            " gram_per_unit, updated_at, created_by_publish_event_id) "
            "VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?)",
            (
                sku,
                template_item["name"],
                cat_id,
                float(template_item.get("safety_stock", 0)),
                template_item.get("unit", "件"),
                float(template_item.get("unit_cost", 0)),
                float(template_item.get("gram_per_unit", 0)),
                ts,
                publish_event_id,
            ),
        )
        w.commit()
        return int(cur.lastrowid)


def _apply_overwrite(wh_db_path: str, item_id: int, template_item: dict, publish_event_id: int) -> None:
    """UPDATE existing item row from template fields; stamp created_by_publish_event_id."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Resolve category name → category_id. If the template's category
    # name doesn't exist in this warehouse, fall back to the first row
    # (consistent with the rest of the items blueprint, which also
    # defaults to LIMIT 1 when adding new items).
    cat_name = template_item.get("category")
    with closing(sqlite3.connect(wh_db_path)) as w:
        w.row_factory = sqlite3.Row
        cat_row = None
        if cat_name:
            cat_row = w.execute(
                "SELECT id FROM categories WHERE name=?", (cat_name,)
            ).fetchone()
        if cat_row is None:
            cat_row = w.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()
        cat_id = cat_row["id"] if cat_row else 1
        w.execute(
            "UPDATE items SET "
            "  category_id=?, "
            "  unit=?, unit_cost=?, gram_per_unit=?, safety_stock=?, "
            "  updated_at=?, created_by_publish_event_id=? "
            "WHERE id=?",
            (
                cat_id,
                template_item.get("unit", "件"),
                float(template_item.get("unit_cost", 0)),
                float(template_item.get("gram_per_unit", 0)),
                float(template_item.get("safety_stock", 0)),
                ts,
                publish_event_id,
                item_id,
            ),
        )
        w.commit()


def _apply_merge(wh_db_path: str, item_id: int, template_item: dict, publish_event_id: int,
                 store_row: dict, diff_fields: list[str]) -> None:
    """Merge: fill only the fields the store has at their default.

    "Fill-in" semantics (PRD §2.4.3): if a field differs AND the store
    value is the default (0 for numeric, missing/empty for string), take
    the template's value. Otherwise keep the store value.

    Stamps created_by_publish_event_id regardless.
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sets: list[str] = []
    params: list = []
    if "unit_cost" in diff_fields and float(store_row.get("unit_cost") or 0) == 0:
        sets.append("unit_cost=?"); params.append(float(template_item.get("unit_cost", 0)))
    if "gram_per_unit" in diff_fields and float(store_row.get("gram_per_unit") or 0) == 0:
        sets.append("gram_per_unit=?"); params.append(float(template_item.get("gram_per_unit", 0)))
    if "safety_stock" in diff_fields and float(store_row.get("safety_stock") or 0) == 0:
        sets.append("safety_stock=?"); params.append(float(template_item.get("safety_stock", 0)))
    if "unit" in diff_fields and not (store_row.get("unit") or "").strip():
        sets.append("unit=?"); params.append(template_item.get("unit", "件"))
    if "category" in diff_fields and not (store_row.get("category") or "").strip():
        cat_name = template_item.get("category")
        cat_id = None
        with closing(sqlite3.connect(wh_db_path)) as w:
            w.row_factory = sqlite3.Row
            row = w.execute(
                "SELECT id FROM categories WHERE name=?", (cat_name,)
            ).fetchone() if cat_name else None
            if row is not None:
                cat_id = row["id"]
            else:
                row = w.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()
                if row is not None:
                    cat_id = row["id"]
        if cat_id is not None:
            sets.append("category_id=?"); params.append(cat_id)
    sets.append("updated_at=?"); params.append(ts)
    sets.append("created_by_publish_event_id=?"); params.append(publish_event_id)
    params.append(item_id)
    with closing(sqlite3.connect(wh_db_path)) as w:
        w.execute(f"UPDATE items SET {', '.join(sets)} WHERE id=?", params)
        w.commit()


def _now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@bp.route("/admin/publish/items/preview", methods=["POST"])
@require_platform_admin
def preview_items():
    payload = request.get_json(silent=True) or {}
    template_id = payload.get("template_id")
    template_version = payload.get("template_version")
    warehouse_codes = payload.get("warehouse_codes") or []
    if not isinstance(template_id, int) or not isinstance(template_version, int):
        return jsonify({"error": "invalid_request"}), 400
    if not isinstance(warehouse_codes, list) or not warehouse_codes:
        return jsonify({"error": "warehouse_codes_required"}), 400

    tv = _load_template_version(template_id, template_version)
    if tv is None:
        return jsonify({"error": "template_version_not_found"}), 404
    _, template_items = tv

    known = {w["code"] for w in _resolve_warehouses(warehouse_codes)}
    missing = [c for c in warehouse_codes if c not in known]
    if missing:
        return jsonify({"error": "warehouse_not_found"}), 400

    out_warehouses: list[dict] = []
    for code in warehouse_codes:
        wh = next(w for w in _resolve_warehouses([code]))
        store_items = _existing_items(wh["db_path"])
        diff = compute_publish_diff(template_items, store_items)
        out_warehouses.append({
            "warehouse_code": code,
            "items": diff,
        })

    return jsonify({
        "template_id": template_id,
        "template_version": template_version,
        "warehouses": out_warehouses,
    })


@bp.route("/admin/publish/items/confirm", methods=["POST"])
@require_platform_admin
def confirm_items():
    payload = request.get_json(silent=True) or {}
    template_id = payload.get("template_id")
    template_version = payload.get("template_version")
    warehouse_codes = payload.get("warehouse_codes") or []
    resolutions = payload.get("resolutions")
    if not isinstance(template_id, int) or not isinstance(template_version, int):
        return jsonify({"error": "invalid_request"}), 400
    if not isinstance(warehouse_codes, list) or not warehouse_codes:
        return jsonify({"error": "warehouse_codes_required"}), 400
    if resolutions is None:
        return jsonify({"error": "missing_resolutions"}), 400
    if not isinstance(resolutions, list):
        return jsonify({"error": "invalid_request"}), 400
    # Empty list is also "missing resolutions" (PRD §3 exception table).
    if not resolutions:
        return jsonify({"error": "missing_resolutions"}), 400

    tv = _load_template_version(template_id, template_version)
    if tv is None:
        return jsonify({"error": "template_version_not_found"}), 404
    _, template_items = tv

    known = {w["code"] for w in _resolve_warehouses(warehouse_codes)}
    missing = [c for c in warehouse_codes if c not in known]
    if missing:
        return jsonify({"error": "warehouse_not_found"}), 400

    # Build a per-warehouse conflict map so we can flag unresolved_conflicts
    # before any writes happen.
    res_by_wh: dict[str, dict[int, str]] = {}
    for r in resolutions:
        if not isinstance(r, dict):
            return jsonify({"error": "invalid_request"}), 400
        idx = r.get("template_item_idx")
        code = r.get("warehouse_code")
        action = r.get("action")
        if not isinstance(idx, int) or not isinstance(code, str) or action not in (
            "keep_store", "overwrite", "merge", "add",
        ):
            return jsonify({"error": "invalid_request"}), 400
        res_by_wh.setdefault(code, {})[idx] = action

    for code in warehouse_codes:
        wh = next(w for w in _resolve_warehouses([code]))
        diff = compute_publish_diff(template_items, _existing_items(wh["db_path"]))
        for row in diff:
            if row["status"] == "conflict":
                wh_res = res_by_wh.get(code, {})
                if row["template_item_idx"] not in wh_res:
                    return jsonify({"error": "unresolved_conflicts"}), 400
                if wh_res[row["template_item_idx"]] not in ("keep_store", "overwrite", "merge"):
                    return jsonify({"error": "unresolved_conflicts"}), 400

    # Write the publish_events row first so we can attach the id to item rows.
    now = _now()
    init_master_db()
    with closing(sqlite3.connect(config.MASTER_DB)) as m:
        cur = m.execute(
            "INSERT INTO publish_events "
            "(template_id, template_version, started_by, started_at, "
            " completed_at, warehouse_codes_json, resolutions_json) "
            "VALUES (?, ?, ?, ?, NULL, ?, ?)",
            (
                template_id,
                template_version,
                g.user["id"] if g.user else None,
                now,
                json.dumps(warehouse_codes, ensure_ascii=False),
                json.dumps(resolutions, ensure_ascii=False),
            ),
        )
        publish_event_id = int(cur.lastrowid)
        m.commit()

    # Per-warehouse apply, wrapped in try/except — partial success is OK.
    rows_to_insert: list[tuple] = []
    completed_at = _now()
    any_failed = False
    for code in warehouse_codes:
        wh = next(w for w in _resolve_warehouses([code]))
        wh_res = res_by_wh.get(code, {})
        try:
            for idx, action in wh_res.items():
                t_item = template_items[idx]
                if action == "keep_store":
                    # No item write. Still log a publish_event_items row.
                    rows_to_insert.append((
                        publish_event_id, idx, code, None, action, "success", None,
                    ))
                    continue
                if action == "add":
                    new_id = _insert_item(wh["db_path"], t_item, publish_event_id)
                    rows_to_insert.append((
                        publish_event_id, idx, code, new_id, action, "success", None,
                    ))
                    continue
                # overwrite / merge → need existing row.
                existing = _find_item_by_name(wh["db_path"], t_item["name"])
                if existing is None:
                    # Should not happen given the conflict check above, but
                    # if a concurrent delete happened between preview and
                    # confirm, fall back to add.
                    new_id = _insert_item(wh["db_path"], t_item, publish_event_id)
                    rows_to_insert.append((
                        publish_event_id, idx, code, new_id, "add", "success",
                        "fallback_add_after_concurrent_delete",
                    ))
                    continue
                if action == "overwrite":
                    _apply_overwrite(wh["db_path"], existing["id"], t_item, publish_event_id)
                    rows_to_insert.append((
                        publish_event_id, idx, code, existing["id"], action, "success", None,
                    ))
                elif action == "merge":
                    # Compute diff_fields once more so we only UPDATE what
                    # actually differs.
                    diff_now = compute_publish_diff([t_item], [existing])
                    if diff_now and diff_now[0]["status"] == "conflict":
                        _apply_merge(wh["db_path"], existing["id"], t_item, publish_event_id,
                                     existing, diff_now[0]["diff_fields"])
                    else:
                        # Nothing to merge — still stamp the back-reference.
                        _apply_merge(wh["db_path"], existing["id"], t_item, publish_event_id, existing, [])
                    rows_to_insert.append((
                        publish_event_id, idx, code, existing["id"], action, "success", None,
                    ))
        except Exception as exc:  # noqa: BLE001 — per-warehouse partial failure path
            any_failed = True
            rows_to_insert.append((
                publish_event_id, -1, code, None, "n/a", "failed", str(exc),
            ))

    if rows_to_insert:
        with closing(sqlite3.connect(config.MASTER_DB)) as m:
            m.executemany(
                "INSERT INTO publish_event_items "
                "(publish_event_id, template_item_idx, warehouse_code, item_id, "
                " action, status, error_message) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows_to_insert,
            )
            m.execute(
                "UPDATE publish_events SET completed_at=? WHERE id=?",
                (completed_at, publish_event_id),
            )
            m.commit()

    return jsonify({"publish_event_id": publish_event_id})