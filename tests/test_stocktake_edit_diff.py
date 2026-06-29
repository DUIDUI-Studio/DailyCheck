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
    client.post(f"/stocktake/batch/{batch_id}/approve", follow_redirects=True)
    live_after_approve = _query(wh_path, "SELECT quantity FROM items WHERE id=?", item_id)["quantity"]

    # Edit → approve contract:
    #   - submit_edit does NOT touch items.quantity.
    #   - approve sees the 盘点修正 movement, skips applying diff,
    #     skips writing synthetic outbound_request.
    # Result: items.quantity unchanged across submit → edit → approve.
    assert live_after_approve == pytest.approx(10.0), live_after_approve


def test_approve_idempotency_guards_exact_batch_only(logged_client):
    """Regression: approve's `edited_item_ids` guard must scope to the
    EXACT batch_id being approved, not by substring matching the
    movement note.

    Bug: the old guard queried `note LIKE '%修正盘点批次#X%'`. SQLite
    LIKE wildcards on both sides mean `#X` substring matches `#X`,
    `#X0`, `#X1`, ... `#XY`. So approving batch X also incorrectly
    tags items as "edited" when those items were edited by a
    different batch whose ID contains #X as a substring (e.g.
    approving batch 9 would falsely skip items edited by batch 99).
    The skipped item then never receives batch 9's diff, silently
    corrupting items.quantity.

    Fix: join stocktakes (exact batch_id + item_id) instead of
    matching the note. Now approving batch 9 only skips items
    actually edited by batch 9; items edited by batch 99 still
    receive batch 9's apply normally.
    """
    from tests.conftest import _seed_item, _wh
    from datetime import datetime
    client, wh_path = logged_client

    item_id, _ = _seed_item(wh_path, "collision-item", qty=100.0, unit_cost=1.0)

    # Force-insert two stocktake batches with IDs 9 and 99 via raw SQL
    # so the LIKE `#9` substring collision is real on both notes.
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _wh(wh_path)
    # Pad auto-increment by inserting 8 throwaway batches first, so the
    # next one is ID 9. Then 89 more pads, so the next is ID 99.
    for i in range(8):
        conn.execute(
            "INSERT INTO stocktake_batches (created_at, note, status, rolled_back) "
            "VALUES (?, ?, 'pending', 0)",
            (ts, f"pad-{i}"),
        )
    conn.execute(
        "INSERT INTO stocktake_batches (id, created_at, note, status, rolled_back) "
        "VALUES (9, ?, 'batch-9', 'pending', 0)", (ts,))
    for i in range(89):
        conn.execute(
            "INSERT INTO stocktake_batches (created_at, note, status, rolled_back) "
            "VALUES (?, ?, 'pending', 0)",
            (ts, f"pad-after-9-{i}"),
        )
    conn.execute(
        "INSERT INTO stocktake_batches (id, created_at, note, status, rolled_back) "
        "VALUES (99, ?, 'batch-99', 'pending', 0)", (ts,))
    # Two stocktakes rows for the SAME item_id, each on a different batch.
    # batch 9: previous=100, actual=80 → diff=-20 (a loss).
    # batch 99: previous=100, actual=70 → diff=-30 (a loss).
    conn.execute(
        "INSERT INTO stocktakes "
        "(item_id, previous_quantity, actual_quantity, diff, batch_id, created_at, note) "
        "VALUES (?, 100.0, 80.0, -20.0, 9, ?, 'batch-9')",
        (item_id, ts),
    )
    conn.execute(
        "INSERT INTO stocktakes "
        "(item_id, previous_quantity, actual_quantity, diff, batch_id, created_at, note) "
        "VALUES (?, 100.0, 70.0, -30.0, 99, ?, 'batch-99')",
        (item_id, ts),
    )
    conn.commit()
    conn.close()

    # ONLY edit batch 99 (not batch 9). This means:
    #   - batch 9's stocktakes.diff stays at -20 (untouched by edit).
    #   - batch 99's stocktakes.diff was already -30 at submit.
    #   - the 盘点修正 movement was written for batch 99 (note contains '#99'
    #     which is also '#9' substring).
    client.post("/stocktake/batch/99/edit", data={
        f"actual_{item_id}": "75.0",  # actual changes → triggers movement
    }, follow_redirects=True)

    # Approve batch 9 (which was NOT edited).
    # With the OLD LIKE-based guard: scan looks for note LIKE
    # '%修正盘点批次#9%', which matches BOTH the batch-9 note AND the
    # batch-99 note (since '#99' contains '#9'). Result: the item
    # is wrongly tagged as edited for batch 9, batch 9's diff -20
    # is NOT applied, items.quantity stays at 100.0 instead of 80.0.
    # With the NEW join-based guard: scan only finds items where
    # stocktakes.batch_id = 9. Batch 99's note doesn't matter.
    # Result: batch 9's diff -20 IS applied, items.quantity becomes 80.0.
    client.post("/stocktake/batch/9/approve", follow_redirects=True)

    conn = _wh(wh_path)
    live = conn.execute(
        "SELECT quantity FROM items WHERE id = ?", (item_id,)
    ).fetchone()["quantity"]
    conn.close()

    # The decisive assertion: items.quantity must drop by batch 9's
    # diff (-20), so 100.0 → 80.0. The OLD LIKE bug would have left
    # it at 100.0 because the false-positive guard skipped batch 9's apply.
    assert live == pytest.approx(80.0), (
        f"approve batch 9 must apply its diff (-20) to items.quantity; "
        f"if guard wrongly matches batch 99's 盘点修正 note (LIKE '#9' "
        f"collides with '#99'), batch 9's apply is skipped. live={live}"
    )
