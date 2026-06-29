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

    # PWA endpoints (manifest + service worker) stay at root paths.
    from blueprints.auth import pwa_bp
    app.register_blueprint(pwa_bp)

    register_cli(app)
    from blueprints._helpers import register_jinja_filters, register_template_context
    register_jinja_filters(app)
    register_template_context(app)

    @app.route("/health")
    def health() -> tuple[str, int]:
        return "ok", 200

    return app


app = create_app()


if __name__ == "__main__":
    import os
    if os.environ.get("RUNAPP") == "1":
        app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5001")))
