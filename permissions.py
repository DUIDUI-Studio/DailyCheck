"""Role-based access control.

Two layers coexist during the permission-granularity migration:

1. **Legacy role ladder** — `require_role(min_role)` ranks staff < manager <
   admin via `config.ROLE_RANK`. Kept unchanged for the routes the spec
   deliberately leaves on the compatibility layer (§1.2-D of the Phase-2 plan).

2. **Permission points** — a role is now a *set* of permission-point codes
   (`permissions_manifest.ROLE_PACKAGES`). `require_permission()` /
   `require_any_permission()` gate routes on those codes, and the resolved
   set for the current request is available via `current_permissions()` /
   `has_permission()`.

Resolution contract (Spec §4, hard constraints):
  - **fail-closed**: any lookup error yields an empty set → 403, never a pass.
  - **per-request only**: the set is cached on `g.perm_set`; there is
    deliberately NO process-level cache (gunicorn runs multiple workers, and
    a process cache would drift the moment an admin edits role packages).
  - **platform superadmin bypass**: `users.is_admin=1` keeps its existing
    "pass everything" semantics (equivalent to `require_role`).
"""
from __future__ import annotations

import functools
from collections.abc import Callable

from flask import abort, current_app, flash, g, redirect, request, url_for

from config import ROLE_RANK
from permissions_manifest import PERMISSION_CATALOG

# View function names that are allowed to run without a warehouse selected.
# All others are redirected to the picker.
WAREHOUSE_EXEMPT = {"warehouse_picker", "warehouse_select"}

# Sentinel: every permission point in the catalog. Returned for platform
# superadmins so `has_permission()` / the template `can()` helper work
# without an extra `is_admin` check at every call site.
_ALL_PERMISSIONS: frozenset[str] = frozenset(PERMISSION_CATALOG)


def current_permissions() -> frozenset[str]:
    """Return the permission-point set the current user holds on this warehouse.

    Order of resolution:
      1. not logged in                     -> frozenset() (empty)
      2. is_admin == 1 (platform superadmin) -> every catalog point (sentinel)
      3. warehouse not selected            -> frozenset() (empty; caller decides
                                              whether to bounce to the picker)
      4. otherwise                         -> one master.db query, cached on
                                              `g.perm_set` for the request

    Never raises: any failure resolves to an empty set (fail-closed) and logs a
    warning that carries no sensitive values.
    """
    user = g.get("user")
    if user is None:
        return frozenset()
    if user["is_admin"]:
        return _ALL_PERMISSIONS
    if "perm_set" in g:
        return g.perm_set
    perms = _resolve_permissions()
    g.perm_set = perms
    return perms


def _resolve_permissions() -> frozenset[str]:
    """Query master.db for the resolved permission points (fail-closed).

    An unknown `warehouse_users.role` code (no matching `roles` row) simply
    yields an empty set via the JOIN, which surfaces as a 403 at the guard —
    exactly the fail-closed behaviour the spec requires (AC-12).
    """
    if g.get("warehouse_db_path") is None or g.get("warehouse") is None:
        return frozenset()
    try:
        from db import get_master_db

        master = get_master_db()
        rows = master.execute(
            """
            SELECT DISTINCT rp.permission_code
              FROM warehouse_users wu
              JOIN roles r
                ON r.code = wu.role
               AND (r.is_builtin = 1 OR r.tenant_id = ?)
              JOIN role_permissions rp ON rp.role_id = r.id
             WHERE wu.user_id = ? AND wu.warehouse_id = ?
            """,
            (g.get("tenant_id") or 1, g.user["id"], g.warehouse["id"]),
        ).fetchall()
        return frozenset(row[0] for row in rows)
    except Exception as exc:  # noqa: BLE001 — fail-closed by design
        current_app.logger.warning("[permissions] resolve failed: %s", exc)
        return frozenset()


def has_permission(*codes: str) -> bool:
    """Non-decorator query: True iff the user holds every code.

    Platform superadmins always pass. Safe to call from handlers and templates
    (the Phase-2 UI exposes it as the `can()` helper).
    """
    user = g.get("user")
    if user is None:
        return False
    if user["is_admin"]:
        return True
    return set(codes) <= current_permissions()


def current_role() -> str | None:
    """Return the role of the logged-in user on the current warehouse, or None."""
    if g.user is None:
        return None
    if g.get("role") is None:
        return None
    return g.role["role"]


def require_role(min_role: str) -> Callable:
    """Block the request unless the user has at least <min_role> on the current warehouse.

    Platform admins (g.user['is_admin']) pass through.
    """
    def decorator(view: Callable) -> Callable:
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            if g.user is None:
                return redirect(url_for("auth.login", next=request.path))
            if g.user["is_admin"]:
                return view(*args, **kwargs)
            role = current_role()
            if role is None:
                abort(403)
                return None  # for type checkers
            if ROLE_RANK.get(role, 0) < ROLE_RANK.get(min_role, 0):
                flash(f"需要 {min_role} 及以上权限")
                abort(403)
                return None
            return view(*args, **kwargs)
        return wrapped
    return decorator


