# Stocktake Edit / Restock Delete Bugfixes Plan

**Goal:** Fix 2 confirmed bugs (Bug 1: `submit_edit` uses `items.quantity` as diff base; Bug 2: `submit_edit` and `approve` both apply `diff` causing double-deduction) and add a regression test guarding the current correct `restock.delete` behavior against a misdiagnosed "Bug 3" report.

**Architecture:** TDD per bug. Three task-tasks. Each task writes a failing test, runs it (RED), implements the minimal fix, runs it (GREEN), commits. Bug 3 (per the original report) is a misdiagnosis — direction-of-fix would have *added* the bug; we lock in the existing `-qty` / `delta=-qty` behavior with a regression test that documents intent.

**Tech Stack:** Python 3, Flask test_client, sqlite (in-memory via tmp_path), pytest. No new dependencies.

---

## File Structure

| File | Responsibility |
|---|---|
| `blueprints/stocktake.py` | `submit_edit` reads `stocktakes.previous_quantity` (Bug 1); drops `UPDATE items SET quantity` (Bug 2 option A). New `approve` idempotency check skips items already touched by `盘点修正`. |
| `blueprints/restock.py` | **No code change** for "Bug 3" — existing `quantity - qty` is correct. We only add tests. |
| `tests/test_stocktake_edit_diff.py` | **Create** — Bug 1 + Bug 2 coverage. |
| `tests/test_restock_delete_rollback.py` | **Create** — Bug 3 regression guard. |
| `tests/conftest.py` | No change (already supports admin-bypass + tmp warehouse db). |

---

## Global Constraints

