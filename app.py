"""Application factory.

Routes are split into blueprints under blueprints/. Each blueprint reads
from the per-request warehouse db via get_warehouse_db() — the db path
is bound in blueprints/auth.py's before_request hook from session.
"""
from __future__ import annotations

from flask import Flask, request

from cli import register_cli
from config import IS_PRODUCTION, SECRET_KEY
from db import close_dbs, init_master_db


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = SECRET_KEY
    # Make sessions a bit longer-lived; default is one browser session.
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    # Only mark the session cookie Secure in production — dev runs plain HTTP,
    # where a Secure cookie would never be sent back by the browser.
    app.config["SESSION_COOKIE_SECURE"] = IS_PRODUCTION
    app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024  # 4 MB uploads

    # CSRF 保护（P0-3）。必须在注册 CSRFProtect **之前**注册本 gate：
    # Flask 的 before_request 按注册顺序执行，gate 先跑并按 TESTING 翻开关，
    # 随后 CSRFProtect.protect() 读到 WTF_CSRF_ENABLED=False 即放行。
    # - 生产/开发常态：CSRF 开启（所有 POST form 必带 csrf_token）。
    # - TESTING=True：默认关闭（存量测试零改动）；专测 CSRF 的用例设
    #   WTF_CSRF_FORCE_ENABLED=True 显式打开。
    # - MCP（/api/mcp/ 等）跑在独立 Starlette 进程、Bearer 鉴权，不经本 app。
    app.config.setdefault("WTF_CSRF_ENABLED", True)

    @app.before_request
    def _csrf_testing_gate() -> None:
        force = app.config.get("WTF_CSRF_FORCE_ENABLED", False)
        app.config["WTF_CSRF_ENABLED"] = force or not app.config.get("TESTING", False)

    from flask_wtf import CSRFProtect

    CSRFProtect(app)

    # 全局 IP 限速（P0-4）。内存滑动窗口，per-app 实例（app.extensions），
    # gunicorn 多 worker 各自计数、重启清零（v1 约定，见 rate_limit_pure）。
    # /health、static 不限；TESTING 默认关闭（RATE_LIMIT_FORCE_ENABLED 打开），
    # 与 CSRF gate 同一模式。
    import os as _os

    from rate_limit_pure import GLOBAL_WEB_LIMIT_PER_MIN, SlidingWindowLimiter

    app.extensions["rate_limiter"] = SlidingWindowLimiter(
        max_events=int(
            _os.environ.get("DAILYCHECK_RATE_LIMIT_PER_MIN", GLOBAL_WEB_LIMIT_PER_MIN)
        ),
        window_seconds=60.0,
    )

    @app.before_request
    def _global_rate_limit_gate():
        if request.path.startswith("/static") or request.endpoint == "health":
            return None
        force = app.config.get("RATE_LIMIT_FORCE_ENABLED", False)
        if not (force or not app.config.get("TESTING", False)):
            return None
        limiter = app.extensions["rate_limiter"]
        key = request.remote_addr or "unknown"
        if not limiter.hit(key):
            return {"error": "rate_limited"}, 429
        return None

    # Database teardown applies to both master + warehouse connections.
    app.teardown_appcontext(close_dbs)

    # Ensure master.db exists on first boot.
    with app.app_context():
        init_master_db()

    # Register blueprints.
    from blueprints.agent_tokens import bp as agent_tokens_bp
    from blueprints.auth import bp as auth_bp
    from blueprints.canonical import bp as canonical_bp
    from blueprints.core import bp as core_bp
    from blueprints.forecast import _start_scheduler
    from blueprints.forecast import bp as forecast_bp
    from blueprints.import_items import bp as import_items_bp
    from blueprints.items import bp as items_bp
    from blueprints.notifications import bp as notifications_bp
    from blueprints.outbound import bp as outbound_bp
    from blueprints.procurement import bp as procurement_bp
    from blueprints.production import bp as production_bp
    from blueprints.recipe_cost import bp as recipe_cost_bp
    from blueprints.reports import bp as reports_bp
    from blueprints.restock import bp as restock_bp
    from blueprints.stocktake import bp as stocktake_bp
    from blueprints.store_ordering import bp as store_ordering_bp
    from blueprints.users import bp as users_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(core_bp)
    app.register_blueprint(items_bp)
    app.register_blueprint(stocktake_bp)
    app.register_blueprint(restock_bp)
    app.register_blueprint(outbound_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(production_bp)
    app.register_blueprint(recipe_cost_bp)
    app.register_blueprint(users_bp)
    app.register_blueprint(import_items_bp)
    app.register_blueprint(forecast_bp)
    app.register_blueprint(procurement_bp)
    app.register_blueprint(notifications_bp)
    app.register_blueprint(agent_tokens_bp)
    app.register_blueprint(canonical_bp)
    app.register_blueprint(store_ordering_bp)

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
        from blueprints.forecast import _read_lock_counter
        from db import get_master_db
        last_success = None
        try:
            db = get_master_db()
            row = db.execute(
                """SELECT finished_at FROM forecast_runs
                   WHERE status='success' AND finished_at IS NOT NULL
                   ORDER BY finished_at DESC LIMIT 1"""
            ).fetchone()
            if row is not None and row["finished_at"]:
                # finished_at is stored in local time ("YYYY-MM-DD HH:MM:SS").
                # The endpoint contract is ISO-8601 UTC ("...Z"), so convert to
                # real UTC — the previous code merely appended "Z" to a local
                # timestamp, i.e. it lied about the zone.
                from datetime import datetime as _dt
                from datetime import timezone as _tz
                last_success = (
                    _dt.strptime(row["finished_at"], "%Y-%m-%d %H:%M:%S")
                    .astimezone(_tz.utc)
                    .strftime("%Y-%m-%dT%H:%M:%SZ")
                )
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