def require_platform_admin(view: Callable) -> Callable:
    """Block the request unless the user is a platform admin (users.is_admin=1).

    Use this for routes that ONLY platform admins should touch, regardless of
    per-warehouse role. Unlike require_role, this does NOT bypass on the
    is_admin flag — it requires it.
    """
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("auth.login", next=request.path))
        if not g.user["is_admin"]:
            flash("需要平台管理员权限")
            abort(403)
            return None
        return view(*args, **kwargs)
    return wrapped


def require_login(view: Callable) -> Callable:
    """Block the request unless the user is logged in AND has a warehouse selected.

    Views whose __name__ is in WAREHOUSE_EXEMPT can run with just login.
    """
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("auth.login", next=request.path))
        if view.__name__ not in WAREHOUSE_EXEMPT and g.get("warehouse_db_path") is None:
            return redirect(url_for("auth.warehouse_picker"))
        return view(*args, **kwargs)
    return wrapped


def require_warehouse_type(*allowed: str) -> Callable:
    """Block the request unless the current warehouse's type is in `allowed`.

    Example:
        @require_warehouse_type("rd")
        def ic_recipes_list(): ...

    Platform admins (`g.user['is_admin']`) bypass the check — useful for testing
    in dev. Otherwise an RD-only or storefront-only route 403s when the user is
    on the wrong kind of warehouse.
    """
    def decorator(view: Callable) -> Callable:
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            if g.user is None:
                return redirect(url_for("auth.login", next=request.path))
            wh = g.get("warehouse")
            if wh is None:
                return redirect(url_for("auth.warehouse_picker"))
            if g.user["is_admin"]:
                return view(*args, **kwargs)
            wh_type = wh["warehouse_type"] if hasattr(wh, "keys") else wh["warehouse_type"]
            if wh_type not in allowed:
                flash(f"当前仓库类型为 {wh_type}，无权访问该功能")
                abort(403)
                return None
            return view(*args, **kwargs)
        return wrapped
    return decorator


def storefront_only(view: Callable) -> Callable:
    """Decorator: 403 unless the current warehouse is a storefront (has inventory ops).

    Used by stocktake / restock / outbound / items / production modules to hide
    them in 研发中心 (RD), where there is no physical inventory to move.

    Equivalent to `@require_warehouse_type("storefront")` but expressed as the
    domain concept rather than a type enum value, so call sites read cleaner.
    """
    return require_warehouse_type("storefront")(view)


def distribution_center_only(view: Callable) -> Callable:
    """Decorator: 403 unless the current warehouse is a distribution center.

    Used by store-ordering DC-side routes (review / ship) to express the
    domain concept rather than the raw type string.
    """
    return require_warehouse_type("distribution_center")(view)


# ---------------------------------------------------------------------------
# Permission-point guards (Phase 3)
# ---------------------------------------------------------------------------

def _guard_prelude(view: Callable) -> tuple[bool, object]:
    """Shared prologue for the permission-point decorators.

    Returns `(allowed, response)`:
      - `(True, None)`            -> proceed to the view
      - `(False, <resp>)`         -> return <resp> (redirect or 403)

    Ordering mirrors `require_role` so existing users see zero behaviour change:
    login -> platform-admin bypass -> warehouse-selected -> permission check.
    """
    if g.get("user") is None:
        return False, redirect(url_for("auth.login", next=request.path))
    if g.user["is_admin"]:
        return True, None
    if view.__name__ not in WAREHOUSE_EXEMPT and g.get("warehouse_db_path") is None:
        return False, redirect(url_for("auth.warehouse_picker"))
    return True, None


def _warn_if_no_codes(kind: str, view: Callable, codes: tuple[str, ...]) -> None:
    """An empty code list is a call-site bug; log it (but keep it login-only)."""
    if not codes:
        current_app.logger.warning(
            "[permissions] %s() on %s called with no codes; "
            "treating as login-only guard",
            kind, view.__name__,
        )


def require_permission(*codes: str) -> Callable:
    """Block the request unless the user holds ALL `codes` (AND).

    Semantics aligned with `require_role`:
      1. not logged in            -> redirect to login
      2. platform superadmin      -> pass (is_admin bypass)
      3. no warehouse + non-exempt view -> redirect to warehouse picker
      4. empty `codes`            -> login-only (warned as a call-site bug)
      5. perms superset of codes  -> pass
      6. otherwise                -> flash + 403
    """
    def decorator(view: Callable) -> Callable:
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            allowed, resp = _guard_prelude(view)
            if not allowed:
                return resp
            _warn_if_no_codes("require_permission", view, codes)
            if codes and not set(codes) <= current_permissions():
                flash(f"需要权限：{'/'.join(codes)}")
                abort(403)
                return None
            return view(*args, **kwargs)
        return wrapped
    return decorator


def require_any_permission(*codes: str) -> Callable:
    """Block the request unless the user holds ANY of `codes` (OR).

    Same prologue as `require_permission`; used for "owner OR elevated" cases
    such as cancelling an order (`order.create` owner vs `order.cancel.any`).
    Empty `codes` degrades to a login-only guard (warned).
    """
    def decorator(view: Callable) -> Callable:
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            allowed, resp = _guard_prelude(view)
            if not allowed:
                return resp
            _warn_if_no_codes("require_any_permission", view, codes)
            if codes and not (set(codes) & current_permissions()):
                flash(f"需要权限：{'/'.join(codes)}")
                abort(403)
                return None
            return view(*args, **kwargs)
        return wrapped
    return decorator

