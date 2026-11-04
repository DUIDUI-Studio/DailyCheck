"""Whole-proposal commercial commands against disposable master/warehouse files."""

from contextlib import closing

import pytest

from blueprints import order_changes_pure as ocp
from blueprints import store_ordering_pure as sop
from tests import multi_warehouse_env as builder


@pytest.fixture
def change_env(tmp_path, monkeypatch):
    import config
    from db import init_warehouse_db

    env = builder.build(tmp_path, monkeypatch)
    builder.enable(monkeypatch)
    monkeypatch.setattr(config, "ORDER_CHANGES_ENABLED", True)
    m = builder.connect(env["master_path"])
    for code, wid, kind in [("dc_gamma", 4, "distribution_center"), ("store_two", 5, "storefront")]:
        path = tmp_path / "warehouses" / (code + ".db")
        init_warehouse_db(path)
        env["paths"][code] = path
        m.execute(
            "INSERT INTO warehouses(id,code,name,db_path,warehouse_type,created_at) VALUES(?,?,?,?,?,?)",
            (wid, code, code, str(path), kind, builder.TS),
        )
    for uid, name, admin, active, tenant in [
        (4, "union_approver", 0, 1, 1),
        (5, "store_manager", 0, 1, 1),
        (6, "platform", 1, 1, 1),
        (7, "inactive", 0, 0, 1),
        (8, "other_tenant", 0, 1, 2),
        (9, "other_store", 0, 1, 1),
    ]:
        m.execute(
            "INSERT INTO users(id,username,password_hash,is_admin,is_active,tenant_id,created_at) VALUES(?,?,?,?,?,?,?)",
            (uid, name, "disabled", admin, active, tenant, builder.TS),
        )
    for uid, wid, role in [
        (4, 1, "manager"),
        (4, 2, "manager"),
        (4, 4, "manager"),
        (5, 3, "manager"),
        (7, 1, "manager"),
        (8, 1, "manager"),
        (8, 2, "manager"),
        (9, 5, "manager"),
    ]:
        m.execute(
            "INSERT INTO warehouse_users(user_id,warehouse_id,role) VALUES(?,?,?)", (uid, wid, role)
        )
    m.commit()
    env["master"] = m
    env["root"] = tmp_path
    yield env
    m.close()


def new_order(e, lines=((201, 10), (203, 4)), status="approved"):
    m = e["master"]
    cart = sop.get_or_create_cart(m, 1, "store_one", None)
    for cid, qty in lines:
        sop.add_cart_item(m, cart["id"], cid, qty, "件")
    order = sop.submit_order(m, cart["id"], 1, None, "test #145")
    if status == "approved":
        sop.review_order(m, order["id"], "approved", 4)
    return sop.get_order_detail(m, order["id"])


def intent_for(order, lines=None, **extra):
    return {
        "base_change_revision": order["change_revision"],
        "reason": "缺货调整整份订单",
        "lines": lines or [{"order_item_id": order["order_items"][0]["id"], "operation": "cancel"}],
        **extra,
    }


def publish(e, order, intent=None, key="create", actor=4):
    intent = intent or intent_for(order)
    preview = ocp.preview_proposal(
        e["master"],
        order_id=order["id"],
        actor_id=actor,
        current_warehouse_code="dc_alpha",
        intent=intent,
    )
    result = ocp.create_proposal(
        e["master"],
        order_id=order["id"],
        actor_id=actor,
        current_warehouse_code="dc_alpha",
        request_key=key,
        intent=intent,
        seen_digest=preview["digest"],
    )
    return ocp.proposal_detail(e["master"], order["id"], result["proposal_id"]), preview


def decide(
    e, order, proposal, action="accept", key="decision", actor=1, current="store_one", **extra
):
    kwargs = dict(
        order_id=order["id"],
        proposal_id=proposal["id"],
        actor_id=actor,
        current_warehouse_code=current,
        request_key=key,
        seen_digest=ocp.payload_hash(proposal["snapshot"]),
        note="整份核对",
    )
    kwargs.update(extra)
    if action == "accept":
        kwargs.setdefault("agree", True)
        return ocp.accept_proposal(e["master"], **kwargs)
    return ocp.reject_proposal(e["master"], **kwargs)


def rows(m, table):
    return [tuple(row) for row in m.execute("SELECT * FROM " + table + " ORDER BY rowid")]


def commercial(m, oid):
    return [
        tuple(row)
        for row in m.execute("SELECT * FROM store_order_items WHERE order_id=? ORDER BY id", (oid,))
    ]


