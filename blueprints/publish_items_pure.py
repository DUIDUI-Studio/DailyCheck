"""Pure functions for the /admin/publish/items blueprint.

Spec: docs/superpowers/specs/2026-06-29-item-publish-design.md
PRD : §2.4

Lives in its own module so unit tests can import without pulling in
Flask or the db layer. The blueprint (blueprints/publish_items.py)
wires these to routes and adds I/O.
"""
from __future__ import annotations

from typing import Any


# The fields the diff cares about (PRD §2.4.4 item shape).
# Order matters: diff_fields are returned in this order so the UI
# shows them deterministically.
_DIFF_FIELDS = ("category", "unit", "unit_cost", "gram_per_unit", "safety_stock")


def _index_store(store_items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Map item_name → store_item row for fast lookup."""
    return {row["name"]: row for row in store_items}


def compute_publish_diff(
    template_items: list[dict[str, Any]],
    store_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compute per-item publish diff for one warehouse.

    For each template item (in order):
      - not in store → status='add'
      - in store and all DIFF_FIELDS equal → status='skip'
      - in store but any field differs → status='conflict' + diff_fields list

    Output shape (PRD §2.4.4):
      {template_item_idx, item_name, status, existing_item_id, diff_fields}
    where existing_item_id is the store item's id for skip/conflict, and
    None for add. diff_fields is [] for add and skip.
    """
    by_name = _index_store(store_items)
    out: list[dict[str, Any]] = []
    for idx, t in enumerate(template_items):
        name = t["name"]
        row = by_name.get(name)
        if row is None:
            out.append({
                "template_item_idx": idx,
                "item_name": name,
                "status": "add",
                "existing_item_id": None,
                "diff_fields": [],
            })
            continue

        diff_fields: list[str] = []
        for field in _DIFF_FIELDS:
            t_val = t.get(field)
            r_val = row.get(field)
            # Numeric fields: compare as floats (handle int|float|Decimal).
            # String fields (category, unit): compare as-is.
            if field in ("unit_cost", "gram_per_unit", "safety_stock"):
                if float(t_val or 0) != float(r_val or 0):
                    diff_fields.append(field)
            else:
                if t_val != r_val:
                    diff_fields.append(field)
        if diff_fields:
            out.append({
                "template_item_idx": idx,
                "item_name": name,
                "status": "conflict",
                "existing_item_id": row["id"],
                "diff_fields": diff_fields,
            })
        else:
            out.append({
                "template_item_idx": idx,
                "item_name": name,
                "status": "skip",
                "existing_item_id": row["id"],
                "diff_fields": [],
            })
    return out