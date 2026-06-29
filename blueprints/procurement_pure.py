"""Pure functions for the /procurement blueprint.

Spec: docs/superpowers/specs/2026-06-29-procurement-design.md §0.
PRD: §2.2 (procurement suggestions, safety stock formula).
"""
from __future__ import annotations

from decimal import Decimal
import math
from typing import TypedDict


class StoreItem(TypedDict):
    item_id: int
    item_name: str
    suggested_qty: int


class StoreReport(TypedDict):
    warehouse_code: str
    items: list[StoreItem]


class HubEntry(TypedDict):
    item_id: int
    item_name: str
    total_suggested_qty: int
    stores_needing: int
    stores_detail: list[dict]


def compute_safety_stock(daily_avg: float, cover_days: int, min_absolute: float) -> float:
    """PRD §2.2.3: safety_stock = max(daily_avg * cover_days, min_absolute)."""
    raw = float(Decimal(str(daily_avg)) * Decimal(str(cover_days)))
    return float(Decimal(str(max(raw, min_absolute))).quantize(Decimal('0.01')))


def compute_suggested_qty(safety_stock: float, current_qty: float, in_transit_qty: float) -> int:
    """PRD §2.2.3: ceil(max(0, safety_stock - current_qty - in_transit_qty))."""
    raw = safety_stock - current_qty - in_transit_qty
    if raw <= 0:
        return 0
    return int(math.ceil(raw))


def aggregate_hub(store_reports: list[StoreReport]) -> list[HubEntry]:
    """Aggregate per-store suggestions into a hub-level view.

    Per PRD §2.2.2:
      total_suggested_qty = sum across stores
      stores_needing      = count of stores with suggested_qty > 0
      stores_detail       = [(warehouse_code, suggested_qty), ...]

    Returned list is sorted by total_suggested_qty DESC so the highest-
    demand items surface first.
    """
    by_item: dict[int, dict] = {}
    for rep in store_reports:
        for it in rep.get("items", []):
            iid = it["item_id"]
            entry = by_item.setdefault(iid, {
                "item_id": iid,
                "item_name": it["item_name"],
                "total_suggested_qty": 0,
                "stores_needing": 0,
                "stores_detail": [],
            })
            sq = it["suggested_qty"]
            entry["total_suggested_qty"] += sq
            if sq > 0:
                entry["stores_needing"] += 1
            entry["stores_detail"].append({
                "warehouse_code": rep["warehouse_code"],
                "suggested_qty": sq,
            })
    out = list(by_item.values())
    out.sort(key=lambda e: e["total_suggested_qty"], reverse=True)
    return out