def test_mixed_whole_accept_preserves_history_and_partial_receipt(change_env):
    e, m = change_env, change_env["master"]
    order = new_order(e, ((201, 10), (203, 4), (202, 5), (206, 2)))
    oid = order["id"]
    ids = {line["canonical_id"]: line["id"] for line in order["order_items"]}
    sop.ship_partial(
        m,
        sop.open_warehouse_db,
        order_id=oid,
        operator_dc_code="dc_alpha",
        op_no="initial",
        line_qty={ids[201]: 4},
        shipped_by=2,
    )
    sop.receive_partial(
        m,
        sop.open_warehouse_db,
        order_id=oid,
        op_no="initial-receive",
        item_qty={ids[201]: 2},
        received_by=1,
    )
    order = sop.get_order_detail(m, oid)
    histories = {
        table: rows(m, table)
        for table in (
            "store_order_deliveries",
            "store_order_delivery_items",
            "store_order_receipts",
        )
    }
    before = commercial(m, oid)
    intent = intent_for(
        order,
        [
            {
                "order_item_id": ids[201],
                "operation": "replace",
                "replacement_canonical_id": 207,
                "replacement_quantity": 3,
            },
            {"order_item_id": ids[203], "operation": "reduce", "remove_quantity": 1},
            {"order_item_id": ids[202], "operation": "cancel"},
        ],
    )
    proposal, _ = publish(e, order, intent)
    assert commercial(m, oid) == before
    assert set(ocp.blocked_lines(m, oid)) == {ids[201], ids[203], ids[202]}
    with pytest.raises(ValueError):
        sop.ship_partial(
            m,
            sop.open_warehouse_db,
            order_id=oid,
            operator_dc_code="dc_alpha",
            op_no="blocked",
            line_qty={ids[201]: 1},
            shipped_by=2,
        )
    assert e["paths"]["dc_alpha"].exists()
    sop.ship_partial(
        m,
        sop.open_warehouse_db,
        order_id=oid,
        operator_dc_code="dc_alpha",
        op_no="unrelated",
        line_qty={ids[206]: 1},
        shipped_by=2,
    )
    assert decide(e, order, proposal)["status"] == "accepted"
    refreshed = sop.get_order_detail(m, oid)
    a = next(line for line in refreshed["order_items"] if line["id"] == ids[201])
    b = next(
        line for line in refreshed["order_items"] if line["replaces_order_item_id"] == ids[201]
    )
    assert (
        a["quantity"],
        a["original_quantity"],
        a["shipped_quantity"],
        a["fulfilled_quantity"],
    ) == (4, 10, 4, 2)
    assert a["source_warehouse_code"] == "dc_alpha" and a["canonical_id"] == 201
    assert (b["quantity"], b["source_warehouse_code"], b["canonical_id"]) == (3, "dc_beta", 207)
    assert b["introduced_by_proposal_line_id"] is not None
    assert rows(m, "store_order_receipts") == histories["store_order_receipts"]
    assert rows(m, "store_order_delivery_items")[:1] == histories["store_order_delivery_items"]
    assert rows(m, "store_order_deliveries")[:1] == histories["store_order_deliveries"]
    sop.receive_partial(
        m,
        sop.open_warehouse_db,
        order_id=oid,
        op_no="remaining-a",
        item_qty={ids[201]: 2},
        received_by=1,
    )
    sop.ship_partial(
        m,
        sop.open_warehouse_db,
        order_id=oid,
        operator_dc_code="dc_beta",
        op_no="replacement-b",
        line_qty={b["id"]: 3},
        shipped_by=3,
    )
    assert sop.get_order_detail(m, oid)["change_revision"] == order["change_revision"] + 1
    assert rows(m, "store_order_change_events")[0][3:5] == (None, "pending")


def test_reject_repropose_withdraw_sequence_and_decision_replay(change_env):
    e, m = change_env, change_env["master"]
    order = new_order(e)
    before = commercial(m, order["id"])
    p, _ = publish(e, order)
    with pytest.raises(ValueError):
        decide(e, order, p, "reject", note="")
    rejected = decide(e, order, p, "reject", note="不接受替代", key="reject")
    assert rejected["status"] == "rejected" and commercial(m, order["id"]) == before
    count = len(rows(m, "notifications"))
    assert decide(e, order, p, "reject", key="reject", note="不接受替代") == rejected
    again = decide(e, order, p, "reject", key="new-reject", note="不得覆盖")
    assert again["already_processed"] and again["decision_note"] == "不接受替代"
    assert len(rows(m, "notifications")) == count
    with pytest.raises(ValueError):
        decide(e, order, p, key="opposite")
    p2, _ = publish(e, order, key="create2")
    ocp.withdraw_proposal(
        m,
        order_id=order["id"],
        proposal_id=p2["id"],
        actor_id=4,
        current_warehouse_code="dc_alpha",
        request_key="withdraw",
    )
    p3, _ = publish(e, order, key="create3")
    assert [p["sequence"], p2["sequence"], p3["sequence"]] == [1, 2, 3]
    assert len({p["id"], p2["id"], p3["id"]}) == 3


