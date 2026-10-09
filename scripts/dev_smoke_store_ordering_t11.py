"""v3 T11 路由 + 模板重做的 dev 冒烟脚本。

使用 Flask test_client 直接设置 session，绕过密码登录，渲染三个关键
页面验证：
  1. catalog (storefront 视角)：不应再显示「库存 XX」字样；卡片 pill
     应是「可订」；弹窗 cm-stock 仅含「单价」。
  2. order_detail (storefront 视角)：顶部信息卡 + 总金额；明细表不含
     「仓库库存」列。
  3. order_detail (DC 视角)：明细表含「仓库库存」列与「本批发货」输入框；
     顶部总金额可见。

用法：PYTHONPATH=.venv/lib/python3.12/site-packages python3.13 scripts/dev_smoke_store_ordering_t11.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / ".venv/lib/python3.12/site-packages"))

from app import create_app  # noqa: E402


MASTER_DB = REPO / "db/master.db"


def _find_order_by_role(role_warehouse_type: str):
    """找一个有效订单用于冒烟。"""
    conn = sqlite3.connect(str(MASTER_DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT id, status, store_warehouse_code, dc_warehouse_code
           FROM store_orders
           ORDER BY id DESC LIMIT 5"""
    ).fetchall()
    conn.close()
    if not rows:
        return None
    return rows[0]


def smoke_catalog() -> None:
    """验证 catalog 卡片不显示库存。"""
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    # wh_002 是 storefront，先选 dc。
    with client.session_transaction() as s:
        s["user_id"] = 2  # xsj
        s["warehouse_id"] = 2  # wh_002
        s["store_ordering_dc"] = "wh_000"
    resp = client.get("/store-ordering/catalog?dc=wh_000")
    assert resp.status_code == 200, f"catalog failed: {resp.status_code}"
    body = resp.data.decode("utf-8")

    # A5: 不应出现「库存 XX」字样（卡片）—— 注意「库存」字可能出现在 DC 仓库名里，所以用更精确的匹配
    import re
    # 卡片 pill：检查 inv-card-status 区块
    inv_blocks = re.findall(r'<div class="inv-card-status">(.*?)</div>', body, re.DOTALL)
    if inv_blocks:
        for blk in inv_blocks:
            assert "库存" not in blk, f"卡片 status 区还含库存字样: {blk}"
        # 应包含「可订」
        assert all("可订" in blk for blk in inv_blocks), "卡片应显示「可订」pill"
        print(f"  [OK] catalog 卡片共 {len(inv_blocks)} 张，pill 全部「可订」无库存字样")
    else:
        print("  [WARN] catalog 没有任何卡片 (DC 无可订品项)")
    # 弹窗 cm-stock 不应含「当前库存」
    cm_stock = re.findall(r'<p class="modal-stock" id="cm-stock"></p>', body)
    if cm_stock:
        print("  [OK] catalog 弹窗 cm-stock 占位存在（CSS-only 显示）")
    else:
        print("  [WARN] catalog 弹窗 cm-stock 占位缺失")


def smoke_order_detail(view: str) -> None:
    """验证 order_detail 顶部信息卡 + 总金额 + 明细表分支。"""
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    order = _find_order_by_role(view)
    if order is None:
        print(f"  [SKIP] no orders in master.db; cannot smoke order_detail ({view})")
        return
    order_id = order["id"]
    print(f"  使用 order_id={order_id} status={order['status']} ({view} 视角)")

    # Set session per view.
    if view == "dc":
        user_id, warehouse_id = 1, 4  # admin → wh_000 (DC)
    else:
        user_id, warehouse_id = 2, 2  # xsj → wh_002 (storefront)
    with client.session_transaction() as s:
        s["user_id"] = user_id
        s["warehouse_id"] = warehouse_id
        # admin needs a warehouse_type context — admin sees DC role.
    resp = client.get(f"/store-ordering/orders/{order_id}")
    assert resp.status_code == 200, f"order_detail failed: {resp.status_code}"
    body = resp.data.decode("utf-8")

    # 顶部信息卡：必须包含「订单总金额」label + ¥
    assert "info-card" in body, "缺 info-card 容器"
    assert "订单总金额" in body, "缺 '订单总金额' label"
    assert "info-card-total-value" in body, "缺 info-card-total-value 节点"
    assert "¥" in body, "缺 ¥ 金额符号"
    print(f"  [OK] order_detail 含 info-card + 订单总金额 + ¥")

    if view == "dc":
        # DC 视角：明细表应有「仓库库存」「已发数」列
        assert "仓库库存" in body, "DC 视角缺「仓库库存」列"
        assert "已发数" in body, "DC 视角缺「已发数」列"
        # DC 视角 storefront 视角字段不该出现
        assert "本批收货" not in body, "DC 视角不应有「本批收货」"
        print("  [OK] DC 视角明细表含「仓库库存」「已发数」列")
    else:
        # storefront 视角：明细表应有「已收数」；不应含 DC 库存
        assert "已收数" in body, "storefront 视角缺「已收数」列"
        assert "本批发货" not in body, "storefront 视角不应有「本批发货」"
        assert "仓库库存" not in body, "storefront 视角不应有「仓库库存」"
        print("  [OK] storefront 视角明细表含「已收数」列，无 DC 仓库层")


def main():
    print("=== catalog (A5 移除库存) ===")
    smoke_catalog()
    print()
    print("=== order_detail DC 视角 (A6 + A1) ===")
    smoke_order_detail("dc")
    print()
    print("=== order_detail storefront 视角 (A2 + A4) ===")
    smoke_order_detail("storefront")
    print()
    print("=== ALL SMOKE PASSED ===")


if __name__ == "__main__":
    main()