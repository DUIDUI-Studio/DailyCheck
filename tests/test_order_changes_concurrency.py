"""Independent SQLite connections, explicit barriers and actual persisted winners."""

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from blueprints import order_changes_pure as ocp
from blueprints import store_ordering_pure as sop
from tests import multi_warehouse_env as builder
from tests import test_order_changes_pure as domain
from tests.test_order_changes_pure import (
    commercial,
    intent_for,
    new_order,
    publish,
    replacement_intent,
    rows,
)

change_env = domain.change_env


def race(e, jobs):
    barrier = threading.Barrier(len(jobs))

    def run(job):
        with closing(builder.connect(e["master_path"])) as m:
            barrier.wait(10)
            try:
                return ("ok", job(m))
            except (ValueError, PermissionError) as exc:
                return ("denied", str(exc))

    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = [pool.submit(run, job) for job in jobs]
        return [future.result(timeout=20) for future in futures]


@pytest.mark.parametrize("same_key", [True, False])
def test_two_create_have_one_pending_and_one_notification_wave(change_env, same_key):
    e = change_env
    order = new_order(e)
    intent = replacement_intent(order)
    preview = ocp.preview_proposal(
        e["master"],
        order_id=order["id"],
        actor_id=4,
        current_warehouse_code="dc_alpha",
        intent=intent,
    )

    def job(key):
        return lambda m: ocp.create_proposal(
            m,
            order_id=order["id"],
            actor_id=4,
            current_warehouse_code="dc_alpha",
            request_key=key,
            intent=intent,
            seen_digest=preview["digest"],
        )

    results = race(e, [job("create-a"), job("create-a" if same_key else "create-b")])
    assert sum(result[0] == "ok" for result in results) == (2 if same_key else 1)
    assert len(rows(e["master"], "store_order_change_proposals")) == 1
    assert len(rows(e["master"], "store_order_change_events")) == 1
    proposed = [
        row
        for row in e["master"].execute(
            "SELECT user_id FROM notifications WHERE event_type='store_order_change_proposed'"
        )
    ]
    assert len(proposed) == len({row[0] for row in proposed}) > 0
    assert len(rows(e["master"], "store_order_change_commands")) == 1


@pytest.mark.parametrize("competitor", ["same-key", "new-key", "reject", "withdraw"])
def test_accept_competition_has_single_terminal_and_replacement(change_env, competitor):
    e = change_env
    order = new_order(e)
    p, _ = publish(e, order, replacement_intent(order))
    digest = ocp.payload_hash(p["snapshot"])

    def accept(m, key="accept"):
        return ocp.accept_proposal(
            m,
            order_id=order["id"],
            proposal_id=p["id"],
            actor_id=1,
            current_warehouse_code="store_one",
            request_key=key,
            seen_digest=digest,
            agree=True,
        )

    if competitor in ("same-key", "new-key"):

        def other(m):
            return accept(m, "accept" if competitor == "same-key" else "accept2")
    elif competitor == "reject":

        def other(m):
            return ocp.reject_proposal(
                m,
                order_id=order["id"],
                proposal_id=p["id"],
                actor_id=5,
                current_warehouse_code="store_one",
                request_key="reject",
                seen_digest=digest,
                note="不同意",
            )
    else:

        def other(m):
            return ocp.withdraw_proposal(
                m,
                order_id=order["id"],
                proposal_id=p["id"],
                actor_id=4,
                current_warehouse_code="dc_alpha",
                request_key="withdraw",
            )

    results = race(e, [accept, other])
    m = e["master"]
    final = ocp.proposal_detail(m, order["id"], p["id"])["status"]
    assert final in ("accepted", "rejected", "withdrawn")
    assert len(rows(m, "store_order_change_events")) == 2
    assert m.execute(
        "SELECT COUNT(*) FROM store_order_items WHERE replaces_order_item_id IS NOT NULL"
    ).fetchone()[0] == (1 if final == "accepted" else 0)
    assert sop.get_order_detail(m, order["id"])["change_revision"] == order["change_revision"] + (
        final == "accepted"
    )
    if competitor in ("same-key", "new-key"):
        assert all(result[0] == "ok" for result in results)
    else:
        assert sum(result[0] == "ok" for result in results) == 1
    notifications = rows(m, "notifications")
    identities = [(row[1], row[2], row[4]) for row in notifications]
    assert len(identities) == len(set(identities))


@pytest.mark.parametrize("first", ["create", "ship"])
def test_create_and_ship_both_commit_orders(change_env, first):
    e = change_env
    order = new_order(e)
    oid = order["id"]
    item = order["order_items"][0]["id"]
    intent = intent_for(order)
    preview = ocp.preview_proposal(
        e["master"], order_id=oid, actor_id=4, current_warehouse_code="dc_alpha", intent=intent
    )
    settled = threading.Event()

    def create(m):
        if first == "ship":
            assert settled.wait(10)
        try:
            return ocp.create_proposal(
                m,
                order_id=oid,
                actor_id=4,
                current_warehouse_code="dc_alpha",
                request_key="race-create",
                intent=intent,
                seen_digest=preview["digest"],
            )
        finally:
            if first == "create":
                settled.set()

    def ship(m):
        if first == "create":
            assert settled.wait(10)
        try:
            return sop.ship_partial(
                m,
                sop.open_warehouse_db,
                order_id=oid,
                operator_dc_code="dc_alpha",
                op_no="race-ship",
                line_qty={item: 2},
                shipped_by=2,
            )
        finally:
            if first == "ship":
                settled.set()

    result = race(e, [create, ship])
    m = e["master"]
    assert sum(entry[0] == "ok" for entry in result) == 1
    line = sop.get_order_detail(m, oid)["order_items"][0]
    assert line["shipped_quantity"] == (2 if first == "ship" else 0)
    assert len(rows(m, "store_order_change_proposals")) == (first == "create")
    with closing(builder.connect(e["paths"]["dc_alpha"])) as wh:
        assert wh.execute("SELECT quantity FROM items WHERE canonical_id=201").fetchone()[0] == (
            98 if first == "ship" else 100
        )