@pytest.mark.parametrize("status", ["pending", "approved"])
def test_zero_effective_cancellation_and_pending_stays_pending(change_env, status):
    e, m = change_env, change_env["master"]
    order = new_order(e, ((201, 3),), status)
    p, _ = publish(e, order)
    decide(e, order, p)
    final = sop.get_order_detail(m, order["id"])
    assert final["status"] == "cancelled" and final["shipping_fee"] == 0
    assert not rows(m, "store_order_deliveries") and not rows(m, "store_order_receipts")
    order2 = new_order(e, ((203, 4),), status)
    p2, _ = publish(
        e,
        order2,
        intent_for(
            order2,
            [
                {
                    "order_item_id": order2["order_items"][0]["id"],
                    "operation": "reduce",
                    "remove_quantity": 1,
                }
            ],
        ),
        key="new2",
    )
    decide(e, order2, p2, key="decide2")
    assert sop.get_order_detail(m, order2["id"])["status"] == status


def test_decimal_fee_zero_cost_and_snapshot_survives_live_changes(change_env):
    e, m = change_env, change_env["master"]
    with closing(builder.connect(e["paths"]["dc_alpha"])) as wh:
        wh.execute("UPDATE items SET selling_price=.335,unit_cost=88 WHERE canonical_id=201")
        wh.commit()
    sop.upsert_shipping_rule(m, base_fee=0.005, pct_fee=0)
    order = new_order(e, ((201, 4),))
    p, _ = publish(
        e,
        order,
        intent_for(
            order,
            [
                {
                    "order_item_id": order["order_items"][0]["id"],
                    "operation": "reduce",
                    "remove_quantity": 1,
                }
            ],
        ),
    )
    assert p["snapshot"]["after"]["goods_amount"] == "1.01"
    assert (
        p["snapshot"]["after"]["shipping_fee"] == "0.01"
        and p["snapshot"]["after"]["total_amount"] == "1.02"
    )
    decide(e, order, p)
    with closing(builder.connect(e["paths"]["dc_alpha"])) as wh:
        wh.execute("UPDATE items SET selling_price=9,unit_cost=99")
        wh.commit()
    sop.upsert_shipping_rule(m, base_fee=100, pct_fee=1)
    assert sop.get_order_detail(m, order["id"])["total_amount"] == "1.02"
    p2, preview = publish(e, sop.get_order_detail(m, order["id"]), key="next")
    assert p2["snapshot"]["before"]["total_amount"] == "1.02"
    assert preview["snapshot"]["delta"] == "-1.02"


def legacy_order(e):
    m = e["master"]
    oid = m.execute(
        "INSERT INTO store_orders(order_no,store_warehouse_code,dc_warehouse_code,status,requested_by,shipping_fee,created_at,updated_at) VALUES('LEGACY','store_one','dc_alpha','approved',1,2,?,?)",
        (builder.TS, builder.TS),
    ).lastrowid
    for cid in (201, 203):
        m.execute(
            "INSERT INTO store_order_items(order_id,canonical_id,source_warehouse_code,quantity,unit,created_at) VALUES(?,?,'dc_alpha',3,'件',?)",
            (oid, cid, builder.TS),
        )
    m.commit()
    return sop.get_order_detail(m, oid)


