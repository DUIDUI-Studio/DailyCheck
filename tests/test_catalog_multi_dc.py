"""Task 7 catalog: 去 DC 选择器、路由合并目录、专属品过滤、草稿键 v2。

Flag off (default) must keep the legacy single-DC selector behaviour; flag on
merges every routed source warehouse into one list for the storefront.
"""
from __future__ import annotations

import json
import re

import pytest

from tests import multi_warehouse_env as env_builder


@pytest.fixture
def env(tmp_path, monkeypatch):
    return env_builder.build(tmp_path, monkeypatch)


def _card_attr(body: str, canonical_id: int, attr: str) -> str | None:
    match = re.search(
        rf'data-canonical-id="{canonical_id}"(?:(?!data-canonical-id=).)*?'
        rf'{attr}="([^"]+)"',
        body,
        re.DOTALL,
    )
    return match.group(1) if match else None


def test_flag_off_catalog_keeps_single_dc_selector(env):
    """Default flag: untouched legacy flow — selector then one DC's rows."""
    body = env["client"].get("/store-ordering/catalog").data.decode()
    assert "选择配送中心" in body
    assert "dc_alpha" in body and "dc_beta" in body

    body = env["client"].get("/store-ordering/catalog?dc=dc_alpha").data.decode()
    assert "当前配送中心" in body
    assert _card_attr(body, 201, "data-unit-price") == "9.9"
    assert _card_attr(body, 202, "data-unit-price") is None  # beta-only row
    # Legacy view is header-DC scoped: unrouted/exclusive rows still show.
    assert _card_attr(body, 205, "data-unit-price") == "5.0"


def test_flag_on_catalog_merges_dcs_hides_unrouted_and_exclusive(env, monkeypatch):
    env_builder.enable(monkeypatch)
    body = env["client"].get("/store-ordering/catalog").data.decode()

    assert "选择配送中心" not in body
    assert "当前配送中心" not in body

    # Alpha-routed and beta-routed items share one catalogue.
    assert _card_attr(body, 201, "data-unit-price") == "9.9"
    assert _card_attr(body, 206, "data-unit-price") == "6.0"
    assert _card_attr(body, 202, "data-unit-price") == "1.5"
    assert _card_attr(body, 207, "data-unit-price") == "7.0"
    # Price/multiple come from the routed source, never the mirror DC.
    assert "99.0" not in body
    assert _card_attr(body, 202, "data-multiple") == "5"

    # Unrouted (205) and store-exclusive (204) never enter a DC catalogue.
    assert 'data-canonical-id="205"' not in body
    assert 'data-canonical-id="204"' not in body


def test_flag_on_catalog_filters_from_the_routed_dc(env, monkeypatch):
    env_builder.enable(monkeypatch)
    body = env["client"].get("/store-ordering/catalog?cat=PACKAGING").data.decode()
    assert _card_attr(body, 201, "data-unit-price") == "9.9"

    body = env["client"].get("/store-ordering/catalog?q=DCB-A").data.decode()
    assert 'data-canonical-id="201"' not in body  # mirror SKU lives in the other DC
    body = env["client"].get("/store-ordering/catalog?q=DCB-B").data.decode()
    assert _card_attr(body, 202, "data-unit-price") == "1.5"


def test_flag_on_draft_key_is_user_and_store_v2(env, monkeypatch):
    env_builder.enable(monkeypatch)
    body = env["client"].get("/store-ordering/catalog").data.decode()

    assert "dc.catalogDraft.v2:" in body
    match = re.search(r"var DRAFT_SCOPE = (\"[^\"]*\");", body)
    assert match, "catalog.html 未注入 DRAFT_SCOPE"
    assert json.loads(match.group(1)) == "1:store_one"
    # Legacy per-DC drafts are discarded with a visible prompt.
    assert "dc.catalogDraft.v1:" in body
    assert "已丢弃" in body
    # Draft restore validates against the full (unfiltered) valid set.
    assert "VALID_DRAFT_ITEMS" in body
    assert '"201"' in body


def test_flag_on_cart_writes_null_dc_and_single_cart_per_user_store(env, monkeypatch):
    env_builder.enable(monkeypatch)
    client = env["client"]
    client.post("/store-ordering/cart/add", data={"canonical_id": 201, "quantity": "3", "unit": "件"})
    client.post("/store-ordering/cart/add", data={"canonical_id": 202, "quantity": "5", "unit": "件"})

    master = env_builder.connect(env["master_path"])
    carts = master.execute("SELECT * FROM store_order_carts").fetchall()
    master.close()
    assert len(carts) == 1
    assert carts[0]["dc_warehouse_code"] is None
    assert carts[0]["store_warehouse_code"] == "store_one"

    body = client.get("/store-ordering/cart").data.decode()
    assert "配送中心：" not in body
    assert "品项A" in body and "品项B" in body
    assert "dc.catalogDraft.v2:" in body


def test_flag_on_deprecated_session_dc_is_dropped(env, monkeypatch):
    env_builder.enable(monkeypatch)
    client = env["client"]
    with client.session_transaction() as session:
        session["store_ordering_dc"] = "dc_alpha"
    body = client.get("/store-ordering/catalog").data.decode()
    assert "选择配送中心" not in body
    assert _card_attr(body, 202, "data-unit-price") == "1.5"
    with client.session_transaction() as session:
        assert "store_ordering_dc" not in session
