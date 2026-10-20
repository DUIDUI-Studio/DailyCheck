"""库存查询工具层 —— 给 LLM 调用的只读工具。

安全边界有三层，缺一不可：

1. **工具集里没有写能力**：本模块只做 SELECT，不 import 任何写 service。
   提示词可以被绕过（员工说「帮我下单」、或更隐蔽的注入），但工具集不会
   凭空多出一个写函数。
2. **数据库以只读模式打开**（``file:...?mode=ro``）：即便将来有人在工具里
   手滑写了 UPDATE，SQLite 也会直接拒绝。
3. **品项可见性复用统一谓词**：停用品项、被主数据停用的品项一律不返回，
   否则员工会查到已经下架的货。

价格取数遵循项目既定语义：
``items.selling_price`` 是本仓售价，``price_follow_canonical=1`` 时改用
``canonical_items.selling_price``（Q6 预留开关，生产当前全为 0）。
两者都为 0 时**不当作 0 元报出去**，而是标记「未维护」——把 0 当价格是最
容易让员工误判的坑。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

# 与 reports_health_pure._visible_items_clause 保持一致：
# 停用行 + 主数据停用行都不对外展示。这里内联一份常量，避免为四个只读查询
# 拉进整个报表模块（那个模块依赖 Flask 配置）。
CANONICAL_DISABLED = "disabled"
VISIBLE_CLAUSE = (
    f"COALESCE(is_active, 1) = 1 AND COALESCE(canonical_status, '') <> '{CANONICAL_DISABLED}'"
)

_REPO_ROOT = Path(__file__).resolve().parent.parent


def db_dir() -> Path:
    """库目录。优先 ``DAILYCHECK_DB_DIR``，便于指向生产快照做验证。"""
    raw = os.environ.get("DAILYCHECK_DB_DIR")
    return Path(raw).expanduser().resolve() if raw else _REPO_ROOT / "db"


def _base_dir() -> Path:
    return db_dir().parent


def _connect_ro(path: Path) -> sqlite3.Connection:
    """只读打开。``mode=ro`` 让写操作在 SQLite 层就被拒，不依赖代码自觉。"""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _resolve_path(stored: str) -> Path:
    """``warehouses.db_path`` 可能是相对路径，相对 BASE_DIR 解析。"""
    path = Path(stored)
    return path if path.is_absolute() else _base_dir() / path


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# 仓库
# ---------------------------------------------------------------------------


def list_warehouses() -> list[dict[str, Any]]:
    """列出所有仓库（code + name）。只返回启用的。"""
    master = db_dir() / "master.db"
    if not master.exists():
        return []
    with _connect_ro(master) as conn:
        rows = conn.execute("SELECT code, name FROM warehouses ORDER BY code").fetchall()
    return [dict(r) for r in rows]


def _warehouse_map() -> dict[str, dict[str, Any]]:
    """code -> {name, db_path}。一次查全，供跨仓遍历用。"""
    master = db_dir() / "master.db"
    if not master.exists():
        return {}
    with _connect_ro(master) as conn:
        rows = conn.execute("SELECT code, name, db_path FROM warehouses ORDER BY code").fetchall()
    return {r["code"]: dict(r) for r in rows}


def resolve_warehouse_code(code_or_name: str | None) -> str | None:
    """仓名 → 编码。模型经常直接传「配送中心仓库」而不是 wh_000，就地容错。

    匹配顺序：精确编码 → 名称完全相等 → 名称互相包含。都不中返回 None，
    让上层当作「未指定仓库」处理，而不是猜一个。
    """
    if not code_or_name:
        return None
    key = code_or_name.strip()
    wh_map = _warehouse_map()
    if key in wh_map:
        return key
    for code, meta in wh_map.items():
        if meta["name"] == key:
            return code
    for code, meta in wh_map.items():
        if key in meta["name"] or meta["name"] in key:
            return code
    return None


def warehouse_brief() -> list[dict[str, str]]:
    """给提示词用的仓库清单（code + name），避免模型为了查编码多跑一轮。"""
    return [{"code": c, "name": m["name"]} for c, m in _warehouse_map().items()]


# ---------------------------------------------------------------------------
# 价格
# ---------------------------------------------------------------------------


def _canonical_selling_price(canonical_id: int | None) -> float | None:
    """主数据售价。仅 ``price_follow_canonical=1`` 时才用得到。"""
    if not canonical_id:
        return None
    master = db_dir() / "master.db"
    if not master.exists():
        return None
    with _connect_ro(master) as conn:
        row = conn.execute(
            "SELECT selling_price FROM canonical_items WHERE id = ?", (canonical_id,)
        ).fetchone()
    return row["selling_price"] if row else None


def resolve_price(item: dict[str, Any]) -> dict[str, Any]:
    """解析一个品项的有效售价与成本。

    返回值里 ``selling_price`` 为 None 表示**未维护**，调用方不要当成 0。
    """
    selling = item.get("selling_price")
    if item.get("price_follow_canonical"):
        canonical_price = _canonical_selling_price(item.get("canonical_id"))
        if canonical_price is not None:
            selling = canonical_price

    cost = item.get("unit_cost")
    return {
        "selling_price": selling if selling and selling > 0 else None,
        "unit_cost": cost if cost and cost > 0 else None,
        "follow_canonical": bool(item.get("price_follow_canonical")),
    }


# ---------------------------------------------------------------------------
# 品项检索
# ---------------------------------------------------------------------------


def _match_items(keyword: str, limit: int = 20) -> list[dict[str, Any]]:
    """跨仓按名称/SKU 模糊匹配，返回去重后的品项骨架。

    员工不会说 SKU，会说「面粉」。这里同时匹配 name 和 sku，且要求
    可见性谓词成立，避免搜到停用货。
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return []
    like = f"%{keyword}%"
    seen: dict[str, dict[str, Any]] = {}

    for meta in _warehouse_map().values():
        path = _resolve_path(meta["db_path"])
        if not path.exists():
            continue
        try:
            with _connect_ro(path) as conn:
                rows = conn.execute(
                    f"SELECT sku, name, unit, category_id FROM items "
                    f"WHERE {VISIBLE_CLAUSE} AND (name LIKE ? OR sku LIKE ?) "
                    f"ORDER BY name LIMIT ?",
                    (like, like, limit),
                ).fetchall()
        except sqlite3.Error:
            continue
        for row in rows:
            entry = seen.setdefault(
                row["sku"],
                {
                    "sku": row["sku"],
                    "name": row["name"],
                    "unit": row["unit"],
                    "warehouses": [],
                },
            )
            entry["warehouses"].append(meta["name"])

    results = sorted(seen.values(), key=lambda x: x["name"])
    return results[:limit]