def test_legacy_all_missing_prices_require_evidence_and_accept_only_locks_null(change_env):
    e, m = change_env, change_env["master"]
    order = legacy_order(e)
    intent = intent_for(order)
    with pytest.raises(ValueError, match="全单"):
        publish(e, order, intent)
    assert not rows(m, "store_order_change_proposals") and not ocp.blocked_lines(m, order["id"])
    intent["historical_prices"] = {
        str(line["id"]): {
            "price": "0" if i == 0 else ".335",
            "reference": "OFFLINE-145",
            "note": "逐项核对历史计价",
        }
        for i, line in enumerate(order["order_items"])
    }
    p, _ = publish(e, order, intent)
    assert all(
        line["unit_price_snapshot"] is None
        for line in sop.get_order_detail(m, order["id"])["order_items"]
    )
    assert (
        p["snapshot"]["before"]["goods_amount"] == "1.01"
        and p["snapshot"]["after"]["shipping_fee"] == "2.00"
    )
    assert p["snapshot"]["before"]["lines"][0]["product_basis"] == "current_identification_only"
    decide(e, order, p)
    final = sop.get_order_detail(m, order["id"])
    assert [line["unit_price_snapshot"] for line in final["order_items"]] == ["0", "0.335"]
    assert all(line["price_snapshot_origin"] == "verified" for line in final["order_items"])
    assert final["shipping_rule_snapshot"] is None
    assert all(
        evidence["checked_by"] == 4 and evidence["checked_at"]
        for evidence in p["snapshot"]["historical_verification"].values()
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("remove_quantity", True),
        ("remove_quantity", "NaN"),
        ("remove_quantity", "-1"),
        ("remove_quantity", 10),
    ],
)
def test_invalid_reduce_cannot_publish(change_env, field, value):
    e = change_env
    order = new_order(e, ((201, 10),))
    intent = intent_for(
        order,
        [{"order_item_id": order["order_items"][0]["id"], "operation": "reduce", field: value}],
    )
    with pytest.raises(ValueError):
        publish(e, order, intent)
    assert not rows(e["master"], "store_order_change_proposals")


def replacement_intent(order):
    return intent_for(
        order,
        [
            {
                "order_item_id": order["order_items"][0]["id"],
                "operation": "replace",
                "replacement_canonical_id": 207,
                "replacement_quantity": 3,
            }
        ],
    )


@pytest.mark.parametrize(
    "actor,current",
    [(2, "dc_alpha"), (9, "store_two"), (4, "dc_alpha"), (7, "store_one"), (8, "store_one")],
)
def test_decision_permissions_reject_without_staling(change_env, actor, current):
    e = change_env
    order = new_order(e)
    p, _ = publish(e, order, replacement_intent(order))
    with pytest.raises(PermissionError):
        decide(e, order, p, actor=actor, current=current)
    assert ocp.proposal_detail(e["master"], order["id"], p["id"])["status"] == "pending"


def test_proposer_must_have_new_source_endorsement(change_env):
    e = change_env
    order = new_order(e)
    with pytest.raises(PermissionError):
        publish(e, order, replacement_intent(order), actor=2)
    assert not rows(e["master"], "store_order_change_proposals")


@pytest.mark.parametrize(
    "mutation",
    [
        "price",
        "route",
        "alias",
        "inactive",
        "orderable",
        "unit",
        "multiple",
        "double",
        "canonical",
        "store_disabled",
        "exclusive",
        "endorsement",
        "revision",
    ],
)
def test_accept_stale_is_durable_and_never_partially_applies(change_env, mutation):
    e, m = change_env, change_env["master"]
    order = new_order(e)
    p, _ = publish(e, order, replacement_intent(order))
    before = commercial(m, order["id"])
    if mutation in ("price", "alias", "inactive", "orderable", "unit", "multiple", "double"):
        with closing(builder.connect(e["paths"]["dc_beta"])) as wh:
            updates = {
                "price": "selling_price=8",
                "alias": "is_alias=1",
                "inactive": "is_active=0",
                "orderable": "is_orderable=0",
                "unit": "unit='箱'",
                "multiple": "order_multiple=5",
            }
            if mutation == "double":
                wh.execute(
                    "INSERT INTO items(sku,name,category_id,canonical_id,unit,updated_at) SELECT 'dup','dup',category_id,canonical_id,unit,updated_at FROM items WHERE canonical_id=207"
                )
            else:
                wh.execute("UPDATE items SET " + updates[mutation] + " WHERE canonical_id=207")
            wh.commit()
    elif mutation in ("store_disabled", "exclusive"):
        with closing(builder.connect(e["paths"]["store_one"])) as wh:
            wh.execute(
                "UPDATE items SET "
                + ("is_active=0" if mutation == "store_disabled" else "is_store_exclusive=1")
                + " WHERE canonical_id=207"
            )
            wh.commit()
    elif mutation == "route":
        m.execute(
            "UPDATE item_routing SET fulfillment_warehouse_code='dc_gamma' WHERE canonical_id=207"
        )
    elif mutation == "canonical":
        m.execute("UPDATE canonical_items SET status='inactive' WHERE id=207")
    elif mutation == "endorsement":
        m.execute("DELETE FROM warehouse_users WHERE user_id=4 AND warehouse_id=2")
    elif mutation == "revision":
        m.execute(
            "UPDATE store_orders SET change_revision=change_revision+1 WHERE id=?", (order["id"],)
        )
    m.commit()
    result = decide(e, order, p)
    assert result["status"] == "stale" and result["conflict"]
    assert commercial(m, order["id"]) == before
    assert ocp.proposal_detail(m, order["id"], p["id"])["status"] == "stale"
    assert decide(e, order, p) == result
    assert len(rows(m, "store_order_change_events")) == 2 and not ocp.blocked_lines(m, order["id"])


