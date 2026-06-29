"""Unit tests for the pure compute_publish_diff fn.

PRD §2.4.4 data contract: diff shape is
  {template_item_idx, item_name, status, existing_item_id, diff_fields}
where status ∈ {add, skip, conflict}.

Match key is item_name (template sets the canonical name; spec §2 stores
items by name within a warehouse's items table — name is the natural
identity for this feature, matching the rest of the project's mental
model where items are referred to by name).
"""
from __future__ import annotations

import pytest

from blueprints.publish_items_pure import compute_publish_diff


# ---------------------------------------------------------------------------
# add / skip / conflict / empty store
# ---------------------------------------------------------------------------


def test_diff_add_when_template_item_not_in_store():
    """Template has item that store doesn't → status='add'."""
    template = [
        {"name": "木樨子油", "unit_cost": 10.0, "unit": "件",
         "gram_per_unit": 100.0, "safety_stock": 5.0, "category": "调味酱"},
    ]
    store: list[dict] = []
    out = compute_publish_diff(template, store)
    assert len(out) == 1
    assert out[0]["status"] == "add"
    assert out[0]["item_name"] == "木樨子油"
    assert out[0]["template_item_idx"] == 0
    assert out[0].get("existing_item_id") is None
    assert out[0].get("diff_fields") == []


def test_diff_skip_when_template_item_matches_store_exactly():
    """Store item has identical fields → status='skip'."""
    template = [
        {"name": "白糖", "unit_cost": 5.0, "unit": "克",
         "gram_per_unit": 1.0, "safety_stock": 100.0, "category": "调味酱"},
    ]
    store = [
        {"id": 42, "name": "白糖", "unit_cost": 5.0, "unit": "克",
         "gram_per_unit": 1.0, "safety_stock": 100.0, "category": "调味酱"},
    ]
    out = compute_publish_diff(template, store)
    assert len(out) == 1
    assert out[0]["status"] == "skip"
    assert out[0]["item_name"] == "白糖"
    assert out[0]["template_item_idx"] == 0
    assert out[0].get("existing_item_id") == 42
    assert out[0].get("diff_fields") == []


def test_diff_conflict_when_field_differs():
    """Store item exists but unit_cost differs → conflict + diff_fields=['unit_cost']."""
    template = [
        {"name": "柠檬", "unit_cost": 8.0, "unit": "个",
         "gram_per_unit": 50.0, "safety_stock": 20.0, "category": "辅料"},
    ]
    store = [
        {"id": 7, "name": "柠檬", "unit_cost": 6.5, "unit": "个",
         "gram_per_unit": 50.0, "safety_stock": 20.0, "category": "辅料"},
    ]
    out = compute_publish_diff(template, store)
    assert len(out) == 1
    assert out[0]["status"] == "conflict"
    assert out[0]["existing_item_id"] == 7
    assert out[0]["diff_fields"] == ["unit_cost"]


def test_diff_empty_store_returns_all_adds():
    """Empty store → every template item is 'add'."""
    template = [
        {"name": "A", "unit_cost": 1.0, "unit": "件",
         "gram_per_unit": 0, "safety_stock": 0, "category": "辅料"},
        {"name": "B", "unit_cost": 2.0, "unit": "件",
         "gram_per_unit": 0, "safety_stock": 0, "category": "辅料"},
    ]
    out = compute_publish_diff(template, [])
    assert [o["status"] for o in out] == ["add", "add"]
    assert [o["item_name"] for o in out] == ["A", "B"]


# ---------------------------------------------------------------------------
# Additional sanity checks beyond the 4 mandated cases
# ---------------------------------------------------------------------------


def test_diff_conflict_multiple_fields():
    """When two fields differ, diff_fields contains both."""
    template = [
        {"name": "X", "unit_cost": 10.0, "unit": "件",
         "gram_per_unit": 100.0, "safety_stock": 5.0, "category": "辅料"},
    ]
    store = [
        {"id": 9, "name": "X", "unit_cost": 9.0, "unit": "克",
         "gram_per_unit": 100.0, "safety_stock": 5.0, "category": "辅料"},
    ]
    out = compute_publish_diff(template, store)
    assert out[0]["status"] == "conflict"
    assert sorted(out[0]["diff_fields"]) == ["unit", "unit_cost"]


def test_diff_template_order_preserved():
    """Result list preserves the order of template_items."""
    template = [
        {"name": "z", "unit_cost": 1.0, "unit": "件",
         "gram_per_unit": 0, "safety_stock": 0, "category": "辅料"},
        {"name": "a", "unit_cost": 2.0, "unit": "件",
         "gram_per_unit": 0, "safety_stock": 0, "category": "辅料"},
        {"name": "m", "unit_cost": 3.0, "unit": "件",
         "gram_per_unit": 0, "safety_stock": 0, "category": "辅料"},
    ]
    store = [{"id": 1, "name": "m", "unit_cost": 3.0, "unit": "件",
              "gram_per_unit": 0, "safety_stock": 0, "category": "辅料"}]
    out = compute_publish_diff(template, store)
    assert [o["item_name"] for o in out] == ["z", "a", "m"]
    assert [o["status"] for o in out] == ["add", "add", "skip"]


def test_diff_mixed_add_skip_conflict():
    """Combined scenario: some add, some skip, some conflict."""
    template = [
        {"name": "new1", "unit_cost": 1.0, "unit": "件",
         "gram_per_unit": 0, "safety_stock": 0, "category": "辅料"},
        {"name": "same", "unit_cost": 5.0, "unit": "件",
         "gram_per_unit": 0, "safety_stock": 0, "category": "辅料"},
        {"name": "diff1", "unit_cost": 9.0, "unit": "件",
         "gram_per_unit": 0, "safety_stock": 0, "category": "辅料"},
    ]
    store = [
        {"id": 11, "name": "same", "unit_cost": 5.0, "unit": "件",
         "gram_per_unit": 0, "safety_stock": 0, "category": "辅料"},
        {"id": 12, "name": "diff1", "unit_cost": 8.0, "unit": "件",
         "gram_per_unit": 0, "safety_stock": 0, "category": "辅料"},
    ]
    out = compute_publish_diff(template, store)
    by_name = {o["item_name"]: o for o in out}
    assert by_name["new1"]["status"] == "add"
    assert by_name["same"]["status"] == "skip"
    assert by_name["diff1"]["status"] == "conflict"
    assert by_name["diff1"]["diff_fields"] == ["unit_cost"]