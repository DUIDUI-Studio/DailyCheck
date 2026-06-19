"""Role-based access control.

Each user gets a per-warehouse role (staff / manager / admin).
Platform admins (users.is_admin=1) bypass warehouse checks.
"""
from __future__ import annotations

import functools
from typing import Callable

from flask import abort, g, redirect, request, url_for, flash

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

