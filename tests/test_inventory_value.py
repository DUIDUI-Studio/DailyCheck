"""/inventory 库存价值口径回归测试。

storefront 仓库按 selling_price(售价) 计算库存价值，
R&D 仓库按 unit_cost(成本) 计算。回归 commit 76f18bc 引入的 bug:
storefront 曾把 cost gating 到 `is_rd`，导致价值恒为 0。
"""
import sqlite3

from db import migrate_warehouse_db_columns
from tests.conftest import _seed_item


def _set_selling_price(wh_path, item_id, price):
    conn = sqlite3.connect(wh_path)
    conn.execute("UPDATE items SET selling_price = ? WHERE id = ?", (price, item_id))
    conn.commit()
    conn.close()


def test_storefront_inventory_value_uses_selling_price(logged_client):
    """storefront 库存价值 = quantity × selling_price，而非 unit_cost 或 0。"""
    client, wh_path = logged_client
    item_id, _ = _seed_item(wh_path, "valItem", qty=10, unit_cost=5)
    # 模拟生产行为：selling_price 列由 get_warehouse_db() 在首个请求时懒迁移，
    # 直接 UPDATE 前先补列。
    migrate_warehouse_db_columns(wh_path)
    # selling_price=25 与 unit_cost=5 区分开，验证用的是售价而非成本。
    _set_selling_price(wh_path, item_id, 25)

    resp = client.get("/inventory")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")

    # 库存总金额 = 10 × 25 = 250.00（summary 汇总块 + 卡片「库存金额」各出现一次）
    assert "¥ 250.00" in body, "storefront 库存价值应按 selling_price(25)×数量(10)=¥ 250.00"
    # 不应按 unit_cost 计算（10 × 5 = 50.00）
    assert "¥ 50.00" not in body, "storefront 库存价值不应回退到 unit_cost(5)×数量(10)=¥ 50.00"
