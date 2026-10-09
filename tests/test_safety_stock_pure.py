"""P0-12 安全库存自动计算测试。

规则：safety_stock = Σ近 N 天消耗量 × 系数（默认 7 天 × 1.2）；
消耗历史覆盖不满 N 天窗口 → 写 0。

消耗口径（与 /inventory 页一致）：
  - outbound_requests 中 rolled_back=0 且 reason 非「生产领料(run=#...)」
  - production_run_items JOIN production_runs 中 rolled_back=0

注意：SQLite 的 datetime('now') 为 UTC，测试里生成时间一律用 UTC，
否则本地时区（CST）会让 7 天窗口边界产生 8 小时漂移。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from blueprints.items_pure import (
    compute_safety_stock,
    consumption_history_covers_window,
    recompute_warehouse_safety_stocks,
)
from config import SAFETY_STOCK_FACTOR
from tests.conftest import _seed_item, _wh


def _utc_days_ago(days: float) -> str:
    """UTC 口径的 N 天前时间戳（与 SQLite datetime('now') 同源）。"""
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _seed_outbound_at(
    wh_path, item_id, qty, days_ago,
    *, reason: str | None = None, rolled_back: int = 0,
) -> None:
    conn = _wh(wh_path)
    conn.execute(
        "INSERT INTO outbound_requests "
        "(item_id, requested_quantity, reason, rolled_back, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (item_id, qty, reason, rolled_back, _utc_days_ago(days_ago)),
    )
    conn.commit()
    conn.close()


def _seed_production_at(wh_path, item_id, qty, days_ago) -> None:
    """插入一条生产消耗（product + run + run_item），可指定时间。"""
    conn = _wh(wh_path)
    ts = _utc_days_ago(days_ago)
    cur = conn.execute(
        "INSERT INTO products (name, unit, note, created_at) VALUES ('p', '件', '', ?)",
        (ts,),
    )
    product_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO production_runs (product_id, output_qty, note, rolled_back, created_at) "
        "VALUES (?, 1, 'test', 0, ?)",
        (product_id, ts),
    )
    run_id = cur.lastrowid
    conn.execute(
        "INSERT INTO production_run_items (run_id, item_id, planned_qty, actual_qty) "
        "VALUES (?, ?, ?, ?)",
        (run_id, item_id, qty, qty),
    )
    conn.commit()
    conn.close()


def _get_safety_stock(wh_path, item_id: int) -> float:
    conn = _wh(wh_path)
    value = conn.execute(
        "SELECT safety_stock FROM items WHERE id=?", (item_id,)
    ).fetchone()["safety_stock"]
    conn.close()
    return float(value or 0)


# ───────────────────────── 纯函数 ─────────────────────────

def test_compute_safety_stock_applies_factor():
    assert compute_safety_stock(10) == 10 * SAFETY_STOCK_FACTOR
    assert compute_safety_stock(0) == 0.0


def test_compute_safety_stock_rounds_2dp():
    # 3.33 * 1.2 = 3.996 → 4.0
    assert compute_safety_stock(3.33) == 4.0


def test_history_covers_window_only_after_full_window(logged_client):
    _, wh_path = logged_client
    item_id, _ = _seed_item(wh_path, "满窗品项", qty=0, unit_cost=1)
    conn = _wh(wh_path)
    assert consumption_history_covers_window(conn, item_id) is False  # 无记录

    _seed_outbound_at(wh_path, item_id, 1, days_ago=2)
    assert consumption_history_covers_window(conn, item_id) is False  # 只有 2 天

    _seed_outbound_at(wh_path, item_id, 1, days_ago=9)
    assert consumption_history_covers_window(conn, item_id) is True  # 覆盖满
    conn.close()


def test_recompute_uses_window_sum_times_factor(logged_client):
    _, wh_path = logged_client
    item_id, _ = _seed_item(wh_path, "窗口品项", qty=0, unit_cost=1)
    _seed_outbound_at(wh_path, item_id, 1, days_ago=10)   # 窗口外，仅用于满足覆盖
    _seed_outbound_at(wh_path, item_id, 5, days_ago=2)
    _seed_outbound_at(wh_path, item_id, 3, days_ago=6)

    conn = _wh(wh_path)
    result = recompute_warehouse_safety_stocks(conn, window_days=7, factor=1.2)
    conn.commit()
    conn.close()

    assert result["window_days"] == 7
    assert result["factor"] == 1.2
    assert _get_safety_stock(wh_path, item_id) == 9.6  # (5 + 3) * 1.2


def test_recompute_writes_zero_when_history_too_short(logged_client):
    _, wh_path = logged_client
    item_id, _ = _seed_item(wh_path, "新品项", qty=0, unit_cost=1)
    _seed_outbound_at(wh_path, item_id, 100, days_ago=1)  # 只有 1 天历史

    conn = _wh(wh_path)
    result = recompute_warehouse_safety_stocks(conn)
    conn.commit()
    conn.close()

    assert result["zeroed"] == 1
    assert _get_safety_stock(wh_path, item_id) == 0.0


def test_recompute_writes_zero_without_consumption(logged_client):
    _, wh_path = logged_client
    item_id, _ = _seed_item(wh_path, "无消耗品项", qty=0, unit_cost=1)

    conn = _wh(wh_path)
    result = recompute_warehouse_safety_stocks(conn)
    conn.commit()
    conn.close()

    assert result["zeroed"] == 1
    assert _get_safety_stock(wh_path, item_id) == 0.0


def test_recompute_excludes_rolled_back_outbound(logged_client):
    _, wh_path = logged_client
    item_id, _ = _seed_item(wh_path, "回退出库品项", qty=0, unit_cost=1)
    _seed_outbound_at(wh_path, item_id, 1, days_ago=10)  # 覆盖满
    _seed_outbound_at(wh_path, item_id, 5, days_ago=2)
    _seed_outbound_at(wh_path, item_id, 99, days_ago=2, rolled_back=1)

    conn = _wh(wh_path)
    recompute_warehouse_safety_stocks(conn)
    conn.commit()
    conn.close()

    assert _get_safety_stock(wh_path, item_id) == 6.0  # 5 * 1.2，99 不计


def test_recompute_counts_production_consumption(logged_client):
    _, wh_path = logged_client
    item_id, _ = _seed_item(wh_path, "生产耗用品项", qty=0, unit_cost=1)
    _seed_outbound_at(wh_path, item_id, 1, days_ago=10)   # 覆盖满
    _seed_production_at(wh_path, item_id, 4, days_ago=3)

    conn = _wh(wh_path)
    recompute_warehouse_safety_stocks(conn)
    conn.commit()
    conn.close()

    assert _get_safety_stock(wh_path, item_id) == 4.8  # 4 * 1.2


def test_recompute_skips_production_picking_outbound(logged_client):
    """生产领料出库是产量的镜像，不能与 production_run_items 重复计数。"""
    _, wh_path = logged_client
    item_id, _ = _seed_item(wh_path, "领料镜像品项", qty=0, unit_cost=1)
    _seed_outbound_at(wh_path, item_id, 1, days_ago=10)  # 覆盖满
    _seed_outbound_at(wh_path, item_id, 100, days_ago=3,
                      reason="生产领料(run=#12)")
    _seed_production_at(wh_path, item_id, 4, days_ago=3)

    conn = _wh(wh_path)
    recompute_warehouse_safety_stocks(conn)
    conn.commit()
    conn.close()

    assert _get_safety_stock(wh_path, item_id) == 4.8  # 只算生产消耗，领料 100 排除


def test_recompute_is_idempotent(logged_client):
    _, wh_path = logged_client
    item_id, _ = _seed_item(wh_path, "幂等品项", qty=0, unit_cost=1)
    _seed_outbound_at(wh_path, item_id, 1, days_ago=10)
    _seed_outbound_at(wh_path, item_id, 5, days_ago=2)

    conn = _wh(wh_path)
    recompute_warehouse_safety_stocks(conn)
    conn.commit()
    first = _get_safety_stock(wh_path, item_id)
    recompute_warehouse_safety_stocks(conn)
    conn.commit()
    conn.close()

    assert first == _get_safety_stock(wh_path, item_id) == 6.0


def test_recompute_returns_stats(logged_client):
    _, wh_path = logged_client
    _seed_item(wh_path, "A", qty=0, unit_cost=1)
    _seed_item(wh_path, "B", qty=0, unit_cost=1)

    conn = _wh(wh_path)
    result = recompute_warehouse_safety_stocks(conn)
    conn.commit()
    conn.close()

    assert result["total"] == 2
    assert result["updated"] == 2
    assert result["zeroed"] == 2


# ───────────────────────── 路由 ─────────────────────────

def test_recompute_route_requires_platform_admin(staff_client):
    client, _ = staff_client
    resp = client.post("/items/recompute-safety-stock")
    assert resp.status_code == 403


def test_recompute_route_persists_and_redirects(logged_client):
    client, wh_path = logged_client
    item_id, _ = _seed_item(wh_path, "路由品项", qty=0, unit_cost=1)
    _seed_outbound_at(wh_path, item_id, 1, days_ago=10)
    _seed_outbound_at(wh_path, item_id, 10, days_ago=2)

    resp = client.post("/items/recompute-safety-stock")
    assert resp.status_code == 302
    assert _get_safety_stock(wh_path, item_id) == 12.0


def test_items_page_shows_recompute_button_for_admin(logged_client):
    client, _ = logged_client
    resp = client.get("/items")
    assert resp.status_code == 200
    assert "重算安全库存".encode() in resp.data
