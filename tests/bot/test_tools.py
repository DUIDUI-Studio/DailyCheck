"""工具层单测：用临时 SQLite 造一套最小仓库数据。

重点覆盖三件容易出错的事：只读边界、跨仓汇总、售价缺失的语义。
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


def _make_warehouse_db(path: Path, rows: list[dict]) -> None:
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE items ({_ITEM_COLUMNS})")
    conn.execute("CREATE TABLE categories (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO categories (id, name) VALUES (1, '原料'), (2, '包材')")
    for row in rows:
        conn.execute(
            "INSERT INTO items (sku, name, unit, category_id, quantity, safety_stock, "
            "unit_cost, selling_price, price_follow_canonical, canonical_id, "
            "is_active, canonical_status) "
            "VALUES (:sku, :name, :unit, :category_id, :quantity, :safety_stock, "
            ":unit_cost, :selling_price, :price_follow_canonical, :canonical_id, "
            ":is_active, :canonical_status)",
            {
                "unit": "箱",
                "category_id": 1,
                "quantity": 0,
                "safety_stock": 0,
                "unit_cost": 0,
                "selling_price": 0,
                "price_follow_canonical": 0,
                "canonical_id": None,
                "is_active": 1,
                "canonical_status": "",
                **row,
            },
        )
    conn.commit()
    conn.close()


@pytest.fixture
def warehouse_env(tmp_path, monkeypatch):
    """造两个仓 + master，并把 DAILYCHECK_DB_DIR 指过去。"""
    db_dir = tmp_path / "db"
    (db_dir / "warehouses").mkdir(parents=True)

    _make_warehouse_db(
        db_dir / "warehouses" / "wh_000.db",
        [
            {
                "sku": "PP017",
                "name": "冷冻芒果酱",
                "quantity": 1.2,
                "safety_stock": 5,
                "unit_cost": 393,
                "selling_price": 468,
            },
            {
                "sku": "PP089",
                "name": "可颂",
                "quantity": 0,
                "safety_stock": 50,
                "unit_cost": 10,
                "selling_price": 20,
            },
            # 停用品项：不该出现在任何查询结果里
            {
                "sku": "PP099",
                "name": "停用货",
                "quantity": 999,
                "selling_price": 1,
                "is_active": 0,
            },
            # 主数据被停用
            {
                "sku": "PP098",
                "name": "主数据停用货",
                "quantity": 888,
                "selling_price": 1,
                "canonical_status": "disabled",
            },
        ],
    )
    _make_warehouse_db(
        db_dir / "warehouses" / "wh_003.db",
        [
            # 售价未维护
            {
                "sku": "PP017",
                "name": "冷冻芒果酱",
                "quantity": 4.47,
                "safety_stock": 5,
                "unit_cost": 393,
                "selling_price": 0,
            },
            # 跟随主数据售价
            {
                "sku": "PP051",
                "name": "原味奶浆",
                "quantity": 3,
                "safety_stock": 10,
                "unit_cost": 8,
                "selling_price": 0,
                "price_follow_canonical": 1,
                "canonical_id": 7,
            },
        ],
    )

    master = sqlite3.connect(db_dir / "master.db")
    master.execute("CREATE TABLE warehouses (code TEXT, name TEXT, db_path TEXT)")
    master.execute(
        "CREATE TABLE canonical_items (id INTEGER PRIMARY KEY, sku TEXT, selling_price REAL)"
    )
    master.execute("INSERT INTO canonical_items VALUES (7, 'PP051', 15.5)")
    master.executemany(
        "INSERT INTO warehouses VALUES (?, ?, ?)",
        [
            # 生产里 db_path 相对仓库根目录（形如 db/warehouses/xxx.db），
            # 不是相对库目录，这里照抄真实格式
            ("wh_000", "配送中心仓库", "db/warehouses/wh_000.db"),
            ("wh_003", "新天地店", "db/warehouses/wh_003.db"),
        ],
    )
    master.commit()
    master.close()

    monkeypatch.setenv("DAILYCHECK_DB_DIR", str(db_dir))
    return db_dir


class TestReadOnlyBoundary:
    def test_写操作被_SQLite_拒绝(self, warehouse_env):
        """这是安全底线：即便代码里手滑写了 UPDATE，库也写不进去。"""
        conn = tools._connect_ro(warehouse_env / "warehouses" / "wh_000.db")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("UPDATE items SET quantity = 999")
        conn.close()

    def test_删除也被拒绝(self, warehouse_env):
        conn = tools._connect_ro(warehouse_env / "warehouses" / "wh_000.db")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM items")
        conn.close()


class TestWarehouseResolve:
    def test_精确编码(self, warehouse_env):
        assert tools.resolve_warehouse_code("wh_000") == "wh_000"

    def test_精确名称(self, warehouse_env):
        assert tools.resolve_warehouse_code("新天地店") == "wh_003"

    def test_名称包含_模型多说几个字(self, warehouse_env):
        # 模型常把「配送中心仓库」说成「配送中心」，也要能对上
        assert tools.resolve_warehouse_code("配送中心") == "wh_000"

    def test_未知仓库返回_None_而不是猜(self, warehouse_env):
        assert tools.resolve_warehouse_code("火星仓") is None

    def test_空值(self, warehouse_env):
        assert tools.resolve_warehouse_code(None) is None
        assert tools.resolve_warehouse_code("") is None

    def test_清单带编码和名称(self, warehouse_env):
        brief = tools.warehouse_brief()
        assert {"code": "wh_000", "name": "配送中心仓库"} in brief


class TestItemDetail:
    def test_跨仓汇总总量由服务端算(self, warehouse_env):
        result = tools.get_item_detail("PP017")
        assert result["ok"] is True
        assert result["warehouse_count"] == 2
        assert result["total_quantity"] == pytest.approx(5.67)

    def test_指定仓只返回一个(self, warehouse_env):
        result = tools.get_item_detail("冷冻芒果酱", warehouse_code="新天地店")
        assert result["warehouse_count"] == 1
        assert result["details"][0]["warehouse_code"] == "wh_003"

    def test_停用品项查不到(self, warehouse_env):
        assert tools.get_item_detail("PP099")["ok"] is False
        assert tools.get_item_detail("主数据停用货")["ok"] is False

    def test_缺口与低于安全库存标记(self, warehouse_env):
        detail = tools.get_item_detail("可颂", warehouse_code="wh_000")["details"][0]
        assert detail["below_safety"] is True
        assert detail["shortage"] == pytest.approx(50)

    def test_查不到时提示先搜索(self, warehouse_env):
        result = tools.get_item_detail("不存在的货")
        assert result["error"] == "not_found"
        assert "search_items" in result["message"]


class TestPrice:
    def test_售价有值时返回售价(self, warehouse_env):
        rows = tools.get_price("PP017")["items"]
        prices = {r["warehouse_name"]: r["selling_price"] for r in rows}
        assert prices["配送中心仓库"] == 468

    def test_售价缺失返回_None_不是_0(self, warehouse_env):
        """把未维护的售价当 0 元报出去，是最容易让员工误判的坑。"""
        rows = tools.get_price("PP017")["items"]
        missing = [r for r in rows if r["warehouse_name"] == "新天地店"][0]
        assert missing["selling_price"] is None
        assert missing["price_note"] == "售价未维护"

    def test_成本仍然给出(self, warehouse_env):
        rows = tools.get_price("PP017")["items"]
        assert all(r["unit_cost"] == 393 for r in rows)

    def test_跟随主数据时取主数据价(self, warehouse_env, monkeypatch):
        # 主数据售价 15.5 在 master.db 里，启用开关后应该盖掉本仓的空价
        result = tools.get_price("PP051")
        assert result["items"][0]["selling_price"] == pytest.approx(15.5)
        assert result["items"][0]["price_note"] == "跟随主数据售价"


class TestLowStock:
    def test_按缺口从大到小排(self, warehouse_env):
        alerts = tools.list_low_stock()["alerts"]
        shortages = [a["shortage"] for a in alerts]
        assert shortages == sorted(shortages, reverse=True)

    def test_零库存标记缺货(self, warehouse_env):
        top = tools.list_low_stock()["alerts"][0]
        assert top["sku"] == "PP089"
        assert top["out_of_stock"] is True

    def test_不缺货的不进列表(self, warehouse_env):
        skus = {a["sku"] for a in tools.list_low_stock()["alerts"]}
        assert "PP099" not in skus  # 停用品项即使数量为 0 也不该报

    def test_指定仓(self, warehouse_env):
        alerts = tools.list_low_stock(warehouse_code="新天地店")["alerts"]
        assert {a["warehouse_code"] for a in alerts} == {"wh_003"}


class TestWarehouseItems:
    def test_总数不含停用品项(self, warehouse_env):
        result = tools.list_warehouse_items("配送中心仓库")
        assert result["ok"] is True
        assert result["total_items"] == 2

    def test_未知仓给出可选清单(self, warehouse_env):
        result = tools.list_warehouse_items("火星仓")
        assert result["error"] == "unknown_warehouse"
        assert len(result["available"]) == 2


class TestSearch:
    def test_跨仓去重(self, warehouse_env):
        result = tools.search_items("芒果酱")
        assert result["count"] == 1
        assert len(result["items"][0]["warehouses"]) == 2

    def test_搜不到停用品项(self, warehouse_env):
        assert tools.search_items("停用货")["count"] == 0

    def test_空关键词(self, warehouse_env):
        assert tools.search_items("")["count"] == 0


class TestDbDir:
    def test_环境变量优先(self, warehouse_env, monkeypatch):
        assert tools.db_dir() == warehouse_env.resolve()

    def test_未设置时用仓库内_db(self, monkeypatch):
        monkeypatch.delenv("DAILYCHECK_DB_DIR", raising=False)
        assert tools.db_dir().name == "db"
