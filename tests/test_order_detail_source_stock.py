"""Issue #109: order detail enrichment uses each line's frozen fulfillment identity."""
from __future__ import annotations

import re
from contextlib import closing

import pytest
from flask import template_rendered

from blueprints import store_ordering_pure as sop
from tests import multi_warehouse_env as env_builder


@pytest.fixture
def env(tmp_path, monkeypatch):
    env = env_builder.build(tmp_path, monkeypatch)
    env_builder.enable(monkeypatch)
    with closing(env_builder.connect(env["master_path"])) as master:
        master.execute(
            "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
            "VALUES (4, 'platform_admin', 'x', 1, ?)", (env_builder.TS,),
        )
        cart = sop.get_or_create_cart(master, 1, "store_one", None)
        sop.add_cart_item(master, cart["id"], 201, 2, "件")
        sop.add_cart_item(master, cart["id"], 202, 5, "件")
        order = sop.submit_order(master, cart["id"], 1, None, "source regression")
        env["order_id"] = order["id"]
        env["lines"] = {it["canonical_id"]: it["id"] for it in order["order_items"]}
        assert order["dc_warehouse_code"] == "dc_alpha"
        # Routing changes after submit must never redirect these existing lines.
        master.execute(
            "UPDATE item_routing SET fulfillment_warehouse_code='dc_alpha' "
            "WHERE canonical_id=202"
        )
        master.commit()
    for code, quantities, category in (
        ("dc_alpha", {201: 7, 203: 13}, "alpha category"),
        ("dc_beta", {201: 91, 202: 25}, "beta category"),
    ):
        with closing(env_builder.connect(env["paths"][code])) as wh:
            cat_id = wh.execute(
                "INSERT INTO categories (name, created_at) VALUES (?, ?)",
                (category, env_builder.TS),
            ).lastrowid
            for cid, quantity in quantities.items():
                wh.execute(
                    "UPDATE items SET quantity=?, category_id=? WHERE canonical_id=?",
                    (quantity, cat_id, cid),
                )
            wh.commit()
    # Both physical DBs have item id 2; alpha's is an unrelated canonical item.
    assert env["alpha_item_ids"][203] == env["beta_item_ids"][202]
    return env


def _info(env):
    with closing(env_builder.connect(env["master_path"])) as master:
        return sop.get_dc_items_for_order_detail(master, env["order_id"])


def _update_line(env, sql, params=()):
    with closing(env_builder.connect(env["master_path"])) as master:
        # These mutations construct legacy/damaged fixtures. Production code cannot
        # erase a snapshot identity; keep the trigger enabled outside this seed step.
        from migrations.order_changes import DDL
        if sql.startswith('UPDATE store_order_items'):
            master.execute('DROP TRIGGER order_item_identity_immutable')
        master.execute(sql, params)
        if sql.startswith('UPDATE store_order_items'):
            master.execute(next(ddl for ddl in DDL if 'CREATE TRIGGER IF NOT EXISTS order_item_identity_immutable ' in ddl))
        master.commit()


@pytest.mark.parametrize("frozen_id", [True, False])
def test_each_line_uses_recorded_source_despite_routing_change(env, frozen_id):
    if not frozen_id:
        _update_line(env, "UPDATE store_order_items SET dc_item_id=NULL")
    info = _info(env)
    assert info[env["lines"][201]] == {
        "name": "品项A", "dc_available": 7.0, "unit_price": 9.9,
        "category_name": "alpha category",
    }
    assert info[env["lines"][202]] == {
        "name": "品项B", "dc_available": 25.0, "unit_price": 1.5,
        "category_name": "beta category",
    }


@pytest.mark.parametrize("who", [(1, 3), (2, 1), (3, 2), (4, 3)])
def test_detail_render_uses_source_enrichment_for_store_dc_and_admin(env, who):
    client = env["client"]
    with client.session_transaction() as session:
        session["user_id"], session["warehouse_id"] = who
    board = (
        "/store-ordering/admin/orders" if who[0] == 4
        else "/store-ordering/orders" if who[0] == 1
        else "/store-ordering/review"
    )
    response = client.get(board)
    assert response.status_code == 200
    assert f'/store-ordering/orders/{env["order_id"]}' in response.get_data(as_text=True)
    contexts = []

    def capture(sender, template, context, **extra):
        if template.name == "store_ordering/order_detail.html":
            contexts.append(context)

    with template_rendered.connected_to(capture, env["app"]):
        response = client.get(f"/store-ordering/orders/{env['order_id']}")
    assert response.status_code == 200
    items = {it["canonical_id"]: it for it in contexts[0]["order"]["order_items"]}
    assert items[201]["dc_available"] == 7
    assert items[202]["dc_available"] == 25
    assert items[202]["unit_price"] == 1.5
    body = response.get_data(as_text=True)
    assert "¥ 1.50" in body
    if who in ((2, 1), (3, 2)):
        stocks = re.findall(r'class="dc-stock [^\"]*">\s*([^<]+)', body)
        assert [stock.strip() for stock in stocks] == ["7", "25"]


@pytest.mark.parametrize("frozen_id", [True, False])
def test_historical_null_source_falls_back_to_header(env, frozen_id):
    _update_line(
        env, "UPDATE store_order_items SET source_warehouse_code=NULL, dc_item_id=? "
        "WHERE canonical_id=201",
        (env["alpha_item_ids"][201] if frozen_id else None,),
    )
    assert _info(env)[env["lines"][201]]["dc_available"] == 7


