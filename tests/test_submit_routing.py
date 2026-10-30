"""Task 7 submit: 路由归仓、快照固化、缺路由整单退回、价格/倍数确认契约。"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta

import pytest

from tests import multi_warehouse_env as env_builder


@pytest.fixture
def env(tmp_path, monkeypatch):
    env = env_builder.build(tmp_path, monkeypatch)
    env_builder.enable(monkeypatch)
    return env


def _tomorrow() -> str:
    return (datetime.now().date() + timedelta(days=1)).isoformat()


def _add(client, canonical_id: int, quantity: float = 1, unit: str = "件") -> None:
    client.post(
        "/store-ordering/cart/add",
        data={"canonical_id": canonical_id, "quantity": str(quantity), "unit": unit},
    )


def _submit(client, **extra):
    data = {"expected_delivery_date": _tomorrow(), "note": ""}
    data.update(extra)
    return client.post("/store-ordering/cart/submit", data=data, follow_redirects=True)


def _orders(master_path) -> list[sqlite3.Row]:
    master = env_builder.connect(master_path)
    rows = master.execute("SELECT * FROM store_orders ORDER BY id").fetchall()
    master.close()
    return rows


def _lines(master_path, order_id: int) -> list[sqlite3.Row]:
    master = env_builder.connect(master_path)
    rows = master.execute(
        "SELECT * FROM store_order_items WHERE order_id=? ORDER BY canonical_id",
        (order_id,),
    ).fetchall()
    master.close()
    return rows


def _set_selling_price(env, dc_code: str, canonical_id: int, price: float) -> None:
    conn = sqlite3.connect(str(env["paths"][dc_code]))
    conn.execute("UPDATE items SET selling_price=? WHERE canonical_id=?", (price, canonical_id))
    conn.commit()
    conn.close()


def _set_multiple(env, dc_code: str, canonical_id: int, multiple: float) -> None:
    conn = sqlite3.connect(str(env["paths"][dc_code]))
    conn.execute("UPDATE items SET order_multiple=? WHERE canonical_id=?", (multiple, canonical_id))
    conn.commit()
    conn.close()


def test_cross_warehouse_order_freezes_line_sources_and_primary_header(env):
    client = env["client"]
    _add(client, 201, 3)   # dc_alpha
    _add(client, 206, 1)   # dc_alpha
    _add(client, 202, 10)  # dc_beta

    resp = _submit(client)
    assert resp.status_code == 200
    orders = _orders(env["master_path"])
    assert len(orders) == 1
    # D8: two alpha lines beat one beta line.
    assert orders[0]["dc_warehouse_code"] == "dc_alpha"

    lines = {row["canonical_id"]: row for row in _lines(env["master_path"], orders[0]["id"])}
    assert lines[201]["source_warehouse_code"] == "dc_alpha"
    assert lines[206]["source_warehouse_code"] == "dc_alpha"
    assert lines[202]["source_warehouse_code"] == "dc_beta"
    assert lines[201]["dc_item_id"] == env["alpha_item_ids"][201]
    assert lines[202]["dc_item_id"] == env["beta_item_ids"][202]
    assert lines[202]["quantity"] == 10


def test_primary_header_tie_breaks_on_smallest_code(env):
    client = env["client"]
    _add(client, 201, 1)  # dc_alpha
    _add(client, 202, 5)  # dc_beta
    _submit(client)
    assert _orders(env["master_path"])[0]["dc_warehouse_code"] == "dc_alpha"


def test_primary_header_follows_line_count(env):
    client = env["client"]
    _add(client, 201, 1)   # dc_alpha
    _add(client, 202, 5)   # dc_beta
    _add(client, 207, 1)   # dc_beta
    _submit(client)
    assert _orders(env["master_path"])[0]["dc_warehouse_code"] == "dc_beta"


def test_missing_routing_rejects_whole_order_with_item_list(env):
    client = env["client"]
    _add(client, 201, 1)
    _add(client, 205, 1)  # no routing row

    resp = _submit(client)
    body = resp.data.decode()
    assert "未配置履约仓" in body
    assert "品项E" in body
    assert _orders(env["master_path"]) == []

    master = env_builder.connect(env["master_path"])
    cart_items = master.execute("SELECT COUNT(*) FROM store_order_cart_items").fetchone()[0]
    master.close()
    assert cart_items == 2  # nothing was consumed


def test_store_exclusive_item_is_rejected_at_submit(env):
    env["client"].post(
        "/store-ordering/cart/add",
        data={"canonical_id": 204, "quantity": "1", "unit": "件"},
    )
    body = _submit(env["client"]).data.decode()
    assert "门店专属" in body
    assert "品项D" in body
    assert _orders(env["master_path"]) == []


def test_routing_change_does_not_rewrite_existing_order(env):
    client = env["client"]
    _add(client, 203, 2)
    _submit(client)
    first = _orders(env["master_path"])[0]
    assert _lines(env["master_path"], first["id"])[0]["source_warehouse_code"] == "dc_alpha"

    master = env_builder.connect(env["master_path"])
    master.execute(
        "UPDATE item_routing SET fulfillment_warehouse_code='dc_beta' WHERE canonical_id=203"
    )
    master.commit()
    master.close()

    # Frozen: the existing order keeps its snapshot; new orders re-resolve.
    assert _lines(env["master_path"], first["id"])[0]["source_warehouse_code"] == "dc_alpha"
    _add(client, 203, 2)
    _submit(client)
    second = _orders(env["master_path"])[-1]
    assert _lines(env["master_path"], second["id"])[0]["source_warehouse_code"] == "dc_beta"


def test_price_change_needs_explicit_confirmation(env):
    client = env["client"]
    _add(client, 201, 2)

    page = client.get("/store-ordering/cart/submit").data.decode()
    assert re.search(r'name="snapshot_price_201" value="9\.9"', page)

    _set_selling_price(env, "dc_alpha", 201, 12.5)
    body = _submit(client, snapshot_price_201="9.9").data.decode()
    assert "价格/起订倍数已变化" in body
    assert "品项A" in body
    assert "12.5" in body
    assert _orders(env["master_path"]) == []

    confirmed = _submit(
        client, snapshot_price_201="12.5", confirm_price_change="1",
    )
    assert confirmed.status_code == 200
    assert "提交成功" in confirmed.data.decode()
    assert len(_orders(env["master_path"])) == 1


def test_multiple_change_needs_explicit_confirmation(env):
    client = env["client"]
    _add(client, 202, 10)

    page = client.get("/store-ordering/cart/submit").data.decode()
    assert re.search(r'name="snapshot_multiple_202" value="5(\.0)?"', page)

    _set_multiple(env, "dc_beta", 202, 2)
    body = _submit(client, snapshot_multiple_202="5").data.decode()
    assert "价格/起订倍数已变化" in body
    assert "品项B" in body
    assert "起订倍数" in body
    assert _orders(env["master_path"]) == []

    confirmed = _submit(
        client, snapshot_multiple_202="2", confirm_price_change="1",
    )
    assert "提交成功" in confirmed.data.decode()
    assert len(_orders(env["master_path"])) == 1


def test_submit_without_snapshot_fields_keeps_working(env):
    client = env["client"]
    _add(client, 201, 1)
    resp = _submit(client)
    assert "提交成功" in resp.data.decode()
    assert len(_orders(env["master_path"])) == 1
