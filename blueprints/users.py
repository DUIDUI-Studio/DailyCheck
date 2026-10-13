"""User management (platform admin only).

- Create users (with optional per-warehouse role bindings at creation time)
- Reset passwords
- Toggle platform admin (is_admin)
- Toggle account active (is_active) — v2
- Bind / unbind / change role on a per-warehouse basis
- Bulk bind (v2) — bind N users to one warehouse with one role in a single POST
- Search / filter / paginate / tenant-scope the user list (v2)
- Create warehouses (optionally cloning items / products / product_bom
  from an existing one)
- v2 route hardening: every view is gated by ``@require_platform_admin``
  (the canonical platform-admin decorator). The legacy in-module
  ``_require_admin()`` shim has been removed; ``permissions.py`` owns the
  semantics so admin guards stay in one place.
"""
from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from flask import (
    Blueprint,
    abort,
    flash,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.security import generate_password_hash

from config import BASE_DIR, ROLE_RANK, WAREHOUSE_DB_DIR
from db import get_master_db, init_warehouse_db
from permissions import require_platform_admin

bp = Blueprint("users", __name__)

VALID_ROLES = set(ROLE_RANK.keys())  # staff / manager / admin

# v2 list page — default page size for paginated listings.
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

# v2 search — minimum query length to avoid full-table scans on every
# keystroke. 2 chars matches the typical "search-as-you-type" UX without
# accidentally filtering every user away on a 1-char typo.
MIN_SEARCH_LEN = 2


# ---------------------------------------------------------------------------
# v2 helpers — tenant scoping + listing
# ---------------------------------------------------------------------------

def _list_tenants() -> list[sqlite3.Row]:
    """All tenants (used by admin's tenant filter dropdown).

    Returns active + disabled tenants — the admin view is the place where
    you'd discover a disabled tenant, so we don't pre-filter on status.
    Sorted by id ascending so tenant 1 (default) is always first.
    """
    db = get_master_db()
    return db.execute(
        "SELECT id, name, status FROM tenants ORDER BY id"
    ).fetchall()


def _scoped_user_query(
    *,
    q: str | None,
    role: str | None,
    tenant_id: int | None,
    is_active: int | None,
    sort: str,
    page: int,
    page_size: int,
) -> tuple[str, list]:
    """Build (sql, params) for the filtered user list.

    Tenant scoping rules:
    - Platform admin (g.user['is_admin']=1): can ask for any tenant via
      ?tenant_id=, OR see all (tenant_id is None).
    - Non-platform-admin: hard-pinned to g.tenant_id. (In practice the
      route is gated by @require_platform_admin so this branch is dead
      today, but keeping the guard here makes the intent explicit and
      future-proof if the route ever loosens.)

    is_active:
    - None → show both active and inactive (admin wants to see disabled rows).
    - 0/1 → filter to that bucket.
    """
    where: list[str] = []
    params: list = []

    # Tenant scope — admin sees all by default.
    effective_tenant = tenant_id
    if not g.user["is_admin"]:
        effective_tenant = g.user["tenant_id"]
    if effective_tenant is not None:
        where.append("u.tenant_id = ?")
        params.append(effective_tenant)

    if q and len(q) >= MIN_SEARCH_LEN:
        where.append("u.username LIKE ?")
        params.append(f"%{q}%")

    if role in VALID_ROLES:
        # Subquery: any user that has at least one binding with this role.
        where.append(
            "EXISTS (SELECT 1 FROM warehouse_users wu "
            "WHERE wu.user_id = u.id AND wu.role = ?)"
        )
        params.append(role)

    if is_active in (0, 1):
        where.append("u.is_active = ?")
        params.append(is_active)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    # Whitelist sort columns to avoid SQL injection via ?sort=.
    # Strip the optional leading "-" (descending marker) BEFORE the
    # whitelist check — otherwise "-last_login_at" misses the set and
    # silently falls back to "id" while keeping DIRECTION=DESC, which
    # made every descending sort return id-descending order instead.
    sort_key = sort[1:] if sort.startswith("-") else sort
    if sort_key not in {"id", "username", "last_login_at"}:
        sort_key = "id"
    direction = "DESC" if sort.startswith("-") else "ASC"
    order_sql = f"ORDER BY u.{sort_key} {direction}, u.id ASC"

    offset = max(0, (page - 1) * page_size)
    limit_sql = "LIMIT ? OFFSET ?"
    params.extend([page_size, offset])

    sql = f"""
        SELECT u.id, u.username, u.is_admin, u.is_active, u.tenant_id,
               u.last_login_at, u.created_at, t.name AS tenant_name
        FROM users u
        LEFT JOIN tenants t ON t.id = u.tenant_id
        {where_sql}
        {order_sql}
        {limit_sql}
    """
    return sql, params


def _count_scoped_users(
    *,
    q: str | None,
    role: str | None,
    tenant_id: int | None,
    is_active: int | None,
) -> tuple[str, list]:
    """Same WHERE as _scoped_user_query but with COUNT(*). LIMIT/OFFSET excluded."""
    where: list[str] = []
    params: list = []
    effective_tenant = tenant_id
    if not g.user["is_admin"]:
        effective_tenant = g.user["tenant_id"]
    if effective_tenant is not None:
        where.append("u.tenant_id = ?")
        params.append(effective_tenant)
    if q and len(q) >= MIN_SEARCH_LEN:
        where.append("u.username LIKE ?")
        params.append(f"%{q}%")
    if role in VALID_ROLES:
        where.append(
            "EXISTS (SELECT 1 FROM warehouse_users wu "
            "WHERE wu.user_id = u.id AND wu.role = ?)"
        )
        params.append(role)
    if is_active in (0, 1):
        where.append("u.is_active = ?")
        params.append(is_active)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    return f"SELECT COUNT(*) AS c FROM users u {where_sql}", params


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@bp.route("/users", methods=["GET"])
@require_platform_admin
def list_users():
    """v2 list: search/filter/paginate/tenant-scope the user list.

    Query params:
    - q: substring of username (≥2 chars)
    - role: filter by users who hold any binding with this role
    - tenant_id: filter to one tenant (admin only; non-admin scoped to g.tenant_id)
    - is_active: 0 / 1 / None (None = show both)
    - sort: id / username / last_login_at, optional leading "-"
    - page: 1-based, default 1
    - page_size: default DEFAULT_PAGE_SIZE, capped at MAX_PAGE_SIZE
    - format: only the literal "json" switches the response to a JSON
      envelope; anything else (including the browser's own Accept header)
      renders the HTML page. The browser never asks for json — the toolbar
      fetches the HTML page and swaps the tbody client-side.
    """
    q = (request.args.get("q") or "").strip() or None
    role = (request.args.get("role") or "").strip() or None
    tenant_id_raw = (request.args.get("tenant_id") or "").strip()
    tenant_id: int | None = None
    if tenant_id_raw.isdigit():
        tenant_id = int(tenant_id_raw)
    is_active_raw = (request.args.get("is_active") or "").strip()
    is_active: int | None = None
    if is_active_raw in ("0", "1"):
        is_active = int(is_active_raw)
    sort = (request.args.get("sort") or "id").strip()
    try:
        page = max(1, int(request.args.get("page") or 1))
    except ValueError:
        page = 1
    try:
        page_size = min(MAX_PAGE_SIZE, max(1, int(request.args.get("page_size") or DEFAULT_PAGE_SIZE)))
    except ValueError:
        page_size = DEFAULT_PAGE_SIZE

    db = get_master_db()

    # Row fetch (paginated).
    list_sql, list_params = _scoped_user_query(
        q=q, role=role, tenant_id=tenant_id, is_active=is_active,
        sort=sort, page=page, page_size=page_size,
    )
    user_rows = db.execute(list_sql, list_params).fetchall()

    # Bindings per user — pulled in one round-trip, then grouped in Python
    # (cheaper than per-row subquery in the main list SQL).
    # Convert sqlite3.Row → dict so the template can | tojson safely.
    user_rows = [dict(r) for r in user_rows]

    user_ids = [r["id"] for r in user_rows]
    bindings_by_user: dict[int, dict[int, str]] = {uid: {} for uid in user_ids}
    if user_ids:
        placeholders = ",".join("?" for _ in user_ids)
        bind_rows = db.execute(
            f"""SELECT wu.user_id, wu.warehouse_id, wu.role, w.code, w.name
                FROM warehouse_users wu
                JOIN warehouses w ON w.id = wu.warehouse_id
                WHERE wu.user_id IN ({placeholders})
                ORDER BY wu.user_id, w.code""",
            user_ids,
        ).fetchall()
        for br in bind_rows:
            bindings_by_user.setdefault(br["user_id"], {})[br["warehouse_id"]] = {
                "role": br["role"],
                "warehouse_code": br["code"],
                "warehouse_name": br["name"],
            }

    # Total count for pagination UI (same WHERE, no LIMIT/OFFSET).
    count_sql, count_params = _count_scoped_users(
        q=q, role=role, tenant_id=tenant_id, is_active=is_active,
    )
    total = db.execute(count_sql, count_params).fetchone()["c"]
    total_pages = max(1, (total + page_size - 1) // page_size)

    # Always load the warehouse list + tenants list — used by the create-
    # account drawer regardless of pagination state. Convert Row → dict
    # so the template's `| tojson` (used by users-admin.js to read the
    # data into JS) doesn't trip on sqlite3.Row.
    warehouses = [
        dict(r) for r in db.execute(
            "SELECT id, code, name, tenant_id FROM warehouses ORDER BY code"
        ).fetchall()
    ]
    tenants = [dict(r) for r in _list_tenants()]

    # JSON envelope: only when the caller explicitly opts in via
    # ?format=json. Plain GET (browser navigation) MUST always render
    # the HTML page so the toolbar / drawer JS can bootstrap from the
    # SSR'd DOM. (Detecting application/json from Accept would also
    # fire on the default "*/*" Accept header and break normal
    # page navigation.)
    if request.args.get("format") == "json":
        payload = {
            "users": [
                {
                    "id": r["id"],
                    "username": r["username"],
                    "is_admin": bool(r["is_admin"]),
                    "is_active": bool(r["is_active"]),
                    "tenant_id": r["tenant_id"],
                    "tenant_name": r["tenant_name"],
                    "last_login_at": r["last_login_at"],
                    "created_at": r["created_at"],
                    "bindings": [
                        {"warehouse_id": wid, "role": info["role"],
                         "warehouse_code": info["warehouse_code"],
                         "warehouse_name": info["warehouse_name"]}
                        for wid, info in bindings_by_user.get(r["id"], {}).items()
                    ],
                }
                for r in user_rows
            ],
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
        }
        return _no_store(jsonify(payload))

    resp = make_response(render_template(
        "users.html",
        users=user_rows,
        warehouses=warehouses,
        tenants=tenants,
        bindings_by_user=bindings_by_user,
        current_user_id=g.user["id"],
        # Echo filter state so the toolbar can rehydrate after reload.
        filter_state={
            "q": q or "",
            "role": role or "",
            "tenant_id": tenant_id,
            "is_active": is_active_raw,
            "sort": sort,
            "page": page,
            "page_size": page_size,
        },
        pagination={
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
        },
    ))
    return _no_store(resp)


def _no_store(resp):
    """标记响应不可缓存。

    /users 是「账号状态的事实来源」，且只有平台管理员能看。默认响应没有任何
    Cache-Control，一旦被 Service Worker 的 Cache API 存下来，缓存副本按 URL
    命中即可返回、**不经过服务端鉴权**（实测匿名会话也能拿到 200 的管理员页面），
    并且列表会静默显示陈旧数据。

    注意 no-store 的作用域：它只约束**浏览器 HTTP 缓存与中间代理**，对
    Service Worker 的 Cache API **无效** —— 实测 `cache.put()` 照样成功落盘
    （MDN 的 Cache.put 异常清单里没有 no-store 规则）。所以挡住这条路的**唯一
    防线是 `static/sw.js` 里「只缓存同源 /static/」的范围判断**；本头是纵深防御
    的附加收益（避免 HTTP 层与代理缓存），不要把它当成可以放心回退范围判断的兜底。
    """
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.route("/users/create", methods=["POST"])
@require_platform_admin
def create_user():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    is_admin = request.form.get("is_admin") == "1"
    # v2: honor explicit tenant_id from the form so admin can create users
    # into a non-default tenant. Falls back to the actor's tenant.
    tenant_raw = (request.form.get("tenant_id") or "").strip()
    if tenant_raw.isdigit():
        tenant_id = int(tenant_raw)
    else:
        tenant_id = g.user["tenant_id"]
    if not username or not password:
        flash("账号和密码必填")
        return redirect(url_for("users.list_users"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db = get_master_db()
    try:
        db.execute(
            """INSERT INTO users (username, password_hash, is_admin, created_at, tenant_id)
               VALUES (?, ?, ?, ?, ?)""",
            (username, generate_password_hash(password, method="pbkdf2:sha256"),
             1 if is_admin else 0, now, tenant_id),
        )
        new_id = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        db.commit()
        _apply_bindings_from_form(new_id, request.form, db)
        _log_admin_action(
            f"create user #{new_id} {username} admin={is_admin} tenant={tenant_id} "
            f"bindings={_bindings_summary(new_id, db)}"
        )
        flash(f"已创建账号 {username}")
    except sqlite3.IntegrityError:
        flash(f"账号 {username} 已存在")
    return redirect(url_for("users.list_users"))


@bp.route("/users/<int:user_id>/bind", methods=["POST"])
@require_platform_admin
def bind_role(user_id: int):
    """Add a binding (warehouse_id + role) for an existing user."""
    db = get_master_db()
    if db.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone() is None:
        abort(404)
    wh_id, role, err = _parse_binding(request.form, db)
    if err:
        flash(err)
        return redirect(url_for("users.list_users"))
    try:
        db.execute(
            """INSERT INTO warehouse_users (user_id, warehouse_id, role)
               VALUES (?, ?, ?)""",
            (user_id, wh_id, role),
        )
        db.commit()
        _log_admin_action(f"bind user #{user_id} wh={wh_id} role={role}")
        flash(f"已绑定仓库 {wh_id} 角色 {role}")
    except sqlite3.IntegrityError:
        flash("该仓库已绑定(请用修改角色)")
    return redirect(url_for("users.list_users"))


@bp.route("/users/<int:user_id>/unbind", methods=["POST"])
@require_platform_admin
def unbind_role(user_id: int):
    """Remove a binding for an existing user."""
    db = get_master_db()
    if db.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone() is None:
        abort(404)
    wh_id_raw = request.form.get("warehouse_id", "").strip()
    if not wh_id_raw.isdigit():
        flash("仓库 id 非法")
        return redirect(url_for("users.list_users"))
    wh_id = int(wh_id_raw)
    db.execute(
        "DELETE FROM warehouse_users WHERE user_id=? AND warehouse_id=?",
        (user_id, wh_id),
    )
    db.commit()
    _log_admin_action(f"unbind user #{user_id} wh={wh_id}")
    flash("已解除绑定")
    return redirect(url_for("users.list_users"))


@bp.route("/users/<int:user_id>/change-role", methods=["POST"])
@require_platform_admin
def change_role(user_id: int):
    """Change the role on an existing binding."""
    db = get_master_db()
    if db.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone() is None:
        abort(404)
    wh_id, role, err = _parse_binding(request.form, db)
    if err:
        flash(err)
        return redirect(url_for("users.list_users"))
    existing = db.execute(
        "SELECT role FROM warehouse_users WHERE user_id=? AND warehouse_id=?",
        (user_id, wh_id),
    ).fetchone()
    if existing is None:
        flash("该仓库未绑定,请用绑定")
        return redirect(url_for("users.list_users"))
    db.execute(
        "UPDATE warehouse_users SET role=? WHERE user_id=? AND warehouse_id=?",
        (role, user_id, wh_id),
    )
    db.commit()
    _log_admin_action(f"change-role user #{user_id} wh={wh_id} -> {role}")
    flash(f"角色已更新为 {role}")
    return redirect(url_for("users.list_users"))


@bp.route("/users/<int:user_id>/reset-password", methods=["POST"])
@require_platform_admin
def reset_password(user_id: int):
    password = request.form.get("password", "")
    if not password:
        flash("新密码必填")
        return redirect(url_for("users.list_users"))
    db = get_master_db()
    target = db.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
    if target is None:
        abort(404)
    db.execute(
        "UPDATE users SET password_hash=? WHERE id=?",
        (generate_password_hash(password, method="pbkdf2:sha256"), user_id),
    )
    db.commit()
    _log_admin_action(f"reset password for user #{user_id} {target['username']}")
    flash(f"已重置 {target['username']} 的密码")
    return redirect(url_for("users.list_users"))


@bp.route("/users/<int:user_id>/toggle-admin", methods=["POST"])
@require_platform_admin
def toggle_admin(user_id: int):
    if g.user["id"] == user_id:
        flash("不能修改自己的平台管理员状态")
        return redirect(url_for("users.list_users"))
    db = get_master_db()
    target = db.execute(
        "SELECT username, is_admin FROM users WHERE id=?", (user_id,)
    ).fetchone()
    if target is None:
        abort(404)
    new_state = 0 if target["is_admin"] else 1
    db.execute("UPDATE users SET is_admin=? WHERE id=?", (new_state, user_id))
    db.commit()
    _log_admin_action(
        f"toggle admin user #{user_id} {target['username']} -> {bool(new_state)}"
    )
    flash(
        f"{target['username']} {'已设为' if new_state else '已取消'}平台管理员"
    )
    return redirect(url_for("users.list_users"))


# ---------------------------------------------------------------------------
# v2 endpoints — toggle-active + bulk-bind
# ---------------------------------------------------------------------------

@bp.route("/users/<int:user_id>/toggle-active", methods=["POST"])
@require_platform_admin
def toggle_active(user_id: int):
    """Flip users.is_active (0 ↔ 1). Used by the drawer "停用账号" toggle.

    Guards:
    - Cannot deactivate self (would lock the only admin out of /users).
    - Deactivation is reversible (re-enable by submitting the same route
      again), unlike delete which is permanent.
    """
    if g.user["id"] == user_id:
        flash("不能停用/启用自己的账号")
        return redirect(url_for("users.list_users"))
    db = get_master_db()
    target = db.execute(
        "SELECT username, is_active FROM users WHERE id=?", (user_id,)
    ).fetchone()
    if target is None:
        abort(404)
    new_state = 0 if target["is_active"] else 1
    db.execute("UPDATE users SET is_active=? WHERE id=?", (new_state, user_id))
    db.commit()
    _log_admin_action(
        f"toggle-active user #{user_id} {target['username']} -> {bool(new_state)}"
    )
    flash(
        f"{target['username']} {'已停用' if new_state == 0 else '已启用'}"
    )
    return redirect(url_for("users.list_users"))


@bp.route("/users/bulk-bind", methods=["POST"])
@require_platform_admin
def bulk_bind():
    """Bind N users to one warehouse with one role, in a single POST.

    Form fields:
    - user_ids: comma-separated user ids (e.g. "2,3,5,7")
    - warehouse_id: integer
    - role: staff | manager | admin

    Behavior:
    - Each insert is INSERT OR IGNORE so already-bound (user, warehouse)
      rows are skipped silently. The audit log records the requested
      set and the actually-inserted count so the admin can reconcile.
    - Invalid user_ids (non-existent) are skipped (not an error) so a
      stale selection from the table doesn't break the whole POST.
    - Invalid role / warehouse_id → flash error, no writes.
    """
    raw_ids = (request.form.get("user_ids") or "").strip()
    wh_id_raw = (request.form.get("warehouse_id") or "").strip()
    role = (request.form.get("role") or "").strip()

    if role not in VALID_ROLES:
        flash(f"角色必须是 {sorted(VALID_ROLES)} 之一")
        return redirect(url_for("users.list_users"))
    if not wh_id_raw.isdigit():
        flash("仓库 id 必填")
        return redirect(url_for("users.list_users"))
    wh_id = int(wh_id_raw)

    db = get_master_db()
    if db.execute("SELECT 1 FROM warehouses WHERE id=?", (wh_id,)).fetchone() is None:
        flash("仓库不存在")
        return redirect(url_for("users.list_users"))

    user_ids: list[int] = []
    for tok in raw_ids.split(","):
        tok = tok.strip()
        if tok.isdigit():
            user_ids.append(int(tok))
    if not user_ids:
        flash("至少选择一个用户")
        return redirect(url_for("users.list_users"))

    inserted = 0
    for uid in user_ids:
        # Make sure the user actually exists; otherwise the FK would
        # raise IntegrityError and roll back the whole batch.
        if db.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone() is None:
            continue
        cur = db.execute(
            """INSERT OR IGNORE INTO warehouse_users (user_id, warehouse_id, role)
               VALUES (?, ?, ?)""",
            (uid, wh_id, role),
        )
        if cur.rowcount:
            inserted += 1
    db.commit()
    _log_admin_action(
        f"bulk-bind wh={wh_id} role={role} "
        f"requested={len(user_ids)} inserted={inserted}"
    )
    flash(f"批量绑定完成:请求 {len(user_ids)} 人,实际新增 {inserted} 条")
    return redirect(url_for("users.list_users"))


@bp.route("/users/<int:user_id>/delete", methods=["POST"])
@require_platform_admin
def delete_user(user_id: int):
    """Permanently delete a user.

    Guard rails:
    - Cannot delete self (would lock out the only admin).
    - Cannot delete the last platform admin (must keep ≥1 admin).
    - warehouse_users bindings cascade-delete with the user.
    """
    if g.user["id"] == user_id:
        flash("不能删除自己的账号")
        return redirect(url_for("users.list_users"))
    db = get_master_db()
    target = db.execute(
        "SELECT username, is_admin FROM users WHERE id=?", (user_id,)
    ).fetchone()
    if target is None:
        abort(404)
    if target["is_admin"]:
        admin_count = db.execute(
            "SELECT COUNT(*) AS c FROM users WHERE is_admin=1"
        ).fetchone()["c"]
        if admin_count <= 1:
            flash("至少保留一个平台管理员,无法删除")
            return redirect(url_for("users.list_users"))
    db.execute("DELETE FROM warehouse_users WHERE user_id=?", (user_id,))
    db.execute("DELETE FROM users WHERE id=?", (user_id,))
    db.commit()
    _log_admin_action(f"delete user #{user_id} {target['username']}")
    flash(f"已删除账号 {target['username']}")
    return redirect(url_for("users.list_users"))


# ---------------------------------------------------------------------------
# Warehouse routes (unchanged behavior, decorator upgraded to @require_platform_admin)
# ---------------------------------------------------------------------------

@bp.route("/warehouses/create", methods=["POST"])
@require_platform_admin
def create_warehouse():
    """Create a new warehouse. Supports two types:

    * storefront (门店): code must start with 'wh_'; may clone catalog
      (items / products / BOM) from an existing warehouse.
    * rd (研发中心): code must start with 'rd_'; no cloning — the R&D
      warehouse starts empty and gets recipes/items populated manually
      (or via the rd seed script).
    """
    code = request.form.get("code", "").strip().lower()
    name = request.form.get("name", "").strip()
    clone_from_code = request.form.get("clone_from_code", "").strip()
    warehouse_type = request.form.get("warehouse_type", "storefront").strip().lower()

    if warehouse_type not in ("storefront", "rd"):
        flash("仓库类型必须是 storefront 或 rd")
        return redirect(url_for("users.list_users"))

    if warehouse_type == "storefront":
        if not re.fullmatch(r"wh_\w+", code):
            flash("门店仓库编码必须以 wh_ 开头,后面接字母/数字/下划线")
            return redirect(url_for("users.list_users"))
    else:  # rd
        if not re.fullmatch(r"rd_\w+", code):
            flash("研发中心仓库编码必须以 rd_ 开头,后面接字母/数字/下划线")
            return redirect(url_for("users.list_users"))
        # rd warehouses start empty — clone_from makes no sense.
        clone_from_code = ""

    if not name:
        flash("仓库名称必填")
        return redirect(url_for("users.list_users"))

    db_path = WAREHOUSE_DB_DIR / f"{code}.db"
    if db_path.exists():
        flash(f"仓库编码 {code} 的数据库文件已存在")
        return redirect(url_for("users.list_users"))

    db = get_master_db()
    if db.execute("SELECT 1 FROM warehouses WHERE code=?", (code,)).fetchone() is not None:
        flash(f"仓库编码 {code} 已注册")
        return redirect(url_for("users.list_users"))

    db_path.parent.mkdir(parents=True, exist_ok=True)
    init_warehouse_db(db_path)
    counts = {"items": 0, "products": 0, "bom": 0}
    if clone_from_code:
        if clone_from_code == code:
            flash("克隆源不能是自己")
            return redirect(url_for("users.list_users"))
        if db.execute("SELECT 1 FROM warehouses WHERE code=?", (clone_from_code,)).fetchone() is None:
            flash(f"克隆源仓库 {clone_from_code} 不存在")
            return redirect(url_for("users.list_users"))
        src_path = WAREHOUSE_DB_DIR / f"{clone_from_code}.db"
        if not src_path.exists():
            flash(f"克隆源数据库 {src_path} 缺失")
            return redirect(url_for("users.list_users"))
        counts = _clone_catalog_from(src_path, db_path)

    rel_path = str(db_path.relative_to(BASE_DIR))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        """INSERT INTO warehouses (code, name, db_path, warehouse_type, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (code, name, rel_path, warehouse_type, now),
    )
    db.commit()
    _log_admin_action(
        f"create warehouse {code} ({name}, type={warehouse_type})"
        + (f" cloned {counts['items']} items / {counts['products']} products / "
           f"{counts['bom']} bom from {clone_from_code}"
           if clone_from_code else "")
    )
    flash(
        f"已创建 {('研发中心' if warehouse_type == 'rd' else '门店')}仓库 {code} ({name})"
        + (f",从 {clone_from_code} 复制了 {counts['items']} 个品项"
           if clone_from_code else "")
    )
    return redirect(url_for("users.list_users"))


@bp.route("/warehouses/<int:warehouse_id>/init", methods=["POST"])
@require_platform_admin
def init_warehouse(warehouse_id: int):
    """Wipe business data for one warehouse, preserving the catalog.

    What gets cleared:
      - stock_movements, stocktakes, stocktake_batches
      - restock_requests, outbound_requests, adjustment_requests
      - daily_revenue, audit_log
      - items.quantity / items.safety_stock reset to 0

    What gets preserved:
      - categories (system-fixed seeds)
      - items (the catalog: sku, name, unit, unit_cost) — only
        quantity and safety_stock are zeroed

    Two-step confirmation: the form sends a hidden confirm_token that
    must equal the warehouse's code. Plain-text confirmation in the
    button label.
    """
    db = get_master_db()
    wh = db.execute(
        "SELECT code, name, db_path FROM warehouses WHERE id=?", (warehouse_id,)
    ).fetchone()
    if wh is None:
        abort(404)

    expected = request.form.get("confirm_token", "")
    if expected.strip() != wh["code"]:
        flash(f"确认口令不匹配:请输入仓库编码 '{wh['code']}' 二次确认")
        return redirect(url_for("users.list_users"))

    db_path = BASE_DIR / wh["db_path"]
    target_tables = [
        "stock_movements",
        "stocktakes",
        "stocktake_batches",
        "restock_requests",
        "outbound_requests",
        "adjustment_requests",
        "daily_revenue",
        "audit_log",
    ]
    with closing(sqlite3.connect(db_path)) as wh_db:
        cur = wh_db.cursor()
        cur.execute("PRAGMA foreign_keys = OFF")
        for tbl in target_tables:
            # target_tables is a hard-coded literal list (never user input);
            # the f-string only avoids eight near-identical statements. Guard
            # anyway so a future edit cannot smuggle a dynamic name in.
            if not tbl.isidentifier():
                raise ValueError(f"unexpected table name: {tbl!r}")
            cur.execute(f"DELETE FROM {tbl}")
        cur.execute("UPDATE items SET quantity = 0, safety_stock = 0")
        wh_db.commit()

    _log_admin_action(
        f"init warehouse #{warehouse_id} {wh['code']} ({wh['name']}) "
        f"cleared {len(target_tables)} tables + zeroed item quantities"
    )
    flash(f"已初始化仓库 {wh['name']}({wh['code']}):业务数据已清空,品类/品项保留")
    return redirect(url_for("users.list_users"))


# ---------------------------------------------------------------------------
# Helpers (unchanged from v1 except where noted)
# ---------------------------------------------------------------------------

def _bindings_for(user_id: int) -> dict[int, str]:
    """Return {warehouse_id: role} for a user."""
    db = get_master_db()
    rows = db.execute(
        "SELECT warehouse_id, role FROM warehouse_users WHERE user_id=?",
        (user_id,),
    ).fetchall()
    return {r["warehouse_id"]: r["role"] for r in rows}


def _parse_binding(form, db) -> tuple[int | None, str | None, str | None]:
    """Parse and validate warehouse_id + role from a form. Returns
    (wh_id, role, error_msg). error_msg is non-None when validation fails."""
    wh_id_raw = form.get("warehouse_id", "").strip()
    role = form.get("role", "").strip()
    if not wh_id_raw.isdigit():
        return None, None, "仓库 id 必填"
    wh_id = int(wh_id_raw)
    if role not in VALID_ROLES:
        return None, None, f"角色必须是 {sorted(VALID_ROLES)} 之一"
    if db.execute("SELECT 1 FROM warehouses WHERE id=?", (wh_id,)).fetchone() is None:
        return None, None, "仓库不存在"
    return wh_id, role, None


def _apply_bindings_from_form(user_id: int, form, db) -> None:
    """For the create form: pull all `wh_<id>` role select fields and bind."""
    for key, value in form.items():
        if not key.startswith("wh_"):
            continue
        raw_id = key[3:]
        if not raw_id.isdigit():
            continue
        wh_id = int(raw_id)
        if value in VALID_ROLES:
            db.execute(
                """INSERT OR IGNORE INTO warehouse_users (user_id, warehouse_id, role)
                   VALUES (?, ?, ?)""",
                (user_id, wh_id, value),
            )
    db.commit()


def _bindings_summary(user_id: int, db) -> str:
    rows = db.execute(
        """SELECT w.code, wu.role FROM warehouse_users wu
           JOIN warehouses w ON w.id = wu.warehouse_id
           WHERE wu.user_id=?""",
        (user_id,),
    ).fetchall()
    return ",".join(f"{r['code']}:{r['role']}" for r in rows)


def _log_admin_action(detail: str) -> None:
    """Append an admin action line to a simple flat file. Master.db has no
    audit_log table; the per-warehouse audit_log only covers business writes."""
    import json
    from pathlib import Path
    log = Path(__file__).resolve().parent.parent / "admin_audit.log"
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "actor": g.user["username"] if g.user else None,
            "detail": detail,
        }, ensure_ascii=False) + "\n")


def _clone_catalog_from(src_path: Path, dst_path: Path) -> dict[str, int]:
    """Copy items / products / product_bom from src to dst.

    Preserves SKUs and per-item fields (safety_stock, unit_cost, unit,
    gram_per_unit, category_id, name) but resets quantity to 0 so the
    new warehouse starts empty. The fixed categories must already be
    seeded in dst by init_warehouse_db before calling this.

    Returns a {items, products, bom} count dict for the audit log.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with closing(sqlite3.connect(src_path)) as src, \
         closing(sqlite3.connect(dst_path)) as dst:
        src.row_factory = sqlite3.Row
        items = src.execute(
            """SELECT name, category_id, sku, safety_stock, unit_cost, unit,
                      gram_per_unit
               FROM items"""
        ).fetchall()
        dst.executemany(
            """INSERT INTO items
               (name, category_id, sku, quantity, safety_stock, unit_cost, unit,
                gram_per_unit, updated_at)
               VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?)""",
            [(r["name"], r["category_id"], r["sku"], r["safety_stock"],
              r["unit_cost"], r["unit"], r["gram_per_unit"], now) for r in items],
        )

        products = src.execute(
            "SELECT name, unit, note, created_at FROM products"
        ).fetchall()
        dst.executemany(
            """INSERT INTO products (name, unit, note, created_at)
               VALUES (?, ?, ?, ?)""",
            [(r["name"], r["unit"], r["note"], now) for r in products],
        )

        # BOM preserves product_id / item_id from src. This relies on both
        # dbs inserting items and products in the same order so the
        # auto-increment ids line up — true because we just inserted them
        # in that order above. Cloning a fully-built warehouse this way
        # is the supported entry point; ad-hoc per-row clones aren't.
        bom = src.execute(
            "SELECT product_id, item_id, qty_per_unit FROM product_bom"
        ).fetchall()
        dst.executemany(
            """INSERT INTO product_bom (product_id, item_id, qty_per_unit)
               VALUES (?, ?, ?)""",
            [(r["product_id"], r["item_id"], r["qty_per_unit"]) for r in bom],
        )
        dst.commit()
    return {"items": len(items), "products": len(products), "bom": len(bom)}