@pytest.mark.parametrize("damage", ["missing", "mismatched", "replaced"])
def test_frozen_item_cannot_fall_back_to_another_binding(env, damage):
    item_id = env["beta_item_ids"][202]
    with closing(env_builder.connect(env["paths"]["dc_beta"])) as wh:
        if damage == "missing":
            wh.execute("DELETE FROM items WHERE id=?", (item_id,))
        else:
            wh.execute("UPDATE items SET canonical_id=207 WHERE id=?", (item_id,))
        if damage in ("missing", "replaced"):
            env_builder._insert_items(wh, [("REPLACEMENT", 202, 88, 1)])
        wh.commit()
    info = _info(env)[env["lines"][202]]
    assert info["dc_available"] == 0
    assert info["unit_price"] == 0
    assert info["category_name"] == "包材"


@pytest.mark.parametrize("frozen_id", [True, False])
@pytest.mark.parametrize("damage", ["disabled", "alias", "ambiguous", "unit", "conflict", "missing"])
def test_unsafe_bindings_use_existing_default_display(env, frozen_id, damage):
    if not frozen_id:
        _update_line(env, "UPDATE store_order_items SET dc_item_id=NULL WHERE canonical_id=202")
    item_id = env["beta_item_ids"][202]
    with closing(env_builder.connect(env["paths"]["dc_beta"])) as wh:
        if damage == "disabled":
            wh.execute("UPDATE items SET is_active=0 WHERE id=?", (item_id,))
        elif damage == "alias":
            wh.execute("UPDATE items SET is_alias=1 WHERE id=?", (item_id,))
        elif damage == "ambiguous":
            env_builder._insert_items(wh, [("DUPLICATE", 202, 88, 1)])
        elif damage == "unit":
            wh.execute("UPDATE items SET unit='箱' WHERE id=?", (item_id,))
        elif damage == "missing":
            wh.execute("DELETE FROM items WHERE id=?", (item_id,))
        wh.commit()
    if damage == "conflict":
        _update_line(
            env, "INSERT INTO canonical_conflicts (canonical_id, warehouse_code, local_item_id, "
            "field, conflict_type, status, created_at) "
            "VALUES (202, 'dc_beta', ?, 'unit', 'unit_conversion', 'open', ?)",
            (item_id, env_builder.TS),
        )
    info = _info(env)
    assert info[env["lines"][202]] == {
        "name": "品项B", "dc_available": 0.0, "unit_price": 0.0, "category_name": "包材",
    }
    assert info[env["lines"][201]]["dc_available"] == 7


def test_inventory_page_retains_current_store_stock(env):
    contexts = []

    def capture(sender, template, context, **extra):
        if template.name == "inventory.html":
            contexts.append(context)

    with template_rendered.connected_to(capture, env["app"]):
        response = env["client"].get("/inventory")
    assert response.status_code == 200
    quantities = {it["canonical_id"]: it["quantity"] for it in contexts[0]["items"]}
    assert quantities[201] == 7
    assert quantities[202] == 0


@pytest.mark.parametrize("ignored", ["is_alias", "is_active"])
def test_ineligible_duplicate_does_not_make_legacy_binding_ambiguous(env, ignored):
    _update_line(env, "UPDATE store_order_items SET dc_item_id=NULL WHERE canonical_id=202")
    with closing(env_builder.connect(env["paths"]["dc_beta"])) as wh:
        item_id = env_builder._insert_items(wh, [("IGNORED", 202, 88, 1)])[202]
        wh.execute(f"UPDATE items SET {ignored}=? WHERE id=?", (1 if ignored == "is_alias" else 0, item_id))
        wh.commit()
    assert _info(env)[env["lines"][202]]["dc_available"] == 25


def test_price_cost_fallback_and_historical_negative_stock(env):
    with closing(env_builder.connect(env["paths"]["dc_beta"])) as wh:
        wh.execute(
            "UPDATE items SET quantity=-2.5, selling_price=0, unit_cost=4.25 WHERE canonical_id=202"
        )
        wh.commit()
    info = _info(env)[env["lines"][202]]
    assert info["dc_available"] == -2.5
    assert info["unit_price"] == 4.25


@pytest.mark.parametrize("source", ["dc_beta", "not_created"])
def test_missing_source_database_is_not_created_and_other_lines_survive(env, source):
    _update_line(
        env, "UPDATE store_order_items SET source_warehouse_code=? WHERE canonical_id=202",
        (source,),
    )
    missing = env["paths"]["dc_beta"].parent / f"{source}.db"
    missing.unlink(missing_ok=True)
    info = _info(env)
    assert not missing.exists()
    assert info[env["lines"][202]]["dc_available"] == 0
    assert info[env["lines"][201]]["dc_available"] == 7


def test_enrichment_is_read_only_and_does_not_rewrite_snapshots(env):
    paths = [env["master_path"], *env["paths"].values()]
    before = {path: path.read_bytes() for path in paths}
    _info(env)
    assert {path: path.read_bytes() for path in paths} == before
    with closing(env_builder.connect(env["master_path"])) as master:
        assert sop.get_dc_items_for_order_detail(master, 999999) == {}
