"""Store ordering blueprint (placeholder shell for T01 registration).

Routes will be implemented in T03 / T04.
"""
from __future__ import annotations

from flask import Blueprint, render_template_string

bp = Blueprint("store_ordering", __name__, url_prefix="/store-ordering")


@bp.route("/catalog")
def catalog() -> str:
    """Placeholder for T03 catalog page so base.html nav link resolves."""
    return render_template_string("<p>门店订货目录页占位（T03 实现）</p>")