def _item_detail_in_warehouse(
    code: str, meta: dict[str, Any], sku_or_name: str
) -> dict[str, Any] | None:
    """在一个仓里定位品项并取详情。"""
    path = _resolve_path(meta["db_path"])
    if not path.exists():
        return None
    like = f"%{sku_or_name}%"
    try:
        with _connect_ro(path) as conn:
            row = conn.execute(
                f"SELECT id, sku, name, unit, quantity, safety_stock, unit_cost, "
                f"selling_price, price_follow_canonical, canonical_id, updated_at "
                f"FROM items WHERE {VISIBLE_CLAUSE} AND (sku = ? OR name LIKE ?) "
                f"ORDER BY (sku = ?) DESC, name LIMIT 1",
                (sku_or_name, like, sku_or_name),
            ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None

    item = dict(row)
    price = resolve_price(item)
    quantity = item.get("quantity") or 0
    safety = item.get("safety_stock") or 0
    return {
        "warehouse_code": code,
        "warehouse_name": meta["name"],
        "sku": item["sku"],
        "name": item["name"],
        "unit": item["unit"],
        "quantity": quantity,
        "safety_stock": safety,
        "below_safety": safety > 0 and quantity < safety,
        "shortage": round(max(safety - quantity, 0), 3) if safety > 0 else 0,
        "selling_price": price["selling_price"],
        "unit_cost": price["unit_cost"],
        "price_follows_canonical": price["follow_canonical"],
        "updated_at": item.get("updated_at"),
    }


# ---------------------------------------------------------------------------
# 对外工具（LLM 可调用的四个）
# ---------------------------------------------------------------------------


def search_items(keyword: str, limit: int = 20) -> dict[str, Any]:
    """按名称或 SKU 模糊搜索品项。员工说不准名字时先调这个。"""
    matches = _match_items(keyword, limit=limit)
    return {
        "ok": True,
        "keyword": keyword,
        "count": len(matches),
        "items": matches,
        "hint": "选出唯一一个再调 get_item_detail；多个候选就把候选念给用户确认",
    }


def get_item_detail(sku_or_name: str, warehouse_code: str | None = None) -> dict[str, Any]:
    """查品项在各仓的库存与价格。不给 warehouse_code 就查全部仓。"""
    wh_map = _warehouse_map()
    resolved = resolve_warehouse_code(warehouse_code)
    targets = {resolved: wh_map[resolved]} if resolved else wh_map
    if not targets:
        return {"ok": False, "error": "no_warehouse", "message": "没有可查询的仓库"}

    details = []
    for code, meta in targets.items():
        detail = _item_detail_in_warehouse(code, meta, sku_or_name)
        if detail:
            details.append(detail)

    if not details:
        return {
            "ok": False,
            "error": "not_found",
            "message": f"没有找到「{sku_or_name}」，建议先调 search_items 找近似项",
        }

    total = round(sum(d["quantity"] for d in details), 3)
    return {
        "ok": True,
        "query": sku_or_name,
        "total_quantity": total,
        "warehouse_count": len(details),
        "details": details,
    }


def get_price(sku_or_name: str) -> dict[str, Any]:
    """只查价格（售价 + 成本），不返回库存明细。"""
    detail = get_item_detail(sku_or_name)
    if not detail.get("ok"):
        return detail

    rows = detail["details"]
    return {
        "ok": True,
        "query": sku_or_name,
        "items": [
            {
                "sku": r["sku"],
                "name": r["name"],
                "warehouse_name": r["warehouse_name"],
                "unit": r["unit"],
                "selling_price": r["selling_price"],
                "unit_cost": r["unit_cost"],
                "price_note": (
                    "售价未维护"
                    if r["selling_price"] is None
                    else ("跟随主数据售价" if r["price_follows_canonical"] else None)
                ),
            }
            for r in rows
        ],
    }


def list_warehouse_items(warehouse_code: str, limit: int = 30) -> dict[str, Any]:
    """列某个仓有哪些品项。品项多的仓只给分类概览 + 有库存的前 N 条。

    一个仓动辄上百个品项，全念给员工没有意义。先给「共多少、按分类怎么分」，
    再列库存最多的几条，员工要细节会自己追问。
    """
    wh_map = _warehouse_map()
    resolved = resolve_warehouse_code(warehouse_code)
    meta = wh_map.get(resolved) if resolved else None
    if not meta:
        return {
            "ok": False,
            "error": "unknown_warehouse",
            "message": f"没有「{warehouse_code}」这个仓",
            "available": [{"code": c, "name": m["name"]} for c, m in wh_map.items()],
        }

    path = _resolve_path(meta["db_path"])
    if not path.exists():
        return {"ok": False, "error": "db_missing", "message": f"{resolved} 的库文件不存在"}

    alias_visible = (
        f"COALESCE(i.is_active, 1) = 1 "
        f"AND COALESCE(i.canonical_status, '') <> '{CANONICAL_DISABLED}'"
    )
    try:
        with _connect_ro(path) as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM items i WHERE {alias_visible}").fetchone()[
                0
            ]
            categories = conn.execute(
                f"SELECT COALESCE(c.name, '未分类') AS category, COUNT(*) AS count "
                f"FROM items i LEFT JOIN categories c ON c.id = i.category_id "
                f"WHERE {alias_visible} GROUP BY i.category_id ORDER BY count DESC"
            ).fetchall()
            top_items = conn.execute(
                f"SELECT i.sku, i.name, i.unit, i.quantity, i.safety_stock "
                f"FROM items i WHERE {alias_visible} "
                f"ORDER BY COALESCE(i.quantity, 0) DESC, i.name LIMIT ?",
                (limit,),
            ).fetchall()
    except sqlite3.Error as exc:
        return {"ok": False, "error": "query_failed", "message": str(exc)}

    return {
        "ok": True,
        "warehouse_code": warehouse_code,
        "warehouse_name": meta["name"],
        "total_items": total,
        "categories": [dict(r) for r in categories],
        "top_items": [
            {
                "sku": r["sku"],
                "name": r["name"],
                "unit": r["unit"],
                "quantity": r["quantity"],
                "safety_stock": r["safety_stock"],
            }
            for r in top_items
        ],
        "note": f"只列了库存最多的 {len(top_items)} 条，共 {total} 个品项",
    }


