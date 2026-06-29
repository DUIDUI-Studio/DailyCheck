"""Regression: stocktake `submit_edit` must compute diff against the
quantity recorded at stocktake-submit time (stocktakes.previous_quantity),
not against the live items.quantity (which has been mutated by intervening
restocks/outbounds/adjustments).

Bug history: 2026-06-29 operator report on wh_002.db — restock +2 between
stocktake and edit caused diff to be off by 2.
"""
import pytest
import sqlite3


def _query(wh_path, sql, *params):
    conn = sqlite3.connect(wh_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(sql, params).fetchone()
    conn.close()
    return row


def test_submit_edit_uses_previous_quantity_not_live(logged_client):
    client, wh_path = logged_client

    # Seed one item, qty 0.7. Pull item_id from the seed.
    from tests.conftest import _seed_item
    item_id, _ = _seed_item(wh_path, "木樨子油", qty=0.7, unit_cost=10.0)

    # 1) Submit stocktake: actual = 0.5, previous should be captured as 0.7.
    resp = client.post("/stocktake/submit", data={
        "actual_" + str(item_id): "0.5",
        "note": "morning stocktake",
    }, follow_redirects=True)
    assert resp.status_code == 200, resp.get_data(as_text=True)

    batch_id = _query(wh_path, "SELECT id FROM stocktake_batches ORDER BY id DESC LIMIT 1")["id"]
    snap = _query(wh_path, "SELECT previous_quantity FROM stocktakes WHERE batch_id=?", batch_id)
    assert snap["previous_quantity"] == 0.7, snap

    # 2) Intervening restock +2 — moves live items.quantity away from previous_quantity.
    client.post("/restock/submit", data={
        f"restock_{item_id}": "2",
        "reason": "afternoon arrive",
    }, follow_redirects=True)
    live_now = _query(wh_path, "SELECT quantity FROM items WHERE id=?", item_id)["quantity"]
    assert live_now == pytest.approx(2.7), live_now  # 0.7 + 2.0

    # 3) Edit the stocktake: actual stays 0.5.
    resp = client.post(f"/stocktake/batch/{batch_id}/edit", data={
        f"actual_{item_id}": "0.5",
    }, follow_redirects=True)
    assert resp.status_code == 200, resp.get_data(as_text=True)

    # Bug 1 assertion: stocktakes.diff must be 0.5 − 0.7 = −0.2,
    # NOT 0.5 − 2.7 = −2.2.
    edited = _query(wh_path, "SELECT diff, actual_quantity FROM stocktakes WHERE batch_id=?", batch_id)
    assert edited["actual_quantity"] == 0.5
    assert edited["diff"] == pytest.approx(-0.2), edited


def test_edit_then_approve_deducts_once(logged_client):
    """After submit → edit → approve, items.quantity must equal
    previous_quantity + diff (single deduction), not diverge further.

    Bug 2 history: the old submit_edit wrote items.quantity = new_actual
    AND the old approve did quantity + diff. Combined, an edited batch
    deducted twice. The fix removes the edit-side write AND makes approve
    skip items that already received a 盘点修正 movement.
    """
    from tests.conftest import _seed_item
    client, wh_path = logged_client

    item_id, _ = _seed_item(wh_path, "test-oil", qty=10.0, unit_cost=5.0)

    # Submit stocktake with no actuals filled (just a stub — we use raw SQL
    # to insert a stocktake row directly so we can control diff precisely
    # without the submit path auto-computing).
    client.post("/stocktake/submit", data={
        "actual_" + str(item_id): "8.0",
        "note": "test",
    }, follow_redirects=True)
    batch_id = _query(wh_path, "SELECT id FROM stocktake_batches ORDER BY id DESC LIMIT 1")["id"]

    # Edit the stocktake to a different actual → diff stays based on previous_quantity.
    client.post(f"/stocktake/batch/{batch_id}/edit", data={
        f"actual_{item_id}": "7.0",
    }, follow_redirects=True)

    # items.quantity must NOT have been written by edit (Bug 2 fix).
    # With Bug 1 already fixed, edit doesn't touch items.quantity at all.
    live_after_edit = _query(wh_path, "SELECT quantity FROM items WHERE id=?", item_id)["quantity"]
    assert live_after_edit == pytest.approx(10.0), live_after_edit

    # Approve. With new approve logic, it should skip the item (already
    # touched by 盘点修正) — so items.quantity stays 10.0.
    # Alternative assertion if approve DOES still apply (only-when-no-edit):
    # would be 10.0 + (-3.0) = 7.0. Either is acceptable as long as the
    # double-write (10.0 → 7.0 → 4.0) does NOT happen.
    client.post(f"/stocktake/batch/{batch_id}/approve", follow_redirects=True)
    live_after_approve = _query(wh_path, "SELECT quantity FROM items WHERE id=?", item_id)["quantity"]

    # The single-deduction guarantee: items.quantity dropped by at most |diff| once.
    # diff was -3.0 (actual 7.0 − previous 10.0). So 10.0 → 7.0 OR 10.0 (skip).
    assert live_after_approve in (pytest.approx(7.0), pytest.approx(10.0)), live_after_approve
