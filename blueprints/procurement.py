"""/procurement blueprint: per-store + hub procurement suggestions,
CSV acceptance export, plus invalidation hooks consumed by the four
write-side blueprints (outbound / restock / stocktake / adjustment).

Spec: docs/superpowers/specs/2026-06-29-procurement-design.md
PRD : §2.2
"""
from __future__ import annotations

import csv
import sqlite3
import tempfile
from collections import defaultdict
from contextlib import closing
from datetime import datetime
from pathlib import Path

from flask import (
    Blueprint, abort, g, jsonify, render_template, request, send_from_directory,
)

import config  # use config.MASTER_DB at call time so monkeypatch works
from db import get_master_db, get_warehouse_db, init_master_db
from permissions import require_role
from .procurement_pure import (
    aggregate_hub,
    compute_safety_stock,
    compute_suggested_qty,
)

bp = Blueprint("procurement", __name__)


# ---------------------------------------------------------------------------
# Invalidation helper — exposed for other blueprints to call
# ---------------------------------------------------------------------------


def mark_procurement_invalid(item_id: int) -> None:
    """Mark this item's procurement_cache row as invalid (forces recompute).

    Called from outbound/restock/stocktake/adjustment approve hooks so
    that the next /procurement/store GET re-derives the suggestion.
    Idempotent: marks the row invalid even if it does not exist yet
    (the recompute will create it on the next read).
    """
    init_master_db()
    with closing(sqlite3.connect(config.MASTER_DB)) as m:
        m.row_factory = sqlite3.Row
        m.execute(
            """UPDATE procurement_cache SET invalid=1
               WHERE item_id=?""",
            (item_id,),
        )
        m.commit()


# ---------------------------------------------------------------------------
# Core per-warehouse computation (shared by /store and /hub)
# ---------------------------------------------------------------------------


def _get_config() -> tuple[int, float]:
    """Return (cover_days, min_absolute) from procurement_config."""
    init_master_db()
    with closing(sqlite3.connect(config.MASTER_DB)) as m:
        m.row_factory = sqlite3.Row
        row = m.execute("SELECT cover_days, min_absolute FROM procurement_config WHERE id=1").fetchone()
    if row is None:
        return 14, 0.0
    return int(row["cover_days"]), float(row["min_absolute"])


def _resolve_warehouse(warehouse_code: str) -> dict:
    """Look up a warehouse row from master.db by code. Returns sqlite Row or None."""
    init_master_db()
    with closing(sqlite3.connect(config.MASTER_DB)) as m:
        m.row_factory = sqlite3.Row
        return m.execute(
            "SELECT * FROM warehouses WHERE code=?", (warehouse_code,)
        ).fetchone()


def _user_can_access_warehouse(wh_code: str) -> bool:
    """Platform admins bypass; others must have a warehouse_users binding."""
    if g.user is None:
        return False
    if g.user["is_admin"]:
        return True
    init_master_db()
    with closing(sqlite3.connect(config.MASTER_DB)) as m:
        m.row_factory = sqlite3.Row
        row = m.execute(
            """SELECT 1 FROM warehouse_users wu
               JOIN warehouses w ON w.id = wu.warehouse_id
               WHERE wu.user_id=? AND w.code=?""",
            (g.user["id"], wh_code),
        ).fetchone()
    return row is not None


def _read_cache(wh_code: str) -> dict[int, dict]:
    """Read cache rows for the given warehouse as {item_id: row_dict}."""
    init_master_db()
    with closing(sqlite3.connect(config.MASTER_DB)) as m:
        m.row_factory = sqlite3.Row
        rows = m.execute(
            "SELECT * FROM procurement_cache WHERE warehouse_code=?",
            (wh_code,),
        ).fetchall()
    return {r["item_id"]: dict(r) for r in rows}


def _write_cache(wh_code: str, item_id: int, payload: dict) -> None:
    """Upsert a single item's cache row."""
    init_master_db()
    with closing(sqlite3.connect(config.MASTER_DB)) as m:
        m.execute(
            """INSERT INTO procurement_cache
               (item_id, warehouse_code, computed_at, daily_avg, current_qty,
                in_transit_qty, safety_stock, suggested_qty, invalid)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
               ON CONFLICT(item_id, warehouse_code) DO UPDATE SET
                 computed_at=excluded.computed_at,
                 daily_avg=excluded.daily_avg,
                 current_qty=excluded.current_qty,
                 in_transit_qty=excluded.in_transit_qty,
                 safety_stock=excluded.safety_stock,
                 suggested_qty=excluded.suggested_qty,
                 invalid=0""",
            (
                item_id, wh_code, payload["computed_at"],
                payload["daily_avg"], payload["current_qty"],
                payload["in_transit_qty"], payload["safety_stock"],
                payload["suggested_qty"],
            ),
        )
        m.commit()


def _outbound_30d_sum(wh_path: str, item_id: int) -> float:
    """Sum of 30-day consumption (outbound + production) for an item.

    Delegates to blueprints.consumption.raw_30d_sum so /procurement and
    /forecast agree on what "consumption" means (PRD §1.1 A2).
    """
    with closing(sqlite3.connect(wh_path)) as w:
        return _fetch_item_movements_30d(w, item_id)  # type: ignore[return-value]


