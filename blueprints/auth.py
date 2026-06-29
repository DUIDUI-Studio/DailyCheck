"""Auth blueprint: login, logout, warehouse picker, PWA manifest + sw.

The before_request hook here does three things on every request:
1. Load the logged-in user from session.
2. If a warehouse is selected, look up the db_path from master.db and
   bind it to g.warehouse_db_path so get_warehouse_db() can find it.
3. Redirect to /login or /select-warehouse when missing.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from flask import (
    Blueprint, abort, flash, g, jsonify, redirect, render_template,
    request, send_from_directory, session, url_for,
)
from werkzeug.security import check_password_hash

from config import BASE_DIR, FIXED_CATEGORIES, MASTER_DB, SECRET_KEY
from db import get_master_db, init_warehouse_db
from permissions import require_login


bp = Blueprint("auth", __name__)
pwa_bp = Blueprint("pwa", __name__)


# ---------------------------------------------------------------------------
# Request lifecycle
# ---------------------------------------------------------------------------

PUBLIC_ENDPOINTS = {
    "auth.login", "auth.logout", "static",
    "pwa.service_worker", "pwa.webmanifest",
    # /health is an operator probe (subproject 1 §3.6) — must be
    # reachable without auth, so monitoring tools can hit it.
    "health",
}


@bp.before_app_request
def load_user_and_warehouse():
    g.user = None
    g.warehouse = None
    g.role = None
    g.warehouse_db_path = None

    user_id = session.get("user_id")
    warehouse_id = session.get("warehouse_id")
    if user_id is None:
        if request.endpoint not in PUBLIC_ENDPOINTS:
            return redirect(url_for("auth.login", next=request.path))
        return None

    master = get_master_db()
    g.user = master.execute(
        "SELECT * FROM users WHERE id=?", (user_id,)
    ).fetchone()
    if g.user is None:
        session.clear()
        return redirect(url_for("auth.login"))

    if warehouse_id is not None:
        row = master.execute(
            "SELECT * FROM warehouses WHERE id=?", (warehouse_id,)
        ).fetchone()
        if row is not None:
            g.warehouse = row
            g.warehouse_db_path = str(BASE_DIR / row["db_path"])
            g.role = master.execute(
                """SELECT role FROM warehouse_users
                   WHERE user_id=? AND warehouse_id=?""",
                (user_id, warehouse_id),
            ).fetchone()
            # Run idempotent column migrations on the warehouse db so
            # legacy dbs created before certain columns still work.
            # Cheap when already up-to-date (just PRAGMA lookups).
            from db import migrate_warehouse_db_columns
            migrate_warehouse_db_columns(Path(g.warehouse_db_path))
        else:
            session.pop("warehouse_id", None)

    # If a non-public route fires but warehouse is missing, send to picker.
    # Skip the picker itself to avoid an infinite redirect loop.
    if (
        request.endpoint not in PUBLIC_ENDPOINTS
        and request.endpoint != "auth.warehouse_picker"
        and g.warehouse is None
        and request.method == "GET"
    ):
        return redirect(url_for("auth.warehouse_picker"))

    return None


# ---------------------------------------------------------------------------
# Login / logout
# ---------------------------------------------------------------------------

@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        master = get_master_db()
        row = master.execute(
            "SELECT * FROM users WHERE username=?", (username,)
        ).fetchone()
        if row is None or not check_password_hash(row["password_hash"], password):
            flash("账号或密码错误")
            return render_template("login.html"), 401
        session.clear()
        session["user_id"] = row["id"]
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        master.execute(
            "UPDATE users SET last_login_at=? WHERE id=?", (now, row["id"])
        )
        master.commit()
        nxt = request.args.get("next") or url_for("auth.warehouse_picker")
        return redirect(nxt)
    return render_template("login.html")


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


# ---------------------------------------------------------------------------
# Warehouse picker
# ---------------------------------------------------------------------------

@bp.route("/select-warehouse", methods=["GET"])
@require_login
def warehouse_picker():
    """Always show the list of warehouses the user has access to.

    Per project decision: no auto-remember of last warehouse. User clicks
    each time. Admins see all warehouses; regular users see only those
    bound via warehouse_users.
    """
    master = get_master_db()
    if g.user["is_admin"]:
        rows = master.execute(
            "SELECT id, code, name FROM warehouses ORDER BY id"
        ).fetchall()
    else:
        rows = master.execute(
            """SELECT w.id, w.code, w.name
               FROM warehouses w
               JOIN warehouse_users wu ON wu.warehouse_id = w.id
               WHERE wu.user_id = ?
               ORDER BY w.id""",
            (g.user["id"],),
        ).fetchall()
    return render_template("warehouse_picker.html", warehouses=rows, no_sidebar=True)


@bp.route("/select-warehouse/<int:warehouse_id>", methods=["POST"])
@require_login
def warehouse_select(warehouse_id: int):
    master = get_master_db()
    wh = master.execute(
        "SELECT * FROM warehouses WHERE id=?", (warehouse_id,)
    ).fetchone()
    if wh is None:
        abort(404)
    if not g.user["is_admin"]:
        binding = master.execute(
            "SELECT 1 FROM warehouse_users WHERE user_id=? AND warehouse_id=?",
            (g.user["id"], warehouse_id),
        ).fetchone()
        if binding is None:
            abort(403)
    session["warehouse_id"] = warehouse_id
    return redirect(url_for("core.land"))


# ---------------------------------------------------------------------------
# PWA endpoints
# ---------------------------------------------------------------------------

@pwa_bp.route("/sw.js")
def service_worker():
    resp = send_from_directory(BASE_DIR / "static", "sw.js")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@pwa_bp.route("/manifest.webmanifest")
def webmanifest():
    return send_from_directory(
        BASE_DIR / "static", "manifest.webmanifest",
        mimetype="application/manifest+json",
    )


# Convenience helper used by other blueprints to write audit rows.
def audit(action: str, target_type: str | None = None,
          target_id: int | None = None, detail: dict | None = None) -> None:
    """Append an audit_log row to the current warehouse db."""
    if g.warehouse_db_path is None:
        return
    import sqlite3
    from db import get_warehouse_db
    db = get_warehouse_db()
    db.execute(
        """INSERT INTO audit_log
           (user_id, username, action, target_type, target_id, detail, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            g.user["id"] if g.user else None,
            g.user["username"] if g.user else None,
            action,
            target_type,
            target_id,
            json.dumps(detail, ensure_ascii=False) if detail else None,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    db.commit()
