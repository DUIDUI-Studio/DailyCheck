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
    from tests.conftest import _seed_item, _wh
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
    rollback = _wh(wh_path).execute(
        """SELECT action, delta FROM stock_movements
           WHERE item_id=? AND action='补货删除回滚'""",
        (item_id,),
    ).fetchall()
    assert len(rollback) == 1
    assert rollback[0]["delta"] == pytest.approx(-2.0)