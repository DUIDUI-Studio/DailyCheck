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
from urllib.parse import urlsplit

from flask import (
    Blueprint,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.security import check_password_hash

from config import BASE_DIR
from db import get_master_db
from permissions import current_relative_path, require_login

bp = Blueprint("auth", __name__)
pwa_bp = Blueprint("pwa", __name__)


# ---------------------------------------------------------------------------
# Deep-link helper
# ---------------------------------------------------------------------------

def _safe_next(raw: str | None) -> str | None:
    """Return `raw` only when it is a safe same-origin redirect target.

    Purpose: **prevent open redirect**. `next` travels through the browser
    (login form hidden field / query string / picker form action), so by the
    time it comes back it is untrusted input. A bare `redirect(next)` would
    let anyone craft `/login?next=https://evil.example` — the victim logs in
    for real, then lands on the attacker's page and gets phished for the same
    credentials.

    Accepts only same-origin *relative* paths:
      - non-empty, starting with exactly one "/"
      - not starting with "//" or "/\\" (protocol-relative / browser-quirk
        forms that a browser resolves to another host)
      - `urlsplit()` yields neither a scheme nor a netloc
      - no control characters (defence against header injection)

    Returns None for anything else, so callers fall back to their default
    landing page. Never raises: a malformed value must not turn the login
    flow into a 500.
    """
    if not raw:
        return None
    candidate = raw.strip()
    if not candidate.startswith("/"):
        return None
    if candidate.startswith("//") or candidate.startswith("/\\"):
        return None
    if any(ch < " " or ch == "\x7f" for ch in candidate):
        return None
    parts = urlsplit(candidate)
    if parts.scheme or parts.netloc:
        return None
    return candidate


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
    g.tenant_id = None  # P0-1 多租户 PoC：请求级租户上下文

    user_id = session.get("user_id")
    warehouse_id = session.get("warehouse_id")

    # Agent MPC routes (/api/v1/*) authenticate with Bearer tokens, not
    # sessions, so this hook must not bounce them to /login.
    #
    # Deliberately scoped by endpoint prefix instead of a bare "/api/v1/"
    # path prefix: a path rule silently makes ANY future /api/v1/* route
    # session-free, which is an auth bypass waiting to happen. The pending
    # sub6-agent-mpc branch registers its routes under the "agent_mpc"
    # blueprint, so this form matches it — when that branch lands, keep the
    # endpoint-scoped check rather than reinstating the path prefix.
    if request.endpoint is not None and request.endpoint.startswith("agent_mpc."):
        return None

    if user_id is None:
        if request.endpoint not in PUBLIC_ENDPOINTS:
            return redirect(url_for("auth.login", next=current_relative_path()))
        return None

    master = get_master_db()
    g.user = master.execute(
        "SELECT * FROM users WHERE id=?", (user_id,)
    ).fetchone()
    if g.user is None:
        session.clear()
        return redirect(url_for("auth.login", next=current_relative_path()))

    # P0-1 多租户 PoC：平台管理员（is_admin=1）跨租户；普通用户锁定本租户。
    g.tenant_id = g.user["tenant_id"]

    if warehouse_id is not None:
        row = master.execute(
            "SELECT * FROM warehouses WHERE id=?", (warehouse_id,)
        ).fetchone()
        if row is not None and (
            g.user["is_admin"] or row["tenant_id"] == g.tenant_id
        ):
            g.warehouse = row
            g.warehouse_db_path = str(BASE_DIR / row["db_path"])
            g.role = master.execute(
                """SELECT role FROM warehouse_users
                   WHERE user_id=? AND warehouse_id=?""",
                (user_id, warehouse_id),
            ).fetchone()
            # Run version-gated column migrations before any view runs, so
            # code paths that open a warehouse db directly (canonical,
            # procurement, store-ordering, ...) never hit a missing column
            # on a legacy db. No-op once the db has been stamped.
            from db import migrate_warehouse_db_columns
            migrate_warehouse_db_columns(Path(g.warehouse_db_path))
        else:
            session.pop("warehouse_id", None)

    # If a non-public route fires but warehouse is missing, send to picker.
    # Skip the picker itself to avoid an infinite redirect loop.
    # `next` carries the original target so the deep-link survives both the
    # login detour and the warehouse-selection detour.
    if (
        request.endpoint not in PUBLIC_ENDPOINTS
        and request.endpoint != "auth.warehouse_picker"
        and g.warehouse is None
        and request.method == "GET"
    ):
        return redirect(
            url_for("auth.warehouse_picker", next=current_relative_path())
        )

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

        # 登录爆破锁定（P0-4）：先查锁再验密码。
        # 时间一律 UTC naive（rate_limit_pure 约定），与进程时区无关。
        from rate_limit_pure import login_lock_state, next_failure_state

        ip = request.remote_addr or "unknown"
        now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        throttle = master.execute(
            "SELECT failed_count, locked_until, updated_at FROM login_throttle "
            "WHERE ip=? AND username=?",
            (ip, username),
        ).fetchone()
        locked, remaining = login_lock_state(
            throttle["failed_count"] if throttle else 0,
            throttle["locked_until"] if throttle else None,
        )
        if locked:
            flash(f"失败次数过多，已临时锁定，请 {remaining // 60 + 1} 分钟后再试")
            return render_template("login.html"), 429

        row = master.execute(
            "SELECT * FROM users WHERE username=?", (username,)
        ).fetchone()
        # v2 账号管理：停用账号禁止登录（feat/user-admin-v2）。
        # 顺序：密码校验之前先查 is_active，避免已停用账号仍触发失败计数。
        if row is not None and row["is_active"] == 0:
            flash("账号已停用，请联系平台管理员")
            return render_template("login.html"), 403
        if row is None or not check_password_hash(row["password_hash"], password):
            new_count, new_lock = next_failure_state(
                throttle["failed_count"] if throttle else 0,
                throttle["updated_at"] if throttle else None,
            )
            master.execute(
                """INSERT INTO login_throttle (ip, username, failed_count,
                                               locked_until, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(ip, username) DO UPDATE SET
                       failed_count=excluded.failed_count,
                       locked_until=excluded.locked_until,
                       updated_at=excluded.updated_at""",
                (ip, username, new_count, new_lock, now_str),
            )
            master.commit()
            flash("账号或密码错误")
            return render_template("login.html"), 401

        # 登录成功：清除该 (ip, username) 的失败计数。
        master.execute(
            "DELETE FROM login_throttle WHERE ip=? AND username=?", (ip, username)
        )
        session.clear()
        session["user_id"] = row["id"]
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        master.execute(
            "UPDATE users SET last_login_at=? WHERE id=?", (now, row["id"])
        )
        master.commit()
        # 深链回跳：next 可能来自 query（登录前 302 带的）或表单隐藏域
        # （action 丢 query 时的双保险），故用 request.values 同时覆盖两者。
        nxt = _safe_next(request.values.get("next")) or url_for(
            "auth.warehouse_picker"
        )
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

    `next` (validated) is handed to the template so each warehouse button
    can carry the original deep-link target into the POST below; without it
    the user lands on /land after selecting a warehouse and the
    notification link is lost for good.
    """
    master = get_master_db()
    if g.user["is_admin"]:
        rows = master.execute(
            "SELECT id, code, name FROM warehouses ORDER BY id"
        ).fetchall()
    else:
        # P0-1 多租户：非平台管理员只见本租户 + 有绑定关系的仓。
        # （绑定与租户双闸——脏数据里跨租户绑定也不可见。）
        rows = master.execute(
            """SELECT w.id, w.code, w.name
               FROM warehouses w
               JOIN warehouse_users wu ON wu.warehouse_id = w.id
               WHERE wu.user_id = ? AND w.tenant_id = ?
               ORDER BY w.id""",
            (g.user["id"], g.user["tenant_id"]),
        ).fetchall()
    next_url = _safe_next(request.args.get("next")) or ""
    return render_template(
        "warehouse_picker.html",
        warehouses=rows,
        no_sidebar=True,
        next_url=next_url,
    )


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
        # P0-1 多租户：租户闸先行（跨租户绑定视为脏数据，直接拒绝）。
        if wh["tenant_id"] != g.user["tenant_id"]:
            abort(403)
        binding = master.execute(
            "SELECT 1 FROM warehouse_users WHERE user_id=? AND warehouse_id=?",
            (g.user["id"], warehouse_id),
        ).fetchone()
        if binding is None:
            abort(403)
    session["warehouse_id"] = warehouse_id
    # 深链回跳：有 next 就回到用户原本要看的页面，否则维持原有 /land 行为。
    nxt = _safe_next(request.values.get("next"))
    if nxt:
        return redirect(nxt)
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
