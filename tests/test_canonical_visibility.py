"""主数据「全公司停用」在门店侧的可见性回归（2026-10-10）。

背景：主数据 canonical_items.status='disabled' 经扇出落到门店行
items.canonical_status='disabled'，但门店侧此前从不读该字段 —— 停用品项
列表照显示、出库/入库/生产/盘点/调整照可选（Q3 门禁从未实现）。

owner 2026-10-10 拍板「列表直接隐藏」，据此补两处读取：
  · 列表隐藏：/items（品类与品项）、/inventory（库存）
  · 选择点排除：/outbound/session、/restock/session、
                /stocktake/session、/adjustment/session

与 P0-10 单仓启停(items.is_active) 相互独立：
  - 单仓停用 → 选择点排除，但列表**仍可见**（显示「已停用」徽标）；
  - 全公司停用 → 列表隐藏 + 选择点排除。
"""
from __future__ import annotations

from tests.conftest import _seed_item, _wh

SELECT_ROUTES = (
    "/outbound/session",
    "/restock/session",
    "/stocktake/session",
)


def _mark_canonical(wh_path, item_id: int, status: str, canonical_id: int = 1):
    """模拟扇出后的门店行状态：绑定 canonical_id + 写 canonical_status。"""
    conn = _wh(wh_path)
    conn.execute(
        "UPDATE items SET canonical_id=?, canonical_status=? WHERE id=?",
        (canonical_id, status, item_id),
    )
    conn.commit()
    conn.close()


def _set_active(wh_path, item_id: int, active: int):
    conn = _wh(wh_path)
    conn.execute("UPDATE items SET is_active=? WHERE id=?", (active, item_id))
    conn.commit()
    conn.close()


# ────────────────────────────── 纯谓词 ──────────────────────────────

def test_clause_generation():
    from blueprints import canonical_pure as cp

    assert cp.CANONICAL_DISABLED == "disabled"
    assert cp.store_visible_clause("i") == (
        "COALESCE(i.canonical_status, '') <> 'disabled'"
    )
    assert cp.store_selectable_clause("i") == (
        "COALESCE(i.is_active, 1) = 1 "
        "AND COALESCE(i.canonical_status, '') <> 'disabled'"
    )
    # alias 可换（跨表查询时用）
    assert "x.canonical_status" in cp.store_visible_clause("x")
    assert "x.is_active" in cp.store_selectable_clause("x")


# ────────────────────────────── 列表隐藏 ──────────────────────────────

def test_items_list_hides_disabled(logged_client):
    client, wh_path = logged_client
    _seed_item(wh_path, "在售冰碗", 5, 10)
    off_id, _ = _seed_item(wh_path, "停用冰碗", 5, 10)
    _mark_canonical(wh_path, off_id, "disabled")

    body = client.get("/items").get_data(as_text=True)
    assert "在售冰碗" in body
    assert "停用冰碗" not in body


def test_inventory_hides_disabled(logged_client):
    client, wh_path = logged_client
    _seed_item(wh_path, "在售冰碗", 5, 10)
    off_id, _ = _seed_item(wh_path, "停用冰碗", 5, 10)
    _mark_canonical(wh_path, off_id, "disabled")

    body = client.get("/inventory").get_data(as_text=True)
    assert "在售冰碗" in body
    assert "停用冰碗" not in body


def test_active_and_unbound_stay_visible(logged_client):
    """纳管且 active + 未纳管（canonical_status IS NULL）都必须保留。"""
    client, wh_path = logged_client
    on_id, _ = _seed_item(wh_path, "在售冰碗", 5, 10)
    _mark_canonical(wh_path, on_id, "active")
    _seed_item(wh_path, "自建冰碗", 5, 10)  # 未纳管

    body = client.get("/items").get_data(as_text=True)
    assert "在售冰碗" in body
    assert "自建冰碗" in body


def test_single_warehouse_disable_keeps_row_visible(logged_client):
    """P0-10 单仓停用：列表**不隐藏**（只徽标变「已停用」），与全公司停用区分。"""
    client, wh_path = logged_client
    off_id, _ = _seed_item(wh_path, "单仓停用冰碗", 5, 10)
    _mark_canonical(wh_path, off_id, "active")
    _set_active(wh_path, off_id, 0)

    body = client.get("/items").get_data(as_text=True)
    assert "单仓停用冰碗" in body


# ────────────────────────────── 选择点排除 ──────────────────────────────

def test_selection_points_exclude_disabled(logged_client):
    client, wh_path = logged_client
    _seed_item(wh_path, "在售冰碗", 5, 10)
    off_id, _ = _seed_item(wh_path, "停用冰碗", 5, 10)
    _mark_canonical(wh_path, off_id, "disabled")

    for route in SELECT_ROUTES:
        resp = client.get(route)
        assert resp.status_code == 200, f"{route} -> {resp.status_code}"
        body = resp.get_data(as_text=True)
        assert "在售冰碗" in body, f"{route} 应包含在售品项"
        assert "停用冰碗" not in body, f"{route} 不应出现停用品项"


def test_selection_points_keep_unbound(logged_client):
    """未纳管行（canonical_status IS NULL）必须仍可选。"""
    client, wh_path = logged_client
    _seed_item(wh_path, "自建冰碗", 5, 10)

    for route in SELECT_ROUTES:
        body = client.get(route).get_data(as_text=True)
        assert "自建冰碗" in body, f"{route} 未纳管行应可选"