def test_completed_replay_does_not_rebind_catalog_or_duplicate_notifications(change_env):
    e, m = change_env, change_env["master"]
    order = new_order(e)
    p, preview = publish(e, order, replacement_intent(order))
    result = decide(e, order, p)
    before = commercial(m, order["id"])
    count = len(rows(m, "notifications"))
    m.execute("DELETE FROM item_routing WHERE canonical_id=207")
    m.commit()
    assert decide(e, order, p) == result
    assert commercial(m, order["id"]) == before and len(rows(m, "notifications")) == count
    same = ocp.create_proposal(
        m,
        order_id=order["id"],
        actor_id=4,
        current_warehouse_code="dc_alpha",
        request_key="create",
        intent=replacement_intent(order),
        seen_digest=preview["digest"],
    )
    assert same["proposal_id"] == p["id"]
    with pytest.raises(ValueError):
        decide(e, order, p, note="changed")


@pytest.mark.parametrize("action", ["create", "accept", "reject"])
def test_notification_enqueue_then_failure_rolls_back_every_table(change_env, monkeypatch, action):
    from blueprints import notifications_pure

    e, m = change_env, change_env["master"]
    order = new_order(e)
    p = None
    if action != "create":
        p, _ = publish(e, order)
    tables = (
        "store_orders",
        "store_order_items",
        "store_order_change_proposals",
        "store_order_change_lines",
        "store_order_change_commands",
        "store_order_change_events",
        "notifications",
        "store_order_status_history",
    )
    before = {table: rows(m, table) for table in tables}
    original = notifications_pure.enqueue_event

    def fail(*args):
        original(*args)
        raise RuntimeError("after enqueue")

    monkeypatch.setattr(notifications_pure, "enqueue_event", fail)
    with pytest.raises(RuntimeError, match="after enqueue"):
        publish(e, order) if action == "create" else decide(e, order, p, action)
    assert {table: rows(m, table) for table in tables} == before


def test_existing_caller_transaction_rejected_before_warehouse_access(change_env, monkeypatch):
    e = change_env
    order = new_order(e)
    m = e["master"]
    m.execute("BEGIN")
    monkeypatch.setattr(ocp, "_candidate", lambda *args: pytest.fail("warehouse touched"))
    with pytest.raises(ValueError, match="独立master"):
        ocp.create_proposal(
            m,
            order_id=order["id"],
            actor_id=4,
            current_warehouse_code="dc_alpha",
            request_key="tx",
            intent=replacement_intent(order),
            seen_digest="x",
        )
    m.rollback()


def test_last_remaining_cancel_completes_without_rewriting_delivery_receipt(change_env):
    e, m = change_env, change_env["master"]
    order = new_order(e, ((201, 10), (203, 4)))
    a = order["order_items"][0]["id"]
    sop.ship_partial(
        m,
        sop.open_warehouse_db,
        order_id=order["id"],
        operator_dc_code="dc_alpha",
        op_no="sent",
        line_qty={a: 4},
        shipped_by=2,
    )
    sop.receive_partial(
        m, sop.open_warehouse_db, order_id=order["id"], op_no="recv", item_qty={a: 4}, received_by=1
    )
    order = sop.get_order_detail(m, order["id"])
    old = {
        table: rows(m, table)
        for table in (
            "store_order_deliveries",
            "store_order_delivery_items",
            "store_order_receipts",
        )
    }
    p, _ = publish(
        e,
        order,
        intent_for(
            order,
            [{"order_item_id": line["id"], "operation": "cancel"} for line in order["order_items"]],
        ),
    )
    decide(e, order, p)
    final = sop.get_order_detail(m, order["id"])
    assert final["status"] == "delivered"
    assert {table: rows(m, table) for table in old} == old
    assert sum(line["quantity"] for line in final["order_items"]) == 4
    assert sop.compute_store_order_report(m)["summary"]["ordered_qty"] == 4