def list_low_stock(warehouse_code: str | None = None, limit: int = 15) -> dict[str, Any]:
    """列出低于安全库存的品项，按缺口从大到小。"""
    wh_map = _warehouse_map()
    resolved = resolve_warehouse_code(warehouse_code)
    targets = {resolved: wh_map[resolved]} if resolved else wh_map
    if not targets:
        return {"ok": False, "error": "no_warehouse", "message": "没有可查询的仓库"}

    alerts = []
    for code, meta in targets.items():
        path = _resolve_path(meta["db_path"])
        if not path.exists():
            continue
        try:
            with _connect_ro(path) as conn:
                rows = conn.execute(
                    f"SELECT sku, name, unit, quantity, safety_stock FROM items "
                    f"WHERE {VISIBLE_CLAUSE} AND COALESCE(safety_stock, 0) > 0 "
                    f"AND COALESCE(quantity, 0) < safety_stock "
                    f"ORDER BY (safety_stock - quantity) DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        except sqlite3.Error:
            continue
        for row in rows:
            alerts.append(
                {
                    "warehouse_code": code,
                    "warehouse_name": meta["name"],
                    "sku": row["sku"],
                    "name": row["name"],
                    "unit": row["unit"],
                    "quantity": row["quantity"],
                    "safety_stock": row["safety_stock"],
                    "shortage": round(row["safety_stock"] - row["quantity"], 3),
                    "out_of_stock": (row["quantity"] or 0) <= 0,
                }
            )

    alerts.sort(key=lambda a: a["shortage"], reverse=True)
    return {
        "ok": True,
        "count": len(alerts),
        "alerts": alerts[:limit],
        "note": "shortage 是距安全库存的缺口，不是建议采购量",
    }