def _fetch_item_movements_30d(db: sqlite3.Connection, item_id: int) -> float:
    from .consumption import raw_30d_sum
    return raw_30d_sum(db, item_id)


def _count_consumption_30d(db: sqlite3.Connection, item_id: int) -> int:
    from .consumption import count_30d_records
    return count_30d_records(db, item_id)


def _weighted_daily_avg(wh_path: str, item_id: int) -> float:
    """Linear-decay weighted average of consumption qty over last 30d.

    Source = outbound_requests UNION production_run_items (matches
    /inventory and /forecast). Mirrors
    blueprints.consumption.compute_weighted_daily_avg exactly so the
    procurement safety stock + the forecast daily_avg come from the
    same number. Returns 0.0 if no recent movements.
    """
    from .consumption import compute_weighted_daily_avg, fetch_item_movements_30d
    with closing(sqlite3.connect(wh_path)) as w:
        movements = fetch_item_movements_30d(w, item_id)
    return compute_weighted_daily_avg(movements)


def _in_transit_qty(wh_path: str, item_id: int) -> float:
    """Sum of open restock_requests (status not '已到货' / '已取消')."""
    with closing(sqlite3.connect(wh_path)) as w:
        w.row_factory = sqlite3.Row
        row = w.execute(
            """SELECT COALESCE(SUM(requested_quantity), 0) AS total
               FROM restock_requests
               WHERE item_id=?
                 AND status NOT IN ('已到货', '已取消')""",
            (item_id,),
        ).fetchone()
    return float(row["total"] or 0)


def compute_store_procurement(wh_code: str, wh_path: str) -> list[dict]:
    """Compute procurement suggestions for one warehouse (no cache read).

    Returns a list of {item_id, item_name, current_qty, in_transit_qty,
    daily_avg, forecast_total_horizon, safety_stock, suggested_qty}.
    Cold-start items (n<7 outbound) are excluded (PRD §2.2.6).
    Items with safety_stock <= current + in_transit are excluded too
    (suggested_qty would be 0, no actionable signal).
    """
    cover_days, min_absolute = _get_config()
    now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    out: list[dict] = []
    with closing(sqlite3.connect(wh_path)) as w:
        w.row_factory = sqlite3.Row
        items = w.execute(
            "SELECT id, name, quantity, unit FROM items ORDER BY id"
        ).fetchall()
    for it in items:
        # Replicate forecast's cold-start threshold: n<7 records in the
        # last 30 days (outbound + production consumption, same source
        # as the weighted_avg below).
        with closing(sqlite3.connect(wh_path)) as w:
            n = _count_consumption_30d(w, it["id"])
        if n < 7:
            continue
        avg = _weighted_daily_avg(wh_path, it["id"])
        if avg <= 0:
            continue
        in_transit = _in_transit_qty(wh_path, it["id"])
        current = float(it["quantity"])
        safety = compute_safety_stock(avg, cover_days, min_absolute)
        forecast_horizon = round(avg * cover_days, 2)
        suggested = compute_suggested_qty(safety, current, in_transit)
        if suggested <= 0:
            continue
        out.append({
            "item_id": it["id"],
            "item_name": it["name"],
            "current_qty": current,
            "in_transit_qty": in_transit,
            "daily_avg": avg,
            "forecast_total_horizon": forecast_horizon,
            "safety_stock": safety,
            "suggested_qty": suggested,
            "_computed_at": now_iso,  # internal, stripped from response
        })
    return out


