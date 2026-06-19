"""Shared helpers used by business blueprints.

Each blueprint assumes g.user / g.warehouse / g.role are populated by
blueprints/auth.py's before_app_request hook.
"""
from __future__ import annotations

from datetime import datetime

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from werkzeug.security import generate_password_hash

from db import get_warehouse_db


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_db():
    """Backward-compat shim: the legacy code called get_db() inside views."""
    return get_warehouse_db()


def fixed_category_ids() -> list[int]:
    """Return ids of the four system-fixed categories for this warehouse."""
    db = get_warehouse_db()
    rows = db.execute(
        "SELECT id, name FROM categories ORDER BY id"
    ).fetchall()
    return [r["id"] for r in rows]


def fixed_categories_in_clause() -> tuple[str, list[str]]:
    """Build a parameterized IN clause over the four fixed categories.

    Returns (placeholder_sql, params). Use in SELECTs that should only
    surface the seeded categories (e.g. items list, category picker).
    """
    from config import FIXED_CATEGORIES
    placeholders = ",".join("?" for _ in FIXED_CATEGORIES)
    return placeholders, list(FIXED_CATEGORIES)


def gen_sku() -> str:
    return f"AUTO-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"


def render(template: str, **context):
    """Pass common context (current warehouse / user / role) implicitly."""
    ctx = dict(context)
    if "warehouse" not in ctx and g.get("warehouse") is not None:
        ctx["warehouse"] = g.warehouse
    if "user" not in ctx and g.get("user") is not None:
        ctx["user"] = g.user
    return render_template(template, **ctx)
