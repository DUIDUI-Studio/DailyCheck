"""Issue #146: validate submitted ownership before skipping zero quantities."""
from __future__ import annotations

import re
import sqlite3

import pytest

from blueprints import store_ordering_pure as sop
from tests.test_ship_op_protocol import ship_env as ship_env


def _state(env):
    """Snapshot both orders, deliveries and all three warehouses' stock/movements."""
    state = {}
    for code in ("A", "B", "store_test"):
        with sqlite3.connect(env["wh_dir"] / f"{code}.db") as conn:
            state[code] = {
                table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                for table in ("items", "stock_movements")
            }
    state["master"] = {
        table: [tuple(row) for row in env["master"].execute(
            f"SELECT * FROM {table} ORDER BY rowid"
        )]
        for table in ("store_orders", "store_order_items", "store_order_deliveries",
                      "store_order_delivery_items", "store_order_status_history")
    }
    return state


def _form(env, order_id, user_id=2, warehouse_id=1):
    client = env["client"]
    client.application.config["WTF_CSRF_FORCE_ENABLED"] = True
    with client.session_transaction() as session:
        session["user_id"] = user_id
        session["warehouse_id"] = warehouse_id
    response = client.get(f"/store-ordering/orders/{order_id}")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    return {
        name: re.search(rf'name="{name}"[^>]*value="([^"]+)"', html)[1]
        for name in ("csrf_token", "op_no")
    }


def _post(env, order_id, form):
    return env["client"].post(
        f"/store-ordering/orders/{order_id}/ship", data=form,
        headers={"X-Requested-With": "fetch"},
    )


def test_new_shipment_and_exact_replay_skip_own_zero_line(ship_env):
    env = ship_env
    order, ids = env["create_order"]([(101, 5, "A"), (102, 5, "A")])
    form = _form(env, order)
    form.update({f"shipped_items[{ids[0]}]": "3", f"shipped_items[{ids[1]}]": "0"})
    response = _post(env, order, form)
    assert response.status_code == 200
    assert response.json["ok"] is True
    assert env["stock"]("A", 101) == 7
    assert env["stock"]("A", 102) == 10
    master = env["master"]
    assert [tuple(row) for row in master.execute(
        "SELECT order_item_id, quantity FROM store_order_delivery_items"
    )] == [(ids[0], 3)]
    assert master.execute("SELECT count(*) FROM store_order_deliveries").fetchone()[0] == 1
    state = _state(env)
    replay = _post(env, order, form)
    assert replay.status_code == 200
    assert replay.json["ok"] is True
    assert _state(env) == state


@pytest.mark.parametrize("quantity", ["0", "3", ""])
@pytest.mark.parametrize("after_valid_shipment", [False, True], ids=["new", "used-key"])
def test_foreign_order_line_rejected_without_writes(ship_env, quantity, after_valid_shipment):
    env = ship_env
    order, own = env["create_order"]([(101, 5, "A")])
    _, foreign = env["create_order"]([(102, 5, "A")])
    form = _form(env, order)
    form[f"shipped_items[{own[0]}]"] = "3"
    if after_valid_shipment:
        assert _post(env, order, form).status_code == 200
    form[f"shipped_items[{foreign[0]}]"] = quantity
    state = _state(env)
    for _ in range(2):
        response = _post(env, order, form)
        assert response.status_code == 409
        assert response.json["ok"] is False
        assert response.json["reason"] == "business_rule"
        assert _state(env) == state


@pytest.mark.parametrize("quantity", ["0", "3", ""])
def test_other_source_warehouse_line_rejected_without_writes(ship_env, quantity):
    env = ship_env
    order, ids = env["create_order"]([(101, 5, "A"), (102, 5, "B")])
    form = _form(env, order)
    form.update({f"shipped_items[{ids[0]}]": "3", f"shipped_items[{ids[1]}]": quantity})
    state = _state(env)
    response = _post(env, order, form)
    assert response.status_code == 409
    assert response.json["ok"] is False
    assert _state(env) == state


@pytest.mark.parametrize("quantity", ["0", ""])
def test_explicit_empty_batch_never_auto_ships(ship_env, quantity):
    env = ship_env
    order, ids = env["create_order"]([(101, 5, "A")])
    form = _form(env, order)
    form[f"shipped_items[{ids[0]}]"] = quantity
    # A legacy positive qty must not override an explicitly empty v3 batch.
    form["qty"] = "3"
    state = _state(env)
    response = _post(env, order, form)
    assert response.status_code == 409
    assert response.json["ok"] is False
    assert _state(env) == state


def test_csrf_and_warehouse_permission_still_guard_shipping(ship_env):
    env = ship_env
    order, ids = env["create_order"]([(101, 5, "A")])
    form = _form(env, order)
    form[f"shipped_items[{ids[0]}]"] = "3"
    state = _state(env)
    without_csrf = {key: value for key, value in form.items() if key != "csrf_token"}
    assert _post(env, order, without_csrf).status_code == 400
    assert _state(env) == state
    with env["client"].session_transaction() as session:
        session["user_id"] = 3
        session["warehouse_id"] = 2
    assert _post(env, order, form).status_code == 403
    assert _state(env) == state


@pytest.mark.parametrize("after_valid_shipment", [False, True], ids=["new", "used-key"])
def test_domain_rejects_foreign_zero_id_in_complete_map(ship_env, after_valid_shipment):
    env = ship_env
    order, own = env["create_order"]([(101, 5, "A")])
    _, foreign = env["create_order"]([(102, 5, "A")])
    op_no = sop.generate_op_no()

    def ship(line_qty):
        return sop.ship_partial(
            env["master"], sop.open_warehouse_db, order_id=order,
            operator_dc_code="A", op_no=op_no, line_qty=line_qty, shipped_by=2,
        )

    if after_valid_shipment:
        ship({own[0]: 3})
    state = _state(env)
    with pytest.raises(ValueError, match="不属于 order_id"):
        ship({own[0]: 3, foreign[0]: 0})
    assert _state(env) == state