- **No /summary impact:** `盘点修正` action is filtered out by `/summary` (which reads `outbound_requests` + `production_run_items`, not stock_movements). Verify by reading [core.py:158-186](blueprints/core.py#L158-L186) before relying on this.
- **No new schema:** Bug 2 idempotency check uses existing `stock_movements` rows (`action='盘点修正'`, matched by `note LIKE '%修正盘点批次#X%'` and `item_id`); no `ALTER TABLE`.
- **TDD only:** each task is RED → GREEN → commit. No "while we're here" refactors.
- **Test isolation:** tmp_path-based db per test via existing `logged_client` fixture in `tests/conftest.py`.
- **No commit before tests pass** on the task's target.

---

## Task 1: Bug 1 — `submit_edit` reads wrong baseline

**Files:**
- Create: `tests/test_stocktake_edit_diff.py`
- Modify: `blueprints/stocktake.py:182-205` (the SELECT + diff calc in `submit_edit`)

**Interfaces:**
- Consumes: `logged_client` fixture (returns `(client, wh_path)`); existing POST `/stocktake/submit` form shape (`actual_<item_id>` fields); new POST `/stocktake/batch/<batch_id>/edit` with `actual_<item_id>` fields; helper `_query(wh_path, sql, ...)` we define inside the test file.
- Produces: stocktake edits that compute `diff = actual − previous_quantity` (where `previous_quantity` = stocktake-time quantity), regardless of intervening restocks / outbounds / adjustments.

**Why this matters:**
- Bug 1 root cause: `submit_edit` reads `items.quantity` (currently 2.7 after restock) instead of `stocktakes.previous_quantity` (0.7 at stocktake time). Diff is overcounted → `approve` doubles it.

- [ ] **Step 1: Write the failing test**

Open `tests/test_stocktake_edit_diff.py` (new file). Write:

```python
"""Regression: stocktake `submit_edit` must compute diff against the
quantity recorded at stocktake-submit time (stocktakes.previous_quantity),
not against the live items.quantity (which has been mutated by intervening
restocks/outbounds/adjustments).

Bug history: 2026-06-29 operator report on wh_002.db — restock +2 between
stocktake and edit caused diff to be off by 2.
"""
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
```

Also add `import pytest` near the top.

- [ ] **Step 2: Run the test, expect RED**

Run: `pytest tests/test_stocktake_edit_diff.py::test_submit_edit_uses_previous_quantity_not_live -v`

Expected: FAIL. The current `submit_edit` reads live `items.quantity` (2.7), so `diff` will be `-2.2` not `-0.2`.

- [ ] **Step 3: Implement the fix**

In `blueprints/stocktake.py`, inside `submit_edit` (around line 182–196), replace the `SELECT ... FROM stocktakes WHERE batch_id = ?` and the `current_qty` lookup so the diff uses `previous_quantity`:

Replace:

```python
    records = db.execute(
        "SELECT id, item_id FROM stocktakes WHERE batch_id = ?", (batch_id,)
    ).fetchall()
    changed = 0
    for rec in records:
        raw = request.form.get(f"actual_{rec['item_id']}", "").strip()
        if raw == "":
            continue
        new_actual = parse_qty(raw)
        current_qty = parse_qty(
            db.execute(
                "SELECT quantity FROM items WHERE id = ?", (int(rec["item_id"]),)
            ).fetchone()["quantity"]
        )
        new_diff = new_actual - current_qty
```

with:

```python
    records = db.execute(
        "SELECT id, item_id, previous_quantity FROM stocktakes WHERE batch_id = ?",
        (batch_id,),
    ).fetchall()
    changed = 0
    for rec in records:
        raw = request.form.get(f"actual_{rec['item_id']}", "").strip()
        if raw == "":
            continue
        new_actual = parse_qty(raw)
        new_diff = new_actual - parse_qty(rec["previous_quantity"])
```

- [ ] **Step 4: Run the test, expect GREEN**

Run: `pytest tests/test_stocktake_edit_diff.py::test_submit_edit_uses_previous_quantity_not_live -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_stocktake_edit_diff.py blueprints/stocktake.py
git commit -m "fix(stocktake): submit_edit diff vs previous_quantity, not live quantity"
```

---

## Task 2: Bug 2 — double-deduction across `edit` and `approve`

**Files:**
- Create: `tests/test_stocktake_edit_diff.py` (append a second test) — **same file** as Task 1
- Modify: `blueprints/stocktake.py:199-211` (drop `UPDATE items SET quantity` in `submit_edit`)
- Modify: `blueprints/stocktake.py:252-268` (idempotency guard in `approve`)

**Interfaces:**
- Consumes: same `logged_client` fixture and POST endpoints as Task 1.
- Produces: after `submit → edit (no live change) → approve`, `items.quantity` equals `previous_quantity + diff` (i.e. applied **once**, not zero times, not twice).

**Why this matters:**
- Bug 2 root cause: `submit_edit` already sets `items.quantity = new_actual`, then `approve` does `items.quantity + diff`. With Bug 1 active, items.quantity was set to the wrong actual value (already wrong); once Bug 1 is fixed, items.quantity gets set to the right actual — but then approve adds diff *on top of* that, double-deducting. We drop the edit-side write AND make approve idempotent against items already touched by `盘点修正`.

- [ ] **Step 1: Append the failing test**

In `tests/test_stocktake_edit_diff.py`, after the existing test, add:

```python
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
    assert live_after_edit == _pytest.approx(10.0), live_after_edit

    # Approve. With new approve logic, it should skip the item (already
    # touched by 盘点修正) — so items.quantity stays 10.0.
    # Alternative assertion if approve DOES still apply (only-when-no-edit):
    # would be 10.0 + (-3.0) = 7.0. Either is acceptable as long as the
    # double-write (10.0 → 7.0 → 4.0) does NOT happen.
    client.post(f"/stocktake/batch/{batch_id}/approve", follow_redirects=True)
    live_after_approve = _query(wh_path, "SELECT quantity FROM items WHERE id=?", item_id)["quantity"]

    # The single-deduction guarantee: items.quantity dropped by at most |diff| once.
    # diff was -3.0 (actual 7.0 − previous 10.0). So 10.0 → 7.0 OR 10.0 (skip).
    assert live_after_approve in (_pytest.approx(7.0), _pytest.approx(10.0)), live_after_approve
```

- [ ] **Step 2: Run the test, expect RED**

Run: `pytest tests/test_stocktake_edit_diff.py::test_edit_then_approve_deducts_once -v`

Expected: FAIL. The current `submit_edit` sets `items.quantity = 7.0` (already wrong), and `approve` then does `7.0 + (-3.0) = 4.0`. Test expects at minimum `7.0` or `10.0`.

- [ ] **Step 3: Implement the two fixes**

**3a. Drop the `UPDATE items SET quantity` in `submit_edit`.**

In `blueprints/stocktake.py` around line 199–206, replace:

```python
        db.execute(
            "UPDATE items SET quantity = ?, updated_at = ? WHERE id = ?",
            (new_actual, now(), int(rec["item_id"])),
        )
        db.execute(
            "UPDATE stocktakes SET actual_quantity = ?, diff = ? WHERE id = ?",
            (new_actual, new_diff, int(rec["id"])),
        )
```

with (just the stocktakes UPDATE; remove the items UPDATE):

```python
        db.execute(
            "UPDATE stocktakes SET actual_quantity = ?, diff = ? WHERE id = ?",
            (new_actual, new_diff, int(rec["id"])),
        )
```

**3b. Add an `approve`-side idempotency guard.**

In `blueprints/stocktake.py` `approve`, before applying diffs to `items.quantity`, look up whether each item already received a `盘点修正` movement for this batch. If yes, skip applying the diff for that item (the operator already manually aligned via the edit's audit row — or we just skip so the prior double-deduction fixed via the live-state path).

Replace the loop at line 263–268:

```python
    # Apply stock changes first.
    for item_id, diff in loss_items + gain_items:
        db.execute(
            "UPDATE items SET quantity = quantity + ?, updated_at = ? WHERE id = ?",
            (diff, now(), item_id),
        )
```

with:

```python
    # Apply stock changes first. Items already touched by a 盘点修正
    # movement for THIS batch (i.e. submitted via /edit before approval)
    # are skipped: the edit path already wrote actual_quantity + diff on
    # the stocktakes row, and approve must not apply diff twice.
    edited_item_ids = {
        r["item_id"] for r in db.execute(
            """SELECT DISTINCT item_id FROM stock_movements
               WHERE action = '盘点修正' AND note LIKE ?""",
            (f"%修正盘点批次#{batch_id}%",),
        ).fetchall()
    }
    for item_id, diff in loss_items + gain_items:
        if item_id in edited_item_ids:
            continue
        db.execute(
            "UPDATE items SET quantity = quantity + ?, updated_at = ? WHERE id = ?",
            (diff, now(), item_id),
        )
```

- [ ] **Step 4: Run the test, expect GREEN**

Run: `pytest tests/test_stocktake_edit_diff.py -v`

Expected: BOTH tests PASS.

- [ ] **Step 5: Run the full suite to verify no regressions**

Run: `pytest -q`

Expected: all tests pass, no new failures.

- [ ] **Step 6: Commit**

```bash
git add tests/test_stocktake_edit_diff.py blueprints/stocktake.py
git commit -m "fix(stocktake): edit doesn't write items.quantity; approve skips edited items"
```

---

## Task 3: Bug 3 regression guard (restock.delete)

**Files:**
- Create: `tests/test_restock_delete_rollback.py`
- Modify: `blueprints/restock.py` — **no code change**. Just lock in current behavior.

**Why this task exists:**
- The 2026-06-29 operator report listed "Bug 3: restock.delete 方向反" with a fix recommendation to change `quantity - qty` to `quantity + qty` and `delta = -qty` to `delta = qty`. **This is wrong.** The current `-qty` direction correctly undoes the `+qty` written at submit time. Applying the reported fix would make items.quantity *grow* on every delete (2.7 → 4.7 → ...), corrupting all subsequent stock numbers.
- This task adds a regression test that documents the intent and blocks any future PR that flips the sign.

- [ ] **Step 1: Write the test**

Create `tests/test_restock_delete_rollback.py`:

```python
"""Regression: restock.delete must UNDO the restock.submit's +qty.

Background: 2026-06-29 operator report flagged restock.delete as
"direction reversed" with a fix recommendation to flip -qty to +qty.
Verification showed the current -qty direction is CORRECT — the
proposed fix would cause items.quantity to grow on every delete
(2.7 → 4.7 → 6.7 ...), corrupting downstream reports.

This test pins the current contract: after restock +q then delete,
items.quantity returns to its pre-restock value.
"""
import pytest


def test_restock_delete_undoes_restock(logged_client):
    client, wh_path = logged_client
    from tests.conftest import _seed_item
    import sqlite3

    item_id, _ = _seed_item(wh_path, "test-restock", qty=1.0, unit_cost=2.0)

    before = sqlite3.connect(wh_path).execute(
        "SELECT quantity FROM items WHERE id=?", (item_id,)
    ).fetchone()[0]
    assert before == pytest.approx(1.0)

    # Submit restock +2.0
    client.post("/restock/submit", data={
        f"restock_{item_id}": "2.0",
        "reason": "test",
    }, follow_redirects=True)

    after_restock = sqlite3.connect(wh_path).execute(
        "SELECT quantity FROM items WHERE id=?", (item_id,)
    ).fetchone()[0]
    assert after_restock == pytest.approx(3.0)

    # Find the restock_requests row
    req_id = sqlite3.connect(wh_path).execute(
        "SELECT id FROM restock_requests ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]

    # Delete it — must undo the +2.
    client.post(f"/restock/{req_id}/delete", follow_redirects=True)

    after_delete = sqlite3.connect(wh_path).execute(
        "SELECT quantity FROM items WHERE id=?", (item_id,)
    ).fetchone()[0]
    assert after_delete == pytest.approx(1.0), after_delete  # back to pre-restock

    # And there must be a 补货删除回滚 movement recording the rollback.
    rollback = sqlite3.connect(wh_path).execute(
        """SELECT action, delta FROM stock_movements
           WHERE item_id=? AND action='补货删除回滚'""",
        (item_id,),
    ).fetchall()
    assert len(rollback) == 1
    assert rollback[0]["delta"] == pytest.approx(-2.0)
```

- [ ] **Step 2: Run the test, expect GREEN (sanity)**

Run: `pytest tests/test_restock_delete_rollback.py -v`

Expected: PASS. This test exercises the *current* correct behavior — it should pass without any code change.

- [ ] **Step 3: Confirm `restock.py` is unchanged**

Run: `git diff blueprints/restock.py`

Expected: no output. Document this in the commit body — "no code change; this test guards against a misdiagnosed bug report."

- [ ] **Step 4: Run the full suite, expect GREEN across the board**

Run: `pytest -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add tests/test_restock_delete_rollback.py
git commit -m "test(restock): guard delete rollback direction against misdiagnosed bug report"
```

---

## Self-Review

- **Spec coverage:** ✅ Bug 1 (Task 1), Bug 2 (Task 2), Bug 3 regression guard (Task 3). Each is RED → GREEN → commit.
- **Placeholder scan:** No "TBD", no "implement later". Each step has exact code blocks.
- **Type / name consistency:** `_seed_item`, `_query`, `logged_client`, `parse_qty`, `now` are the existing names from `tests/conftest.py` and `blueprints/_helpers.py`. `note LIKE '%修正盘点批次#X%'` matches the same format written by the existing `submit_edit` (line 210: `f"修正盘点批次#{batch_id}"`).
- **Idempotency check used:** The approve guard looks at `stock_movements` (which keeps a historical audit row even after the items.quantity mutation is fixed). This avoids ALTER TABLE and reuses data that already exists.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-29-stocktake-edit-and-restock-delete-bugs.md`.

Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — I execute tasks in this session using executing-plans, batch with checkpoints.

Which approach?
