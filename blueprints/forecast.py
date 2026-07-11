"""/forecast blueprint: HTTP routes for the daily-average / horizon
prediction feature (PRD §2.1).

Routes:
  GET  /forecast/item/<item_id>?horizon_days=14
  GET  /forecast/product/<product_id>?horizon_days=14
  POST /forecast/recompute              (TASK 6 — added in next commit)

Pure math lives in blueprints/forecast_pure.py; this module is the
HTTP/DB glue only.

Daily-batch scheduling is implemented as a module-level daemon thread
(see `_start_scheduler`) so the feature is self-contained without
introducing a heavyweight dependency.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from datetime import datetime
from pathlib import Path

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, url_for

from db import get_warehouse_db
from permissions import require_platform_admin, require_role
from .forecast_pure import (
    classify_confidence,
    compute_daily_avg,
    compute_forecast_total,
)

bp = Blueprint("forecast", __name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_MIN_HORIZON = 1
_MAX_HORIZON = 90
_DEFAULT_HORIZON = 14


def _parse_horizon(raw) -> int | None:
    """Return int horizon in [1, 90] or None on invalid input."""
    if raw is None:
        return _DEFAULT_HORIZON
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    if n < _MIN_HORIZON or n > _MAX_HORIZON:
        return None
    return n


def _warehouse_code() -> str:
    return g.warehouse["code"] if g.warehouse else "unknown"


def _fetch_outbound_rows(item_id: int) -> list[tuple[datetime, float]]:
    """Return [(datetime, qty), ...] for non-rolled-back outbounds in last 30d."""
    db = get_warehouse_db()
    rows = db.execute(
        """SELECT requested_quantity, created_at
           FROM outbound_requests
           WHERE item_id = ? AND rolled_back = 0
             AND created_at >= datetime('now', '-30 days')""",
        (item_id,),
    ).fetchall()
    parsed: list[tuple[datetime, float]] = []
    for r in rows:
        try:
            ts = datetime.strptime(r["created_at"], "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue
        parsed.append((ts, float(r["requested_quantity"])))
    return parsed


def _fetch_production_rows(product_id: int) -> list[tuple[datetime, float]]:
    """Return [(datetime, qty), ...] for non-rolled-back production outputs in last 30d."""
    db = get_warehouse_db()
    rows = db.execute(
        """SELECT output_qty, created_at
           FROM production_runs
           WHERE product_id = ? AND rolled_back = 0
             AND created_at >= datetime('now', '-30 days')""",
        (product_id,),
    ).fetchall()
    parsed: list[tuple[datetime, float]] = []
    for r in rows:
        try:
            ts = datetime.strptime(r["created_at"], "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue
        parsed.append((ts, float(r["output_qty"])))
    return parsed


def _build_response(
    target_id: int,
    horizon: int,
    movements: list[tuple[datetime, float]],
) -> dict:
    """Assemble the JSON response (PRD §2.1.2 contract)."""
    n = len(movements)
    confidence = classify_confidence(n)
    if confidence == "cold_start":
        daily_avg = 0.0
        forecast_total = 0.0
        data_status = "cold_start"
    else:
        daily_avg = compute_daily_avg(movements)
        forecast_total = compute_forecast_total(daily_avg, horizon)
        data_status = "ok"
    return {
        "item_id": target_id,            # for product responses, this field
        "warehouse_code": _warehouse_code(),  # name is stable per spec
        "horizon_days": horizon,
        "daily_avg": daily_avg,
        "forecast_total": forecast_total,
        "confidence": confidence,
        "computed_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data_status": data_status,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@bp.route("/forecast/item/<int:item_id>", methods=["GET"])
@require_role("manager")
def forecast_item(item_id: int):
    horizon = _parse_horizon(request.args.get("horizon_days"))
    if horizon is None:
        return jsonify({"error": "invalid_horizon"}), 400

    db = get_warehouse_db()
    if db.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone() is None:
        return jsonify({"error": "not_found"}), 404

    movements = _fetch_outbound_rows(item_id)
    return jsonify(_build_response(item_id, horizon, movements))


@bp.route("/forecast/product/<int:product_id>", methods=["GET"])
@require_role("manager")
def forecast_product(product_id: int):
    horizon = _parse_horizon(request.args.get("horizon_days"))
    if horizon is None:
        return jsonify({"error": "invalid_horizon"}), 400

    db = get_warehouse_db()
    if db.execute("SELECT 1 FROM products WHERE id=?", (product_id,)).fetchone() is None:
        return jsonify({"error": "not_found"}), 404

    movements = _fetch_production_rows(product_id)
    body = _build_response(product_id, horizon, movements)
    # Spec §1: the field is "item_id" for both shapes (stability for Agent).
    # We keep the same key, just changing its semantic to the product id.
    return jsonify(body)


@bp.route("/forecast", methods=["GET"])
@require_platform_admin
@require_role("manager")
def forecast_index():
    """Render the forecast dashboard for the current warehouse.

    Lists up to 50 items with their computed daily_avg + forecast_total.
    Manager+ can click 'recompute' to insert a fresh forecast_runs row.
    """
    db = get_warehouse_db()
    rows = db.execute(
        "SELECT id, name, quantity FROM items ORDER BY id LIMIT 50"
    ).fetchall()
    items = []
    horizon = 14
    for r in rows:
        movements = _fetch_outbound_rows(r["id"])
        n = len(movements)
        if classify_confidence(n) == "cold_start":
            avg, total, status = 0.0, 0.0, "cold_start"
        else:
            avg = compute_daily_avg(movements)
            total = compute_forecast_total(avg, horizon)
            status = "ok"
        items.append({
            "id": r["id"],
            "name": r["name"],
            "current_qty": r["quantity"],
            "daily_avg": avg,
            "forecast_total": total,
            "data_status": status,
        })
    return render_template("forecast.html", items=items, horizon=horizon)


@bp.route("/forecast/recompute", methods=["POST"])
@require_role("manager")
def forecast_recompute():
    """Mark a manual batch as complete (idempotent).

    The actual per-item forecast is computed on demand by the GET routes
    (no cache). This endpoint exists to satisfy PRD §2.1.5 (manual
    recompute) and to leave a trace row in forecast_runs for the
    /admin/health last-success display.

    Idempotency: if the most recent run is still status='success' (or
    'running') within the same minute, return that row's id instead of
    creating a new one. This matches the spec's "多次点击结果一致" claim
    while still allowing a real second run after a minute has passed.
    """
    from db import get_master_db

    db = get_master_db()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Idempotency: if a 'success' or 'running' row exists in the current
    # minute, return its id. strftime + sub-string compare is needed
    # because sqlite's datetime() modifier does not parse bound strings
    # (verified empirically — datetime(?, 'start of minute') returns NULL).
    minute_start = now[:17] + "00"  # "YYYY-MM-DD HH:MM:00"
    existing = db.execute(
        """SELECT id FROM forecast_runs
           WHERE status IN ('success', 'running')
             AND started_at >= ?
           ORDER BY id DESC LIMIT 1""",
        (minute_start,),
    ).fetchone()
    if existing is not None:
        return jsonify({"ok": True, "last_run_id": existing["id"]})

    cur = db.execute(
        "INSERT INTO forecast_runs (started_at, finished_at, status) VALUES (?, ?, 'success')",
        (now, now),
    )
    db.commit()
    return jsonify({"ok": True, "last_run_id": cur.lastrowid})


# ---------------------------------------------------------------------------
# Scheduler (TASK 8)
# ---------------------------------------------------------------------------

import logging
import time

from db import init_master_db

_logger = logging.getLogger(__name__)

_scheduler_lock = threading.Lock()
_scheduler_started = False
_STOP = threading.Event()

# Counter file path for /health observability. The path is module-level
# so tests can monkeypatch it. In production it lives under the project
# base dir next to access.log so operators can find it.
_LOCK_COUNTER_PATH = Path(__file__).resolve().parent.parent / "forecast_lock_failures.txt"


def _bump_lock_counter() -> None:
    """Increment the lock-failure counter (best-effort; never raises)."""
    try:
        n = 0
        if _LOCK_COUNTER_PATH.exists():
            try:
                n = int(_LOCK_COUNTER_PATH.read_text().strip() or "0")
            except ValueError:
                n = 0
        _LOCK_COUNTER_PATH.write_text(str(n + 1))
    except Exception:  # noqa: BLE001
        _logger.exception("forecast_lock: counter write failed")


def _read_lock_counter() -> int:
    try:
        if not _LOCK_COUNTER_PATH.exists():
            return 0
        return int(_LOCK_COUNTER_PATH.read_text().strip() or "0")
    except (ValueError, OSError):
        return 0


def _recover_orphaned_runs() -> None:
    """Mark any 'running' rows left behind by a previous crash as 'failed'.

    Called once at app startup (TASK 8). Sets finished_at to now and
    error_message to 'scheduler_restart'. Idempotent and safe to call
    multiple times. Uses a direct sqlite3 connection (not get_master_db)
    because this runs before/outside the Flask app context, where 'g'
    is unbound. Calls init_master_db() first so a fresh db is a no-op.
    """
    from config import MASTER_DB
    from datetime import datetime as _dt
    now = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
    init_master_db()  # no-op if already initialized
    with closing(sqlite3.connect(MASTER_DB)) as conn:
        conn.execute(
            """UPDATE forecast_runs
               SET status='failed', finished_at=?, error_message='scheduler_restart'
               WHERE status='running' AND finished_at IS NULL""",
            (now,),
        )
        conn.commit()


def _run_daily_forecast() -> int:
    """Run a single batch forecast pass over all warehouses.

    Returns the forecast_runs.id of the run (existing reused via
    same-minute idempotency, or newly inserted). 'items_processed'
    counts (items + products) touched across all warehouses — the
    values are computed on demand by GET routes, so this count is
    best-effort metadata for /admin/health operators.
    """
    from config import MASTER_DB, WAREHOUSE_DB_DIR
    from datetime import datetime as _dt
    now_str = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
    minute_start = now_str[:17] + "00"

    init_master_db()
    with closing(sqlite3.connect(MASTER_DB)) as m:
        m.row_factory = sqlite3.Row
        existing = m.execute(
            """SELECT id FROM forecast_runs
               WHERE status IN ('success', 'running')
                 AND started_at >= ?
               ORDER BY id DESC LIMIT 1""",
            (minute_start,),
        ).fetchone()
        if existing is not None:
            return existing["id"]

        items_processed = 0
        last_error: str | None = None
        if WAREHOUSE_DB_DIR.exists():
            for wh_path in WAREHOUSE_DB_DIR.glob("*.db"):
                attempt = 0
                while attempt < 3:
                    try:
                        with closing(sqlite3.connect(wh_path)) as w:
                            items_processed += w.execute(
                                "SELECT COUNT(*) FROM items"
                            ).fetchone()[0]
                            items_processed += w.execute(
                                "SELECT COUNT(*) FROM products"
                            ).fetchone()[0]
                        break
                    except sqlite3.OperationalError as exc:
                        attempt += 1
                        if attempt >= 3:
                            last_error = f"{wh_path.name}: {exc}"
                            _logger.warning("forecast_lock: %s", last_error)
                            _bump_lock_counter()
                        else:
                            time.sleep(0.05 * (2 ** (attempt - 1)))
                    except sqlite3.Error as exc:  # noqa: BLE001
                        last_error = f"{wh_path.name}: {exc}"
                        _logger.warning("forecast_lock: %s", last_error)
                        break

        if last_error is not None:
            cur = m.execute(
                "INSERT INTO forecast_runs (started_at, finished_at, status, items_processed, error_message) "
                "VALUES (?, ?, 'failed', ?, ?)",
                (now_str, now_str, items_processed, last_error),
            )
        else:
            cur = m.execute(
                "INSERT INTO forecast_runs (started_at, finished_at, status, items_processed) "
                "VALUES (?, ?, 'success', ?)",
                (now_str, now_str, items_processed),
            )
        m.commit()
        return cur.lastrowid


def _scheduler_loop() -> None:
    """Daemon thread loop: trigger _run_daily_forecast at 03:00 local.

    Polls every 30 seconds — sufficient for a once-a-day cron-style
    trigger. The 30s granularity means the actual run may fire up to
    30s after 03:00, which is acceptable per spec §4 (no precise time
    contract was promised). Uses local time (datetime.now()).
    """
    while not _STOP.is_set():
        now = datetime.now()
        if now.hour == 3 and now.minute == 0:
            try:
                _run_daily_forecast()
            except Exception:  # noqa: BLE001
                _logger.exception("forecast scheduler tick failed")
        # 30s poll — _STOP.wait returns True if stopped, breaking the loop
        if _STOP.wait(30):
            return


def _start_scheduler() -> None:
    """Idempotent: safe to call from create_app() multiple times.

    Recovers orphaned runs first, then starts the daemon thread. Thread
    is daemon=True so it does not block process exit. NOTE: under
    gunicorn multi-worker, each worker will run its own thread, so
    the same /admin/health minute-bucket can absorb N inserts (the
    idempotency key prevents that — only the first wins). Multi-worker
    is acknowledged in spec §4 as a known limitation.
    """
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        _scheduler_started = True
    _recover_orphaned_runs()
    t = threading.Thread(target=_scheduler_loop, name="forecast-scheduler", daemon=True)
    t.start()