@pytest.mark.parametrize("guard", ["unapplied", "active", "recovering"])
def test_accept_does_not_complete_through_other_claim_or_unapplied_receipt(change_env, guard):
    e, m = change_env, change_env["master"]
    order = new_order(e, ((201, 5), (203, 1)))
    a, b = [line["id"] for line in order["order_items"]]
    sop.ship_partial(
        m,
        sop.open_warehouse_db,
        order_id=order["id"],
        operator_dc_code="dc_alpha",
        op_no="sent",
        line_qty={a: 2, b: 1},
        shipped_by=2,
    )
    sop.receive_partial(
        m,
        sop.open_warehouse_db,
        order_id=order["id"],
        op_no="recv",
        item_qty={a: 2, b: 1},
        received_by=1,
    )
    order = sop.get_order_detail(m, order["id"])
    p, _ = publish(e, order)
    if guard == "unapplied":
        m.execute("UPDATE store_order_receipts SET applied=0 WHERE order_item_id=?", (b,))
    else:
        payload = ocp.ship_payload(order["id"], "dc_alpha", {b: 1})
        m.execute(
            "INSERT INTO store_order_ship_claims(op_no,order_id,source_warehouse_code,payload_hash,payload_json,state,actor_id,created_at) VALUES('held',?,'dc_alpha',?,?,'active',2,?)",
            (order["id"], ocp.payload_hash(payload), ocp.canonical_json(payload), builder.TS),
        )
        row = m.execute("SELECT * FROM store_order_items WHERE id=?", (b,)).fetchone()
        m.execute(
            "INSERT INTO store_order_ship_claim_lines(op_no,order_id,order_item_id,quantity,base_line_revision,baseline_quantity,baseline_shipped,canonical_id,dc_item_id,unit,state) VALUES('held',?,?,1,?,?,?,?,?,'件','active')",
            (
                order["id"],
                b,
                row["line_revision"],
                row["quantity"],
                row["shipped_quantity"],
                row["canonical_id"],
                row["dc_item_id"],
            ),
        )
        if guard == "recovering":
            m.execute("UPDATE store_order_ship_claims SET state='recovering' WHERE op_no='held'")
    m.commit()
    decide(e, order, p)
    assert sop.get_order_detail(m, order["id"])["status"] != "delivered"
    assert (
        not ocp.effective_progress(m, order["id"])["all_received"] if guard == "unapplied" else True
    )


@pytest.mark.parametrize(
    "fee,reference,note,allowed",
    [("1", "", "", False), ("1", "OFF-145", "运费凭据核对", True), ("-1", "OFF", "说明", False)],
)
def test_legacy_fee_adjustment_requires_nonnegative_explicit_evidence(
    change_env, fee, reference, note, allowed
):
    e = change_env
    order = legacy_order(e)
    prices = {
        str(line["id"]): {"price": ".335", "reference": "HIST-145", "note": "原价核对"}
        for line in order["order_items"]
    }
    intent = intent_for(
        order, historical_prices=prices, after_fee=fee, fee_reference=reference, fee_note=note
    )
    if not allowed:
        with pytest.raises(ValueError):
            publish(e, order, intent)
        assert not rows(e["master"], "store_order_change_proposals")
    else:
        p, _ = publish(e, order, intent)
        decide(e, order, p)
        assert sop.get_order_detail(e["master"], order["id"])["shipping_fee"] == 1
        assert p["snapshot"]["fee_basis"] == "verified_adjustment_rule_unknown"


@pytest.mark.parametrize(
    "mutation", ["duplicate", "boolean_id", "empty", "excess", "abnormal", "proof_other_order"]
)
def test_invalid_whole_intent_never_pauses_order(change_env, mutation):
    e, m = change_env, change_env["master"]
    order = new_order(e)
    intent = intent_for(order)
    if mutation == "duplicate":
        intent["lines"] *= 2
    elif mutation == "boolean_id":
        intent["lines"][0]["order_item_id"] = True
    elif mutation == "empty":
        intent["lines"] = []
    elif mutation == "excess":
        intent["lines"][0].update(operation="reduce", remove_quantity=99)
    elif mutation == "abnormal":
        m.execute(
            "UPDATE store_order_items SET fulfilled_quantity=99 WHERE id=?",
            (order["order_items"][0]["id"],),
        )
        m.commit()
        order = sop.get_order_detail(m, order["id"])
    else:
        order = legacy_order(e)
        intent = intent_for(
            order,
            historical_prices={
                str(line["id"]): {"price": "0", "reference": "proof:999", "note": "核对"}
                for line in order["order_items"]
            },
        )
    with pytest.raises(ValueError):
        publish(e, order, intent)
    assert not rows(m, "store_order_change_proposals") and not ocp.blocked_lines(m, order["id"])


