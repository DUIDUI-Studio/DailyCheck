"""Resolve exactly one fulfillment row in an explicitly supplied warehouse.

``warehouse_code`` is the caller's source snapshot: this module never looks up
current ``get_routing`` configuration. Submission callers resolve routing first;
shipment callers pass the order line's frozen source so later routing edits
cannot redirect an existing order. Connections belong to the caller.

The result's ``unit_cost`` is the original warehouse cost. Selling-price-first
order pricing is exposed separately by ``parse_price_and_multiple``; it follows
the existing cart rules in store_ordering_pure.list_cart_items (price/multiple).
No Flask context or warehouse connection factory is required.
"""
from __future__ import annotations

import sqlite3


class FulfillmentError(ValueError):
    """Diagnosable fulfillment failure, carrying both parts of the binding key."""

    def __init__(self, canonical_id: int, warehouse_code: str, reason: str) -> None:
        self.canonical_id = canonical_id
        self.warehouse_code = warehouse_code
        super().__init__(
            f"canonical_id={canonical_id}, warehouse_code={warehouse_code}: {reason}"
        )


class FulfillmentAmbiguousError(FulfillmentError):
    """More than one active non-alias row is bound to the canonical item."""


class FulfillmentMissingError(FulfillmentError):
    """The canonical item or its eligible warehouse row does not exist."""


class FulfillmentConflictError(FulfillmentError):
    """The binding has incomparable units or an unresolved blocking conflict."""


def parse_price_and_multiple(item_row) -> tuple[float, float]:
    """Use a positive selling price, otherwise cost; normalize the order multiple.

    Keep the cart's existing float conversion, fallback and rounding behavior.
    The local import reuses its normalization without creating a module import
    cycle when store_ordering_pure callers adopt this helper.
    """
    from blueprints.store_ordering_pure import (
        ORDER_MULTIPLE_UNLIMITED,
        normalize_order_multiple,
    )

    item = dict(item_row) if item_row is not None else None
    price = (
        float(item["selling_price"] or 0)
        if item and item["selling_price"] is not None and float(item["selling_price"]) > 0
        else (float(item["unit_cost"] or 0) if item else 0.0)
    )
    multiple = normalize_order_multiple(
        item.get("order_multiple") if item else ORDER_MULTIPLE_UNLIMITED
    )
    return price, multiple


def resolve_fulfillment_item(
    master_conn: sqlite3.Connection,
    wh_conn: sqlite3.Connection,
    *,
    canonical_id: int,
    warehouse_code: str,
) -> dict:
    """Resolve the unique active non-alias row, failing closed on unsafe bindings.

    Units must match exactly: this resolver does not invent a conversion for
    quantities or prices. Open unit-conversion/double-binding conflicts on this
    particular warehouse row remain blocking until explicitly resolved.
    Cursor-local row factories preserve both caller connections' configuration.
    """
    from blueprints.store_ordering_pure import normalize_order_multiple

    cursor = wh_conn.cursor()
    cursor.row_factory = sqlite3.Row
    candidates = cursor.execute(
        """SELECT id, unit, unit_cost, order_multiple, quantity
           FROM items WHERE canonical_id=? AND is_active=1 AND is_alias=0""",
        (canonical_id,),
    ).fetchall()
    if not candidates:
        raise FulfillmentMissingError(canonical_id, warehouse_code, "no active fulfillment row")
    if len(candidates) != 1:
        raise FulfillmentAmbiguousError(
            canonical_id, warehouse_code, f"{len(candidates)} active fulfillment rows"
        )
    item = candidates[0]

    cursor = master_conn.cursor()
    cursor.row_factory = sqlite3.Row
    canonical = cursor.execute(
        "SELECT unit FROM canonical_items WHERE id=?", (canonical_id,),
    ).fetchall()
    if not canonical:
        raise FulfillmentMissingError(canonical_id, warehouse_code, "canonical item missing")
    if not item["unit"] or item["unit"] != canonical[0]["unit"]:
        raise FulfillmentConflictError(
            canonical_id, warehouse_code,
            f"incomparable units: local={item['unit']!r}, canonical={canonical[0]['unit']!r}",
        )
    conflicts = cursor.execute(
        """SELECT id FROM canonical_conflicts
           WHERE canonical_id=? AND warehouse_code=? AND local_item_id=?
             AND status='open' AND conflict_type IN ('unit_conversion', 'double_bind')""",
        (canonical_id, warehouse_code, item["id"]),
    ).fetchall()
    if conflicts:
        raise FulfillmentConflictError(
            canonical_id, warehouse_code,
            f"unresolved blocking conflicts: {[row['id'] for row in conflicts]}",
        )
    return {
        "item_id": item["id"],
        "unit": item["unit"],
        "unit_cost": float(item["unit_cost"] or 0),
        "order_multiple": normalize_order_multiple(item["order_multiple"]),
        "available": float(item["quantity"] or 0),
    }
