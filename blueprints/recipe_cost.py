"""Recipe cost: ice-cream recipes + serving recipes with cost/margin."""
from __future__ import annotations

from flask import Blueprint, redirect, url_for

from permissions import require_login


bp = Blueprint("recipe_cost", __name__)


@bp.route("/recipe-cost/")
@require_login
def landing():
    """入口：跳到冰激凌配方列表。"""
    return redirect(url_for("recipe_cost.ic_recipes_list"))