def test_fractional_cancel_keeps_exact_shipped_floor(change_env):
    e = change_env
    # The warehouse's existing multiple contract admits fractional basic units.
    with closing(builder.connect(e["paths"]["dc_alpha"])) as wh:
        wh.execute("UPDATE items SET order_multiple=.1 WHERE canonical_id=201")
        wh.commit()
    order = new_order(e, ((201, 0.3),))
    item = order["order_items"][0]["id"]
    sop.ship_partial(
        e["master"],
        sop.open_warehouse_db,
        order_id=order["id"],
        operator_dc_code="dc_alpha",
        op_no="fraction-ship",
        line_qty={item: 0.1},
        shipped_by=2,
    )
    order = sop.get_order_detail(e["master"], order["id"])
    p, _ = publish(e, order)
    assert p["snapshot"]["operations"][0]["remove_quantity"] == "0.20"
    decide(e, order, p)
    line = sop.get_order_detail(e["master"], order["id"])["order_items"][0]
    assert line["quantity"] == line["shipped_quantity"] == 0.1


@pytest.mark.parametrize(
    "price,cost,expected,origin",
    [(0, 2, "2.0", "unit_cost"), (0, 0, "0", "unit_cost"), (0.335, 99, "0.335", "selling_price")],
)
def test_replacement_server_price_multiple_and_aux_units(change_env, price, cost, expected, origin):
    e, m = change_env, change_env["master"]
    m.execute("UPDATE canonical_items SET aux_unit='箱',aux_rate=3 WHERE id=207")
    m.commit()
    with closing(builder.connect(e["paths"]["dc_beta"])) as wh:
        wh.execute(
            "UPDATE items SET selling_price=?,unit_cost=?,order_multiple=3 WHERE canonical_id=207",
            (price, cost),
        )
        wh.commit()
    order = new_order(e)
    p, _ = publish(e, order, replacement_intent(order))
    quote = p["snapshot"]["operations"][0]["replacement"]
    assert quote["unit_price_snapshot"] == expected and quote["price_snapshot_origin"] == origin
    assert (
        quote["unit"] == "件"
        and quote["quantity"] == "3.00"
        and quote["product"]["aux_unit"] == "箱"
    )
    decide(e, order, p)
    final = sop.get_order_detail(m, order["id"])
    assert final["order_items"][-1]["unit_price_snapshot"] == expected


def test_platform_admin_must_choose_target_store_and_records_acting_confirmation(change_env):
    e = change_env
    order = new_order(e)
    p, _ = publish(e, order)
    with pytest.raises(PermissionError):
        decide(e, order, p, actor=6, current="dc_alpha")
    result = decide(e, order, p, actor=6, current="store_one")
    assert result["decided_by"] == 6 and "平台管理员代确认" in result["decision_note"]


def test_accepted_effective_projection_mcp_is_read_only_without_double_count(
    change_env, monkeypatch
):
    from mcp_server.data import unit_of_work
    from mcp_server.service.auth import AuthContext
    from mcp_server.service.store_order import get_store_order_detail

    e, m = change_env, change_env["master"]
    order = new_order(e)
    p, _ = publish(e, order, replacement_intent(order))
    decide(e, order, p)
    monkeypatch.setattr(unit_of_work, "MASTER_DB", e["master_path"])
    tables = (
        "store_orders",
        "store_order_items",
        "store_order_change_proposals",
        "store_order_change_events",
        "store_order_change_commands",
    )
    before = {table: rows(m, table) for table in tables}
    ctx = AuthContext(
        token_id=1,
        allowed_read_paths=["/api/v1/store-orders"],
        allowed_write_paths=[],
        allowed_warehouses=["store_one"],
        read_only=True,
    )
    response = get_store_order_detail(ctx, order["id"])
    assert sum(line["quantity"] for line in response["items"]) == 7
    assert response["items"][0]["original_quantity"] == 10 and response["items"][0]["quantity"] == 0
    assert "snapshot_json" not in str(response) and "payload_hash" not in str(response)
    assert {table: rows(m, table) for table in tables} == before


def fractional_order(e, amount):
    with closing(builder.connect(e["paths"]["dc_alpha"])) as wh:
        wh.execute("UPDATE items SET order_multiple=.1 WHERE canonical_id=201")
        wh.commit()
    return new_order(e, ((201, amount), (203, 4)))


def ship_fraction_batches(e, order, batches):
    item = order["order_items"][0]["id"]
    for index, amount in enumerate(batches):
        sop.ship_partial(
            e["master"],
            sop.open_warehouse_db,
            order_id=order["id"],
            operator_dc_code="dc_alpha",
            op_no=f"fraction-batch-{index}",
            line_qty={item: amount},
            shipped_by=2,
        )


