"""Application factory.

Routes are split into blueprints under blueprints/. Each blueprint reads
from the per-request warehouse db via get_warehouse_db() — the db path
is bound in blueprints/auth.py's before_request hook from session.
"""
from __future__ import annotations

from flask import Flask, redirect, url_for

from config import SECRET_KEY
from db import close_dbs, init_master_db
from cli import register_cli


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = SECRET_KEY
    # Make sessions a bit longer-lived; default is one browser session.
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024  # 4 MB uploads

    # Database teardown applies to both master + warehouse connections.
    app.teardown_appcontext(close_dbs)

    # Ensure master.db exists on first boot.
    with app.app_context():
        init_master_db()

    # Register blueprints.
    from blueprints.auth import bp as auth_bp
    from blueprints.core import bp as core_bp
    from blueprints.items import bp as items_bp
    from blueprints.stocktake import bp as stocktake_bp
    from blueprints.restock import bp as restock_bp
    from blueprints.outbound import bp as outbound_bp
    from blueprints.adjustment import bp as adjustment_bp
    from blueprints.reports import bp as reports_bp
    from blueprints.production import bp as production_bp
    from blueprints.users import bp as users_bp
    from blueprints.forecast import bp as forecast_bp, _start_scheduler
    from blueprints.procurement import bp as procurement_bp
    from blueprints.notifications import bp as notifications_bp
    from blueprints.publish_items import bp as publish_items_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(core_bp)
    app.register_blueprint(items_bp)
    app.register_blueprint(stocktake_bp)
    app.register_blueprint(restock_bp)
    app.register_blueprint(outbound_bp)
    app.register_blueprint(adjustment_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(production_bp)
    app.register_blueprint(users_bp)
    app.register_blueprint(forecast_bp)
    app.register_blueprint(procurement_bp)
    app.register_blueprint(notifications_bp)
    app.register_blueprint(publish_items_bp)

    # PWA endpoints (manifest + service worker) stay at root paths.
    from blueprints.auth import pwa_bp
    app.register_blueprint(pwa_bp)

    _start_scheduler()

    register_cli(app)
    from blueprints._helpers import register_jinja_filters, register_template_context
    register_jinja_filters(app)
    register_template_context(app)

    @app.route("/health")
    def health() -> tuple[dict, int]:
        """JSON health probe with forecast last-success timestamp + lock counter.

        Returns the most recent forecast_runs.finished_at for any
        status='success' row, or null if none. Surfaces PRD §3.6
        (must-automate observability) without a separate /admin/health
        page in this subproject. Lock-failure counter is read from a
        file maintained by blueprints.forecast._bump_lock_counter.
        """
        from db import get_master_db
        from blueprints.forecast import _read_lock_counter
        last_success = None
        try:
            db = get_master_db()
            row = db.execute(
                """SELECT finished_at FROM forecast_runs
                   WHERE status='success' AND finished_at IS NOT NULL
                   ORDER BY finished_at DESC LIMIT 1"""
            ).fetchone()
            if row is not None and row["finished_at"]:
                # Convert "YYYY-MM-DD HH:MM:SS" → ISO Z
                last_success = row["finished_at"].replace(" ", "T") + "Z"
        except Exception:  # noqa: BLE001 — health must never 500
            pass
        return {
            "status": "ok",
            "forecast_last_success_at": last_success,
            "forecast_lock_failures": _read_lock_counter(),
        }, 200

    return app


app = create_app()


if __name__ == "__main__":
    import os
    if os.environ.get("RUNAPP") == "1":
        app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5001")))
