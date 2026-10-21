"""新增工具的单测：门店概览 / 消耗 / 订单 / 帮助。

单独一个文件而不是往 ``test_tools.py`` 里塞：那边的 fixture 是最小库存库，
这里的 fixture 要多两张表（库存流水、订单），混在一起会让两边都难读。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bot import tools

_ITEM_COLUMNS = (
    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "sku TEXT, name TEXT, unit TEXT, category_id INTEGER, "
    "quantity REAL DEFAULT 0, safety_stock REAL DEFAULT 0, "
    "unit_cost REAL DEFAULT 0, selling_price REAL DEFAULT 0, "
    "price_follow_canonical INTEGER DEFAULT 0, canonical_id INTEGER, "
    "is_active INTEGER DEFAULT 1, canonical_status TEXT DEFAULT '', "
    "updated_at TEXT"
)


def _build_warehouse(
    path: Path, rows: list[dict], movements: list[tuple[str, str, float]] | None = None
) -> None:
    """建一个仓的 items + stock_movements。

    ``movements`` 是 ``(sku, action, delta)``，时间统一写成「1 天前」，
    保证落在任何合理的查询窗口内。
    """
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE items ({_ITEM_COLUMNS})")
    conn.execute(
        "CREATE TABLE stock_movements ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER, action TEXT, "
        "delta REAL, note TEXT, created_at TEXT)"
    )
    for row in rows:
        conn.execute(
            "INSERT INTO items (sku, name, unit, quantity, safety_stock, selling_price) "
            "VALUES (:sku, :name, :unit, :quantity, :safety_stock, :selling_price)",
            {
                "unit": "箱",
                "quantity": 0,
                "safety_stock": 0,
                "selling_price": 0,
                **row,
            },
        )
    for sku, action, delta in movements or []:
        item = conn.execute("SELECT id FROM items WHERE sku = ?", (sku,)).fetchone()
        if item:
            conn.execute(
                "INSERT INTO stock_movements (item_id, action, delta, created_at) "
                "VALUES (?, ?, ?, datetime('now', '-1 day'))",
                (item[0], action, delta),
            )
    conn.commit()
    conn.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    db_dir = tmp_path / "db"
    (db_dir / "warehouses").mkdir(parents=True)

    # 门店：2 个品项，1 个缺货、1 个低于安全库存
    _build_warehouse(
        db_dir / "warehouses" / "wh_003.db",
        [
            {
                "sku": "PP017",
                "name": "冷冻芒果酱",
                "quantity": 0,
                "safety_stock": 5,
                "selling_price": 468,
            },
            {
                "sku": "PP089",
                "name": "可颂",
                "quantity": 10,
                "safety_stock": 50,
                "selling_price": 20,
            },
        ],
        movements=[
            ("PP017", "出库", -2.0),  # 计入消耗
            ("PP017", "补货入库", 5.0),  # 不计入
            ("PP017", "出库回退", 1.0),  # 不计入
            ("PP089", "门店订货出库", -3.5),  # 计入消耗
        ],
    )
    # 配送中心：1 个品项
    _build_warehouse(
        db_dir / "warehouses" / "wh_000.db",
        [
            {
                "sku": "PP017",
                "name": "冷冻芒果酱",
                "quantity": 20,
                "safety_stock": 5,
                "selling_price": 393,
            }
        ],
        movements=[("PP017", "总仓调货出库", -6.0)],
    )

    master = sqlite3.connect(db_dir / "master.db")
    master.execute(
        "CREATE TABLE warehouses (code TEXT, name TEXT, db_path TEXT, warehouse_type TEXT)"
    )
    master.execute(
        "CREATE TABLE canonical_items (id INTEGER PRIMARY KEY, sku TEXT, selling_price REAL)"
    )
    master.execute(
        "CREATE TABLE store_orders (id INTEGER PRIMARY KEY, order_no TEXT, "
        "store_warehouse_code TEXT, dc_warehouse_code TEXT, status TEXT, "
        "created_at TEXT, expected_delivery_date TEXT)"
    )
    master.execute(
        "CREATE TABLE store_order_items (id INTEGER PRIMARY KEY, order_id INTEGER, quantity REAL)"
    )
    master.executemany(
        "INSERT INTO warehouses VALUES (?, ?, ?, ?)",
        [
            ("wh_000", "配送中心仓库", "db/warehouses/wh_000.db", "distribution_center"),
            ("wh_003", "新天地店", "db/warehouses/wh_003.db", "storefront"),
        ],
    )
    # 一条在窗口内待发货，一条 40 天前已送达（用于验证天数过滤）
    master.executemany(
        "INSERT INTO store_orders VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                7,
                "SO-20261018-0003",
                "wh_003",
                "wh_000",
                "approved",
                "2026-10-18 16:31:26",
                "2026-10-21",
            ),
            (
                1,
                "SO-20260901-0001",
                "wh_000",
                "wh_000",
                "delivered",
                "2026-09-01 10:00:00",
                "2026-09-03",
            ),
        ],
    )
    master.executemany(
        "INSERT INTO store_order_items VALUES (?, ?, ?)",
        [(1, 7, 2), (2, 7, 3), (3, 1, 1)],
    )
    master.commit()
    master.close()

    monkeypatch.setenv("DAILYCHECK_DB_DIR", str(db_dir))
    monkeypatch.setattr(tools, "DEFAULT_PUBLIC_BASE", "https://kjtang.cn")
    return db_dir


class TestStoreOverview:
    def test_每仓一行且带类型(self, env):
        result = tools.list_store_overview()
        assert result["count"] == 2
        by_code = {w["warehouse_code"]: w for w in result["warehouses"]}
        assert by_code["wh_003"]["type_label"] == "门店"
        assert by_code["wh_000"]["type_label"] == "配送中心"

    def test_门店排在配送中心前面(self, env):
        result = tools.list_store_overview()
        assert result["warehouses"][0]["warehouse_code"] == "wh_003"

    def test_缺货与低库存分开统计(self, env):
        by_code = {w["warehouse_code"]: w for w in tools.list_store_overview()["warehouses"]}
        store = by_code["wh_003"]
        assert store["item_count"] == 2
        assert store["in_stock"] == 1  # 只有可颂有货
        assert store["out_of_stock"] == 1  # 芒果酱 0
        assert store["below_safety"] == 2  # 两个都低于安全库存

    def test_不给跨单位总量(self, env):
        # 箱/个/包相加没有业务含义，工具刻意不返回该字段
        assert "total_quantity" not in tools.list_store_overview()["warehouses"][0]


class TestConsumption:
    def test_只算负向动作(self, env):
        # 出库 -2 计入；补货入库 +5 与出库回退 +1 必须排除，否则消耗会被抵消
        result = tools.get_item_consumption("PP017")
        assert result["total_consumed"] == pytest.approx(8.0)  # 门店 2 + 配送 6

    def test_按仓拆分(self, env):
        result = tools.get_item_consumption("PP017")
        by_code = {w["warehouse_code"]: w for w in result["by_warehouse"]}
        assert by_code["wh_000"]["consumed"] == pytest.approx(6.0)
        assert by_code["wh_003"]["consumed"] == pytest.approx(2.0)

    def test_指定仓只返回一个(self, env):
        result = tools.get_item_consumption("PP017", warehouse_code="新天地店")
        assert [w["warehouse_code"] for w in result["by_warehouse"]] == ["wh_003"]

    def test_计量单位带出来(self, env):
        assert tools.get_item_consumption("PP017")["unit"] == "箱"

    def test_品项不存在时明确报错(self, env):
        result = tools.get_item_consumption("不存在的货")
        assert result["ok"] is False
        assert result["error"] == "not_found"

    def test_天数值防呆(self, env):
        # days=0/None 视为「没指定」，回落默认 30；超大值收敛到 365。
        # 不这么做的话，0 天窗口会查出一份空结果，员工以为「没有消耗」。
        assert tools.get_item_consumption("PP017", days=0)["days"] == 30
        assert tools.get_item_consumption("PP017", days=9999)["days"] == 365


class TestOrders:
    def test_默认返回窗口内订单(self, env):
        result = tools.list_orders(days=7)
        assert result["ok"] is True
        # 40 天前那条不该出现
        assert [o["order_no"] for o in result["orders"]] == ["SO-20261018-0003"]

    def test_状态筛选(self, env):
        result = tools.list_orders(status="approved", days=365)
        assert len(result["orders"]) == 1
        assert result["orders"][0]["status_label"] == "已审批待发货"

    def test_订单带可点链接(self, env):
        order = tools.list_orders(days=365)["orders"][0]
        assert order["url"] == "https://kjtang.cn/store-ordering/orders/7"

    def test_门店名解析成中文(self, env):
        assert tools.list_orders(days=365)["orders"][0]["store"] == "新天地店"

    def test_品项数来自子表(self, env):
        assert tools.list_orders(days=365)["orders"][0]["item_count"] == 2

    def test_状态汇总(self, env):
        summary = tools.list_orders(days=365)["status_summary"]
        assert summary["已审批待发货"] == 1
        assert summary["已送达"] == 1

    def test_列表页链接(self, env):
        assert tools.list_orders()["list_url"] == "https://kjtang.cn/store-ordering/orders"


class TestProjectHelp:
    def test_含看板链接(self, env):
        text = tools.get_project_help()["text"]
        assert "https://kjtang.cn/forecast" in text
        assert "https://kjtang.cn/report/health" in text

    def test_覆盖六类问法(self, env):
        text = tools.get_project_help()["text"]
        for keyword in ("库存", "价格", "补货", "消耗", "订单"):
            assert keyword in text

    def test_不含_emoji(self, env):
        import re

        emoji = re.compile("[\U0001f300-\U0001f9ff\u2600-\u27bf]")
        assert emoji.search(tools.get_project_help()["text"]) is None

    def test_明确说明只读(self, env):
        assert "不能改" in tools.get_project_help()["text"]