def fraction_history(e, order):
    m = e["master"]
    return {
        "lines": [
            tuple(row)
            for row in m.execute(
                "SELECT id,original_quantity,shipped_quantity,fulfilled_quantity,canonical_id,source_warehouse_code,unit FROM store_order_items WHERE order_id=? ORDER BY id",
                (order["id"],),
            )
        ],
        **{
            table: rows(m, table)
            for table in (
                "store_order_deliveries",
                "store_order_delivery_items",
                "store_order_receipts",
            )
        },
    }


def dump_fraction_state(e, label):
    import json

    m = e["master"]
    tables = (
        "store_order_items",
        "store_order_change_proposals",
        "store_order_change_commands",
        "store_order_change_events",
        "store_order_ship_claims",
        "store_order_ship_claim_lines",
        "notifications",
    )
    (e["root"] / (label + ".sql")).write_text("\n".join(m.iterdump()))
    (e["root"] / (label + ".json")).write_text(
        json.dumps(
            {
                table: [
                    dict(row) for row in m.execute("SELECT * FROM " + table + " ORDER BY rowid")
                ]
                for table in tables
            },
            ensure_ascii=False,
            indent=2,
        )
    )


FRACTION_BATCHES = [(0.3,), (0.1, 0.2), (0.2, 0.1), (0.1, 0.1, 0.1)]


@pytest.mark.parametrize("batches", FRACTION_BATCHES)
def test_b1_unrelated_native_fraction_batches_accept_without_stale(change_env, batches):
    e = change_env
    order = fractional_order(e, 0.3)
    b = order["order_items"][1]["id"]
    p, _ = publish(e, order, intent_for(order, [{"order_item_id": b, "operation": "cancel"}]))
    ship_fraction_batches(e, order, batches)
    before = fraction_history(e, order)
    dump_fraction_state(e, "before-decision")
    try:
        assert decide(e, order, p)["status"] == "accepted"
        assert fraction_history(e, order) == before
        final = sop.get_order_detail(e["master"], order["id"])
        assert final["order_items"][1]["quantity"] == 0 and final["status"] == "shipped"
        assert final["goods_amount"] == "2.97"
    finally:
        dump_fraction_state(e, "after-decision")


@pytest.mark.parametrize("batches", FRACTION_BATCHES)
def test_b1_affected_native_fraction_batches_cancel_remaining_without_history_rewrite(
    change_env, batches
):
    e = change_env
    order = fractional_order(e, 0.5)
    ship_fraction_batches(e, order, batches)
    order = sop.get_order_detail(e["master"], order["id"])
    before = fraction_history(e, order)
    p, _ = publish(e, order)
    dump_fraction_state(e, "before-decision")
    try:
        assert p["snapshot"]["operations"][0]["remove_quantity"] == "0.20"
        assert decide(e, order, p)["status"] == "accepted"
        assert fraction_history(e, order) == before
        final = sop.get_order_detail(e["master"], order["id"])
        assert final["order_items"][0]["quantity"] == 0.3
        assert final["order_items"][0]["shipped_quantity"] == sum(batches)
        assert final["order_items"][0]["original_quantity"] == 0.5
        assert final["order_items"][0]["line_amount"] == "2.97"
    finally:
        dump_fraction_state(e, "after-decision")


@pytest.mark.parametrize("batches", FRACTION_BATCHES)
@pytest.mark.parametrize("affected", [False, True])
def test_b1_native_fraction_receipts_preserve_history_and_complete_at_effective_quantity(
    change_env, batches, affected
):
    e = change_env
    order = fractional_order(e, 0.5 if affected else 0.3)
    a, b = [line["id"] for line in order["order_items"]]
    ship_fraction_batches(e, order, (0.3,))
    order = sop.get_order_detail(e["master"], order["id"])
    operations = [{"order_item_id": b, "operation": "cancel"}]
    if affected:
        operations.append({"order_item_id": a, "operation": "cancel"})
    p, _ = publish(e, order, intent_for(order, operations))
    for index, amount in enumerate(batches):
        sop.receive_partial(
            e["master"],
            sop.open_warehouse_db,
            order_id=order["id"],
            op_no=f"fraction-receive-{index}",
            item_qty={a: amount},
            received_by=1,
        )
    before = fraction_history(e, order)
    dump_fraction_state(e, "before-decision")
    try:
        assert decide(e, order, p)["status"] == "accepted"
        assert fraction_history(e, order) == before
        final = sop.get_order_detail(e["master"], order["id"])
        assert final["status"] == "delivered" and final["goods_amount"] == "2.97"
        assert final["order_items"][0]["fulfilled_quantity"] == sum(batches)
    finally:
        dump_fraction_state(e, "after-decision")