def _store_procurement_json(wh_code: str) -> dict:
    """Return the JSON body for /procurement/store, with cache logic."""
    wh = _resolve_warehouse(wh_code)
    if wh is None:
        return None
    wh_path = wh["db_path"]
    cache = _read_cache(wh_code)
    items: list[dict] = []
    any_invalid = any(row.get("invalid") for row in cache.values())
    needs_recompute = not cache or any_invalid
    if needs_recompute:
        # Recompute every item that exists. For now we just always
        # recompute when the cache is empty or any row is invalid; the
        # per-item invalidation handler ensures we re-derive consistently.
        computed = compute_store_procurement(wh_code, wh_path)
        for c in computed:
            payload = {
                "computed_at": c["_computed_at"],
                "daily_avg": c["daily_avg"],
                "current_qty": c["current_qty"],
                "in_transit_qty": c["in_transit_qty"],
                "safety_stock": c["safety_stock"],
                "suggested_qty": c["suggested_qty"],
            }
            _write_cache(wh_code, c["item_id"], payload)
        items = computed
    else:
        # Use cache, but filter items whose safety_stock+current_qty
        # conditions are no longer satisfied — defensive in case
        # mark_procurement_invalid was missed.
        for item_id, row in cache.items():
            suggested = int(row["suggested_qty"])
            if suggested <= 0:
                continue
            items.append({
                "item_id": item_id,
                "item_name": row.get("item_name", ""),  # may be empty in cache
                "current_qty": row["current_qty"],
                "in_transit_qty": row["in_transit_qty"],
                "daily_avg": row["daily_avg"],
                "forecast_total_horizon": round(row["daily_avg"] * 14, 2),
                "safety_stock": row["safety_stock"],
                "suggested_qty": suggested,
                "_computed_at": row["computed_at"],
            })
    # Enrich item_name when reading from cache (cache doesn't store name
    # to keep the schema simple; the per-warehouse db is the source of
    # truth for naming).
    if items and not items[0].get("item_name"):
        names = {}
        with closing(sqlite3.connect(wh_path)) as w:
            w.row_factory = sqlite3.Row
            for r in w.execute("SELECT id, name FROM items").fetchall():
                names[r["id"]] = r["name"]
        for it in items:
            it["item_name"] = names.get(it["item_id"], "")
    # Strip internal fields
    for it in items:
        it.pop("_computed_at", None)
    return {
        "warehouse_code": wh_code,
        "computed_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "items": items,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@bp.route("/procurement/store", methods=["GET"])
@require_role("staff")
def procurement_store():
    wh_code = request.args.get("warehouse_code") or (
        g.warehouse["code"] if g.warehouse else None
    )
    if not wh_code:
        return jsonify({"error": "warehouse_code_required"}), 400
    if not _user_can_access_warehouse(wh_code):
        return jsonify({"error": "forbidden"}), 403
    body = _store_procurement_json(wh_code)
    if body is None:
        return jsonify({"error": "not_found"}), 404
    if request.args.get("format") == "html" or request.headers.get("Accept", "").startswith("text/html"):
        return render_template("procurement_store.html", **body)
    return jsonify(body)


@bp.route("/procurement/hub", methods=["GET"])
@require_role("manager")
def procurement_hub():
    init_master_db()
    with closing(sqlite3.connect(config.MASTER_DB)) as m:
        m.row_factory = sqlite3.Row
        codes = [r["code"] for r in m.execute("SELECT code FROM warehouses ORDER BY code").fetchall()]
    if not codes:
        return jsonify({"computed_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), "items": []})
    reports = []
    for c in codes:
        body = _store_procurement_json(c)
        if body is None:
            continue
        # Strip non-essential fields for the hub report
        reports.append({
            "warehouse_code": body["warehouse_code"],
            "items": [
                {
                    "item_id": it["item_id"],
                    "item_name": it["item_name"],
                    "suggested_qty": it["suggested_qty"],
                }
                for it in body["items"]
            ],
        })
    hub_items = aggregate_hub(reports)
    return jsonify({
        "computed_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "items": hub_items,
    })


@bp.route("/procurement/store/accept", methods=["POST"])
@require_role("staff")
def procurement_accept():
    """Generate the CSV file and return download metadata."""
    payload = request.get_json(silent=True) or request.form
    wh_code = payload.get("warehouse_code") or (g.warehouse["code"] if g.warehouse else None)
    if not wh_code:
        return jsonify({"error": "warehouse_code_required"}), 400
    if not _user_can_access_warehouse(wh_code):
        return jsonify({"error": "forbidden"}), 403
    body = _store_procurement_json(wh_code)
    if body is None:
        return jsonify({"error": "not_found"}), 404
    if not body["items"]:
        return jsonify({"error": "no_items_to_export"}), 400

    # Fetch unit + note for each item from the warehouse db
    wh = _resolve_warehouse(wh_code)
    with closing(sqlite3.connect(wh["db_path"])) as w:
        w.row_factory = sqlite3.Row
        meta = {
            r["id"]: {"unit": r["unit"], "note": ""}
            for r in w.execute("SELECT id, unit, '' AS note FROM items").fetchall()
        }

    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    filename = f"procurement_acceptance_{wh_code}_{ts}.csv"
    out_path = Path(tempfile.gettempdir()) / filename
    try:
        with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["item_id", "item_name", "suggested_qty", "unit", "note"])
            for it in body["items"]:
                m = meta.get(it["item_id"], {"unit": "", "note": ""})
                writer.writerow([
                    it["item_id"],
                    it["item_name"],
                    it["suggested_qty"],
                    m["unit"],
                    m["note"],
                ])
    except OSError:
        from logging import getLogger
        getLogger(__name__).exception("procurement_csv_fail: %s", wh_code)
        return jsonify({"error": "csv_write_failed"}), 500

    return jsonify({
        "ok": True,
        "filename": filename,
        "item_count": len(body["items"]),
        "download_url": f"/procurement/store/accept/download?filename={filename}",
    })


@bp.route("/procurement/store/accept/download", methods=["GET"])
@require_role("staff")
def procurement_accept_download():
    filename = request.args.get("filename", "")
    # Sanity check: only allow filenames in the expected pattern to prevent
    # path traversal (send_from_directory also enforces, defense in depth).
    if not filename.startswith("procurement_acceptance_") or "/" in filename or ".." in filename:
        abort(400)
    return send_from_directory(
        tempfile.gettempdir(), filename, as_attachment=True
    )
