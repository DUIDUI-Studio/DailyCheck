"""Shared helpers used by business blueprints.

Each blueprint assumes g.user / g.warehouse / g.role are populated by
blueprints/auth.py's before_app_request hook.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation

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


def warehouse_categories_in_clause() -> tuple[str, list[str]]:
    """按当前仓库自身的 categories 表生成 IN 子句。

    让每家店用自己的品类集合。极端情况(仓库无 categories)
    返回 ("1", [0]) 防御性 0 行。
    """
    db = get_warehouse_db()
    names = [r["name"] for r in db.execute(
        "SELECT name FROM categories ORDER BY id"
    ).fetchall()]
    if not names:
        return "1", [0]
    return ",".join("?" for _ in names), names


# 向后兼容别名:旧 fixed_categories_in_clause 已废弃,统一改读仓库自身 categories。
# Deprecated: use warehouse_categories_in_clause directly.
fixed_categories_in_clause = warehouse_categories_in_clause


def gen_sku() -> str:
    return f"AUTO-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"


# Quantities are decimal with 2 dp. Float would be lossy
# (e.g. 0.1 + 0.2 = 0.30000000000000004); Decimal preserves precision.
def parse_qty(raw) -> float:
    """Parse a quantity value (str | int | float | None) as float with 2 dp.

    Uses Decimal internally to avoid float-string rounding traps
    (0.1 + 0.2 = 0.30000000000000004) and emits float() so sqlite3
    accepts the value as a parameter binding. The 2-dp quantize keeps
    "1.55" from becoming 1.5500000000000003.

    Returns 0.0 on empty / None / invalid input — callers decide
    whether to treat 0 as a no-op (skip) or an explicit value.
    Negative numbers are preserved — the caller is expected to
    validate non-negativity (or not) before/after calling.
    """
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        try:
            return float(Decimal(str(raw)).quantize(Decimal('0.01')))
        except InvalidOperation:
            return 0.0
    s = str(raw).strip()
    if not s:
        return 0.0
    try:
        v = Decimal(s).quantize(Decimal('0.01'))
    except InvalidOperation:
        return 0.0
    return float(v)


def grams_to_stock(grams: float, gram_per_unit: float) -> float:
    """克 → 库存单位。

    gram_per_unit<=0 表示该物品未启用克换算，原样返回入参
    （此时调用方传入的本就是库存单位量）。否则做 grams/gram_per_unit
    的除法并量化到 2 位小数（与系统其余数量精度一致）。
    """
    if gram_per_unit <= 0:
        return float(Decimal(str(grams)).quantize(Decimal('0.01')))
    return float(
        (Decimal(str(grams)) / Decimal(str(gram_per_unit)))
        .quantize(Decimal('0.01'))
    )


def fmt_qty(value) -> str:
    """Format a quantity for display: trim trailing zeros but keep
    up to 2 decimal places. Decimal('1.50') → '1.5' (but display
    as '1.50' if we wanted). Returns '0' for None / zero."""
    if value is None:
        return '0'
    d = Decimal(str(value)).quantize(Decimal('0.01'))
    s = str(d)
    # '1.50' -> '1.5'; '1.00' -> '1'
    if '.' in s:
        s = s.rstrip('0').rstrip('.')
    return s or '0'


def render(template: str, **context):
    """Pass common context (current warehouse / user / role) implicitly."""
    ctx = dict(context)
    if "warehouse" not in ctx and g.get("warehouse") is not None:
        ctx["warehouse"] = g.warehouse
    if "user" not in ctx and g.get("user") is not None:
        ctx["user"] = g.user
    return render_template(template, **ctx)


def register_jinja_filters(app) -> None:
    """Expose fmt_qty and fmt_money to Jinja templates."""
    app.jinja_env.filters["fmt_qty"] = fmt_qty
    app.jinja_env.filters["fmt_money"] = fmt_money


def register_template_context(app) -> None:
    """Expose current_role and g.user/is_admin to every template."""
    @app.context_processor
    def _inject():
        role = g.get("role")
        return {
            "current_role": role["role"] if role else None,
        }


def fmt_money(value) -> str:
    """Render a money value with thousands separator and 2 dp."""
    if value is None:
        return "0.00"
    try:
        d = Decimal(str(value)).quantize(Decimal('0.01'))
    except InvalidOperation:
        return "0.00"
    return f"{d:,.2f}"
