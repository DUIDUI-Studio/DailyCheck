"""POST /admin/publish/recipe blueprint.

Spec: docs/superpowers/specs/2026-06-29-recipe-publish-design.md
PRD : §2.5 (versioned recipes, per-store effective version, cross-warehouse
       notification fan-out via emit_event).

This is the first real caller of blueprints.notifications_pure.emit_event
(PRD §2.5.4 / spec §0.4) — a successful publish produces exactly one
"recipe_published" event with summary + target_url = /products/<pid>/versions/<vid>.

Per spec §0.1: emit_event failures do NOT roll back the version insert.
Per spec §0.6: partial-success semantics — invalid warehouse codes are
recorded as failed in recipe_publish_event_warehouses but do not
poison the whole call.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime

from flask import Blueprint, g, jsonify, request

import config  # use config.MASTER_DB at call time so monkeypatch works
from db import get_warehouse_db, init_master_db
from permissions import require_platform_admin

from .notifications_pure import emit_event
from .publish_recipe_pure import create_new_bom_version

bp = Blueprint("publish_recipe", __name__)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _resolve_warehouse_db_path(code: str) -> str | None:
    """Look up warehouse db_path by code in master.db. Returns None if missing."""
    init_master_db()
    with closing(sqlite3.connect(config.MASTER_DB)) as m:
        row = m.execute(
            "SELECT db_path FROM warehouses WHERE code=?", (code,)
        ).fetchone()
    return None if row is None else str(row[0])


def _all_user_ids() -> list[int]:
    """Return all user ids for notification fan-out (spec §0.4, PRD §2.5.4)."""
    init_master_db()
    with closing(sqlite3.connect(config.MASTER_DB)) as m:
        m.row_factory = sqlite3.Row
        rows = m.execute("SELECT id FROM users").fetchall()
    return [int(r["id"]) for r in rows]


@bp.route("/admin/publish/recipe", methods=["POST"])
@require_platform_admin
def publish_recipe():
    """Publish a new recipe version to N warehouses + fan-out notification.

    Body (JSON):
      product_id:        int, required
      bom_items:         [{item_id, qty_per_unit}, ...], required (non-empty)
      warehouse_codes:   [str, ...], required (at least one)
      summary:           str, optional (default: '配方已发布')

    Returns 200 with:
      {
        version_id:        int,
        publish_event_id:  int,
        failed_warehouses: [{warehouse_code, error_message}, ...]
      }

    Error responses:
      400 empty_bom
      400 no_warehouses
      404 product_not_found
      500 unexpected (logged + audit)
    """
    payload = request.get_json(silent=True) or request.form

    product_id_raw = payload.get("product_id")
    bom_items = payload.get("bom_items") or []
    warehouse_codes = payload.get("warehouse_codes") or []
    summary = payload.get("summary") or "配方已发布"

    # ---- Validation -------------------------------------------------------
    if not bom_items:
        return jsonify({"error": "empty_bom"}), 400
    if not warehouse_codes:
        return jsonify({"error": "no_warehouses"}), 400
    try:
        product_id = int(product_id_raw)
    except (TypeError, ValueError):
        return jsonify({"error": "product_not_found"}), 404

    wh_db = get_warehouse_db()
    row = wh_db.execute(
        "SELECT id FROM products WHERE id=?", (product_id,)
    ).fetchone()
    if row is None:
        return jsonify({"error": "product_not_found"}), 404

    # ---- Step 1: new bom version (warehouse.db, current session's wh) -----
    new_version_id = create_new_bom_version(wh_db, product_id, bom_items)
    wh_db.execute(
        "UPDATE products SET current_version_id=? WHERE id=?",
        (new_version_id, product_id),
    )
    wh_db.commit()

    # ---- Step 2: per-warehouse store_versions (in the product's wh_db) ----
    # Spec §2.2 places product_bom_store_versions in warehouse.db. The product
    # lives in the current session's wh_db (where the admin is acting), so all
    # store-version rows go there keyed by warehouse_code.
    failed: list[dict] = []
    effective_at = _now()
    for code in warehouse_codes:
        wh_path = _resolve_warehouse_db_path(code)
        if wh_path is None:
            failed.append({"warehouse_code": code, "error_message": "warehouse_not_found"})
            continue
        try:
            wh_db.execute(
                """INSERT INTO product_bom_store_versions
                   (product_id, warehouse_code, bom_version_id, effective_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(product_id, warehouse_code) DO UPDATE SET
                     bom_version_id=excluded.bom_version_id,
                     effective_at=excluded.effective_at""",
                (product_id, code, new_version_id, effective_at),
            )
            wh_db.commit()
        except sqlite3.Error as exc:  # noqa: BLE001
            failed.append({"warehouse_code": code, "error_message": str(exc)})

    # ---- Step 3: master.db publish_event + per-warehouse status ------------
    ts = _now()
    completed_at = ts  # publish completed synchronously
    init_master_db()
    with closing(sqlite3.connect(config.MASTER_DB)) as m:
        cur = m.execute(
            """INSERT INTO recipe_publish_events
               (product_id, bom_version_id, started_by, started_at,
                completed_at, summary, warehouse_codes_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                product_id,
                new_version_id,
                int(g.user["id"]) if g.user else None,
                ts,
                completed_at,
                summary,
                json.dumps(warehouse_codes, ensure_ascii=False),
            ),
        )
        publish_event_id = int(cur.lastrowid)
        # success rows for the ones we successfully linked
        for code in warehouse_codes:
            if any(f["warehouse_code"] == code for f in failed):
                err = next(f["error_message"] for f in failed if f["warehouse_code"] == code)
                m.execute(
                    """INSERT INTO recipe_publish_event_warehouses
                       (publish_event_id, warehouse_code, status, error_message)
                       VALUES (?, ?, 'failed', ?)""",
                    (publish_event_id, code, err),
                )
            else:
                m.execute(
                    """INSERT INTO recipe_publish_event_warehouses
                       (publish_event_id, warehouse_code, status)
                       VALUES (?, ?, 'success')""",
                    (publish_event_id, code),
                )
        # Audit log (PRD §3.6)
        try:
            m.execute(
                """INSERT INTO audit_log
                   (user_id, username, action, target_type, target_id, detail, created_at)
                   VALUES (?, ?, 'publish_recipe', 'product', ?, ?, ?)""",
                (
                    int(g.user["id"]) if g.user else None,
                    g.user["username"] if g.user else None,
                    product_id,
                    json.dumps({
                        "version_id": new_version_id,
                        "publish_event_id": publish_event_id,
                        "warehouses": warehouse_codes,
                        "failed": failed,
                    }, ensure_ascii=False),
                    ts,
                ),
            )
        except sqlite3.Error:
            # Audit must never block the publish.
            pass
        m.commit()

    # ---- Step 4: emit_event — fan out to all users (spec §0.4, §6) ---------
    # Per spec §0.1: emit_event failures are logged but do not roll back.
    target_url = f"/products/{product_id}/versions/{new_version_id}"
    try:
        with closing(sqlite3.connect(config.MASTER_DB)) as m:
            m.row_factory = sqlite3.Row
            emit_event(m, "recipe_published", summary, target_url, _all_user_ids())
    except Exception:  # noqa: BLE001
        # Logged but not propagated (PRD §2.5.4: notification is a side
        # channel; the publish already succeeded).
        import logging
        logging.getLogger(__name__).exception(
            "emit_event failed for publish_event_id=%s", publish_event_id
        )

    return jsonify({
        "version_id": new_version_id,
        "publish_event_id": publish_event_id,
        "failed_warehouses": failed,
    })