def test_ship_warehouse_commit_before_finalize_keeps_affected_occupied(change_env, monkeypatch):
    e = change_env
    order = new_order(e)
    oid = order["id"]
    item = order["order_items"][0]["id"]
    reached, release = threading.Event(), threading.Event()
    original = sop._apply_warehouse_shipment

    def pause(*args, **kwargs):
        value = original(*args, **kwargs)
        reached.set()
        assert release.wait(10)
        return value

    monkeypatch.setattr(sop, "_apply_warehouse_shipment", pause)

    def ship():
        with closing(builder.connect(e["master_path"])) as m:
            return sop.ship_partial(
                m,
                sop.open_warehouse_db,
                order_id=oid,
                operator_dc_code="dc_alpha",
                op_no="occupied",
                line_qty={item: 2},
                shipped_by=2,
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(ship)
        assert reached.wait(10)
        try:
            assert ocp.get_claim(e["master"], "occupied")["state"] == "active"
            before = commercial(e["master"], oid)
            with pytest.raises(ValueError, match="发货处理中"):
                publish(e, order)
            assert commercial(e["master"], oid) == before and not rows(
                e["master"], "store_order_change_proposals"
            )
            with closing(builder.connect(e["paths"]["dc_alpha"])) as wh:
                assert (
                    wh.execute("SELECT quantity FROM items WHERE canonical_id=201").fetchone()[0]
                    == 98
                )
        finally:
            release.set()
        future.result(timeout=15)
    assert ocp.get_claim(e["master"], "occupied")["state"] == "committed"


def test_b1_affected_quantity_changed_after_preparation_stales_under_lock(change_env, monkeypatch):
    e = change_env
    order = domain.fractional_order(e, 0.5)
    domain.ship_fraction_batches(e, order, (0.1, 0.2))
    order = sop.get_order_detail(e["master"], order["id"])
    p, _ = publish(e, order, replacement_intent(order))
    reached, committed = threading.Barrier(2), threading.Event()
    original = ocp._candidate

    def pause(*args):
        quote = original(*args)
        reached.wait(10)
        assert committed.wait(10)
        return quote

    monkeypatch.setattr(ocp, "_candidate", pause)

    def accept():
        with closing(builder.connect(e["master_path"])) as m:
            return ocp.accept_proposal(
                m,
                order_id=order["id"],
                proposal_id=p["id"],
                actor_id=1,
                current_warehouse_code="store_one",
                request_key="changed-after-read",
                seen_digest=ocp.payload_hash(p["snapshot"]),
                agree=True,
            )

    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(accept)
        reached.wait(10)
        with closing(builder.connect(e["master_path"])) as m:
            m.execute(
                "UPDATE store_order_items SET quantity=.49,line_revision=line_revision+1 WHERE id=?",
                (order["order_items"][0]["id"],),
            )
            m.commit()
        before = commercial(e["master"], order["id"])
        committed.set()
        assert future.result(15)["status"] == "stale"
    assert commercial(e["master"], order["id"]) == before
    assert (
        not e["master"]
        .execute("SELECT 1 FROM store_order_items WHERE replaces_order_item_id IS NOT NULL")
        .fetchone()
    )
    domain.dump_fraction_state(e, "changed-under-lock")


@pytest.mark.parametrize("different_actor", [False, True])
def test_b1_tail_accept_same_key_rechecks_identity_and_hash_under_lock(
    change_env, monkeypatch, different_actor
):
    e = change_env
    order = domain.fractional_order(e, 0.5)
    domain.ship_fraction_batches(e, order, (0.1, 0.2))
    order = sop.get_order_detail(e["master"], order["id"])
    p, _ = publish(e, order)
    initial = threading.Barrier(2)
    local = threading.local()
    original = ocp._prior

    def prior(*args):
        result = original(*args)
        if not getattr(local, "observed", False):
            local.observed = True
            assert result is None
            initial.wait(10)
        return result

    monkeypatch.setattr(ocp, "_prior", prior)

    def accept(actor):
        return lambda m: ocp.accept_proposal(
            m,
            order_id=order["id"],
            proposal_id=p["id"],
            actor_id=actor,
            current_warehouse_code="store_one",
            request_key="shared-tail",
            seen_digest=ocp.payload_hash(p["snapshot"]),
            agree=True,
        )

    before = domain.fraction_history(e, order)
    results = race(e, [accept(1), accept(5 if different_actor else 1)])
    assert sum(result[0] == "ok" for result in results) == (1 if different_actor else 2)
    assert domain.fraction_history(e, order) == before
    assert (
        e["master"]
        .execute(
            "SELECT quantity FROM store_order_items WHERE id=?", (order["order_items"][0]["id"],)
        )
        .fetchone()[0]
        == 0.3
    )
    assert (
        e["master"]
        .execute("SELECT COUNT(*) FROM store_order_change_commands WHERE action='accept'")
        .fetchone()[0]
        == 1
    )
    assert (
        e["master"]
        .execute("SELECT COUNT(*) FROM store_order_change_events WHERE to_status='accepted'")
        .fetchone()[0]
        == 1
    )
    domain.dump_fraction_state(e, "identity-race")
