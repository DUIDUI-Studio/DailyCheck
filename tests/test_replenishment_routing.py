"""多仓履约：补货看板 / 采购建议按路由归仓（spec §10 / Task 9）。

验收口径：

* 门店需求只拉动 `item_routing` 指向的**来源仓**；非来源仓镜像保留自身水位
  逻辑，不承接门店需求，也不静默归零；
* 未配置路由的 canonical 维持现状算法（所有 DC 都按下游拉动算）；
* 可订开关从路由来源仓解析（修复 `dc_orderable[canonical_id]` 多 DC 覆盖 bug）；
* 采购邮件按路由去重 DC 行：双 DC 镜像同 canonical、路由=B → 只出现 B 一次；
* 开关关闭时全部回落到现状（路由表存在也不生效）。
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest

from blueprints import procurement_digest_pure as pdp
from blueprints.replenishment_data import build_board, load_params

TODAY = date.today()
TS = "2026-10-29 12:00:00"

# canonical → (名称, 路由来源仓)
CANONICALS = (
    (301, "路由品R", "dc_beta"),   # 双 DC 镜像；路由 = beta
    (302, "无路由品U", None),       # 未配置路由 → 维持现状
    (303, "可订品C", "dc_alpha"),   # 来源仓 alpha 自采（is_orderable=0）
)

# (warehouse, canonical, is_orderable)
ITEMS = (
    ("dc_alpha", 301, 1),
    ("dc_alpha", 302, 1),
    ("dc_alpha", 303, 0),
    ("dc_beta", 301, 1),
    ("dc_beta", 302, 1),
    ("dc_beta", 303, 1),
    ("store_one", 301, 1),
    ("store_one", 302, 1),
    ("store_one", 303, 1),
)


def _days_ago(days: int) -> str:
    return (TODAY - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture
def routing_env(tmp_path, monkeypatch):
    import config as config_module
    import db as db_module

    master_path = tmp_path / "master.db"
    wh_dir = tmp_path / "warehouses"
    wh_dir.mkdir()
    paths = {code: wh_dir / f"{code}.db" for code in ("dc_alpha", "dc_beta", "store_one")}

    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_dir)
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", wh_dir)
    monkeypatch.setattr(config_module, "MULTI_WAREHOUSE_FULFILLMENT", True, raising=False)

    db_module.init_master_db()
    for path in paths.values():
        db_module.init_warehouse_db(path)

    master = sqlite3.connect(str(master_path))
    master.row_factory = sqlite3.Row
    for wid, code, kind in (
        (1, "dc_alpha", "distribution_center"),
        (2, "dc_beta", "distribution_center"),
        (3, "store_one", "storefront"),
    ):
        master.execute(
            "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (wid, code, code, str(paths[code]), kind, TS))
    master.execute(
        "INSERT INTO canonical_categories (code, name, description, created_at, updated_at) "
        "VALUES ('PACKAGING', '包材', '', ?, ?)", (TS, TS))
    for cid, name, _route in CANONICALS:
        master.execute(
            "INSERT INTO canonical_items (id, canonical_sku, name, category_code, unit, "
            "gram_per_unit, aux_unit, aux_rate, status, created_from, created_at, updated_at) "
            "VALUES (?, ?, ?, 'PACKAGING', '件', 0, NULL, 0, 'active', 'rd_manual', ?, ?)",
            (cid, f"IC-{cid:06d}", name, TS, TS))
    for cid, _name, route in CANONICALS:
        if route:
            master.execute(
                "INSERT INTO item_routing (tenant_id, canonical_id, fulfillment_warehouse_code, "
                "updated_at, updated_by) VALUES (1, ?, ?, ?, NULL)", (cid, route, TS))
    master.commit()
    master.close()

    item_ids: dict[tuple[str, int], int] = {}
    name_by_cid = {cid: name for cid, name, _route in CANONICALS}
    for wh_code, cid, orderable in ITEMS:
        conn = sqlite3.connect(str(paths[wh_code]))
        conn.row_factory = sqlite3.Row
        category_id = conn.execute(
            "SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()[0]
        cur = conn.execute(
            "INSERT INTO items (sku, name, category_id, quantity, unit, unit_cost, "
            "canonical_id, is_orderable, updated_at) "
            "VALUES (?, ?, ?, 0, '件', 10.0, ?, ?, ?)",
            (f"{wh_code}-{cid}", name_by_cid[cid], category_id, cid, orderable, TS))
        item_ids[(wh_code, cid)] = int(cur.lastrowid)
        conn.commit()
        conn.close()

    # 门店消耗：三个 canonical 都有需求（下游拉动序列非零）
    for cid in (301, 302, 303):
        _seed_movements(paths["store_one"], item_ids[("store_one", cid)], "出库", 5, 10.0)
    # alpha 镜像的自身水位：有自己的出库史（不承接门店需求后仍有建议量）
    _seed_movements(paths["dc_alpha"], item_ids[("dc_alpha", 301)], "门店订货出库", 5, 10.0)

    return {
        "master_path": master_path,
        "paths": paths,
        "item_ids": item_ids,
        "routing": {cid: route for cid, _name, route in CANONICALS if route},
    }


def _seed_movements(path, item_id: int, action: str, days: int, qty: float) -> None:
    conn = sqlite3.connect(str(path))
    for days_ago in range(1, days + 1):
        conn.execute(
            "INSERT INTO stock_movements (item_id, action, delta, note, created_at) "
            "VALUES (?, ?, ?, NULL, ?)",
            (item_id, action, -qty, _days_ago(days_ago)))
    conn.commit()
    conn.close()


def _row(rows: list[dict], wh_code: str, cid: int) -> dict | None:
    for row in rows:
        if row.get("warehouse_code") == wh_code and row.get("canonical_id") == cid:
            return row
    return None


def test_demand_pulls_only_routed_source_warehouse(routing_env):
    """路由=B → B 承接门店需求；A 镜像保留自身水位逻辑。"""
    board = build_board(today=TODAY, params=load_params())
    rows = board["rows"]

    beta = _row(rows, "dc_beta", 301)
    alpha = _row(rows, "dc_alpha", 301)
    assert beta is not None and alpha is not None
    assert beta["demand_source"] == "downstream"
    assert beta["status"] == "critical"
    assert beta["suggested_qty"] > 0

    # 镜像仓不承接门店需求（不静默归零：它按自己的出库史算出水位）
    assert alpha["demand_source"] == "own"
    assert alpha["status"] == "critical"
    assert alpha["suggested_qty"] > 0


def test_unrouted_canonical_keeps_legacy_behaviour(routing_env):
    """未配置路由的 canonical：两个 DC 仍按现状都承接下游需求。"""
    board = build_board(today=TODAY, params=load_params())
    rows = board["rows"]
    assert _row(rows, "dc_alpha", 302)["demand_source"] == "downstream"
    assert _row(rows, "dc_beta", 302)["demand_source"] == "downstream"


def test_orderable_flag_resolves_from_routed_source(routing_env):
    """可订开关按来源仓解析：alpha=0 / beta=1，来源是 alpha → 门店侧为自采。

    旧实现按 DC 遍历 last-wins，beta 是最后一个 DC，会把开关错误地读成 1。
    """
    board = build_board(today=TODAY, params=load_params())
    store_row = _row(board["rows"], "store_one", 303)
    assert store_row is not None
    assert store_row["orderable"] is False
    assert store_row["supply"] == "self"
    # 来源仓自采 → 该 DC 行退出补货计算
    assert _row(board["rows"], "dc_alpha", 303) is None


def test_procurement_picks_routed_dc_once(routing_env):
    """双 DC 镜像同 canonical、路由=B → 采购拉动仅 B 出现一次。"""
    board = build_board(today=TODAY, params=load_params())
    rows = board["rows"]

    picked = pdp.pick_purchase_rows(rows, routing=routing_env["routing"])
    matched = [r for r in picked if r.get("canonical_id") == 301]
    assert [r["warehouse_code"] for r in matched] == ["dc_beta"]

    digest = pdp.build_digest(
        day=TODAY, rows=rows, stores=[], routing=routing_env["routing"])
    names = [item["name"] for item in digest["items"]]
    assert names.count("路由品R") == 1
    assert digest["item_count_total"] == len(
        [r for r in pdp.pick_purchase_rows(rows, routing=routing_env["routing"])])


def test_flag_off_falls_back_to_current_behaviour(routing_env, monkeypatch):
    """开关关：路由表存在也不生效，补货与采购口径与现状恒等。"""
    import config as config_module

    monkeypatch.setattr(config_module, "MULTI_WAREHOUSE_FULFILLMENT", False, raising=False)
    board = build_board(today=TODAY, params=load_params())
    rows = board["rows"]

    # 现状：所有 DC 都按下游拉动（含镜像仓）
    assert _row(rows, "dc_alpha", 301)["demand_source"] == "downstream"
    # 现状：dc_orderable 为 last-wins（beta=1 覆盖 alpha=0）→ 门店侧视为统配
    assert _row(rows, "store_one", 303)["orderable"] is True

    picked = pdp.pick_purchase_rows(rows)
    assert len([r for r in picked if r.get("canonical_id") == 301]) == 2
