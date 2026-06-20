"""Production: products, BOM, runs, rollback, delete, CSV export."""
from __future__ import annotations

from flask import Blueprint

from permissions import require_login
from ._helpers import render


bp = Blueprint("production", __name__)


@bp.route("/production", methods=["GET"])
@require_login
def products_list():
    return render("production/products.html", products=[])
