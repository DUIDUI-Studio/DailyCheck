"""Role-based access control.

Each user gets a per-warehouse role (staff / manager / admin).
Platform admins (users.is_admin=1) bypass warehouse checks.
"""
from __future__ import annotations

import functools
from collections.abc import Callable

from flask import abort, flash, g, redirect, request, url_for

from config import ROLE_RANK

# View function names that are allowed to run without a warehouse selected.
# All others are redirected to the picker.
WAREHOUSE_EXEMPT = {"warehouse_picker", "warehouse_select"}


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

